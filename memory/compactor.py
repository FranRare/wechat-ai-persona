import json
import os
from openai import OpenAI

MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))
COMPACT_TO = MAX_HISTORY // 2

def maybe_compact(history: list, client: OpenAI, model: str) -> list:
    if len(history) <= MAX_HISTORY:
        return history

    to_compact = history[:COMPACT_TO]
    keep = history[COMPACT_TO:]

    lines = []
    for m in to_compact:
        role = "用户" if m.get("role") == "user" else "AI"
        lines.append(f"{role}: {m.get('content', '')}")

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": f"以下是一段对话历史，请用200字以内概括其中的关键信息、决定和情感节点，供AI伴侣参考：\n\n" + "\n".join(lines)
            }],
            max_tokens=300,
            temperature=0.3
        )
        summary = resp.choices[0].message.content or ""

        from memory.memory_store import remember
        remember(summary, category="conversation_summary", importance=7)

        summary_msg = {"role": "system", "content": f"[早期对话摘要] {summary}"}
        return [summary_msg] + keep
    except Exception as e:
        print(f"[compactor] failed: {e}")
        return history
