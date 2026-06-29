import json
from memory.memory_store import remember, search, list_recent

MEMORY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_remember",
            "description": "保存一条需要长期记住的信息，比如用户的偏好、重要事件、关键决定",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的内容"},
                    "category": {"type": "string", "default": "general", "description": "分类：general/preference/event/decision"},
                    "importance": {"type": "integer", "default": 5, "description": "重要程度1-10"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "搜索长期记忆，当需要回忆用户说过的事情时使用",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "integer", "default": 10}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": "列出最近保存的记忆",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20}
                }
            }
        }
    }
]

def handle_tool_call(name: str, args: dict) -> str:
    try:
        if name == "memory_remember":
            rid = remember(
                content=args["content"],
                category=args.get("category", "general"),
                importance=args.get("importance", 5)
            )
            return f"已记住（id={rid}）"
        elif name == "memory_search":
            results = search(args["query"], args.get("limit", 10))
            if not results:
                return "没有找到相关记忆"
            return "\n".join([f"[{r['created_at']}] {r['content']}" for r in results])
        elif name == "memory_list":
            results = list_recent(args.get("limit", 20))
            if not results:
                return "暂无记忆"
            return "\n".join([f"[{r['created_at']}] {r['content']}" for r in results])
        else:
            return f"未知工具: {name}"
    except Exception as e:
        return f"工具调用失败: {e}"
