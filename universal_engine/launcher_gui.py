"""PRTS · MAA 通用智能自主控制台 Compatibility Entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    if getattr(sys, "frozen", False):
        import universal_engine
        __package__ = "universal_engine"
    else:
        _pkg_dir = Path(__file__).resolve().parent
        _pkg_parent = str(_pkg_dir.parent)
        if _pkg_parent not in sys.path:
            sys.path.insert(0, _pkg_parent)
        __package__ = _pkg_dir.name

from .gui import (
    API_PRESETS,
    BRIDGE_API_VERSION,
    EMULATOR_TYPE_OPTIONS,
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
    LauncherController,
    MaaLauncherApp,
    WebViewJsApi,
    _format_ra_mode_display,
    _format_rl_mode_display,
    _format_rl_theme_display,
    _opt_label_to_val,
    _opt_val_to_label,
    build_init_agent_prompt,
    build_repair_agent_prompt,
    classify_log_tag,
    collect_dashboard_snapshot,
    format_snapshot_payload,
    get_options_bundle,
    run_desktop_app,
)
from .master_scheduler import MaaEngine
from .utils import env_doctor


# ---------------------------------------------------------------------------
# Backward Compatibility Shims for Legacy Tests
# ---------------------------------------------------------------------------
class _MessageBoxShim:
    @staticmethod
    def showinfo(title: str, message: str, **kwargs: Any) -> bool:
        return True

    @staticmethod
    def showwarning(title: str, message: str, **kwargs: Any) -> bool:
        return True

    @staticmethod
    def showerror(title: str, message: str, **kwargs: Any) -> bool:
        return True

    @staticmethod
    def askyesno(title: str, message: str, **kwargs: Any) -> bool:
        return True

    @staticmethod
    def askokcancel(title: str, message: str, **kwargs: Any) -> bool:
        return True


messagebox = _MessageBoxShim()


class _CtkShim:
    @staticmethod
    def get_appearance_mode() -> str:
        return "Light"


ctk = _CtkShim()


class AgentConnectDialog:
    def __init__(
        self,
        master: Any = None,
        base_dir: Any = None,
        maa_dir: Any = None,
        latest_task_info: Any = None,
    ) -> None:
        self.master = master
        self.base_dir = base_dir

    def withdraw(self) -> None:
        pass

    def update_idletasks(self) -> None:
        pass

    def _on_close(self) -> None:
        pass


class SettingsDialog:
    def __init__(
        self,
        master: Any = None,
        engine: Any = None,
        on_saved: Any = None,
        on_reopen_wizard: Any = None,
    ) -> None:
        self.master = master
        self.engine = engine
        self.on_saved = on_saved
        self.on_reopen_wizard = on_reopen_wizard

    def withdraw(self) -> None:
        pass

    def update_idletasks(self) -> None:
        pass

    def _on_close(self) -> None:
        pass


class OnboardingWizard:
    def __init__(
        self,
        master: Any = None,
        engine: Any = None,
        on_completed: Any = None,
    ) -> None:
        self.master = master
        self.engine = engine
        self.on_completed = on_completed

    def withdraw(self) -> None:
        pass

    def update_idletasks(self) -> None:
        pass

    def _render_step(self, step: int) -> None:
        pass

    def _on_close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Main CLI & Desktop App Dispatcher
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PRTS · MAA 通用智能自主控制台")
    parser.add_argument("--wizard", action="store_true", help="强制打开新手向导")
    parser.add_argument(
        "--smoke-test",
        type=str,
        default=None,
        help="运行无头/自动化冒烟检查并输出结果到指定 JSON 文件",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    return run_desktop_app(
        open_wizard_force=args.wizard,
        smoke_test_output=args.smoke_test,
    )


if __name__ == "__main__":
    raise SystemExit(main())
