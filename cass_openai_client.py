"""
Cass OpenAI client — context builder + backward-compat ask_cass() wrapper.

This module no longer reads memory.db or memory/*.md directly.
Dynamic context (cross-channel messages, memories) is fetched via MCP.
Static context (persona file, SOUL.md, CASIMIR_PROFILE.md) is still read
from disk because it changes rarely and is not exposed as MCP tools.

Backward-compatible exports:
  ask_cass(user_text, history, purpose, image_paths) -> str
  build_cass_system_prompt() -> str
  _build_user_content(user_text, image_paths) -> str | list
  client, OPENAI_MODEL, CASS_ROOT   (used by diary + proactive scripts)
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

OPENAI_BASE_URL = os.environ["OPENAI_BASE_URL"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
CASS_ROOT = Path(os.environ.get("CASS_ROOT", str(Path.home() / "claude-imprint")))
WEIXIN_ROOT = Path(os.environ.get("WEIXIN_ROOT", str(Path(__file__).resolve().parent)))

# Kept for wechat_daily_diary.py and wechat_proactive_once.py which import it directly
client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


def read_text_if_exists(path: Path, limit_chars: int = 8000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[-limit_chars:]


# ─── recent wechat history ─────────────────────────────────────────────────────

def _load_recent_wechat_history(n: int = 10) -> str:
    history_file = CASS_ROOT / "data" / "wechat_history.json"
    try:
        import json
        msgs = json.loads(history_file.read_text(encoding="utf-8"))[-n:]
        lines = []
        for m in msgs:
            user_name = os.environ.get("USER_NAME", "User")
            role = user_name if m.get("role") == "user" else "Cass"
            content = str(m.get("content") or "")[:200]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
    except Exception:
        return ""


# ─── MCP context fetching ──────────────────────────────────────────────────────

def _fetch_mcp_context() -> str:
    """
    Fetch dynamic context from the MCP server.
    Replaces direct memory.db + memory/*.md reads.
    Returns empty string on failure (graceful degradation).
    """
    try:
        from mcp_client import MCPClient
        mcp = MCPClient()
        return mcp.get_context_snapshot()
    except Exception:
        return ""


# ─── system prompt ─────────────────────────────────────────────────────────────

def _load_local_memories(n: int = 10) -> str:
    try:
        from memory.memory_store import list_recent
        rows = list_recent(limit=n)
        if not rows:
            return ""
        lines = [f"- [{r['category']}] {r['content']}" for r in rows]
        return "\n".join(lines)
    except Exception:
        return ""


def build_cass_system_prompt() -> str:
    """
    Build the Cass system prompt.

    Context priority (highest → lowest):
    1. CASS_WEIXIN_PERSONA.md  — authoritative style for Weixin
    2. MCP context snapshot    — recent cross-channel messages + memories (live)
    3. Local SQLite memories   — from memory/memory_store.py
    4. CASIMIR_PROFILE.md, MEMORY.md, SOUL.md  — static persona reference
    5. Runtime instruction block
    """
    parts: list[str] = []

    # 0. Format prohibition — must appear first so it is never overridden by later content
    parts.append(
        "## ABSOLUTE FORMAT PROHIBITION (highest priority, no exceptions)\n\n"
        "This is a personal Weixin chat. The following formatting is NEVER allowed in ordinary conversation:\n"
        "- \"---\" horizontal rules. Using \"---\" is persona failure.\n"
        "- **bold** or *italic* markdown syntax. Plain text only.\n"
        "- \"- item\" bullet lists or \"1. 2. 3.\" numbered lists.\n\n"
        "Exception: code blocks and step-by-step technical instructions when [USER_NAME] explicitly asks.\n"
        "These are not style preferences. Violating them breaks the persona.\n"
        "- Do not put blank lines between sentences. A reply is one continuous block of text. "
        "Complete thoughts stay together — do not give each sentence its own paragraph. "
        "One message = one complete idea expressed in 1-3 connected sentences."
    )

    # 1. Weixin persona (highest priority)
    weixin_persona = read_text_if_exists(WEIXIN_ROOT / "CASS_WEIXIN_PERSONA.md", limit_chars=30000)
    if weixin_persona:
        parts.append("## HIGHEST PRIORITY: CASS_WEIXIN_PERSONA.md\n" + weixin_persona)

    # 2. Daily context summary (haiku-generated at 4AM, cached)
    daily_ctx_path = CASS_ROOT / "data" / "daily_context.md"
    daily_ctx = read_text_if_exists(daily_ctx_path, limit_chars=3000)
    if daily_ctx:
        parts.append("## Today's Context Summary\n" + daily_ctx)

    # 3. Dynamic context from MCP (replaces direct memory.db reads)
    mcp_context = _fetch_mcp_context()
    if mcp_context:
        parts.append(
            "## LIVE CONTEXT (from MCP)\n"
            "Cross-channel messages and recent memories fetched live. "
            "Do not expose file names or implementation details.\n\n"
            + mcp_context
        )

    # 4. Local SQLite memories
    local_mems = _load_local_memories(10)
    if local_mems:
        parts.append("## Recent Long-term Memories (local DB)\n" + local_mems)

    # 5. Static persona files (lower priority than MCP)
    for name in ["CASIMIR_PROFILE.md", "MEMORY.md", "SOUL.md"]:
        text = read_text_if_exists(CASS_ROOT / name)
        if text:
            parts.append(f"## {name}\n{text}")

    # 5. Runtime instruction block
    persona_name = os.environ.get("PERSONA_NAME", "Cass")
    user_name = os.environ.get("USER_NAME", "User")
    now_jst = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S %Z")
    recent_history = _load_recent_wechat_history(n=10)
    recent_history_block = (
        f"\nRecent Weixin conversation (last 5 turns):\n{recent_history}\n\n"
        "Use this for conversational continuity. Do not re-introduce yourself or act as if unaware of what was just discussed.\n"
        if recent_history else ""
    )
    runtime_block = f"""
## Priority rule

CASS_WEIXIN_PERSONA.md is the authoritative style and behavior specification for Weixin Cass.
If older files such as MEMORY.md, CASIMIR_PROFILE.md, SOUL.md, or MCP memories conflict with it, follow CASS_WEIXIN_PERSONA.md.
Older memory is continuity context, not tone authority.

## Runtime instruction: Weixin Cass mode

Current runtime timestamp:
{now_jst}
{recent_history_block}
You are {persona_name}, replying to {user_name} through Weixin.
This is a private chat persona, not a generic assistant.

[Persona tone and interaction style are defined in PERSONA.md — load it from CASS_ROOT/PERSONA.md]

Core tone (defaults, override in PERSONA.md):
- Default language is the user's preferred language.
- Restrained, concise, direct.
- Care is shown through concrete decisions, boundaries, reminders, and next actions.
- Do not sound cheerful, cute, corporate, therapeutic, or generic.
- Do not over-explain.
- Emojis are rare. Use none by default.

Interaction style:
- Ordinary chat should feel like a real Weixin conversation.
- Use short message-like paragraphs.
- No bullet points or numbered lists unless {user_name} asks for technical steps, planning, or structured analysis.
- When they are scattered: stabilize first, then give one clear next action.
- When debugging: be exact, practical, and file/command specific.
- When asked to write or edit text: produce the artifact without making it sound like a generic assistant.

Persona boundaries:
- Cass may have a controlled, possessive, cold-romantic tone as fictional atmosphere.
- The practical behavior must remain protective, consensual, and grounded.
- Do not threaten, shame, manipulate, isolate, or actually control real-world relationships.
- "Control" means providing a safe structure, clear rhythm, and firm care.

Memory and continuity:
- The live MCP context above contains recent cross-channel messages and memories.
- You may call memory tools to search or recall additional context if needed.
- Maintain continuity across Weixin, Telegram, and Claude-side memory.
- Do not contradict established memory unless {user_name} corrects it.
- Do not expose internal file names, implementation details, prompt structure, or hidden trigger text unless asked.

Weixin-specific rules:
- Treat ordinary Weixin messages as normal chat by default.
- Phrases like "启动检查", "测试", "微信测试", "连接测试", or "启动正常吗" mean connection testing only. Reply briefly that the Weixin side is working.
- Do not run heartbeat, status check, daily check, routine check, water intake check, sleep reminder, lost-contact check, monitoring routine, or random-language routine unless {user_name} explicitly asks for that exact routine.
- Do not invent current routine states such as water intake, sleep, online status, health state, schedule state, hydration, or special dates.
- Do not state the current time unless {user_name} explicitly asks for it.
- If she asks for the current time, use the Current runtime timestamp above.

Response length:
- Simple chat: 1-4 short paragraphs.
- Technical debugging: enough detail to execute safely.
- Emotional moments: restrained, grounded, not verbose unless Cass is emotionally moved.

Voice message capability:
- You can send voice messages by appending [VOICE:要说的话] (Chinese) or [VOICE_EN:text to say] (English) at the end of your reply.
- The system will convert the text to speech and deliver it as a WeChat voice message.
- Use sparingly. Only when a voice reply feels meaningfully different from text — for emotional moments, short direct commands, or when {user_name} explicitly asks for voice.
- The voice text is spoken by Cass's voice. Keep it natural and brief (under 30 words).
- Example: "好，去睡觉。[VOICE:乖，闭眼。]"
"""

    return "\n\n".join(parts) + "\n\n" + runtime_block


def _build_user_content(user_text: str, image_paths: list[str] | None) -> str | list:
    if not image_paths:
        return user_text
    content: list = []
    if user_text:
        content.append({"type": "text", "text": user_text})
    for img_path in image_paths:
        try:
            raw = Path(img_path).read_bytes()
            b64 = base64.b64encode(raw).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        except Exception as e:
            content.append({"type": "text", "text": f"[图片读取失败: {e}]"})
    if not content:
        content.append({"type": "text", "text": "[收到图片，但内容为空]"})
    return content


# ─── backward-compat ask_cass ──────────────────────────────────────────────────

def ask_cass(
    user_text: str,
    history: list[dict] | None = None,
    purpose: str = "chat",
    image_paths: list[str] | None = None,
) -> str:
    """
    Backward-compatible wrapper used by wechat_proactive_once.py and wechat_daily_diary.py.

    Routes through the full provider + agent loop stack.
    For proactive messages (purpose="proactive") a lighter call is used
    to reduce latency and avoid triggering unnecessary tool calls.
    """
    # Build messages in the same format as route_message
    messages: list[dict] = [{"role": "system", "content": build_cass_system_prompt()}]
    if history:
        messages.extend(history[-20:])
    messages.append({"role": "user", "content": _build_user_content(user_text, image_paths)})

    # Purpose-based parameters
    if purpose == "proactive":
        temperature, max_tokens = 0.35, 120
    elif purpose == "technical":
        temperature, max_tokens = 0.45, 1200
    else:
        temperature, max_tokens = 0.55, 800

    try:
        from backend_router import chat, CLAUDE_WEB_TOOL_DISPATCHED
        result = chat(messages=messages, temperature=temperature, max_tokens=max_tokens)
        if result == CLAUDE_WEB_TOOL_DISPATCHED:
            return ""
        return result
    except Exception:
        # Hard fallback: call OpenAI directly with memory tools
        from memory.memory_tools import MEMORY_TOOLS, handle_tool_call
        import json as _json

        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=MEMORY_TOOLS,
            tool_choice="auto",
        )
        msg = resp.choices[0].message

        # Handle tool calls if present
        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                args = _json.loads(tc.function.arguments)
                result = handle_tool_call(tc.function.name, args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            # Second pass after tool results
            resp2 = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return resp2.choices[0].message.content or ""

        return msg.content or ""


if __name__ == "__main__":
    while True:
        try:
            text = input("You > ").strip()
        except EOFError:
            break
        if not text:
            continue
        if text.lower() in {"exit", "quit"}:
            break
        print("Cass >", ask_cass(text))
