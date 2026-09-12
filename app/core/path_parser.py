# -*- coding: utf-8 -*-
"""路径解析: 把三种路径表示统一转换为像素域段列表。

输入(归一化坐标 0~1000):
1. 点列 points: [[x, y], ...] >= 3 点用 Catmull-Rom 平滑。
2. SVG path d: 支持 M L C Q A Z(及 H V S T), 大小写(绝对/相对)。
3. 基本图形 shape + params: circle / ellipse / rect / line / arc。

输出段列表(像素坐标):
  ("M", x, y)                      移动
  ("L", x, y)                      直线
  ("C", x1, y1, x2, y2, x, y)      三次贝塞尔
  ("Z",)                           闭合(连回子路径起点)

尺寸换算规则见 geometry.PixelMapper。
"""
import math
import re

from .geometry import PixelMapper, catmull_rom_to_beziers

KAPPA = 0.5522847498307936  # 圆的贝塞尔近似系数


class PathError(ValueError):
    """路径解析/校验错误, message 面向模型可读。"""


# ---------------------------------------------------------------------------
# SVG path d 解析
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"([MmLlHhVvCcSsQqTtAaZz])|([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)"
)


def _tokenize(d: str):
    tokens = []
    pos = 0
    d = d.strip()
    while pos < len(d):
        ch = d[pos]
        if ch in " \t\r\n,":

            pos += 1
            continue
        m = _TOKEN_RE.match(d, pos)
        if not m:
            raise PathError(f"SVG path 解析失败: 无法识别的字符 '{ch}' (位置 {pos})。")
        if m.group(1):
            tokens.append(("cmd", m.group(1)))
        else:
            tokens.append(("num", float(m.group(2))))
        pos = m.end()
    return tokens


class _SvgParser:
    def __init__(self, d: str, mapper: PixelMapper):
        self.tokens = _tokenize(d)
        self.i = 0
        self.mapper = mapper
        self.segments = []
        self.cx = 0.0  # 当前点(归一化域)
        self.cy = 0.0
        self.sx = 0.0  # 子路径起点(归一化域)
        self.sy = 0.0
        self.has_current = False
        self.last_ctrl_c = None  # S 命令用
        self.last_ctrl_q = None  # T 命令用

    # --- 基础读取 ---
    def peek_cmd(self):
        if self.i < len(self.tokens) and self.tokens[self.i][0] == "cmd":
            return self.tokens[self.i][1]
        return None

    def next_num(self):
        if self.i >= len(self.tokens) or self.tokens[self.i][0] != "num":
            raise PathError("SVG path 解析失败: 命令参数不足, 缺少数字。")
        v = self.tokens[self.i][1]
        self.i += 1
        return v

    def next_flag(self):
        v = self.next_num()
        return 1 if v else 0

    # --- 坐标换算: 点在归一化域, 直径/半径按规则 ---
    def px_point(self, u, v):
        return self.mapper.point(u, v)

    def _emit_pixel_cubic(self, p0, c1, c2, p1):
        self.segments.append(("C", c1[0], c1[1], c2[0], c2[1], p1[0], p1[1]))

    # --- 命令实现(在归一化域做几何, 输出像素) ---
    def moveto(self, rel, x, y):
        if rel and self.has_current:
            x += self.cx
            y += self.cy
        self.cx, self.cy = x, y
        self.sx, self.sy = x, y
        self.has_current = True
        self.last_ctrl_c = None
        self.last_ctrl_q = None
        p = self.px_point(x, y)
        self.segments.append(("M", p[0], p[1]))

    def lineto(self, rel, x, y):
        if not self.has_current:
            raise PathError("SVG path 解析失败: L 命令前缺少 M 命令。")
        if rel:
            x += self.cx
            y += self.cy
        self.cx, self.cy = x, y
        p = self.px_point(x, y)
        self.segments.append(("L", p[0], p[1]))
        self.last_ctrl_c = None
        self.last_ctrl_q = None

    def curveto(self, rel, x1, y1, x2, y2, x, y):
        if not self.has_current:
            raise PathError("SVG path 解析失败: C 命令前缺少 M 命令。")
        if rel:
            x1 += self.cx; y1 += self.cy
            x2 += self.cx; y2 += self.cy
            x += self.cx; y += self.cy
        p0 = self.px_point(self.cx, self.cy)
        c1 = self.px_point(x1, y1)
        c2 = self.px_point(x2, y2)
        p1 = self.px_point(x, y)
        self._emit_pixel_cubic(p0, c1, c2, p1)
        self.cx, self.cy = x, y
        self.last_ctrl_c = (x2, y2)
        self.last_ctrl_q = None

    def smooth_curveto(self, rel, x2, y2, x, y):
        if not self.has_current:
            raise PathError("SVG path 解析失败: S 命令前缺少 M 命令。")
        if self.last_ctrl_c is None:
            x1, y1 = self.cx, self.cy
        else:
            x1 = 2 * self.cx - self.last_ctrl_c[0]
            y1 = 2 * self.cy - self.last_ctrl_c[1]
        self.curveto(rel, x1, y1, x2, y2, x, y)

    def quadto(self, rel, x1, y1, x, y):
        if not self.has_current:
            raise PathError("SVG path 解析失败: Q 命令前缺少 M 命令。")
        if rel:
            x1 += self.cx; y1 += self.cy
            x += self.cx; y += self.cy
        # 二次升三次
        x0, y0 = self.cx, self.cy
        cx1 = x0 + 2 / 3.0 * (x1 - x0)
        cy1 = y0 + 2 / 3.0 * (y1 - y0)
        cx2 = x + 2 / 3.0 * (x1 - x)
        cy2 = y + 2 / 3.0 * (y1 - y)
        p0 = self.px_point(x0, y0)
        c1 = self.px_point(cx1, cy1)
        c2 = self.px_point(cx2, cy2)
        p1 = self.px_point(x, y)
        self._emit_pixel_cubic(p0, c1, c2, p1)
        self.cx, self.cy = x, y
        self.last_ctrl_q = (x1, y1)
        self.last_ctrl_c = None

    def smooth_quadto(self, rel, x, y):
        if not self.has_current:
            raise PathError("SVG path 解析失败: T 命令前缺少 M 命令。")
        if self.last_ctrl_q is None:
            x1, y1 = self.cx, self.cy
        else:
            x1 = 2 * self.cx - self.last_ctrl_q[0]
            y1 = 2 * self.cy - self.last_ctrl_q[1]
        self.quadto(rel, x1, y1, x, y)

    def arc(self, rel, rx, ry, phi_deg, fa, fs, x, y):
        if not self.has_current:
            raise PathError("SVG path 解析失败: A 命令前缺少 M 命令。")
        if rel:
            x += self.cx
            y += self.cy
        if rx <= 0 or ry <= 0:
            raise PathError("SVG path 解析失败: A 命令的 rx/ry 必须为正数。")
        # 半径换算: 横向按宽, 纵向按高(MISSION 6.2)
        rx_px = self.mapper.width_size(rx)
        ry_px = self.mapper.height_size(ry)
        p0 = self.px_point(self.cx, self.cy)
        p1 = self.px_point(x, y)
        cubics = _svg_arc_to_cubics(p0, rx_px, ry_px, phi_deg, fa, fs, p1)
        for (c1, c2, end) in cubics:
            self._emit_pixel_cubic(p0, c1, c2, end)
            p0 = end
        self.cx, self.cy = x, y
        self.last_ctrl_c = None
        self.last_ctrl_q = None

    def closepath(self):
        if not self.has_current:
            raise PathError("SVG path 解析失败: Z 命令前缺少 M 命令。")
        self.segments.append(("Z",))
        self.cx, self.cy = self.sx, self.sy
        self.last_ctrl_c = None
        self.last_ctrl_q = None

    def parse(self):
        if not self.tokens:
            raise PathError("SVG path 解析失败: d 为空。")
        need_new_subpath = True
        first_cmd = None
        while self.i < len(self.tokens):
            tok = self.tokens[self.i]
            if tok[0] != "cmd":
                # 隐式重复上一命令(M 后视为 L)
                if first_cmd in ("M", "m"):
                    cmd = "L" if first_cmd.isupper() else "l"
                else:
                    raise PathError("SVG path 解析失败: 路径开头必须是命令字母。")
            else:
                cmd = tok[1]
                self.i += 1
                if first_cmd is None:
                    first_cmd = cmd
            rel = cmd.islower()
            up = cmd.upper()
            if up == "M":
                if need_new_subpath:
                    x, y = self.next_num(), self.next_num()
                    self.moveto(rel, x, y)
                # 后续隐式坐标对按 L 处理
                while self._peek_is_num():
                    x, y = self.next_num(), self.next_num()
                    self.lineto(rel, x, y)
                need_new_subpath = False
            elif up == "L":
                while self._peek_is_num():
                    x, y = self.next_num(), self.next_num()
                    self.lineto(rel, x, y)
            elif up == "H":
                while self._peek_is_num():
                    v = self.next_num()
                    x = (self.cx + v) if rel else v
                    self.lineto(False, x, self.cy)
            elif up == "V":
                while self._peek_is_num():
                    v = self.next_num()
                    y = (self.cy + v) if rel else v
                    self.lineto(False, self.cx, y)
            elif up == "C":
                while self._peek_is_num():
                    x1, y1 = self.next_num(), self.next_num()
                    x2, y2 = self.next_num(), self.next_num()
                    x, y = self.next_num(), self.next_num()
                    self.curveto(rel, x1, y1, x2, y2, x, y)
            elif up == "S":
                while self._peek_is_num():
                    x2, y2 = self.next_num(), self.next_num()
                    x, y = self.next_num(), self.next_num()
                    self.smooth_curveto(rel, x2, y2, x, y)
            elif up == "Q":
                while self._peek_is_num():
                    x1, y1 = self.next_num(), self.next_num()
                    x, y = self.next_num(), self.next_num()
                    self.quadto(rel, x1, y1, x, y)
            elif up == "T":
                while self._peek_is_num():
                    x, y = self.next_num(), self.next_num()
                    self.smooth_quadto(rel, x, y)
            elif up == "A":
                while self._peek_is_num():
                    rx, ry = self.next_num(), self.next_num()
                    phi = self.next_num()
                    fa = self.next_flag()
                    fs = self.next_flag()
                    x, y = self.next_num(), self.next_num()
                    self.arc(rel, rx, ry, phi, fa, fs, x, y)
            elif up == "Z":
                self.closepath()
                need_new_subpath = True
            else:
                raise PathError(f"SVG path 解析失败: 不支持的命令 '{cmd}'。")
        return self.segments

    def _peek_is_num(self):
        return self.i < len(self.tokens) and self.tokens[self.i][0] == "num"


def _svg_arc_to_cubics(p0, rx, ry, phi_deg, fa, fs, p1):
    """SVG 椭圆弧端点参数化 -> 圆心参数化 -> 分段三次贝塞尔(像素域)。

    p0/p1 像素坐标; rx/ry 像素半径; phi_deg x 轴旋转角; fa/fs 大弧/顺时针标志。
    返回 [(c1, c2, end), ...]。
    """
    x1, y1 = p0
    x2, y2 = p1
    if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
        return []
    phi = math.radians(phi_deg % 360.0)
    cos_phi = math.cos(phi)
    sin_phi = math.sin(phi)

    # 端点变换到椭圆局部坐标系
    dx = (x1 - x2) / 2.0
    dy = (y1 - y2) / 2.0
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy

    # 半径不足时放大
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx *= s
        ry *= s

    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    if den == 0:
        return []
    co = math.sqrt(max(0.0, num / den))
    if fa == fs:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx

    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        if n == 0:
            return 0.0
        a = math.acos(max(-1.0, min(1.0, dot / n)))
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    theta1 = angle(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                   (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dtheta > 0:
        dtheta -= 2 * math.pi
    elif fs and dtheta < 0:
        dtheta += 2 * math.pi

    # 分段: 每段 <= 90 度
    n = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    delta = dtheta / n
    k = 4.0 / 3.0 * math.tan(delta / 4.0)
    cubics = []
    th = theta1
    px, py = x1, y1
    for _ in range(n):
        # 当前点方向单位向量
        cos1 = math.cos(th)
        sin1 = math.sin(th)
        cos2 = math.cos(th + delta)
        sin2 = math.sin(th + delta)
        # 当前点(应由 px,py 对齐)
        e1x = cx + rx * cos1 * cos_phi - ry * sin1 * sin_phi
        e1y = cy + rx * cos1 * sin_phi + ry * sin1 * cos_phi
        e2x = cx + rx * cos2 * cos_phi - ry * sin2 * sin_phi
        e2y = cy + rx * cos2 * sin_phi + ry * sin2 * cos_phi
        # 切线方向
        t1x = -rx * sin1 * cos_phi - ry * cos1 * sin_phi
        t1y = -rx * sin1 * sin_phi + ry * cos1 * cos_phi
        t2x = -rx * sin2 * cos_phi - ry * cos2 * sin_phi
        t2y = -rx * sin2 * sin_phi + ry * cos2 * cos_phi
        n1 = math.hypot(t1x, t1y)
        n2 = math.hypot(t2x, t2y)
        if n1 == 0 or n2 == 0:
            th += delta
            continue
        c1 = (e1x + k * t1x / n1, e1y + k * t1y / n1)
        c2 = (e2x - k * t2x / n2, e2y - k * t2y / n2)
        cubics.append((c1, c2, (e2x, e2y)))
        th += delta
    return cubics


# ---------------------------------------------------------------------------
# 点列解析
# ---------------------------------------------------------------------------

def _parse_points(points, mapper: PixelMapper):
    if not isinstance(points, (list, tuple)) or len(points) == 0:
        raise PathError("Invalid path: points must be a non-empty array of [x, y] pairs.")
    pts_norm = []
    for i, p in enumerate(points):
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(v, (int, float)) for v in p)):
            raise PathError("Invalid path: points must be an array of [x, y] pairs.")
        u, v = float(p[0]), float(p[1])
        u = max(0.0, min(1000.0, u))
        v = max(0.0, min(1000.0, v))
        pts_norm.append((u, v))
    segments = []
    if len(pts_norm) == 1:
        u, v = mapper.point(*pts_norm[0])
        segments.append(("M", u, v))
        return segments
    px_pts = [mapper.point(u, v) for (u, v) in pts_norm]
    if len(px_pts) == 2:
        segments.append(("M", px_pts[0][0], px_pts[0][1]))
        segments.append(("L", px_pts[1][0], px_pts[1][1]))
        return segments
    beziers = catmull_rom_to_beziers(px_pts)
    first = px_pts[0]
    segments.append(("M", first[0], first[1]))
    for (p0, c1, c2, p1) in beziers:
        segments.append(("C", c1[0], c1[1], c2[0], c2[1], p1[0], p1[1]))
    return segments


# ---------------------------------------------------------------------------
# 基本图形解析
# ---------------------------------------------------------------------------

_SHAPE_KEYS = ("circle", "ellipse", "rect", "line", "arc")


def _need(params: dict, shape: str, key: str):
    if key not in params:
        raise PathError(f"Invalid path: shape '{shape}' requires parameter '{key}'.")
    try:
        v = float(params[key])
    except (TypeError, ValueError):
        raise PathError(f"Invalid path: parameter '{key}' of shape '{shape}' must be a number.")
    return v


def _clamp_param(params, key, lo=0.0, hi=1000.0):
    v = float(params[key])
    return max(lo, min(hi, v))


def _parse_shape(shape: str, params, mapper: PixelMapper):
    if not isinstance(params, dict):
        raise PathError("Invalid path: shape 'params' must be an object.")
    shape = str(shape).lower()
    if shape not in _SHAPE_KEYS:
        raise PathError(
            f"Invalid path: unsupported shape '{shape}'. Expected one of: "
            + ", ".join(_SHAPE_KEYS) + ".")
    segments = []
    if shape == "circle":
        cx, cy = _need(params, shape, "cx"), _need(params, shape, "cy")
        r = _need(params, shape, "r")
        cx, cy, r = _clamp_param(params, "cx"), _clamp_param(params, "cy"), _clamp_param(params, "r")
        c = mapper.point(cx, cy)
        rpx = mapper.visual_radius(r)
        segments.extend(_ellipse_segments(c[0], c[1], rpx, rpx))
    elif shape == "ellipse":
        cx, cy = _clamp_param(params, "cx"), _clamp_param(params, "cy")
        rx, ry = _clamp_param(params, "rx"), _clamp_param(params, "ry")
        c = mapper.point(cx, cy)
        rxpx = mapper.width_size(rx)
        rypx = mapper.height_size(ry)
        segments.extend(_ellipse_segments(c[0], c[1], rxpx, rypx))
    elif shape == "rect":
        x, y = _clamp_param(params, "x"), _clamp_param(params, "y")
        w = float(params.get("width", 0))
        h = float(params.get("height", 0))
        if w < 0 or h < 0:
            raise PathError("Invalid path: rect width/height must be >= 0.")
        w = min(w, 1000.0)
        h = min(h, 1000.0)
        p0 = mapper.point(x, y)
        wpx = mapper.width_size(w) if w > 0 else 1
        hpx = mapper.height_size(h) if h > 0 else 1
        corners = [
            (p0[0], p0[1]),
            (p0[0] + wpx, p0[1]),
            (p0[0] + wpx, p0[1] + hpx),
            (p0[0], p0[1] + hpx),
        ]
        segments.append(("M", corners[0][0], corners[0][1]))
        for c in corners[1:]:
            segments.append(("L", c[0], c[1]))
        segments.append(("Z",))
    elif shape == "line":
        x1, y1 = _clamp_param(params, "x1"), _clamp_param(params, "y1")
        x2, y2 = _clamp_param(params, "x2"), _clamp_param(params, "y2")
        p1 = mapper.point(x1, y1)
        p2 = mapper.point(x2, y2)
        segments.append(("M", p1[0], p1[1]))
        segments.append(("L", p2[0], p2[1]))
    elif shape == "arc":
        cx, cy = _clamp_param(params, "cx"), _clamp_param(params, "cy")
        r = _clamp_param(params, "r")
        a0 = float(params.get("start_angle", 0.0))
        a1 = float(params.get("end_angle", 0.0))
        c = mapper.point(cx, cy)
        rpx = mapper.visual_radius(r)
        segments.extend(_arc_segments(c[0], c[1], rpx, a0, a1))
    return segments


def _ellipse_segments(cx, cy, rx, ry):
    """四段三次贝塞尔近似椭圆(像素域), 闭合。"""
    if rx <= 0:
        rx = 1.0
    if ry <= 0:
        ry = 1.0
    kx = KAPPA * rx
    ky = KAPPA * ry
    pts = [
        ((cx + rx, cy), (cx + rx, cy + ky), (cx + kx, cy + ry), (cx, cy + ry)),
        ((cx, cy + ry), (cx - kx, cy + ry), (cx - rx, cy + ky), (cx - rx, cy)),
        ((cx - rx, cy), (cx - rx, cy - ky), (cx - kx, cy - ry), (cx, cy - ry)),
        ((cx, cy - ry), (cx + kx, cy - ry), (cx + rx, cy - ky), (cx + rx, cy)),
    ]
    segments = [("M", cx + rx, cy)]
    for (p0, c1, c2, p1) in pts:
        segments.append(("C", c1[0], c1[1], c2[0], c2[1], p1[0], p1[1]))
    segments.append(("Z",))
    return segments


def _arc_segments(cx, cy, r, a0_deg, a1_deg):
    """圆弧(像素域, r 为视觉半径按短边换算), 不闭合。

    角度制, 0 度在 x 正方向, 屏幕坐标系下角度增大为顺时针(y 向下)。
    """
    if r <= 0:
        r = 1.0
    delta = a1_deg - a0_deg
    if abs(delta) >= 360.0:
        # 完整圆
        return _ellipse_segments(cx, cy, r, r)
    n = max(1, int(math.ceil(abs(delta) / 90.0)))
    step = math.radians(delta) / n
    th0 = math.radians(a0_deg)
    segments = []
    x0 = cx + r * math.cos(th0)
    y0 = cy + r * math.sin(th0)
    segments.append(("M", x0, y0))
    k = 4.0 / 3.0 * math.tan(step / 4.0)
    for i in range(n):
        th1 = th0 + step
        x1 = cx + r * math.cos(th1)
        y1 = cy + r * math.sin(th1)
        # 切线: d/dtheta (cos, sin) * r = (-sin, cos)
        t0x, t0y = -math.sin(th0), math.cos(th0)
        t1x, t1y = -math.sin(th1), math.cos(th1)
        c1 = (x0 + k * r * t0x, y0 + k * r * t0y)
        c2 = (x1 - k * r * t1x, y1 - k * r * t1y)
        segments.append(("C", c1[0], c1[1], c2[0], c2[1], x1, y1))
        x0, y0, th0 = x1, y1, th1
    return segments


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def parse_path_spec(path_obj, closed: bool, mapper: PixelMapper):
    """解析 use_tool 的 path 对象。

    返回 (segments, path_type, geometry_summary)。
    segments 为像素域段列表; closed 由调用者传入(eraser_area 强制 True)。
    """
    if not isinstance(path_obj, dict):
        raise PathError("Invalid path: path must be an object.")
    keys = [k for k in ("points", "d", "shape") if k in path_obj]
    if len(keys) == 0:
        raise PathError("path must contain exactly one of points, d, or shape + params.")
    if len(keys) > 1:
        raise PathError("path must contain exactly one of points, d, or shape + params.")
    key = keys[0]
    if key == "points":
        segments = _parse_points(path_obj["points"], mapper)
        path_type = "polyline"
        summary = f"polyline({len(path_obj['points'])} points)"
    elif key == "d":
        d = path_obj["d"]
        if not isinstance(d, str) or not d.strip():
            raise PathError("Invalid path: d must be a non-empty SVG path string.")
        segments = _SvgParser(d, mapper).parse()
        path_type = "svg_path"
        d_show = d if len(d) <= 48 else d[:48] + "..."
        summary = f"svg_path({d_show})"
    else:
        shape = path_obj["shape"]
        segments = _parse_shape(shape, path_obj.get("params", {}), mapper)
        path_type = "shape"
        summary = f"shape({shape})"

    if not segments:
        raise PathError("Invalid path: parsed path is empty.")

    # closed 处理: 对每个未闭合子路径追加 Z
    # circle/ellipse/rect 自带 Z; line 不闭合; arc/points/d 依 closed。
    segments = _apply_closed(segments, closed)
    return segments, path_type, summary


def _apply_closed(segments, closed: bool):
    """closed=True 时给每个未闭合的子路径追加 Z(连接回起点)。"""
    if not closed:
        return segments
    out = []
    sub_open = False
    has_moveto = False
    for seg in segments:
        out.append(seg)
        if seg[0] == "M":
            if sub_open:  # 上一个子路径未闭合
                out.insert(len(out) - 1, ("Z",))
            sub_open = True
            has_moveto = True
        elif seg[0] == "Z":
            sub_open = False
    if has_moveto and sub_open:
        out.append(("Z",))
    return out
