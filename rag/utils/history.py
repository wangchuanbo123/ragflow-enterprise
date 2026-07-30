"""历史对话工具：截断与格式化。

多轮上下文只读取最近若干轮已完成消息，并设置总长度上限，避免无限增长。
"""

from app.core.config import MAX_HISTORY_MESSAGES

HISTORY_TOTAL_CHARS = 3000


def trim_history(history: list[dict], limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """只保留最近 limit 条已完成消息，并控制总字符数。"""
    recent = (history or [])[-limit:]
    trimmed: list[dict] = []
    total = 0
    for item in reversed(recent):
        content = (item.get("content") or "")[:HISTORY_TOTAL_CHARS]
        total += len(content)
        if total > HISTORY_TOTAL_CHARS:
            break
        trimmed.append({"role": item.get("role", "user"), "content": content})
    trimmed.reverse()
    return trimmed


def format_history(history: list[dict]) -> str:
    """把历史消息格式化为提示词片段；无历史时返回空字符串。"""
    trimmed = trim_history(history or [])
    if not trimmed:
        return ""
    lines = []
    role_map = {"user": "用户", "assistant": "助手"}
    for item in trimmed:
        role = role_map.get(item["role"], item["role"])
        lines.append(f"{role}: {item['content']}")
    return "\n".join(lines)
