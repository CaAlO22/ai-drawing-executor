# -*- coding: utf-8 -*-
"""常驻绘画应用进程: 由 draw_cli.py launch_app 启动(默认以脱离进程方式运行)。

职责(全部只经由 ToolExecutor 完成绘画, 应用本身没有任何人工绘画入口):
- 持有画布引擎与 ToolExecutor(Skill 模式公共入口);
- 轮询命令邮箱(<session>/mailbox), 执行模型发来的工具调用并写回响应;
- 逐条记录完整会话日志(session.jsonl), 供 finish 导出与事后重放;
- 打开只读观察窗口, 让用户实时旁观模型怎么画;
  无 GUI 环境(Quest/服务器)自动降级为纯 headless, 不影响作画;
- finish / itsHardToFinish 时把最终图片与绘画过程 JSON 导出到启动时的
  工作目录(当前目录), 供人类查看与 replay.py 重放。

本模块同时承担 draw_cli.py 与本进程共用的底层设施(命令邮箱 / 会话日志 /
pid 探活), 因此 import 本模块不需要任何第三方依赖。
"""
import argparse
import base64
import collections
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SESSION_DIR_NAME = ".ai-draw-session"
PROCESS_SCHEMA = "ai-draw-process/1"

# 需要 base64 图片但没必要进日志/重放的大字段
_LOG_STRIP_KEYS = {"base64_image"}


def _sanitize_result(result):
    """浅拷贝并去掉超大字段(日志与导出过程里不存 base64 图片)。"""
    if not isinstance(result, dict):
        return result
    return {k: v for k, v in result.items() if k not in _LOG_STRIP_KEYS}


def pid_alive(pid) -> bool:
    """跨平台探活: 进程是否存在(不区分僵尸, 精度足够本用途)。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限发信号


def _write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 不存在或正好读到写一半的内容


# --------------------------------------------------------------------------
# 命令邮箱: 双槽文件协议(固定两个文件名, 全程零删除)
# --------------------------------------------------------------------------
class Mailbox:
    """双槽命令邮箱: ``mailbox/req.json`` 与 ``mailbox/resp.json``。

    设计要点(**不删除任何文件**):
    - 客户端每次请求整体覆盖写 req.json(原子替换), 请求带唯一 id 与会话 token;
    - 应用端只处理 token 匹配、且 id 未处理过的请求, 响应整体覆盖写 resp.json;
    - 客户端只认 ``resp.id == 自己的 id``, 因此固定文件名不会串包。
    好处: 文件数恒定(2 个), 不产生垃圾、不进回收站、不受删除拦截器影响。
    """

    def __init__(self, session_dir):
        self.dir = Path(session_dir) / "mailbox"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.req_path = self.dir / "req.json"
        self.resp_path = self.dir / "resp.json"
        self._seen = collections.deque(maxlen=500)
        self._seen_set = set()

    # ---- 客户端 ----
    def post(self, payload: dict, token: str = "") -> str:
        """整体覆盖写请求, 返回本次请求 id。"""
        mid = uuid.uuid4().hex
        _write_json_atomic(self.req_path, {**payload, "id": mid,
                                           "token": token})
        return mid

    def await_response(self, mid: str, timeout: float = 120.0,
                       alive_check=None) -> dict:
        """等待匹配本 id 的响应; alive_check() 返回 False 时立刻报错。"""
        deadline = time.monotonic() + timeout
        while True:
            data = _read_json(self.resp_path)
            if isinstance(data, dict) and data.get("id") == mid:
                return data
            if alive_check is not None and not alive_check():
                raise AppDeadError(
                    "绘画应用进程已退出(窗口可能被关闭或发生异常)。"
                    "请重新 launch_app。")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等待绘画应用响应超时({timeout:.0f}s)。")
            time.sleep(0.03)

    # ---- 应用端 ----
    def take_request(self, token: str):
        """取出一条待处理请求; 无新请求时返回 None。"""
        obj = _read_json(self.req_path)
        if not isinstance(obj, dict):
            return None
        if token and obj.get("token") != token:
            return None  # 上一次会话的残留请求, 忽略
        mid = obj.get("id")
        if not mid or mid in self._seen_set:
            return None
        return obj

    def mark_seen(self, obj: dict) -> None:
        mid = obj.get("id")
        if mid and mid not in self._seen_set:
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(mid)
            self._seen_set.add(mid)

    def respond(self, obj: dict, payload: dict) -> None:
        _write_json_atomic(self.resp_path, {**payload, "id": obj.get("id")})


class AppDeadError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 会话日志: 每次工具调用逐条追加, finish 时整体导出为可重放的过程 JSON
# --------------------------------------------------------------------------
class SessionLog:
    """会话日志(session.jsonl) + 过程导出(drawing-process-*.json)。"""

    def __init__(self, session_dir, canvas_info: dict, options: dict = None):
        self.path = Path(session_dir) / "session.jsonl"
        self.canvas = dict(canvas_info)
        # 建画布时的参数(重放按同一套参数重建, 保证与原始会话同构)
        self.options = dict(options or {})
        self.records = []
        self._append({"seq": 0, "type": "init", "ts": _now_iso(),
                      "schema": PROCESS_SCHEMA, "canvas": self.canvas,
                      "options": self.options})

    def _append(self, rec: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def log_call(self, tool: str, arguments: dict, result: dict) -> None:
        rec = {"seq": len(self.records) + 1, "type": "call",
               "ts": _now_iso(), "tool": tool,
               "arguments": arguments, "result": _sanitize_result(result)}
        self.records.append(rec)
        self._append(rec)

    def log_terminated(self, status: str, text: str) -> None:
        self._append({"seq": len(self.records) + 1, "type": "terminated",
                      "ts": _now_iso(), "status": status, "text": text})

    def write_process(self, out_path: Path, status: str = None,
                      text: str = None) -> None:
        data = {"schema": PROCESS_SCHEMA, "created_at": _now_iso(),
                "canvas": self.canvas, "options": self.options,
                "status": status, "text": text,
                "calls": self.records}
        _write_json_atomic(Path(out_path), data)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# 自然语言映射: 把工具调用翻译成人类可读的一行(观察窗左/右栏共用)
# --------------------------------------------------------------------------
TOOL_LABELS = {
    "pen": "硬笔", "brush": "软笔", "eraser_area": "区域橡皮",
    "eraser_stroke": "笔画橡皮", "bucket": "油漆桶",
    # 命令级工具(失败提示里也想说人话)
    "pick_tools": "选择工具", "use_tool": "使用工具",
    "undo": "撤销", "redo": "重做",
    "get_current_picture": "查看画面", "get_canvas_info": "查看画布",
    "finish": "完成绘画", "itsHardToFinish": "终止绘画",
}


def tool_label(tool: str) -> str:
    return TOOL_LABELS.get(tool, str(tool))


SHAPE_LABELS = {
    "circle": "圆形", "ellipse": "椭圆", "rect": "矩形",
    "line": "直线", "arc": "圆弧",
}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _fmt_point(x, y) -> str:
    """归一化坐标 → "(x,y)"(坐标本身就是 0~1000 的整数)。"""
    try:
        return f"({int(round(float(x)))},{int(round(float(y)))})"
    except (TypeError, ValueError):
        return ""


def _point_args(args: dict) -> str:
    """point 模式下的 (x,y); 缺参数时返回空串。"""
    if "x" in args and "y" in args:
        return _fmt_point(args["x"], args["y"])
    return ""


def _path_phrase(args: dict) -> str:
    """把 use_tool 的几何参数压成一句人话。

    路径只说**起点→终点**(完整路径对旁观者没有意义, 反而刷屏):
    - point 模式      → "在 (x,y) 点了一个点"
    - points 折线     → "画了 (x0,y0)→(xn,yn)", 闭合时补"(闭合)"
    - shape 形状      → "画了 圆形 / 矩形 / ..."
    - SVG path "d"    → 从数字流取首尾两点; 取不到就说"画了一段路径"
    """
    if args.get("mode") == "point":
        pt = _point_args(args)
        return f"在 {pt} 点了一个点" if pt else "点了一个点"
    path = args.get("path")
    if not isinstance(path, dict):
        return "画了一笔"
    shape = path.get("shape")
    if shape:
        return f"画了 {SHAPE_LABELS.get(str(shape).lower(), str(shape))}"
    pts = path.get("points")
    if isinstance(pts, (list, tuple)) and pts:
        try:
            head, tail = pts[0], pts[-1]
            text = (f"{_fmt_point(head[0], head[1])}"
                    f"→{_fmt_point(tail[0], tail[1])}")
        except (TypeError, IndexError, ValueError):
            return "画了一段路径"
        return f"画了 {text}（闭合）" if args.get("closed") else f"画了 {text}"
    d = path.get("d")
    if isinstance(d, str) and d.strip():
        nums = _NUM_RE.findall(d)
        if len(nums) >= 4:
            return (f"画了 {_fmt_point(nums[0], nums[1])}"
                    f"→{_fmt_point(nums[-2], nums[-1])}")
        return "画了一段路径"
    return "画了一笔"


def _fmt_settings(tool: str, state: dict) -> str:
    """把当前工具设置格式化为一行人类可读文本。"""
    parts = []
    if "color" in state:
        parts.append(f"颜色 {state['color']}")
    if "size" in state and tool in ("pen", "brush"):
        parts.append(f"粗细 {state['size']}")
    if "opacity" in state and tool == "brush":
        parts.append(f"不透明度 {state['opacity']}")
    if "tolerance" in state and tool == "bucket":
        parts.append(f"容差 {state['tolerance']}")
    if "hit_radius" in state and tool == "eraser_stroke":
        parts.append(f"命中半径 {state['hit_radius']}")
    return "，".join(parts)


def describe_call(tool: str, args: dict, result: dict, step_no: int):
    """把一次工具调用翻译成观察窗右栏的一行。

    返回 (kind, text) 或 None(不值得展示的调用)。
    kind: "action" 画布动作 / "info" 辅助操作 / "end" 结束。
    """
    if not isinstance(result, dict) or not result.get("ok"):
        err = (result or {}).get("error", "未知错误")
        return ("info", f"{tool_label(tool)} 执行失败：{err}")
    state = result.get("current_tool") or {}
    if tool == "pick_tools":
        sel = state.get("tool", "")
        return ("info", f"选好工具：{tool_label(sel)}"
                        f"（{_fmt_settings(sel, state)}）")
    if tool == "use_tool":
        action = result.get("action")
        name = tool_label(state.get("tool", ""))
        if not result.get("changed"):
            msg = str(result.get("message") or "").lower()
            if "stroke" in msg:
                why = "这个位置没有命中任何笔画"
            elif state.get("tool") == "bucket" or "fill" in msg:
                why = "这个区域已经是要填充的颜色"
            else:
                why = "没有产生可见变化"
            return ("info", f"{name}：{why}")
        color = args.get("color") or state.get("color") or ""
        lead = f"{color} " if color else ""
        if action == "draw":
            return ("action",
                    f"第 {step_no} 步：{lead}{name}{_path_phrase(args)}")
        if action == "bucket_fill":
            pt = _point_args(args) or "画布中心"
            return ("action",
                    f"第 {step_no} 步：{lead}油漆桶选择 {pt} 填充")
        if action == "erase_area":
            where = _path_phrase(args).removeprefix("画了").strip()
            return ("action",
                    f"第 {step_no} 步：区域橡皮擦掉了 {where} 围成的区域")
        if action == "erase_stroke":
            removed = result.get("removed_stroke_id")
            pt = _point_args(args)
            at = f"在 {pt} " if pt else ""
            tail = f"擦掉了笔画 {removed}" if removed else "擦掉了一笔"
            return ("action", f"第 {step_no} 步：笔画橡皮{at}{tail}")
        return ("action", f"第 {step_no} 步：执行了一次 {tool_label(tool)} 操作")
    if tool == "undo":
        undone = result.get("undone_action_id")
        return ("info", "撤销了上一步" + (f"（{undone}）" if undone else "（无操作可撤销）"))
    if tool == "redo":
        redone = result.get("redone_action_id")
        return ("info", "重做了一步" + (f"（{redone}）" if redone else "（无操作可重做）"))
    if tool == "get_current_picture":
        return ("info", "看了一眼当前画面")
    if tool == "get_canvas_info":
        return ("info", "查看了画布信息")
    if tool == "finish":
        text = result.get("summary") or ""
        return ("end", "完成本次绘画" + (f"：{text}" if text else ""))
    if tool == "itsHardToFinish":
        text = result.get("reason") or ""
        return ("end", "终止本次绘画" + (f"：{text}" if text else ""))
    return None


# --------------------------------------------------------------------------
# 常驻应用本体
# --------------------------------------------------------------------------
class DrawApp:
    """绘画应用: 引擎 + 工具执行器 + 邮箱服务 + 观察窗 + 会话日志。"""

    def __init__(self, session_dir, export_dir, width: int, height: int,
                 background_color: str = "#ffffff",
                 max_history_steps: int = 100, observe_max_side: int = 640,
                 session_token: str = ""):
        # 延迟导入: 本模块被 draw_cli 引用时不强求 numpy/Pillow
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from app.skill import create_skill_executor

        self.session_dir = Path(session_dir)
        self.export_dir = Path(export_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        self.executor = create_skill_executor(
            width, height, background_color=background_color,
            max_history_steps=max_history_steps,
            observe_max_side=observe_max_side)
        self.canvas_info = {"width": self.executor.engine.width,
                            "height": self.executor.engine.height,
                            "background_color": background_color}
        self.log = SessionLog(self.session_dir, self.canvas_info, {
            "max_history_steps": max_history_steps,
            "observe_max_side": observe_max_side,
        })
        self.mailbox = Mailbox(self.session_dir)
        self.session_token = session_token
        self.stop_requested = False
        self.dirty = True           # 画布有变化待重绘
        self.status_text = f"{self.canvas_info['width']}x{self.canvas_info['height']} · pen"
        self.last_terminated = None
        self.export_count = 0
        # 观察窗数据: 左栏当前工具 / 右栏历史记录(自然语言)
        self.history_entries: list = []   # [(kind, text), ...]
        self.step_no = 0                  # 已产生的画布动作步数
        self.panels_revision = 0          # 面板内容版本号(变了才重绘)
        self._panels_rendered = -1
        self.executor.add_event_handler(self._on_event)

    # ---- 事件(注意: 可能在引擎锁内被调用, 只做轻量标记) ----
    def _on_event(self, event: str, payload: dict) -> None:
        if event == "canvas_changed":
            self.dirty = True
            self.panels_revision += 1
        elif event == "tool_selected":
            self.status_text = (f"{self.canvas_info['width']}x"
                                f"{self.canvas_info['height']}"
                                f" · {tool_label(payload.get('tool', '?'))}")
            self.panels_revision += 1
        elif event == "terminated":
            self.last_terminated = payload

    # ---- 观察窗面板内容 ----
    def current_tool_state(self) -> dict:
        return self.executor.current_tool_state()

    def current_tool_lines(self) -> list:
        """左栏: 当前工具及其设置的文本行。"""
        state = self.current_tool_state()
        tool = state.get("tool", "?")
        lines = [f"当前工具：{tool_label(tool)}（{tool}）"]
        detail = _fmt_settings(tool, state)
        lines.append(detail if detail else "（该工具无可调参数）")
        lines.append("")
        lines.append(f"画布：{self.canvas_info['width']} × "
                     f"{self.canvas_info['height']}")
        lines.append(f"已画：{self.step_no} 步")
        lines.append(f"已导出：{self.export_count} 次")
        return lines

    # ---- 请求处理 ----
    def handle_request(self, obj: dict):
        """处理一条请求, 返回 (响应, 是否请求停止)。"""
        if not isinstance(obj, dict):
            return {"ok": False, "error": "Invalid request."}, False
        cmd = obj.get("command")
        if cmd == "shutdown":
            return {"ok": True, "bye": True}, True
        if cmd == "ping":
            return {"ok": True, "pid": os.getpid(),
                    "canvas": self.canvas_info,
                    "calls": len(self.log.records)}, False
        tool = obj.get("tool")
        if not tool:
            return {"ok": False, "error": "Missing 'tool' or 'command'."}, False
        args = obj.get("arguments")
        if not isinstance(args, dict):
            args = {}
        if tool == "get_current_picture":
            # 模型看到的画面一律压缩传输(长边 ≤ observe_max_side, 默认 640),
            # 不接受原图请求 —— 这是节约模型上下文的关键约束。
            args = dict(args)
            args["full_resolution"] = False
        result = self.executor.call_tool(tool, args)
        self.log.log_call(tool, args, result)
        # 右栏历史记录: 每步画布动作计数一次, 辅助操作只记为提示行
        if isinstance(result, dict) and result.get("ok") and result.get(
                "changed") and tool == "use_tool":
            self.step_no += 1
        entry = describe_call(tool, args, result, self.step_no)
        if entry:
            self.history_entries.append(entry)
            self.panels_revision += 1
        if tool in ("finish", "itsHardToFinish") and result.get("ok"):
            status = "finished" if tool == "finish" else "aborted"
            text = result.get("summary") or result.get("reason") or ""
            self.log.log_terminated(status, text)
            try:
                result["exported"] = self.export_outputs(status, text)
            except Exception as e:  # 导出失败不影响工具结果本身
                result["export_error"] = str(e)
        return {"ok": True, "result": result}, False

    def export_outputs(self, status: str, text: str) -> dict:
        """把最终图片(原始分辨率)与绘画过程 JSON 导出到当前目录。"""
        self.export_count += 1
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = "" if status == "finished" else "-aborted"
        png_path = self.export_dir / f"drawing-{stamp}{suffix}.png"
        img = self.executor.engine.observation_image(max_side=0,
                                                     full_resolution=True)
        img.save(png_path, format="PNG")
        proc_path = self.export_dir / f"drawing-process-{stamp}{suffix}.json"
        self.log.write_process(proc_path, status=status, text=text)
        return {"image": str(png_path), "process": str(proc_path)}

    # ---- 主循环 tick(由 GUI 定时器或无头循环驱动) ----
    def tick(self) -> bool:
        """处理一条待处理请求; 返回 True 表示请求停止。"""
        obj = self.mailbox.take_request(self.session_token)
        if obj is None:
            return False
        self.mailbox.mark_seen(obj)   # 先标记, 避免异常时反复重试同一请求
        resp, stop = self.handle_request(obj)
        self.mailbox.respond(obj, resp)
        if stop:
            self.stop_requested = True
            return True
        return False

    def write_app_json(self, viewer: str) -> None:
        _write_json_atomic(self.session_dir / "app.json", {
            "pid": os.getpid(), "started_at": _now_iso(),
            "canvas": self.canvas_info, "viewer": viewer,
            "session_token": self.session_token,
            "session_dir": str(self.session_dir),
            "export_dir": str(self.export_dir),
        })

    # ---- 运行方式 1: 无 GUI(headless) ----
    def run_headless(self, poll_seconds: float = 0.04) -> None:
        self.write_app_json("none")
        print("[draw_app] headless 模式启动(无观察窗口), 邮箱就绪。",
              flush=True)
        try:
            while not self.tick():
                time.sleep(poll_seconds)
        finally:
            self._cleanup()

    # ---- 运行方式 2: 只读观察窗(Qt 优先, tkinter 兜底) ----
    def run_viewer(self) -> str:
        """创建观察窗并进入事件循环; 返回实际使用的后端名。"""
        try:
            self._run_qt()
            return "qt"
        except ImportError:
            pass
        try:
            self._run_tk()
            return "tk"
        except ImportError:
            pass
        self.run_headless()
        return "none"

    def _snapshot_png(self, max_side: int = 1400) -> bytes:
        import io
        img = self.executor.engine.observation_image(max_side=max_side)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _tool_html(self) -> str:
        """左栏 HTML: 当前工具 + 设置。"""
        state = self.current_tool_state()
        tool = state.get("tool", "?")
        detail = _fmt_settings(tool, state) or "（该工具无可调参数）"
        return (f"<b>{tool_label(tool)}</b> "
                f"<span style='color:#6b7280'>({tool})</span><br><br>"
                f"<span style='color:#374151'>{detail}</span><br><br>"
                f"<span style='color:#6b7280'>画布 "
                f"{self.canvas_info['width']} × {self.canvas_info['height']}"
                f"<br>已画 {self.step_no} 步"
                f"<br>已导出 {self.export_count} 次</span>")

    def _history_html(self) -> str:
        """右栏 HTML: 历史记录(自然语言)。"""
        import html as _html
        if not self.history_entries:
            return ("<div style='color:#9ca3af'>（还没有动作，"
                    "模型开始作画后这里会逐条记录）</div>")
        out = []
        for kind, text in self.history_entries:
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

    @staticmethod
    def _make_qt_window():
        """构建观察窗: 左=当前工具 / 中=画布 / 右=历史记录(自然语言)。

        用子类拦截 closeEvent(关窗只隐藏, 不退出)。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMainWindow,
                                       QTextBrowser, QVBoxLayout, QWidget)

        class _ViewerWindow(QMainWindow):
            def closeEvent(self, ev):
                self.hide()
                ev.ignore()

        win = _ViewerWindow()
        win.setWindowTitle("AI 绘画 · 模型作画中（只读）")
        win.resize(1360, 780)
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(6)
        hint = QLabel("用户旁观窗口：画面完全由模型的工具调用产生，本窗口不可绘画。"
                      "关闭窗口不影响后台作画。")
        hint.setStyleSheet("color:#6b7280;font-size:11px;")
        outer.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)

        # 左: 当前工具
        left = QWidget()
        left.setFixedWidth(230)
        left.setStyleSheet("background:#ffffff;border:1px solid #e5e7eb;"
                           "border-radius:6px;")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 10, 10)
        lv.setSpacing(6)
        l_title = QLabel("当前工具")
        l_title.setStyleSheet("font-weight:600;color:#111827;font-size:12px;")
        lv.addWidget(l_title)
        tool_view = QLabel()
        tool_view.setWordWrap(True)
        tool_view.setTextFormat(Qt.RichText)
        tool_view.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        tool_view.setStyleSheet("color:#111827;font-size:12px;")
        lv.addWidget(tool_view, 1)
        row.addWidget(left)

        # 中: 画布(只读)
        canvas_view = QLabel(alignment=Qt.AlignCenter)
        canvas_view.setStyleSheet("background:#f3f4f6;border:1px solid "
                                  "#e5e7eb;border-radius:6px;")
        canvas_view.setMinimumSize(320, 220)
        row.addWidget(canvas_view, 1)

        # 右: 历史记录(自然语言)
        right = QWidget()
        right.setFixedWidth(350)
        right.setStyleSheet("background:#ffffff;border:1px solid #e5e7eb;"
                            "border-radius:6px;")
        rv = QVBoxLayout(right)
        rv.setContentsMargins(10, 10, 10, 10)
        rv.setSpacing(6)
        r_title = QLabel("历史记录")
        r_title.setStyleSheet("font-weight:600;color:#111827;font-size:12px;")
        rv.addWidget(r_title)
        history_view = QTextBrowser()
        history_view.setStyleSheet("background:#f9fafb;border:1px solid "
                                   "#e5e7eb;border-radius:4px;padding:4px;")
        rv.addWidget(history_view, 1)
        row.addWidget(right)

        outer.addLayout(row, 1)
        win.setCentralWidget(central)
        return win, canvas_view, tool_view, history_view

    def _repaint_qt_canvas(self, label) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap
        png = self._snapshot_png()
        pm = QPixmap()
        pm.loadFromData(png, "PNG")
        label.setPixmap(pm.scaled(label.size(), Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation))

    def _update_qt_panels(self, tool_view, history_view) -> None:
        tool_view.setText(self._tool_html())
        history_view.setHtml(
            "<div style='font-family:system-ui,Segoe UI;font-size:12px'>"
            + self._history_html() + "</div>")
        bar = history_view.verticalScrollBar()
        bar.setValue(bar.maximum())   # 自动滚到最新一条

    def _run_qt(self) -> None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QStatusBar

        qapp = QApplication.instance() or QApplication(["draw_app"])
        win, canvas_view, tool_view, history_view = self._make_qt_window()
        win.setStatusBar(QStatusBar())

        def refresh_panels():
            if self.panels_revision != self._panels_rendered:
                self._panels_rendered = self.panels_revision
                try:
                    self._update_qt_panels(tool_view, history_view)
                except Exception:
                    pass

        def on_tick():
            if self.tick():
                refresh_panels()
                QTimer.singleShot(120, qapp.quit)
                return
            if self.dirty:
                self.dirty = False
                try:
                    self._repaint_qt_canvas(canvas_view)
                except Exception:
                    pass
            refresh_panels()
            win.statusBar().showMessage(
                f"{self.status_text} · 已画 {self.step_no} 步 · "
                f"已导出 {self.export_count} 次 · 只读观察")

        timer = QTimer()
        timer.setInterval(40)
        timer.timeout.connect(on_tick)
        timer.start()
        try:
            self._repaint_qt_canvas(canvas_view)
        except Exception:
            pass
        refresh_panels()
        win.show()
        self.write_app_json("qt")
        print("[draw_app] 观察窗口(Qt)已就绪: 左=当前工具, 中=画布, 右=历史记录。",
              flush=True)
        qapp.exec()
        self._cleanup()

    def _run_tk(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.title("AI 绘画 · 模型作画中（只读）")
        root.geometry("1360x780")
        tk.Label(root, anchor="w", justify="left", fg="#6b7280",
                 text="用户旁观窗口：画面完全由模型的工具调用产生，本窗口不可绘画。"
                      "关闭窗口不影响后台作画。").pack(fill="x", padx=8,
                                                      pady=(6, 2))
        body = tk.Frame(root)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        # 左: 当前工具
        left = tk.Frame(body, width=230, bd=1, relief="solid")
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="当前工具", anchor="w",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", padx=8,
                                                     pady=(8, 2))
        tool_text = tk.Label(left, anchor="nw", justify="left", wraplength=200,
                             fg="#374151")
        tool_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # 右: 历史记录(自然语言)
        right = tk.Frame(body, width=350, bd=1, relief="solid")
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        tk.Label(right, text="历史记录", anchor="w",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", padx=8,
                                                     pady=(8, 2))
        history = tk.Text(right, wrap="word", bd=0, bg="#f9fafb",
                          fg="#111827", state="disabled",
                          font=("Segoe UI", 9))
        history.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        history.tag_config("action", foreground="#111827")
        history.tag_config("info", foreground="#6b7280")
        history.tag_config("end", foreground="#6d28d9")

        # 中: 画布(只读)
        canvas = tk.Canvas(body, bg="#f3f4f6", highlightthickness=1,
                           highlightbackground="#e5e7eb")
        canvas.pack(side="left", fill="both", expand=True, padx=8)
        status = tk.Label(root, anchor="w", fg="#374151")
        status.pack(fill="x", padx=8, pady=(0, 6))
        state = {"img": None, "rendered": -1}

        def repaint():
            import base64 as _b64
            png = self._snapshot_png()
            state["img"] = tk.PhotoImage(master=root, data=_b64.b64encode(png))
            canvas.delete("all")
            w = canvas.winfo_width() or state["img"].width()
            h = canvas.winfo_height() or state["img"].height()
            canvas.create_image(w // 2, h // 2, image=state["img"])

        def refresh_panels():
            if state["rendered"] == self.panels_revision:
                return
            state["rendered"] = self.panels_revision
            tool_text.config(text="\n".join(self.current_tool_lines()))
            history.config(state="normal")
            history.delete("1.0", "end")
            if not self.history_entries:
                history.insert("end", "（还没有动作，模型开始作画后这里会逐条记录）\n",
                               "info")
            for kind, text in self.history_entries:
                history.insert("end", text + "\n\n", kind)
            history.config(state="disabled")
            history.see("end")

        def loop():
            if root.winfo_exists():
                try:
                    if self.tick():
                        refresh_panels()
                        root.after(120, root.destroy)
                        return
                    if self.dirty:
                        self.dirty = False
                        repaint()
                    refresh_panels()
                    status.config(text=f"{self.status_text} · 已画 "
                                       f"{self.step_no} 步 · 已导出 "
                                       f"{self.export_count} 次 · 只读观察")
                    root.after(40, loop)
                except tk.TclError:
                    pass  # 窗口已销毁

        def on_close():
            # 只隐藏, 后台继续服务
            try:
                root.withdraw()
            except tk.TclError:
                pass

        root.protocol("WM_DELETE_WINDOW", on_close)
        root.update_idletasks()
        repaint()
        refresh_panels()
        self.write_app_json("tk")
        print("[draw_app] 观察窗口(tk)已就绪: 左=当前工具, 中=画布, 右=历史记录。",
              flush=True)
        root.after(40, loop)
        try:
            root.mainloop()
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        """收尾: 不做任何删除。

        app.json 保留在会话目录里, 客户端靠其中的 pid 判活(进程已退出即视为
        无会话); 下次 launch_app 会用新 token 覆盖它, 旧请求因 token 不匹配
        被忽略 —— 全程零删除, 不产生垃圾文件、不进回收站。
        """


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="AI 绘画常驻应用(一般由 draw_cli.py launch_app 启动)")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--bg", default="#ffffff",
                        help="画布底色 #RRGGBB(默认白色)")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--export-dir", required=True)
    parser.add_argument("--max-history", type=int, default=100)
    parser.add_argument("--observe-max-side", type=int, default=640,
                        help="模型可见观察图的最长边(默认 640, 节约模型上下文)")
    parser.add_argument("--session-token", default="",
                        help="本次会话令牌(由 launch_app 生成; 旧会话残留请求"
                             "因 token 不匹配被忽略)")
    parser.add_argument("--no-viewer", action="store_true",
                        help="不打开观察窗口(无 GUI 环境或测试)")
    args = parser.parse_args(argv)

    app = DrawApp(args.session_dir, args.export_dir, args.width, args.height,
                  background_color=args.bg,
                  max_history_steps=args.max_history,
                  observe_max_side=args.observe_max_side,
                  session_token=args.session_token)
    if args.no_viewer:
        app.run_headless()
        return 0
    app.run_viewer()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
