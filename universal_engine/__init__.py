"""MAA Universal Backend Engine package."""

import importlib
from typing import Any

from . import ai_strategist, auto_growth, maa_runner, roguelike_ai_copilot, stage_pilot

__all__ = [
    "MaaEngine",
    "ai_strategist",
    "auto_growth",
    "maa_runner",
    "master_scheduler",
    "roguelike_ai_copilot",
    "stage_pilot",
]


def __getattr__(name: str) -> Any:
    if name == "master_scheduler":
        mod = importlib.import_module(".master_scheduler", __name__)
        globals()["master_scheduler"] = mod
        return mod
    if name == "MaaEngine":
        mod = __getattr__("master_scheduler")
        globals()["MaaEngine"] = mod.MaaEngine
        return mod.MaaEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
