# -*- coding: utf-8 -*-
"""应用内 Drawing Loop: 应用自己调用多模态模型并管理上下文。

- QThread 后台运行, 模型调用不阻塞 GUI 主线程。
- 对模型暴露的工具集不含 get_current_picture;
  每轮所有工具执行完成后自动返回最新画布图片。
- 真实上下文只保留最近 keep_recent_canvas_images 张画布图片。
- 终止条件: finish / itsHardToFinish / 最大轮数 / 用户停止 /
  连续多轮请求失败 / 连续多轮全部工具调用失败。
"""
import base64
import io
import json
import threading

from PySide6.QtCore import QThread, Signal

from ..tools.executor import ToolExecutor
from .client import ModelClient
from .context import ConversationManager

SYSTEM_PROMPT = """你是一个绘画代理，通过工具操作画布。

画布坐标系：
左上角为 (0, 0)，右下角为 (1000, 1000)。
所有坐标和尺寸使用归一化整数 0~1000。
x 按画布宽度换算，y 按画布高度换算；
circle.r / arc.r / size / hit_radius 按画布短边换算。

绘制前，请先调用 pick_tools 选择当前工具。
然后调用 use_tool 执行实际绘画动作。

pen、brush、eraser_area 使用 path 模式。
bucket、eraser_stroke 使用 point 模式。
bucket 从点击点做 4 连通洪水填充，点击点要落在要填的区域内部；
细描边留下的抗锯齿小缝会被自动封住，但轮廓本身必须闭合，
否则填充仍会漫到轮廓外面。
path 支持三种表示之一：points 点列（>=3 点自动平滑）、
d (SVG path, 支持 M L C Q A Z 等命令)、shape+params
(circle/ellipse/rect/line/arc)。

pick_tools 只改变当前工具状态，不会修改画布。
use_tool 才会真正修改画布，并进入撤销历史。
undo / redo 只作用于 use_tool 动作。

作画节奏（重要）：
一笔一笔地画，一次 use_tool 只画一笔或一个部件，不要用一条超长路径
一口气画完整幅画。每轮只画几笔就停下来，看系统自动返回的最新画布图片，
确认位置、比例、走向没问题，再画下一笔。画歪了就 undo 重画这一笔，
不要将错就错往下堆。推荐节奏：轮廓起稿（几笔）→ 看图 → 修形加细节（几笔）
→ 看图 → 用 bucket 一块一块上色 → 看图 → 补高光细节 → finish。

你不需要调用查看画布的工具。
每一轮工具执行完成后，系统会自动向你提供最新画布图片。

任务完成时调用 finish。
如果任务无法完成，调用 itsHardToFinish，并说明原因。"""


def pil_to_data_uri(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


class DrawingLoopWorker(QThread):
    # ---- 线程安全信号(跨线程自动排队到 GUI 线程) ----
    round_started = Signal(int)                       # 轮次(从 1 开始)
    assistant_text = Signal(str)                      # 模型文本输出
    tool_call_made = Signal(str, str)                 # 工具名, 参数摘要
    tool_result = Signal(str)                         # 工具结果摘要
    auto_canvas_image = Signal()                      # 自动返回画布图片
    context_updated = Signal(str)                     # 当前真实上下文摘要
    loop_error = Signal(str)                          # 错误信息
    loop_finished = Signal(str, str)                  # 终止状态, 原因

    def __init__(self, executor: ToolExecutor, client: ModelClient,
                 goal: str, max_rounds: int = 20,
                 keep_recent_canvas_images: int = 3,
                 observe_max_side: int = 640,
                 parent=None):
        super().__init__(parent)
        self.executor = executor
        self.client = client
        self.goal = goal.strip() or "请绘制一幅简洁完整的图片。"
        self.max_rounds = max(1, int(max_rounds))
        self.keep_images = max(1, int(keep_recent_canvas_images))
        self.observe_max_side = max(64, int(observe_max_side))
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    # ------------------------------------------------------------------
    def run(self) -> None:  # noqa: C901
        tools_schema = self.executor.get_tools_schema("loop")
        ctx = ConversationManager()
        ctx.add_system(SYSTEM_PROMPT + f"\n\n绘画目标：{self.goal}")
        ctx.add_user_text(f"绘画目标：{self.goal}\n请开始作画。")
        # 初始画布图片
        img = self.executor.engine.observation_image(self.observe_max_side)
        ctx.add_canvas_image(pil_to_data_uri(img), "初始画布(自动返回):")
        ctx.prune_canvas_images(self.keep_images)
        self.context_updated.emit(ctx.summary())

        consecutive_api_errors = 0
        consecutive_all_fail_rounds = 0
        status, reason = "stopped", ""

        try:
            for round_no in range(1, self.max_rounds + 1):
                if self.stop_requested:
                    status, reason = "stopped", "用户停止了 Drawing Loop。"
                    break
                self.round_started.emit(round_no)
                try:
                    response = self.client.chat(
                        ctx.build_messages(), tools_schema)
                    consecutive_api_errors = 0
                except Exception as e:
                    consecutive_api_errors += 1
                    self.loop_error.emit(f"模型请求失败({consecutive_api_errors}/3): {e}")
                    if consecutive_api_errors >= 3:
                        status, reason = "error", f"连续 {consecutive_api_errors} 轮模型请求失败，循环终止。"
                        break
                    continue

                content = response.get("content", "")
                tool_calls = response.get("tool_calls", [])
                if content.strip():
                    self.assistant_text.emit(content.strip())
                ctx.append_assistant(content, tool_calls)

                if not tool_calls:
                    ctx.add_user_text(
                        "你本轮没有调用任何工具。请继续使用工具作画；"
                        "任务完成时调用 finish，无法完成时调用 itsHardToFinish。")

                terminated = None
                any_call = bool(tool_calls)
                any_success = False
                for tc in tool_calls:
                    if self.stop_requested:
                        break
                    name = tc.get("name", "")
                    args = tc.get("arguments", {}) or {}
                    summary = _args_summary(args)
                    self.tool_call_made.emit(name, summary)
                    result = self.executor.call_tool(name, args)
                    result_text = _result_text(result)
                    self.tool_result.emit(result_text)
                    ctx.append_tool_result(tc.get("id", ""), name,
                                           json.dumps(result, ensure_ascii=False))
                    if result.get("ok") and result.get("changed", False):
                        any_success = True
                    if not result.get("ok"):
                        self.loop_error.emit(f"工具错误 {name}: {result.get('error', '')}")
                    if name == "finish":
                        terminated = ("finished",
                                      result.get("summary") or "任务已完成。")
                        break
                    if name == "itsHardToFinish":
                        terminated = ("aborted",
                                      result.get("reason") or "")
                        break

                if self.stop_requested and terminated is None:
                    status, reason = "stopped", "用户停止了 Drawing Loop。"
                    break
                if terminated is not None:
                    status, reason = terminated
                    break

                # 连续全部工具调用失败检测(防死循环)
                if any_call and not any_success:
                    consecutive_all_fail_rounds += 1
                    if consecutive_all_fail_rounds >= 5:
                        status = "error"
                        reason = "连续 5 轮所有工具调用均未成功修改画布，循环终止。"
                        break
                else:
                    consecutive_all_fail_rounds = 0

                # 本轮工具执行完成: 统一自动返回最新画布图片
                img = self.executor.engine.observation_image(self.observe_max_side)
                ctx.add_canvas_image(pil_to_data_uri(img))
                removed = ctx.prune_canvas_images(self.keep_images)
                self.auto_canvas_image.emit()
                self.context_updated.emit(ctx.summary())
            else:
                status, reason = "max_rounds", f"已达到最大轮数({self.max_rounds})，循环终止。"
        except Exception as e:  # 兜底: 任何异常都不能让应用崩溃
            status, reason = "error", f"Drawing Loop 异常终止: {e}"

        self.loop_finished.emit(status, reason or status)


def _args_summary(args: dict, limit: int = 80) -> str:
    try:
        s = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(args)
    return s if len(s) <= limit else s[:limit] + "..."


def _result_text(result: dict, limit: int = 120) -> str:
    try:
        s = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(result)
    return s if len(s) <= limit else s[:limit] + "..."
