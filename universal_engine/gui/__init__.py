"""PRTS · MAA Modern Desktop GUI & Bridge Module."""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False) and __package__ in (None, ""):
    _pkg_dir = Path(__file__).resolve().parent
    _pkg_parent = str(_pkg_dir.parent)
    if _pkg_parent not in sys.path:
        sys.path.insert(0, _pkg_parent)
    __package__ = _pkg_dir.name

from . import contract
from .app import MaaLauncherApp, get_web_dir, run_desktop_app
from .bridge import WebViewJsApi
from .contract import (
    API_PRESETS,
    BRIDGE_API_VERSION,
    EMULATOR_TYPE_OPTIONS,
    FALLBACK_README_CONTENT,
    FROZEN_BRIDGE_METHODS,
    FROZEN_PUSH_EVENTS,
    LOG_CATEGORIES,
    RA_MODE_OPTIONS,
    RA_THEME_OPTIONS,
    RL_CARD_MODES,
    RL_CARD_MODE_AI,
    RL_CARD_MODE_PURE,
    RL_TARGET_OPTIONS,
    RL_THEME_OPTIONS,
    SKIP_TASK_OPTIONS,
    _format_ra_mode_display,
    _format_rl_mode_display,
    _format_rl_theme_display,
    _opt_label_to_val,
    _opt_val_to_label,
    build_init_agent_prompt,
    build_repair_agent_prompt,
    classify_log_tag,
    get_options_bundle,
    load_readme_content,
)
from .controller import LauncherController, collect_dashboard_snapshot, format_snapshot_payload


def main(argv: list[str] | None = None) -> int:
    from ..launcher_gui import main as _main

    return _main(argv)


__all__ = [
    "contract",
    "FALLBACK_README_CONTENT",
    "load_readme_content",
    "BRIDGE_API_VERSION",
    "FROZEN_BRIDGE_METHODS",
    "FROZEN_PUSH_EVENTS",
    "RL_CARD_MODES",
    "RL_CARD_MODE_PURE",
    "RL_CARD_MODE_AI",
    "RL_THEME_OPTIONS",
    "RL_TARGET_OPTIONS",
    "RA_THEME_OPTIONS",
    "RA_MODE_OPTIONS",
    "EMULATOR_TYPE_OPTIONS",
    "SKIP_TASK_OPTIONS",
    "API_PRESETS",
    "LOG_CATEGORIES",
    "classify_log_tag",
    "build_init_agent_prompt",
    "build_repair_agent_prompt",
    "get_options_bundle",
    "_opt_val_to_label",
    "_opt_label_to_val",
    "_format_rl_theme_display",
    "_format_rl_mode_display",
    "_format_ra_mode_display",
    "collect_dashboard_snapshot",
    "format_snapshot_payload",
    "LauncherController",
    "WebViewJsApi",
    "MaaLauncherApp",
    "run_desktop_app",
    "get_web_dir",
    "main",
]
