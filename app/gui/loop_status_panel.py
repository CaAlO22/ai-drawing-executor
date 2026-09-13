# -*- coding: utf-8 -*-
"""右侧状态框: Loop Log(按时间日志) + Current Context(真实上下文摘要)。

- 每条日志都带 [HH:MM:SS] 时间戳(与观察窗/replay 的时间戳同一读法);
- 图片一律用 [canvas image] 占位符, 不直接塞进状态框。
- 明确区分模型主动输出与应用自动返回的画布图片。
"""
from datetime import datetime

from PySide6.QtWidgets import (QPlainTextEdit, QTabWidget, QVBoxLayout,
                               QWidget)

from ..ai.context import ConversationManager


class LoopStatusPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(380)
        self.setMinimumHeight(300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.tabs = QTabWidget()
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(2000)
        self.log_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.tabs.addTab(self.log_edit, "Loop Log")

        self.ctx_edit = QPlainTextEdit()
        self.ctx_edit.setReadOnly(True)
        self.ctx_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.tabs.addTab(self.ctx_edit, "Current Context")

        layout.addWidget(self.tabs)

    # ------------------------------------------------------------------
    def append_log(self, text: str) -> None:
        """每条日志行首加墙钟时间戳(只加一次, 多行正文照旧)。"""
        self.log_edit.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def append_assistant(self, text: str) -> None:
        self.append_log(f"[assistant]\n{text}")

    def append_tool_call(self, name: str, args_summary: str) -> None:
        self.append_log(f"[tool_call]\n{name}\n{args_summary}")

    def append_tool_result(self, result_text: str) -> None:
        self.append_log(f"[tool_result]\n{result_text}")

    def append_auto_canvas(self) -> None:
        self.append_log("[auto_canvas_image]\n[canvas image]")

    def append_error(self, text: str) -> None:
        self.append_log(f"[error]\n{text}")

    def append_round(self, round_no: int) -> None:
        self.append_log(f"===== Round {round_no} =====")

    def set_context_summary(self, summary: str) -> None:
        self.ctx_edit.setPlainText(summary)

    def clear_log(self) -> None:
        self.log_edit.clear()
