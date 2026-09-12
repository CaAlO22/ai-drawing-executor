# -*- coding: utf-8 -*-
"""画布引擎: 位图画布 + 笔画元数据 + 动作历史。

职责:
- 维护原始分辨率 RGB 位图(白底, 不透明)。
- 执行绘制(pen/brush)、区域擦除、洪水填充、笔画删除。
- 维护 actions / strokes 两类记录, 支持 undo / redo。
- 线程安全: 所有公共方法持锁; AI Loop 在后台线程调用, GUI 在主线程读取快照。
- 变化通知: 画布变化后回调 listeners(回调内不要做重活)。

笔画删除采用"底图 + 顺序重放"策略:
  被删笔画的 draw 动作保存了其 bbox 的 before 补丁(底图);
  删除时以底图为起点, 按序重放其后所有仍有效动作(笔画矢量重绘, 像素操作贴
  after 补丁), 得到"该笔画从未存在过"的区域状态。
"""
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import numpy as np
from PIL import Image

from .geometry import PixelMapper
from .history import ActionRecord, HistoryManager, compress_patch, decompress_patch
from .path_parser import parse_path_spec
from . import renderer


def _hex_to_rgb(color: str):
    c = str(color).strip()
    if len(c) == 7 and c[0] == "#":
        try:
            return (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16))
        except ValueError:
            pass
    raise ValueError("Invalid color format. Expected #RRGGBB.")


# ---------------------------------------------------------------------------
# 障碍图形态学(仅用 numpy, 不引入 scipy)
# ---------------------------------------------------------------------------

def _dilate8(mask: np.ndarray) -> np.ndarray:
    """3x3 结构元的 8 邻域膨胀(画布外不计入, 结构元不会越过边界回卷)。"""
    h, w = mask.shape
    p = np.zeros((h + 2, w + 2), dtype=bool)
    p[1:-1, 1:-1] = mask
    out = mask.copy()
    for dy in range(3):
        for dx in range(3):
            out |= p[dy:dy + h, dx:dx + w]
    return out


@dataclass
class StrokeMeta:
    stroke_id: str
    tool: str                     # pen / brush
    color: tuple                  # (r, g, b)
    size: int                     # 归一化尺寸
    opacity: float
    path_type: str
    geometry_summary: str
    samples: np.ndarray           # 像素域采样点 (N,2)
    bbox: tuple                   # 笔画影响区域(含笔宽, 画布内)
    created_index: int
    alive: bool = True
    raw_path: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "stroke_id": self.stroke_id,
            "tool": self.tool,
            "color": "#%02x%02x%02x" % self.color,
            "size": self.size,
            "opacity": self.opacity,
            "type": self.path_type,
            "point_count": int(len(self.samples)),
            "alive": self.alive,
        }


class CanvasEngine:
    def __init__(self, width: int, height: int, background_color: str = "#ffffff",
                 max_history_steps: int = 100):
        self.width = max(1, int(width))
        self.height = max(1, int(height))
        self.background = _hex_to_rgb(background_color)
        self._canvas = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        self._canvas[:, :] = self.background
        self._lock = threading.RLock()
        self.mapper = PixelMapper(self.width, self.height)
        self.history = HistoryManager(max_history_steps)
        self.strokes: Dict[str, StrokeMeta] = {}
        self._id_counter = 0
        self._listeners: List[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def add_listener(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    def _next_id(self, prefix: str) -> str:
        self._id_counter += 1
        return f"{prefix}_{self._id_counter:06d}"

    # ------------------------------------------------------------------
    # 绘制: pen / brush
    # ------------------------------------------------------------------
    def draw_stroke(self, segments, tool: str, color: str, size_norm: int,
                    opacity: float, raw_path: dict, path_type: str,
                    geometry_summary: str):
        """沿路径绘制笔画。返回 dict(ok, changed, stroke_id, message...)。"""
        rgb = _hex_to_rgb(color)
        size_px = self.mapper.size(size_norm)
        subpaths = renderer.sample_path_segments(
            segments, step=float(max(1.0, size_px / 4.0)))
        samples = np.vstack([p for p in subpaths if len(p) >= 1]) if subpaths else None
        if samples is None or len(samples) == 0:
            return {"ok": False, "error": "Invalid path: path contains no points."}
        with self._lock:
            bbox, alpha = renderer.render_stroke_alpha(
                samples, tool, size_px, self.width, self.height)
            if bbox is None:
                return {"ok": True, "changed": False,
                        "message": "Path is entirely outside the canvas."}
            x0, y0, x1, y1 = bbox
            before = self._canvas[y0:y1, x0:x1].copy()
            if tool == "brush":
                alpha = np.clip(alpha * float(opacity), 0.0, 1.0).astype(np.float32)
            renderer.composite_color(self._canvas, bbox, rgb, alpha)
            after = self._canvas[y0:y1, x0:x1]
            if np.array_equal(before, after):
                # 无任何像素变化: 不进入历史
                self._canvas[y0:y1, x0:x1] = before
                return {"ok": True, "changed": False,
                        "message": "No visible change on canvas."}
            stroke_id = self._next_id("stroke")
            action_id = self._next_id("action")
            meta = StrokeMeta(
                stroke_id=stroke_id, tool=tool, color=rgb, size=int(size_norm),
                opacity=float(opacity), path_type=path_type,
                geometry_summary=geometry_summary, samples=samples,
                bbox=bbox, created_index=self.history.total_count,
                raw_path=dict(raw_path))
            self.strokes[stroke_id] = meta
            rec = ActionRecord(
                action_id=action_id, action_type="draw", stroke_id=stroke_id,
                bbox=bbox, before=compress_patch(before),
                after=compress_patch(self._canvas[y0:y1, x0:x1]),
                params_summary=f"{tool} size={size_norm} color={color}")
            self.history.commit(rec)
            self._notify()
            return {"ok": True, "changed": True, "stroke_id": stroke_id,
                    "action_id": action_id, "bbox": bbox}

    # ------------------------------------------------------------------
    # 区域擦除
    # ------------------------------------------------------------------
    def erase_area(self, segments):
        """擦除闭合路径区域为背景白色。"""
        white = self.background
        with self._lock:
            bbox, alpha = renderer.render_erase_area_mask(
                segments, self.width, self.height)
            if bbox is None:
                return {"ok": True, "changed": False,
                        "message": "Erase area is entirely outside the canvas."}
            x0, y0, x1, y1 = bbox
            before = self._canvas[y0:y1, x0:x1].copy()
            renderer.composite_color(self._canvas, bbox, white, alpha)
            after = self._canvas[y0:y1, x0:x1]
            if np.array_equal(before, after):
                self._canvas[y0:y1, x0:x1] = before
                return {"ok": True, "changed": False,
                        "message": "No visible change on canvas."}
            action_id = self._next_id("action")
            rec = ActionRecord(
                action_id=action_id, action_type="erase_area",
                bbox=bbox, before=compress_patch(before),
                after=compress_patch(self._canvas[y0:y1, x0:x1]),
                params_summary="eraser_area")
            self.history.commit(rec)
            self._notify()
            return {"ok": True, "changed": True, "action_id": action_id}

    # ------------------------------------------------------------------
    # 洪水填充(bucket)
    # ------------------------------------------------------------------
    def bucket_fill(self, px: int, py: int, color: str, tolerance: int):
        """从像素点开始的 4 连通洪水填充, 逐通道容差比较。

        "连通性判定"与"着色"用两份不同的掩膜, 这是不漏色的关键:

        * 着色掩膜 `fillable` = 与点击点逐通道差 ≤ tolerance 的像素;
        * 连通掩膜 `gate`     = `~fillable`(障碍图) **膨胀 1px 后**的结果。

        细描边(实际笔宽 ≲2px)经抗锯齿后, 描边像素的覆盖率会沿走向周期性地
        掉到容差以下, 于是障碍图退化成一条带 1px 断点的虚线(近水平/近垂直的
        段在换行处还会错开约 2px)。直接对着色掩膜洪水会顺着断点穿到描边
        另一侧, 把整片背景一起染色 —— 这正是"从抗锯齿缝隙里漏色"。

        断点补不上闭运算(膨胀再腐蚀)的窟窿: 对 1px 粗的虚线做腐蚀, 会把刚
        连起来的桥原样啃掉。所以这里**只膨胀不腐蚀**, 让障碍在判定连通时胖
        1px, 封死 ≤2px 的断点。

        代价是连通域会从描边处缩进 1px, 因此最后把连通域膨胀 1px 再与
        `fillable` 取交集, 把这 1px 边缘还回去 —— 正常边界的填充范围
        (以及整幅均匀填充的像素数)因此保持不变。
        """
        rgb = _hex_to_rgb(color)
        tol = int(max(0, min(255, tolerance)))
        w, h = self.width, self.height
        if not (0 <= px < w and 0 <= py < h):
            return {"ok": False,
                    "error": "Click point is outside the canvas."}
        with self._lock:
            target = self._canvas[py, px].astype(np.int16)
            diff = np.abs(self._canvas.astype(np.int16) - target)
            fillable = (diff.max(axis=2) <= tol)
            gate = _dilate8(~fillable)
            if gate[py, px]:
                return {"ok": True, "changed": False, "filled_pixels": 0,
                        "message": "No fillable region at the given point: "
                                   "the point sits on (or right next to) a "
                                   "stroke. Click inside the area you want "
                                   "to fill."}
            core = self._scanline_flood(~gate, px, py)
            if core is None:
                return {"ok": True, "changed": False, "filled_pixels": 0,
                        "message": "No fillable region at the given point: "
                                   "the point sits on (or right next to) a "
                                   "stroke. Click inside the area you want "
                                   "to fill."}
            # 加厚吃掉的 1px 边缘还回来; 膨胀只有 1px, 越不过描边
            filled = _dilate8(core) & fillable
            region = self._canvas[filled]
            if (region == np.array(rgb, dtype=np.uint8)).all():
                return {"ok": True, "changed": False, "filled_pixels": 0,
                        "message": "No visible change on canvas."}
            ys, xs = np.nonzero(filled)
            x0, x1 = int(xs.min()), int(xs.max()) + 1
            y0, y1 = int(ys.min()), int(ys.max()) + 1
            before = self._canvas[y0:y1, x0:x1].copy()
            self._canvas[filled] = np.array(rgb, dtype=np.uint8)
            n_filled = int(filled.sum())
            action_id = self._next_id("action")
            rec = ActionRecord(
                action_id=action_id, action_type="bucket_fill",
                bbox=(x0, y0, x1, y1), before=compress_patch(before),
                after=compress_patch(self._canvas[y0:y1, x0:x1]),
                params_summary=f"bucket color={color} tol={tol}")
            self.history.commit(rec)
            self._notify()
            return {"ok": True, "changed": True, "action_id": action_id,
                    "filled_pixels": n_filled}

    @staticmethod
    def _scanline_flood(mask: np.ndarray, sx: int, sy: int) -> Optional[np.ndarray]:
        """扫描线洪水填充: mask 内与 (sx,sy) 4 连通的区域。

        返回 bool 数组(True=填充), 起点不在 mask 内返回 None。
        """
        h, w = mask.shape
        if not mask[sy, sx]:
            return None
        visited = np.zeros((h, w), dtype=bool)
        stack = [(sx, sy)]
        while stack:
            x, y = stack.pop()
            if visited[y, x]:
                continue
            row = mask[y] & ~visited[y]
            if not row[x]:
                continue
            # 行内向两侧扩展(向量化找边界)
            left_false = np.flatnonzero(~row[: x + 1])
            left = int(left_false[-1]) + 1 if left_false.size else 0
            right_false = np.flatnonzero(~row[x:])
            right = x + int(right_false[0]) - 1 if right_false.size else w - 1
            visited[y, left: right + 1] = True
            for ny in (y - 1, y + 1):
                if 0 <= ny < h:
                    row2 = mask[ny] & ~visited[ny]
                    seg = row2[left: right + 1]
                    fi = np.flatnonzero(seg)
                    if fi.size:
                        starts = np.concatenate(
                            ([0], np.flatnonzero(np.diff(fi) > 1) + 1))
                        for s in starts:
                            stack.append((left + int(fi[s]), ny))
        return visited

    # ------------------------------------------------------------------
    # 笔画删除(eraser_stroke)
    # ------------------------------------------------------------------
    def erase_stroke_at(self, px: int, py: int, hit_radius_px: int):
        """删除点命中的最上层 alive 笔画, 按底图+顺序重放恢复下方内容。"""
        w, h = self.width, self.height
        if not (0 <= px < w and 0 <= py < h):
            return {"ok": False, "error": "Point is outside the canvas."}
        with self._lock:
            target = None
            for meta in reversed(list(self.strokes.values())):
                if not meta.alive:
                    continue
                d = _point_to_polyline_dist(px, py, meta.samples)
                stroke_r = self.mapper.size(meta.size) / 2.0
                if d <= hit_radius_px + stroke_r:
                    target = meta
                    break
            if target is None:
                return {"ok": True, "changed": False, "removed_stroke_id": None,
                        "message": "No stroke hit at the given point."}
            bbox = target.bbox
            x0, y0, x1, y1 = bbox
            before = self._canvas[y0:y1, x0:x1].copy()
            region = self._replay_without(target, bbox)
            self._canvas[y0:y1, x0:x1] = region
            target.alive = False
            action_id = self._next_id("action")
            rec = ActionRecord(
                action_id=action_id, action_type="erase_stroke",
                stroke_id=target.stroke_id, bbox=bbox,
                before=compress_patch(before),
                after=compress_patch(region),
                params_summary=f"erase_stroke {target.stroke_id}")
            self.history.commit(rec)
            self._notify()
            return {"ok": True, "changed": True, "action_id": action_id,
                    "removed_stroke_id": target.stroke_id}

    def _replay_without(self, target: StrokeMeta, bbox):
        """在 bbox 区域重放 target 之后的有效动作, 得到 target 不存在的状态。"""
        x0, y0, x1, y1 = bbox
        rw, rh = x1 - x0, y1 - y0
        # 底图: target 的 draw 动作 before 补丁
        region = None
        for rec in self.history.all_actions:
            if rec.stroke_id == target.stroke_id and rec.action_type == "draw":
                if rec.before is not None:
                    region = decompress_patch(rec.before, (rh, rw, 3))
                break
        if region is None:
            # before 补丁已被释放(超出撤销上限): 退化为白底近似
            region = np.zeros((rh, rw, 3), dtype=np.uint8)
            region[:, :] = self.background
        # 顺序重放后续动作
        for rec in self.history.all_actions:
            if rec.created_index <= target.created_index:
                continue
            if rec._undone:
                continue  # 已撤销动作无效
            ax0, ay0, ax1, ay1 = rec.bbox
            if ax0 >= x1 or ax1 <= x0 or ay0 >= y1 or ay1 <= y0:
                continue  # 不相交
            if rec.action_type == "draw":
                meta = self.strokes.get(rec.stroke_id)
                if meta is None or not meta.alive:
                    continue
                size_px = self.mapper.size(meta.size)
                rb, alpha = renderer.render_stroke_alpha(
                    meta.samples, meta.tool, size_px, self.width, self.height)
                if rb is None:
                    continue
                # 平移到 region 坐标并求交
                rx0, ry0 = max(rb[0], x0), max(rb[1], y0)
                rx1, ry1 = min(rb[2], x1), min(rb[3], y1)
                if rx1 <= rx0 or ry1 <= ry0:
                    continue
                sub = alpha[ry0 - rb[1]: ry1 - rb[1], rx0 - rb[0]: rx1 - rb[0]]
                if meta.tool == "brush":
                    sub = np.clip(sub * meta.opacity, 0.0, 1.0).astype(np.float32)
                # 合成到 region
                local_bbox = (rx0 - x0, ry0 - y0, rx1 - x0, ry1 - y0)
                renderer.composite_color(region, local_bbox, meta.color, sub)
            else:
                # 像素操作: 直接贴 after 补丁的相交部分
                if rec.after is None:
                    continue
                aw = ax1 - ax0
                ah = ay1 - ay0
                patch = decompress_patch(rec.after, (ah, aw, 3))
                ix0, iy0 = max(ax0, x0) - ax0, max(ay0, y0) - ay0
                ix1, iy1 = min(ax1, x1) - ax0, min(ay1, y1) - ay0
                dx, dy = max(ax0, x0) - x0, max(ay0, y0) - y0
                region[dy: dy + (iy1 - iy0), dx: dx + (ix1 - ix0)] = \
                    patch[iy0:iy1, ix0:ix1]
        return region

    # ------------------------------------------------------------------
    # 撤销 / 重做
    # ------------------------------------------------------------------
    def undo(self) -> dict:
        with self._lock:
            rec = self.history.pop_undo()
            if rec is None:
                return {"ok": True, "undone_action_id": None,
                        "message": "Nothing to undo.",
                        "can_undo": self.history.can_undo,
                        "can_redo": self.history.can_redo}
            self._apply_patch(rec, use_before=True)
            self._update_alive(rec, undo=True)
            self.history.mark_undone(rec)
            self._notify()
            return {"ok": True, "undone_action_id": rec.action_id,
                    "can_undo": self.history.can_undo,
                    "can_redo": self.history.can_redo}

    def redo(self) -> dict:
        with self._lock:
            rec = self.history.pop_redo()
            if rec is None:
                return {"ok": True, "redone_action_id": None,
                        "message": "Nothing to redo.",
                        "can_undo": self.history.can_undo,
                        "can_redo": self.history.can_redo}
            self._apply_patch(rec, use_before=False)
            self._update_alive(rec, undo=False)
            self.history.mark_effective(rec)
            self.history.undo_stack.append(rec)
            self._notify()
            return {"ok": True, "redone_action_id": rec.action_id,
                    "can_undo": self.history.can_undo,
                    "can_redo": self.history.can_redo}

    def _apply_patch(self, rec: ActionRecord, use_before: bool) -> None:
        data = rec.before if use_before else rec.after
        if data is None or rec.bbox is None:
            return
        x0, y0, x1, y1 = rec.bbox
        patch = decompress_patch(data, (y1 - y0, x1 - x0, 3))
        self._canvas[y0:y1, x0:x1] = patch

    def _update_alive(self, rec: ActionRecord, undo: bool) -> None:
        if rec.stroke_id and rec.stroke_id in self.strokes:
            meta = self.strokes[rec.stroke_id]
            if rec.action_type == "draw":
                meta.alive = (not undo)
            elif rec.action_type == "erase_stroke":
                meta.alive = undo

    # ------------------------------------------------------------------
    # 查询 / 导出
    # ------------------------------------------------------------------
    def get_info(self, include_geometry: bool = False) -> dict:
        with self._lock:
            alive = [m for m in self.strokes.values() if m.alive]
            strokes_summary = [m.summary() for m in alive[-50:]]
            if include_geometry:
                for s, m in zip(strokes_summary, alive[-50:]):
                    pts = m.samples[:200].tolist()
                    s["points_px"] = [[round(p[0], 1), round(p[1], 1)] for p in pts]
            return {
                "ok": True,
                "canvas": {"width": self.width, "height": self.height},
                "history": {
                    "can_undo": self.history.can_undo,
                    "can_redo": self.history.can_redo,
                    "action_count": self.history.total_count,
                },
                "stroke_count": len(alive),
                "strokes": strokes_summary,
            }

    def snapshot_rgb(self) -> np.ndarray:
        with self._lock:
            return self._canvas.copy()

    def to_pil_image(self, arr: Optional[np.ndarray] = None) -> Image.Image:
        data = self.snapshot_rgb() if arr is None else arr
        return Image.fromarray(data, mode="RGB")

    def export_png(self, path: str) -> None:
        """导出原始分辨率 PNG(不改变画布状态, 不自动保存)。"""
        arr = self.snapshot_rgb()
        Image.fromarray(arr, mode="RGB").save(path, format="PNG")

    def observation_image(self, max_side: int = 1024, full_resolution: bool = False):
        """观察用图片: 默认最长边不超过 max_side 的 PNG PIL Image。"""
        arr = self.snapshot_rgb()
        if full_resolution:
            return Image.fromarray(arr, mode="RGB")
        img = Image.fromarray(arr, mode="RGB")
        w, h = img.size
        scale = min(1.0, float(max_side) / max(w, h))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                             Image.LANCZOS)
        return img

    # ------------------------------------------------------------------
    # 外部 Skill 模式入口: 直接以归一化参数执行路径动作
    # ------------------------------------------------------------------
    def execute_path_action(self, path_obj: dict, tool: str, closed: bool,
                            color: str, size_norm: int, opacity: float):
        """解析归一化路径并执行 pen/brush/eraser_area 动作。"""
        segments, path_type, geo_summary = parse_path_spec(
            path_obj, closed, self.mapper)
        if tool in ("pen", "brush"):
            return self.draw_stroke(
                segments, tool, color, size_norm, opacity,
                raw_path=path_obj, path_type=path_type,
                geometry_summary=geo_summary)
        elif tool == "eraser_area":
            return self.erase_area(segments)
        raise ValueError(f"execute_path_action: unsupported tool '{tool}'")


def _point_to_polyline_dist(px: float, py: float, pts: np.ndarray) -> float:
    """点到折线的最短距离(单点折线退化为点到点距离)。"""
    p = np.array([px, py], dtype=np.float64)
    pts = np.asarray(pts, dtype=np.float64)
    if len(pts) == 1:
        return float(np.hypot(*(p - pts[0])))
    a = pts[:-1]
    b = pts[1:]
    ab = b - a
    ab2 = np.einsum("ij,ij->i", ab, ab)
    ab2[ab2 == 0] = 1e-12
    ap = p - a
    t = np.clip(np.einsum("ij,ij->i", ap, ab) / ab2, 0.0, 1.0)
    proj = a + t[:, None] * ab
    d = np.sqrt(np.einsum("ij,ij->i", proj - p, proj - p))
    return float(d.min())
