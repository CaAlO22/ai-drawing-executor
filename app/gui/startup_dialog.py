# -*- coding: utf-8 -*-
"""启动对话框: 输入画布宽高与绘画目标。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QSpinBox, QVBoxLayout)

from ..config import DEFAULT_CONFIG

DEFAULT_GOAL = "请绘制一幅简洁完整的图片。"


class StartupDialog(QDialog):
    """应用启动后第一个界面: 画布尺寸 + 绘画目标。"""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 绘画执行器 - 启动")
        self.setModal(True)
        self.setMinimumWidth(460)

        canvas_cfg = config.get("canvas", DEFAULT_CONFIG["canvas"])
        default_w = int(canvas_cfg.get("default_width", 1920))
        default_h = int(canvas_cfg.get("default_height", 1080))

        layout = QVBoxLayout(self)
        intro = QLabel(
            "所有画布绘制均由 AI 工具调用完成，人类不能直接绘画。\n"
            "请设置画布尺寸与绘画目标：")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.width_spin = QSpinBox()
        self.width_spin.setRange(64, 8192)
        self.width_spin.setValue(default_w)
        self.height_spin = QSpinBox()
        self.height_spin.setRange(64, 8192)
        self.height_spin.setValue(default_h)
        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_spin)
        size_row.addStretch(1)
        form.addRow("画布宽度 × 高度 (px):", size_row)

        self.goal_edit = QPlainTextEdit()
        self.goal_edit.setPlaceholderText(
            f"例如：画一只戴着圆眼镜的橘猫，风格简洁。\n留空则使用默认目标：{DEFAULT_GOAL}")
        self.goal_edit.setFixedHeight(88)
        form.addRow("绘画目标:", self.goal_edit)
        layout.addLayout(form)

        hint = QLabel("提示：画布初始为白色背景，稍后可在主界面导出原始分辨率 PNG。")
        hint.setStyleSheet("color: gray;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, Qt.Horizontal)
        buttons.button(QDialogButtonBox.Ok).setText("开始")
        buttons.button(QDialogButtonBox.Cancel).setText("退出")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        """返回 (width, height, goal)。goal 为空时返回默认目标。"""
        goal = self.goal_edit.toPlainText().strip()
        return (self.width_spin.value(), self.height_spin.value(),
                goal or DEFAULT_GOAL)
