#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { loadConfig } from "/usr/lib/node_modules/claude-wechat-channel/dist/config.js";
import {
  setDataDir,
  loadAccount,
} from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/auth.js";
import {
  sendMessageWeixin,
  markdownToPlainText,
} from "/usr/lib/node_modules/claude-wechat-channel/dist/weixin/send.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const WEIXIN_MAX_CHARS = 4000;
const TARGET_FILE = path.join(__dirname, "logs", "last-wechat-target.json");

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
  const bubbles = [];

  for (const line of lines) {
    const current = bubbles[bubbles.length - 1] || "";

    if (!current) {
      bubbles.push(line);
      continue;
    }

    const merged = `${current}\n${line}`;

    if (current.length <= 28 && merged.length <= 70) {
      bubbles[bubbles.length - 1] = merged;
      continue;
    }

    if (bubbles.length < 3) {
      bubbles.push(line);
    } else {
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

  let parts = trimmed
    .split(/\n\s*(?:---|<split>)\s*\n/g)
    .map((x) => x.trim())
    .filter(Boolean);

  if (parts.length > 1) return groupShortLinesIntoBubbles(parts);

  parts = trimmed
    .split(/\n\s*\n+/g)
    .map((x) => x.trim())
    .filter(Boolean);

  if (
    parts.length >= 2 &&
    parts.length <= 6 &&
    parts.every((x) => x.length <= 120) &&
    trimmed.length <= 420
  ) {
    return groupShortLinesIntoBubbles(parts);
  }

  parts = trimmed
    .split(/\n+/g)
    .map((x) => x.trim())
    .filter(Boolean);

  if (
    parts.length >= 2 &&
    parts.length <= 8 &&
    parts.every((x) => x.length <= 100) &&
    trimmed.length <= 420
  ) {
    return groupShortLinesIntoBubbles(parts);
  }

  return [trimmed];
}

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data.trim()));
  });
}

async function main() {
  const argText = process.argv.slice(2).join(" ").trim();
  const stdinText = argText ? "" : await readStdin();
  const text = (argText || stdinText).trim();

  if (!text) {
    throw new Error("No message text provided via argv or stdin.");
  }

  if (!fs.existsSync(TARGET_FILE)) {
    throw new Error(`No saved Weixin target found: ${TARGET_FILE}. Send one Weixin message to Cass first.`);
  }

  const target = JSON.parse(fs.readFileSync(TARGET_FILE, "utf8"));

  if (!target.to || !target.contextToken) {
    throw new Error("Saved Weixin target is missing to/contextToken.");
  }

  const config = loadConfig();
  setDataDir(config.dataDir);

  const account = loadAccount();
  if (!account?.token) {
    throw new Error("No saved Weixin account token found.");
  }

  const plain = stripMarkdown(markdownToPlainText(text));
  const semanticParts = splitSemanticMessages(plain);
  const chunks = semanticParts.flatMap((part) => splitMessage(part));

  for (const chunk of chunks) {
    await sendMessageWeixin({
      to: target.to,
      text: chunk,
      opts: {
        baseUrl: config.weixinBaseUrl,
        token: account.token,
        contextToken: target.contextToken,
      },
    });

    if (chunks.length > 1) {
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }

  console.log(`[send_wechat_once] sent chunks=${chunks.length} to=${target.to}`);
}

main().catch((err) => {
  console.error(`[send_wechat_once] failed: ${String(err)}`);
  process.exit(1);
});
