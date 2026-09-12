# -*- coding: utf-8 -*-
"""渲染器: 把像素域路径段渲染成 alpha 平面并合成到画布数组。

- pen: 硬画笔。stamp 圆核带 1px 抗锯齿边缘, 重叠处取 max(不产生透明叠加),
  最终 alpha 语义上固定为不透明。
- brush: 毛笔。高斯衰减核 w(d) = (exp(-d^2/(2*sigma^2)) - exp(-2)) / (1 - exp(-2)),
  sigma = R / 2, alpha = opacity * w(d), 重叠处取 max。
- eraser_area: 闭合区域填充白色, 2x 超采样抗锯齿。

所有函数只依赖 numpy/PIL, 不依赖 GUI。
"""
import threading

import numpy as np
from PIL import Image, ImageDraw

from .geometry import cubic_bezier_length_approx, sample_cubic_bezier

_KERN_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _pen_kernel(radius: float) -> np.ndarray:
    """硬画笔 stamp 核: 圆形, 1px 抗锯齿边缘, 值域 [0,1]。"""
    key = ("pen", round(radius, 2))
    with _CACHE_LOCK:
        if key in _KERN_CACHE:
            return _KERN_CACHE[key]
    rf = int(np.ceil(radius))
    size = 2 * rf + 1
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    d = np.sqrt((xs - rf) ** 2 + (ys - rf) ** 2)
    # 半径 R 处为 0, 内部为 1, 边缘 1px 线性过渡
    k = np.clip(radius + 0.5 - d, 0.0, 1.0).astype(np.float32)
    with _CACHE_LOCK:
        _KERN_CACHE[key] = k
    return k


def _brush_kernel(radius: float) -> np.ndarray:
    """毛笔 stamp 核: 中心到边缘高斯衰减(值为权重, 不含 opacity)。"""
    key = ("brush", round(radius, 2))
    with _CACHE_LOCK:
        if key in _KERN_CACHE:
            return _KERN_CACHE[key]
    rf = max(1, int(np.ceil(radius)))
    size = 2 * rf + 1
    ys, xs = np.mgrid[0:size, 0:size].astype(np.float32)
    d = np.sqrt((xs - rf) ** 2 + (ys - rf) ** 2)
    sigma = radius / 2.0 if radius > 0 else 1.0
    denom = 1.0 - np.exp(-2.0)
    with np.errstate(over="ignore", under="ignore"):
        w = (np.exp(-(d * d) / (2.0 * sigma * sigma)) - np.exp(-2.0)) / denom
    k = np.clip(w, 0.0, 1.0).astype(np.float32)
    k[d > radius] = 0.0
    with _CACHE_LOCK:
        _KERN_CACHE[key] = k
    return k


def sample_path_segments(segments, step: float):
    """把段列表采样为子路径点列。

    返回 list[np.ndarray (N,2)]; 单点子路径保留(长度 1)。
    """
    subpaths = []
    cur = []
    cur_start = None
    for seg in segments:
        tag = seg[0]
        if tag == "M":
            if cur:
                subpaths.append(np.asarray(cur, dtype=np.float64))
            cur = [(seg[1], seg[2])]
            cur_start = (seg[1], seg[2])
        elif tag == "L":
            if not cur:
                cur = [(seg[1], seg[2])]
                cur_start = (seg[1], seg[2])
            else:
                cur.append((seg[1], seg[2]))
        elif tag == "C":
            if not cur:
                raise ValueError("C segment without current point.")
            p0 = cur[-1]
            c1 = (seg[1], seg[2])
            c2 = (seg[3], seg[4])
            p1 = (seg[5], seg[6])
            approx = cubic_bezier_length_approx(p0, c1, c2, p1)
            n = max(1, int(np.ceil(approx / max(step, 1.0))))
            pts = sample_cubic_bezier(p0, c1, c2, p1, n)
            cur.extend(pts[1:])
        elif tag == "Z":
            if cur and cur_start is not None:
                cur.append(cur_start)
                subpaths.append(np.asarray(cur, dtype=np.float64))
                cur = [cur_start]
    if cur:
        subpaths.append(np.asarray(cur, dtype=np.float64))
    return subpaths


def render_stroke_alpha(samples, tool: str, size_px: int, canvas_w: int, canvas_h: int):
    """渲染笔画 alpha 平面。

    samples: 像素坐标点列(np.ndarray (N,2) 或 list)。
    返回 (bbox, alpha): bbox=(x0,y0,x1,y1) 画布内整数区间(x1/y1 为开区间上界),
    alpha 为 float32 (h, w), 值域 [0,1]。
    若笔画完全在画布外, 返回 (None, None)。
    """
    pts = np.asarray(samples, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] == 0 or pts.shape[1] != 2:
        return None, None
    radius = max(0.5, size_px / 2.0)
    if tool == "pen":
        kernel = _pen_kernel(radius)
        step = float(np.clip(radius * 0.3, 1.0, 4.0))
    elif tool == "brush":
        kernel = _brush_kernel(radius)
        step = float(np.clip(radius * 0.35, 1.0, 6.0))
    else:
        raise ValueError(f"render_stroke_alpha: unsupported tool '{tool}'")

    rf = (kernel.shape[0] - 1) // 2
    # bbox: 采样点范围膨胀核半径
    pad = rf + 2
    x0 = int(np.floor(pts[:, 0].min())) - pad
    y0 = int(np.floor(pts[:, 1].min())) - pad
    x1 = int(np.ceil(pts[:, 0].max())) + pad
    y1 = int(np.ceil(pts[:, 1].max())) + pad
    # 裁剪到画布
    cx0, cy0 = max(x0, 0), max(y0, 0)
    cx1, cy1 = min(x1, canvas_w), min(y1, canvas_h)
    if cx1 <= cx0 or cy1 <= cy0:
        return None, None

    w, h = cx1 - cx0, cy1 - cy0
    alpha = np.zeros((h, w), dtype=np.float32)

    n = len(pts)
    for i in range(n):
        px, py = pts[i]
        ix, iy = int(round(px)), int(round(py))
        # stamp 在 alpha 平面内的切片
        sx0 = ix - rf - cx0
        sy0 = iy - rf - cy0
        sx1 = sx0 + 2 * rf + 1
        sy1 = sy0 + 2 * rf + 1
        # 与 [0, w) x [0, h) 求交
        ax0, ay0 = max(sx0, 0), max(sy0, 0)
        ax1, ay1 = min(sx1, w), min(sy1, h)
        if ax0 >= ax1 or ay0 >= ay1:
            continue
        kx0, ky0 = ax0 - sx0, ay0 - sy0
        kx1, ky1 = kx0 + (ax1 - ax0), ky0 + (ay1 - ay0)
        np.maximum(alpha[ay0:ay1, ax0:ax1], kernel[ky0:ky1, kx0:kx1],
                   out=alpha[ay0:ay1, ax0:ax1])

    # 采样不足时插值补点(两点间隔 > step * 2 时加密)
    # 注意: _densify 只返回"新插入的点", 因此不能用 len(dense) > n 判断是否补点
    # (闭合路径的收尾段很长、但全路径采样点本来就多时会被整体跳过, 导致轮廓
    #  缺口 —— 看起来像一条抗锯齿细缝, bucket 填充会从这里漏出去)。
    if n > 1:
        dense = _densify(pts, step)
        if dense:
            for px, py in dense:
                ix, iy = int(round(px)), int(round(py))
                sx0 = ix - rf - cx0
                sy0 = iy - rf - cy0
                ax0, ay0 = max(sx0, 0), max(sy0, 0)
                ax1 = min(sx0 + 2 * rf + 1, w)
                ay1 = min(sy0 + 2 * rf + 1, h)
                if ax0 >= ax1 or ay0 >= ay1:
                    continue
                kx0, ky0 = ax0 - sx0, ay0 - sy0
                np.maximum(alpha[ay0:ay1, ax0:ax1],
                           kernel[ky0:ky0 + (ay1 - ay0), kx0:kx0 + (ax1 - ax0)],
                           out=alpha[ay0:ay1, ax0:ax1])

    bbox = (cx0, cy0, cx1, cy1)
    return bbox, alpha


def _densify(pts: np.ndarray, step: float):
    """沿折线按 step 插值加密点(只补间隔大的段)。"""
    out = []
    for i in range(len(pts) - 1):
        p0 = pts[i]
        p1 = pts[i + 1]
        d = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        if d > step * 2:
            n = int(d / step)
            for j in range(1, n + 1):
                t = j / (n + 1)
                out.append((p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t))
    return out


def composite_color(canvas: np.ndarray, bbox, color, alpha: np.ndarray) -> None:
    """把颜色按 alpha 平面合成为画布区域(原地修改 canvas)。

    canvas: (H, W, 3) uint8; bbox=(x0,y0,x1,y1); alpha 与区域同形。
    """
    x0, y0, x1, y1 = bbox
    region = canvas[y0:y1, x0:x1]
    a = alpha[:, :, None]
    src = np.empty_like(region, dtype=np.float32)
    src[..., 0] = color[0]
    src[..., 1] = color[1]
    src[..., 2] = color[2]
    out = region.astype(np.float32) * (1.0 - a) + src * a
    np.clip(out + 0.5, 0.0, 255.0, out=out)
    canvas[y0:y1, x0:x1] = out.astype(np.uint8)


def render_erase_area_mask(segments, canvas_w: int, canvas_h: int, step: float = 4.0):
    """把闭合路径渲染为区域 mask(2x 超采样抗锯齿)。

    返回 (bbox, alpha) 或 (None, None)。
    每个子路径独立作为闭合多边形填充(区域取并集)。
    """
    subpaths = sample_path_segments(segments, step)
    polys = [p for p in subpaths if len(p) >= 3]
    if not polys:
        # 单点/两点退化: 用小圆点
        pts = np.vstack([p for p in subpaths if len(p) >= 1]) if any(
            len(p) >= 1 for p in subpaths) else None
        if pts is None:
            return None, None
        polys = [np.vstack([pts, pts + [2, 0], pts + [2, 2], pts + [0, 2]])]

    all_pts = np.vstack(polys)
    x0 = int(np.floor(all_pts[:, 0].min()))
    y0 = int(np.floor(all_pts[:, 1].min()))
    x1 = int(np.ceil(all_pts[:, 0].max()))
    y1 = int(np.ceil(all_pts[:, 1].max()))
    cx0, cy0 = max(x0, 0), max(y0, 0)
    cx1, cy1 = min(x1, canvas_w), min(y1, canvas_h)
    if cx1 <= cx0 or cy1 <= cy0:
        return None, None
    w, h = cx1 - cx0, cy1 - cy0

    scale = 2
    mask_img = Image.new("L", (w * scale, h * scale), 0)
    draw = ImageDraw.Draw(mask_img)
    for poly in polys:
        shifted = [( (p[0] - cx0) * scale, (p[1] - cy0) * scale) for p in poly]
        draw.polygon(shifted, fill=255)
    mask_img = mask_img.resize((w, h), Image.BILINEAR)
    alpha = np.asarray(mask_img, dtype=np.float32) / 255.0
    bbox = (cx0, cy0, cx1, cy1)
    return bbox, alpha
