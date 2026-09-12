# -*- coding: utf-8 -*-
"""应用入口: 加载配置 -> 启动对话框 -> 创建画布与执行器 -> 主窗口。

没有模型配置时仍可作为只读画布应用正常启动和导出。
"""
import sys
from pathlib import Path

# 保证以任意工作目录启动时均可 import app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from .config import load_config
from .core.canvas_engine import CanvasEngine
from .gui.main_window import MainWindow
from .gui.startup_dialog import StartupDialog
from .tools.executor import ToolExecutor


def main() -> int:
    config = load_config()
    app = QApplication(sys.argv)
    app.setApplicationName("AI 绘画执行器")

    dialog = StartupDialog(config)
    if dialog.exec() != StartupDialog.Accepted:
        return 0
    width, height, goal = dialog.values()

    engine = CanvasEngine(
        width, height,
        background_color=str(config.get("canvas", {}).get(
            "background_color", "#ffffff")),
        max_history_steps=int(
            config.get("history", {}).get("max_steps", 100)),
    )
    executor = ToolExecutor(
        engine,
        observe_max_side=int(
            config.get("loop", {}).get("observe_max_side", 640)),
    )
    window = MainWindow(engine, executor, config, goal)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
