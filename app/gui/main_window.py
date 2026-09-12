# -*- coding: utf-8 -*-
"""主窗口: 只读画布 + AI 工具状态栏 + Drawing Loop 状态框 + 人类操作按钮。

人类仅有的操作: 启动/停止 Drawing Loop、导出 PNG。
画布为只读视图, 本窗口不提供任何人工绘画入口。
"""
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QMainWindow, QMessageBox,
                               QPushButton, QStatusBar, QWidget, QHBoxLayout,
                               QVBoxLayout)

from ..ai.client import ModelClient
from ..ai.loop import DrawingLoopWorker
from ..config import model_config_ready, resolve_api_key
from ..core.canvas_engine import CanvasEngine
from ..tools.executor import ToolExecutor
from .canvas_view import CanvasView
from .loop_status_panel import LoopStatusPanel
from .tool_status_panel import ToolStatusPanel


class _EventBridge(QObject):
    """把 executor 后台线程事件桥接为 Qt 信号(自动排队到 GUI 线程)。"""

    canvas_changed = Signal()
    tool_selected = Signal(str)
    history_changed = Signal(str)
    terminated = Signal(str, str)

    def __init__(self, executor: ToolExecutor, parent=None):
        super().__init__(parent)
        executor.add_event_handler(self._on_event)

    def _on_event(self, event: str, payload: dict) -> None:
        # 注意: 可能在后台线程被调用, emit 跨线程安全
        if event == "canvas_changed":
            self.canvas_changed.emit()
        elif event == "tool_selected":
            self.tool_selected.emit(str(payload.get("tool", "")))
        elif event == "history_changed":
            self.history_changed.emit(str(payload.get("action", "")))
        elif event == "terminated":
            self.terminated.emit(str(payload.get("status", "")),
                                 str(payload.get("text", "")))


class MainWindow(QMainWindow):
    def __init__(self, engine: CanvasEngine, executor: ToolExecutor,
                 config: dict, goal: str, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.executor = executor
        self.config = config
        self.goal = goal
        self.worker = None
        self.setWindowTitle("AI 绘画执行器")
        self.resize(1500, 900)

        # ---- 中央布局 ----
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # 顶部按钮行(人类仅有的操作)
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("启动 Drawing Loop")
        self.btn_start.setMinimumHeight(34)
        self.btn_stop = QPushButton("停止 Drawing Loop")
        self.btn_stop.setMinimumHeight(34)
        self.btn_stop.setEnabled(False)
        self.btn_export = QPushButton("导出 PNG")
        self.btn_export.setMinimumHeight(34)
        self.btn_start.clicked.connect(self.start_loop)
        self.btn_stop.clicked.connect(self.stop_loop)
        self.btn_export.clicked.connect(self.export_png)
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_export)
        root.addLayout(btn_row)

        # 三栏: 工具状态 | 画布 | Loop 状态
        body = QHBoxLayout()
        self.tool_panel = ToolStatusPanel()
        self.canvas_view = CanvasView(engine)
        self.loop_panel = LoopStatusPanel()
        body.addWidget(self.tool_panel)
        body.addWidget(self.canvas_view, 1)
        body.addWidget(self.loop_panel)
        root.addLayout(body, 1)
        self.setCentralWidget(central)

        self.statusBar().showMessage(
            f"画布 {engine.width}x{engine.height} · 白底 · 目标: {goal[:60]}")

        # ---- executor 事件桥接 ----
        self._bridge = _EventBridge(executor)
        self._bridge.canvas_changed.connect(self.canvas_view.request_refresh)
        self._bridge.canvas_changed.connect(self._sync_tool_settings)
        self._bridge.tool_selected.connect(self.tool_panel.set_active_tool)
        self._bridge.tool_selected.connect(lambda _: self._sync_tool_settings())
        self._bridge.history_changed.connect(self._on_history)
        self._bridge.terminated.connect(self._on_terminated)

        self.canvas_view.refresh_now()

    # ------------------------------------------------------------------
    # Drawing Loop 控制
    # ------------------------------------------------------------------
    def start_loop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            return
        if not model_config_ready(self.config):
            QMessageBox.warning(
                self, "模型配置缺失",
                "模型配置不完整(base_url / model_name / api_key)。\n\n"
                "请在 config.json 中填写 model 配置, 或设置环境变量 "
                "AI_DRAWING_API_KEY。\n应用仍可作为只读画布使用并支持导出 PNG。")
            self.loop_panel.append_error(
                "启动失败: 模型配置缺失(base_url / model_name / api_key)。")
            return
        model_cfg = self.config.get("model", {})
        loop_cfg = self.config.get("loop", {})
        client = ModelClient(
            base_url=str(model_cfg.get("base_url", "")),
            api_key=resolve_api_key(self.config),
            model_name=str(model_cfg.get("model_name", "")),
            timeout_seconds=int(model_cfg.get("timeout_seconds", 120)),
        )
        self.tool_panel.reset_terminated()
        self.worker = DrawingLoopWorker(
            self.executor, client, self.goal,
            max_rounds=int(loop_cfg.get("max_rounds", 20)),
            keep_recent_canvas_images=int(
                loop_cfg.get("keep_recent_canvas_images", 3)),
            observe_max_side=int(loop_cfg.get("observe_max_side", 640)),
        )
        self.worker.round_started.connect(self.loop_panel.append_round)
        self.worker.assistant_text.connect(self.loop_panel.append_assistant)
        self.worker.tool_call_made.connect(self.loop_panel.append_tool_call)
        self.worker.tool_result.connect(self.loop_panel.append_tool_result)
        self.worker.auto_canvas_image.connect(
            self.loop_panel.append_auto_canvas)
        self.worker.context_updated.connect(self.loop_panel.set_context_summary)
        self.worker.loop_error.connect(self.loop_panel.append_error)
        self.worker.loop_finished.connect(self.on_loop_finished)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.statusBar().showMessage("Drawing Loop 运行中...")
        self.worker.start()

    def stop_loop(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.statusBar().showMessage("正在停止 Drawing Loop...")
            self.btn_stop.setEnabled(False)

    def on_loop_finished(self, status: str, reason: str) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        labels = {
            "finished": "任务完成",
            "aborted": "模型主动放弃任务。",
            "stopped": "已停止",
            "max_rounds": "达到最大轮数",
            "error": "错误终止",
        }
        text = labels.get(status, status)
        if reason:
            text += f" {reason}"
        if status == "aborted":
            self.tool_panel.set_terminated("aborted", reason)
        elif status == "finished":
            self.tool_panel.set_terminated("finished", reason)
        self.loop_panel.append_log(f"[loop_finished] {text}")
        self.statusBar().showMessage(text)

    # ------------------------------------------------------------------
    # 导出 PNG(原始分辨率)
    # ------------------------------------------------------------------
    def export_png(self) -> None:
        default_name = self.config.get("export", {}).get(
            "default_filename", "drawing.png")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 PNG", default_name, "PNG 图片 (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        try:
            self.engine.export_png(path)
            self.statusBar().showMessage(f"导出成功: {path}")
            self.loop_panel.append_log(f"[export] 已导出原始分辨率 PNG: {path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", f"导出 PNG 失败: {e}")

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _on_history(self, action: str) -> None:
        self.tool_panel.flash_tool(action)

    def _on_terminated(self, status: str, text: str) -> None:
        # 外部 Skill 模式或 Loop 中调用 finish/itsHardToFinish 时触发
        if status == "finished":
            self.tool_panel.set_terminated("finished", text)
            msg = f"任务完成。{text}" if text else "任务完成。"
        else:
            self.tool_panel.set_terminated("aborted", text)
            msg = "模型主动放弃任务。" + (f"原因: {text}" if text else "")
        self.loop_panel.append_log(f"[terminated] {msg}")
        self.statusBar().showMessage(msg)

    def _sync_tool_settings(self) -> None:
        state = self.executor.current_tool_state()
        parts = [f"当前工具: {state.get('tool')}"]
        for k, v in state.items():
            if k != "tool":
                parts.append(f"{k}={v}")
        self.tool_panel.update_settings_text("  ".join(parts))

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(3000)
        super().closeEvent(event)
