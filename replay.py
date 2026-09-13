# -*- coding: utf-8 -*-
"""重放绘画过程: 读取 finish 导出的 drawing-process-*.json, 按原顺序重放
**每一次**工具调用, 复现整幅画, 并校验重放结果与原始记录一致。

用法:
    python replay.py drawing-process-20260912-183000.json
    python replay.py <过程JSON> --out replay.png        # 指定输出文件
    python replay.py <过程JSON> --verbose               # 逐条打印每个动作
    python replay.py <过程JSON> --watch [--step-ms 200] # 边重放边旁观

忠实重放(与原始会话逐条对齐):
- 失败的工具调用照原样再执行一次 —— 同样的参数通常会得到同样的失败, 因此
  模型"走错的路"也会被完整重演, 而不是被静默跳过。
- undo / redo 也按原顺序重放, 所以"画了又撤、撤了又重做"的中间过程都能还原。
- 唯一跳过的是 get_current_picture(纯观察, 不改变画布)。
- 每执行完一次调用, 都与过程 JSON 里记录的原始结果比对(ok / changed /
  action_id / undone_action_id / stroke_id / filled_pixels 等), 不一致会
  列出来 —— 有差异说明重放没能忠实还原, 退出码为 1。

- --watch 时打开三栏重放观察窗(左=当前工具 / 中=画布 / 右=历史记录,
  布局与作画观察窗一致, 自动选 Qt/tk 后端), 窗口关闭即结束。
- 重放结束后输出最终 PNG(默认与过程 JSON 同目录, 文件名加 replay- 前缀)。
- 每条历史行都带时间戳(原始记录里的墙钟时间 + 距第一次调用的用时),
  末尾汇总"记录时长(原始会话画了多久)"与"重放用时(这次重放跑了多久)"。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from draw_app import (PROCESS_SCHEMA, format_clock,  # noqa: E402
                      format_duration, format_timeline)

# 纯观察类工具: 不改变画布, 重放无意义(get_canvas_info 会照常重放, 无害)
SKIP_TOOLS = {"get_current_picture"}
UNDO_REDO_TOOLS = {"undo", "redo"}

# 判定"重放是否忠实"要比对的字段: 覆盖状态变化、撤销/重做目标与擦除结果
_COMPARE_KEYS = ("ok", "changed", "action", "action_id", "stroke_id",
                 "undone_action_id", "redone_action_id", "removed_stroke_id",
                 "filled_pixels")


def load_process(path: Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != PROCESS_SCHEMA:
        raise ValueError(f"不是本 skill 导出的绘画过程文件 "
                         f"(schema={data.get('schema')!r}): {path}")
    return data


def _signature(result) -> dict:
    """取结果的"可比对签名"(只留下与还原度有关的字段)。"""
    if not isinstance(result, dict):
        return {}
    return {k: result.get(k) for k in _COMPARE_KEYS if k in result}


def _diff_ms(a, b):
    """两个 ISO 时间戳之间相差多少毫秒(解析失败返回 None)。"""
    from datetime import datetime
    try:
        return int((datetime.fromisoformat(str(b))
                    - datetime.fromisoformat(str(a))).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None


def _recorded_span(data: dict):
    """从过程记录推算 (started_at, ended_at, 总时长毫秒)。

    新版本导出的过程 JSON 自带 ``duration_ms`` / ``started_at`` / ``ended_at``,
    直接采用; 旧文件(或测试构造的最小记录)没有这些字段, 就从各条记录的
    ``ts`` / ``elapsed_ms`` 推算; 什么都取不到时返回 (None, None, None)。
    """
    calls = [r for r in (data.get("calls") or []) if isinstance(r, dict)]
    started = data.get("started_at")
    ended = data.get("ended_at")
    stamps = [r.get("ts") for r in calls if r.get("ts")]
    started = started or (stamps[0] if stamps else None)
    ended = ended or (stamps[-1] if stamps else None)
    total = data.get("duration_ms")
    if total is None:
        offsets = [r.get("elapsed_ms") for r in calls
                   if isinstance(r.get("elapsed_ms"), int)]
        if offsets:
            total = offsets[-1]
        elif started and ended:
            total = _diff_ms(started, ended)
    return started, ended, total


def write_blank_frame(canvas: dict, frame_path: Path) -> None:
    """把帧文件原子覆盖为空白画布(与过程记录同尺寸同底色)。

    用途: 帧文件名是固定的, 上一次重放残留的最终成图会让 --watch 窗口
    一打开就先显示完整旧画 —— 启动重放前先重置为空白初始帧。
    覆盖失败(只读目录等)不致命: 观察窗会退回旧行为, 只是首帧可能是旧的。
    """
    try:
        from PIL import Image
        w = int(canvas.get("width", 1920))
        h = int(canvas.get("height", 1080))
        bg = str(canvas.get("background_color", "#ffffff"))
        tmp = frame_path.with_name(frame_path.name + ".tmp")
        Image.new("RGB", (max(1, w), max(1, h)), bg).save(tmp, "PNG")
        tmp.replace(frame_path)  # 原子替换, 观察者不会读到半截
    except Exception:  # noqa: BLE001
        pass


def replay(data: dict, on_frame=None, step_seconds: float = 0.0,
           on_call=None, on_executor=None):
    """在内存中重放; 返回 (executor, 统计信息 dict)。

    on_call(record, result, step_no): 每次调用后回调(跳过的调用 result 为
    None), 用于逐条打印 --verbose 轨迹。
    on_executor(executor): executor 创建完成后立即回调(在第一次任何调用
    之前), 供 --watch 的帧回调提前拿到 executor 引用。

    统计信息里含时间维度: ``started_at`` / ``ended_at`` / ``recorded_ms``
    (原始会话画了多久, 取自过程记录)与 ``replay_ms`` / ``pure_ms``
    (本次重放的墙钟用时与纯执行用时)。
    """
    from app.skill import create_skill_executor

    canvas = data.get("canvas") or {}
    options = data.get("options") or {}
    executor = create_skill_executor(
        canvas.get("width", 1920), canvas.get("height", 1080),
        background_color=canvas.get("background_color", "#ffffff"),
        max_history_steps=int(options.get("max_history_steps", 100)))
    if on_executor is not None:
        on_executor(executor)
    started_at, ended_at, recorded_ms = _recorded_span(data)
    stats = {"total": 0, "executed": 0, "skipped": 0, "undo_redo": 0,
             "failed_recorded": 0, "failed": 0, "mismatched": [],
             "status": None, "text": None, "errors": [],
             "started_at": started_at, "ended_at": ended_at,
             "recorded_ms": recorded_ms, "replay_ms": 0, "pure_ms": 0}
    t_replay = time.monotonic()
    step_no = 0
    for rec in data.get("calls", []):
        if rec.get("type") != "call":
            continue
        stats["total"] += 1
        tool = rec.get("tool")
        args = rec.get("arguments")
        if not isinstance(args, dict):
            args = {}
        recorded = rec.get("result")
        if not isinstance(recorded, dict):
            recorded = {}
        if not recorded.get("ok"):
            stats["failed_recorded"] += 1
        if tool in SKIP_TOOLS:
            stats["skipped"] += 1
            if on_call is not None:
                on_call(rec, None, step_no)
            continue
        stats["executed"] += 1
        if tool in UNDO_REDO_TOOLS:
            stats["undo_redo"] += 1
        t_call = time.monotonic()
        try:
            result = executor.call_tool(tool, args)
        except Exception as e:  # call_tool 本身不抛, 这里只是兜底
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        stats["pure_ms"] += int((time.monotonic() - t_call) * 1000)
        if not isinstance(result, dict):
            result = {}
        if not result.get("ok"):
            stats["failed"] += 1
            stats["errors"].append({"tool": tool, "error": result.get("error")})
        if result.get("ok") and result.get("changed") and tool == "use_tool":
            step_no += 1
        # 与原始记录比对: 同一份参数重放出来应当得到同样的结果
        sig_a, sig_b = _signature(recorded), _signature(result)
        fields = sorted(k for k in set(sig_a) | set(sig_b)
                        if sig_a.get(k) != sig_b.get(k))
        if fields:
            stats["mismatched"].append({
                "seq": rec.get("seq"), "tool": tool, "fields": fields,
                "recorded": {k: sig_a.get(k) for k in fields},
                "replayed": {k: sig_b.get(k) for k in fields}})
        if on_call is not None:
            on_call(rec, result, step_no)
        if on_frame is not None and step_seconds > 0:
            on_frame()
            time.sleep(step_seconds)
    stats["replay_ms"] = int((time.monotonic() - t_replay) * 1000)
    if data.get("status"):
        stats["status"] = data["status"]
    if data.get("text"):
        stats["text"] = data["text"]
    return executor, stats


def _make_tracer():
    """--verbose: 每步用观察窗同一套自然语言映射打印一行(带时间戳)。"""
    from draw_app import describe_call

    def on_call(rec, result, step_no):
        tool = rec.get("tool")
        stamp = {"ts": rec.get("ts"), "elapsed_ms": rec.get("elapsed_ms")}
        if result is None:
            # 跳过重放的纯观察调用: 也打印出来, 保持轨迹完整
            entry = describe_call(tool, rec.get("arguments") or {},
                                  rec.get("result") or {}, step_no, **stamp)
            if entry is not None:
                print(f"· [{rec.get('seq')}] {entry[1]}（纯观察, 跳过重放）",
                      flush=True)
            return
        entry = describe_call(tool, rec.get("arguments") or {}, result,
                              step_no, **stamp)
        if entry is None:
            return
        kind, text = entry
        mark = {"action": "  ", "info": "· ", "end": "■ "}.get(kind, "  ")
        print(f"{mark}[{rec.get('seq')}] {text}", flush=True)
    return on_call


def _timing_lines(rec: dict, replay_seconds: float = None) -> list:
    """左栏的"时间"两行: 当前这一步的记录时间 + 重放已用时。"""
    lines = []
    stamp = format_timeline(rec.get("ts"), rec.get("elapsed_ms"))
    if stamp:
        lines.append(f"记录时间：{stamp}")
    if replay_seconds is not None:
        lines.append(f"重放用时：{format_duration(replay_seconds * 1000)}")
    return lines


def _duration_summary(stats: dict) -> str:
    """一句话时长汇总(记录时长 + 重放用时)。"""
    parts = []
    if stats.get("recorded_ms") is not None:
        span = ""
        if stats.get("started_at") and stats.get("ended_at"):
            span = (f"（{format_clock(stats['started_at'])} → "
                    f"{format_clock(stats['ended_at'])}）")
        parts.append(f"记录时长 {format_duration(stats['recorded_ms'])}{span}")
    if stats.get("replay_ms") is not None:
        parts.append(f"重放用时 {format_duration(stats['replay_ms'])}")
    return "；".join(parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="重放 AI 绘画过程")
    parser.add_argument("process_json", help="finish 导出的绘画过程 JSON")
    parser.add_argument("--out", default=None,
                        help="最终 PNG 输出路径(默认与过程 JSON 同目录, "
                             "文件名加 replay- 前缀)")
    parser.add_argument("--watch", action="store_true",
                        help="边重放边在观察窗口里看(窗口关闭即结束)")
    parser.add_argument("--step-ms", type=int, default=200,
                        help="--watch 时每笔之间的间隔(毫秒, 默认 200)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="逐条打印每个工具调用(含失败与撤销/重做)")
    args = parser.parse_args(argv)

    src = Path(args.process_json)
    if not src.is_file():
        print(f"文件不存在: {src}", file=sys.stderr)
        return 2
    try:
        data = load_process(src)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"过程文件无效: {e}", file=sys.stderr)
        return 2

    out_path = (Path(args.out) if args.out
                else src.with_name(
                    f"replay-{src.stem.replace('drawing-process-', '')}.png"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tracer = _make_tracer() if args.verbose else None

    if args.watch:
        import io
        import threading

        from draw_app import describe_call
        from replay_viewer import ReplayViewer

        frame_path = src.parent / "replay-frame.png"
        # 帧文件是固定名, 上一次重放残留的最终成图还在里面 —— 若不清掉,
        # 观察窗一打开就会先显示完整的旧画。用"空白画布帧"原子覆盖:
        # 既清除残留, 也作为重放开始前的初始画面。
        write_blank_frame(data.get("canvas") or {}, frame_path)
        # 三栏面板数据(worker 线程写快照, GUI 主线程读), 布局与观察窗一致
        panel = {"entries": [], "tool_state": None, "step_no": 0,
                 "canvas_info": data.get("canvas") or {}, "revision": 0,
                 "timing_lines": _timing_lines({}, replay_seconds=0.0)}
        box = {"done": False}
        try:
            viewer = ReplayViewer(frame_path, panel,
                                  interval_ms=max(30, args.step_ms // 2))
        except RuntimeError as e:
            print(e, file=sys.stderr)
            return 2
        t_start = time.monotonic()

        def on_call(rec, result, step_no):
            if tracer is not None:
                tracer(rec, result, step_no)
            # 右栏历史: 与观察窗同一套自然语言映射(带记录时间戳);
            # 跳过的观察调用用记录里的原始结果描述, 保持轨迹完整
            desc_result = result if result is not None else (
                rec.get("result") or {})
            entry = describe_call(rec.get("tool"),
                                  rec.get("arguments") or {},
                                  desc_result, step_no,
                                  ts=rec.get("ts"),
                                  elapsed_ms=rec.get("elapsed_ms"))
            if entry:
                panel["entries"].append(entry)
            # 左栏当前工具快照(executor 只在 worker 线程访问)
            ex = box.get("executor")
            if ex is not None:
                try:
                    panel["tool_state"] = ex.current_tool_state()
                except Exception:
                    pass
            panel["timing_lines"] = _timing_lines(
                rec, replay_seconds=time.monotonic() - t_start)
            if rec.get("ts"):
                panel["last_ts"] = rec["ts"]
            panel["step_no"] = step_no
            panel["revision"] += 1

        def worker():
            stats = None
            try:
                def frame_cb():
                    img = box["executor"].engine.observation_image(
                        max_side=1400)
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    tmp = frame_path.with_name(frame_path.name + ".tmp")
                    tmp.write_bytes(buf.getvalue())
                    tmp.replace(frame_path)  # 原子替换, 观察者不会读到半截

                def on_executor(ex):
                    # 在第一笔回调之前就把 executor 交给 box / 面板,
                    # 否则 frame_cb 会因 box["executor"] 尚未赋值而 KeyError
                    box["executor"] = ex
                    try:
                        panel["tool_state"] = ex.current_tool_state()
                    except Exception:
                        pass
                    panel["revision"] += 1

                _, stats = replay(
                    data, on_frame=frame_cb,
                    step_seconds=args.step_ms / 1000, on_call=on_call,
                    on_executor=on_executor)
                box["stats"] = stats
                summary = _duration_summary(stats)
                tail = (f"重放完成：共重放 {stats['total']} 次调用"
                        + (f"（{len(stats['mismatched'])} 次与原始记录不一致）"
                           if stats["mismatched"] else "，全部与原始记录一致"))
            except Exception as e:  # noqa: BLE001
                box["error"] = e
                summary = ""
                tail = f"重放失败：{e}"
            panel["entries"].append(("end", tail))
            if summary:
                # 时长汇总单独一行(info 样式): 两个 GUI 后端都能正确换行显示
                panel["entries"].append(("info", summary))
            panel["timing_lines"] = _timing_lines(
                {"ts": panel.get("last_ts"),
                 "elapsed_ms": stats.get("recorded_ms") if stats else None},
                replay_seconds=time.monotonic() - t_start)
            panel["revision"] += 1
            box["done"] = True

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            viewer.run()  # 阻塞直到用户关闭窗口
        finally:
            t.join(timeout=5)
            viewer.close()
        stats = box.get("stats")
        executor = box.get("executor")
        if stats and executor:
            executor.engine.observation_image(max_side=0,
                                              full_resolution=True).save(
                out_path, format="PNG")
            _report(stats, out_path)
            return 0 if not stats["mismatched"] else 1
        if box.get("error"):
            print(f"重放失败: {box['error']}", file=sys.stderr)
        else:
            print("重放未完成(观察窗口在重放结束前被关闭), 未导出最终 PNG。",
                  file=sys.stderr)
        return 1

    executor, stats = replay(data, on_call=tracer)
    executor.engine.observation_image(max_side=0,
                                      full_resolution=True).save(
        out_path, format="PNG")
    _report(stats, out_path)
    return 0 if not stats["mismatched"] else 1


def _report(stats: dict, out_path: Path) -> None:
    lines = [f"重放完成: 记录 {stats['total']} 次调用 "
             f"(执行 {stats['executed']}, 跳过观察 {stats['skipped']})",
             f"  撤销/重做 {stats['undo_redo']} 次; "
             f"记录中的失败调用 {stats['failed_recorded']} 次(按原样重放)"]
    if stats["failed"]:
        lines.append(f"  重放时再次失败 {stats['failed']} 次(与原始记录一致)")
    # 时长汇总: 记录时长(原会话画了多久) + 重放用时(这次跑完用了多久)
    if stats.get("recorded_ms") is not None:
        span = ""
        if stats.get("started_at") and stats.get("ended_at"):
            span = (f"（{format_clock(stats['started_at'])} → "
                    f"{format_clock(stats['ended_at'])}）")
        lines.append(f"  记录时长 {format_duration(stats['recorded_ms'])}{span}")
    if stats.get("replay_ms") is not None:
        extra = ""
        if stats.get("pure_ms") is not None:
            extra = f"（纯执行 {format_duration(stats['pure_ms'])}"
            if stats["executed"] > 0:
                extra += (f"，平均每次 "
                          f"{format_duration(stats['pure_ms'] / stats['executed'])}")
            extra += "）"
        lines.append(f"  重放用时 {format_duration(stats['replay_ms'])}{extra}")
    if stats["mismatched"]:
        lines.append(f"  [!] {len(stats['mismatched'])} 次调用的结果与原始记录"
                     f"不一致 —— 重放未能忠实还原:")
        for m in stats["mismatched"][:10]:
            lines.append(f"    seq {m['seq']} {m['tool']}: {m['fields']} "
                         f"原始={m['recorded']} 重放={m['replayed']}")
    else:
        lines.append("  [ok] 每次调用的结果都与原始记录一致"
                     "(含失败调用与撤销/重做)")
    if stats.get("status"):
        lines.append(f"结束状态: {stats['status']}"
                     + (f"({stats['text']})" if stats.get("text") else ""))
    lines.append(f"最终图片: {out_path}")
    print("\n".join(lines))


if __name__ == "__main__":
    raise SystemExit(main())
