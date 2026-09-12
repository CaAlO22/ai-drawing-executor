# -*- coding: utf-8 -*-
"""自检: 一键确认本机能否运行本 skill / 本项目。

拷到新机器后先跑这个, 全绿即代表可以开始作画。它会:
  1. 检查 Python 版本与依赖(numpy / Pillow), 并报告可选的 PySide6 / tkinter;
  2. 用同目录的 app 包实际建画布、画一笔、导出 PNG 并回读校验。

用法(可从任意工作目录执行, 路径无需写死):
    python -X utf8 <项目根>/selfcheck.py
    python -X utf8 <项目根>/selfcheck.py --out check.png   # 指定产物路径
"""
import argparse
import base64
import io
import sys
from pathlib import Path

# 与 bridge_stdio.py 一致: 用 __file__ 定位项目根, 不写死任何路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {detail}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AI 绘画执行器 skill 自检")
    parser.add_argument("--out", default="skill_selfcheck.png",
                        help="自检产物 PNG 路径(默认 ./skill_selfcheck.png)")
    args = parser.parse_args(argv)

    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    print(f"平台   {sys.platform}")
    check("Python >= 3.9", sys.version_info >= (3, 9),
          f"{sys.version_info.major}.{sys.version_info.minor}")

    try:
        import numpy
        check("numpy 可用", True, numpy.__version__)
    except ImportError as e:
        check("numpy 可用", False, str(e))

    try:
        import PIL
        from PIL import Image  # noqa: F401
        check("Pillow 可用", True, getattr(PIL, "__version__", "?"))
    except ImportError as e:
        check("Pillow 可用", False, str(e))

    if FAIL:
        print("\n缺少依赖, 请先执行: "
              f'"{sys.executable}" -m pip install -r '
              f'{Path(__file__).with_name("requirements.txt")}')
        print(f"自检失败: {PASS} 通过, {FAIL} 失败")
        return 1

    try:
        from app.skill import create_skill_executor
        check("加载同目录 app 包(绘画引擎)", True)
    except Exception as e:
        check("加载同目录 app 包(绘画引擎)", False, repr(e))
        print("引擎加载失败: 请确认在项目根目录运行, 且已安装依赖")
        return 1

    ex = create_skill_executor(640, 480, background_color="#ffffff")
    tools = ex.get_tools_schema("skill")
    names = [t["function"]["name"] for t in tools]
    check("工具集完整(8 个, 含 get_current_picture)",
          len(names) == 8 and "get_current_picture" in names, f"{len(names)} 个")

    ex.call_tool("pick_tools", {"tool": "pen",
                                "settings": {"size": 24, "color": "#1f3864"}})
    r = ex.call_tool("use_tool", {"mode": "path", "path": {
        "shape": "circle", "params": {"cx": 500, "cy": 500, "r": 280}}})
    check("作画: pen 画圆", r.get("ok") is True and r.get("changed") is True)

    ex.call_tool("pick_tools", {"tool": "bucket",
                                "settings": {"color": "#ffe08a"}})
    r = ex.call_tool("use_tool", {"mode": "point", "x": 500, "y": 500})
    check("作画: bucket 填充", r.get("ok") is True)

    r = ex.call_tool("undo", {})
    check("撤销可用", r.get("ok") is True and r.get("undone_action_id"))
    r = ex.call_tool("redo", {})
    check("重做可用", r.get("ok") is True and r.get("redone_action_id"))

    pic = ex.call_tool("get_current_picture", {"full_resolution": True})
    ok_png = False
    if pic.get("ok") and pic.get("base64_image"):
        raw = base64.b64decode(pic["base64_image"])
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img.load()
            ok_png = img.size == (640, 480)
        except Exception:
            ok_png = False
    check("取回画布图片(base64 PNG)", ok_png, f"{pic.get('width')}x"
          f"{pic.get('height')}")

    info = ex.call_tool("get_canvas_info", {"include_geometry": True})
    check("读取画布信息", info.get("ok") is True)

    out = Path(args.out)
    try:
        engine = ex.engine
        buf = io.BytesIO()
        engine.observation_image(full_resolution=True).save(buf, format="PNG")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(buf.getvalue())
        check("导出 PNG 到磁盘", out.is_file() and out.stat().st_size > 0,
              f"{out} ({out.stat().st_size} bytes)")
    except Exception as e:
        check("导出 PNG 到磁盘", False, repr(e))

    # 可选能力: GUI 应能跑(项目手工运行时需要), 观察窗口自带双后端
    try:
        import PySide6  # noqa: F401
        print("[info] 可选: PySide6 已安装 —— GUI 可正常启动, "
              "观察窗口将使用 Qt 后端")
    except ImportError:
        print("[warn] 可选: 未安装 PySide6 —— 无法启动 GUI "
              "(headless 作画不受影响; 观察窗口将退回 tkinter)")
    try:
        import tkinter  # noqa: F401
        backend = "可用"
    except ImportError:
        backend = "不可用"
    print(f"[info] 可选: 观察窗口 tkinter 兜底后端 {backend}"
          + ("" if backend == "可用" else "(不影响作画, 可用任意看图工具打开帧 PNG)"))

    print(f"\n自检结果: {PASS} 通过, {FAIL} 失败")
    if FAIL:
        return 1
    print("本机可以直接运行本 skill ✅  (下一步: "
          f'python -X utf8 {Path(__file__).with_name("bridge_stdio.py")})')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
