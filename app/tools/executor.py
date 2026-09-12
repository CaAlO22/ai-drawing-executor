# -*- coding: utf-8 -*-
"""ToolExecutor: 接收 AI 工具调用并执行, 与 GUI 完全解耦。

- 外部 Skill 模式与应用内 Drawing Loop 复用同一个执行器。
- call_tool 校验参数、调用 CanvasEngine, 返回 JSON 可序列化 dict。
- 工具失败返回 {"ok": false, "error": ...}, 不抛异常。
- 事件回调(供 GUI 订阅):
    on_event("tool_selected", {"tool": name})
    on_event("canvas_changed", {})
    on_event("history_changed", {"action": "undo"|"redo"})
    on_event("terminated", {"status": "finished"|"aborted", "text": ...})
"""
import base64
import io
import threading
from typing import Callable, Optional

from ..core.canvas_engine import CanvasEngine
from ..core.path_parser import PathError, parse_path_spec
from .schema import get_tools_schema

VALID_TOOLS = ("pen", "brush", "eraser_area", "eraser_stroke", "bucket")

_DEFAULT_SETTINGS = {
    "pen": {"size": 20, "color": "#000000"},
    "brush": {"size": 60, "color": "#888888", "opacity": 0.6},
    "eraser_area": {},
    "eraser_stroke": {"hit_radius": 10},
    "bucket": {"color": "#ff0000", "tolerance": 20},
}


def _clamp_int(v, lo, hi, name):
    try:
        x = int(round(float(v)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid value for '{name}': expected a number.")
    return max(lo, min(hi, x))


def _valid_color(v):
    s = str(v).strip()
    if len(s) == 7 and s[0] == "#":
        try:
            int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
            return s.lower()
        except ValueError:
            pass
    raise ValueError("Invalid color format. Expected #RRGGBB.")


class ToolExecutor:
    """AI 工具执行器。所有绘画能力收敛于此, GUI 不提供人工绘画入口。"""

    def __init__(self, engine: CanvasEngine,
                 observe_max_side: int = 640):
        self.engine = engine
        self.observe_max_side = max(64, int(observe_max_side))
        self._lock = threading.RLock()
        self._current_tool = "pen"
        self._settings = {name: dict(cfg) for name, cfg in _DEFAULT_SETTINGS.items()}
        self._event_handlers: list = []
        # 画布变化 -> 转发为 canvas_changed 事件(供 GUI / 观察器订阅)
        if engine is not None:
            engine.add_listener(self._on_canvas_changed)

    def _on_canvas_changed(self) -> None:
        # 注意: 由 CanvasEngine 在持有引擎锁时调用, 也可能在后台线程;
        # 事件处理函数应保持轻量(只置标记或 emit 信号), 不要在锁内做重活。
        self._emit("canvas_changed", {})

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def add_event_handler(self, fn: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._event_handlers.append(fn)

    def _emit(self, event: str, payload: dict) -> None:
        with self._lock:
            handlers = list(self._event_handlers)
        for fn in handlers:
            try:
                fn(event, payload)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def get_tools_schema(self, mode: Optional[str] = None) -> list:
        """返回 OpenAI tools JSON。

        mode 缺省 "skill"(外部 Skill 模式); "loop" 为应用内 Drawing Loop
        (不含 get_current_picture)。
        """
        return get_tools_schema(mode or "skill")

    # ------------------------------------------------------------------
    # 当前工具状态
    # ------------------------------------------------------------------
    def current_tool_state(self) -> dict:
        with self._lock:
            state = {"tool": self._current_tool}
            state.update(self._settings[self._current_tool])
            return state

    def _state_response(self, message: str = None) -> dict:
        out = {"current_tool": self.current_tool_state()}
        if message:
            out["message"] = message
        return out

    # ------------------------------------------------------------------
    # 工具入口
    # ------------------------------------------------------------------
    def call_tool(self, name: str, arguments: dict) -> dict:
        """执行一次工具调用, 永远返回 JSON 可序列化 dict, 不抛异常。"""
        try:
            if not isinstance(arguments, dict):
                arguments = {}
            if name == "pick_tools":
                return self._pick_tools(arguments)
            elif name == "use_tool":
                return self._use_tool(arguments)
            elif name == "undo":
                return self._undo()
            elif name == "redo":
                return self._redo()
            elif name == "get_canvas_info":
                result = self.engine.get_info(
                    include_geometry=bool(arguments.get("include_geometry", False)))
                result["current_tool"] = self.current_tool_state()
                return result
            elif name == "get_current_picture":
                return self._get_current_picture(arguments)
            elif name == "finish":
                return self._finish(arguments)
            elif name == "itsHardToFinish":
                return self._its_hard_to_finish(arguments)
            else:
                return {"ok": False, "error": f"Unknown tool: {name}."}
        except PathError as e:
            return {"ok": False, "error": str(e)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # 兜底: 任何异常都不能让应用崩溃
            return {"ok": False, "error": f"Tool execution failed: {e}"}

    # ------------------------------------------------------------------
    # pick_tools
    # ------------------------------------------------------------------
    def _pick_tools(self, args: dict) -> dict:
        tool = args.get("tool")
        if tool not in VALID_TOOLS:
            return {"ok": False,
                    "error": "Invalid tool. Expected one of: "
                             + ", ".join(VALID_TOOLS) + "."}
        settings = args.get("settings") or {}
        if not isinstance(settings, dict):
            return {"ok": False, "error": "settings must be an object."}
        with self._lock:
            merged = dict(self._settings[tool])
            try:
                merged = self._apply_settings(tool, merged, settings)
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            self._settings[tool] = merged
            self._current_tool = tool
        self._emit("tool_selected", {"tool": tool})
        out = {"ok": True}
        out.update(self._state_response("Tool selected."))
        return out

    @staticmethod
    def _apply_settings(tool: str, current: dict, settings: dict) -> dict:
        merged = dict(current)
        for key, value in settings.items():
            if key in ("size", "hit_radius") and value is not None:
                merged[key] = _clamp_int(value, 0, 1000, key)
            elif key == "color":
                merged["color"] = _valid_color(value)
            elif key == "tolerance":
                t = _clamp_int(value, 0, 255, "tolerance")
                merged["tolerance"] = t
            elif key in ("opacity", "transparency"):
                # transparency 与 opacity 同义, 冲突时以 opacity 为准
                if "opacity" in settings and key == "transparency":
                    continue
                try:
                    op = float(value)
                except (TypeError, ValueError):
                    raise ValueError("opacity must be a number between 0.3 and 0.9.")
                if not (0.3 - 1e-9 <= op <= 0.9 + 1e-9):
                    raise ValueError("opacity must be between 0.3 and 0.9.")
                merged["opacity"] = min(0.9, max(0.3, op))
            # 未知字段忽略(宽松 schema)
        return merged

    # ------------------------------------------------------------------
    # use_tool
    # ------------------------------------------------------------------
    def _use_tool(self, args: dict) -> dict:
        mode = args.get("mode")
        with self._lock:
            tool = self._current_tool
            settings = dict(self._settings[tool])

        if mode == "path":
            if tool not in ("pen", "brush", "eraser_area"):
                return {"ok": False,
                        "error": f"Current tool is {tool}. use_tool requires "
                                 f"mode=point with x and y."}
            path = args.get("path")
            if path is None:
                return {"ok": False, "error": "mode=path requires a 'path' object."}
            closed = bool(args.get("closed", False))
            if tool == "eraser_area":
                closed = True  # 区域橡皮强制闭合
            if tool == "eraser_area":
                result = self.engine.execute_path_action(
                    path, "eraser_area", True, "#ffffff", 0, 1.0)
                if not result.get("ok"):
                    return result
                out = {"ok": True}
                if result.get("changed"):
                    out["action"] = "erase_area"
                    out["action_id"] = result["action_id"]
                    out["changed"] = True
                else:
                    out["action"] = "erase_area"
                    out["changed"] = False
                    out["message"] = result.get("message", "No change.")
                out.update(self._state_response())
                return out
            # pen / brush
            try:
                segments, path_type, geo = parse_path_spec(
                    path, closed, self.engine.mapper)
            except PathError as e:
                return {"ok": False, "error": str(e)}
            result = self.engine.draw_stroke(
                segments, tool, settings.get("color", "#000000"),
                int(settings.get("size", 20)), float(settings.get("opacity", 1.0)),
                raw_path=path, path_type=path_type, geometry_summary=geo)
            if not result.get("ok"):
                return result
            out = {"ok": True}
            if result.get("changed"):
                out["action"] = "draw"
                out["action_id"] = result["action_id"]
                out["stroke_id"] = result["stroke_id"]
                out["changed"] = True
            else:
                out["action"] = "draw"
                out["changed"] = False
                out["message"] = result.get("message", "No change.")
            out.update(self._state_response())
            return out

        elif mode == "point":
            if tool in ("pen", "brush"):
                # 单点笔画: 在点处绘制一个画笔点
                if "x" not in args or "y" not in args:
                    return {"ok": False,
                            "error": "mode=point requires both 'x' and 'y'."}
                x = _clamp_int(args.get("x"), 0, 1000, "x")
                y = _clamp_int(args.get("y"), 0, 1000, "y")
                p = self.engine.mapper.point(x, y)
                segments = [("M", p[0], p[1])]
                result = self.engine.draw_stroke(
                    segments, tool, settings.get("color", "#000000"),
                    int(settings.get("size", 20)),
                    float(settings.get("opacity", 1.0)),
                    raw_path={"point": [x, y]}, path_type="point",
                    geometry_summary=f"point({x},{y})")
                if not result.get("ok"):
                    return result
                out = {"ok": True}
                if result.get("changed"):
                    out["action"] = "draw"
                    out["action_id"] = result["action_id"]
                    out["stroke_id"] = result["stroke_id"]
                    out["changed"] = True
                else:
                    out["action"] = "draw"
                    out["changed"] = False
                    out["message"] = result.get("message", "No change.")
                out.update(self._state_response())
                return out
            if tool in ("bucket", "eraser_stroke"):
                if "x" not in args or "y" not in args:
                    return {"ok": False,
                            "error": "mode=point requires both 'x' and 'y'."}
                x = _clamp_int(args.get("x"), 0, 1000, "x")
                y = _clamp_int(args.get("y"), 0, 1000, "y")
                px = int(round(x / 1000 * self.engine.width))
                py = int(round(y / 1000 * self.engine.height))
                px = min(px, self.engine.width - 1)
                py = min(py, self.engine.height - 1)
                if tool == "bucket":
                    result = self.engine.bucket_fill(
                        px, py, settings.get("color", "#ff0000"),
                        int(settings.get("tolerance", 20)))
                    if not result.get("ok"):
                        return result
                    out = {"ok": True, "action": "bucket_fill"}
                    if result.get("changed"):
                        out["action_id"] = result["action_id"]
                        out["filled_pixels"] = result["filled_pixels"]
                        out["changed"] = True
                    else:
                        out["changed"] = False
                        out["filled_pixels"] = result.get("filled_pixels", 0)
                        out["message"] = result.get("message", "No change.")
                    out.update(self._state_response())
                    return out
                else:
                    hit_px = self.engine.mapper.size(
                        int(settings.get("hit_radius", 10)))
                    result = self.engine.erase_stroke_at(px, py, hit_px)
                    if not result.get("ok"):
                        return result
                    out = {"ok": True, "action": "erase_stroke"}
                    if result.get("changed"):
                        out["action_id"] = result["action_id"]
                        out["removed_stroke_id"] = result["removed_stroke_id"]
                        out["changed"] = True
                    else:
                        out["removed_stroke_id"] = None
                        out["changed"] = False
                        out["message"] = "No stroke hit at the given point."
                    out.update(self._state_response())
                    return out
            return {"ok": False,
                    "error": f"Current tool is {tool}. use_tool requires "
                             f"mode=path with a path object."}
        else:
            return {"ok": False,
                    "error": "Invalid mode. Expected 'path' or 'point'."}

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------
    def _undo(self) -> dict:
        result = self.engine.undo()
        if result.get("undone_action_id"):
            self._emit("history_changed", {"action": "undo"})
        return result

    def _redo(self) -> dict:
        result = self.engine.redo()
        if result.get("redone_action_id"):
            self._emit("history_changed", {"action": "redo"})
        return result

    # ------------------------------------------------------------------
    # 画布观察(外部 Skill 模式专用)
    # ------------------------------------------------------------------
    def _get_current_picture(self, args: dict) -> dict:
        full = bool(args.get("full_resolution", False))
        img = self.engine.observation_image(
            max_side=self.observe_max_side, full_resolution=full)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "format": "png",
                "width": img.size[0], "height": img.size[1],
                "base64_image": b64}

    # ------------------------------------------------------------------
    # 终止
    # ------------------------------------------------------------------
    def _finish(self, args: dict) -> dict:
        summary = str(args.get("summary") or "")
        self._emit("terminated", {"status": "finished", "text": summary})
        return {"ok": True, "status": "finished", "summary": summary}

    def _its_hard_to_finish(self, args: dict) -> dict:
        reason = str(args.get("reason") or "")
        self._emit("terminated", {"status": "aborted", "text": reason})
        return {"ok": True, "status": "aborted", "reason": reason}
