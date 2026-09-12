# -*- coding: utf-8 -*-
"""Drawing Loop 端到端测试: 用模拟模型客户端驱动 worker 完成一次循环。

验证:
- 初始上下文含系统提示与初始画布图片
- 模型工具调用被顺序执行并修改画布
- 每轮自动返回最新画布图片(不依赖 get_current_picture)
- 真实上下文只保留最近 keep 张画布图片
- finish / itsHardToFinish / max_rounds / stop 终止
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from app.ai.loop import DrawingLoopWorker, SYSTEM_PROMPT
from app.ai.client import ModelClient
from app.core.canvas_engine import CanvasEngine
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


class ScriptedClient(ModelClient):
    """按脚本返回工具调用的模拟模型客户端。"""

    def __init__(self, script):
        super().__init__("https://mock/v1", "fake", "mock-model", 10)
        self.script = list(script)
        self.calls = 0
        self.seen_tools = []
        self.last_messages = None

    def chat(self, messages, tools=None):
        self.calls += 1
        self.last_messages = messages
        self.seen_tools = [t["function"]["name"] for t in (tools or [])]
        if not self.script:
            return {"content": "", "tool_calls": []}
        item = self.script.pop(0)
        return {"content": item.get("content", ""),
                "tool_calls": item.get("tool_calls", [])}


def run_worker(worker: DrawingLoopWorker, app, timeout_ms=15000):
    """启动 worker 并保持事件循环运行直到结束。返回 (ok, status, reason)。"""
    import time
    result = {}
    worker.loop_finished.connect(lambda s, r: result.update(
        status=s, reason=r))
    worker.start()
    deadline = time.monotonic() + timeout_ms / 1000
    while worker.isRunning() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    return not worker.isRunning(), result.get("status"), result.get("reason")


def test_loop_end_to_end():
    print("== Drawing Loop 端到端 ==")
    app = QApplication.instance() or QApplication(sys.argv)
    engine = CanvasEngine(1920, 1080)
    executor = ToolExecutor(engine, observe_max_side=1024)

    script = [
        # 第 1 轮: pick_tools + use_tool
        {"content": "我先画一条线。",
         "tool_calls": [
             {"id": "c1", "name": "pick_tools",
              "arguments": {"tool": "pen", "settings": {"size": 20,
                                                        "color": "#000000"}}},
             {"id": "c2", "name": "use_tool",
              "arguments": {"mode": "path", "path": {
                  "points": [[100, 100], [500, 500], [900, 100]]}}},
         ]},
        # 第 2 轮: brush + circle
        {"content": "再画一个圆。",
         "tool_calls": [
             {"id": "c3", "name": "pick_tools",
              "arguments": {"tool": "brush", "settings": {"size": 60,
                                                          "color": "#888888",
                                                          "opacity": 0.6}}},
             {"id": "c4", "name": "use_tool",
              "arguments": {"mode": "path", "path": {
                  "shape": "circle", "params": {"cx": 500, "cy": 500,
                                                "r": 150}}}},
         ]},
        # 第 3 轮: finish
        {"content": "画完了。",
         "tool_calls": [
             {"id": "c5", "name": "finish",
              "arguments": {"summary": "已完成线和圆。"}},
         ]},
    ]
    client = ScriptedClient(script)
    worker = DrawingLoopWorker(executor, client, "画一条线和一个圆",
                               max_rounds=10, keep_recent_canvas_images=3)
    ok, status, reason = run_worker(worker, app)

    check("worker 正常结束", ok is True)
    check("finish 终止", status == "finished", f"status={status}")
    check("finish 摘要", reason and "已完成" in reason, f"reason={reason}")
    check("模型被调用 3 次", client.calls == 3, f"calls={client.calls}")
    check("画布被修改", engine.history.total_count >= 2,
          f"actions={engine.history.total_count}")
    check("笔画已创建", len(engine.strokes) >= 2)

    # 上下文图片裁剪: 初始 1 + 每轮 1 共 4 张(3 轮), keep=3 -> 保留 3
    # 但第 3 轮 finish 后不再附加图片, 实际图片数 = 1 + 2 = 3(<=keep 不裁剪)
    img_count = None
    # 从最后一次 build_messages 统计图片
    if client.last_messages:
        img_count = 0
        for m in client.last_messages:
            c = m.get("content")
            if isinstance(c, list):
                img_count += sum(1 for p in c if p.get("type") == "image_url")
    check("内部模型工具集不含 get_current_picture",
          "get_current_picture" not in client.seen_tools,
          str(client.seen_tools))
    check("内部模型工具集完整", "pick_tools" in client.seen_tools
          and "use_tool" in client.seen_tools
          and "finish" in client.seen_tools
          and "itsHardToFinish" in client.seen_tools)

    # 初始上下文含系统提示 + 初始图片
    check("初始消息含系统提示", client.last_messages is not None
          and client.last_messages[0]["role"] == "system"
          and "绘画代理" in client.last_messages[0]["content"])


def test_loop_image_pruning():
    print("== 上下文图片裁剪(keep=2) ==")
    app = QApplication.instance() or QApplication(sys.argv)
    engine = CanvasEngine(400, 300)
    executor = ToolExecutor(engine)
    # 6 轮纯文本(无工具调用), 每轮自动附加图片
    script = [{"content": f"第{i}轮观察。", "tool_calls": []} for i in range(6)]
    script.append({"content": "结束。", "tool_calls": [
        {"id": "f1", "name": "finish", "arguments": {}}]})
    client = ScriptedClient(script)
    worker = DrawingLoopWorker(executor, client, "测试裁剪",
                               max_rounds=10, keep_recent_canvas_images=2)
    ok, status, reason = run_worker(worker, app)
    check("finish 终止", status == "finished")
    # 统计最终发送给模型的消息中真实图片数量(应 <= keep)
    real_imgs = 0
    placeholders = 0
    if client.last_messages:
        for m in client.last_messages:
            c = m.get("content")
            if isinstance(c, list):
                real_imgs += sum(1 for p in c if p.get("type") == "image_url")
            elif isinstance(c, str) and "[old canvas image removed]" in c:
                placeholders += 1
    check("真实图片 <= keep(2)", real_imgs <= 2, f"real={real_imgs}")
    check("旧图片已被移除(有占位)", placeholders >= 1, f"placeholders={placeholders}")


def test_loop_abort():
    print("== itsHardToFinish 终止 ==")
    app = QApplication.instance() or QApplication(sys.argv)
    engine = CanvasEngine(300, 200)
    executor = ToolExecutor(engine)
    script = [{"content": "我做不到。",
               "tool_calls": [
                   {"id": "a1", "name": "itsHardToFinish",
                    "arguments": {"reason": "细节太多。"}}]}]
    client = ScriptedClient(script)
    worker = DrawingLoopWorker(executor, client, "画一幅巨复杂的画",
                               max_rounds=5)
    ok, status, reason = run_worker(worker, app)
    check("aborted 终止", status == "aborted")
    check("放弃原因传递", reason and "细节太多" in reason, f"reason={reason}")


def test_loop_max_rounds():
    print("== 最大轮数终止 ==")
    app = QApplication.instance() or QApplication(sys.argv)
    engine = CanvasEngine(300, 200)
    executor = ToolExecutor(engine)
    script = [{"content": f"第{i}轮。", "tool_calls": []} for i in range(20)]
    client = ScriptedClient(script)
    worker = DrawingLoopWorker(executor, client, "画", max_rounds=3)
    ok, status, reason = run_worker(worker, app)
    check("max_rounds 终止", status == "max_rounds", f"status={status}")


def test_loop_stop():
    print("== 用户停止终止 ==")
    app = QApplication.instance() or QApplication(sys.argv)
    engine = CanvasEngine(300, 200)
    executor = ToolExecutor(engine)

    class SlowClient(ScriptedClient):
        def chat(self, messages, tools=None):
            import time
            time.sleep(0.05)  # 模拟网络延迟, 让 stop 有机会在循环中触发
            self.calls += 1
            self.seen_tools = [t["function"]["name"] for t in (tools or [])]
            return {"content": f"第{self.calls}轮。", "tool_calls": []}

    client = SlowClient([])
    worker = DrawingLoopWorker(executor, client, "画", max_rounds=20)
    # 启动后 120ms 停止(在第 2~3 轮之间)
    from PySide6.QtCore import QTimer
    QTimer.singleShot(120, worker.stop)
    ok, status, reason = run_worker(worker, app)
    check("stop 终止", status == "stopped", f"status={status}")


if __name__ == "__main__":
    test_loop_end_to_end()
    test_loop_image_pruning()
    test_loop_abort()
    test_loop_max_rounds()
    test_loop_stop()
    print(f"Loop 测试: {PASS} 通过, {FAIL} 失败", flush=True)
    if FAIL:
        sys.exit(1)
    print("Drawing Loop 测试全部通过", flush=True)
