#!/usr/bin/env node

import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import fs from "node:fs";
import os from "node:os";

import { loadConfig } from "/usr/lib/node_modules/claude-wechat-channel/dist/config.js";
import {
  setDataDir,
  loadAccount,
  saveAccount,
  loginWithQR,
  loadSyncBuf,
  saveSyncBuf,
} from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/auth.js";
import { getUpdates } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/api.js";
import { bodyFromItemList } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/inbound.js";
import { sendMessageWeixin, markdownToPlainText } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/send.js";
import { sendWeixinMediaFile } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/send-media.js";
import { downloadRemoteImageToTemp } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/cdn/upload.js";
import { downloadMediaFromItem } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/media/media-download.js";
import { MessageType, MessageItemType } from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/types.js";
// send-voice.js (ITEM_VOICE) not used: iLink Bot ITEM_VOICE is accepted by API but not rendered
// by personal WeChat clients. Using ITEM_FILE (MP3 attachment) as the verified-reliable path.
import {
  SESSION_EXPIRED_ERRCODE,
  pauseSession,
  getRemainingPauseMs,
} from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/session-guard.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PYTHON = path.join(__dirname, ".venv", "bin", "python");
const CASS_ONCE = path.join(__dirname, "cass_once.py");

const DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000;
const MAX_CONSECUTIVE_FAILURES = 3;
const BACKOFF_DELAY_MS = 30_000;
const RETRY_DELAY_MS = 2_000;
const WEIXIN_MAX_CHARS = 4000;

const LAST_TARGET_FILE = path.join(__dirname, "logs", "last-wechat-target.json");
const SEND_TRACE_FILE = path.join(__dirname, "logs", "wechat-send-trace.jsonl");

const RECV_MEDIA_TMP = path.join(os.tmpdir(), "cass-weixin-recv");
const SEND_MEDIA_TMP = path.join(os.tmpdir(), "cass-weixin-send");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function readBackendMode() {
  try {
    const p = path.join(process.env.HOME, "claude-imprint", "data", "backend_mode.json");
    const data = JSON.parse(fs.readFileSync(p, "utf8"));
    return data.mode || "api";
  } catch (_) {
    return "api";
  }
}

function saveLastWechatTarget({ to, contextToken }) {
  if (!to || !contextToken) return;

  try {
    fs.mkdirSync(path.dirname(LAST_TARGET_FILE), { recursive: true });
    fs.writeFileSync(
      LAST_TARGET_FILE,
      JSON.stringify(
        {
          to,
          contextToken,
          updatedAt: new Date().toISOString(),
        },
        null,
        2
      ),
      "utf8"
    );
    console.log(`[target] saved last wechat target to=${to}`);
  } catch (err) {
    console.error(`[target] failed to save last target: ${String(err)}`);
  }
}

function stripMarkdown(text) {
  return text
    .replace(/\*\*(.+?)\*\*/gs, "$1")
    .replace(/\*(.+?)\*/gs, "$1")
    .replace(/^-{3,}\s*$/gm, "")
    .replace(/^[-*+]\s+/gm, "")
    .replace(/^\d+\.\s+/gm, "")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function splitMessage(text) {
  if (text.length <= WEIXIN_MAX_CHARS) return [text];

  const chunks = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= WEIXIN_MAX_CHARS) {
      chunks.push(remaining);
      break;
    }

    let splitAt = remaining.lastIndexOf("\n", WEIXIN_MAX_CHARS);
    if (splitAt <= 0 || splitAt < WEIXIN_MAX_CHARS * 0.5) {
      splitAt = WEIXIN_MAX_CHARS;
    }

    chunks.push(remaining.substring(0, splitAt));
    remaining = remaining.substring(splitAt).trimStart();
  }

  return chunks;
}

function groupShortLinesIntoBubbles(lines) {
  // 一次回复最多 3 条气泡。
  // 原则：每条气泡表达一个完整意思，不把每个短句都拆成独立气泡。
  const bubbles = [];

  for (const line of lines) {
    const current = bubbles[bubbles.length - 1] || "";

    if (!current) {
      bubbles.push(line);
      continue;
    }

    const merged = `${current}\n${line}`;

    // 如果当前气泡还很短，就合并，让“明白了/我改”这种短句不要刷屏。
    if (current.length <= 28 && merged.length <= 70) {
      bubbles[bubbles.length - 1] = merged;
      continue;
    }

    if (bubbles.length < 3) {
      bubbles.push(line);
    } else {
      // 超过 3 条后并入最后一条，避免太吵。
      bubbles[bubbles.length - 1] = `${bubbles[bubbles.length - 1]}\n${line}`;
    }
  }

  return bubbles;
}

function splitSemanticMessages(text) {
  const trimmed = text.trim();
  if (!trimmed) return [];

  const looksTechnical =
    trimmed.includes("```") ||
    trimmed.includes("cd ") ||
    trimmed.includes("python") ||
    trimmed.includes("systemctl") ||
    trimmed.includes("grep ") ||
    trimmed.includes("cat ") ||
    trimmed.includes("=>") ||
    trimmed.includes("{") ||
    trimmed.includes("}");

  if (looksTechnical) return [trimmed];

  // 1. Explicit split marker from model.
  let parts = trimmed
    .split(/\n\s*(?:---|<split>)\s*\n/g)
    .map((x) => x.trim())
    .filter(Boolean);

  if (parts.length > 1) {
    return groupShortLinesIntoBubbles(parts);
  }

  // 2. Blank-line paragraph split.
  parts = trimmed
    .split(/\n\s*\n+/g)
    .map((x) => x.trim())
    .filter(Boolean);

  const paragraphSplitOK =
    parts.length >= 2 &&
    parts.length <= 6 &&
    parts.every((x) => x.length <= 120) &&
    trimmed.length <= 420;

  if (paragraphSplitOK) {
    return groupShortLinesIntoBubbles(parts);
  }

  // 3. Single-newline short-line split.
  // 只处理短私聊风格，不处理长解释。
  parts = trimmed
    .split(/\n+/g)
    .map((x) => x.trim())
    .filter(Boolean);

  const lineSplitOK =
    parts.length >= 2 &&
    parts.length <= 8 &&
    parts.every((x) => x.length <= 100) &&
    trimmed.length <= 420;

  if (lineSplitOK) {
    return groupShortLinesIntoBubbles(parts);
  }

  return [trimmed];
}

function appendSendTrace({ source, to, rawText, plain, chunks }) {
  try {
    fs.mkdirSync(path.dirname(SEND_TRACE_FILE), { recursive: true });
    fs.appendFileSync(
      SEND_TRACE_FILE,
      JSON.stringify(
        {
          ts: new Date().toISOString(),
          source,
          to,
          raw_text: rawText,
          plain,
          chunk_count: chunks.length,
          chunks,
        },
        null,
        0
      ) + "\n",
      "utf8"
    );
  } catch (err) {
    console.error(`[trace] failed to write send trace: ${String(err)}`);
  }
}

function parseVoiceMarkers(text) {
  const voices = [];
  let clean = text;
  clean = clean.replace(/\[VOICE:([^\]]+)\]/g, (_, t) => {
    voices.push({ lang: "zh", text: t.trim() });
    return "";
  });
  clean = clean.replace(/\[VOICE_EN:([^\]]+)\]/g, (_, t) => {
    voices.push({ lang: "en", text: t.trim() });
    return "";
  });
  return { text: clean.trim(), voices };
}

async function fetchTtsAudio(text, lang) {
  const resp = await fetch("http://127.0.0.1:8101/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!resp.ok) throw new Error(`TTS HTTP ${resp.status}`);
  const data = await resp.json();
  if (!data?.audio_base64) throw new Error("TTS: no audio_base64");
  return Buffer.from(data.audio_base64, "base64");
}

function silkEncodeViaScript(mp3Path, silkPath) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [path.join(__dirname, "silk_encode.py"), mp3Path, silkPath], {
      cwd: __dirname,
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (d) => { stdout += d.toString(); });
    child.stderr.on("data", (d) => { stderr += d.toString(); });
    child.on("close", (code) => {
      if (code !== 0) return reject(new Error(`silk_encode.py failed (${code}): ${stderr}`));
      const durationMs = parseInt(stdout.trim(), 10) || 3000;
      resolve(durationMs);
    });
  });
}

async function sendVoiceReply({ baseUrl, token, to, contextToken, cdnBaseUrl, text, lang }) {
  console.log(`[voice] TTS lang=${lang} text=${JSON.stringify(text.substring(0, 60))}`);
  const ts = Date.now();
  const mp3Path = path.join(os.tmpdir(), `cass-voice-${ts}.mp3`);

  const mp3Buf = await fetchTtsAudio(text, lang);
  await fs.promises.writeFile(mp3Path, mp3Buf);

  // ITEM_VOICE is silently dropped by WeChat personal clients on the public iLink path.
  // Send as ITEM_FILE (MP3) instead — verified to deliver reliably.
  await sendWeixinMediaFile({
    filePath: mp3Path,
    to,
    opts: { baseUrl, token, contextToken },
    cdnBaseUrl,
  });

  try { await fs.promises.unlink(mp3Path); } catch (_) {}
  console.log(`[voice] sent as MP3 file to=${to} lang=${lang}`);
}

function parseImageMarkers(text) {
  const images = [];
  let clean = text;
  clean = clean.replace(/\[IMAGE_URL:([^\]]+)\]/g, (_, url) => {
    images.push({ type: "url", value: url.trim() });
    return "";
  });
  clean = clean.replace(/\[IMAGE_FILE:([^\]]+)\]/g, (_, fp) => {
    images.push({ type: "file", value: fp.trim() });
    return "";
  });
  return { text: clean.trim(), images };
}

async function sendReplyWithMedia({ baseUrl, token, to, contextToken, cdnBaseUrl, text }) {
  // Strip voice markers first, then image markers
  const { text: textAfterVoice, voices } = parseVoiceMarkers(text);
  const { text: cleanText, images } = parseImageMarkers(textAfterVoice);

  if (cleanText) {
    await sendReply({ baseUrl, token, to, contextToken, text: cleanText });
  }

  for (const img of images) {
    let filePath;
    if (img.type === "url") {
      try {
        filePath = await downloadRemoteImageToTemp(img.value, SEND_MEDIA_TMP);
        console.log(`[media] downloaded image url=${img.value} -> ${filePath}`);
      } catch (err) {
        console.error(`[media] image download failed url=${img.value}: ${String(err)}`);
        continue;
      }
    } else {
      filePath = img.value;
    }

    try {
      await sendWeixinMediaFile({
        filePath,
        to,
        opts: { baseUrl, token, contextToken },
        cdnBaseUrl,
      });
      console.log(`[media] image sent to=${to} file=${filePath}`);
    } catch (err) {
      console.error(`[media] image send failed file=${filePath}: ${String(err)}`);
    }
  }

  for (const v of voices) {
    try {
      await sendVoiceReply({ baseUrl, token, to, contextToken, cdnBaseUrl, text: v.text, lang: v.lang });
    } catch (err) {
      console.error(`[voice] failed lang=${v.lang}: ${String(err)}`);
    }
  }
}

function askCassViaPython(text) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [CASS_ONCE], {
      cwd: __dirname,
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });

    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    child.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    child.on("error", reject);

    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(`cass_once.py exited with code ${code}: ${stderr}`));
        return;
      }
      resolve(stdout.trim());
    });

    child.stdin.write(text);
    child.stdin.end();
  });
}

async function sendReply({ baseUrl, token, to, contextToken, text }) {
  const plain = stripMarkdown(markdownToPlainText(text));
  const semanticParts = splitSemanticMessages(plain);
  const chunks = semanticParts.flatMap((part) => splitMessage(part));

  appendSendTrace({
    source: "standalone",
    to,
    rawText: text,
    plain,
    chunks,
  });

  for (const chunk of chunks) {
    await sendMessageWeixin({
      to,
      text: chunk,
      opts: {
        baseUrl,
        token,
        contextToken,
      },
    });

    // More human pacing between separate bubbles.
    if (chunks.length > 1) {
      await sleep(1200);
    }
  }

  console.log(`[reply] sent to=${to} chunks=${chunks.length}`);
}

async function main() {
  const config = loadConfig();
  setDataDir(config.dataDir);

  const baseUrl = config.weixinBaseUrl;
  const cdnBaseUrl = config.weixinCdnBaseUrl;

  let account = loadAccount();
  if (!account?.token || !account?.accountId) {
    console.log("[login] 未找到已保存的微信账号，开始扫码登录...");
    const result = await loginWithQR({ apiBaseUrl: baseUrl });

    if (!result.connected || !result.botToken || !result.accountId) {
      throw new Error(`登录失败: ${result.message}`);
    }

    saveAccount(result.accountId, {
      token: result.botToken,
      baseUrl: result.baseUrl,
      userId: result.userId,
    });

    console.log(`[login] ${result.message}`);
    account = loadAccount();
  }

  if (!account?.token || !account?.accountId) {
    throw new Error("无法加载微信账号信息");
  }

  console.log(`[start] Cass Weixin standalone started`);
  console.log(`[start] accountId=${account.accountId}`);
  console.log(`[start] baseUrl=${baseUrl}`);

  let getUpdatesBuf = loadSyncBuf();
  if (getUpdatesBuf) {
    console.log(`[sync] resuming from previous sync buf (${getUpdatesBuf.length} bytes)`);
  } else {
    console.log(`[sync] no previous sync buf, starting fresh`);
  }

  let nextTimeoutMs = DEFAULT_LONG_POLL_TIMEOUT_MS;
  let consecutiveFailures = 0;

  while (true) {
    try {
      const resp = await getUpdates({
        baseUrl,
        token: account.token,
        get_updates_buf: getUpdatesBuf,
        timeoutMs: nextTimeoutMs,
      });

      if (resp.longpolling_timeout_ms != null && resp.longpolling_timeout_ms > 0) {
        nextTimeoutMs = resp.longpolling_timeout_ms;
      }

      const isApiError =
        (resp.ret !== undefined && resp.ret !== 0) ||
        (resp.errcode !== undefined && resp.errcode !== 0);

      if (isApiError) {
        const isSessionExpired =
          resp.errcode === SESSION_EXPIRED_ERRCODE ||
          resp.ret === SESSION_EXPIRED_ERRCODE;

        if (isSessionExpired) {
          pauseSession(account.accountId);
          const pauseMs = getRemainingPauseMs(account.accountId);
          console.error(`[wechat] session expired, pausing ${Math.ceil(pauseMs / 60_000)} min`);
          consecutiveFailures = 0;
          await sleep(pauseMs);
          continue;
        }

        consecutiveFailures += 1;
        console.error(`[wechat] getUpdates failed ret=${resp.ret} errcode=${resp.errcode} errmsg=${resp.errmsg ?? ""}`);

        if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
          consecutiveFailures = 0;
          await sleep(BACKOFF_DELAY_MS);
        } else {
          await sleep(RETRY_DELAY_MS);
        }

        continue;
      }

      consecutiveFailures = 0;

      if (resp.get_updates_buf != null && resp.get_updates_buf !== "") {
        saveSyncBuf(resp.get_updates_buf);
        getUpdatesBuf = resp.get_updates_buf;
      }

      const msgs = resp.msgs ?? [];

      for (const msg of msgs) {
        if (msg.message_type === MessageType.BOT) continue;

        const fromUserId = msg.from_user_id ?? "";
        if (!fromUserId) continue;

        const contextToken = msg.context_token;
        if (!contextToken) {
          console.warn(`[skip] no contextToken from=${fromUserId}`);
          continue;
        }

        // Extract text body (also picks up voice transcription if available)
        
console.log("========== RAW MSG ==========");
console.log(JSON.stringify(msg, null, 2));
console.log("================================");

let inputText = bodyFromItemList(msg.item_list);


        // Download received images and prepend markers
        for (const item of (msg.item_list ?? [])) {
          if (item.type === MessageItemType.IMAGE) {
            try {
              const result = await downloadMediaFromItem(item, {
                cdnBaseUrl,
                tmpDir: RECV_MEDIA_TMP,
                label: `recv-img:${fromUserId}`,
              });
              if (result.decryptedPicPath) {
                inputText = `[RECEIVED_IMAGE:${result.decryptedPicPath}]${inputText ? "\n" + inputText : ""}`;
                console.log(`[inbound] image saved: ${result.decryptedPicPath}`);
              }
            } catch (err) {
              console.error(`[inbound] image download failed: ${String(err)}`);
            }
          }
        }

        // Voice with no transcription: pass a placeholder so Cass can acknowledge
        if (!inputText) {
          for (const item of (msg.item_list ?? [])) {
            if (item.type === MessageItemType.VOICE) {
        console.log("========== REAL VOICE MSG ==========");
        console.log(JSON.stringify(msg, null, 2));
        console.log("====================================");
              inputText = "[语音消息，无文字转写]";
              break;
            }
          }
        }

        if (!inputText) {
          console.log(`[skip] non-text/non-image message from=${fromUserId}`);
          continue;
        }

        saveLastWechatTarget({ to: fromUserId, contextToken });

        console.log(`[inbound] from=${fromUserId} text=${JSON.stringify(inputText.substring(0, 120))}`);
        // 消息 buffer：10秒内合并多条消息再统一回复
        if (!global._msgBuffer) global._msgBuffer = {};
        if (!global._msgBuffer[fromUserId]) {
          global._msgBuffer[fromUserId] = { texts: [], contextToken, timer: null };
        }
        global._msgBuffer[fromUserId].texts.push(inputText);
        global._msgBuffer[fromUserId].contextToken = contextToken;
        if (global._msgBuffer[fromUserId].timer) {
          clearTimeout(global._msgBuffer[fromUserId].timer);
        }
        const _cap = { baseUrl, token: account.token, to: fromUserId, cdnBaseUrl };
        global._msgBuffer[fromUserId].timer = setTimeout(async () => {
          const buf = global._msgBuffer[fromUserId];
          delete global._msgBuffer[fromUserId];
          const combined = buf.texts.join("\n");
          console.log(`[buffer] flushing ${buf.texts.length} msg(s) from=${fromUserId}`);
          let reply;
          try {
            reply = await askCassViaPython(combined);
          } catch (err) {
            console.error(`[cass] failed: ${String(err)}`);
            reply = "调用失败。再发一次。";
          }
          if (!reply || !reply.trim()) {
            const backendMode = readBackendMode();

            // Claude Web tool-dispatch mode:
            // cass_once.py intentionally prints nothing because Claude has already called send_wechat.
            // Do not send fallback text, or WeChat will receive a second bogus message.
            if (backendMode === "claude_web") {
              console.log("[reply] empty stdout in claude_web tool-dispatch mode; skip fallback send");
              return;
            }

            reply = "刚才没生成有效回复。";
          }

          try {
            await sendReplyWithMedia({
              baseUrl: _cap.baseUrl,
              token: _cap.token,
              to: _cap.to,
              contextToken: buf.contextToken,
              cdnBaseUrl: _cap.cdnBaseUrl,
              text: reply,
            });
          } catch (err) {
            console.error(`[reply] failed to=${_cap.to}: ${String(err)}`);
          }
        }, 10000);
      }
    } catch (err) {
      consecutiveFailures += 1;

      console.error(`[loop] error (${consecutiveFailures}/${MAX_CONSECUTIVE_FAILURES}): ${String(err)}`);

      if (consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
        consecutiveFailures = 0;
        await sleep(BACKOFF_DELAY_MS);
      } else {
        await sleep(RETRY_DELAY_MS);
      }
    }
  }
}

main().catch((err) => {
  console.error(`[fatal] ${String(err)}`);
  process.exit(1);
});
