# AI 绘画执行器 (ai-drawing-executor.skill)

是时候让VLM拿起画笔了！

一个由 **AI 工具调用驱动** 的桌面绘画应用：画布上的所有绘制、擦除、填充、  
撤销、重做都只能通过工具调用完成，人类用户只负责启动、停止、查看和导出。

模型通过命令行（`draw_cli.py`）或 stdio 桥（`bridge_stdio.py`）与常驻绘画  
应用 `draw_app.py` 交互——**每执行一条命令就是一次工具调用**，应用持有画布，  
并打开只读观察窗口让用户实时旁观模型一笔一笔作画。

## 特性

- **人类零画笔**：GUI 画布只读，绘画工具只暴露给 AI
- **function-call 作画流程**：`launch_app → pick_tools / use_tool / undo / redo / get_current_picture → finish`，逐笔调用、逐笔可见
- **实时旁观窗口**：左栏当前工具状态、中栏只读画布、右栏自然语言历史记录
- **上下文友好**：模型可见的画布观察图一律压缩到长边 ≤ 640，原图只在 `finish` 导出时生成
- **过程可重放**：结束时自动导出原始分辨率 PNG + 完整绘画过程 JSON，`replay.py` 可忠实重放每一步（含失败的调用与撤销/重做）
- **可移植分发**：整个仓库即分发包，无写死路径；headless 模式只需 Python 3.9+ 与 numpy / Pillow
- **双接入方式**：Python 框架直接 import，任意语言走 JSON Lines over stdio 桥

## 目录结构

```text
SKILL.md            # Skill 提示词与作画规范（模型侧的完整说明书）
draw_cli.py         # 命令行入口：launch_app / pick_tools / use_tool / finish ...
draw_app.py         # 常驻绘画应用（持有画布 + 只读观察窗口）
bridge_stdio.py     # JSON Lines over stdio 桥，供任意语言框架调用
app/                # 引擎与 GUI（CanvasEngine / ToolExecutor / Drawing Loop / PySide6 界面）
replay.py           # 重放绘画过程 JSON（--verbose 逐条打印，--watch 边放边看）
replay_viewer.py    # 重放三栏观察窗口（配合 --watch）
watch_viewer.py     # 只读观察窗口
selfcheck.py        # 新机器自检（12 项）
tests/              # 完整测试套件（smoke / GUI / loop / skill / CLI / 可移植性）
run.bat             # 启动 GUI 应用
```

## 安装

```bash
# headless 作画（最小依赖）
pip install numpy Pillow

# 带观察窗口 / GUI（Windows 可直接双击 run.bat）
pip install -r requirements.txt
```

新机器建议先跑一次自检：

```bash
python -X utf8 selfcheck.py
```

## 快速开始

### 方式一：命令行逐笔作画（模型主流程）

会话数据基于当前工作目录，作画过程中不要中途切换目录。

```bash
# 1. 启动画布应用 + 只读观察窗口
python -X utf8 draw_cli.py launch_app --width 1920 --height 1080

# 2. 选工具（pen | brush | eraser_area | eraser_stroke | bucket）
python -X utf8 draw_cli.py pick_tools '{"tool": "pen", "settings": {"size": 20, "color": "#000000"}}'

# 3. 画一笔（坐标归一化 0~1000）
python -X utf8 draw_cli.py use_tool '{"mode": "path", "path": {"points": [[100,100],[500,500],[900,100]]}}'

# 4. 保存观察图到 PNG 并查看，画歪了就 undo
python -X utf8 draw_cli.py get_current_picture
python -X utf8 draw_cli.py undo

# 5. 结束：自动导出 drawing-<时间戳>.png（原图）+ drawing-process-<时间戳>.json（过程）
python -X utf8 draw_cli.py finish '{"summary": "一幅简笔画猫"}'

# 6. 收尾
python -X utf8 draw_cli.py shutdown
```

> ⚠️ 作画节奏的硬性规范见 `SKILL.md`：每条命令必须单独作为一次工具调用，  
> 禁止写脚本一次性作画、禁止把多条命令用 `&&` 串联。

### 方式二：重放绘画过程

```bash
python -X utf8 replay.py drawing-process-xxx.json            # 忠实重放并校验
python -X utf8 replay.py drawing-process-xxx.json --verbose  # 逐条打印动作
python -X utf8 replay.py drawing-process-xxx.json --watch    # 三栏观察窗边放边看
python -X utf8 replay.py drawing-process-xxx.json --out x.png
```

重放会按原顺序执行**每一次**调用（失败调用照原样再跑、undo/redo 按原位重放），  
并与记录结果逐字段比对，全一致输出 `[ok]`。

### 方式三：Python 框架直接接入

```python
from app.skill import create_skill_executor

executor = create_skill_executor()   # 提供 OpenAI tools schema + call_tool
```

### 方式四：任意语言经 stdio 桥接入

```bash
python -X utf8 bridge_stdio.py
```

JSON Lines over stdio，协议细节见 `SKILL.md`。

### GUI 应用

```bash
python -m app.main        # 或 Windows 双击 run.bat
```

启动后输入画布尺寸与绘画目标，即可运行应用内 Drawing Loop（由应用自行  
调用多模态模型并管理上下文，每轮自动回传最新画布图片）。模型参数在首次  
运行时自动生成的 `config.json` 中配置，`api_key` 也可用环境变量  
`AI_DRAWING_API_KEY` 或 `OPENAI_API_KEY` 覆盖（该文件不入版本库）。

## 工具一览

| 工具                           | 说明                             |
| ---------------------------- | ------------------------------ |
| `pick_tools`                 | 选择当前工具并设置参数（不进撤销历史）            |
| `use_tool`                   | 真正修改画布：笔画 / 区域擦除 / 洪水填充（进撤销历史） |
| `undo` / `redo`              | 撤销 / 重做 `use_tool` 动作          |
| `get_current_picture`        | 保存并查看压缩观察图（长边 ≤ 640）           |
| `get_canvas_info`            | 画布尺寸、工具状态、历史与笔画摘要              |
| `finish` / `itsHardToFinish` | 完成 / 放弃，自动导出 PNG 与过程 JSON      |

坐标与尺寸统一使用归一化整数 `0~1000`（左上角为原点），详细语义、路径表示  
（点列 / SVG path / 基本图形）与毛笔渲染公式见 `SKILL.md`。

## 测试

```bash
python -X utf8 tests/test_smoke.py             # 引擎与工具执行器
python -X utf8 tests/test_gui_smoke.py         # GUI（offscreen）
python -X utf8 tests/test_loop_smoke.py        # 应用内 Drawing Loop（Mock 模型）
python -X utf8 tests/test_skill.py             # Skill 分发与桥
python -X utf8 tests/test_cli_app.py           # CLI 无头端到端
python -X utf8 tests/test_skill_standalone.py  # 可移植性（任意 cwd 运行）
```

## 许可证

[MIT](./LICENSE)
