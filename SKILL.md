---
name: ai-drawing-executor
description: |
  AI 绘画执行器 Skill。模型通过 launch_app 启动画布应用与用户旁观窗口,
  然后用 function-call 风格的命令逐笔调用绘画工具(pick_tools / use_tool /
  undo / redo / get_current_picture / get_canvas_info), 调用 finish 或
  itsHardToFinish 结束时会自动把最终图片与
  绘画过程 JSON 导出到当前目录, 过程可用 replay.py 重放。
  作画节奏: 一笔一笔画, 每画几笔用 get_current_picture 看一眼再继续,
  ⛔ 严禁写完整脚本(或用 && / 循环串联命令)一整次作画, 每条命令必须
  单独作为一次工具调用。模型看到的画布图一律压缩到长边 ≤640(节约上下文),
  不传原图; 原分辨率图片只在 finish 导出时生成。本文件夹即分发包: 整个目录可拷贝到任何机器直接使用,
  不含任何写死的绝对路径(headless 作画只需 Python 3.9+ 与 numpy / Pillow)。
  Python 框架也可直接 from app.skill import create_skill_executor;
  任意语言框架通过根目录 bridge_stdio.py (JSON Lines over stdio) 调用。
---

# AI 绘画执行器

画布上的所有绘制、擦除、填充、撤销、重做都只能通过工具调用完成。
模型通过 `draw_cli.py` 与常驻绘画应用 `draw_app.py` 交互: **每执行一条命令
就是一次工具调用**, 应用持有画布并打开只读观察窗口让用户实时旁观。

## 1. Function-call 作画流程(主流程, 必按此顺序)

所有命令都可从任意工作目录执行; **会话与导出都基于当前工作目录**,
作画过程中不要中途切换目录。

### 1.1 启动: launch_app

```bash
python -X utf8 <本skill根目录>/draw_cli.py launch_app --width 1920 --height 1080
```

- `--width` / `--height`: 画布像素尺寸; `--bg #RRGGBB` 可选底色(默认白)。
- 启动常驻后台应用, 并同时打开一个**只读观察窗口**(左栏=当前工具,
  中栏=画布, 右栏=历史记录), 用户可实时看到模型怎么画
  (关掉观察窗口不影响作画)。
- `--observe-max-side`(默认 640): 模型可见观察图的最长边; 为节约模型上下文,
  传给模型的画布图片一律压缩到该尺寸, 不传原图。
- 命令返回 `{"ok": true, "pid": ..., "canvas": {...}, ...}`。
- 幂等: 同一目录已有运行中的会话时直接返回现有会话信息。

### 1.2 作画: 每条命令 = 一次工具调用

```bash
# 选工具并设置(工具: pen|brush|eraser_area|eraser_stroke|bucket)
python -X utf8 <根>/draw_cli.py pick_tools '{"tool": "pen", "settings": {"size": 20, "color": "#000000"}}'

# 画一笔(归一化坐标 0~1000)
python -X utf8 <根>/draw_cli.py use_tool '{"mode": "path", "path": {"points": [[100,100],[500,500],[900,100]]}}'

# 撤销/重做(只作用于 use_tool)
python -X utf8 <根>/draw_cli.py undo
python -X utf8 <根>/draw_cli.py redo

# 画布摘要
python -X utf8 <根>/draw_cli.py get_canvas_info
```

### 1.3 看画面: get_current_picture

```bash
python -X utf8 <根>/draw_cli.py get_current_picture
```

返回 `{"ok": true, "path": "<PNG 路径>", "hint": ...}` —— 观察图已保存为
PNG 文件(长边压缩到 ≤640, 节约模型上下文)。**必须立刻用 Read 工具查看该
PNG** 来观察当前画布(不要试图读 base64)。画歪了就 `undo` 重画, 不要将错就错。

注意: 模型拿到的永远是压缩观察图, **不会传原图**(即使请求 full_resolution
也会被忽略)。原始分辨率的图片只在 finish 导出时生成。

### 1.4 结束: 两个 finish 工具(自动导出)

```bash
python -X utf8 <根>/draw_cli.py finish '{"summary": "一幅简笔画猫"}'
python -X utf8 <根>/draw_cli.py itsHardToFinish '{"reason": "无法完成的原因"}'
```

任一结束工具执行成功时, 应用会自动把两样东西**导出到当前目录**:

- `drawing-<时间戳>.png` —— 最终画面的原始分辨率图片;
- `drawing-process-<时间戳>.json` —— 完整绘画过程(画布信息 + 每一次
  工具调用的参数与结果 + 时间戳与总时长), 之后可用 `replay.py` 重放:
  `python -X utf8 <根>/replay.py drawing-process-xxx.json`
  (加 `--verbose` 逐条打印每个动作, `--watch` 边重放边看,
  `--out x.png` 指定输出)。

结束结果里还会带一份时长汇总, 可直接告知用户:

```json
{"timing": {"started_at": "2026-09-13T10:18:46.630",
            "ended_at":   "2026-09-13T10:18:55.121",
            "total_ms": 8490, "total_text": "8.5 秒", "calls": 8}}
```

把导出的两个文件路径告知用户, 然后收尾关闭应用:

```bash
python -X utf8 <根>/draw_cli.py shutdown
```

### 1.5 时间戳与时长(每次调用都带)

**每一次工具调用的结果里都有三个时间字段**, 模型可据此掌握作画节奏
(例如发现自己已经画了很久, 该收尾了):

| 字段 | 含义 |
|---|---|
| `ts` | 这次调用的墙钟时间(ISO, 毫秒精度), 如 `2026-09-13T10:18:49.595` |
| `elapsed_ms` / `elapsed_text` | 距本次会话开始的用时(`2965` / `"3.0 秒"`) |
| `duration_ms` | 这次调用本身耗掉的时间(毫秒) |

观察窗口(左栏与状态栏)显示 `开始时间 / 用时`, 右栏历史记录里**每一行都以
`[10:18:49 +3.0s]` 开头**(墙钟时间 + 距第一次调用的用时); 重放时同样逐条
显示记录时间, 结束行之后另给一行总时长汇总。

重放报告(不带 `--watch` 时打印在终端)含两段时长:

```text
  记录时长 8.5 秒（10:18:46 → 10:18:55）      # 原会话画了多久
  重放用时 0.3 秒（纯执行 0.1 秒，平均每次 0.0 秒）  # 这次重放跑了多久
```

### 1.6 完整节奏示例

```text
launch_app --width 1920 --height 1080
→ pick_tools pen → use_tool 画轮廓(几笔) → get_current_picture(Read 看图)
→ 修形/加细节(几笔) → get_current_picture(Read 看图)
→ pick_tools bucket → use_tool 一块一块上色 → get_current_picture
→ finish '{"summary": "..."}'   # 自动导出 drawing-*.png + drawing-process-*.json
→ 把导出路径告诉用户 → shutdown
```

## 2. 作画节奏(最重要的一条, 硬性禁止)

**⛔ 禁止以"写完整脚本"的方式偷懒一整次作画。** 以下做法一律禁止:

- 不得把整幅画写成 shell / Python / PowerShell / batch 脚本(或临时脚本文件)
  一次性执行完成;
- 不得用 `&&`、`;`、`|`、循环、here-doc、`$(...)` 等手段把多条 `draw_cli.py`
  作画命令串在**一条** shell 命令里;
- 不得在一条 `use_tool` 里塞下整幅画的全部路径/形状(例如把人物轮廓、五官、
  全部上色都写进一个巨大的 `points` / SVG `d`)。

正确的做法只有一种: **每条 `draw_cli.py` 命令单独作为一次工具调用发出**,
一次 `use_tool` 只画一笔或一个部件, 每画 2~4 笔就 `get_current_picture`
看一眼再继续。违反本节即为偷懒, 即使结果图片看起来正常, 也视为本次作画失败
——因为跳过了逐笔观察与修正, 无法保证构图质量, 也破坏了本 skill 让用户
旁观作画过程的设计初衷。

理由:

- 单次调用越简单, 参数越不容易写错; 出错时 `undo` 一步就能干净回退。
- 每画几笔就对一次画面, 比例、位置、走向偏了能立刻发现并修正;
  闷头一次画完, 往往到最后一笔才发现整体构图是歪的, 只能大面积重画。
- 大段 SVG `d` 或上百个点的一次性路径很难自查, 一旦参数有误会整条路径失败,
  而且失败原因往往看不出来。

## 3. 目录即分发包

**本文件夹本身就是分发单元**, 同时扮演两个角色:

1. **skill 包** —— 模型经 `draw_cli.py` 用 function-call 方式作画;
2. **可手工运行的应用** —— 人类启动 GUI、点开始/停止、导出图片。

```text
<项目根>/
├── SKILL.md            本文档(接口文档)
├── draw_cli.py         ★ function-call 客户端(模型的入口)
├── draw_app.py         ★ 常驻绘画应用(引擎 + 双槽邮箱 + 三栏观察窗)
├── replay.py           ★ 重放绘画过程(读 drawing-process-*.json)
├── bridge_stdio.py     stdio 桥(任意语言 agent 框架的接入点)
├── selfcheck.py        自检脚本(到新机器先跑这个)
├── watch_viewer.py     独立只读观察窗口(可选, 供人类旁观帧文件)
├── requirements.txt    第三方依赖
├── run.bat             Windows 手工启动 GUI
├── app/                应用包
│   ├── skill.py        Skill 模式公共入口(create_skill_executor)
│   ├── core/           画布引擎 / 历史 / 渲染 / 路径解析 / 几何换算
│   ├── tools/          OpenAI tools JSON schema 与工具执行器
│   ├── ai/             Drawing Loop(应用内模型调用, 与本 Skill 无关)
│   └── gui/            只读画布 GUI(PySide6, 与本 Skill 无关)
├── tests/              测试
└── config.json         GUI 配置(首次运行自动生成, 不含密钥)
```

**运行要求**: Python 3.9+ 与两个第三方库(numpy, Pillow);
观察窗口优先用 PySide6, 没有则退回 tkinter, 都没有自动降级为纯 headless
(不影响作画, 用户可用任意看图工具打开 `get_current_picture` 存下的 PNG)。

```bash
python -m pip install -r requirements.txt      # numpy, Pillow
python -X utf8 selfcheck.py                    # 自检: 全绿即可开始作画
```

**会话数据**: 存于当前目录的 `.ai-draw-session/`: `mailbox/req.json` +
`resp.json`(双槽通信, 固定两个文件)、`session.jsonl`(调用日志)、
`observe-*.png`(观察图)、`app.json`(会话信息)。同一目录同时只有一个会话;
作画期间**不删除任何文件**(旧会话残留请求靠会话 token 自动失效),
`shutdown` 后日志保留备查, 整个目录可随时手工删除。

## 4. 工具集(8 个)

| 工具 | 作用 | 进历史 |
|---|---|---|
| `pick_tools` | 选择当前工具并更新其设置 | 否 |
| `use_tool` | 用当前工具执行一次原子动作 | 成功修改画布后进入 |
| `undo` / `redo` | 撤销/重做 use_tool 动作 | - |
| `get_current_picture` | 保存并返回当前画布观察图路径 | 否 |
| `get_canvas_info` | 画布尺寸/当前工具/历史/笔画摘要 | 否 |
| `finish` | 任务完成, 可带 `summary`; **自动导出图片+过程** | 否 |
| `itsHardToFinish` | 任务无法完成, 可带 `reason`; **同样自动导出** | 否 |

### pick_tools

```json
{"tool": "pen|brush|eraser_area|eraser_stroke|bucket",
 "settings": {"size": 20, "color": "#000000", "opacity": 0.7,
              "tolerance": 20, "hit_radius": 10}}
```

- `settings` 可选; 未提供字段保留该工具之前的设置(每个工具独立记忆)。
- pen: `size`(0~1000, 按短边)、`color`(#RRGGBB); 不透明。
- brush: `size`、`color`、`opacity`(0.3~0.9; `transparency` 同义兼容,
  冲突时以 `opacity` 为准); 中心到边缘高斯变淡。
- eraser_area: 无设置; 区域擦除为白色, 非删除整条笔画。
- eraser_stroke: `hit_radius`(0~1000, 默认 10); 删除点命中的最上层
  pen/brush 笔画, 未命中不视为错误。
  **顺序提示**: 若该笔画之后执行过 `bucket` / `eraser_area`(像素操作),
  删除会返回成功、笔画也不再计入, 但受历史"底图 + 顺序重放"实现影响,
  其可见像素可能仍留在画布上。要彻底去掉, 建议先删笔画再做像素填充。
- bucket: `color`、`tolerance`(0~255, 默认 20); 从点击点 4 连通洪水填充,
  逐通道容差比较。**填充不会从描边缝里漏到背景**: 判定连通时用的障碍图会比
  实际描边"胖" 1px, 所以哪怕笔画细到亚像素(归一笔宽 1~2)也不会漏色。
  代价: 点击点若落在描边上、或紧贴描边的 1px 之内, 会返回"无可填充区域"——
  把点挪到要填的区域内部即可。

### use_tool

```json
{"mode": "path|point", "path": {...}, "closed": false, "x": 500, "y": 500}
```

- 工具与模式映射: pen/brush/eraser_area → `path`;
  bucket/eraser_stroke → `point`(pen/brush 也允许 `point` 画单点)。
- `path` 只能含一种表示:
  - `points`: `[[x, y], ...]` 归一化整数, ≥3 点自动 Catmull-Rom 平滑;
  - `d`: SVG path 字符串, 支持 M L C Q A Z(及 h v s t), 大小写;
  - `shape`+`params`: circle{cx,cy,r} / ellipse{cx,cy,rx,ry} /
    rect{x,y,width,height} / line{x1,y1,x2,y2} /
    arc{cx,cy,r,start_angle,end_angle}(角度制 0~360)。
- `closed`: 末尾连回起点; circle/ellipse/rect 固定闭合, line 不闭合,
  eraser_area 强制闭合; closed 只闭合轮廓, 不代表填充(填充用 bucket)。
- pen/brush 成功返回 `stroke_id` + `action_id`; bucket/eraser_area 只有
  `action_id`; eraser_stroke 返回 `removed_stroke_id`。
- 无画布变化(白笔画白底、未命中笔画、无像素变化填充)不进历史。

### undo / redo

- 只作用于 use_tool 动作; 支持多步; 无动作时返回 `{"ok": true,
  "undone_action_id": null, "message": "Nothing to undo."}`。
- 新的 use_tool 成功后清空 redo 栈; stroke_id 在撤销/重做/删除/恢复中稳定。

### get_current_picture

返回观察图 PNG 的**文件路径**(保存在 `.ai-draw-session/observe-*.png`),
用 Read 工具查看该文件即可。观察图**长边恒压缩到 ≤640**(默认 640, 可用
launch_app 的 `--observe-max-side` 调整, 但模型侧不接受原图请求), 用来判断
构图、位置、
比例是否合适; 细到像素的偏差看不出来, 不必反复确认。

### finish / itsHardToFinish

两个结束工具, 成功时自动向当前目录导出 `drawing-*.png`(原始分辨率)与
`drawing-process-*.json`(完整工具调用过程, schema=`ai-draw-process/1`,
含每次调用的 `ts` / `elapsed_ms` / `duration_ms` 与会话总时长)。
导出路径在返回结果的 `exported.image` / `exported.process` 字段,
总时长汇总在 `timing` 字段(`total_text` 是给人看的, 如 `"1 分 23.4 秒"`)。

### 时间字段(所有工具通用)

每次调用的返回结果都附:

```json
{"ts": "2026-09-13T10:18:49.595", "elapsed_ms": 2965,
 "elapsed_text": "3.0 秒", "duration_ms": 52}
```

`ts` 是墙钟时间, `elapsed_ms` 是距本次会话开始的用时, `duration_ms` 是这次
调用本身的耗时。`status` 命令的响应里也有 `elapsed_ms` / `elapsed_text`,
可用来判断"已经画了多久", 从而决定是否该收尾。

## 5. 坐标与尺寸

- 所有坐标为归一化整数 0~1000: 左上 (0,0), 右下 (1000,1000)。
- 点坐标: x 按画布宽, y 按画布高换算。
- 横向尺寸(rect.width/ellipse.rx/SVG 横向半径)按宽, 纵向按高。
- `circle.r`/`arc.r`/`size`/`hit_radius` 按画布短边(视觉尺寸不被拉伸)。
- 越界坐标 clamp; 渲染允许出界, 只绘制画布内部分。

## 6. 错误返回

所有工具失败统一返回 `{"ok": false, "error": "<可读的说明>"}`, 不抛异常。
常见: 工具与模式不匹配、缺 path、缺 x/y、path 多种表示并存、points 非法、
SVG 无法解析、图形缺参数、颜色非法、opacity 越界。
CLI 层错误(会话未启动 / 应用退出 / 超时)同样返回 `{"ok": false, "error": ...}`
并带退出码 1 —— 先 `launch_app` 重开会话即可继续。

## 7. 观察作画过程

- **launch_app 自带观察窗口**(Qt 优先, tkinter 兜底, 都没有则 headless),
  三栏实时更新, 用户可旁观每一笔:
  - **左栏「当前工具」**: 当前工具的中文名与英文名、颜色 / 粗细 / 不透明度 /
    容差 / 命中半径等设置, 以及画布尺寸、已画步数、用时、已导出次数
    (状态栏另有实时的"用时 / 开始时间");
  - **中栏**: 只读画布实时画面;
  - **右栏「历史记录」**: 每一次工具调用映射成**自然语言**, 每行以
    `[墙钟时间 +距首次调用的用时]` 开头, 正文格式为
    `(颜色) 工具 动作 (路径/形状或坐标)` —— 路径只写**起点→终点**,
    不铺完整路径。例如:

    ```text
    [10:18:47 +1.1s] 选好工具：硬笔（颜色 #1f4e79，粗细 40）   ← 灰色提示行
    [10:18:49 +3.0s] 第 1 步：#1f4e79 硬笔画了 (150,700)→(750,700)（闭合）
    [10:18:51 +4.4s] 看了一眼当前画面
    [10:18:52 +5.5s] 选好工具：油漆桶（颜色 #ffd966，容差 20）
    [10:18:52 +6.2s] 第 2 步：#ffd966 油漆桶选择 (480,520) 填充
    [10:18:53 +7.2s] 撤销了上一步（action_000003）
    [10:18:54 +7.9s] 第 3 步：#c00000 软笔画了 (300,700)→(600,700)
    [10:18:54 +8.1s] 第 4 步：#c00000 软笔画了 圆形             ← 形状报名称
    [10:18:54 +8.3s] 笔画橡皮：这个位置没有命中任何笔画        ← 失败翻成人话
    [10:18:54 +8.5s] 选择工具 执行失败：Invalid tool. ...
    [10:18:55 +8.5s] 完成本次绘画：观察窗三栏（新文案）        ← 紫色结束行
    总时长 8.5 秒（10:18:46 → 10:18:55），共 8 次工具调用      ← 结束后的汇总行
    ```

    画布动作按步计数(深色), 查看/选择/撤销类操作记为提示行(灰色);
    shape 形状(circle/ellipse/rect/line/arc)显示中文名, SVG `d` 取首尾两点。
  - 关闭窗口不影响后台作画(只隐藏)。
- 另一种方式: 独立观察窗轮询帧文件 `watch_viewer.py <帧PNG>`(备选)。
- 重放: `replay.py <过程JSON> [--verbose] [--watch] [--out x.png]`,
  不需要常驻应用。
  - **忠实重放**: 按原顺序执行**每一次**调用 —— 失败调用照原样再跑一次,
    `undo` / `redo` 也按原位重放, 所以模型"画错又撤销"的过程能完整还原;
    只有 `get_current_picture`(纯观察)跳过, 且 `--verbose` 里仍会打印出来。
  - **时间戳**: 每条轨迹/历史行都带原始记录的时间戳(墙钟 + 相对用时);
    `--watch` 的左栏显示当前这一步的记录时间与重放已用时。
  - **时长汇总**: 无论哪种方式, 结束时都会给出"记录时长(原会话画了多久)"
    与"重放用时(这次跑了多久)", 以及纯执行耗时与平均每次调用耗时。
  - **一致性校验**: 每次调用都与过程 JSON 里记录的原始结果比对
    (ok / changed / action_id / undone_action_id / stroke_id /
    filled_pixels 等), 全部一致才输出 `[ok]` 并以 0 退出; 出现 `[!]`
    不一致则以 1 退出, 并逐条列出 `seq N tool: 字段 原始=… 重放=…`。

## 8. 框架直接接入(高级, 一般模型用不到)

### 8.1 Python agent 框架(直接 import)

```python
import sys
sys.path.insert(0, "<项目根>")
from app.skill import create_skill_executor

executor = create_skill_executor(width=1920, height=1080)
tools_json = executor.get_tools_schema("skill")   # OpenAI tools JSON
result = executor.call_tool("use_tool", {"mode": "path", "path": {
    "points": [[100, 100], [500, 500], [900, 100]]}})
```

### 8.2 任意语言 agent 框架(stdio 桥)

```bash
python -X utf8 <项目根>/bridge_stdio.py          # JSON Lines over stdio
python -X utf8 <项目根>/bridge_stdio.py --watch-file out/current.png  # 帧导出
```

协议与示例见 bridge_stdio.py 源码注释。每次工具调用的 `result` 同样带
`ts` / `elapsed_ms` / `duration_ms`; `ping` 返回 `started_at` / `elapsed_ms`,
`shutdown` 返回 `timing`(起止时间 + 总时长 + 累计调用次数), 便于在
一次桥会话结束时向用户汇报"这次画了多久"。

## 9. 手工运行(GUI, 与 Skill 模式无关)

```bash
run.bat                        # Windows: 自动探测解释器后启动
python -X utf8 -m app.main     # 或在项目根直接运行(需要 PySide6)
```

- 首次运行会在项目根生成 `config.json`(模型配置与画布尺寸)。
- API key 可用环境变量 `AI_DRAWING_API_KEY` 或 `OPENAI_API_KEY` 覆盖。
- `run.bat` 按 `AI_DRAWING_PYTHON` → `.python-path` → `.venv` → `python`
  → `py -3` 的顺序查找解释器, 不写死绝对路径。

## 10. 安全说明

本包不访问网络、不读取任何密钥(Skill 模式不调用模型)、不执行文件删除等
破坏性操作; 画布操作仅在内存与会话目录内进行。导出(finish)只写入
当前目录两个新文件, 不覆盖既有文件。`config.json` 与 `.python-path`
属于本机配置, 分发时无需携带(前者可能含密钥)。
