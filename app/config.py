# -*- coding: utf-8 -*-
"""配置加载与默认配置生成。

规则:
- config.json 不存在时自动生成默认配置。
- api_key 支持环境变量覆盖: AI_DRAWING_API_KEY > OPENAI_API_KEY > config.json。
- api_key 绝不打印到日志、状态框或异常信息。
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "model": {
        "base_url": "",
        "api_key": "",
        "model_name": "",
        "timeout_seconds": 120,
    },
    "loop": {
        "max_rounds": 20,
        "keep_recent_canvas_images": 3,
        "observe_max_side": 640,
    },
    "canvas": {
        "default_width": 1920,
        "default_height": 1080,
        "background_color": "#ffffff",
    },
    "history": {
        "max_steps": 100,
    },
    "export": {
        "default_filename": "drawing.png",
    },
}

ENV_KEY_NAMES = ("AI_DRAWING_API_KEY", "OPENAI_API_KEY")


def _deep_merge(base: dict, override: dict) -> dict:
    """用 override 覆盖 base(仅覆盖已存在路径, 不引入新键)。"""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path = CONFIG_PATH) -> dict:
    """加载配置; 文件不存在时生成默认配置文件。缺失键自动补默认值。"""
    cfg = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError):
            cfg = {}
    merged = _deep_merge(DEFAULT_CONFIG, cfg if isinstance(cfg, dict) else {})
    if not path.exists():
        save_config(merged, path)
    return merged


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def resolve_api_key(cfg: dict) -> str:
    """api_key 优先级: 环境变量 AI_DRAWING_API_KEY > OPENAI_API_KEY > 配置文件。"""
    for name in ENV_KEY_NAMES:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    model = cfg.get("model", {})
    return str(model.get("api_key", "") or "")


def model_config_ready(cfg: dict) -> bool:
    """模型配置是否足以启动 Drawing Loop(不含 api_key 环境变量覆盖的情况)。"""
    model = cfg.get("model", {})
    return bool(
        str(model.get("base_url", "")).strip()
        and str(model.get("model_name", "")).strip()
        and resolve_api_key(cfg)
    )


def redact_config_for_display(cfg: dict) -> dict:
    """生成用于界面展示的配置摘要, api_key 以掩码显示。"""
    model = dict(cfg.get("model", {}))
    key = str(model.get("api_key", "") or "")
    model["api_key"] = ("<set>" if key else "<empty>")
    out = dict(cfg)
    out["model"] = model
    return out
