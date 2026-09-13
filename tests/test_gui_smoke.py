# -*- coding: utf-8 -*-
"""GUI 冒烟测试(无头): 启动对话框 + 主窗口 + 画布渲染 + 导出, 验证不崩。"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from app.config import load_config, model_config_ready
from app.core.canvas_engine import CanvasEngine
from app.gui.main_window import MainWindow
from app.gui.startup_dialog import StartupDialog
from app.tools.executor import ToolExecutor

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}", flush=True)


def test_gui_smoke():
    print("== GUI 冒烟测试(offscreen) ==", flush=True)
    app = QApplication.instance() or QApplication(sys.argv)
    config = load_config()
    engine = CanvasEngine(800, 600)
    executor = ToolExecutor(engine)

    # 模拟 AI 工具调用
    executor.call_tool("pick_tools", {"tool": "pen", "settings": {
        "size": 20, "color": "#000000"}})
    executor.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [200, 200], [300, 100]]}})
    executor.call_tool("pick_tools", {"tool": "brush", "settings": {
        "size": 80, "color": "#888888", "opacity": 0.6}})
    executor.call_tool("use_tool", {"mode": "path", "path": {
        "shape": "circle", "params": {"cx": 400, "cy": 300, "r": 100}}})

    win = MainWindow(engine, executor, config, "测试画")
    win.show()
    check("主窗口实例化", win.isVisible())
    check("启动按钮存在", win.btn_start is not None)
    check("停止按钮存在", win.btn_stop is not None)
    check("导出按钮存在", win.btn_export is not None)
    check("工具状态栏存在", win.tool_panel is not None)
    check("画布视图存在", win.canvas_view is not None)
    check("Loop 状态面板存在", win.loop_panel is not None)

    # Loop Log 时间戳与总时长(与观察窗/replay 同一套读法)
    import re
    import time as _time

    from draw_app import format_duration
    from app.gui.main_window import _format_duration
    win.loop_panel.clear_log()
    win.loop_panel.append_round(1)
    win.loop_panel.append_tool_call("use_tool", '{"mode": "point"}')
    log_text = win.loop_panel.log_edit.toPlainText()
    check("Loop Log 每行带墙钟时间戳",
          bool(re.search(r"^\[\d{2}:\d{2}:\d{2}\] ===== Round 1 =====", log_text,
                         re.M))
          and bool(re.search(r"^\[\d{2}:\d{2}:\d{2}\] \[tool_call\]", log_text,
                             re.M)), log_text.replace("\n", " | "))
    check("时长格式与 draw_app 一致(防两处漂移)",
          all(_format_duration(x) == format_duration(x)
              for x in (0, 12400, 83200, 3723000, None)),
          f"{_format_duration(83200)} vs {format_duration(83200)}")
    win._loop_t0 = _time.monotonic() - 1.25
    win.on_loop_finished("finished", "测试完成。")
    tail = win.loop_panel.log_edit.toPlainText().strip().splitlines()[-1]
    check("Loop 结束时汇总总时长",
          re.match(r"^\[\d{2}:\d{2}:\d{2}\] \[duration\] 本次运行总时长 "
                   r"1\.[0-9] 秒$", tail), tail)
    win.loop_panel.clear_log()

    # 事件桥接
    win._bridge.tool_selected.emit("brush")
    check("tool_selected 桥接", True)
    win._bridge.history_changed.emit("undo")
    check("history_changed 桥接", True)
    win._bridge.terminated.emit("finished", "测试完成。")
    check("terminated 桥接(完成)", True)
    win._bridge.terminated.emit("aborted", "模型放弃了。")
    check("terminated 桥接(放弃)", True)
    win.tool_panel.reset_terminated()
    check("重置终止状态", True)

    # 配置检查(不启动 loop, 避免模态对话框阻塞)
    check("默认配置缺模型参数", not model_config_ready(config))
    config_full = dict(config)
    config_full["model"] = {"base_url": "https://example.com/v1",
                            "api_key": "test", "model_name": "test-model",
                            "timeout_seconds": 120}
    check("模型配置完整检测", model_config_ready(config_full))

    # 导出(底层, 不触发文件对话框)
    out = Path(__file__).resolve().parent.parent / "demo_gui_export.png"
    engine.export_png(str(out))
    check("导出 PNG 成功", out.exists() and out.stat().st_size > 0)

    # 高频画布变化线程安全
    for _ in range(10):
        executor.call_tool("use_tool", {"mode": "path", "path": {
            "points": [[400, 300], [500, 400]]}})
    app.processEvents()
    check("高频画布变化无卡死", True)

    # 关键: AI 工具调用必须驱动 GUI 画布刷新(engine -> executor -> 信号 -> 视图)
    eng2 = CanvasEngine(600, 400)
    ex2 = ToolExecutor(eng2)
    win2 = MainWindow(eng2, ex2, config, "刷新验证")
    win2.show()
    app.processEvents()
    before_pm = win2.canvas_view._pixmap
    ex2.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 40,
                                                            "color": "#ff0000"}})
    ex2.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [500, 300]]}})
    app.processEvents()          # 投递 canvas_changed -> request_refresh
    win2.canvas_view.refresh_now()   # 合并到下一帧
    app.processEvents()
    after_pm = win2.canvas_view._pixmap
    check("绘制产生 canvas_changed 并标记刷新",
          win2.canvas_view._dirty is False and after_pm is not None
          and not after_pm.isNull())
    arr = eng2.snapshot_rgb()
    check("画布确有内容(非全白)", not bool((arr == 255).all()))
    win2.close()
    app.processEvents()

    # ---- 重放三栏观察窗: worker 重放 -> panel 快照 -> offscreen Qt 渲染 ----
    import io
    import tempfile
    import threading

    from draw_app import describe_call
    from PIL import Image

    from replay import replay
    from replay_viewer import ReplayViewer

    with tempfile.TemporaryDirectory() as td:
        frame_path = Path(td) / "frame.png"
        Image.new("RGB", (400, 300), "#ffffff").save(frame_path, "PNG")
        panel = {"entries": [], "tool_state": None, "step_no": 0,
                 "canvas_info": {"width": 400, "height": 300,
                                 "background_color": "#ffffff"},
                 "revision": 0}
        box = {}
        # 残留成图清理: 帧文件固定名, 上次重放的最终成图会让窗口一打开
        # 就先显示完整旧画 —— write_blank_frame 应把它重置为空白初始帧
        from replay import write_blank_frame
        Image.new("RGB", (400, 300), "#ff0000").save(frame_path, "PNG")
        write_blank_frame(panel["canvas_info"], frame_path)
        chk = Image.open(frame_path)
        chk.load()
        g_ext = chk.convert("RGB").split()[1].getextrema()
        check("重放启动重置空白帧(清掉残留成图)",
              chk.size == (400, 300) and g_ext == (255, 255))
        replay_data = {"schema": "ai-drawing-process-v1",
                       "canvas": {"width": 400, "height": 300,
                                  "background_color": "#ffffff"},
                       "options": {"max_history_steps": 100},
                       "calls": [
                           {"type": "call", "seq": 1, "tool": "pick_tools",
                            "ts": "2026-09-13T10:10:26.000",
                            "elapsed_ms": 0,
                            "arguments": {"tool": "pen",
                                          "settings": {"color": "#ff0000",
                                                       "size": 4}},
                            "result": {"ok": True,
                                       "current_tool": {"tool": "pen",
                                                        "color": "#ff0000",
                                                        "size": 4}}},
                           {"type": "call", "seq": 2, "tool": "use_tool",
                            "ts": "2026-09-13T10:10:28.500",
                            "elapsed_ms": 2500,
                            "arguments": {"mode": "path", "closed": False,
                                          "path": {"points": [
                                              [100, 100], [200, 200],
                                              [300, 100]]},
                                          "settings": {"tool": "pen",
                                                       "color": "#ff0000",
                                                       "size": 4}},
                            "result": {"ok": True, "changed": True,
                                       "action": "draw"}},
                       ]}

        def on_executor(ex):
            box["executor"] = ex
            panel["tool_state"] = ex.current_tool_state()
            panel["revision"] += 1

        def on_call(rec, result, step_no):
            desc = result if result is not None else (rec.get("result") or {})
            entry = describe_call(rec.get("tool"), rec.get("arguments") or {},
                                  desc, step_no, ts=rec.get("ts"),
                                  elapsed_ms=rec.get("elapsed_ms"))
            if entry:
                panel["entries"].append(entry)
            ex = box.get("executor")
            if ex is not None:
                panel["tool_state"] = ex.current_tool_state()
            panel["step_no"] = step_no
            panel["revision"] += 1

        def _worker():
            _, st = replay(replay_data, on_call=on_call,
                           on_executor=on_executor)
            box["stats"] = st
            panel["entries"].append(("end", "重放完成：共重放 2 次调用"))
            panel["timing_lines"] = ["记录时间：[10:10:28 +2.5s]",
                                     "重放用时：0.4 秒"]
            panel["revision"] += 1

        wt = threading.Thread(target=_worker, daemon=True)
        wt.start()
        wt.join(timeout=30)
        check("重放 worker 完成", box.get("stats") is not None)
        check("重放统计含时长(记录时长 + 重放用时)",
              box["stats"].get("recorded_ms") == 2500
              and isinstance(box["stats"].get("replay_ms"), int)
              and isinstance(box["stats"].get("pure_ms"), int))
        check("面板步数统计", panel["step_no"] == 1)
        check("面板历史行完整",
              [k for k, _ in panel["entries"]] ==
              ["info", "action", "end"])
        check("历史行带记录时间戳",
              panel["entries"][0][1].startswith("[10:10:26 +0.0s]")
              and panel["entries"][1][1].startswith("[10:10:28 +2.5s]"),
              panel["entries"])
        viewer = ReplayViewer(frame_path, panel, interval_ms=30)
        viewer._tick_qt()
        check("左栏渲染当前工具", "硬笔" in viewer._tool_view.text())
        check("左栏渲染时间信息",
              "重放用时" in viewer._tool_view.text()
              and "记录时间" in viewer._tool_view.text())
        doc = viewer._history_view.toPlainText()
        check("右栏渲染历史记录",
              "选好工具" in doc and "第 1 步" in doc and "重放完成" in doc)
        check("右栏历史行带时间戳", "[10:10:26 +0.0s]" in doc
              and "[10:10:28 +2.5s]" in doc, doc)
        check("中栏渲染画布帧",
              viewer._canvas.pixmap() is not None
              and not viewer._canvas.pixmap().isNull())
        # 自适应缩放: 窗口变小后画作仍等比完整落在画布区域内
        viewer._win.resize(900, 500)
        app.processEvents()
        viewer._scale_and_show_qt()
        pm = viewer._canvas.pixmap()
        cw, ch = viewer._canvas.width(), viewer._canvas.height()
        check("窗口缩放后画作完整适配",
              pm is not None and not pm.isNull()
              and pm.width() <= cw + 1 and pm.height() <= ch + 1
              and pm.width() > 0 and pm.height() > 0)
        viewer.close()

    win.close()
    app.processEvents()
    print(f"GUI 测试: {PASS} 通过, {FAIL} 失败", flush=True)
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    test_gui_smoke()
    print("GUI 冒烟测试全部通过", flush=True)
