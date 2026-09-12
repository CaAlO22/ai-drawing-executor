# -*- coding: utf-8 -*-
"""draw_cli / draw_app / replay 的 function-call 流程测试(全部无头)。

覆盖:
- 命令邮箱 Mailbox 的请求/响应往返(含存活检测);
- 会话日志 SessionLog 的记录与过程导出;
- 无头常驻应用 draw_app --no-viewer 的端到端流程:
  pick_tools → use_tool → get_current_picture → undo → finish → shutdown;
- finish 导出的最终图片与绘画过程 JSON 存在且内容正确;
- replay.py 重放结果与 finish 导出的最终图片逐像素一致。
"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from draw_app import (AppDeadError, Mailbox, SessionLog,  # noqa: E402
                      _sanitize_result, pid_alive)

PY = sys.executable


def _run_cli(args, cwd, timeout=90):
    """以子进程方式执行 draw_cli.py(与真实模型调用路径一致)。"""
    return subprocess.run(
        [PY, "-X", "utf8", str(ROOT / "draw_cli.py")] + args,
        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        timeout=timeout)


class TestSanitize(unittest.TestCase):
    def test_strip_base64(self):
        r = _sanitize_result({"ok": True, "base64_image": "xxx", "w": 1})
        self.assertNotIn("base64_image", r)
        self.assertTrue(r["ok"])

    def test_non_dict_passthrough(self):
        self.assertEqual(_sanitize_result([1, 2]), [1, 2])


class TestPidAlive(unittest.TestCase):
    def test_self_alive(self):
        self.assertTrue(pid_alive(os.getpid()))

    def test_bogus_pid(self):
        self.assertFalse(pid_alive(999999999))
        self.assertFalse(pid_alive(None))


class TestMailbox(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            client = Mailbox(td)
            server = Mailbox(td)

            def responder():
                for _ in range(200):
                    obj = server.take_request("tok")
                    if obj is not None:
                        server.mark_seen(obj)
                        self.assertEqual(obj["tool"], "ping")
                        server.respond(obj, {"ok": True, "echo": obj})
                        return
                    time.sleep(0.01)

            t = threading.Thread(target=responder, daemon=True)
            t.start()
            mid = client.post({"tool": "ping"}, token="tok")
            resp = client.await_response(mid, timeout=5.0)
            self.assertEqual(resp["echo"]["tool"], "ping")
            self.assertEqual(resp["id"], mid)
            t.join(timeout=2)

    def test_stale_token_ignored_and_idempotent(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            server = Mailbox(td)
            client = Mailbox(td)
            client.post({"tool": "use_tool"}, token="old-session")
            self.assertIsNone(server.take_request("new-session"))  # token 不匹配
            obj = server.take_request("old-session")
            self.assertIsNotNone(obj)
            server.mark_seen(obj)
            # 同一请求不会被重复执行
            self.assertIsNone(server.take_request("old-session"))

    def test_alive_check_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mb = Mailbox(td)
            mid = mb.post({"tool": "ping"}, token="tok")
            with self.assertRaises(AppDeadError):
                mb.await_response(mid, timeout=5.0,
                                  alive_check=lambda: False)

    def test_files_are_never_deleted(self):
        """双槽协议: 请求/响应只覆盖写, 不产生删除。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            server = Mailbox(td)
            client = Mailbox(td)
            for i in range(3):
                mid = client.post({"tool": "use_tool", "n": i}, token="t")
                obj = server.take_request("t")
                server.mark_seen(obj)
                server.respond(obj, {"ok": True})
                client.await_response(mid, timeout=5.0)
            names = sorted(p.name for p in (Path(td) / "mailbox").iterdir())
            self.assertEqual(names, ["req.json", "resp.json"])


class TestSessionLog(unittest.TestCase):
    def test_log_and_export(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log = SessionLog(td, {"width": 100, "height": 80,
                                  "background_color": "#ffffff"})
            log.log_call("use_tool", {"mode": "point", "x": 1},
                         {"ok": True, "base64_image": "BIG"})
            log.log_call("undo", {}, {"ok": True})
            log.log_terminated("finished", "done")
            out = Path(td) / "proc.json"
            log.write_process(out, status="finished", text="done")
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(data["calls"]), 2)
            self.assertNotIn("base64_image",
                             data["calls"][0]["result"])
            self.assertEqual(data["status"], "finished")
            # jsonl 也逐条落盘
            lines = (Path(td) / "session.jsonl").read_text(
                encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 4)  # init + 2 calls + terminated


class TestHeadlessEndToEnd(unittest.TestCase):
    """draw_app --no-viewer + draw_cli 全流程。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = Path(tempfile.mkdtemp(prefix="aidraw-cli-test-"))
        cls.export_dir = cls.tmp / "work"
        cls.export_dir.mkdir()
        cls.proc = subprocess.Popen(
            [PY, "-X", "utf8", str(ROOT / "draw_app.py"),
             "--no-viewer", "--width", "320", "--height", "200",
             "--bg", "#ffffff",
             "--session-dir", str(cls.export_dir / ".ai-draw-session"),
             "--export-dir", str(cls.export_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        app_json = cls.export_dir / ".ai-draw-session" / "app.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if app_json.is_file():
                break
            if cls.proc.poll() is not None:
                raise RuntimeError("draw_app 启动即退出")
            time.sleep(0.05)
        else:
            raise RuntimeError("draw_app 就绪超时")

    @classmethod
    def tearDownClass(cls):
        try:
            _run_cli(["shutdown"], cwd=cls.export_dir, timeout=20)
        except Exception:
            pass
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def cli(self, *args):
        r = _run_cli(list(args), cwd=self.export_dir)
        assert r.returncode == 0, f"CLI 失败: {args}\n{r.stdout}\n{r.stderr}"
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_01_status_and_launch_info(self):
        st = self.cli("status")
        self.assertTrue(st["ok"])
        self.assertEqual(st["canvas"]["width"], 320)

    def test_02_draw_undo_finish_replay(self):
        # 逐笔 function-call: 选笔 → 画 → 看一眼 → 撤销 → 重画 → finish
        r = self.cli("pick_tools",
                     json.dumps({"tool": "pen",
                                 "settings": {"size": 30,
                                              "color": "#3366cc"}}))
        self.assertTrue(r["ok"], r)

        r = self.cli("use_tool",
                     json.dumps({"mode": "path",
                                 "path": {"points": [[100, 100],
                                                     [900, 900]]}}))
        self.assertTrue(r["ok"] and r.get("changed"), r)

        # 看画面: 应落盘 observe PNG 而不是返回 base64
        r = self.cli("get_current_picture")
        self.assertTrue(r["ok"], r)
        self.assertNotIn("base64_image", r)
        self.assertIn("path", r)
        self.assertTrue(Path(r["path"]).is_file())

        r = self.cli("undo")
        self.assertTrue(r["ok"] and r.get("undone_action_id"), r)

        r = self.cli("pick_tools",
                     json.dumps({"tool": "bucket",
                                 "settings": {"color": "#ff0000"}}))
        self.assertTrue(r["ok"], r)
        r = self.cli("use_tool", json.dumps({"mode": "point",
                                             "x": 500, "y": 500}))
        self.assertTrue(r["ok"], r)

        # finish: 导出最终图片与绘画过程到当前目录
        r = self.cli("finish", json.dumps({"summary": "测试画作"}))
        self.assertTrue(r["ok"], r)
        exported = r["exported"]
        png = Path(exported["image"])
        proc_json = Path(exported["process"])
        self.assertTrue(png.is_file(), exported)
        self.assertTrue(proc_json.is_file(), exported)
        self.assertIn("drawing", png.name)
        self.assertEqual(png.stat().st_size > 0, True)

        data = json.loads(proc_json.read_text(encoding="utf-8"))
        self.assertEqual(data["status"], "finished")
        self.assertEqual(data["text"], "测试画作")
        tools = [c["tool"] for c in data["calls"]]
        self.assertIn("use_tool", tools)
        self.assertNotIn("base64_image",
                         json.dumps(data["calls"]))

        # replay: 重放结果应与导出的最终图片逐像素一致
        rp = subprocess.run(
            [PY, "-X", "utf8", str(ROOT / "replay.py"), str(proc_json),
             "--out", str(self.tmp / "replay.png")],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(rp.returncode, 0, rp.stdout + rp.stderr)
        from PIL import Image, ImageChops
        a = Image.open(png).convert("RGB")
        b = Image.open(self.tmp / "replay.png").convert("RGB")
        self.assertEqual(a.size, b.size)
        self.assertIsNone(ImageChops.difference(a, b).getbbox())

    def test_03_shutdown(self):
        r = self.cli("shutdown")
        self.assertTrue(r["ok"], r)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(self.proc.poll(), "应用应在 shutdown 后退出")


class TestObservationImageCap(unittest.TestCase):
    """模型获取图片必须是压缩图(长边 ≤640), 不接受原图。"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = Path(tempfile.mkdtemp(prefix="aidraw-cap-test-"))
        cls.export_dir = cls.tmp / "work"
        cls.export_dir.mkdir()
        cls.proc = subprocess.Popen(
            [PY, "-X", "utf8", str(ROOT / "draw_app.py"),
             "--no-viewer", "--width", "1280", "--height", "960",
             "--session-dir", str(cls.export_dir / ".ai-draw-session"),
             "--export-dir", str(cls.export_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        app_json = cls.export_dir / ".ai-draw-session" / "app.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if app_json.is_file():
                break
            if cls.proc.poll() is not None:
                raise RuntimeError("draw_app 启动即退出")
            time.sleep(0.05)
        else:
            raise RuntimeError("draw_app 就绪超时")

    @classmethod
    def tearDownClass(cls):
        try:
            _run_cli(["shutdown"], cwd=cls.export_dir, timeout=20)
        except Exception:
            pass
        cls.proc.terminate()
        cls.proc.wait(timeout=10)

    def test_cap_640_and_ignore_full_resolution(self):
        r = _run_cli(["pick_tools", json.dumps(
            {"tool": "pen", "settings": {"size": 60, "color": "#000000"}})],
            cwd=self.export_dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = _run_cli(["use_tool", json.dumps(
            {"mode": "path", "path": {"points": [[50, 50], [950, 950]]}})],
            cwd=self.export_dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # 默认: 1280x960 画布的观察图长边应压缩到 640(高度 480)
        r = _run_cli(["get_current_picture"], cwd=self.export_dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pic = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertTrue(pic["ok"], pic)
        self.assertEqual(max(pic["width"], pic["height"]), 640,
                         f"{pic['width']}x{pic['height']}")
        self.assertNotIn("base64_image", pic)
        self.assertTrue(Path(pic["path"]).is_file())

        # 即使显式请求原图, 也仍然只给压缩图
        r = _run_cli(["get_current_picture",
                      json.dumps({"full_resolution": True})],
                     cwd=self.export_dir)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pic2 = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(max(pic2["width"], pic2["height"]), 640,
                         f"{pic2['width']}x{pic2['height']}")

        # finish 导出的才是原始分辨率(1280x960)
        r = _run_cli(["finish", json.dumps({"summary": "cap test"})],
                     cwd=self.export_dir)
        fin = json.loads(r.stdout.strip().splitlines()[-1])
        from PIL import Image
        self.assertEqual(Image.open(fin["exported"]["image"]).size,
                         (1280, 960))
        _run_cli(["shutdown"], cwd=self.export_dir, timeout=20)


class TestViewerPanels(unittest.TestCase):
    """观察窗左/右栏数据: 当前工具 + 历史记录(自然语言)。"""

    def setUp(self):
        import tempfile
        from draw_app import DrawApp
        self.tmp = Path(tempfile.mkdtemp(prefix="aidraw-panel-test-"))
        self.app = DrawApp(self.tmp / "session", self.tmp / "work",
                           640, 480)

    def _call(self, tool, args=None):
        resp, stop = self.app.handle_request(
            {"tool": tool, "arguments": args or {}})
        self.assertFalse(stop)
        return resp["result"]

    def test_history_is_natural_language(self):
        self._call("pick_tools", {"tool": "pen",
                                  "settings": {"size": 30,
                                               "color": "#3366cc"}})
        self._call("use_tool", {"mode": "path",
                                "path": {"points": [[100, 100], [900, 900]]}})
        self._call("get_current_picture")
        self._call("undo")
        self._call("redo")
        self._call("pick_tools", {"tool": "bucket",
                                  "settings": {"color": "#ffdd55"}})
        self._call("use_tool", {"mode": "point", "x": 320, "y": 240})

        kinds = [k for k, _ in self.app.history_entries]
        texts = [t for _, t in self.app.history_entries]
        joined = " | ".join(texts)

        # 工具选择/看画面/撤销重做 记为提示行, 画布动作记为步骤行
        self.assertIn("action", kinds)
        self.assertIn("info", kinds)
        self.assertIn("硬笔", joined)
        self.assertIn("选好工具：硬笔", joined)   # 不是工具名 pick_tools
        self.assertNotIn("选好工具：pick_tools", joined)
        self.assertIn("第 1 步", joined)      # 画了一笔
        self.assertIn("第 2 步", joined)      # 油漆桶填充
        # 画布动作行: (颜色) 工具 动作 (路径只报起点→终点 / 点坐标)
        self.assertIn("#3366cc 硬笔画了 (100,100)→(900,900)", joined)
        self.assertIn("#ffdd55 油漆桶选择 (320,240) 填充", joined)
        self.assertIn("撤销", joined)
        self.assertIn("看了一眼当前画面", joined)
        # 不出现 base64 / 原始 JSON 之类的机器文本
        self.assertNotIn("{", joined)
        self.assertEqual(self.app.step_no, 2)

    def test_current_tool_panel_lines(self):
        self._call("pick_tools", {"tool": "brush",
                                  "settings": {"size": 60,
                                               "color": "#2266aa",
                                               "opacity": 0.7}})
        lines = self.app.current_tool_lines()
        text = "\n".join(lines)
        self.assertIn("软笔", text)
        self.assertIn("#2266aa", text)
        self.assertIn("粗细 60", text)
        self.assertIn("不透明度 0.7", text)
        self.assertIn("画布：640 × 480", text)
        self.assertIn("已画：0 步", text)

    def test_unchanged_action_is_not_counted(self):
        self._call("pick_tools", {"tool": "eraser_stroke"})
        self._call("use_tool", {"mode": "point", "x": 500, "y": 500})
        self.assertEqual(self.app.step_no, 0)
        self.assertTrue(any("没有命中任何笔画" in t
                            for _, t in self.app.history_entries))

    def test_path_line_reports_only_start_and_end(self):
        """右栏路径只写起点→终点, 不刷完整路径。"""
        self._call("pick_tools", {"tool": "pen",
                                  "settings": {"color": "#123456"}})
        self._call("use_tool", {
            "mode": "path", "closed": True,
            "path": {"points": [[10, 20], [300, 400], [500, 600],
                                [700, 800]]}})
        text = self.app.history_entries[-1][1]
        self.assertIn("#123456 硬笔画了 (10,20)→(700,800)（闭合）", text)
        self.assertNotIn("300,400", text)   # 中间点不出现在右栏
        self.assertNotIn("500,600", text)

    def test_shape_line_reports_shape_name(self):
        self._call("pick_tools", {"tool": "pen",
                                  "settings": {"color": "#abcdef"}})
        self._call("use_tool", {"mode": "path", "path": {
            "shape": "circle", "params": {"cx": 500, "cy": 500, "r": 200}}})
        text = self.app.history_entries[-1][1]
        self.assertIn("#abcdef 硬笔画了 圆形", text)

    def test_erase_and_failure_lines(self):
        self._call("pick_tools", {"tool": "brush",
                                  "settings": {"color": "#009966"}})
        self._call("use_tool", {"mode": "path",
                                "path": {"points": [[100, 100], [500, 500]]}})
        self._call("pick_tools", {"tool": "eraser_stroke"})
        self._call("use_tool", {"mode": "point", "x": 300, "y": 300})
        joined = " | ".join(t for _, t in self.app.history_entries)
        self.assertIn("笔画橡皮在 (300,300) 擦掉了笔画", joined)
        # 失败调用: 翻译成人话, 且不占步数
        self._call("pick_tools", {"tool": "no_such_tool"})
        joined = " | ".join(t for _, t in self.app.history_entries)
        self.assertIn("选择工具 执行失败", joined)
        self.assertIn("Invalid tool", joined)
        self.assertNotIn("pick_tools 执行失败", joined)   # 用中文名而非工具名
        self.assertEqual(self.app.step_no, 2)   # 画笔一笔 + 笔画橡皮一笔

    def test_panels_revision_bumps(self):
        before = self.app.panels_revision
        self._call("pick_tools", {"tool": "pen"})
        self.assertGreater(self.app.panels_revision, before)

    def test_finish_entry_described(self):
        self._call("pick_tools", {"tool": "pen"})
        self._call("use_tool", {"mode": "point", "x": 100, "y": 100})
        self._call("finish", {"summary": "一只猫"})
        last_kind, last_text = self.app.history_entries[-1]
        self.assertEqual(last_kind, "end")
        self.assertIn("完成本次绘画", last_text)
        self.assertIn("一只猫", last_text)


class TestViewerWindowLayout(unittest.TestCase):
    """观察窗三栏布局: 左=当前工具 / 中=画布 / 右=历史记录(离屏渲染)。"""

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication  # noqa: F401
        except ImportError:
            self.skipTest("本机没有 PySide6, 跳过观察窗布局测试")
        import tempfile
        from draw_app import DrawApp
        self.tmp = Path(tempfile.mkdtemp(prefix="aidraw-layout-test-"))
        self.app = DrawApp(self.tmp / "session", self.tmp / "work", 800, 600)
        self.app.handle_request({"tool": "pick_tools", "arguments": {
            "tool": "pen", "settings": {"size": 24, "color": "#123456"}}})
        self.app.handle_request({"tool": "use_tool", "arguments": {
            "mode": "path", "path": {"points": [[100, 100], [900, 900]]}}})
        self.app.handle_request({"tool": "get_current_picture",
                                 "arguments": {}})

    def test_three_panes_render(self):
        from PySide6.QtWidgets import QApplication
        qt = QApplication.instance() or QApplication(["layout-test"])
        win, canvas_view, tool_view, history_view = self.app._make_qt_window()
        self.assertIsNotNone(win.centralWidget())
        self.app._repaint_qt_canvas(canvas_view)
        self.app._update_qt_panels(tool_view, history_view)
        qt.processEvents()

        # 左栏: 当前工具小标题 + 工具内容
        from PySide6.QtWidgets import QLabel
        titles = [w.text() for w in win.findChildren(QLabel)]
        self.assertIn("当前工具", titles)
        self.assertIn("历史记录", titles)
        left = tool_view.text()
        self.assertIn("硬笔", left)
        self.assertIn("粗细 24", left)
        self.assertIn("画布 800 × 600", left)
        # 中栏: 画布已渲染出像素
        self.assertFalse(canvas_view.pixmap().isNull())
        # 右栏: 历史记录为自然语言
        right = history_view.toPlainText()
        self.assertIn("选好工具：硬笔", right)
        self.assertIn("第 1 步", right)
        self.assertIn("看了一眼当前画面", right)
        self.assertNotIn("{", right)      # 不是原始 JSON
        win.close()


class TestReplayFidelity(unittest.TestCase):
    """重放必须忠实还原模型表现: 失败调用、undo/redo 都要重放,
    且每次调用的结果要与原始记录比对一致。"""

    def _session(self):
        """造一份含失败调用 + undo/redo 的会话, 返回 (tmp, 过程JSON)。"""
        import tempfile
        from draw_app import DrawApp
        tmp = Path(tempfile.mkdtemp(prefix="aidraw-replay-test-"))
        app = DrawApp(tmp / "session", tmp / "work", 400, 300)

        def call(tool, args=None):
            resp, _ = app.handle_request(
                {"tool": tool, "arguments": args or {}})
            return resp["result"]

        call("pick_tools", {"tool": "pen",
                            "settings": {"size": 30, "color": "#112233"}})
        call("use_tool", {"mode": "path",
                          "path": {"points": [[100, 100], [900, 900]]}})
        call("get_current_picture")          # 纯观察, 重放时跳过
        call("undo")                         # 撤销
        call("redo")                         # 重做
        call("pick_tools", {"tool": "no_such_tool"})            # 失败
        call("pick_tools", {"tool": "brush",
                            "settings": {"opacity": 9.9}})      # 失败
        call("pick_tools", {"tool": "eraser_stroke"})
        call("use_tool", {"mode": "point", "x": 950, "y": 950})  # 未命中
        fin = call("finish", {"summary": "重放保真测试"})
        return tmp, Path(fin["exported"]["process"])

    def test_replay_executes_failures_and_undo(self):
        tmp, proc = self._session()
        data = json.loads(proc.read_text(encoding="utf-8"))
        calls = [c for c in data["calls"] if c["type"] == "call"]
        tools = [c["tool"] for c in calls]
        self.assertIn("undo", tools)
        self.assertIn("redo", tools)
        n_failed = len([c for c in calls if not c["result"].get("ok")])
        self.assertGreaterEqual(n_failed, 2, calls)

        r = subprocess.run(
            [PY, "-X", "utf8", str(ROOT / "replay.py"), str(proc),
             "--out", str(tmp / "r.png"), "--verbose"],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = r.stdout + r.stderr
        # 结果与原始记录完全一致 → 退出码 0
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("撤销/重做 2 次", out)
        self.assertIn(f"失败调用 {n_failed} 次", out)
        self.assertIn("与原始记录一致", out)
        # --verbose 逐条打印, 含撤销、失败与被跳过的观察调用
        self.assertIn("撤销了上一步", out)
        self.assertIn("选择工具 执行失败", out)
        self.assertIn("这个位置没有命中任何笔画", out)
        self.assertIn("看了一眼当前画面（纯观察, 跳过重放）", out)
        self.assertNotIn("不一致", out)

    def test_divergence_is_detected(self):
        """篡改原始记录的结果 → 重放应发现不一致并以非零退出码报错。"""
        tmp, proc = self._session()
        data = json.loads(proc.read_text(encoding="utf-8"))
        target = next(c for c in data["calls"]
                      if c["type"] == "call" and c["result"].get("ok")
                      and c["result"].get("changed"))
        target["result"] = dict(target["result"], ok=False,
                                error="fabricated")
        bad = tmp / "tampered.json"
        bad.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        r = subprocess.run(
            [PY, "-X", "utf8", str(ROOT / "replay.py"), str(bad),
             "--out", str(tmp / "r2.png")],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("不一致", out)
        self.assertIn("ok", out)


if __name__ == "__main__":
    unittest.main()
