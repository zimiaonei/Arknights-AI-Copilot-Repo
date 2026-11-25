"""pywebview JS <-> Python Bridge Interface.

Exposes exactly and strictly the methods defined in `contract.FROZEN_BRIDGE_METHODS`.
All internal attributes are strictly prefixed with `_` so pywebview reflection
never exposes backend objects (MaaEngine, threading.Event, etc.) to the JavaScript runtime.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if getattr(sys, "frozen", False):
    from universal_engine.gui import contract
    from universal_engine.gui.controller import LauncherController
else:
    from . import contract
    from .controller import LauncherController


def _copy_to_system_clipboard(text: str) -> bool:
    """Zero-GUI-dependency clipboard writer supporting Windows, macOS and Linux."""
    raw_str = str(text or "")
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            # Define 64-bit safe signatures
            kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
            kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
            kernel32.GlobalUnlock.restype = wintypes.BOOL
            user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            user32.SetClipboardData.restype = wintypes.HANDLE
            user32.OpenClipboard.argtypes = [wintypes.HWND]
            user32.OpenClipboard.restype = wintypes.BOOL
            user32.EmptyClipboard.argtypes = []
            user32.EmptyClipboard.restype = wintypes.BOOL
            user32.CloseClipboard.argtypes = []
            user32.CloseClipboard.restype = wintypes.BOOL

            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002

            if user32.OpenClipboard(None):
                try:
                    user32.EmptyClipboard()
                    data = raw_str.encode("utf-16le") + b"\x00\x00"
                    h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                    if h_mem:
                        ptr = kernel32.GlobalLock(h_mem)
                        if ptr:
                            ctypes.memmove(ptr, data, len(data))
                            kernel32.GlobalUnlock(h_mem)
                            user32.SetClipboardData(CF_UNICODETEXT, h_mem)
                            return True
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass

        try:
            subprocess.run(["clip"], input=raw_str.encode("utf-16le"), check=True, capture_output=True)
            return True
        except Exception:
            return False

    if sys.platform == "darwin":
        try:
            subprocess.run(["pbcopy"], input=raw_str.encode("utf-8"), check=True, capture_output=True)
            return True
        except Exception:
            return False

    try:
        subprocess.run(
            ["xclip", "-selection", "clipboard"], input=raw_str.encode("utf-8"), check=True, capture_output=True
        )
        return True
    except Exception:
        return False


class WebViewJsApi:
    """JS API bound to `window.pywebview.api` in frontend Chromium webview."""

    def __init__(
        self,
        controller: LauncherController,
        window_getter: Optional[Callable[[], Any]] = None,
        smoke_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._controller: LauncherController = controller
        self._window_getter: Optional[Callable[[], Any]] = window_getter
        self._smoke_callback: Optional[Callable[[Dict[str, Any]], None]] = smoke_callback
        self._frontend_ready: bool = False

    # 1. get_initial_state
    def get_initial_state(self) -> Dict[str, Any]:
        self._frontend_ready = True
        return self._controller.get_initial_state()

    # 2. refresh_snapshot
    def refresh_snapshot(self, check_adb: bool = False) -> Dict[str, Any]:
        return self._controller.refresh_snapshot(check_adb=check_adb)

    # 3. start_daily
    def start_daily(self) -> Dict[str, Any]:
        return self._controller.start_daily()

    # 4. start_auto_growth
    def start_auto_growth(self) -> Dict[str, Any]:
        return self._controller.start_auto_growth()

    # 5. preview_growth_plan
    def preview_growth_plan(self) -> Dict[str, Any]:
        return self._controller.preview_growth_plan()

    # 6. start_annihilation
    def start_annihilation(self) -> Dict[str, Any]:
        return self._controller.start_annihilation()

    # 7. start_push
    def start_push(self, stages_raw: str = "") -> Dict[str, Any]:
        return self._controller.start_push(stages_raw=stages_raw)

    # 8. start_sync_box
    def start_sync_box(self) -> Dict[str, Any]:
        return self._controller.start_sync_box()

    # 9. set_roguelike_mode
    def set_roguelike_mode(self, mode_label: str) -> Dict[str, Any]:
        return self._controller.set_roguelike_mode(mode_label=mode_label)

    # 10. start_roguelike
    def start_roguelike(self, mode_label: str = "") -> Dict[str, Any]:
        return self._controller.start_roguelike(mode_label=mode_label)

    # 11. start_roguelike_post_settlement
    def start_roguelike_post_settlement(self) -> Dict[str, Any]:
        return self._controller.start_roguelike_post_settlement()

    # 12. toggle_reclamation
    def toggle_reclamation(self) -> Dict[str, Any]:
        return self._controller.toggle_reclamation()

    # 13. stop_current_task
    def stop_current_task(self) -> Dict[str, Any]:
        return self._controller.stop_current_task()

    # 14. set_server
    def set_server(self, server: str) -> Dict[str, Any]:
        return self._controller.set_server(server=server)

    # 15. set_post_action
    def set_post_action(self, post_action: str) -> Dict[str, Any]:
        return self._controller.set_post_action(post_action=post_action)

    # 16. probe_or_launch_emulator
    def probe_or_launch_emulator(self) -> Dict[str, Any]:
        return self._controller.probe_or_launch_emulator()

    # 17. check_maa_update
    def check_maa_update(self) -> Dict[str, Any]:
        return self._controller.check_maa_update()

    # 18. confirm_maa_update
    def confirm_maa_update(self, release_info: Dict[str, Any], local_ver: str, remote_ver: str) -> Dict[str, Any]:
        return self._controller.confirm_maa_update(release_info=release_info, local_ver=local_ver, remote_ver=remote_ver)

    # 19. cancel_maa_update
    def cancel_maa_update(self) -> Dict[str, Any]:
        return self._controller.cancel_maa_update()

    # 20. get_agent_prompts
    def get_agent_prompts(self) -> Dict[str, Any]:
        return self._controller.get_agent_prompts()

    # 21. copy_to_clipboard
    def copy_to_clipboard(self, text: str) -> Dict[str, Any]:
        ok = _copy_to_system_clipboard(text)
        return {"ok": ok}

    # 22. open_path
    def open_path(self, target: str) -> Dict[str, Any]:
        return self._controller.open_path(target=target)

    # 23. resume_with_rules
    def resume_with_rules(self) -> Dict[str, Any]:
        return self._controller.resume_with_rules()

    # 24. resume_from_tutorial
    def resume_from_tutorial(self) -> Dict[str, Any]:
        return self._controller.resume_from_tutorial()

    # 25. get_settings
    def get_settings(self) -> Dict[str, Any]:
        return self._controller.get_settings()

    # 26. save_settings
    def save_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._controller.save_settings(payload=payload)

    # 27. sync_exploration_from_maa_gui
    def sync_exploration_from_maa_gui(self, maa_path: str = "") -> Dict[str, Any]:
        return self._controller.sync_exploration_from_maa_gui(maa_path=maa_path)

    # 28. test_webhook
    def test_webhook(self, webhook_url: str) -> Dict[str, Any]:
        return self._controller.test_webhook(webhook_url=webhook_url)

    # 29. test_ai_api
    def test_ai_api(self, base_url: str, api_key: str, model_name: str) -> Dict[str, Any]:
        return self._controller.test_ai_api(base_url=base_url, api_key=api_key, model_name=model_name)

    # 30. browse_maa_folder
    def browse_maa_folder(self) -> Dict[str, Any]:
        if self._window_getter:
            win = self._window_getter()
            if win:
                import webview

                dialog_type = getattr(webview.FileDialog, "FOLDER", None) or getattr(webview, "FOLDER_DIALOG", 20)
                res = win.create_file_dialog(dialog_type)
                if res and len(res) > 0:
                    chosen = str(res[0])
                    bind_res = self._controller.wizard_bind_maa(chosen)
                    return {"ok": True, "folder": chosen, "path": chosen, "bind_res": bind_res}
        return {"ok": False, "cancelled": True}

    # 31. wizard_scan_maa
    def wizard_scan_maa(self) -> Dict[str, Any]:
        return self._controller.wizard_scan_maa()

    # 32. wizard_bind_maa
    def wizard_bind_maa(self, folder: str) -> Dict[str, Any]:
        return self._controller.wizard_bind_maa(folder=folder)

    # 33. wizard_start_download_maa
    def wizard_start_download_maa(self) -> Dict[str, Any]:
        return self._controller.wizard_start_download_maa()

    # 34. wizard_cancel_download_maa
    def wizard_cancel_download_maa(self) -> Dict[str, Any]:
        return self._controller.wizard_cancel_download_maa()

    # 35. wizard_run_diagnostics
    def wizard_run_diagnostics(self, emu_type: str, adb_address: str) -> Dict[str, Any]:
        return self._controller.wizard_run_diagnostics(emu_type=emu_type, adb_address=adb_address)

    # 36. wizard_launch_emulator
    def wizard_launch_emulator(self, emu_type: str, adb_address: str) -> Dict[str, Any]:
        return self._controller.wizard_launch_emulator(emu_type=emu_type, adb_address=adb_address)

    # 37. wizard_finish
    def wizard_finish(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._controller.wizard_finish(payload=payload)

    # 38. get_logs
    def get_logs(self) -> Dict[str, Any]:
        return self._controller.get_logs()

    # 39. clear_logs
    def clear_logs(self) -> Dict[str, Any]:
        return self._controller.clear_logs()

    # 40. report_smoke_metrics
    def report_smoke_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        if self._smoke_callback:
            try:
                self._smoke_callback(metrics)
            except Exception:
                pass
        return self._controller.report_smoke_metrics(metrics=metrics)
