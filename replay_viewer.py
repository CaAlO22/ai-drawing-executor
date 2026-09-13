# -*- coding: utf-8 -*-
"""重放观察窗口: 边重放边旁观, 布局与作画观察窗一致的三栏视图。

左 = 当前工具(中文名 + 设置 + 画布/步数/时间)
中 = 只读画布(轮询帧 PNG)
右 = 历史记录(每次工具调用映射为自然语言, 每行带记录时间戳)

左侧还会显示当前这一步的记录时间与本次重放的已用时长; 重放结束后右栏末尾
汇总记录时长(原会话画了多久)与重放用时(这次重放跑了多久)。

数据流: 重放 worker 线程只往共享 panel dict 里写快照(工具状态、历史行、
步数、版本号), GUI 定时器在**主线程**读快照刷新 —— 跨线程只传数据,
不跨线程碰 UI, 与 draw_app 观察窗同一套刷新模型(panels_revision)。

GUI 后端自动择一: PySide6(Qt) 优先, tkinter 兜底; 都没有时抛 RuntimeError。
"""
import base64
import html as _html
from pathlib import Path

from watch_viewer import available_backends, load_frame_png

# 快照各栏的标题, 与作画观察窗保持一致
_TOOL_TITLE = "当前工具"
_HISTORY_TITLE = "历史记录"
_EMPTY_HISTORY = "（重放开始后这里会逐条记录）"


class ReplayViewer:
    """三栏重放观察窗: 左=当前工具 / 中=画布 / 右=历史记录。

    panel 是 worker 与 GUI 共享的 dict, 约定字段:
    - ``entries``     : [(kind, text), ...] 右栏历史行(kind: action/info/end)
    - ``tool_state``  : executor.current_tool_state() 的最新快照(或 None)
    - ``step_no``     : 已重放出的画布动作步数
    - ``canvas_info`` : {"width": ..., "height": ...}
    - ``revision``    : 面板内容版本号, 变了才重绘面板
    """

    def __init__(self, frame_path, panel, interval_ms: int = 100):
        self.frame_path = Path(frame_path)
        self._panel = panel
        self._interval = max(30, int(interval_ms))
        self._sig = None          # 帧文件 (mtime_ns, size), 变了才重绘画布
        self._rendered = -1       # 已渲染的面板版本号
        backends = available_backends()
        if not backends:
            raise RuntimeError(
                "本机既没有 PySide6 也没有 tkinter, 无法打开重放观察窗口; "
                "可改用不带 --watch 的重放(仍然会导出最终 PNG)。")
        self._backend = backends[0]
        if self._backend == "qt":
            self._init_qt()
        else:
            self._init_tk()

    # ------------------------------------------------------------------
    # 面板内容(纯函数, 与 draw_app 观察窗同一套文案)
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_lines(tool_state, canvas_info, step_no, timing_lines=None) -> list:
        from draw_app import _fmt_settings, tool_label
        state = tool_state or {}
        tool = state.get("tool", "?")
        lines = [f"当前工具：{tool_label(tool)}（{tool}）"]
        detail = _fmt_settings(tool, state)
        lines.append(detail if detail else "（该工具无可调参数）")
        lines.append("")
        lines.append(f"画布：{canvas_info.get('width', '?')} × "
                     f"{canvas_info.get('height', '?')}")
        lines.append(f"已画：{step_no} 步")
        lines.extend(timing_lines or [])
        return lines

    @staticmethod
    def _tool_html(tool_state, canvas_info, step_no, timing_lines=None) -> str:
        from draw_app import _fmt_settings, tool_label
        state = tool_state or {}
        tool = state.get("tool", "?")
        detail = _fmt_settings(tool, state) or "（该工具无可调参数）"
        detail = _html.escape(detail)
        timing = "<br>".join(_html.escape(t) for t in (timing_lines or []))
        timing = f"<br>{timing}" if timing else ""
        return (f"<b>{_html.escape(tool_label(tool))}</b> "
                f"<span style='color:#6b7280'>({_html.escape(tool)})</span>"
                f"<br><br>"
                f"<span style='color:#374151'>{detail}</span><br><br>"
                f"<span style='color:#6b7280'>画布 "
                f"{canvas_info.get('width', '?')} × "
                f"{canvas_info.get('height', '?')}"
                f"<br>已画 {step_no} 步{timing}</span>")

    @staticmethod
    def _history_html(entries) -> str:
        if not entries:
            return (f"<div style='color:#9ca3af'>{_EMPTY_HISTORY}</div>")
        out = []
        for kind, text in entries:
            text = _html.escape(text)
            if kind == "action":
                out.append(f"<div style='margin:0 0 6px 0;color:#111827'>"
                           f"{text}</div>")
            elif kind == "end":
                out.append(f"<div style='margin:6px 0;color:#6d28d9;"
                           f"font-weight:600'>{text}</div>")
            else:
                out.append(f"<div style='margin:0 0 4px 0;color:#6b7280'>"
                           f"{text}</div>")
        return "".join(out)

    # ------------------------------------------------------------------
    # 后端 1: Qt(复用作画观察窗的三栏窗口构建)
    # ------------------------------------------------------------------
    def _init_qt(self):
        from PySide6.QtCore import QEvent, QObject, Qt, QTimer
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QApplication, QStatusBar
        from draw_app import DrawApp

        self._Qt, self._QPixmap = Qt, QPixmap
        self._qapp = QApplication.instance() or QApplication(["replay"])
        self._win, self._canvas, self._tool_view, self._history_view = (
            # 重放窗: 关窗即结束(close_hides=False) —— 与作画观察窗不同,
            # 作画时关窗不能停画, 重放时关窗必须让事件循环返回
            DrawApp._make_qt_window(close_hides=False))
        self._win.setWindowTitle("AI 绘画 · 过程重放（只读）")
        self._win.setStatusBar(QStatusBar())
        self._timer = QTimer()
        self._timer.setInterval(self._interval)
        self._timer.timeout.connect(self._tick_qt)
        # 自适应缩放: 缓存最近一帧原始 PNG, 窗口/画布尺寸变化时按
        # KeepAspectRatio 重新缩放, 保证整幅画始终完整显示
        self._frame_png = None
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(60)   # 防抖: 连续 resize 只缩一次
        self._resize_timer.timeout.connect(self._scale_and_show_qt)

        class _ResizeFilter(QObject):
            def __init__(self, on_resize):
                super().__init__()
                self._on_resize = on_resize

            def eventFilter(self, obj, ev):
                if ev.type() == QEvent.Resize:
                    self._on_resize()
                return False

        self._filter = _ResizeFilter(self._resize_timer.start)
        self._canvas.installEventFilter(self._filter)

    def _tick_qt(self):
        self._repaint_frame_qt()
        self._refresh_panels_qt()

    def _repaint_frame_qt(self):
        try:
            st = self.frame_path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return
        if sig == self._sig:
            return
        png = load_frame_png(self.frame_path, 1400)
        if png is None:
            return  # 可能正好读到写一半的内容, 下次再试
        self._sig = sig
        self._frame_png = png
        self._scale_and_show_qt()

    def _scale_and_show_qt(self):
        """把缓存的帧按当前画布区域等比缩放后显示(完整显示整幅画)。"""
        if not self._frame_png:
            return
        size = self._canvas.size()
        if size.width() < 50 or size.height() < 50:
            return  # 布局尚未就绪, 等 resize 事件触发后再补一次
        pm = self._QPixmap()
        pm.loadFromData(self._frame_png, "PNG")
        self._canvas.setPixmap(pm.scaled(
            size, self._Qt.KeepAspectRatio,
            self._Qt.SmoothTransformation))

    def _refresh_panels_qt(self):
        if self._panel.get("revision", 0) == self._rendered:
            return
        self._rendered = self._panel.get("revision", 0)
        self._tool_view.setText(self._tool_html(
            self._panel.get("tool_state"),
            self._panel.get("canvas_info") or {},
            self._panel.get("step_no", 0),
            self._panel.get("timing_lines")))
        self._history_view.setHtml(
            "<div style='font-family:system-ui,Segoe UI;font-size:12px'>"
            + self._history_html(list(self._panel.get("entries") or []))
            + "</div>")
        bar = self._history_view.verticalScrollBar()
        bar.setValue(bar.maximum())   # 自动滚到最新一条
        timing = [t for t in (self._panel.get("timing_lines") or []) if t]
        if timing and self._win.statusBar() is not None:
            self._win.statusBar().showMessage(" · ".join(timing) + " · 只读重放")

    # ------------------------------------------------------------------
    # 后端 2: tkinter(标准库兜底)
    # ------------------------------------------------------------------
    def _init_tk(self):
        import tkinter as tk

        self._tk = tk
        root = tk.Tk()
        root.title("AI 绘画 · 过程重放（只读）")
        root.geometry("1360x780")
        tk.Label(root, anchor="w", justify="left", fg="#6b7280",
                 text="重放观察窗口：画面按原始记录逐笔重放，本窗口不可绘画。"
                      "关闭窗口即结束。").pack(fill="x", padx=8, pady=(6, 2))
        body = tk.Frame(root)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # 左: 当前工具
        left = tk.Frame(body, width=230, bd=1, relief="solid")
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text=_TOOL_TITLE, anchor="w",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", padx=8,
                                                     pady=(8, 2))
        self._tool_text = tk.Label(left, anchor="nw", justify="left",
                                   wraplength=200, fg="#374151")
        self._tool_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # 右: 历史记录(自然语言)
        right = tk.Frame(body, width=350, bd=1, relief="solid")
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text=_HISTORY_TITLE, anchor="w",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", padx=8,
                                                     pady=(8, 2))
        self._history = tk.Text(right, wrap="word", bd=0, bg="#f9fafb",
                                fg="#111827", state="disabled",
                                font=("Segoe UI", 9))
        self._history.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for tag, color in (("action", "#111827"), ("info", "#6b7280"),
                           ("end", "#6d28d9")):
            self._history.tag_config(tag, foreground=color)

        # 中: 画布(只读)
        self._canvas = tk.Canvas(body, bg="#f3f4f6", highlightthickness=1,
                                 highlightbackground="#e5e7eb")
        self._canvas.pack(side="left", fill="both", expand=True, padx=8)
        self._status = tk.Label(root, anchor="w", fg="#374151")
        self._status.pack(fill="x", padx=8, pady=(0, 6))
        self._img = None
        self._root = root
        # 自适应缩放: 缓存最近一帧原始 PNG, 画布尺寸变化时等比缩放重绘
        self._frame_png = None
        self._fit_pending = None
        self._canvas.bind("<Configure>", self._on_configure_tk)

    def _on_configure_tk(self, _ev):
        # <Configure> 在布局/拖拽时会连续触发, 防抖后只重绘一次
        if self._fit_pending is None:
            self._fit_pending = self._root.after(60, self._fit_tk)

    def _tick_tk(self):
        if not self._root.winfo_exists():
            return
        try:
            self._repaint_frame_tk()
            self._refresh_panels_tk()
            self._root.after(self._interval, self._tick_tk)
        except self._tk.TclError:
            pass  # 窗口已销毁

    def _repaint_frame_tk(self):
        try:
            st = self.frame_path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            return
        if sig == self._sig:
            return
        png = load_frame_png(self.frame_path, 1400)
        if png is None:
            return
        self._sig = sig
        self._frame_png = png
        self._fit_tk()

    def _fit_tk(self):
        """把缓存的帧按当前画布区域等比缩放后居中显示(完整显示整幅画)。"""
        self._fit_pending = None
        if not self._frame_png or not self._root.winfo_exists():
            return
        w = self._canvas.winfo_width()
        h = self._canvas.winfo_height()
        if w < 50 or h < 50:
            return  # 布局尚未就绪, 等 <Configure> 再触发
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(self._frame_png))
            img.load()
        except (OSError, ValueError):
            return
        scale = min(w / img.width, h / img.height)
        tw = max(1, int(img.width * scale))
        th = max(1, int(img.height * scale))
        img = img.convert("RGB").resize((tw, th), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self._img = self._tk.PhotoImage(
            master=self._root, data=base64.b64encode(buf.getvalue()))
        self._canvas.delete("all")
        self._canvas.create_image(w // 2, h // 2, image=self._img)

    def _refresh_panels_tk(self):
        if self._panel.get("revision", 0) == self._rendered:
            return
        self._rendered = self._panel.get("revision", 0)
        self._tool_text.config(text="\n".join(self._tool_lines(
            self._panel.get("tool_state"),
            self._panel.get("canvas_info") or {},
            self._panel.get("step_no", 0),
            self._panel.get("timing_lines"))))
        self._history.config(state="normal")
        self._history.delete("1.0", "end")
        entries = list(self._panel.get("entries") or [])
        if not entries:
            self._history.insert("end", _EMPTY_HISTORY + "\n", "info")
        for kind, text in entries:
            self._history.insert("end", text + "\n\n", kind)
        self._history.config(state="disabled")
        self._history.see("end")
        timing = [t for t in (self._panel.get("timing_lines") or []) if t]
        if timing:
            self._status.config(text=" · ".join(timing) + " · 只读重放")

    # ------------------------------------------------------------------
    # 统一入口
    # ------------------------------------------------------------------
    def run(self) -> None:
        """阻塞直到用户关闭窗口(重放是否已结束由 worker 自行决定)。"""
        if self._backend == "qt":
            self._timer.start()
            self._tick_qt()
            self._win.show()
            self._qapp.exec()
        else:
            self._root.after(self._interval, self._tick_tk)
            self._root.mainloop()

    def close(self) -> None:
        if self._backend == "qt":
            self._timer.stop()
            self._win.close()
        else:
            try:
                self._root.destroy()
            except self._tk.TclError:
                pass
