# -*- coding: utf-8 -*-
"""AI 工具状态栏(最左侧): 只读展示 AI 可用工具与当前高亮工具。

- 不是人类工具栏, 不提供任何点击切换能力(纯 QLabel, 无交互)。
- pick_tools 后高亮当前工具; use_tool 保持高亮;
- undo/redo 短暂高亮; finish/itsHardToFinish 显示终止状态。
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFrame, QLabel, QVBoxLayout, QWidget)

TOOL_ITEMS = [
    ("pen", "pen 硬画笔"),
    ("brush", "brush 毛笔"),
    ("eraser_area", "eraser_area 区域橡皮"),
    ("eraser_stroke", "eraser_stroke 笔画橡皮"),
    ("bucket", "bucket 油漆桶"),
    None,  # 分隔
    ("undo", "undo 撤销"),
    ("redo", "redo 重做"),
    None,
    ("finish", "finish 完成"),
    ("itsHardToFinish", "itsHardToFinish 放弃"),
]

_STYLE_BASE = (
    "QLabel { padding: 5px 8px; border-radius: 4px; color: #444; }"
)
_STYLE_ACTIVE = (
    "QLabel { padding: 5px 8px; border-radius: 4px; "
    "background-color: #4f46e5; color: white; font-weight: 600; }"
)
_STYLE_FLASH = (
    "QLabel { padding: 5px 8px; border-radius: 4px; "
    "background-color: #c7d2fe; color: #1e1b4b; font-weight: 600; }"
)
_STYLE_TERMINATED = (
    "QLabel { padding: 5px 8px; border-radius: 4px; "
    "background-color: #059669; color: white; font-weight: 600; }"
)
_STYLE_ABORTED = (
    "QLabel { padding: 5px 8px; border-radius: 4px; "
    "background-color: #dc2626; color: white; font-weight: 600; }"
)


class ToolStatusPanel(QWidget):
    """只读 AI 工具状态可视化。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(190)
        self.setMinimumHeight(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 10)
        layout.setSpacing(2)

        title = QLabel("AI 工具状态")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-weight: 700; color: #1f2937; padding-bottom: 6px;")
        layout.addWidget(title)

        self._labels = {}
        for item in TOOL_ITEMS:
            if item is None:
                sep = QFrame()
                sep.setFrameShape(QFrame.HLine)
                sep.setStyleSheet("color: #d1d5db;")
                layout.addWidget(sep)
                continue
            key, label = item
            lab = QLabel(label)
            lab.setStyleSheet(_STYLE_BASE)
            self._labels[key] = lab
            layout.addWidget(lab)

        layout.addSpacing(8)
        self._settings_label = QLabel("当前设置: pen")
        self._settings_label.setWordWrap(True)
        self._settings_label.setStyleSheet("color: #6b7280; font-size: 11px;")
        layout.addWidget(self._settings_label)

        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #374151; font-size: 11px;")
        layout.addWidget(self._status_label)

        layout.addStretch(1)

        self._flash_timers = {}
        # 默认当前工具 pen
        self.set_active_tool("pen")

    # ------------------------------------------------------------------
    def set_active_tool(self, tool: str) -> None:
        """高亮当前工具(pick_tools / use_tool 后保持)。"""
        for key, lab in self._labels.items():
            if key in ("pen", "brush", "eraser_area", "eraser_stroke", "bucket"):
                lab.setStyleSheet(
                    _STYLE_ACTIVE if key == tool else _STYLE_BASE)
        self._settings_label.setText(f"当前工具: {tool}")

    def update_settings_text(self, text: str) -> None:
        self._settings_label.setText(text)

    def flash_tool(self, tool: str, ms: int = 1500) -> None:
        """undo/redo 等短暂高亮。"""
        lab = self._labels.get(tool)
        if lab is None:
            return
        lab.setStyleSheet(_STYLE_FLASH)
        if tool in self._flash_timers:
            self._flash_timers[tool].stop()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._clear_flash(tool))
        self._flash_timers[tool] = timer
        timer.start(ms)

    def _clear_flash(self, tool: str) -> None:
        lab = self._labels.get(tool)
        if lab is not None:
            lab.setStyleSheet(_STYLE_BASE)
        self._flash_timers.pop(tool, None)

    def set_terminated(self, status: str, text: str) -> None:
        """finish -> 绿色完成; itsHardToFinish -> 红色放弃。"""
        key = "finish" if status == "finished" else "itsHardToFinish"
        lab = self._labels.get(key)
        if lab is not None:
            lab.setStyleSheet(_STYLE_TERMINATED if status == "finished"
                              else _STYLE_ABORTED)
        msg = "任务完成" if status == "finished" else "模型主动放弃任务。"
        if text:
            msg += f"\n{text}"
        self._status_label.setText(msg)

    def reset_terminated(self) -> None:
        for key in ("finish", "itsHardToFinish"):
            lab = self._labels.get(key)
            if lab is not None:
                lab.setStyleSheet(_STYLE_BASE)
        self._status_label.setText("")
