# -*- coding: utf-8 -*-
"""AI 绘画 function-call 风格客户端: 模型每执行一次命令 = 一次工具调用。

用法(均可从任意工作目录执行; 会话与导出都基于**当前工作目录**):

    python draw_cli.py launch_app --width 1920 --height 1080 [--bg #ffffff]
        启动常驻绘画应用(后台进程) + 只读观察窗口, 就绪后返回画布信息。
        若会话已在运行, 则直接返回现有会话信息(幂等)。

    python draw_cli.py pick_tools '{"tool": "pen", "settings": {"size": 20, "color": "#000000"}}'
    python draw_cli.py use_tool '{"mode": "path", "path": {"points": [[100,100],[900,900]]}}'
    python draw_cli.py undo | redo | get_canvas_info
    python draw_cli.py get_current_picture
        把观察图保存为 PNG 并返回路径, 再用 Read 工具看图(不要打印 base64)。
    python draw_cli.py finish '{"summary": "..."}'
    python draw_cli.py itsHardToFinish '{"reason": "..."}'
        两个结束工具: 把最终图片(原始分辨率)与绘画过程 JSON 导出到当前目录。
    python draw_cli.py status    查看运行中的会话
    python draw_cli.py shutdown  关闭后台应用(正常收尾)

会话数据放在 <当前目录>/.ai-draw-session/(mailbox/req.json + resp.json 双槽
通信、session.jsonl 日志、observe-*.png 观察图); 同一目录同时只有一个会话,
且**全程不删除任何文件**(旧会话残留请求靠会话 token 自动失效)。

时间戳: 每次工具调用的返回里都带 ts(墙钟, 毫秒精度) / elapsed_ms(距会话
开始的用时) / elapsed_text / duration_ms(本次调用耗时), finish 另有 timing
总时长汇总; 观察窗与 replay.py 的每条历史/轨迹也都带时间戳与总时长。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from draw_app import (Mailbox, AppDeadError, SESSION_DIR_NAME,  # noqa: E402
                      pid_alive)

LAUNCH_TIMEOUT = 45.0     # 等待后台应用就绪(含 Qt 初始化)
CALL_TIMEOUT = 120.0      # 等待单次工具调用响应

DRAW_TOOLS = ("pick_tools", "use_tool", "undo", "redo", "get_canvas_info",
              "get_current_picture", "finish", "itsHardToFinish")


def _fail(msg: str) -> int:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    return 1


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False))


def _session_dir() -> Path:
    return Path.cwd() / SESSION_DIR_NAME


def _read_app_info(session_dir: Path):
    info_file = session_dir / "app.json"
    try:
        return json.loads(info_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ping_session(session_dir: Path, info: dict, timeout: float = 2.0) -> bool:
    """用 app.json 里的 token 探活; 能拿到响应才算会话真的在运行。"""
    mailbox = Mailbox(session_dir)
    mid = mailbox.post({"command": "ping"}, token=info.get("session_token", ""))
    try:
        mailbox.await_response(mid, timeout=timeout,
                               alive_check=lambda: pid_alive(info.get("pid")))
        return True
    except (AppDeadError, TimeoutError):
        return False


def cmd_launch_app(args) -> int:
    session_dir = _session_dir()
    export_dir = Path.cwd()

    # 幂等: 已有存活会话(且能 ping 通)则直接返回
    info = _read_app_info(session_dir)
    if info and pid_alive(info.get("pid")) and _ping_session(session_dir, info):
        _print({"ok": True, "already_running": True, **info})
        return 0

    app_py = Path(__file__).resolve().parent / "draw_app.py"
    if not app_py.is_file():
        return _fail(f"找不到 draw_app.py: {app_py}")

    # 新会话令牌: 上一次会话残留的请求因 token 不匹配会被应用忽略,
    # 因此无需删除任何旧文件(全程零删除)。
    token = uuid.uuid4().hex

    # 后台应用用无控制台的 pythonw(若存在), 日志重定向到会话目录
    exe = Path(sys.executable)
    app_exe = exe.with_name("pythonw.exe") if os.name == "nt" else exe
    if not app_exe.is_file():
        app_exe = exe
    session_dir.mkdir(parents=True, exist_ok=True)
    log_fp = (session_dir / "app.log").open("ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = (subprocess.DETACHED_PROCESS
                         | subprocess.CREATE_NEW_PROCESS_GROUP)
    proc = subprocess.Popen(
        [str(app_exe), "-X", "utf8", str(app_py),
         "--width", str(args.width), "--height", str(args.height),
         "--bg", args.bg,
         "--session-dir", str(session_dir),
         "--export-dir", str(export_dir),
         "--max-history", str(args.max_history),
         "--observe-max-side", str(args.observe_max_side),
         "--session-token", token],
        cwd=str(export_dir), stdout=log_fp, stderr=log_fp,
        creationflags=creationflags, close_fds=True)

    # 等就绪(app.json 由应用就绪后写入, 且 token 必须是本次的)
    deadline = time.monotonic() + LAUNCH_TIMEOUT
    while time.monotonic() < deadline:
        info = _read_app_info(session_dir)
        if info and info.get("session_token") == token:
            _print({"ok": True, "already_running": False,
                    "session_dir": str(session_dir),
                    "hint": "观察窗口已打开(左=当前工具, 中=画布, 右=历史记录); "
                            "接下来用 pick_tools / use_tool 逐笔作画。"
                            "⛔ 禁止把作画写成脚本、或用 && / ; / 循环把多条命令串成一条"
                            "一次性执行——每条 draw_cli.py 命令必须单独作为一次工具调用发出, "
                            "每画 2~4 笔用 get_current_picture 看一眼。",
                    **info})
            return 0
        if proc.poll() is not None:
            tail = ""
            log_file = session_dir / "app.log"
            if log_file.is_file():
                try:
                    tail = log_file.read_text(
                        encoding="utf-8", errors="replace")[-800:]
                except OSError:
                    pass
            return _fail(f"绘画应用启动即退出(退出码 {proc.returncode})。"
                         f"日志尾部: {tail}")
        time.sleep(0.1)
    return _fail(f"等待绘画应用就绪超时({LAUNCH_TIMEOUT:.0f}s)。"
                 f"日志: {session_dir / 'app.log'}")


def _require_running():
    session_dir = _session_dir()
    info = _read_app_info(session_dir)
    if not info or not pid_alive(info.get("pid")):
        return None, _fail("没有运行中的绘画会话。请先执行: "
                           "python draw_cli.py launch_app")
    return (session_dir, info), None


def cmd_call(tool: str, args_json: str) -> int:
    try:
        arguments = json.loads(args_json) if args_json else {}
    except ValueError as e:
        return _fail(f"arguments 不是合法 JSON: {e}")
    if not isinstance(arguments, dict):
        return _fail("arguments 必须是 JSON 对象。")

    if tool == "get_current_picture":
        # 模型看到的画面一律压缩传输(长边 ≤ 640), 忽略任何原图请求
        arguments = {k: v for k, v in arguments.items()
                     if k != "full_resolution"}

    ctx, err = _require_running()
    if err:
        return 1
    session_dir, info = ctx
    pid = info.get("pid")
    mailbox = Mailbox(session_dir)
    mid = mailbox.post({"tool": tool, "arguments": arguments},
                       token=info.get("session_token", ""))
    try:
        resp = mailbox.await_response(mid, timeout=CALL_TIMEOUT,
                                      alive_check=lambda: pid_alive(pid))
    except AppDeadError as e:
        return _fail(str(e))
    except TimeoutError as e:
        return _fail(str(e))

    result = resp.get("result", resp)
    # get_current_picture: 落盘为 PNG, 返回路径而不是几 MB 的 base64
    if (tool == "get_current_picture" and isinstance(result, dict)
            and result.get("base64_image")):
        import base64
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        png_path = session_dir / f"observe-{stamp}.png"
        png_path.write_bytes(
            base64.b64decode(result.pop("base64_image")))
        result["path"] = str(png_path)
        result["hint"] = ("已保存压缩观察图(长边 ≤640, 节约模型上下文)。"
                          "请立刻用 Read 工具查看该 PNG 文件以观察当前画布, "
                          "再决定下一笔。")
    _print(result)
    return 0 if result.get("ok") else 1


def cmd_shutdown() -> int:
    ctx, err = _require_running()
    if err:
        return 1
    session_dir, info = ctx
    pid = info.get("pid")
    mailbox = Mailbox(session_dir)
    mid = mailbox.post({"command": "shutdown"},
                       token=info.get("session_token", ""))
    try:
        mailbox.await_response(mid, timeout=10.0,
                               alive_check=lambda: pid_alive(pid))
    except (AppDeadError, TimeoutError):
        pass  # 应用已经退了也算关成功
    _print({"ok": True, "bye": True})
    return 0


def cmd_status() -> int:
    session_dir = _session_dir()
    info = _read_app_info(session_dir)
    if not info or not pid_alive(info.get("pid")):
        return _fail("没有运行中的绘画会话。")
    mailbox = Mailbox(session_dir)
    mid = mailbox.post({"command": "ping"},
                       token=info.get("session_token", ""))
    try:
        resp = mailbox.await_response(mid, timeout=5.0,
                                      alive_check=lambda: pid_alive(info["pid"]))
        _print({"ok": True, **info, "ping": resp})
    except (AppDeadError, TimeoutError) as e:
        return _fail(f"应用无响应: {e}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="AI 绘画 function-call 客户端(会话基于当前工作目录)")
    sub = parser.add_subparsers(dest="cmd")

    p_launch = sub.add_parser("launch_app", help="启动绘画应用与观察窗口")
    p_launch.add_argument("--width", type=int, default=1920)
    p_launch.add_argument("--height", type=int, default=1080)
    p_launch.add_argument("--bg", default="#ffffff")
    p_launch.add_argument("--max-history", type=int, default=100)
    p_launch.add_argument("--observe-max-side", type=int, default=640,
                          help="模型可见观察图最长边(默认 640, 节约上下文)")

    for tool in DRAW_TOOLS:
        p = sub.add_parser(tool, help=f"工具调用: {tool}")
        p.add_argument("arguments", nargs="?", default="{}",
                       help="工具参数(JSON 对象, 可选)")

    sub.add_parser("status", help="查看运行中的会话")
    sub.add_parser("shutdown", help="关闭后台绘画应用")
    args = parser.parse_args(argv)

    if args.cmd == "launch_app":
        return cmd_launch_app(args)
    if args.cmd == "shutdown":
        return cmd_shutdown()
    if args.cmd == "status":
        return cmd_status()
    if args.cmd in DRAW_TOOLS:
        return cmd_call(args.cmd, args.arguments)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
