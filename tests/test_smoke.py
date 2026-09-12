# -*- coding: utf-8 -*-
"""无 GUI 冒烟测试: 覆盖全部工具接口 + 历史语义 + 错误路径 + 导出。

运行: python tests/test_smoke.py
模拟外部 Skill 模式 harness 通过 ToolExecutor 完成一幅演示画,
证明在没有模型配置的情况下工具链本身完整可用。
"""
import base64
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image

from app.core.canvas_engine import CanvasEngine
from app.tools.executor import ToolExecutor
from app.tools.schema import VALID_TOOL_NAMES_LOOP, VALID_TOOL_NAMES_SKILL

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def canvas_stats(engine):
    arr = engine.snapshot_rgb()
    return arr


def test_schemas():
    print("== 工具 schema ==")
    skill = ToolExecutor(CanvasEngine(200, 200)).get_tools_schema("skill")
    loop = ToolExecutor(CanvasEngine(200, 200)).get_tools_schema("loop")
    skill_names = [t["function"]["name"] for t in skill]
    loop_names = [t["function"]["name"] for t in loop]
    check("skill 含 get_current_picture", "get_current_picture" in skill_names)
    check("loop 不含 get_current_picture", "get_current_picture" not in loop_names)
    check("loop 工具集符合任务定义",
          loop_names == ["pick_tools", "use_tool", "undo", "redo",
                         "get_canvas_info", "finish", "itsHardToFinish"],
          str(loop_names))
    check("skill 工具集 = loop + get_current_picture",
          skill_names == loop_names + ["get_current_picture"])
    # schema 可 JSON 序列化
    json.dumps(skill)
    json.dumps(loop)
    check("schema 可 JSON 序列化", True)
    check("VALID_TOOL_NAMES 常量一致",
          sorted(VALID_TOOL_NAMES_SKILL) == sorted(skill_names)
          and sorted(VALID_TOOL_NAMES_LOOP) == sorted(loop_names))
    # 工具描述里必须带上"一笔一笔画、画几笔看一眼"的节奏引导
    desc = {t["function"]["name"]: t["function"].get("description", "")
            for t in skill}
    check("use_tool 描述鼓励小步作画(不要一口气画完)",
          "SMALL STEPS" in desc["use_tool"]
          and "get_current_picture" in desc["use_tool"])
    check("get_current_picture 描述鼓励频繁查看",
          "every few strokes" in desc["get_current_picture"])
    check("pick_tools 描述说明 bucket 会封住细描边",
          "leak" in desc["pick_tools"])


def test_pick_tools(ex: ToolExecutor):
    print("== pick_tools ==")
    r = ex.call_tool("pick_tools", {"tool": "brush", "settings": {
        "size": 60, "color": "#888888", "opacity": 0.6}})
    check("选择 brush 成功", r.get("ok") is True and
          r["current_tool"]["tool"] == "brush")
    check("返回完整工具状态", r["current_tool"]["size"] == 60
          and r["current_tool"]["opacity"] == 0.6)

    r = ex.call_tool("pick_tools", {"tool": "pen", "settings": {"color": "#FF0000"}})
    check("只修改 pen 颜色", r["current_tool"]["color"] == "#ff0000"
          and r["current_tool"]["size"] == 20)

    r = ex.call_tool("pick_tools", {"tool": "brush"})
    check("brush 记住自己的设置", r["current_tool"]["opacity"] == 0.6)

    r = ex.call_tool("pick_tools", {"tool": "nope"})
    check("非法工具报错", r.get("ok") is False and "Invalid tool" in r["error"])

    r = ex.call_tool("pick_tools", {"tool": "pen", "settings": {"color": "red"}})
    check("非法颜色报错", r.get("ok") is False and "#RRGGBB" in r["error"])

    r = ex.call_tool("pick_tools", {"tool": "brush", "settings": {"opacity": 0.95}})
    check("opacity 超上限报错", r.get("ok") is False
          and "0.3 and 0.9" in r["error"])

    r = ex.call_tool("pick_tools", {"tool": "brush", "settings": {"opacity": 0.1}})
    check("opacity 超下限报错", r.get("ok") is False)

    r = ex.call_tool("pick_tools", {"tool": "brush", "settings": {"transparency": 0.5}})
    check("transparency 兼容为 opacity(非 1-opacity)",
          r.get("ok") is True and abs(r["current_tool"]["opacity"] - 0.5) < 1e-9)

    r = ex.call_tool("pick_tools", {"tool": "brush", "settings": {
        "opacity": 0.7, "transparency": 0.4}})
    check("opacity 与 transparency 冲突以 opacity 为准",
          r.get("ok") is True and abs(r["current_tool"]["opacity"] - 0.7) < 1e-9)

    r = ex.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#ffcc00", "tolerance": 25}})
    check("bucket 设置", r.get("ok") is True and r["current_tool"]["tolerance"] == 25)

    r = ex.call_tool("pick_tools", {"tool": "bucket", "settings": {"tolerance": 300}})
    check("tolerance 超范围 clamp(宽松)", r.get("ok") is True)

    r = ex.call_tool("pick_tools", {"tool": "eraser_stroke",
                                    "settings": {"hit_radius": 12}})
    check("eraser_stroke 设置", r.get("ok") is True
          and r["current_tool"]["hit_radius"] == 12)


def test_use_tool_draw(ex: ToolExecutor, engine: CanvasEngine):
    print("== use_tool: pen/brush 绘制 ==")
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 20,
                                                            "color": "#000000"}})
    before = canvas_stats(engine).copy()
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 200], [180, 230], [260, 280]]}, "closed": False})
    check("点列绘制成功", r.get("ok") is True and r.get("changed") is True)
    check("生成 stroke_id / action_id", r.get("stroke_id", "").startswith("stroke_")
          and r.get("action_id", "").startswith("action_"))
    check("返回 current_tool", r["current_tool"]["tool"] == "pen")
    check("画布发生变化", not np.array_equal(before, canvas_stats(engine)))

    # 单点
    r = ex.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
    check("pen point 模式画点", r.get("ok") is True and r.get("changed") is True)

    # SVG path
    ex.call_tool("pick_tools", {"tool": "brush", "settings": {
        "size": 60, "color": "#888888", "opacity": 0.6}})
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "d": "M 200 500 C 300 400 400 600 500 500"}, "closed": False})
    check("SVG path 绘制成功", r.get("ok") is True and r.get("changed") is True)

    # SVG 相对命令
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "d": "m 600 300 c 50 -50 100 50 150 0 l 40 60 h 30 v 30 z"}})
    check("SVG 小写相对命令", r.get("ok") is True and r.get("changed") is True)

    # SVG Q/A 命令
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "d": "M 100 700 Q 200 650 300 700 A 80 80 0 0 1 460 700"}})
    check("SVG Q/A 命令", r.get("ok") is True and r.get("changed") is True)

    # 基本图形
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 15,
                                                            "color": "#0000ff"}})
    for shape, params in [
        ("circle", {"cx": 500, "cy": 500, "r": 200}),
        ("ellipse", {"cx": 300, "cy": 300, "rx": 150, "ry": 80}),
        ("rect", {"x": 200, "y": 200, "width": 300, "height": 150}),
        ("line", {"x1": 100, "y1": 100, "x2": 900, "y2": 900}),
        ("arc", {"cx": 500, "cy": 500, "r": 250,
                 "start_angle": 0, "end_angle": 180}),
    ]:
        r = ex.call_tool("use_tool", {"mode": "path", "path": {
            "shape": shape, "params": params}, "closed": True})
        check(f"shape {shape} 绘制", r.get("ok") is True and r.get("changed") is True)

    # arc 默认不闭合
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "shape": "arc", "params": {"cx": 700, "cy": 700, "r": 150,
                                   "start_angle": 20, "end_angle": 160}},
        "closed": False})
    check("arc 不闭合绘制", r.get("ok") is True)

    # use_tool 后工具状态保持
    info = ex.call_tool("get_canvas_info", {})
    check("use_tool 后当前工具保持", info["current_tool"]["tool"] == "pen")


def test_use_tool_errors(ex: ToolExecutor):
    print("== use_tool 错误路径 ==")
    ex.call_tool("pick_tools", {"tool": "pen"})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
    check("pen 用 point 模式不报错(画点)", r.get("ok") is True)

    ex.call_tool("pick_tools", {"tool": "bucket"})
    r = ex.call_tool("use_tool", {"mode": "path", "path": {"points": [[1, 1]]}})
    check("bucket 用 path 模式报错", r.get("ok") is False
          and "mode=point" in r["error"])

    ex.call_tool("pick_tools", {"tool": "eraser_stroke"})
    r = ex.call_tool("use_tool", {"mode": "path", "path": {"points": [[1, 1]]}})
    check("eraser_stroke 用 path 模式报错", r.get("ok") is False)

    ex.call_tool("pick_tools", {"tool": "eraser_area"})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 100, "y": 100})
    check("eraser_area 用 point 模式报错", r.get("ok") is False
          and "mode=path" in r["error"])

    ex.call_tool("pick_tools", {"tool": "pen"})
    r = ex.call_tool("use_tool", {"mode": "path"})
    check("缺 path 报错", r.get("ok") is False)

    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[1, 1]], "d": "M 0 0 L 1 1"}})
    check("path 多种表示并存报错", r.get("ok") is False
          and "exactly one" in r["error"])

    r = ex.call_tool("use_tool", {"mode": "path", "path": {}})
    check("path 空对象报错", r.get("ok") is False)

    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[1, "x"], [2, 3]]}})
    check("points 非法格式报错", r.get("ok") is False
          and "[x, y]" in r["error"])

    r = ex.call_tool("use_tool", {"mode": "path", "path": {"d": "M 0 0 X 1 1"}})
    check("SVG 非法字符报错", r.get("ok") is False)

    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "shape": "star", "params": {}}})
    check("不支持的基本图形报错", r.get("ok") is False)

    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "shape": "circle", "params": {"cx": 500}}})
    check("图形缺参数报错", r.get("ok") is False
          and "requires parameter" in r["error"])

    r = ex.call_tool("use_tool", {"mode": "point", "x": 500})
    check("point 模式缺 y 报错", r.get("ok") is False)

    r = ex.call_tool("use_tool", {"mode": "banana"})
    check("非法 mode 报错", r.get("ok") is False)

    r = ex.call_tool("not_a_tool", {})
    check("未知工具报错", r.get("ok") is False and "Unknown tool" in r["error"])

    # 坐标越界 clamp 而非崩溃
    ex.call_tool("pick_tools", {"tool": "pen"})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 1200, "y": -50})
    check("坐标越界 clamp", r.get("ok") is True)


def test_history(engine: CanvasEngine, ex: ToolExecutor):
    print("== 历史: undo / redo ==")
    e2 = CanvasEngine(400, 300)
    ex2 = ToolExecutor(e2)
    ex2.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 40,
                                                             "color": "#000000"}})
    a = ex2.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [200, 150]]}})
    b = ex2.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[250, 100], [250, 200]]}})
    s1 = e2.snapshot_rgb()
    check("两次绘制成功", a.get("changed") and b.get("changed"))

    r = ex2.call_tool("undo", {})
    s2 = e2.snapshot_rgb()
    check("undo 成功", r.get("ok") is True and r.get("undone_action_id"))
    check("undo 后画布回退", not np.array_equal(s1, s2))

    r = ex2.call_tool("redo", {})
    s3 = e2.snapshot_rgb()
    check("redo 成功", r.get("ok") is True and r.get("redone_action_id"))
    check("redo 恢复画布", np.array_equal(s1, s3))

    # 新 use_tool 清空 redo 栈
    ex2.call_tool("undo", {})
    ex2.call_tool("use_tool", {"mode": "point", "x": 50, "y": 50})
    r = ex2.call_tool("redo", {})
    check("新动作后 redo 栈被清空", r.get("redone_action_id") is None)

    # undo 到底
    e3 = CanvasEngine(300, 200)
    ex3 = ToolExecutor(e3)
    r = ex3.call_tool("undo", {})
    check("空历史 undo 返回提示", r.get("ok") is True
          and r.get("undone_action_id") is None
          and r.get("can_undo") is False)
    r = ex3.call_tool("redo", {})
    check("空历史 redo 返回提示", r.get("ok") is True
          and r.get("redone_action_id") is None)

    # pick_tools 不进历史
    ex3.call_tool("pick_tools", {"tool": "brush"})
    r = ex3.call_tool("undo", {})
    check("pick_tools 不进历史", r.get("undone_action_id") is None)

    # 无变化动作不进历史
    ex3.call_tool("pick_tools", {"tool": "pen",
                                 "settings": {"size": 10, "color": "#ffffff"}})
    r = ex3.call_tool("use_tool", {"mode": "point", "x": 100, "y": 100})
    check("白笔画白底不进历史", r.get("ok") is True
          and r.get("changed") is False)
    info = ex3.call_tool("get_canvas_info", {})
    check("动作计数为 0", info["history"]["action_count"] == 0)


def test_closed_outline_and_bucket_leak():
    print("== 闭合轮廓收尾段 / bucket 不从细描边缝隙漏色 ==")
    # 回归 1: 闭合折线的收尾段必须被渲染出来。
    # 曾经 _densify 只在 len(插入点) > len(原采样点) 时才补点, 而闭合路径的
    # 收尾段很长、原采样点却很多, 于是收尾段被整体跳过 —— 轮廓上留下一道
    # 细缝(看起来像抗锯齿缝隙), bucket 会从这道缝漏到背景。
    e = CanvasEngine(800, 600)
    ex = ToolExecutor(e)
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 3,
                                                           "color": "#111111"}})
    r = ex.call_tool("use_tool", {"mode": "path", "closed": True, "path": {
        "points": [[250, 200], [750, 200], [750, 800], [250, 800]]}})
    check("闭合折线绘制成功", r.get("ok") is True and r.get("changed") is True)
    arr = e.snapshot_rgb()
    # 归一化 (250,500) -> 像素 (200,300), 正落在收尾的左边线上
    check("闭合折线收尾段已绘制(左边缘非白)",
          tuple(int(v) for v in arr[300, 200]) != (255, 255, 255),
          f"got {tuple(int(v) for v in arr[300, 200])}")

    # 回归 2: 细描边闭合区域内部填充, 不得越过描边把背景染色
    ex.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#00b0ff", "tolerance": 20}})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
    check("细描边区域内填充成功", r.get("ok") is True and r.get("changed") is True)
    arr = e.snapshot_rgb()
    check("内部填充颜色正确",
          tuple(int(v) for v in arr[300, 400]) == (0, 176, 255),
          f"got {tuple(int(v) for v in arr[300, 400])}")
    corners = [(3, 3), (796, 3), (3, 596), (796, 596)]
    check("背景四角未被染色(不漏色)",
          all(tuple(int(v) for v in arr[y, x]) == (255, 255, 255)
              for x, y in corners),
          str([tuple(int(v) for v in arr[y, x]) for x, y in corners]))
    check("填充面积没有漫出轮廓(< 画布一半)",
          r.get("filled_pixels", 0) < 800 * 600 // 2,
          f"got {r.get('filled_pixels')}")

    # 回归 3: 两段细圆弧拼接成的闭合环 + 内部填充
    e2 = CanvasEngine(600, 400)
    ex2 = ToolExecutor(e2)
    ex2.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 2,
                                                            "color": "#111111"}})
    for a0, a1 in ((0, 180), (180, 360)):
        ex2.call_tool("use_tool", {"mode": "path", "closed": True, "path": {
            "shape": "arc", "params": {"cx": 500, "cy": 500, "r": 320,
                                       "start_angle": a0, "end_angle": a1}}})
    ex2.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#ff5252", "tolerance": 20}})
    r = ex2.call_tool("use_tool", {"mode": "point", "x": 500, "y": 300})
    arr2 = e2.snapshot_rgb()
    check("拼接细环内部填充不漏色",
          r.get("ok") is True and tuple(int(v) for v in arr2[3, 3]) == (255, 255, 255),
          f"corner={tuple(int(v) for v in arr2[3, 3])}")

    # 回归 4: 均匀画布的整幅填充像素数不受"障碍图加厚"影响
    e3 = CanvasEngine(200, 150)
    ex3 = ToolExecutor(e3)
    ex3.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#ff0000", "tolerance": 10}})
    r = ex3.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
    check("均匀画布仍为整幅填充",
          r.get("filled_pixels") == 200 * 150, f"got {r.get('filled_pixels')}")

    # 回归 5: 笔宽从亚像素到中粗, 内部填充都不得漏到背景。
    # 亚像素笔宽(归一律 size 1~2 -> 画布短边 600 时实际只有 0.6~1.2px)经
    # 抗锯齿后, 障碍图会退化成带 1px 断点的虚线, 近水平/近垂直的段在换行处
    # 还会错开约 2px。闭运算(膨胀再腐蚀)补不上这种断点 —— 腐蚀会把刚连起来
    # 的桥原样啃掉; 只有"障碍图只膨胀不腐蚀、用它判定连通"才封得住。
    for size in (1, 2, 3, 4, 5, 6, 8, 12):
        ew = CanvasEngine(480, 360)
        exw = ToolExecutor(ew)
        exw.call_tool("pick_tools", {"tool": "pen", "settings": {
            "size": size, "color": "#111111"}})
        exw.call_tool("use_tool", {"mode": "path", "closed": True, "path": {
            "points": [[200, 200], [800, 200], [800, 800], [200, 800]]}})
        exw.call_tool("pick_tools", {"tool": "bucket", "settings": {
            "color": "#00b0ff", "tolerance": 20}})
        rw = exw.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
        aw = ew.snapshot_rgb()
        corner = tuple(int(v) for v in aw[2, 2])
        check(f"笔宽 size={size} 填充不漏到背景",
              rw.get("ok") is True and rw.get("changed") is True
              and corner == (255, 255, 255)
              and rw.get("filled_pixels", 0) < 480 * 360 // 2,
              f"filled={rw.get('filled_pixels')} corner={corner}")

    # 回归 6: 开放轮廓(未闭合)仍然会漏 —— 这是预期行为, 提示不要误用
    e4 = CanvasEngine(400, 300)
    ex4 = ToolExecutor(e4)
    ex4.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 3,
                                                            "color": "#111111"}})
    ex4.call_tool("use_tool", {"mode": "path", "closed": False, "path": {
        "points": [[100, 100], [900, 100], [900, 900], [100, 900]]}})
    ex4.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#00b0ff", "tolerance": 20}})
    r = ex4.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
    check("开放轮廓填充会漫出(符合预期, 不负责补开放轮廓的真缺口)",
          r.get("filled_pixels", 0) > 400 * 300 // 2,
          f"got {r.get('filled_pixels')}")


def test_erase_and_bucket():
    print("== eraser_area / bucket / eraser_stroke ==")
    e = CanvasEngine(600, 400)
    ex = ToolExecutor(e)
    # 红色矩形填充背景(桶)
    ex.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#ff0000", "tolerance": 10}})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 300, "y": 200})
    check("bucket 全画布填充", r.get("ok") is True and r.get("changed") is True)
    check("filled_pixels 合理", r.get("filled_pixels", 0) == 600 * 400)

    # 再点一次同色 -> 无变化不进历史
    r = ex.call_tool("use_tool", {"mode": "point", "x": 300, "y": 200})
    check("bucket 无变化不进历史", r.get("ok") is True
          and r.get("changed") is False)

    # 容差填充: 在红底上放一个深红块再点
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 60,
                                                            "color": "#ff0505"}})
    ex.call_tool("use_tool", {"mode": "point", "x": 100, "y": 100})
    ex.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#0000ff", "tolerance": 30}})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 300, "y": 200})
    check("bucket 容差扩散填充", r.get("ok") is True and r.get("changed") is True)
    px = e.snapshot_rgb()[200, 300]
    check("填充颜色正确", tuple(px) == (0, 0, 255))

    # eraser_area 擦除区域
    # 归一化 rect(400,300,150,80) -> 像素 x∈[240,330) y∈[120,152) (600x400 画布)
    ex.call_tool("pick_tools", {"tool": "eraser_area"})
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "shape": "rect", "params": {"x": 400, "y": 300, "width": 150,
                                    "height": 80}}})
    check("eraser_area 擦除成功", r.get("ok") is True and r.get("changed") is True)
    px = e.snapshot_rgb()[135, 280]
    check("擦除后为白色", tuple(px) == (255, 255, 255), f"got {tuple(px)}")

    # eraser_stroke 删除笔画 + 底下内容恢复
    e2 = CanvasEngine(600, 400)
    ex2 = ToolExecutor(e2)
    ex2.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#00ff00", "tolerance": 0}})
    ex2.call_tool("use_tool", {"mode": "point", "x": 10, "y": 10})   # 绿底
    ex2.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 30,
                                                             "color": "#000000"}})
    d = ex2.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[200, 200], [400, 200]]}})
    stroke_id = d.get("stroke_id")
    # 归一化点 (300,200) -> 像素 (180, 80): 笔画中心线上
    mid = e2.snapshot_rgb()[80, 180].copy()
    check("笔画已绘制(黑)", tuple(mid) == (0, 0, 0), f"got {tuple(mid)}")

    ex2.call_tool("pick_tools", {"tool": "eraser_stroke",
                                 "settings": {"hit_radius": 15}})
    r = ex2.call_tool("use_tool", {"mode": "point", "x": 300, "y": 200})
    check("eraser_stroke 命中删除", r.get("ok") is True
          and r.get("removed_stroke_id") == stroke_id)
    after = e2.snapshot_rgb()[80, 180]
    check("删除后恢复下方内容(绿)", tuple(after) == (0, 255, 0),
          f"got {tuple(after)}")

    # undo 恢复笔画
    r = ex2.call_tool("undo", {})
    again = e2.snapshot_rgb()[80, 180]
    check("undo 恢复被删笔画(黑)", tuple(again) == (0, 0, 0),
          f"got {tuple(again)}")
    r = ex2.call_tool("redo", {})
    gone = e2.snapshot_rgb()[80, 180]
    check("redo 再次删除笔画(绿)", tuple(gone) == (0, 255, 0))

    # 未命中不报错
    r = ex2.call_tool("use_tool", {"mode": "point", "x": 30, "y": 30})
    check("eraser_stroke 未命中返回提示", r.get("ok") is True
          and r.get("removed_stroke_id") is None
          and r.get("changed") is False)

    # stroke_id 在删除/恢复中稳定
    info = ex2.call_tool("get_canvas_info", {})
    check("stroke 元数据查询正常", info.get("stroke_count") == 0)  # 唯一笔画被 redo 删除


def test_get_current_picture():
    print("== get_current_picture (Skill 模式专用) ==")
    e = CanvasEngine(1920, 1080)
    ex = ToolExecutor(e, observe_max_side=1024)
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 40,
                                                            "color": "#000000"}})
    ex.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [900, 900]]}})
    r = ex.call_tool("get_current_picture", {})
    check("返回 base64 PNG", r.get("ok") is True and r.get("format") == "png")
    check("缩放到最长边 1024", r.get("width") == 1024 and r.get("height") == 576,
          f"got {r.get('width')}x{r.get('height')}")
    img = Image.open(io.BytesIO(base64.b64decode(r["base64_image"])))
    check("base64 可解码为图片", img.size == (1024, 576))
    r_full = ex.call_tool("get_current_picture", {"full_resolution": True})
    check("full_resolution 返回原始尺寸",
          r_full.get("width") == 1920 and r_full.get("height") == 1080)


def test_canvas_info():
    print("== get_canvas_info ==")
    e = CanvasEngine(800, 600)
    ex = ToolExecutor(e)
    ex.call_tool("pick_tools", {"tool": "brush", "settings": {
        "size": 60, "color": "#888888", "opacity": 0.6}})
    ex.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [200, 200], [300, 150]]}})
    info = ex.call_tool("get_canvas_info", {})
    check("画布尺寸", info["canvas"] == {"width": 800, "height": 600})
    check("当前工具", info["current_tool"]["tool"] == "brush")
    check("历史状态", info["history"]["action_count"] == 1
          and info["history"]["can_undo"] is True)
    check("笔画摘要", info["stroke_count"] == 1
          and info["strokes"][0]["tool"] == "brush"
          and "opacity" in info["strokes"][0])
    info_geo = ex.call_tool("get_canvas_info", {"include_geometry": True})
    check("include_geometry 返回点列",
          "points_px" in info_geo["strokes"][0])


def test_termination():
    print("== finish / itsHardToFinish ==")
    e = CanvasEngine(300, 200)
    ex = ToolExecutor(e)
    r = ex.call_tool("finish", {"summary": "已完成。"})
    check("finish 返回", r.get("ok") is True and r["status"] == "finished"
          and r["summary"] == "已完成。")
    r = ex.call_tool("itsHardToFinish", {"reason": "太难了。"})
    check("itsHardToFinish 返回", r.get("ok") is True and r["status"] == "aborted"
          and r["reason"] == "太难了。")


def draw_demo(engine: CanvasEngine, ex: ToolExecutor) -> None:
    """模拟 AI 的工具调用序列画一幅简单风景画(证明工具链可用)。"""
    def T(name, args):
        r = ex.call_tool(name, args)
        assert r.get("ok"), f"{name}: {r}"

    # 天空
    T("pick_tools", {"tool": "bucket", "settings": {"color": "#aee3ff",
                                                     "tolerance": 10}})
    T("use_tool", {"mode": "point", "x": 50, "y": 50})
    # 草地
    T("pick_tools", {"tool": "brush", "settings": {"size": 420, "color": "#7ec850",
                                                   "opacity": 0.9}})
    T("use_tool", {"mode": "path", "path": {"points": [[0, 760], [500, 740],
                                                       [1000, 770]]}})
    T("pick_tools", {"tool": "bucket", "settings": {"color": "#7ec850",
                                                    "tolerance": 60}})
    T("use_tool", {"mode": "point", "x": 500, "y": 950})
    # 太阳
    T("pick_tools", {"tool": "pen", "settings": {"size": 24, "color": "#ffb300"}})
    T("use_tool", {"mode": "path", "path": {"shape": "circle", "params": {
        "cx": 830, "cy": 150, "r": 90}}})
    T("pick_tools", {"tool": "bucket", "settings": {"color": "#ffd54f",
                                                    "tolerance": 30}})
    T("use_tool", {"mode": "point", "x": 830, "y": 150})
    # 房子
    T("pick_tools", {"tool": "pen", "settings": {"size": 14, "color": "#8d6e63"}})
    T("use_tool", {"mode": "path", "path": {"shape": "rect", "params": {
        "x": 180, "y": 520, "width": 280, "height": 200}}})
    T("pick_tools", {"tool": "bucket", "settings": {"color": "#ffe0b2",
                                                    "tolerance": 40}})
    T("use_tool", {"mode": "point", "x": 320, "y": 620})
    # 屋顶(用 SVG path)
    T("pick_tools", {"tool": "pen", "settings": {"size": 16, "color": "#c62828"}})
    T("use_tool", {"mode": "path", "path": {
        "d": "M 150 520 L 320 380 L 490 520 Z"}})
    T("pick_tools", {"tool": "bucket", "settings": {"color": "#ef5350",
                                                    "tolerance": 40}})
    T("use_tool", {"mode": "point", "x": 320, "y": 500})
    # 门和窗
    T("pick_tools", {"tool": "pen", "settings": {"size": 10, "color": "#5d4037"}})
    T("use_tool", {"mode": "path", "path": {"shape": "rect", "params": {
        "x": 290, "y": 610, "width": 60, "height": 110}}})
    T("use_tool", {"mode": "path", "path": {"shape": "rect", "params": {
        "x": 220, "y": 560, "width": 50, "height": 50}}})
    # 云(毛笔)
    T("pick_tools", {"tool": "brush", "settings": {"size": 80, "color": "#ffffff",
                                                   "opacity": 0.85}})
    T("use_tool", {"mode": "path", "path": {"points": [[150, 140], [230, 120],
                                                       [310, 140]]}})
    T("pick_tools", {"tool": "brush", "settings": {"size": 60, "color": "#ffffff",
                                                   "opacity": 0.85}})
    T("use_tool", {"mode": "point", "x": 230, "y": 120})
    # 弧形彩虹装饰
    T("pick_tools", {"tool": "pen", "settings": {"size": 10, "color": "#7c4dff"}})
    T("use_tool", {"mode": "path", "path": {"shape": "arc", "params": {
        "cx": 550, "cy": 700, "r": 260, "start_angle": 180, "end_angle": 360}}})
    # 撤销一次(彩虹)再重做, 演示历史
    T("undo", {})
    T("redo", {})


def test_performance():
    print("== 性能: 1920x1080 ==")
    e = CanvasEngine(1920, 1080)
    ex = ToolExecutor(e)
    ex.call_tool("pick_tools", {"tool": "pen", "settings": {"size": 20,
                                                            "color": "#000000"}})
    t0 = time.perf_counter()
    for _ in range(20):
        ex.call_tool("use_tool", {"mode": "path", "path": {
            "points": [[100, 100], [300, 200], [500, 150], [700, 300],
                       [900, 250]]}})
    dt = time.perf_counter() - t0
    check(f"20 次多点笔画 < 3s (实际 {dt:.2f}s)", dt < 3.0)

    ex.call_tool("pick_tools", {"tool": "bucket", "settings": {
        "color": "#ff0000", "tolerance": 0}})
    t0 = time.perf_counter()
    r = ex.call_tool("use_tool", {"mode": "point", "x": 5, "y": 5})
    dt = time.perf_counter() - t0
    check(f"全画布洪水填充 < 2s (实际 {dt:.2f}s, {r.get('filled_pixels')} px)",
          dt < 2.0)

    ex.call_tool("pick_tools", {"tool": "brush", "settings": {
        "size": 300, "color": "#334455", "opacity": 0.8}})
    t0 = time.perf_counter()
    ex.call_tool("use_tool", {"mode": "path", "path": {
        "d": "M 100 540 C 500 300 1400 800 1820 540"}})
    dt = time.perf_counter() - t0
    check(f"大毛笔长曲线 < 2s (实际 {dt:.2f}s)", dt < 2.0)

    t0 = time.perf_counter()
    img = e.observation_image(1024)
    dt = time.perf_counter() - t0
    check(f"观察图缩放 < 0.5s (实际 {dt:.2f}s, {img.size})", dt < 0.5)


def main():
    print("AI 绘画执行器 - 无 GUI 冒烟测试")
    print("=" * 60)
    test_schemas()

    engine = CanvasEngine(1920, 1080)
    ex = ToolExecutor(engine)

    # 初始画布为白色
    arr = engine.snapshot_rgb()
    check("初始画布为白色", bool((arr == 255).all()))
    check("画布尺寸正确", arr.shape == (1080, 1920, 3))

    test_pick_tools(ex)
    test_use_tool_draw(ex, engine)
    test_use_tool_errors(ex)
    test_history(engine, ex)
    test_erase_and_bucket()
    test_closed_outline_and_bucket_leak()
    test_get_current_picture()
    test_canvas_info()
    test_termination()
    test_performance()

    print("=" * 60)
    print(f"总计: {PASS} 通过, {FAIL} 失败")
    if FAIL:
        sys.exit(1)

    # 生成演示画(外部 Skill 模式模拟)
    print("生成演示画 demo_drawing.png ...")
    demo_engine = CanvasEngine(1920, 1080)
    demo_ex = ToolExecutor(demo_engine)
    draw_demo(demo_engine, demo_ex)
    out = Path(__file__).resolve().parent.parent / "demo_drawing.png"
    demo_engine.export_png(str(out))
    print(f"演示画已导出: {out}")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
