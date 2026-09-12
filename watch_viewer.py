# -*- coding: utf-8 -*-
"""只读观察窗口: 旁观外部 agent 的作画过程。

自包含实现: 只依赖 Python 标准库 + Pillow 显示 PNG, 不需要绘画引擎,
也不依赖项目里的其它模块。GUI 后端自动择一:
  1. PySide6(Qt) —— 若目标机器已装(例如本项目环境), 优先使用;
  2. tkinter —— Python 自带的标准库, 作为无 Qt 环境的兜底。
两者都不可用时给出可读提示, 并建议直接用任意看图工具打开帧 PNG。

用法(可从任意工作目录执行, 路径无需写死):
    python -X utf8 <项目根>/watch_viewer.py out/current.png
    python -X utf8 <项目根>/watch_viewer.py out/current.png --interval 150

配合桥的帧导出使用:
    python -X utf8 <项目根>/bridge_stdio.py --watch-file out/current.png

本窗口只做显示: 轮询帧文件变化并刷新画面, **不提供任何人工绘画入口**
(不响应鼠标绘制, 不绑定任何编辑操作, 符合"人类不能直接在画布上绘画"的约束)。
"""
import argparse
import base64
import io
import sys
from pathlib import Path

from PIL import Image

# 本模块只负责"看", 永远不持有画布引擎, 也不暴露任何写操作
READ_ONLY = True

try:
    import tkinter as tk
except ImportError:  # 取决于目标机器的 Python 发行版(精简版/部分 Linux)
    tk = None


def available_backends():
    """返回本机可用的 GUI 后端, 按优先级排列("qt" 优于 "tk")。"""
    backends = []
    try:
        import PySide6  # noqa: F401
        backends.append("qt")
    except ImportError:
        pass
    if tk is not None:
        backends.append("tk")
    return backends


def load_frame_png(path, max_side: int = 0):
    """读取帧文件并返回可直接显示的 PNG 字节。

    与 GUI 无关的纯函数(便于在无显示环境下测试):
    - 文件不存在 / 正在被写入(读到损坏内容) 时返回 None, 由调用方下次重试;
    - `max_side > 0` 时按最长边等比缩小(仅影响显示, 不写回文件)。
    """
    try:
        raw = Path(path).read_bytes()
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (OSError, ValueError):
        return None  # 文件还没有, 或正好读到写一半的内容
    if max_side and max(img.size) > max_side:
        img = img.copy()
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# 后端 1: PySide6(Qt)
# --------------------------------------------------------------------------
class _QtBackend:
    name = "qt"

    def __init__(self):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import (QApplication, QLabel, QMainWindow,
                                       QStatusBar, QVBoxLayout, QWidget)
        self._Qt = Qt
        self._QPixmap = QPixmap
        self._QTimer = QTimer
        self.frames = 0

        self.app = QApplication.instance() or QApplication([])
        self.win = QMainWindow()
        self.win.setWindowTitle("AI 绘画观察器（只读）")
        self.win.resize(1100, 700)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        hint = QLabel("只读观察者：画面完全由外部 agent 的工具调用产生，"
                      "本窗口不提供任何绘画入口。")
        hint.setStyleSheet("color:#6b7280;font-size:11px;")
        layout.addWidget(hint)

        # 仅用于显示图片, 不绑定任何绘制事件
        self.label = QLabel(alignment=Qt.AlignCenter)
        self.label.setStyleSheet("background:#f3f4f6;")
        self.label.setMinimumSize(320, 200)
        layout.addWidget(self.label, 1)
        self.win.setCentralWidget(central)
        self.win.setStatusBar(QStatusBar())

    def schedule(self, fn, interval_ms):
        self.timer = self._QTimer()
        self.timer.setInterval(max(30, int(interval_ms)))
        self.timer.timeout.connect(fn)
        self.timer.start()

    def apply(self, png, caption):
        pm = self._QPixmap()
        pm.loadFromData(png, "PNG")
        self.label.setPixmap(pm.scaled(
            self.label.size(), self._Qt.KeepAspectRatio,
            self._Qt.SmoothTransformation))
        self.win.statusBar().showMessage(caption)

    def run(self):
        self.win.show()
        self.app.exec()

    def close(self):
        self.timer.stop()
        self.win.close()


# --------------------------------------------------------------------------
# 后端 2: tkinter(标准库兜底)
# --------------------------------------------------------------------------
class _TkBackend:
    name = "tk"

    def __init__(self):
        self.frames = 0
        self.root = tk.Tk()
        self.root.title("AI 绘画观察器（只读）")
        self.root.geometry("1100x700")
        tk.Label(self.root, anchor="w", justify="left", fg="#6b7280",
                 text="只读观察者：画面完全由外部 agent 的工具调用产生，"
                      "本窗口不提供任何绘画入口。").pack(
            fill="x", padx=8, pady=(6, 2))
        # 仅用于显示图片, 不绑定任何绘制事件
        self.canvas = tk.Canvas(self.root, bg="#f3f4f6",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=4)
        self.status = tk.Label(self.root, anchor="w", fg="#374151")
        self.status.pack(fill="x", padx=8, pady=(0, 6))
        self._img = None

    def schedule(self, fn, interval_ms):
        """周期回调(after 只触发一次, 这里自行重排实现循环)。"""
        self._fn = fn
        self._interval = max(30, int(interval_ms))
        self._loop()

    def _loop(self):
        try:
            self._fn()
            self.root.after(self._interval, self._loop)
        except tk.TclError:
            pass  # 窗口已关闭

    def apply(self, png, caption):
        # 引用旧图避免被 GC; Tk 8.6+ 原生支持 PNG
        self._img = tk.PhotoImage(master=self.root,
                                  data=base64.b64encode(png))
        self.canvas.delete("all")
        w = self.canvas.winfo_width() or self._img.width()
        h = self.canvas.winfo_height() or self._img.height()
        self.canvas.create_image(w // 2, h // 2, image=self._img)
        self.status.config(text=caption)

    def run(self):
        self.root.mainloop()

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass


_BACKENDS = {"qt": _QtBackend, "tk": _TkBackend}


# --------------------------------------------------------------------------
# 统一门面
# --------------------------------------------------------------------------
class WatchViewer:
    """只读观察窗口。

    公共属性:
    - ``engine``   : 恒为 None —— 用结构性事实声明本窗口没有画布引擎;
    - ``read_only``: 恒为 True;
    - ``frames``   : 已成功刷新的帧数;
    - ``backend``  : 实际使用的 GUI 后端("qt" / "tk")。
    """

    def __init__(self, path, interval_ms: int = 150, max_side: int = 1024,
                 backend=None):
        self.path = Path(path)
        self.engine = None
        self.read_only = READ_ONLY
        self._max_side = max_side
        self._sig = None
        self.backend = backend or (available_backends() or [None])[0]
        if self.backend not in _BACKENDS:
            raise RuntimeError(
                "本机既没有 PySide6 也没有 tkinter, 无法打开观察窗口; "
                "可直接用任意看图工具打开帧 PNG。")
        self._ui = _BACKENDS[self.backend]()
        self._ui.schedule(self.poll, interval_ms)
        self.poll()

    @property
    def frames(self) -> int:
        return self._ui.frames

    def poll(self) -> None:
        """轮询帧文件; 内容变化时刷新画面(用 (mtime, size) 判断变更)。"""
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return  # 文件还不存在, 等下一次轮询
        if sig == self._sig:
            return
        png = load_frame_png(self.path, self._max_side)
        if png is None:
            return  # 可能正好读到写一半的内容, 下次再试
        self._sig = sig
        self._ui.frames += 1
        self._ui.apply(
            png, f"{self.path.name} · 已刷新 {self.frames} 帧 · "
                 f"只读观察（不可绘画）")

    def run(self) -> None:
        self._ui.run()

    def close(self) -> None:
        self._ui.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI 绘画执行器只读观察窗口")
    parser.add_argument("file", nargs="?", default="out/current.png",
                        help="桥导出的画布帧 PNG 路径(默认 out/current.png, "
                             "相对当前工作目录)")
    parser.add_argument("--interval", type=int, default=150,
                        help="轮询间隔(毫秒, 默认 150)")
    parser.add_argument("--max-side", type=int, default=1024,
                        help="显示时最长边缩放(0=不缩放, 默认 1024)")
    parser.add_argument("--backend", choices=sorted(_BACKENDS), default=None,
                        help="强制指定 GUI 后端(默认自动选择)")
    args = parser.parse_args(argv)

    if not available_backends():
        print("本机既没有 PySide6 也没有 tkinter, 无法打开观察窗口。\n"
              "可直接用任意看图工具打开: " + args.file, file=sys.stderr)
        return 2
    WatchViewer(Path(args.file), args.interval, args.max_side,
                args.backend).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
