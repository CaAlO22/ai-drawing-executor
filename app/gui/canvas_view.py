# -*- coding: utf-8 -*-
"""只读画布视图: 按窗口大小缩放显示, 不接受任何人类绘画输入。

- 画布内容只能通过 set_image / request_refresh 更新(来自 AI 工具调用)。
- 鼠标悬停、点击、拖动均不修改画布(无任何鼠标事件处理)。
- 刷新节流: 30 FPS 定时器合并高频刷新请求, 1920x1080 下保持流畅。
"""
import numpy as np
from PySide6.QtCore import QRect, Qt, QTimer
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..core.canvas_engine import CanvasEngine

BG_COLOR = "#3c3f41"


def ndarray_to_qimage(arr: np.ndarray) -> QImage:
    h, w, _ = arr.shape
    img = QImage(arr.data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return img.copy()  # 拷贝脱离原缓冲


class CanvasView(QWidget):
    """只读画布显示区。导出时使用原始分辨率, 与显示缩放无关。"""

    REFRESH_MS = 33  # ~30 FPS 节流

    def __init__(self, engine: CanvasEngine = None, parent=None):
        """engine 为 None 时进入"外部图片模式"(由 set_pixmap 注入画面)。"""
        super().__init__(parent)
        self.engine = engine
        self._pixmap = None
        self._dirty = False
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(f"background-color: {BG_COLOR};")
        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self._on_timer)
        if engine is not None:
            self._timer.start()
            self._dirty = True

    # ------------------------------------------------------------------
    def request_refresh(self) -> None:
        """画布变化通知(GUI 线程调用), 合并到下一帧。"""
        self._dirty = True

    def refresh_now(self) -> None:
        if self.engine is None:
            return
        self._dirty = True
        self._on_timer()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """外部图片模式: 直接注入要显示的画面(仍然只读)。"""
        self._pixmap = pixmap
        self.update()

    def _on_timer(self) -> None:
        if self.engine is None:
            return
        if not self._dirty:
            return
        self._dirty = False
        arr = self.engine.snapshot_rgb()  # 引擎锁内拷贝
        qimg = ndarray_to_qimage(arr)
        self._pixmap = QPixmap.fromImage(qimg)
        self.update()

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor_safe())
            if self._pixmap is None or self._pixmap.isNull():
                return
            target = self.rect().adjusted(8, 8, -8, -8)
            scaled = self._pixmap.scaled(
                target.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation)
            x = target.x() + (target.width() - scaled.width()) // 2
            y = target.y() + (target.height() - scaled.height()) // 2
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawPixmap(QRect(x, y, scaled.width(), scaled.height()),
                               scaled)
        finally:
            painter.end()


def QColor_safe():
    from PySide6.QtGui import QColor
    return QColor(BG_COLOR)
