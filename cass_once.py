import re
import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from cass_openai_client import CASS_ROOT
from backend_router import route_message, CLAUDE_WEB_TOOL_DISPATCHED

_RECEIVED_IMAGE_RE = re.compile(r'\[RECEIVED_IMAGE:([^\]]+)\]')

def strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'^-{3,}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def parse_received_images(text: str) -> tuple[str, list[str]]:
    """Strip [RECEIVED_IMAGE:path] markers, return (clean_text, image_paths)."""
    image_paths = []
    def _collect(m):
        image_paths.append(m.group(1).strip())
        return ""
    clean = _RECEIVED_IMAGE_RE.sub(_collect, text).strip()
    return clean, image_paths

HISTORY_FILE = CASS_ROOT / "data" / "wechat_history.json"
MAX_HISTORY = 20

def load_history() -> list:
    if not HISTORY_FILE.exists():
        return []
    try:
        import json
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))[-MAX_HISTORY:]
    except Exception:
        return []

def save_history(history: list):
    import json
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history[-MAX_HISTORY:], ensure_ascii=False, indent=2), encoding="utf-8")

def append_wechat_raw_log(user_text: str, cass_text: str) -> None:
    memory_dir = CASS_ROOT / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = memory_dir / f"wechat_{now.strftime('%Y-%m-%d')}.md"
    block = f"""
## {now.strftime('%Y-%m-%d %H:%M:%S')} Weixin
User:
{user_text}
Cass:
{cass_text}
---
"""
    with path.open("a", encoding="utf-8") as f:
        f.write(block)

def append_wechat_raw_event(direction: str, content: str) -> None:
    memory_dir = CASS_ROOT / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = memory_dir / f"wechat_{now.strftime('%Y-%m-%d')}.md"

    if direction == "in":
        block = f"""
## {now.strftime('%Y-%m-%d %H:%M:%S')} Weixin In
User:
{content}
---
"""
    else:
        block = f"""
## {now.strftime('%Y-%m-%d %H:%M:%S')} Weixin Out
Cass:
{content}
---
"""

    with path.open("a", encoding="utf-8") as f:
        f.write(block)


def bus_post(source: str, direction: str, content: str):
    db_path = CASS_ROOT / "memory.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO message_bus (created_at, source, direction, content) VALUES (?, ?, ?, ?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M"), source, direction, content)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[bus_post error] {e}", file=sys.stderr)

def is_connection_test(user_text: str) -> bool:
    normalized = (
        user_text.strip()
        .replace("。", "")
        .replace("，", "")
        .replace(",", "")
        .replace(" ", "")
        .lower()
    )
    test_phrases = {
        "启动检查", "测试", "微信测试", "连接测试",
        "cass微信测试", "cass启动检查",
    }
    return normalized in test_phrases

def main():
    raw_input = sys.stdin.read().strip()
    if not raw_input:
        return

    user_text, image_paths = parse_received_images(raw_input)

    if is_connection_test(user_text) and not image_paths:
        cass_text = "微信侧连接正常，我在。"
        print(cass_text)
        return
    # 写入 bus / raw log：用户发来的消息
    log_text = user_text or ("[收到图片]" if image_paths else "")
    bus_post("wechat", "in", log_text)
    try:
        append_wechat_raw_event("in", log_text)
    except Exception as e:
        print(f"[wechat_raw_log_error] {e}", file=sys.stderr)

    history = load_history()
    cass_text = route_message(user_text, history=history, image_paths=image_paths if image_paths else None)

    # Claude Web tool-dispatch mode:
    # Claude has already been instructed to call send_wechat directly.
    # Do not print anything here, otherwise standalone_wechat_cass.js may send a duplicate.
    if cass_text == CLAUDE_WEB_TOOL_DISPATCHED:
        history.append({"role": "user", "content": user_text})
        save_history(history)
        return

    cass_text = strip_markdown(cass_text)
    # 写入 bus：Cass 的回复
    bus_post("wechat", "out", cass_text)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": cass_text})
    save_history(history)
    print(cass_text)
    try:
        append_wechat_raw_event("out", cass_text)
    except Exception as e:
        print(f"[wechat_raw_log_error] {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
