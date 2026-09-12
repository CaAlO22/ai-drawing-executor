# -*- coding: utf-8 -*-
"""Skill 模式公共入口(MISSION.md 第 16 节)。

Skill 模式只提供工具, 不管理模型上下文:
- 对外提供完整 OpenAI tools JSON(含 get_current_picture)。
- 对外提供可调用的 ToolExecutor。
- 外部 harness 自行决定何时调用哪个工具, 自行管理上下文。
- 应用内 Drawing Loop 与 GUI 不是 Skill 模式运行所必需的;
  本模块不 import 任何 GUI / 模型客户端代码。

Python 外部框架用法::

    from app.skill import create_skill_executor

    executor = create_skill_executor(1920, 1080)
    tools_json = executor.get_tools_schema("skill")   # 交给外部模型的工具定义
    result = executor.call_tool("pick_tools", {"tool": "pen",
                                               "settings": {"size": 20,
                                                            "color": "#000000"}})
    result = executor.call_tool("use_tool", {"mode": "path", "path": {
        "points": [[100, 100], [900, 900]]}})
    picture = executor.call_tool("get_current_picture", {})

非 Python 框架用法: 通过 skill/bridge_stdio.py 以 JSON Lines over stdio 调用,
见 skill/SKILL.md。
"""
from typing import Optional

from .core.canvas_engine import CanvasEngine  # noqa: F401  (re-export)
from .tools.executor import ToolExecutor
from .tools.schema import VALID_TOOL_NAMES_SKILL, get_tools_schema  # noqa: F401

__all__ = [
    "create_skill_executor",
    "ToolExecutor",
    "CanvasEngine",
    "get_tools_schema",
    "VALID_TOOL_NAMES_SKILL",
    "SKILL_TOOL_NAMES",
]

# 外部 Skill 模式工具集(含 get_current_picture, MISSION 8.1)
SKILL_TOOL_NAMES = list(VALID_TOOL_NAMES_SKILL)


def create_skill_executor(width: int = 1920, height: int = 1080,
                          background_color: str = "#ffffff",
                          max_history_steps: int = 100,
                          observe_max_side: int = 640) -> ToolExecutor:
    """创建一个供外部 agent 框架使用的 ToolExecutor(Skill 模式)。

    只创建画布引擎与工具执行器, 不创建 GUI、不创建模型客户端、
    不管理模型上下文。返回对象的 get_tools_schema("skill") 提供完整
    OpenAI tools JSON, call_tool(name, arguments) 为唯一调用入口。

    observe_max_side 为模型可见的观察图最长边(默认 640): 传给模型的
    画布图片一律压缩到该尺寸, 以节约模型上下文。
    """
    engine = CanvasEngine(width, height,
                          background_color=background_color,
                          max_history_steps=max_history_steps)
    return ToolExecutor(engine, observe_max_side=observe_max_side)
