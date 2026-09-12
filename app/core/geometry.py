# -*- coding: utf-8 -*-
"""归一化坐标(0~1000)与像素坐标之间的换算规则。

规则(MISSION.md 第 6 节):
- 点坐标: x 按画布宽度, y 按画布高度换算。
- 横向几何尺寸(rect.width, ellipse.rx, SVG 横向半径): 按画布宽度。
- 纵向几何尺寸(rect.height, ellipse.ry, SVG 纵向半径): 按画布高度。
- 视觉半径(circle.r, arc.r): 按画布短边, 避免圆形被拉伸。
- 线宽/笔刷尺寸 size、笔画橡皮命中半径 hit_radius: 按画布短边。

通用公式: pixel = round(value / 1000 * reference_side), 尺寸下限 1。
"""
import math

NORM_MAX = 1000


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_norm_point(x, y):
    """把归一化点坐标 clamp 到 0~1000。"""
    return int(clamp(round(float(x)), 0, NORM_MAX)), int(clamp(round(float(y)), 0, NORM_MAX))


def norm_x_to_px(u: float, width: int) -> float:
    return float(u) / NORM_MAX * width


def norm_y_to_px(v: float, height: int) -> float:
    return float(v) / NORM_MAX * height


def norm_size_to_px(value: float, reference_side: int) -> int:
    """归一化尺寸转像素尺寸, 最小 1。"""
    v = int(round(float(value) / NORM_MAX * reference_side))
    return max(1, v)


def ref_width(canvas_w: int, canvas_h: int) -> int:
    return canvas_w


def ref_height(canvas_w: int, canvas_h: int) -> int:
    return canvas_h


def ref_short(canvas_w: int, canvas_h: int) -> int:
    return min(canvas_w, canvas_h)


class PixelMapper:
    """封装某一画布尺寸下的换算规则, 供路径解析使用。"""

    def __init__(self, width: int, height: int):
        self.width = int(width)
        self.height = int(height)
        self.short = min(self.width, self.height)

    # 点坐标
    def point(self, u, v):
        return (float(u) / NORM_MAX * self.width,
                float(v) / NORM_MAX * self.height)

    def size(self, value):
        """线宽/笔刷尺寸/命中半径: 按短边。"""
        return norm_size_to_px(value, self.short)

    def width_size(self, value):
        """横向尺寸: 按宽。"""
        return norm_size_to_px(value, self.width)

    def height_size(self, value):
        """纵向尺寸: 按高。"""
        return norm_size_to_px(value, self.height)

    def visual_radius(self, value):
        """视觉半径: 按短边。"""
        return norm_size_to_px(value, self.short)


def catmull_rom_to_beziers(points):
    """把点列转成 Catmull-Rom 三次贝塞尔段列表。

    points: [(x, y), ...] 像素坐标, 长度 >= 2。
    返回: [((p0),(c1),(c2),(p1)), ...] 每段三次贝塞尔。
    首尾用重复点补齐。
    """
    n = len(points)
    if n == 1:
        return []
    if n == 2:
        p0, p1 = points[0], points[1]
        return [((p0[0], p0[1]),
                 (p0[0] + (p1[0] - p0[0]) / 3.0, p0[1] + (p1[1] - p0[1]) / 3.0),
                 (p1[0] - (p1[0] - p0[0]) / 3.0, p1[1] - (p1[1] - p0[1]) / 3.0),
                 (p1[0], p1[1]))]
    pts = [points[0]] + list(points) + [points[-1]]
    segments = []
    for i in range(len(points) - 1):
        p0 = pts[i]
        p1 = pts[i + 1]
        p2 = pts[i + 2]
        p3 = pts[i + 3]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        segments.append(((p1[0], p1[1]), c1, c2, (p2[0], p2[1])))
    return segments


def cubic_bezier_length_approx(p0, p1, p2, p3) -> float:
    """控制多边形长度作为贝塞尔弧长上界近似。"""
    def dist(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])
    return dist(p0, p1) + dist(p1, p2) + dist(p2, p3)


def sample_cubic_bezier(p0, c1, c2, p1, n: int):
    """均匀参数采样三次贝塞尔, 返回点列表(n+1 个点, 含端点)。"""
    n = max(1, int(n))
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1.0 - t
        x = mt * mt * mt * p0[0] + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t * t * t * p1[0]
        y = mt * mt * mt * p0[1] + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t * t * t * p1[1]
        pts.append((x, y))
    return pts
