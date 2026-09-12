# -*- coding: utf-8 -*-
"""OpenAI tools JSON schema。

两种工具集:
- SKILL 模式(外部 harness): 含 get_current_picture。
- LOOP 模式(应用内 Drawing Loop): 不含 get_current_picture,
  因为每轮工具执行完成后应用会自动返回最新画布图片。
"""

_PICK_TOOLS = {
    "type": "function",
    "function": {
        "name": "pick_tools",
        "description": (
            "Select the active drawing tool and update its settings. "
            "This does not modify the canvas and does not enter undo history. "
            "pen/brush/eraser_area work with use_tool mode=path; "
            "eraser_stroke/bucket work with use_tool mode=point. "
            "bucket floods 4-connected from the click point, and the barrier "
            "used for the connectivity test is grown by 1px first, so the fill "
            "will not leak into the background through a thin anti-aliased "
            "outline (even a sub-pixel-wide one). "
            "Each tool remembers its own last settings; omitted settings are kept."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "enum": ["pen", "brush", "eraser_area", "eraser_stroke", "bucket"],
                    "description": "The tool to select.",
                },
                "settings": {
                    "type": "object",
                    "description": (
                        "Tool-specific settings. "
                        "pen: {size: 0-1000, color: #RRGGBB}. "
                        "brush: {size: 0-1000, color: #RRGGBB, opacity: 0.3-0.9}. "
                        "eraser_area: no settings. "
                        "eraser_stroke: {hit_radius: 0-1000}. "
                        "bucket: {color: #RRGGBB, tolerance: 0-255}; it floods "
                        "4-connected from the click point, and the barrier is "
                        "grown by 1px before the connectivity test so the fill "
                        "cannot leak into the background through a thin "
                        "anti-aliased outline (even a sub-pixel-wide one) - "
                        "always click inside the region you want to fill. "
                        "size and hit_radius are normalized to the canvas short side."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["tool"],
        },
    },
}

_USE_TOOL = {
    "type": "function",
    "function": {
        "name": "use_tool",
        "description": (
            "Use the currently selected tool to perform one atomic action. "
            "For pen/brush/eraser_area use mode=path and provide a path "
            "(points polyline / SVG path d / shape+params). "
            "For bucket/eraser_stroke use mode=point and provide x,y. "
            "Each successful canvas-modifying use_tool is an undoable action. "
            "WORK IN SMALL STEPS: draw one stroke or one part per call, then "
            "call get_current_picture to look at the result before continuing "
            "(the internal drawing loop auto-returns the latest canvas image "
            "each round). Do NOT try to draw the whole picture in one huge call "
            "with a very long path - it is hard to verify and a single mistake "
            "wastes the entire path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["path", "point"],
                    "description": "path for stroke or area-eraser actions; "
                                   "point for bucket fill or stroke-eraser actions.",
                },
                "path": {
                    "type": "object",
                    "description": (
                        "Path definition. Must contain exactly one of: "
                        "points (array of [x,y] with x,y in 0-1000, >=3 points are "
                        "smoothed with Catmull-Rom), "
                        "d (SVG path string supporting M L C Q A Z and h v s t), "
                        "or shape + params (circle/ellipse/rect/line/arc). "
                        "All coordinates are normalized integers 0-1000: "
                        "x scales with canvas width, y with height, "
                        "circle.r/arc.r and stroke size scale with the short side."
                    ),
                    "additionalProperties": True,
                },
                "closed": {
                    "type": "boolean",
                    "description": (
                        "Whether the path should close back to its start point. "
                        "pen/brush default false; circle/ellipse/rect are always "
                        "closed; line never closes; eraser_area always treats the "
                        "path as a closed region. closed only closes the outline, "
                        "it does not fill; use bucket to fill."
                    ),
                },
                "x": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000,
                    "description": "Normalized x coordinate for point mode.",
                },
                "y": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 1000,
                    "description": "Normalized y coordinate for point mode.",
                },
            },
            "required": ["mode"],
        },
    },
}

_UNDO = {
    "type": "function",
    "function": {
        "name": "undo",
        "description": "Undo the last canvas-modifying use_tool action. "
                       "pick_tools is never undone. Supports multiple steps.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_REDO = {
    "type": "function",
    "function": {
        "name": "redo",
        "description": "Redo the last undone use_tool action. "
                       "A new successful use_tool clears the redo stack.",
        "parameters": {"type": "object", "properties": {}},
    },
}

_GET_CANVAS_INFO = {
    "type": "function",
    "function": {
        "name": "get_canvas_info",
        "description": (
            "Get canvas size, current tool state, history state and stroke "
            "summaries. Use this to recall the current tool and settings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_geometry": {
                    "type": "boolean",
                    "description": "If true, include sampled geometry points "
                                   "of recent strokes. Default false.",
                },
            },
        },
    },
}

_GET_CURRENT_PICTURE = {
    "type": "function",
    "function": {
        "name": "get_current_picture",
        "description": (
            "Get the current canvas image as base64 PNG. This tool is intended "
            "for external skill-mode callers. Use it often: after every few "
            "strokes, look at the canvas and correct mistakes early instead of "
            "drawing the whole picture blind. The internal drawing loop must NOT "
            "use this tool because the latest canvas image is automatically "
            "provided after every round."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "full_resolution": {
                    "type": "boolean",
                    "description": "Framework-level escape hatch only. The "
                                   "model-facing flow always returns the "
                                   "compressed observation image (max side "
                                   "640) to save context; requesting true "
                                   "there has no effect.",
                },
            },
        },
    },
}

_FINISH = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": "Call this when the drawing task is complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Optional summary of what was accomplished.",
                },
            },
        },
    },
}

_ITS_HARD_TO_FINISH = {
    "type": "function",
    "function": {
        "name": "itsHardToFinish",
        "description": "Call this when the task is too hard to continue. "
                       "Provide a reason.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the task cannot be finished.",
                },
            },
        },
    },
}

# 应用内 Drawing Loop 工具集: 不含 get_current_picture
LOOP_TOOLS = [
    _PICK_TOOLS, _USE_TOOL, _UNDO, _REDO, _GET_CANVAS_INFO,
    _FINISH, _ITS_HARD_TO_FINISH,
]

# 外部 Skill 模式工具集: 含 get_current_picture
SKILL_TOOLS = LOOP_TOOLS + [_GET_CURRENT_PICTURE]

VALID_TOOL_NAMES_LOOP = [t["function"]["name"] for t in LOOP_TOOLS]
VALID_TOOL_NAMES_SKILL = [t["function"]["name"] for t in SKILL_TOOLS]


def get_tools_schema(mode: str = "skill") -> list:
    """按模式返回工具 schema。

    mode: "skill"(外部 Skill 模式, 含 get_current_picture)
          或 "loop"(应用内 Drawing Loop, 不含 get_current_picture)。
    """
    if mode == "loop":
        return [dict(t, function=dict(t["function"])) for t in LOOP_TOOLS]
    elif mode == "skill":
        return [dict(t, function=dict(t["function"])) for t in SKILL_TOOLS]
    raise ValueError(f"Unknown tool mode: {mode}. Expected 'skill' or 'loop'.")
