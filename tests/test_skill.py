# -*- coding: utf-8 -*-
"""Skill 模式(外部 agent 框架)验收测试, 严格对应 MISSION.md 第 16 节。

覆盖:
- 对外完整 OpenAI tools JSON(含 get_current_picture)
- 对外可调用的 ToolExecutor(Python 直接调用)
- 不管理模型上下文、不调用模型(结构性验证)
- stdio 桥 JSON Lines 协议往返(任意语言框架路径)
- 应用内 Drawing Loop 不是 Skill 模式运行所必需的
"""
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def _has_timestamp(result) -> bool:
    """工具结果里是否带 ts / elapsed_ms / duration_ms 三件套。"""
    from datetime import datetime
    if not isinstance(result, dict):
        return False
    try:
        datetime.fromisoformat(str(result.get("ts")))
    except (TypeError, ValueError):
        return False
    return (isinstance(result.get("elapsed_ms"), int)
            and isinstance(result.get("duration_ms"), int))


def test_python_skill_entry():
    print("== Python Skill 入口 ==")
    from app.skill import create_skill_executor, SKILL_TOOL_NAMES

    executor = create_skill_executor(1920, 1080)
    tools = executor.get_tools_schema("skill")
    names = [t["function"]["name"] for t in tools]
    check("工具集 8 个且含 get_current_picture", len(names) == 8
          and "get_current_picture" in names, str(names))
    check("工具集与文档一致", set(names) == {
        "pick_tools", "use_tool", "undo", "redo",
        "get_current_picture", "get_canvas_info",
        "finish", "itsHardToFinish"}, str(names))
    check("SKILL_TOOL_NAMES 常量一致", set(SKILL_TOOL_NAMES) == set(names))

    # 完整调用流程(模拟外部 harness 自行编排)
    r = executor.call_tool("pick_tools", {"tool": "brush", "settings": {
        "size": 60, "color": "#2266aa", "opacity": 0.7}})
    check("外部 harness: pick_tools", r.get("ok") is True)
    r = executor.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [300, 400], [700, 300], [900, 800]]}})
    check("外部 harness: use_tool 绘制", r.get("ok") is True
          and r.get("changed") is True and "stroke_id" in r)
    pic = executor.call_tool("get_current_picture", {})
    check("外部 harness: get_current_picture 查看画布(压缩到长边 ≤640)",
          pic.get("ok") is True and pic.get("base64_image")
          and max(pic.get("width"), pic.get("height")) == 640,
          f"size={pic.get('width')}x{pic.get('height')}")
    info = executor.call_tool("get_canvas_info", {})
    check("外部 harness: get_canvas_info", info.get("ok") is True
          and info.get("stroke_count") == 1)
    r = executor.call_tool("undo", {})
    check("外部 harness: undo", r.get("ok") is True
          and r.get("undone_action_id"))
    r = executor.call_tool("redo", {})
    check("外部 harness: redo", r.get("ok") is True
          and r.get("redone_action_id"))
    r = executor.call_tool("finish", {"summary": "外部 harness 完成"})
    check("外部 harness: finish", r.get("ok") is True
          and r.get("status") == "finished")
    r = executor.call_tool("itsHardToFinish", {"reason": "太难"})
    check("外部 harness: itsHardToFinish", r.get("ok") is True
          and r.get("status") == "aborted")

    # 结构性验证: Skill 模式不管理模型上下文、不调模型
    check("executor 无消息上下文存储",
          not hasattr(executor, "messages")
          and not hasattr(executor, "conversation"))
    import app.skill as skill_mod
    src = Path(skill_mod.__file__).read_text(encoding="utf-8")
    check("skill 入口不 import GUI / 模型客户端",
          "PySide6" not in src and "ModelClient" not in src
          and "DrawingLoopWorker" not in src)


def test_stdio_bridge():
    print("== stdio 桥(JSON Lines) ==")
    python = sys.executable  # 用当前解释器, 不写死任何绝对路径
    bridge = ROOT / "bridge_stdio.py"
    proc = subprocess.Popen(
        [python, "-X", "utf8", str(bridge)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def call(obj, timeout=30):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("bridge closed stdout unexpectedly: "
                                   + proc.stderr.read()[:500])
            line = line.strip()
            if line:
                return json.loads(line)
        raise TimeoutError(f"bridge response timeout for {obj}")

    r = call({"command": "ping"})
    check("桥 ping/pong", r.get("ok") is True and r.get("pong") is True)
    check("桥 ping 带起始时间与已用时",
          bool(r.get("started_at")) and isinstance(r.get("elapsed_ms"), int)
          and r.get("calls") == 0, str(r))

    r = call({"command": "get_tools_schema"})
    names = [t["function"]["name"] for t in r.get("tools", [])]
    check("桥返回完整 tools JSON(含 get_current_picture)",
          len(names) == 8 and "get_current_picture" in names, str(names))

    r = call({"command": "init", "width": 800, "height": 600})
    check("桥 init 重建画布", r.get("ok") is True
          and r.get("canvas") == {"width": 800, "height": 600})

    r = call({"id": 10, "tool": "pick_tools",
              "arguments": {"tool": "pen", "settings": {"size": 30,
                                                        "color": "#cc0000"}}})
    check("桥 pick_tools", r.get("id") == 10
          and r["result"].get("ok") is True)
    r_prev = r["result"]

    r = call({"id": 11, "tool": "use_tool",
              "arguments": {"mode": "path", "path": {
                  "shape": "circle", "params": {"cx": 500, "cy": 500,
                                                "r": 200}}}})
    check("桥 use_tool 绘制", r["result"].get("ok") is True
          and r["result"].get("changed") is True)
    # 每次工具调用的结果都带时间戳与用时
    check("桥每次调用带时间戳",
          _has_timestamp(r["result"]) and _has_timestamp(r_prev),
          str(r["result"]))

    r = call({"id": 12, "tool": "get_current_picture", "arguments": {}})
    check("桥 get_current_picture(压缩到长边 ≤640)",
          r["result"].get("ok") is True
          and r["result"].get("base64_image")
          and max(r["result"].get("width"), r["result"].get("height")) == 640,
          f"size={r['result'].get('width')}x{r['result'].get('height')}")

    r = call({"id": 13, "tool": "finish",
              "arguments": {"summary": "done"}})
    check("桥 finish", r["result"].get("status") == "finished")

    # 传输层错误
    proc.stdin.write("not json at all\n")
    proc.stdin.flush()
    line = proc.stdout.readline().strip()
    r = json.loads(line)
    check("非法 JSON 返回错误", r.get("ok") is False and "error" in r)

    r = call({"id": 14, "tool": "unknown_tool", "arguments": {}})
    check("未知工具返回错误", r["result"].get("ok") is False
          and "Unknown tool" in r["result"].get("error", ""))

    r = call({"command": "shutdown"})
    check("桥 shutdown", r.get("bye") is True)
    check("桥 shutdown 带总时长汇总",
          isinstance(r.get("timing", {}).get("total_ms"), int)
          and str(r["timing"].get("total_text", "")).endswith("秒")
          and r["timing"].get("calls") >= 4, str(r.get("timing")))
    proc.wait(timeout=10)
    check("桥进程干净退出", proc.returncode == 0, f"rc={proc.returncode}")


def test_canvas_changed_event():
    """executor 必须把画布变化转发为 canvas_changed 事件(GUI/观察器依赖)。"""
    print("== canvas_changed 事件转发 ==")
    from app.skill import create_skill_executor

    executor = create_skill_executor(400, 300)
    seen = []
    executor.add_event_handler(lambda e, p: seen.append(e))

    executor.call_tool("pick_tools", {"tool": "pen"})
    check("pick_tools 触发 tool_selected", "tool_selected" in seen)

    r = executor.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [300, 300]]}})
    check("绘制触发 canvas_changed", r.get("ok") is True
          and "canvas_changed" in seen, str(seen))

    executor.call_tool("undo", {})
    check("undo 触发 history_changed 与 canvas_changed",
          "history_changed" in seen
          and seen.count("canvas_changed") >= 2, str(seen))

    executor.call_tool("finish", {})
    check("finish 触发 terminated", "terminated" in seen)


def test_bridge_watch_file():
    """桥的 --watch-file 帧导出(供人类旁观作画过程)。"""
    print("== 桥帧导出(--watch-file) ==")
    python = sys.executable  # 用当前解释器, 不写死任何绝对路径
    bridge = ROOT / "bridge_stdio.py"
    watch_path = ROOT / "out" / "test_current.png"
    if watch_path.exists():
        watch_path.unlink()

    proc = subprocess.Popen(
        [python, "-X", "utf8", str(bridge),
         "--watch-file", str(watch_path), "--watch-interval", "0"],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def call(obj, timeout=30):
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("bridge closed: "
                                   + proc.stderr.read()[:500])
            line = line.strip()
            if line:
                return json.loads(line)
        raise TimeoutError("timeout")

    try:
        call({"command": "init", "width": 1200, "height": 800})
        check("启动时导出初始帧", watch_path.exists())
        first_sig = watch_path.stat().st_size if watch_path.exists() else 0

        call({"id": 1, "tool": "pick_tools",
              "arguments": {"tool": "pen", "settings": {"size": 30,
                                                        "color": "#cc0000"}}})
        call({"id": 2, "tool": "use_tool", "arguments": {
            "mode": "path", "path": {"shape": "circle",
                                     "params": {"cx": 500, "cy": 500,
                                                "r": 250}}}})
        deadline = time.monotonic() + 5
        updated = False
        while time.monotonic() < deadline:
            if watch_path.exists() and watch_path.stat().st_size != first_sig:
                updated = True
                break
            time.sleep(0.05)
        check("绘制后帧文件被更新", updated)

        from PIL import Image
        img = Image.open(watch_path)
        check("观察帧为缩放后的 PNG(最长边 1024)",
              img.size == (1024, 682), f"size={img.size}")

        # 帧内容确实包含绘制结果(非全白)
        import numpy as np
        arr = np.asarray(img.convert("RGB"))
        check("观察帧含绘制内容(非全白)", bool((arr != 255).any()))
    finally:
        try:
            call({"command": "shutdown"}, timeout=5)
        except Exception:
            pass
        proc.wait(timeout=10)


def test_watch_viewer_offscreen():
    """只读观察窗口: 帧加载纯函数 + 后端选择 + 不提供任何绘画入口。"""
    print("== 只读观察窗口 ==")
    from watch_viewer import (READ_ONLY, WatchViewer,
                              available_backends, load_frame_png)

    frame = ROOT / "out" / "test_viewer_frame.png"
    frame.parent.mkdir(parents=True, exist_ok=True)
    if frame.exists():
        frame.unlink()

    check("帧文件缺失时返回 None(不抛异常)", load_frame_png(frame) is None)

    from app.core.canvas_engine import CanvasEngine
    from app.tools.executor import ToolExecutor
    engine = CanvasEngine(600, 400)
    ex = ToolExecutor(engine)
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 40,
                                                            "color": "#0033aa"}})
    ex.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [500, 300]]}})
    engine.export_png(str(frame))

    png = load_frame_png(frame)
    check("加载帧文件返回合法 PNG 字节",
          bool(png) and png[:8] == b"\x89PNG\r\n\x1a\n")

    # 半截文件(模拟正在写入)不应抛异常
    half = ROOT / "out" / "test_viewer_half.png"
    half.write_bytes((png or b"")[:40])
    check("读到损坏/半截帧时返回 None", load_frame_png(half) is None)
    half.unlink(missing_ok=True)

    from io import BytesIO
    from PIL import Image
    scaled = load_frame_png(frame, max_side=200)
    check("--max-side 等比缩放仅影响显示",
          bool(scaled) and max(Image.open(BytesIO(scaled)).size) == 200)

    backends = available_backends()
    if not backends:
        print("  [skip] 本机无 PySide6 / tkinter, 跳过窗口外壳测试")
        frame.unlink(missing_ok=True)
        return
    check("检测到可用 GUI 后端", True, str(backends))

    win = WatchViewer(frame, interval_ms=50, max_side=200)
    check("观察窗口加载帧", win.frames >= 1, f"frames={win.frames}")
    check("观察窗口为只读视图(无引擎)",
          win.engine is None and READ_ONLY and win.read_only is True)
    check("观察窗口无任何绘画入口",
          not hasattr(win, "call_tool") and not hasattr(win, "use_tool")
          and not hasattr(win._ui, "engine") and not hasattr(win._ui, "canvas_view"))
    check("观察窗口后端与探测结果一致", win.backend in backends)
    win.close()
    frame.unlink(missing_ok=True)


def main():
    print("Skill 模式(外部 agent 框架)验收测试")
    print("=" * 60)
    test_python_skill_entry()
    test_canvas_changed_event()
    test_stdio_bridge()
    test_bridge_watch_file()
    test_watch_viewer_offscreen()
    print("=" * 60)
    print(f"Skill 测试: {PASS} 通过, {FAIL} 失败")
    if FAIL:
        sys.exit(1)
    print("SKILL MODE ALL TESTS PASSED")


if __name__ == "__main__":
    main()
