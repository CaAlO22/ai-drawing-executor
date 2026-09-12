# -*- coding: utf-8 -*-
"""会话上下文管理: 真实发送给模型的消息列表 + 画布图片裁剪。

策略(MISSION 17.5):
- 文字历史、工具调用历史、工具文字结果永久保留。
- 画布图片只保留最近 keep 张, 更早的替换为文字占位 [old canvas image removed]。
- 应用内部动作历史不因上下文裁剪而丢失。
"""
from dataclasses import dataclass, field
from typing import List, Optional

_CANVAS_TAG = "_canvas_image"


@dataclass
class Message:
    role: str                       # system / user / assistant / tool
    text: str = ""
    image_parts: List[str] = field(default_factory=list)   # data URI 列表
    tool_calls: List[dict] = field(default_factory=list)   # assistant 工具调用
    tool_call_id: str = ""          # tool 消息关联 id
    tool_name: str = ""
    placeholder: bool = False       # 图片被移除后的占位消息


class ConversationManager:
    def __init__(self):
        self.messages: List[Message] = []

    # ------------------------------------------------------------------
    # 追加
    # ------------------------------------------------------------------
    def add_system(self, text: str) -> None:
        self.messages.append(Message(role="system", text=text))

    def add_user_text(self, text: str) -> None:
        self.messages.append(Message(role="user", text=text))

    def add_canvas_image(self, data_uri: str, note: str = "当前画布(自动返回):") -> None:
        self.messages.append(
            Message(role="user", text=note, image_parts=[data_uri]))

    def append_assistant(self, text: str, tool_calls: List[dict]) -> None:
        self.messages.append(
            Message(role="assistant", text=text, tool_calls=tool_calls))

    def append_tool_result(self, tool_call_id: str, name: str,
                           content: str) -> None:
        self.messages.append(Message(
            role="tool", tool_call_id=tool_call_id, tool_name=name,
            text=content))

    # ------------------------------------------------------------------
    # 图片裁剪
    # ------------------------------------------------------------------
    def prune_canvas_images(self, keep: int = 3) -> int:
        """只保留最近 keep 张画布图片, 其余替换为占位文本。返回移除数量。"""
        image_msgs = [m for m in self.messages if m.image_parts]
        excess = len(image_msgs) - keep
        if excess <= 0:
            return 0
        removed = 0
        for m in image_msgs[:excess]:
            removed += len(m.image_parts)
            m.image_parts = []
            m.placeholder = True
            if not m.text:
                m.text = "[old canvas image removed]"
        return removed

    def canvas_image_count(self) -> int:
        return sum(len(m.image_parts) for m in self.messages)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def build_messages(self) -> list:
        """构造 OpenAI 兼容消息列表。"""
        out = []
        for m in self.messages:
            if m.role == "system":
                out.append({"role": "system", "content": m.text})
            elif m.role == "user":
                if m.image_parts:
                    parts = [{"type": "text", "text": m.text or ""}]
                    for uri in m.image_parts:
                        parts.append({"type": "image_url",
                                      "image_url": {"url": uri}})
                    out.append({"role": "user", "content": parts})
                else:
                    text = m.text
                    if m.placeholder and "[old canvas image removed]" not in text:
                        text = (text + "\n[old canvas image removed]").strip()
                    out.append({"role": "user", "content": text})
            elif m.role == "assistant":
                msg = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    msg["tool_calls"] = [{
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": _dumps(tc.get("arguments", {})),
                        },
                    } for tc in m.tool_calls]
                out.append(msg)
            elif m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                            "content": m.text})
        return out

    def summary(self, max_text: int = 120) -> str:
        """给右侧状态框的上下文摘要, 图片用 [canvas image] 占位。"""
        lines = []
        for i, m in enumerate(self.messages):
            head = f"[{i}] {m.role}"
            if m.role == "tool":
                head += f" ({m.tool_name})"
            parts = [head]
            text = (m.text or "").strip()
            if text:
                t = text[:max_text] + ("..." if len(text) > max_text else "")
                parts.append(t)
            if m.tool_calls:
                calls = ", ".join(tc["name"] for tc in m.tool_calls)
                parts.append(f"tool_calls: {calls}")
            for _ in m.image_parts:
                parts.append("[canvas image]")
            if m.placeholder and not m.image_parts:
                parts.append("[old canvas image removed]")
            lines.append(" ".join(parts))
        kept = self.canvas_image_count()
        lines.append(f"--- 上下文中保留画布图片: {kept} 张 ---")
        return "\n".join(lines)


def _dumps(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"
