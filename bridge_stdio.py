# -*- coding: utf-8 -*-
"""外部 Skill 模式 stdio 桥: 让任意语言的 agent 框架调用绘画工具。

本文件位于项目根目录, 整个项目文件夹既是可手工运行的应用, 也是可分发的
skill 包。脚本用 `__file__` 定位自身所在目录并把它加入 sys.path, 因此可从
**任意工作目录**启动, 不依赖项目结构、不写死任何绝对路径。
依赖见 requirements.txt(Python 3.9+ 与 numpy / Pillow)。

协议(JSON Lines, UTF-8):
  请求  {"command": "init"|"get_tools_schema"|"ping"|"shutdown", ...}
       {"id": <any>, "tool": <name>, "arguments": {...}}
  响应  {"ok": true, ...}  或  {"ok": false, "error": "..."}
  工具调用响应: {"id": <any>, "ok": true, "result": {...}}

桥进程只提供工具, 不管理模型上下文, 不调用模型(MISSION 第 16 节)。
init 的 observe_max_side(默认 640)决定返回给模型的观察图最长边:
为节约模型上下文, get_current_picture 一律压缩传输, 不传原图。

可选观察能力(便于人类旁观作画过程, 默认关闭):
  --watch-file out/current.png   每帧自动覆盖导出当前画布
  --watch-scale 1024             观察帧最长边(0 = 原始分辨率)
  --watch-interval 60            节流间隔(毫秒)
导出采用"临时文件 + os.replace"原子替换, 避免观察者读到半截文件。
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

# 把脚本所在目录(即项目根)加入 sys.path, 以便 import 同目录的 app 包。
# 使用 __file__ 相对定位, 不依赖调用方的工作目录, 也不写死任何绝对路径。
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 下强制 stdout 为 UTF-8(JSON 内含中文)
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    from app.skill import (CanvasEngine, ToolExecutor,  # noqa: E402
                           create_skill_executor)
except ImportError as _e:  # 缺第三方依赖时给出可操作的提示
    print(json.dumps({
        "ok": False,
        "error": f"无法加载绘画引擎: {_e}. 请先安装依赖: "
                 f"python -m pip install -r requirements.txt",
    }, ensure_ascii=False), flush=True)
    raise SystemExit(1)

DEFAULTS = {
    "width": 1920,
    "height": 1080,
    "background_color": "#ffffff",
    "max_history_steps": 100,
    "observe_max_side": 640,
}


class FrameWatcher:
    """把当前画布按节流策略原子导出到指定文件, 供外部观察者查看。"""

    def __init__(self, path: str, scale: int, interval_ms: int):
        self.path = Path(path) if path else None
        self.scale = int(scale)
        self.interval = max(0, int(interval_ms)) / 1000.0
        self.dirty = True
        self._last = 0.0

    def mark(self) -> None:
        self.dirty = True

    def flush(self, engine: CanvasEngine, force: bool = False) -> None:
        if self.path is None or not self.dirty:
            return
        now = time.monotonic()
        if not force and (now - self._last) < self.interval:
            return  # 节流: 下一次调用或退出时补写
        self._last = now
        self.dirty = False
        try:
            img = engine.observation_image(
                max_side=(self.scale or 10 ** 9),
                full_resolution=(self.scale <= 0))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            img.save(tmp, format="PNG")
            os.replace(tmp, self.path)  # 原子替换
        except Exception as e:  # 观察能力失败不能影响工具调用
            print(f"[watch] frame export failed: {e}", file=sys.stderr)


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _make_executor(cfg: dict, watcher: FrameWatcher) -> ToolExecutor:
    # 走 Skill 模式的公共入口, 与 Python 框架直接调用时完全一致
    executor = create_skill_executor(
        width=int(cfg.get("width", DEFAULTS["width"])),
        height=int(cfg.get("height", DEFAULTS["height"])),
        background_color=str(cfg.get("background_color",
                                     DEFAULTS["background_color"])),
        max_history_steps=int(cfg.get("max_history_steps",
                                      DEFAULTS["max_history_steps"])),
        observe_max_side=int(cfg.get("observe_max_side",
                                     DEFAULTS["observe_max_side"])),
    )
    # 画布变化 -> 标记待导出(导出在响应写出后执行, 不在引擎锁内做重活)
    executor.add_event_handler(
        lambda event, payload: watcher.mark() if event == "canvas_changed" else None)
    watcher.mark()
    return executor


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="AI 绘画执行器 Skill 模式 stdio 桥")
    parser.add_argument("--watch-file", default="",
                        help="每帧自动导出画布到该 PNG 路径(观察用, 默认关闭)")
    parser.add_argument("--watch-scale", type=int, default=1024,
                        help="观察帧最长边像素, 0=原始分辨率(默认 1024)")
    parser.add_argument("--watch-interval", type=int, default=60,
                        help="帧导出节流间隔(毫秒, 默认 60)")
    args = parser.parse_args(argv)

    watcher = FrameWatcher(args.watch_file, args.watch_scale,
                           args.watch_interval)
    executor = _make_executor(DEFAULTS, watcher)
    watcher.flush(executor.engine, force=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _write({"ok": False, "error": "Invalid JSON line."})
            continue
        if not isinstance(req, dict):
            _write({"ok": False, "error": "Request must be a JSON object."})
            continue

        cmd = req.get("command")
        if cmd == "shutdown":
            watcher.flush(executor.engine, force=True)
            _write({"ok": True, "bye": True})
            return 0
        elif cmd == "ping":
            _write({"ok": True, "pong": True})
            continue
        elif cmd == "get_tools_schema":
            # 外部 Skill 模式工具集(含 get_current_picture)
            _write({"ok": True, "tools": executor.get_tools_schema("skill")})
            continue
        elif cmd == "init":
            try:
                cfg = {**DEFAULTS,
                       **{k: v for k, v in req.items() if k in DEFAULTS}}
                executor = _make_executor(cfg, watcher)
                watcher.flush(executor.engine, force=True)
                _write({"ok": True, "ready": True, "canvas": {
                    "width": executor.engine.width,
                    "height": executor.engine.height}})
            except Exception as e:
                _write({"ok": False, "error": f"init failed: {e}"})
            continue

        tool = req.get("tool")
        if tool is None:
            _write({"ok": False,
                    "error": "Request must contain 'command' or 'tool'."})
            continue
        result = executor.call_tool(str(tool), req.get("arguments") or {})
        _write({"id": req.get("id"), "ok": True, "result": result})
        watcher.flush(executor.engine)  # 响应后补一帧(节流)

    # stdin 结束(EOF): 补写最终帧
    watcher.flush(executor.engine, force=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
