"""Modern Light-Mode Desktop Application Launcher for PRTS · MAA.

Orchestrates pywebview window creation, static resource routing, background event draining,
smoke checking, and backward-compatible wrappers for headless testing.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if getattr(sys, "frozen", False):
    from universal_engine.gui import contract
    from universal_engine.gui.bridge import WebViewJsApi
    from universal_engine.gui.controller import LauncherController, collect_dashboard_snapshot
    from universal_engine.master_scheduler import MaaEngine
else:
    from . import contract
    from .bridge import WebViewJsApi
    from .controller import LauncherController, collect_dashboard_snapshot
    from ..master_scheduler import MaaEngine


def get_web_dir() -> Path:
    """Resolve directory holding frontend HTML/CSS/JS assets."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        p = Path(sys._MEIPASS) / "universal_engine" / "gui" / "web"
        if p.is_dir():
            return p
    return Path(__file__).resolve().parent / "web"


# ---------------------------------------------------------------------------
# Backward Compatibility Shim for Legacy Headless Tests
# ---------------------------------------------------------------------------
class _ShimWidget:
    def __init__(
        self,
        text: str = "",
        value: str = "",
        state: str = "normal",
        managed: bool = True,
        reqwidth: int = 800,
    ) -> None:
        self._text: str = text
        self._value: str = value
        self._state: str = state
        self._managed: bool = managed
        self._reqwidth: int = reqwidth
        self.master: Optional[Any] = None

    def cget(self, key: str) -> Any:
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return ""

    def configure(self, **kwargs: Any) -> None:
        if "text" in kwargs:
            self._text = str(kwargs["text"])
        if "state" in kwargs:
            self._state = str(kwargs["state"])

    def get(self) -> str:
        return self._value if self._value else self._text

    def set(self, val: str) -> None:
        self._value = val
        self._text = val

    def insert(self, idx: Any, text: str) -> None:
        self._text = text
        self._value = text

    def delete(self, *a: Any) -> None:
        self._text = ""
        self._value = ""

    def winfo_manager(self) -> str:
        return "grid" if self._managed else ""

    def winfo_exists(self) -> bool:
        return True

    def winfo_reqwidth(self) -> int:
        return self._reqwidth

    def grid(self, *a: Any, **kw: Any) -> None:
        self._managed = True

    def grid_forget(self) -> None:
        self._managed = False

    def pack(self, *a: Any, **kw: Any) -> None:
        self._managed = True

    def pack_forget(self) -> None:
        self._managed = False


class _ShimTextbox:
    def __init__(self) -> None:
        self._lines: List[Tuple[str, str]] = []

    def tag_names(self, pos: str = "") -> List[str]:
        if self._lines:
            return [self._lines[-1][1]]
        return ["normal"]


class _ShimLogBox:
    def __init__(self) -> None:
        self._textbox = _ShimTextbox()

    def _append(self, text: str) -> None:
        tag = contract.classify_log_tag(text)
        self._textbox._lines.append((text, tag))


class MaaLauncherApp:
    """Backward-compatible wrapper around LauncherController mimicking old CustomTkinter app."""

    def __init__(self, engine: Optional[MaaEngine] = None, open_wizard_force: bool = False, **kwargs: Any) -> None:
        self._controller = LauncherController(engine=engine, open_wizard_force=open_wizard_force)
        self.engine = self._controller.engine

        self._lbl_ai_mode = _ShimWidget(text="桌面 Agent 协同 (免Key)")
        self._status_pills_frame = _ShimWidget(reqwidth=800)
        self._lbl_ai_mode.master = self._status_pills_frame

        self._lbl_maa_ver = _ShimWidget(text="MAA 已就绪")
        self._lbl_adb_status = _ShimWidget(text="ADB 未连接")
        self._lbl_box_count = _ShimWidget(text="干员 Box: 未同步")
        self._lbl_sync_hint = _ShimWidget(text="待同步")
        self._lbl_growth_hint = _ShimWidget(text="推荐: 1-7")
        self._lbl_annih_hint = _ShimWidget(text="本周待检查")
        self._seg_server = _ShimWidget(value="官服")
        self._seg_post_action = _ShimWidget(value="无")
        self._lbl_quick_summary = _ShimWidget(text="刷图策略: AUTO | 自动解锁推图: 关")
        self._lbl_rl_theme_badge = _ShimWidget(text="主题：沉沦者的黑流树海 (BlackFlow)")
        self._opt_rl_mode = _ShimWidget(value=contract.RL_CARD_MODE_PURE)
        self._lbl_rl_sub_hint = _ShimWidget(text="")
        self._lbl_ra_mode_badge = _ShimWidget(text="模式：重启锚点 · RA1")
        self._lbl_ra_sub_hint = _ShimWidget(text="")
        self._btn_ra_toggle = _ShimWidget(text="启动生息演算", state="normal")
        self._btn_stop = _ShimWidget(text="停止任务", state="disabled")
        self._btn_check_maa_update = _ShimWidget(text="检查更新", state="normal")
        self._btn_check_maa_update.master = self._status_pills_frame

        self._agent_banner = _ShimWidget(managed=False)
        self._tutorial_banner = _ShimWidget(managed=False)
        self._tut_banner_title = _ShimWidget(text="")
        self._btn_tut_manual_continue = _ShimWidget(text="我已通关，立即继续")
        self._push_stage_entry = _ShimWidget(text="")
        self._log_box = _ShimLogBox()

    @property
    def _cancel_event(self) -> threading.Event:
        return self._controller._cancel_event

    @property
    def _manual_resume_event(self) -> threading.Event:
        return self._controller._manual_resume_event

    @property
    def _tutorial_resume_event(self) -> threading.Event:
        return self._controller._tutorial_resume_event

    @property
    def _task_running(self) -> bool:
        return self._controller._task_running

    @property
    def _active_task_kind(self) -> Optional[str]:
        return self._controller._active_task_kind

    @property
    def _ui_queue(self) -> queue.Queue[Tuple[str, Any]]:
        return self._controller._ui_queue

    @property
    def _snapshot(self) -> Dict[str, Any]:
        return self._controller._snapshot_raw

    @_snapshot.setter
    def _snapshot(self, val: Dict[str, Any]) -> None:
        self._controller._snapshot_raw = val

    def withdraw(self) -> None:
        pass

    def destroy(self) -> None:
        pass

    def update(self) -> None:
        self._controller.drain_events()

    def update_idletasks(self) -> None:
        self._controller.drain_events()

    def mainloop(self) -> None:
        pass

    def _show_agent_banner(self, p: Dict[str, Any]) -> None:
        self._agent_banner._managed = True
        self._controller._show_agent_banner(p)

    def _hide_agent_banner(self) -> None:
        self._agent_banner._managed = False
        self._controller._hide_agent_banner()

    def _show_tutorial_banner(self, p: Dict[str, Any]) -> None:
        self._tutorial_banner._managed = True
        self._tut_banner_title._text = str(p.get('message', ''))
        self._controller._show_tutorial_banner(p)

    def _hide_tutorial_banner(self) -> None:
        self._tutorial_banner._managed = False
        self._controller._hide_tutorial_banner()

    def _resume_from_tutorial(self) -> Dict[str, Any]:
        self._tutorial_banner._managed = False
        return self._controller.resume_from_tutorial()

    def _resume_with_rules(self) -> Dict[str, Any]:
        self._agent_banner._managed = False
        return self._controller.resume_with_rules()

    def _append_log(self, msg: str) -> Dict[str, Any]:
        self._log_box._append(msg)
        return self._controller._append_log(msg)

    def _set_running_state(self, running: bool, status_text: str = "", task_kind: Optional[str] = None) -> None:
        self._btn_stop._state = "normal" if running else "disabled"
        if running and task_kind == "reclamation":
            self._btn_ra_toggle._text = "停止生息演算"
            self._btn_ra_toggle._state = "normal"
        else:
            self._btn_ra_toggle._text = "启动生息演算"
            self._btn_ra_toggle._state = "normal" if not running else "disabled"
        self._controller._set_running_state(running, status_text, task_kind)

    def _apply_snapshot(self, snap: Dict[str, Any]) -> None:
        self._controller._apply_snapshot(snap)
        integ = snap.get("integrity", {})
        if integ.get("valid"):
            v = integ.get("maa_version") or integ.get("version_hint") or "已就绪"
            self._lbl_maa_ver._text = f"MAA {v}"
        adb = snap.get("adb_info")
        if adb:
            self._apply_adb_status(adb)
        rl_res = snap.get("roguelike_resolved") or {}
        if rl_res.get("theme"):
            self._lbl_rl_theme_badge._text = f"主题：{contract._format_rl_theme_display(rl_res.get('theme'))}"
        ra_res = snap.get("reclamation_resolved") or {}
        if ra_res:
            theme_disp, mode_disp = contract._format_ra_mode_display(
                ra_res.get("theme", "RelaunchAnchor"),
                (ra_res.get("params") or {}).get("mode", 16),
                str(ra_res.get("mode_label", "")),
            )
            self._lbl_ra_mode_badge._text = f"模式：{theme_disp} · {mode_disp}"

    def _apply_adb_status(self, adb: Optional[Dict[str, Any]]) -> None:
        self._controller._apply_adb_status(adb)
        if adb and adb.get("connected"):
            res_s = f"{adb.get('width')}x{adb.get('height')}" if adb.get("width") else ""
            self._lbl_adb_status._text = f"● {adb.get('adb_address')} ({res_s})" if res_s else f"● {adb.get('adb_address')}"
        else:
            self._lbl_adb_status._text = "● ADB 未连接"

    def _run_background_task(
        self,
        task_name: str,
        fn: Callable[[], Dict[str, Any]],
        on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
        task_kind: Optional[str] = None,
    ) -> None:
        return self._controller._run_background_task(task_name, fn, on_done, task_kind)

    def _start_daily_task(self) -> Dict[str, Any]:
        return self._controller.start_daily()

    def _start_auto_growth_fight(self) -> Dict[str, Any]:
        return self._controller.start_auto_growth()

    def _preview_growth_plan(self) -> Dict[str, Any]:
        return self._controller.preview_growth_plan()

    def _start_annihilation_task(self) -> Dict[str, Any]:
        return self._controller.start_annihilation()

    def _start_push_task(self) -> Dict[str, Any]:
        return self._controller.start_push(self._push_stage_entry.get())

    def _start_sync_task(self) -> Dict[str, Any]:
        return self._controller.start_sync_box()

    def _on_rl_mode_dropdown_changed(self, val: str) -> Dict[str, Any]:
        self._opt_rl_mode.set(val)
        return self._controller.set_roguelike_mode(val)

    def _start_roguelike_task(self) -> Dict[str, Any]:
        return self._controller.start_roguelike(self._opt_rl_mode.get())

    def _start_roguelike_post_settlement_task(self) -> Dict[str, Any]:
        return self._controller.start_roguelike_post_settlement()

    def _start_reclamation_task(self) -> Dict[str, Any]:
        self._set_running_state(True, "运行生息演算", task_kind="reclamation")
        return self._controller._run_background_task(
            "生息演算自动刷取",
            lambda: self.engine.run_reclamation(
                log_callback=self._controller._cb_log,
                progress_callback=self._controller._cb_progress,
                cancel_event=self._controller._cancel_event,
            ),
            on_done=self._controller._log_reclamation_report,
            task_kind="reclamation",
        )

    def _on_ra_toggle_clicked(self) -> Dict[str, Any]:
        return self._controller.toggle_reclamation()

    def _stop_current_task(self) -> Dict[str, Any]:
        return self._controller.stop_current_task()

    def _on_check_maa_update(self) -> Dict[str, Any]:
        return self._controller.check_maa_update()

    def _start_maa_core_update(self, rel_info: Dict[str, Any], local_ver: str, remote_ver: str) -> Dict[str, Any]:
        return self._controller.confirm_maa_update(rel_info, local_ver, remote_ver)

    def _refresh_snapshot_async(self, check_adb: bool = False) -> None:
        self._controller._refresh_snapshot_async(check_adb=check_adb)

    def _drain_ui_queue(self) -> List[Dict[str, Any]]:
        return self._controller.drain_events()


# ---------------------------------------------------------------------------
# Main Desktop Window Runner via pywebview
# ---------------------------------------------------------------------------
def run_desktop_app(open_wizard_force: bool = False, smoke_test_output: Optional[str] = None) -> int:
    """Launch the PRTS pywebview EdgeChromium desktop application."""
    import webview

    controller = LauncherController(open_wizard_force=open_wizard_force)
    window_holder: List[Optional[Any]] = [None]
    smoke_res_holder: Dict[str, Any] = {}
    smoke_exit_code = [0]

    def _smoke_cb(metrics: Dict[str, Any]) -> None:
        smoke_res_holder.update(metrics)

    api = WebViewJsApi(controller, window_getter=lambda: window_holder[0], smoke_callback=_smoke_cb)
    web_dir = get_web_dir()

    if smoke_test_output:
        storage_path = Path(tempfile.gettempdir()) / "prts_smoke_webview2"
    else:
        storage_path = controller.engine.base_dir / "cache" / "webview2"
    storage_path.mkdir(parents=True, exist_ok=True)

    window = webview.create_window(
        title="PRTS · MAA 通用智能自主控制台",
        url=str((web_dir / "index.html").resolve()),
        js_api=api,
        width=1120,
        height=740,
        min_size=(920, 600),
        resizable=True,
        background_color="#E8EEF7",
    )
    window_holder[0] = window

    stop_threads = threading.Event()

    def _dispatcher_loop() -> None:
        while not stop_threads.is_set():
            time.sleep(0.08)
            if not getattr(api, "_frontend_ready", False):
                continue
            events = controller.drain_events()
            if not events:
                continue
            try:
                js_code = f"window.__prtsPushBatch && window.__prtsPushBatch({json.dumps(events, ensure_ascii=False)});"
                window.evaluate_js(js_code)
            except Exception:
                pass

    dispatcher_thread = threading.Thread(target=_dispatcher_loop, daemon=True)
    dispatcher_thread.start()

    if smoke_test_output:

        def _smoke_runner() -> None:
            t0 = time.monotonic()
            while not getattr(api, "_frontend_ready", False) and time.monotonic() - t0 < 15.0:
                time.sleep(0.1)

            if not getattr(api, "_frontend_ready", False):
                smoke_exit_code[0] = 1
                smoke_payload = {
                    "pywebviewready": False,
                    "api_handshake_ok": False,
                    "error": "Timeout waiting for frontend pywebviewready handshake after 15.0s",
                    "resize_tested": False,
                    "pages_rendered": [],
                    "overflow_errors": 0,
                    "emoji_count": 0,
                }
                out_p = Path(smoke_test_output)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(json.dumps(smoke_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                time.sleep(0.3)
                window.destroy()
                return

            def _capture_screenshot(name: str) -> None:
                shot_dir = os.environ.get("PRTS_SMOKE_SCREENSHOT_DIR")
                if not shot_dir:
                    return
                sd = Path(shot_dir)
                sd.mkdir(parents=True, exist_ok=True)
                out_file = sd / f"{name}.png"

                captured = False
                try:
                    from webview.platforms import edgechromium
                    from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
                    from System.IO import FileStream, FileMode
                    from System import Action

                    native_form = getattr(window, "native", None)
                    browser = getattr(native_form, "browser", None)
                    wv = getattr(browser, "webview", None) or getattr(native_form, "webview", None)

                    if wv and hasattr(wv, "Invoke"):
                        done_evt = threading.Event()

                        def _do_capture():
                            try:
                                core = getattr(wv, "CoreWebView2", None)
                                if core is None:
                                    done_evt.set()
                                    return
                                stream = FileStream(str(out_file), FileMode.Create)
                                task = core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream)

                                def _on_done(t):
                                    try:
                                        stream.Flush()
                                        stream.Close()
                                        stream.Dispose()
                                    except Exception:
                                        pass
                                    done_evt.set()

                                task.ContinueWith(Action[edgechromium.Task](_on_done))
                            except Exception:
                                done_evt.set()

                        wv.Invoke(Action(_do_capture))
                        if done_evt.wait(timeout=5.0) and out_file.is_file() and out_file.stat().st_size > 0:
                            captured = True
                except Exception:
                    pass

                if not captured:
                    try:
                        from PIL import ImageGrab

                        wx = getattr(window, "x", None)
                        wy = getattr(window, "y", None)
                        ww = getattr(window, "width", None)
                        wh = getattr(window, "height", None)
                        if wx is not None and wy is not None and ww and wh:
                            bbox = (int(wx), int(wy), int(wx + ww), int(wy + wh))
                            im = ImageGrab.grab(bbox=bbox)
                        else:
                            im = ImageGrab.grab()
                        im.save(str(out_file))
                    except Exception:
                        pass

            time.sleep(0.6)  # Wait for initial render

            # Record whether cold-start wizard auto-opened, then dismiss for unobstructed UI checks
            wizard_auto_opened = False
            try:
                wizard_auto_opened = bool(
                    window.evaluate_js(
                        "Boolean(document.getElementById('modal-wizard') && !document.getElementById('modal-wizard').classList.contains('hidden'))"
                    )
                )
                if wizard_auto_opened:
                    window.evaluate_js(
                        "var mw = document.getElementById('modal-wizard'); if (mw) mw.classList.add('hidden');"
                    )
                    time.sleep(0.1)
            except Exception:
                pass

            # 1. Test 1120x740
            res_1120: Any = None
            try:
                res_1120 = window.evaluate_js(
                    "window.__prtsRunSmokeCheck ? window.__prtsRunSmokeCheck() : {ok: false, error: 'no smoke function'}"
                )
                _capture_screenshot("smoke_1120x740")
            except Exception as exc:
                res_1120 = {"ok": False, "error": str(exc)}

            # 2. Test 960x640
            res_960: Any = None
            try:
                window.resize(960, 640)
                time.sleep(0.3)
                res_960 = window.evaluate_js(
                    "window.__prtsRunSmokeCheck ? window.__prtsRunSmokeCheck() : {ok: false, error: 'no smoke function'}"
                )
                _capture_screenshot("smoke_960x640")
            except Exception as exc:
                res_960 = {"ok": False, "error": str(exc)}

            # 3. Test 920x600
            res_920: Any = None
            try:
                window.resize(920, 600)
                time.sleep(0.3)
                res_920 = window.evaluate_js(
                    "window.__prtsRunSmokeCheck ? window.__prtsRunSmokeCheck() : {ok: false, error: 'no smoke function'}"
                )
                _capture_screenshot("smoke_920x600")
            except Exception as exc:
                res_920 = {"ok": False, "error": str(exc)}

            # 4. Test 1280x760
            res_1280: Any = None
            try:
                window.resize(1280, 760)
                time.sleep(0.3)
                res_1280 = window.evaluate_js(
                    "window.__prtsRunSmokeCheck ? window.__prtsRunSmokeCheck() : {ok: false, error: 'no smoke function'}"
                )
                _capture_screenshot("smoke_1280x760")
            except Exception as exc:
                res_1280 = {"ok": False, "error": str(exc)}

            # Capture terminal page & sample rAF borderRadius transition (zero config mutation)
            raf_radius_sample: Any = None
            try:
                window.evaluate_js(
                    "var nav = document.querySelector('[data-page=\"page-terminal\"]') || document.getElementById('btn-jump-terminal');"
                    "if (nav) nav.click(); else if (window.switchPage) window.switchPage('page-terminal');"
                )
                time.sleep(0.15)
                _capture_screenshot("smoke_terminal")
                window.evaluate_js("window.__prtsStartRadiusTransitionSample && window.__prtsStartRadiusTransitionSample();")
                t_raf = time.monotonic()
                while time.monotonic() - t_raf < 1.5:
                    raf_res = window.evaluate_js("window.__prtsRadiusSampleResult")
                    if raf_res is not None:
                        raf_radius_sample = raf_res
                        break
                    time.sleep(0.05)
                window.evaluate_js(
                    "var nav = document.querySelector('[data-page=\"page-overview\"]');"
                    "if (nav) nav.click(); else if (window.switchPage) window.switchPage('page-overview');"
                )
                time.sleep(0.15)
            except Exception as exc:
                raf_radius_sample = {"ok": False, "error": str(exc)}

            # 4a. Dry-run UI check (non-destructive: always reverts toggle to OFF)
            dry_run_ui_check: Any = None
            try:
                _dr_set_js = (
                    "(function(v){var sw=document.getElementById('set-sw-dry-run');"
                    "if(!sw) return false; sw.checked=v;"
                    "sw.dispatchEvent(new Event('change',{bubbles:true})); return true;})(%s)"
                )
                _dr_pill_js = (
                    "(function(){var p=document.getElementById('top-pill-dry-run');"
                    "if(!p) return {exists:false,hidden:true,visible:false};"
                    "return {exists:true,hidden:p.classList.contains('hidden'),"
                    "visible:p.offsetParent!==null};})()"
                )
                _dr_overflow_js = (
                    "(function(){var tb=document.querySelector('.top-bar');var de=document.documentElement;"
                    "return {top_bar_ok: !tb || tb.scrollWidth <= tb.clientWidth + 2,"
                    "doc_ok: de.scrollWidth <= de.clientWidth + 2};})()"
                )
                dr_errors: List[str] = []
                dr_overflow: Dict[str, Any] = {}
                dr_on_pill: Any = None
                dr_off_pill: Any = None
                try:
                    if not window.evaluate_js(_dr_set_js % "true"):
                        dr_errors.append("missing #set-sw-dry-run")
                    time.sleep(0.15)
                    dr_on_pill = window.evaluate_js(_dr_pill_js)
                    if not (isinstance(dr_on_pill, dict) and dr_on_pill.get("exists")
                            and not dr_on_pill.get("hidden") and dr_on_pill.get("visible")):
                        dr_errors.append("pill not visible when dry-run ON")
                    for _w, _h in ((960, 640), (920, 600)):
                        window.resize(_w, _h)
                        time.sleep(0.3)
                        ov = window.evaluate_js(_dr_overflow_js)
                        dr_overflow[f"{_w}x{_h}"] = ov
                        if not (isinstance(ov, dict) and ov.get("top_bar_ok") and ov.get("doc_ok")):
                            dr_errors.append(f"horizontal overflow at {_w}x{_h}")
                finally:
                    window.evaluate_js(_dr_set_js % "false")
                    time.sleep(0.15)
                    dr_off_pill = window.evaluate_js(_dr_pill_js)
                    try:
                        window.resize(1280, 760)
                        time.sleep(0.2)
                    except Exception:
                        pass
                if not (isinstance(dr_off_pill, dict) and dr_off_pill.get("hidden")):
                    dr_errors.append("pill not hidden after dry-run OFF")
                dry_run_ui_check = {
                    "ok": not dr_errors,
                    "errors": dr_errors,
                    "pill_on": dr_on_pill,
                    "pill_off": dr_off_pill,
                    "overflow": dr_overflow,
                }
            except Exception as exc:
                dry_run_ui_check = {"ok": False, "error": str(exc)}

            # 4b. Check all 5 Settings Tabs (DOM visibility, height > 100, controls >= 2, zero overflow)
            settings_tabs_check: Any = None
            try:
                settings_tabs_check = window.evaluate_js(
                    "window.__prtsRunSettingsTabsCheck ? window.__prtsRunSettingsTabsCheck() : {ok: false, error: 'missing check'}"
                )
                if os.environ.get("PRTS_SMOKE_SCREENSHOT_DIR"):
                    window.evaluate_js(
                        "var b = document.getElementById('btn-open-settings-modal'); if (b) b.click();"
                    )
                    time.sleep(0.25)
                    for tab_idx in range(5):
                        window.evaluate_js(
                            f"var btns = document.querySelectorAll('#settings-tabs-bar .modal-tab-btn');"
                            f"if (btns[{tab_idx}]) btns[{tab_idx}].click();"
                        )
                        time.sleep(0.12)
                        _capture_screenshot(f"smoke_settings_tab{tab_idx + 1}")
                    window.evaluate_js(
                        "var c = document.querySelector('#modal-settings [data-close=\"modal-settings\"]'); if (c) c.click();"
                    )
                    time.sleep(0.12)
            except Exception as exc:
                settings_tabs_check = {"ok": False, "error": str(exc)}

            # 4c. Check 4 Material You Palettes, Light/Dark themes & motion/radius checks
            theme_motion_check: Any = None
            try:
                theme_motion_check = window.evaluate_js(
                    "window.__prtsRunThemeAndMotionCheck ? window.__prtsRunThemeAndMotionCheck() : {ok: false, error: 'missing check'}"
                )
                if os.environ.get("PRTS_SMOKE_SCREENSHOT_DIR"):
                    window.evaluate_js("window.__prtsSetTheme && window.__prtsSetTheme('sky', 'dark');")
                    time.sleep(0.15)
                    _capture_screenshot("smoke_dark_mode")
                    window.evaluate_js("window.__prtsSetTheme && window.__prtsSetTheme('sage', 'light');")
                    time.sleep(0.15)
                    _capture_screenshot("smoke_sage_palette")
                    window.evaluate_js("window.__prtsSetTheme && window.__prtsSetTheme('sky', 'light');")
                    time.sleep(0.1)
            except Exception as exc:
                theme_motion_check = {"ok": False, "error": str(exc)}

            # 4d. Check Toolbox Page (Master-Detail, 5 representative tools, zero overflow, visible start button)
            toolbox_page_check: Any = None
            try:
                window.resize(960, 640)
                time.sleep(0.3)
                window.evaluate_js(
                    "var nav = document.querySelector('[data-page=\"page-toolbox\"]');"
                    "if (nav) nav.click(); else if (window.switchPage) window.switchPage('page-toolbox');"
                )
                t_wait_tb = time.monotonic()
                while time.monotonic() - t_wait_tb < 2.0:
                    rendered = window.evaluate_js(
                        "Boolean(document.querySelector('#toolbox-nav-list [data-tool-id=\"secret_front\"]'))"
                    )
                    if bool(rendered):
                        break
                    time.sleep(0.05)

                tools_to_check = [
                    "secret_front",
                    "stronghold_protocol",
                    "reclamation",
                    "event_shop",
                    "auto_raise_potential",
                ]

                tool_results = []
                for tool_id in tools_to_check:
                    tool_res = window.evaluate_js(
                        f"""
                        (function() {{
                            var selector = '[data-tool-id="{tool_id}"], #tool-item-{tool_id}, .toolbox-item[data-id="{tool_id}"]';
                            var item = document.querySelector(selector);
                            if (item) {{
                                item.click();
                            }}
                            var detail = document.getElementById('toolbox-detail-card') || document.querySelector('.toolbox-detail');
                            var startBtn = document.getElementById('btn-start-toolbox') || document.querySelector('.btn-start-toolbox');
                            var secBtn = document.getElementById('btn-toolbox-secondary') || document.querySelector('.btn-toolbox-secondary');
                            var h = detail ? detail.offsetHeight : 0;
                            var sw = detail ? detail.scrollWidth : 0;
                            var cw = detail ? detail.clientWidth : 0;
                            var btnVisible = Boolean(startBtn && (startBtn.offsetParent !== null || window.getComputedStyle(startBtn).display !== 'none'));
                            var secVisible = Boolean(secBtn && secBtn.offsetParent !== null && window.getComputedStyle(secBtn).display !== 'none');
                            var secBtnOk = ("{tool_id}" === "reclamation") ? secVisible : (!secBtn || secBtn.offsetParent === null || window.getComputedStyle(secBtn).display === 'none');
                            return {{
                                tool_id: "{tool_id}",
                                item_found: Boolean(item),
                                detail_height: h,
                                scroll_width: sw,
                                client_width: cw,
                                no_overflow: sw <= (cw + 2),
                                start_btn_visible: btnVisible,
                                sec_btn_visible: secVisible,
                                sec_btn_ok: secBtnOk,
                                ok: Boolean(item && detail && h > 100 && (sw <= cw + 2) && btnVisible && secBtnOk)
                            }};
                        }})()
                        """
                    )
                    time.sleep(0.1)
                    tool_results.append(tool_res)
                    if tool_id == "stronghold_protocol":
                        _capture_screenshot("smoke_toolbox_stronghold")

                _capture_screenshot("smoke_toolbox_960x640")

                # Check category labels: non-empty and no 'undefined'
                category_labels_check = window.evaluate_js(
                    """
                    (function() {
                        var labels = document.querySelectorAll('#page-toolbox .toolbox-category-label');
                        if (!labels || labels.length === 0) return { ok: false, count: 0, reason: 'no category labels found' };
                        for (var i = 0; i < labels.length; i++) {
                            var txt = (labels[i].textContent || '').trim();
                            if (!txt || txt.indexOf('undefined') !== -1) {
                                return { ok: false, count: labels.length, bad_text: txt };
                            }
                        }
                        return { ok: true, count: labels.length };
                    })()
                    """
                )
                category_labels_ok = isinstance(category_labels_check, dict) and bool(category_labels_check.get("ok"))

                # Test 920x600 for zero horizontal overflow
                window.resize(920, 600)
                time.sleep(0.25)
                res_920_tb = window.evaluate_js(
                    """
                    (function() {
                        var detail = document.getElementById('toolbox-detail-card') || document.querySelector('.toolbox-detail');
                        var h = detail ? detail.offsetHeight : 0;
                        var sw = detail ? detail.scrollWidth : 0;
                        var cw = detail ? detail.clientWidth : 0;
                        return {
                            detail_height: h,
                            scroll_width: sw,
                            client_width: cw,
                            no_overflow: sw <= (cw + 2),
                            ok: Boolean(detail && h > 100 && (sw <= cw + 2))
                        };
                    })()
                    """
                )

                tb_console_errors = []
                try:
                    ce_tb = window.evaluate_js("window.__prtsConsoleErrors ? window.__prtsConsoleErrors.slice() : []")
                    if isinstance(ce_tb, list):
                        tb_console_errors = [str(x) for x in ce_tb]
                except Exception:
                    pass

                all_tools_ok = all(isinstance(r, dict) and bool(r.get("ok")) for r in tool_results)
                res_920_ok = isinstance(res_920_tb, dict) and bool(res_920_tb.get("ok"))
                no_console_err = len(tb_console_errors) == 0

                toolbox_page_check = {
                    "ok": bool(all_tools_ok and res_920_ok and no_console_err and category_labels_ok),
                    "tools_checked": tool_results,
                    "res_920": res_920_tb,
                    "category_labels_check": category_labels_check,
                    "category_labels_ok": category_labels_ok,
                    "console_error_count": len(tb_console_errors),
                }

                # Switch back to page-overview
                window.evaluate_js(
                    "var nav = document.querySelector('[data-page=\"page-overview\"]');"
                    "if (nav) nav.click(); else if (window.switchPage) window.switchPage('page-overview');"
                )
                time.sleep(0.15)
            except Exception as exc:
                toolbox_page_check = {"ok": False, "error": str(exc)}

            # 5. Check Agent modal prompts and README Tab 2
            agent_check: Any = None
            try:
                window.evaluate_js(
                    "var btn = document.getElementById('btn-open-agent-modal');"
                    "if (btn) btn.click(); else if (window.openModal) window.openModal('modal-agent');"
                )
                # Wait/poll up to 2.0s in Python until getAgentPrompts Promise finishes loading
                t_poll_agent = time.monotonic()
                while time.monotonic() - t_poll_agent < 2.0:
                    poll_agent = window.evaluate_js(
                        """
                        (function() {
                            var p1 = document.getElementById('text-prompt-init');
                            var readme = document.getElementById('text-readme-content');
                            var l1 = p1 ? (p1.value || '').length : 0;
                            var l2 = readme ? (readme.textContent || readme.value || '').length : 0;
                            return (l1 > 0 && l2 > 0);
                        })()
                        """
                    )
                    if bool(poll_agent):
                        break
                    time.sleep(0.08)

                # Switch to Tab 2 (README)
                window.evaluate_js(
                    "var tab2Btn = document.querySelector('[data-tab=\"tab-agent-readme\"]');"
                    "if (tab2Btn) tab2Btn.click();"
                )
                time.sleep(0.15)

                # Capture screenshot WHILE the modal is open on Tab 2!
                _capture_screenshot("smoke_agent_modal")

                agent_check = window.evaluate_js(
                    """
                    (function() {
                        var p1 = document.getElementById('text-prompt-init');
                        var p2 = document.getElementById('text-prompt-repair');
                        var readme = document.getElementById('text-readme-content');
                        var footer = document.querySelector('#modal-agent .modal-footer');
                        var fRect = footer ? footer.getBoundingClientRect() : null;
                        var rRect = readme ? readme.getBoundingClientRect() : null;
                        var footerPinned = Boolean(fRect && rRect && (fRect.top >= rRect.bottom - 4));
                        var res = {
                            init_prompt_len: (p1 ? p1.value.length : 0),
                            repair_prompt_len: (p2 ? p2.value.length : 0),
                            readme_len: (readme ? (readme.textContent || readme.value || '').length : 0),
                            footer_visible: Boolean(footer && footer.offsetParent !== null),
                            footer_pinned: footerPinned
                        };
                        var closeBtn = document.querySelector('#modal-agent [data-close=\"modal-agent\"]');
                        if (closeBtn) closeBtn.click();
                        else if (window.closeModal) window.closeModal('modal-agent');
                        return res;
                    })()
                    """
                )
                time.sleep(0.15)
            except Exception as exc:
                agent_check = {"ok": False, "error": str(exc)}

            # 6. Optional UI Settings Roundtrip
            roundtrip_res: Any = None
            if os.environ.get("PRTS_SMOKE_ROUNDTRIP") == "1":
                try:
                    # Open Settings modal using the real button ID #btn-open-settings-modal
                    window.evaluate_js(
                        "var btn = document.getElementById('btn-open-settings-modal');"
                        "if (btn) btn.click(); else if (window.openModal) window.openModal('modal-settings');"
                    )
                    time.sleep(0.4)

                    window.evaluate_js(
                        """
                        (function() {
                            var manualBtn = document.querySelector('#set-fight-mode [data-value=\"MANUAL\"]');
                            if (manualBtn) manualBtn.click();

                            var sw = document.getElementById('set-sw-auto-push');
                            if (sw) {
                                sw.checked = !sw.checked;
                                sw.dispatchEvent(new Event('change', { bubbles: true }));
                            }

                            var saveBtn = document.getElementById('btn-save-settings');
                            if (saveBtn) saveBtn.click();
                        })()
                        """
                    )
                    time.sleep(0.6)

                    # Reopen Settings modal to verify persistence
                    window.evaluate_js(
                        "var btn = document.getElementById('btn-open-settings-modal');"
                        "if (btn) btn.click(); else if (window.openModal) window.openModal('modal-settings');"
                    )
                    time.sleep(0.4)

                    # Capture screenshot WHILE Settings modal is open
                    _capture_screenshot("smoke_roundtrip")

                    roundtrip_res = window.evaluate_js(
                        """
                        (function() {
                            var quickSummary = document.getElementById('pref-quick-summary');
                            var summaryText = quickSummary ? quickSummary.textContent : '';

                            var manualBtn = document.querySelector('#set-fight-mode [data-value=\"MANUAL\"]');
                            var manualActive = Boolean(manualBtn && manualBtn.classList.contains('active'));
                            var sw = document.getElementById('set-sw-auto-push');
                            var autoPushChecked = Boolean(sw && sw.checked);

                            var closeBtn = document.querySelector('#modal-settings [data-close=\"modal-settings\"]');
                            if (closeBtn) closeBtn.click();
                            else if (window.closeModal) window.closeModal('modal-settings');

                            return {
                                summary_text: summaryText,
                                has_manual: summaryText.indexOf('MANUAL') !== -1,
                                manual_active: manualActive,
                                auto_push_checked: autoPushChecked
                            };
                        })()
                        """
                    )
                    time.sleep(0.15)
                except Exception as exc:
                    roundtrip_res = {"ok": False, "error": str(exc)}

            pages_rendered = []
            overflow_errs = 0
            emoji_cnt = 0
            button_radius_check: Any = None
            if isinstance(res_1120, dict):
                pages_rendered = res_1120.get("pages_rendered") or []
                overflow_errs = int(res_1120.get("overflow_errors", 0) or 0)
                emoji_cnt = int(res_1120.get("emoji_count", 0) or 0)
                button_radius_check = res_1120.get("button_radius_check")

            final_console_errors: List[str] = []
            try:
                ce = window.evaluate_js("window.__prtsConsoleErrors ? window.__prtsConsoleErrors.slice() : []")
                if isinstance(ce, list):
                    final_console_errors = [str(x) for x in ce]
            except Exception:
                pass

            handshake_ok = bool(getattr(api, "_frontend_ready", False))
            checks_ok = (
                handshake_ok
                and len(final_console_errors) == 0
                and isinstance(settings_tabs_check, dict) and bool(settings_tabs_check.get("ok"))
                and isinstance(button_radius_check, dict) and bool(button_radius_check.get("ok"))
                and isinstance(theme_motion_check, dict) and bool(theme_motion_check.get("ok"))
                and isinstance(raf_radius_sample, dict) and bool(raf_radius_sample.get("ok"))
                and isinstance(toolbox_page_check, dict) and bool(toolbox_page_check.get("ok"))
                and isinstance(dry_run_ui_check, dict) and bool(dry_run_ui_check.get("ok"))
            )
            if not checks_ok:
                smoke_exit_code[0] = 1

            smoke_payload = {
                "pywebviewready": True,
                "api_handshake_ok": handshake_ok,
                "resize_tested": True,
                "pages_rendered": pages_rendered,
                "overflow_errors": overflow_errs,
                "emoji_count": emoji_cnt,
                "wizard_auto_opened": wizard_auto_opened,
                "console_errors": final_console_errors,
                "console_error_count": len(final_console_errors),
                "settings_tabs_check": settings_tabs_check,
                "button_radius_check": button_radius_check,
                "theme_motion_check": theme_motion_check,
                "raf_radius_sample": raf_radius_sample,
                "toolbox_page_check": toolbox_page_check,
                "dry_run_ui_check": dry_run_ui_check,
                "smoke_1120x740": res_1120,
                "smoke_960x640": res_960,
                "smoke_920x600": res_920,
                "smoke_1280x760": res_1280,
                "agent_modal_check": agent_check,
            }
            if roundtrip_res is not None:
                smoke_payload["smoke_roundtrip"] = roundtrip_res

            out_p = Path(smoke_test_output)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_text(json.dumps(smoke_payload, ensure_ascii=False, indent=2), encoding="utf-8")

            time.sleep(0.3)
            window.destroy()

        smoke_thread = threading.Thread(target=_smoke_runner, daemon=True)
        smoke_thread.start()

    def _on_closed() -> None:
        stop_threads.set()
        controller.stop_current_task()

    window.events.closed += _on_closed

    try:
        webview.start(
            gui="edgechromium",
            http_server=True,
            storage_path=str(storage_path),
            private_mode=False,
        )
    except Exception as exc:
        print(f"[ERROR] Failed to start pywebview GUI: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_threads.set()

    if smoke_test_output and smoke_exit_code[0] != 0:
        return smoke_exit_code[0]

    return 0
