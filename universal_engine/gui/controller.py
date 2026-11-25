"""PRTS · MAA Desktop Controller.

Headless MVC Controller providing complete task lifecycle management, background task dispatching,
and event draining for the pywebview modern Light-Mode interface.
Zero GUI framework (tkinter/customtkinter) dependencies.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

if getattr(sys, "frozen", False):
    from universal_engine import ai_strategist, auto_growth, maa_runner, roguelike_ai_copilot, stage_pilot
    from universal_engine.master_scheduler import MaaEngine, get_arknights_week_key, load_state
    from universal_engine.utils import engine_lock, env_doctor, maa_installer
    from universal_engine.gui import contract
else:
    from .. import ai_strategist, auto_growth, maa_runner, roguelike_ai_copilot, stage_pilot
    from ..master_scheduler import MaaEngine, get_arknights_week_key, load_state
    from ..utils import engine_lock, env_doctor, maa_installer
    from . import contract


def _open_path_in_os(target: Path) -> None:
    try:
        p = Path(target)
        if not p.exists():
            p = p.parent
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception:
        pass


def collect_dashboard_snapshot(engine: MaaEngine, check_adb: bool = False) -> Dict[str, Any]:
    """Gather safe status bar & dashboard card data (no heavy DLL loading)."""
    cfg = engine.get_config()
    maa_dir = maa_installer.resolve_maa_path(engine.config_path)
    integrity = env_doctor.check_maa_integrity(maa_dir) if maa_dir else {
        "valid": False,
        "maa_dir": "",
        "maa_version": "",
        "version_hint": "",
        "missing_items": ["MaaCore.dll"],
        "reason": "未检测到 MAA 目录",
    }

    my_box: Dict[str, Any] = {"total_owned": 0, "e2_count": 0}
    depot_info: Dict[str, Any] = {"syncTime": "", "count": 0}
    growth_preview: Dict[str, Any] = {"ok": False, "recommended_stages": ["1-7"], "reason": "尚未就绪"}
    if maa_dir and integrity.get("valid"):
        try:
            my_box = auto_growth.sync_my_box(maa_dir, base_dir=engine.base_dir, persist=False)
        except Exception:
            pass
        try:
            dep = auto_growth.load_depot_data(maa_dir, base_dir=engine.base_dir)
            depot_info = {"syncTime": dep.get("syncTime", ""), "count": len(dep.get("data", {}))}
        except Exception:
            pass
        try:
            growth_preview = engine.plan_auto_growth()
        except Exception:
            pass

    try:
        rl_resolved = maa_runner.resolve_roguelike_settings(config=cfg, maa_dir=maa_dir)
    except Exception:
        rl_resolved = {
            "theme": "BlackFlow",
            "mode_label": "Exp",
            "params": {"mode": 0, "squad": "指挥分队", "roles": "取长补短"},
            "ai_copilot_enabled": False,
            "auto_claim_rewards": True,
            "auto_upgrade_tech_tree": True,
            "loaded_from_gui_config": False,
        }

    try:
        ra_resolved = maa_runner.resolve_reclamation_settings(config=cfg, maa_dir=maa_dir)
    except Exception:
        ra_resolved = {
            "theme": "RelaunchAnchor",
            "mode_label": "RA1",
            "params": {"mode": 16, "clear_store": False},
            "loaded_from_gui_config": False,
        }

    state = load_state(engine.base_dir)
    week_key = get_arknights_week_key()
    annih_cached = state.get("annihilation", {})
    if annih_cached.get("week_key") == week_key and isinstance(annih_cached.get("weekly_process"), list):
        proc = annih_cached["weekly_process"]
    else:
        proc = None

    adb_info: Optional[Dict[str, Any]] = None
    if check_adb:
        adb_exe = env_doctor.discover_adb_executable(cfg, maa_dir=maa_dir)
        emu_addr = str(cfg.get("emulator", {}).get("adb_address", "") or "")
        adb_info = env_doctor.check_adb_resolution(
            adb_exe or "",
            adb_address=emu_addr,
            config=cfg,
            maa_dir=maa_dir,
            allow_self_heal=True,
            allow_kill_server=False,
        )

    return {
        "config": cfg,
        "maa_dir": str(maa_dir) if maa_dir else "",
        "integrity": integrity,
        "my_box": my_box,
        "depot": depot_info,
        "growth_preview": growth_preview,
        "roguelike_resolved": rl_resolved,
        "reclamation_resolved": ra_resolved,
        "week_key": week_key,
        "annihilation_process": proc,
        "adb_info": adb_info,
    }


def _dispatch_collect_snapshot(engine: MaaEngine, check_adb: bool = False) -> Dict[str, Any]:
    """Dispatch snapshot collection respecting mock patches on launcher_gui or controller."""
    lg = sys.modules.get("universal_engine.launcher_gui")
    if lg and hasattr(lg, "collect_dashboard_snapshot") and lg.collect_dashboard_snapshot is not collect_dashboard_snapshot:
        return lg.collect_dashboard_snapshot(engine, check_adb=check_adb)
    return collect_dashboard_snapshot(engine, check_adb=check_adb)


def _parse_annihilation_process(proc: Any) -> Tuple[str, Optional[List[int]], bool, str]:
    """Parse annihilation progress into (process_str, process_raw, done, label) with zero emojis."""
    cur_val: Optional[int] = None
    max_val: Optional[int] = None
    if isinstance(proc, (list, tuple)) and len(proc) >= 2:
        try:
            cur_val = int(proc[0])
            max_val = int(proc[1])
        except (ValueError, TypeError):
            cur_val = None
            max_val = None
    elif isinstance(proc, str) and "/" in proc:
        parts = proc.split("/")
        if len(parts) == 2:
            try:
                cur_val = int(parts[0].strip())
                max_val = int(parts[1].strip())
            except (ValueError, TypeError):
                cur_val = None
                max_val = None

    if cur_val is not None and max_val is not None and max_val > 0:
        done = (cur_val >= max_val)
        process_str = f"{cur_val} / {max_val}"
        process_raw = [cur_val, max_val]
        if done:
            label = "本周已完成"
        else:
            diff = max(0, max_val - cur_val)
            label = f"剩余 {diff} 合成玉"
        return process_str, process_raw, done, label

    return "-- / 1800", None, False, "本周待检查"


def format_snapshot_payload(raw_snap: Dict[str, Any]) -> Dict[str, Any]:
    """Convert raw collector dictionary to frozen contract.py snapshot schema."""
    cfg = raw_snap.get("config", {}) or {}
    maa_dir = raw_snap.get("maa_dir", "") or ""
    integ = raw_snap.get("integrity", {}) or {}
    my_box = raw_snap.get("my_box", {}) or {}
    depot = raw_snap.get("depot", {}) or {}
    growth = raw_snap.get("growth_preview", {}) or {}
    proc = raw_snap.get("annihilation_process")
    adb = raw_snap.get("adb_info") or {}

    maa_ver = str(integ.get("maa_version") or integ.get("version_hint") or "")
    maa_valid = bool(integ.get("valid"))
    maa_reason = str(integ.get("reason", ""))

    adb_connected = bool(adb.get("connected"))
    adb_addr = str(adb.get("adb_address", "") or "")
    adb_w = adb.get("width")
    adb_h = adb.get("height")
    adb_16_9 = bool(adb.get("is_16_9"))
    adb_hung = bool(adb.get("mumu_adbd_hung"))
    adb_warn = str(adb.get("warning", "") or "")

    total_owned = int(my_box.get("total_owned", 0) or 0)
    e2_count = int(my_box.get("e2_count", 0) or 0)
    sync_time = str(depot.get("syncTime", "") or "")
    depot_count = int(depot.get("count", 0) or 0)

    ai_cfg = cfg.get("ai", {}) or {}
    ai_mode = str(ai_cfg.get("mode", "agent_collab") or "agent_collab")
    is_builtin = ai_mode == "builtin_api" and bool(ai_cfg.get("api_key"))
    model_name = str(ai_cfg.get("model_name", "gpt-4o-mini") or "gpt-4o-mini")
    ai_label = f"内置 API ({model_name})" if is_builtin else "桌面 Agent 协同 (免Key)"

    stages = growth.get("recommended_stages") or ["1-7"]
    first_st = stages[0] if stages else "<上一次作战>"
    growth_ok = bool(growth.get("ok"))
    growth_reason = str(growth.get("reason", ""))

    proc_str, proc_raw, done, annih_label = _parse_annihilation_process(proc)
    week_k = str(raw_snap.get("week_key", "") or "")

    server = str(cfg.get("server", "Official") or "Official")
    post_action = str(cfg.get("post_action", "none") or "none")

    daily_cfg = cfg.get("daily", {}) or {}
    fight_cfg = cfg.get("fight", {}) or {}
    fight_mode = str(daily_cfg.get("fight_mode") or fight_cfg.get("fight_mode") or "AUTO").upper()
    auto_push = bool(daily_cfg.get("auto_push", fight_cfg.get("auto_push", False)))
    summary_text = f"刷图策略: {fight_mode} | 自动解锁推图: {'开' if auto_push else '关'}"

    rl_res = raw_snap.get("roguelike_resolved") or {}
    rl_theme = str(rl_res.get("theme") or cfg.get("roguelike", {}).get("theme") or "BlackFlow")
    rl_params = rl_res.get("params") or {}
    rl_ai_on = bool(rl_res.get("ai_copilot_enabled", cfg.get("roguelike", {}).get("ai_copilot_enabled", False)))
    rl_target_disp = contract._format_rl_mode_display(rl_params.get("mode", 0), str(rl_res.get("mode_label", "")))
    rl_card_mode = contract.RL_CARD_MODE_AI if rl_ai_on else contract.RL_CARD_MODE_PURE
    rl_claim = bool(rl_res.get("auto_claim_rewards", True))
    rl_tech = bool(rl_res.get("auto_upgrade_tech_tree", True))
    rl_sub_hint = f"目标: {rl_target_disp} · 领奖:{'开' if rl_claim else '关'} · 加点:{'开' if rl_tech else '关'}"

    ra_res = raw_snap.get("reclamation_resolved") or {}
    ra_theme = str(ra_res.get("theme") or cfg.get("reclamation", {}).get("theme") or "RelaunchAnchor")
    ra_params = ra_res.get("params") or {}
    ra_theme_disp, ra_mode_disp = contract._format_ra_mode_display(
        ra_theme, ra_params.get("mode", 16), str(ra_res.get("mode_label", ""))
    )
    ra_clear = bool(ra_params.get("clear_store"))
    ra_loaded_from_gui = bool(ra_res.get("loaded_from_gui_config"))
    ra_clear_s = "自动搬空商店" if ra_clear else "不自动搬空商店"
    ra_src_s = "已继承原版 MAA 配置" if ra_loaded_from_gui else "使用启动器配置"
    ra_sub_hint = f"当前刷取：{ra_theme_disp} / {ra_mode_disp}（{ra_clear_s} · {ra_src_s}）。支持随时一键启动或优雅停止。"

    return {
        "config": cfg,
        "maa_dir": maa_dir,
        "maa": {
            "valid": maa_valid,
            "version": maa_ver,
            "reason": maa_reason,
        },
        "adb": {
            "connected": adb_connected,
            "adb_address": adb_addr,
            "width": adb_w,
            "height": adb_h,
            "is_16_9": adb_16_9,
            "mumu_adbd_hung": adb_hung,
            "warning": adb_warn,
        },
        "box": {
            "total_owned": total_owned,
            "e2_count": e2_count,
        },
        "depot": {
            "syncTime": sync_time,
            "count": depot_count,
        },
        "ai": {
            "mode": ai_mode,
            "model_name": model_name,
            "is_builtin_api": is_builtin,
            "label": ai_label,
        },
        "growth": {
            "ok": growth_ok,
            "recommended_stages": stages,
            "first_stage": first_st,
            "reason": growth_reason,
        },
        "annihilation": {
            "week_key": week_k,
            "process": proc_str,
            "process_raw": proc_raw,
            "done": done,
            "label": annih_label,
        },
        "server": server,
        "post_action": post_action,
        "quick_summary": {
            "fight_mode": fight_mode,
            "auto_push": auto_push,
            "text": summary_text,
        },
        "roguelike": {
            "theme": rl_theme,
            "theme_display": contract._format_rl_theme_display(rl_theme),
            "mode_label": str(rl_res.get("mode_label", "")),
            "target_display": rl_target_disp,
            "ai_copilot_enabled": rl_ai_on,
            "card_mode": rl_card_mode,
            "auto_claim_rewards": rl_claim,
            "auto_upgrade_tech_tree": rl_tech,
            "sub_hint": rl_sub_hint,
        },
        "reclamation": {
            "theme": ra_theme,
            "theme_display": ra_theme_disp,
            "mode_label": str(ra_res.get("mode_label", "")),
            "mode_display": ra_mode_disp,
            "badge_text": f"模式：{ra_theme_disp} · {ra_mode_disp}",
            "clear_store": ra_clear,
            "loaded_from_gui_config": ra_loaded_from_gui,
            "sub_hint": ra_sub_hint,
        },
    }


class LauncherController:
    """Headless application controller managing state, background tasks, and event streaming."""

    def __init__(
        self,
        engine: Optional[MaaEngine] = None,
        open_wizard_force: bool = False,
        auto_start_refresh: bool = True,
        event_sink: Optional[Callable[[str, Any], None]] = None,
    ) -> None:
        self.engine = engine if engine is not None else MaaEngine(launch_source="gui")
        self._open_wizard_force = open_wizard_force
        self._event_sink = event_sink

        self._cancel_event = threading.Event()
        self._manual_resume_event = threading.Event()
        self._tutorial_resume_event = threading.Event()
        self._maa_update_cancel_event = threading.Event()
        self._dl_cancel_event = threading.Event()

        self._task_running: bool = False
        self._active_task_kind: Optional[str] = None
        self._maa_updating: bool = False
        self._downloading: bool = False

        self._active_agent_task: Optional[Dict[str, Any]] = None
        self._active_tutorial_stage: Optional[str] = None
        self._active_tutorial_payload: Optional[Dict[str, Any]] = None

        self._snapshot_raw: Dict[str, Any] = {}
        self._snapshot_formatted: Dict[str, Any] = {}
        self._logs: collections.deque[Dict[str, Any]] = collections.deque(maxlen=2000)
        self._log_id_counter: int = 0
        self._ui_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()
        self._lock = threading.Lock()

        self._last_task_state: Dict[str, Any] = {
            "running": False,
            "task_kind": None,
            "status_text": "空闲就绪",
            "progress_mode": "determinate",
            "progress_value": 0.0,
            "progress_cur": 0,
            "progress_total": 0,
            "level": "normal",
            "ra_button_text": "启动生息演算",
            "ra_button_enabled": True,
            "stop_enabled": False,
        }

        # Initialize snapshot synchronously or trigger async refresh
        initial_snap = _dispatch_collect_snapshot(self.engine, check_adb=False)
        self._apply_snapshot(initial_snap)

        if self._open_wizard_force:
            self._ui_queue.put(("open_wizard", {}))
        elif auto_start_refresh:
            self._refresh_snapshot_async(check_adb=False)

    # ---------- Public State Properties ----------
    @property
    def task_running(self) -> bool:
        return self._task_running

    @property
    def active_task_kind(self) -> Optional[str]:
        return self._active_task_kind

    @property
    def task_state(self) -> Dict[str, Any]:
        return dict(self._last_task_state)

    @property
    def snapshot_payload(self) -> Dict[str, Any]:
        return dict(self._snapshot_formatted)

    @property
    def agent_banner_state(self) -> Dict[str, Any]:
        if not self._active_agent_task:
            return {"visible": False, "task_type": "", "stage_code": "", "title": "", "subtitle": "", "task_info": {}}
        return {
            "visible": True,
            "task_type": str(self._active_agent_task.get("task_type", "copilot_repair")),
            "stage_code": str(self._active_agent_task.get("stage_code", "")),
            "title": "已生成 agent_task.json，点击复制指令发给你的 AI Agent",
            "subtitle": "",
            "task_info": self._active_agent_task,
        }

    @property
    def tutorial_banner_state(self) -> Dict[str, Any]:
        if not self._active_tutorial_stage:
            return {"visible": False, "stage_code": "", "next_stage": "", "title": "", "subtitle": ""}
        return {
            "visible": True,
            "stage_code": self._active_tutorial_stage,
            "next_stage": str((self._active_tutorial_payload or {}).get("next_stage", "")),
            "title": f"[教学关交接] 关卡={self._active_tutorial_stage}",
            "subtitle": "",
        }

    @property
    def logs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._logs)

    @property
    def manual_resume_event(self) -> threading.Event:
        return self._manual_resume_event

    @property
    def tutorial_resume_event(self) -> threading.Event:
        return self._tutorial_resume_event

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    # ---------- Event Sink & Dispatcher Drain ----------
    def drain_events(self) -> List[Dict[str, Any]]:
        """Drain batched push events for pywebview dispatcher, coalescing consecutive logs."""
        raw_items: List[Tuple[str, Any]] = []
        try:
            while True:
                item = self._ui_queue.get_nowait()
                raw_items.append(item)
        except queue.Empty:
            pass

        events: List[Dict[str, Any]] = []
        log_acc: List[Dict[str, Any]] = []

        def _flush_logs() -> None:
            nonlocal log_acc
            if log_acc:
                events.append({"type": "log_batch", "payload": list(log_acc)})
                log_acc = []

        for kind, payload in raw_items:
            if kind == "log_ready":
                log_acc.append(payload)
                continue

            _flush_logs()

            if kind == "snapshot_ready":
                self._apply_snapshot(payload)
                events.append({"type": "snapshot", "payload": self._snapshot_formatted})
                events.append({"type": "adb_status", "payload": self._snapshot_formatted.get("adb", {})})
            elif kind in ("adb_status_update", "adb_probe_done"):
                self._apply_adb_status(payload)
                events.append({"type": "adb_status", "payload": self._snapshot_formatted.get("adb", {})})
            elif kind == "task_state":
                events.append({"type": "task_state", "payload": payload})
            elif kind == "agent_banner":
                events.append({"type": "agent_banner", "payload": payload})
            elif kind == "tutorial_banner":
                events.append({"type": "tutorial_banner", "payload": payload})
            elif kind == "toast":
                events.append({"type": "toast", "payload": payload})
            elif kind == "maa_update_check_done":
                events.append({"type": "maa_update_check_done", "payload": payload})
            elif kind == "maa_update_progress":
                events.append({"type": "maa_update_progress", "payload": payload})
            elif kind == "maa_update_apply_done":
                events.append({"type": "maa_update_apply_done", "payload": payload})
            elif kind == "wizard_maa_scan":
                events.append({"type": "wizard_maa_scan", "payload": payload})
            elif kind == "wizard_dl_progress":
                events.append({"type": "wizard_dl_progress", "payload": payload})
            elif kind == "wizard_dl_finished":
                events.append({"type": "wizard_dl_finished", "payload": payload})
            elif kind == "wizard_diag_done":
                events.append({"type": "wizard_diag_done", "payload": payload})
            elif kind == "wizard_launch_done":
                events.append({"type": "wizard_launch_done", "payload": payload})
            elif kind == "open_wizard":
                events.append({"type": "open_wizard", "payload": {}})

        _flush_logs()

        if self._event_sink:
            for ev in events:
                try:
                    self._event_sink(ev["type"], ev["payload"])
                except Exception:
                    pass

        return events

    # ---------- Internal Log & Banner Methods ----------
    def _append_log(self, text: str) -> Dict[str, Any]:
        with self._lock:
            self._log_id_counter += 1
            entry = {
                "id": self._log_id_counter,
                "ts": datetime.now().strftime("%H:%M:%S"),
                "text": str(text),
                "tag": contract.classify_log_tag(str(text)),
            }
            self._logs.append(entry)

        self._ui_queue.put(("log_ready", entry))

        # Auto-hide banner upon external agent resolution or fallback resolution
        t = entry["text"]
        if (
            ("[Agent协同]" in t and ("已修改" in t or "规则基线" in t))
            or ("[RL-AI:" in t and ("agent_collab" in t or "rule_timeout_fallback" in t or "rule_engine" in t))
        ):
            self._hide_agent_banner()

        return entry

    def _cb_log(self, msg: str) -> None:
        self._append_log(msg)

    def _cb_progress(self, cur: int, total: int, msg: str) -> None:
        val = min(1.0, max(0.0, cur / total)) if total > 0 else 0.0
        self._last_task_state.update({
            "progress_mode": "determinate",
            "progress_value": val,
            "progress_cur": cur,
            "progress_total": total,
            "status_text": f"执行中 ({cur}/{total}): {msg}",
        })
        self._ui_queue.put(("task_state", dict(self._last_task_state)))

    def _cb_agent_collab(self, task_payload: Dict[str, Any]) -> None:
        self._show_agent_banner(task_payload)

    def _cb_tutorial_handoff(self, payload: Dict[str, Any]) -> None:
        action = str(payload.get("action", "show") or "show")
        if action == "hide":
            self._hide_tutorial_banner()
        else:
            self._show_tutorial_banner(payload)

    def _emit_toast(self, level: str, title: str, message: str) -> None:
        self._ui_queue.put(("toast", {"level": level, "title": title, "message": message}))

    def _show_agent_banner(self, payload: Dict[str, Any]) -> None:
        self._hide_tutorial_banner()
        self._active_agent_task = payload
        stage = str(payload.get("stage_code", "-"))
        if str(payload.get("task_type", "")).lower() == "roguelike_decision":
            theme = payload.get("theme", "BlackFlow")
            dec_type = payload.get("decision_type", "unknown")
            timeout_s = payload.get("timeout_sec", 45)
            auto_fb = payload.get("auto_fallback_on_timeout", True)
            fb_hint = f"超时 {int(float(timeout_s))}s 自动规则兜底" if auto_fb else "等待手动/Agent 响应"
            title = f"[肉鸽AI] 已生成 agent_task.json（抉择点：{dec_type}），点击复制指令发给桌面 AI Agent"
            subtitle = f"主题: {theme} | 监听响应: agent_decision.json | {fb_hint}（或点击「用规则继续」）"
        else:
            rnd = payload.get("round", 1)
            bs = payload.get("battle_state") or {}
            kills = bs.get("kills")
            unavail_ops = payload.get("unavailable_opers") or bs.get("unavailable_opers") or []
            target = Path(str(payload.get("target_copilot_path", ""))).name
            reason = payload.get("trigger_reason", "")
            title = "已生成 agent_task.json，点击复制指令发给你的 AI Agent（如 Antigravity / Claude Code）"
            if reason == "formation_unavailable":
                subtitle = f"关卡: {stage} (第{rnd}轮 · 编队干员不可用: {unavail_ops}) | 作业: {target}（全部替补与备选作业失败，待外部 Agent 处理）"
            else:
                subtitle = f"关卡: {stage} (第{rnd}轮 · {reason}) | 翻车击杀数: {kills} | 正在监听: {target}（Agent 保存修改后将自动续跑）"

        banner_payload = {
            "visible": True,
            "task_type": str(payload.get("task_type", "copilot_repair")),
            "stage_code": stage,
            "title": title,
            "subtitle": subtitle,
            "task_info": payload,
        }
        self._ui_queue.put(("agent_banner", banner_payload))

    def _hide_agent_banner(self) -> None:
        if self._active_agent_task is not None:
            self._active_agent_task = None
            self._ui_queue.put(("agent_banner", {
                "visible": False,
                "task_type": "",
                "stage_code": "",
                "title": "",
                "subtitle": "",
                "task_info": {},
            }))

    def _show_tutorial_banner(self, payload: Dict[str, Any]) -> None:
        self._hide_agent_banner()
        stage = str(payload.get("stage_code", "TL-TR-1") or "TL-TR-1")
        next_st = str(payload.get("next_stage", "") or "")
        msg = str(
            payload.get("message")
            or f"[教学关交接] 当前遇到无法跳过的教学关卡（{stage}），请在模拟器中手动点完本关教学，通关返回地图后系统将自动恢复后续推图！"
        )
        self._active_tutorial_stage = stage
        self._active_tutorial_payload = payload
        next_hint = f"目标解锁下一关：{next_st}" if next_st else "目标状态：返回关卡地图"
        banner_payload = {
            "visible": True,
            "stage_code": stage,
            "next_stage": next_st,
            "title": msg,
            "subtitle": f"后台每 3 秒静默轮询检测地图界面状态（{next_hint}），通关返回后将自动收起本提醒并无缝继续",
        }
        self._ui_queue.put(("tutorial_banner", banner_payload))

    def _hide_tutorial_banner(self) -> None:
        if self._active_tutorial_stage is not None:
            self._active_tutorial_stage = None
            self._active_tutorial_payload = None
            self._ui_queue.put(("tutorial_banner", {
                "visible": False,
                "stage_code": "",
                "next_stage": "",
                "title": "",
                "subtitle": "",
            }))

    # ---------- Snapshot & ADB State ----------
    def _apply_adb_status(self, adb: Optional[Dict[str, Any]]) -> None:
        if adb is None:
            return
        if isinstance(self._snapshot_raw, dict):
            self._snapshot_raw["adb_info"] = dict(adb)
        self._snapshot_formatted = format_snapshot_payload(self._snapshot_raw)

    def _apply_snapshot(self, snap: Dict[str, Any]) -> None:
        prev_adb = self._snapshot_raw.get("adb_info") if isinstance(self._snapshot_raw, dict) else None
        self._snapshot_raw = dict(snap)
        if snap.get("adb_info") is None and prev_adb is not None:
            self._snapshot_raw["adb_info"] = prev_adb
        self._snapshot_formatted = format_snapshot_payload(self._snapshot_raw)

    def _refresh_snapshot_async(self, check_adb: bool = False) -> None:
        def _worker() -> None:
            snap = _dispatch_collect_snapshot(self.engine, check_adb=check_adb)
            self._ui_queue.put(("snapshot_ready", snap))

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- Task State Management ----------
    def _set_running_state(self, running: bool, status_text: str = "", task_kind: Optional[str] = None) -> None:
        self._task_running = running
        self._active_task_kind = task_kind if running else None

        ra_btn_text = "停止生息演算" if (running and self._active_task_kind == "reclamation") else "启动生息演算"
        ra_btn_enabled = True if (not running or self._active_task_kind == "reclamation") else False
        stop_enabled = running

        state_payload = {
            "running": running,
            "task_kind": self._active_task_kind,
            "status_text": status_text or ("任务执行中..." if running else "空闲就绪"),
            "progress_mode": "indeterminate" if running else "determinate",
            "progress_value": 0.0,
            "progress_cur": 0,
            "progress_total": 0,
            "level": "running" if running else "normal",
            "ra_button_text": ra_btn_text,
            "ra_button_enabled": ra_btn_enabled,
            "stop_enabled": stop_enabled,
        }
        self._last_task_state = state_payload
        self._ui_queue.put(("task_state", state_payload))

    def _ensure_adb_before_task(self) -> Dict[str, Any]:
        """Pre-flight ADB verification & auto-reconnect/launch shared before every task entrypoint."""
        if self._cancel_event.is_set():
            raise env_doctor.OperationCancelledError("任务已取消")

        cfg = self.engine.get_config()
        maa_dir = maa_installer.resolve_maa_path(self.engine.config_path)
        cached_adb = self._snapshot_raw.get("adb_info") if isinstance(self._snapshot_raw, dict) else None
        cached_ok = bool(isinstance(cached_adb, dict) and cached_adb.get("connected"))

        adb_exe = env_doctor.discover_adb_executable(cfg, maa_dir=maa_dir)
        emu_addr = str(cfg.get("emulator", {}).get("adb_address", "") or "")
        quick_res: Dict[str, Any] = {"connected": False}
        if cached_ok and adb_exe:
            quick_res = env_doctor.check_adb_resolution(
                adb_exe,
                adb_address=str(cached_adb.get("adb_address") or emu_addr),
                config=cfg,
                maa_dir=maa_dir,
                cancel_event=self._cancel_event,
                allow_self_heal=False,
                allow_kill_server=False,
            )
            if quick_res.get("connected"):
                self._ui_queue.put(("adb_status_update", quick_res))
                return quick_res

        self._append_log("[ADB] 检测到当前 ADB 未连接或连接校验失败，正在自动执行端口探测与重连/拉起模拟器...")
        probe_res = self.engine.probe_or_launch_emulator(
            cfg=cfg,
            timeout_sec=60,
            cancel_event=self._cancel_event,
            log_callback=self._cb_log,
            auto_launch=True,
        )
        self._ui_queue.put(("adb_status_update", probe_res))
        if probe_res.get("connected"):
            self._append_log(
                f"[ADB] 任务启动前自动重连成功: {probe_res.get('adb_address')} "
                f"({probe_res.get('width')}x{probe_res.get('height')})"
            )
        return probe_res

    def _run_background_task(
        self,
        task_name: str,
        fn: Callable[[], Dict[str, Any]],
        on_done: Optional[Callable[[Dict[str, Any]], None]] = None,
        task_kind: Optional[str] = None,
    ) -> None:
        if self._task_running:
            self._emit_toast("warn", "提示", "当前已有任务在运行中，请先等待完成或点击「停止任务」。")
            return
        active_lock = self.engine.check_active_lock()
        if active_lock is not None:
            lock_msg = engine_lock.format_lock_conflict_message(active_lock)
            self._append_log(lock_msg)
            self._emit_toast("warn", "任务冲突 · 禁止双开", lock_msg)
            return
        if not self.engine.ensure_maa_ready(auto_download=False).get("ok"):
            self._ui_queue.put(("open_wizard", {}))
            return

        self._cancel_event.clear()
        self._manual_resume_event.clear()
        self._tutorial_resume_event.clear()
        self._set_running_state(True, f"正在执行：{task_name}...", task_kind=task_kind)
        self._append_log(f"开始任务：{task_name}")

        def _worker() -> None:
            try:
                adb_pre = self._ensure_adb_before_task()
                if self._cancel_event.is_set():
                    res = {"ok": False, "cancelled": True}
                elif adb_pre.get("mumu_adbd_hung"):
                    res = {"ok": False, "error": str(adb_pre.get("warning") or "检测到模拟器 adbd 假死，请重启模拟器后重试")}
                else:
                    res = fn()
            except env_doctor.OperationCancelledError:
                res = {"ok": False, "cancelled": True}
            except Exception as exc:
                if self._cancel_event.is_set():
                    res = {"ok": False, "cancelled": True}
                else:
                    res = {"ok": False, "error": str(exc)}

            # Handle completion & trigger post-task silent adb check
            self._hide_agent_banner()
            self._hide_tutorial_banner()
            cancelled = bool(res.get("cancelled"))
            ok = bool(res.get("ok", True)) and ("error" not in res)
            if cancelled:
                status_text = f"已停止：{task_name}"
            elif ok:
                status_text = f"已完成：{task_name}"
            elif res.get("locked"):
                status_text = "已拒绝启动（存在运行中任务）"
                if res.get("error"):
                    self._append_log(str(res.get("error")))
            else:
                status_text = f"任务结束（异常/未完成）：{task_name}"
                if res.get("error"):
                    self._append_log(f"[ERR] {task_name}: {res.get('error')}")

            self._set_running_state(False, status_text)

            if on_done:
                try:
                    on_done(res)
                except Exception as exc:
                    self._append_log(f"[ERR] 战报解析异常: {exc}")

            # Silently probe ADB in background & refresh top status badge after task stop/finish
            self._refresh_snapshot_async(check_adb=True)

        threading.Thread(target=_worker, daemon=True).start()

    def _stop_current_task(self) -> None:
        if not self._task_running:
            return
        already_stopping = self._cancel_event.is_set()
        self._cancel_event.set()
        self._set_running_state(True, "正在立即中止任务...", task_kind=self._active_task_kind)
        if not already_stopping:
            self._append_log("[WARN] 收到用户停止指令，正在 1 秒内中止当前任务与子进程...")
        self._hide_agent_banner()
        self._hide_tutorial_banner()

    # ---------- 10 Core Task Entrypoints ----------
    def start_daily(self) -> Dict[str, Any]:
        self._run_background_task(
            "一键日常收菜",
            lambda: self.engine.run_daily(
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
                collab_handler=self._cb_agent_collab,
                manual_resume_event=self._manual_resume_event,
                tutorial_handler=self._cb_tutorial_handoff,
                tutorial_resume_event=self._tutorial_resume_event,
            ),
            on_done=self._log_daily_report,
            task_kind="daily",
        )
        return {"ok": True}

    def start_auto_growth(self) -> Dict[str, Any]:
        self._run_background_task(
            "智能自主养成刷图",
            lambda: self.engine.run_fight(
                fight_mode="AUTO",
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
                collab_handler=self._cb_agent_collab,
                manual_resume_event=self._manual_resume_event,
                tutorial_handler=self._cb_tutorial_handoff,
                tutorial_resume_event=self._tutorial_resume_event,
            ),
            on_done=lambda r: self._append_log(
                f"[每日战报] 自主养成刷图完成 | 选中关卡={r.get('selected_stage')} | 完成次数={r.get('runs_finished', 0)} | 策略={r.get('strategy', '')}"
            ),
            task_kind="growth",
        )
        return {"ok": True}

    def preview_growth_plan(self) -> Dict[str, Any]:
        try:
            plan = self.engine.plan_auto_growth()
        except Exception as exc:
            plan = {"ok": False, "error": str(exc), "recommended_stages": ["1-7"], "reason": str(exc)}
        ok = bool(plan.get("ok", True))
        if ok:
            self._append_log(
                f"[AI养成决策] 今日推荐队列={plan.get('recommended_stages')} | 理由: {plan.get('reason')}"
            )
        else:
            self._append_log(f"[WARN] 养成决策预览失败: {plan.get('error', '')}")
        self._refresh_snapshot_async(check_adb=False)
        return {"ok": ok, "plan": plan}

    def start_annihilation(self) -> Dict[str, Any]:
        self._run_background_task(
            "当期剿灭托管",
            lambda: self.engine.run_annihilation(
                force=True,
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
                collab_handler=self._cb_agent_collab,
                manual_resume_event=self._manual_resume_event,
                tutorial_handler=self._cb_tutorial_handoff,
                tutorial_resume_event=self._tutorial_resume_event,
            ),
            on_done=lambda r: self._append_log(
                f"[每日战报] 当期剿灭结束 | 状态={r.get('status')} | 周进度={r.get('weekly_process')} | 本次完成={r.get('runs_finished', 0)}"
            ),
            task_kind="annihilation",
        )
        return {"ok": True}

    def start_push(self, stages_raw: str = "") -> Dict[str, Any]:
        raw = str(stages_raw).strip()
        stages = [s.strip() for s in raw.replace("，", ",").split(",") if s.strip()] if raw else ["auto"]
        self._run_background_task(
            f"新活动/主线推图 ({','.join(stages)})",
            lambda: self.engine.run_push(
                stages=stages,
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
                collab_handler=self._cb_agent_collab,
                manual_resume_event=self._manual_resume_event,
                tutorial_handler=self._cb_tutorial_handoff,
                tutorial_resume_event=self._tutorial_resume_event,
            ),
            on_done=lambda r: self._append_log(
                f"[每日战报] 推图任务结束 | 关卡={r.get('stages')} | 全通={r.get('ok')} | 战报={r.get('report_path', '')}"
            ),
            task_kind="push",
        )
        return {"ok": True}

    def start_sync_box(self) -> Dict[str, Any]:
        self._run_background_task(
            "刷新干员与仓库档案",
            lambda: self.engine.sync_box_and_depot(
                run_maa_tasks=True,
                log_callback=self._cb_log,
                cancel_event=self._cancel_event,
            ),
            on_done=lambda r: self._append_log(
                f"[AI养成决策] Box 与仓库刷新完成：干员 {r.get('my_box', {}).get('total_owned', 0)} 名（精二 {r.get('my_box', {}).get('e2_count', 0)}），"
                f"仓库物品 {len(r.get('depot', {}).get('data', {}))} 项。"
            ),
            task_kind="sync",
        )
        return {"ok": True}

    def set_roguelike_mode(self, mode_label: str) -> Dict[str, Any]:
        ai_on = (mode_label == contract.RL_CARD_MODE_AI)
        self.engine.save_config({"roguelike": {"ai_copilot_enabled": ai_on}})
        self._append_log(f"[肉鸽AI] 已切换集成战略模式为：{mode_label}（ai_copilot_enabled={ai_on}）")
        if isinstance(self._snapshot_raw, dict):
            self._snapshot_raw["config"] = self.engine.get_config()
            if "roguelike_resolved" in self._snapshot_raw and isinstance(self._snapshot_raw["roguelike_resolved"], dict):
                self._snapshot_raw["roguelike_resolved"]["ai_copilot_enabled"] = ai_on
            self._snapshot_formatted = format_snapshot_payload(self._snapshot_raw)
        self._refresh_snapshot_async(check_adb=False)
        return {"ok": True, "ai_copilot_enabled": ai_on, "snapshot": self._snapshot_formatted}

    def start_roguelike(self, mode_label: str = "") -> Dict[str, Any]:
        if mode_label:
            ai_on = (mode_label == contract.RL_CARD_MODE_AI)
            self.engine.save_config({"roguelike": {"ai_copilot_enabled": ai_on}})
        else:
            cfg = self.engine.get_config()
            ai_on = bool(cfg.get("roguelike", {}).get("ai_copilot_enabled", False))
            mode_label = contract.RL_CARD_MODE_AI if ai_on else contract.RL_CARD_MODE_PURE

        self._append_log(f"[肉鸽AI] 准备启动集成战略探索 | 模式={mode_label}")
        self._run_background_task(
            f"集成战略 · {mode_label}",
            lambda: self.engine.run_roguelike(
                ai_copilot_enabled=ai_on,
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
                collab_handler=self._cb_agent_collab,
                manual_resume_event=self._manual_resume_event,
            ),
            on_done=self._log_roguelike_report,
            task_kind="roguelike",
        )
        return {"ok": True}

    def start_roguelike_post_settlement(self) -> Dict[str, Any]:
        self._append_log("[肉鸽AI] 准备执行一键领奖与科技树自动加点...")
        self._run_background_task(
            "集成战略一键领奖与加点",
            lambda: self.engine.run_roguelike_post_settlement(
                auto_claim_rewards=True,
                auto_upgrade_tech_tree=True,
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
            ),
            on_done=self._log_roguelike_post_report,
            task_kind="roguelike_post",
        )
        return {"ok": True}

    def toggle_reclamation(self) -> Dict[str, Any]:
        if self._task_running and self._active_task_kind == "reclamation":
            self._append_log("[生息演算] 用户点击停止生息演算，正在优雅中止任务...")
            self._stop_current_task()
            return {"ok": True, "action": "stopping"}

        self._append_log("[生息演算] 准备启动生息演算长时托管任务...")
        self._run_background_task(
            "生息演算自动刷取",
            lambda: self.engine.run_reclamation(
                log_callback=self._cb_log,
                progress_callback=self._cb_progress,
                cancel_event=self._cancel_event,
            ),
            on_done=self._log_reclamation_report,
            task_kind="reclamation",
        )
        return {"ok": True, "action": "started"}

    def stop_current_task(self) -> Dict[str, Any]:
        self._stop_current_task()
        return {"ok": True}

    # ---------- Global Settings & Controls ----------
    def set_server(self, server: str) -> Dict[str, Any]:
        srv = "Bilibili" if "B" in str(server) else "Official"
        self.engine.save_config({"server": srv})
        self._append_log(f"已切换区服为：{server} ({srv})")
        if isinstance(self._snapshot_raw, dict):
            self._snapshot_raw["config"] = self.engine.get_config()
            self._snapshot_formatted = format_snapshot_payload(self._snapshot_raw)
        self._refresh_snapshot_async(check_adb=False)
        return {"ok": True, "server": srv, "snapshot": self._snapshot_formatted}

    def set_post_action(self, post_action: str) -> Dict[str, Any]:
        act = "close_emulator" if ("关闭" in str(post_action) or "close" in str(post_action).lower()) else "none"
        self.engine.save_config({"post_action": act})
        self._append_log(f"已设置任务完成后动作：{post_action} ({act})")
        if isinstance(self._snapshot_raw, dict):
            self._snapshot_raw["config"] = self.engine.get_config()
            self._snapshot_formatted = format_snapshot_payload(self._snapshot_raw)
        self._refresh_snapshot_async(check_adb=False)
        return {"ok": True, "post_action": act, "snapshot": self._snapshot_formatted}

    def probe_or_launch_emulator(self) -> Dict[str, Any]:
        self._append_log("正在探测 ADB 设备端口（默认 127.0.0.1:16384），如遇 offline 将自动断开重连...")

        def _worker() -> None:
            res = self.engine.probe_or_launch_emulator(
                timeout_sec=60,
                log_callback=self._cb_log,
                auto_launch=True,
            )
            self._ui_queue.put(("adb_probe_done", res))

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True}

    def check_maa_update(self) -> Dict[str, Any]:
        if self._maa_updating:
            return {"ok": False, "error": "MAA 正在更新中"}
        if self._task_running:
            self._emit_toast("info", "提示", "当前已有自动化任务在运行中，请先停止任务后再检查更新 MAA。")
            return {"ok": False, "error": "当前已有任务在运行"}

        self._maa_updating = True
        self._append_log("[更新] 正在连接 GitHub / api.maa.plus 检查 MAA 最新版本...")

        def _worker() -> None:
            res = self.engine.check_maa_update(timeout_sec=15)
            self._maa_updating = False
            self._ui_queue.put(("maa_update_check_done", res))

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True}

    def confirm_maa_update(self, release_info: Dict[str, Any], local_ver: str, remote_ver: str) -> Dict[str, Any]:
        self._maa_updating = True
        self._maa_update_cancel_event.clear()
        self._set_running_state(
            True, f"正在增量更新 MAA 内核 ({local_ver or '未知'} → {remote_ver})...", task_kind="maa_update"
        )
        self._append_log(
            f"[更新] 开始增量覆盖更新 MAA 内核与 resource/（保留 config/、universal_engine/ 与本启动器）: "
            f"{local_ver or '未知'} -> {remote_ver}"
        )

        def _prog(done: int, total: int, msg: str) -> None:
            ratio = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
            self._ui_queue.put(("maa_update_progress", {"done": done, "total": total, "msg": msg, "ratio": ratio}))

        def _worker() -> None:
            res = self.engine.update_maa_core(
                progress_callback=_prog,
                cancel_event=self._maa_update_cancel_event,
                release_info=release_info,
                timeout_sec=30,
            )
            self._maa_updating = False
            self._set_running_state(False, "空闲就绪" if res.get("ok") else "MAA 更新失败")
            self._ui_queue.put(("maa_update_apply_done", res))
            self._refresh_snapshot_async(check_adb=False)

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True}

    def cancel_maa_update(self) -> Dict[str, Any]:
        self._maa_update_cancel_event.set()
        self._append_log("[更新] 正在取消 MAA 内核更新...")
        return {"ok": True}

    def get_agent_prompts(self) -> Dict[str, Any]:
        maa_dir = maa_installer.resolve_maa_path(self.engine.config_path)
        init_prompt = contract.build_init_agent_prompt(self.engine.base_dir, maa_dir)
        repair_prompt = contract.build_repair_agent_prompt(self.engine.base_dir, self._active_agent_task)
        readme_content, readme_path = contract.load_readme_content(self.engine.base_dir)
        return {
            "ok": True,
            "init_prompt": init_prompt,
            "repair_prompt": repair_prompt,
            "prompt_init": init_prompt,
            "prompt_repair": repair_prompt,
            "readme_content": readme_content,
            "readme_path": readme_path,
            "active_task": self._active_agent_task,
        }

    def open_path(self, target: str) -> Dict[str, Any]:
        t = str(target).strip()
        if t == "reports":
            p = self.engine.base_dir / "reports"
        elif t == "logs":
            p = self.engine.base_dir / "logs"
        elif t == "config":
            p = self.engine.base_dir / "config"
        elif t == "maa":
            maa_dir = maa_installer.resolve_maa_path(self.engine.config_path)
            p = maa_dir if maa_dir else self.engine.base_dir
        elif t == "task":
            p = self.engine.base_dir / "agent_task.json"
        elif t == "box":
            p = self.engine.base_dir / "data" / "my_box.json"
        else:
            p = Path(t)
            if not p.is_absolute():
                p = self.engine.base_dir / p
        _open_path_in_os(p)
        return {"ok": True, "path": str(p)}

    def resume_with_rules(self) -> Dict[str, Any]:
        self._append_log("[Agent协同] 用户选择使用本地确定性规则继续执行。")
        self._hide_agent_banner()
        self._manual_resume_event.set()
        return {"ok": True}

    def resume_from_tutorial(self) -> Dict[str, Any]:
        st = self._active_tutorial_stage or "教学关"
        self._append_log(f"[教学关交接] 用户手动触发「我已通关，立即继续」，正在恢复 {st} 之后的推图流程...")
        self._hide_tutorial_banner()
        self._tutorial_resume_event.set()
        return {"ok": True}

    def get_settings(self) -> Dict[str, Any]:
        cfg = self.engine.get_config()
        maa_dir = maa_installer.resolve_maa_path(self.engine.config_path)

        daily_cfg = cfg.get("daily", {}) or {}
        fight_cfg = cfg.get("fight", {}) or {}
        growth_cfg = cfg.get("growth", {}) or {}
        rl_cfg = cfg.get("roguelike", {}) or {}
        ra_cfg = cfg.get("reclamation", {}) or {}
        ai_cfg = cfg.get("ai", {}) or {}
        emu_cfg = cfg.get("emulator", {}) or {}
        notif_cfg = cfg.get("notifications", {}) or {}

        def _get_opt_field(cfg_dict: Dict[str, Any], key: str, fallback: str) -> str:
            if key not in cfg_dict:
                return fallback
            val = cfg_dict[key]
            if val is None:
                return fallback
            return str(val)

        # 1. Daily & Fight normalized fields
        fight_mode = str(daily_cfg.get("fight_mode") or fight_cfg.get("fight_mode") or "AUTO").upper()
        override_stage = str(daily_cfg.get("override_stage") or fight_cfg.get("override_stage") or "")
        medicine = int(daily_cfg.get("medicine", fight_cfg.get("medicine", 0)) or 0)
        stone = int(daily_cfg.get("stone", fight_cfg.get("stone", 0)) or 0)
        max_push_rounds = int(daily_cfg.get("max_push_rounds", fight_cfg.get("max_push_rounds", 5)) or 5)
        auto_push = bool(daily_cfg.get("auto_push", fight_cfg.get("auto_push", False)))
        recruit_preserve_tags = list(daily_cfg.get("recruit_preserve_tags") or ["高级资深干员", "资深干员", "支援机械"])
        recruit_times = int(daily_cfg.get("recruit_times", 4) or 4)
        penguin_id = str(daily_cfg.get("penguin_id", "") or "")
        report_to_penguin = bool(daily_cfg.get("report_to_penguin", False))
        report_to_yituliu = bool(daily_cfg.get("report_to_yituliu", False))
        skip_tasks = list(daily_cfg.get("skip_tasks") or [])

        # 2. Growth normalized fields
        prefer_event = bool(growth_cfg.get("prefer_event", True))
        allow_chip_stages = bool(growth_cfg.get("allow_chip_stages", True))
        allow_resource_stages = bool(growth_cfg.get("allow_resource_stages", True))
        target_ops = growth_cfg.get("target_operators") or []
        if isinstance(target_ops, list):
            target_ops_list = [str(x).strip() for x in target_ops if str(x).strip()]
            target_ops_str = ", ".join(target_ops_list)
        else:
            target_ops_str = str(target_ops)
            target_ops_list = [x.strip() for x in target_ops_str.replace("，", ",").split(",") if x.strip()]
        mat_stock = dict(growth_cfg.get("material_safety_stock") or {})
        t3_stock = int(mat_stock.get("T3_DEFAULT", 30))

        # 3. Roguelike normalized fields (preserve "" for Follow Original MAA)
        ugc = rl_cfg.get("use_gui_config")
        if ugc is None:
            ugc = ra_cfg.get("use_gui_config")
        use_gui_config = True if ugc is None else bool(ugc)

        rl_theme = _get_opt_field(rl_cfg, "theme", "BlackFlow")
        rl_mode = _get_opt_field(rl_cfg, "mode", "Exp")
        rl_squad = _get_opt_field(rl_cfg, "squad", "指挥分队")
        rl_roles = _get_opt_field(rl_cfg, "roles", "取长补短")
        rl_core_char = str(rl_cfg.get("core_char", "") or "")
        rl_starts_count = rl_cfg.get("starts_count")
        rl_ai_copilot = bool(rl_cfg.get("ai_copilot_enabled", False))
        rl_timeout_fallback = bool(rl_cfg.get("ai_auto_fallback_on_timeout", True))
        rl_timeout_sec = float(rl_cfg.get("ai_decision_timeout_sec", 45.0) or 45.0)
        boss_prts = rl_cfg.get("boss_prts_assist")
        rl_boss_prts = True if boss_prts is None else bool(boss_prts)
        rl_claim_rewards = bool(rl_cfg.get("auto_claim_rewards", True))
        rl_upgrade_tech = bool(rl_cfg.get("auto_upgrade_tech_tree", True))

        # 4. Reclamation normalized fields (preserve "" for Follow Original MAA)
        ra_theme = _get_opt_field(ra_cfg, "theme", "RelaunchAnchor")
        ra_mode = _get_opt_field(ra_cfg, "mode", "RA1")
        ra_tools = ra_cfg.get("tools_to_craft") or []
        if isinstance(ra_tools, list):
            ra_tools_list = [str(x).strip() for x in ra_tools if str(x).strip()]
            ra_tools_str = ", ".join(ra_tools_list)
        else:
            ra_tools_str = str(ra_tools)
            ra_tools_list = [x.strip() for x in ra_tools_str.replace("，", ",").split(",") if x.strip()]
        ra_clear_store = bool(ra_cfg.get("clear_store", False))

        # 5. Emulator normalized fields
        emu_type = str(emu_cfg.get("type", "auto") or "auto")
        adb_address = str(emu_cfg.get("adb_address", "") or "")

        # 6. AI normalized fields
        ai_mode = str(ai_cfg.get("mode", "agent_collab") or "agent_collab")
        base_url = str(ai_cfg.get("base_url", "https://api.openai.com/v1") or "https://api.openai.com/v1")
        api_key = str(ai_cfg.get("api_key", "") or "")
        model_name = str(ai_cfg.get("model_name", "gpt-4o-mini") or "gpt-4o-mini")
        timeout_sec = int(ai_cfg.get("timeout_sec", 30) or 30)

        # 7. Notifications normalized fields
        webhook_enabled = bool(notif_cfg.get("webhook_enabled", False))
        webhook_url = str(notif_cfg.get("webhook_url", "") or "")
        notify_on_daily_finish = bool(notif_cfg.get("notify_on_daily_finish", True))
        notify_on_recruit_alert = bool(notif_cfg.get("notify_on_recruit_alert", True))
        notify_on_agent_task = bool(notif_cfg.get("notify_on_agent_task", True))

        # Build clean nested section dicts
        nested_daily = {
            "fight_mode": fight_mode,
            "override_stage": override_stage,
            "medicine": medicine,
            "stone": stone,
            "max_push_rounds": max_push_rounds,
            "auto_push": auto_push,
            "recruit_preserve_tags": recruit_preserve_tags,
            "recruit_times": recruit_times,
            "penguin_id": penguin_id,
            "report_to_penguin": report_to_penguin,
            "report_to_yituliu": report_to_yituliu,
            "skip_tasks": skip_tasks,
        }
        nested_growth = {
            "prefer_event": prefer_event,
            "allow_chip_stages": allow_chip_stages,
            "allow_resource_stages": allow_resource_stages,
            "target_operators": target_ops_list,
            "material_safety_stock": mat_stock,
        }
        nested_roguelike = {
            "use_gui_config": use_gui_config,
            "theme": rl_theme,
            "mode": rl_mode,
            "squad": rl_squad,
            "roles": rl_roles,
            "core_char": rl_core_char,
            "starts_count": rl_starts_count,
            "ai_copilot_enabled": rl_ai_copilot,
            "ai_auto_fallback_on_timeout": rl_timeout_fallback,
            "ai_decision_timeout_sec": rl_timeout_sec,
            "boss_prts_assist": rl_boss_prts,
            "auto_claim_rewards": rl_claim_rewards,
            "auto_upgrade_tech_tree": rl_upgrade_tech,
        }
        nested_reclamation = {
            "use_gui_config": use_gui_config,
            "theme": ra_theme,
            "mode": ra_mode,
            "tools_to_craft": ra_tools_list,
            "clear_store": ra_clear_store,
        }
        nested_ai = {
            "mode": ai_mode,
            "enabled": (ai_mode == "builtin_api"),
            "base_url": base_url,
            "api_key": api_key,
            "model_name": model_name,
            "timeout_sec": timeout_sec,
        }
        nested_emulator = {
            "type": emu_type,
            "adb_address": adb_address,
        }
        nested_notifications = {
            "webhook_enabled": webhook_enabled,
            "webhook_url": webhook_url,
            "notify_on_daily_finish": notify_on_daily_finish,
            "notify_on_recruit_alert": notify_on_recruit_alert,
            "notify_on_agent_task": notify_on_agent_task,
        }

        res: Dict[str, Any] = {
            "ok": True,
            "maa_path": str(maa_dir) if maa_dir else "",
            # Top-level nested sections
            "daily": nested_daily,
            "growth": nested_growth,
            "roguelike": nested_roguelike,
            "reclamation": nested_reclamation,
            "ai": nested_ai,
            "emulator": nested_emulator,
            "notifications": nested_notifications,
            # Flat backward-compatible keys
            "fight_mode": fight_mode,
            "override_stage": override_stage,
            "medicine": medicine,
            "stone": stone,
            "max_push_rounds": max_push_rounds,
            "auto_push": auto_push,
            "prefer_event": prefer_event,
            "allow_chip_stages": allow_chip_stages,
            "allow_resource_stages": allow_resource_stages,
            "target_operators": target_ops_str,
            "t3_stock": t3_stock,
            "recruit_preserve_tags": recruit_preserve_tags,
            "recruit_times": recruit_times,
            "penguin_id": penguin_id,
            "report_to_penguin": report_to_penguin,
            "report_to_yituliu": report_to_yituliu,
            "skip_tasks": skip_tasks,
            "use_gui_config": use_gui_config,
            "rl_theme": rl_theme,
            "rl_mode": rl_mode,
            "rl_squad": rl_squad,
            "rl_roles": rl_roles,
            "rl_core_char": rl_core_char,
            "rl_starts_count": rl_starts_count,
            "rl_ai_copilot": rl_ai_copilot,
            "rl_timeout_fallback": rl_timeout_fallback,
            "rl_timeout_sec": rl_timeout_sec,
            "rl_boss_prts": rl_boss_prts,
            "rl_claim_rewards": rl_claim_rewards,
            "rl_upgrade_tech": rl_upgrade_tech,
            "ra_theme": ra_theme,
            "ra_mode": ra_mode,
            "ra_tools": ra_tools_str,
            "ra_clear_store": ra_clear_store,
            "emu_type": emu_type,
            "adb_address": adb_address,
            "ai_mode": ai_mode,
            "base_url": base_url,
            "api_key": api_key,
            "model_name": model_name,
            "timeout_sec": timeout_sec,
            "webhook_enabled": webhook_enabled,
            "webhook_url": webhook_url,
            "notify_on_daily_finish": notify_on_daily_finish,
            "notify_on_recruit_alert": notify_on_recruit_alert,
            "notify_on_agent_task": notify_on_agent_task,
            # Bundles
            "config": cfg,
            "options": contract.get_options_bundle(),
        }
        return res

    def save_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Invalid payload format"}

        prev_settings = self.get_settings()

        # Ignore echoed config and options bundles
        payload = {k: v for k, v in payload.items() if k not in ("config", "options")}

        maa_input = str(payload.get("maa_path", "")).strip()
        if maa_input and maa_input != prev_settings.get("maa_path", ""):
            bind_res = self.engine.bind_maa_path(maa_input)
            if not bind_res.get("ok"):
                return {"ok": False, "error": bind_res.get("reason", "MAA 路径无效")}

        cfg = dict(self.engine.get_config())

        def _safe_int(v: Any, default: int = 0) -> int:
            try:
                return int(v)
            except Exception:
                return default

        def _safe_float(v: Any, default: float = 0.0) -> float:
            try:
                return float(v)
            except Exception:
                return default

        def _parse_list(val: Any) -> List[str]:
            if isinstance(val, (list, tuple, set)):
                return [str(x).strip() for x in val if str(x).strip()]
            return [x.strip() for x in str(val or "").replace("，", ",").split(",") if x.strip()]

        daily_cfg = cfg.setdefault("daily", {})
        fight_cfg = cfg.setdefault("fight", {})
        growth_cfg = cfg.setdefault("growth", {})
        rl_cfg = cfg.setdefault("roguelike", {})
        ra_cfg = cfg.setdefault("reclamation", {})
        emu_cfg = cfg.setdefault("emulator", {})
        ai_cfg = cfg.setdefault("ai", {})
        notif_cfg = cfg.setdefault("notifications", {})

        has_daily = "daily" in payload and isinstance(payload["daily"], dict)
        has_fight = "fight" in payload and isinstance(payload["fight"], dict)
        has_growth = "growth" in payload and isinstance(payload["growth"], dict)
        has_rl = "roguelike" in payload and isinstance(payload["roguelike"], dict)
        has_ra = "reclamation" in payload and isinstance(payload["reclamation"], dict)
        has_emu = "emulator" in payload and isinstance(payload["emulator"], dict)
        has_ai = "ai" in payload and isinstance(payload["ai"], dict)
        has_notif = "notifications" in payload and isinstance(payload["notifications"], dict)

        # 1. Apply nested section dicts
        if has_daily:
            d = payload["daily"]
            if "fight_mode" in d:
                daily_cfg["fight_mode"] = str(d["fight_mode"]).upper()
            if "override_stage" in d:
                daily_cfg["override_stage"] = str(d["override_stage"]).strip()
            if "medicine" in d:
                daily_cfg["medicine"] = max(0, _safe_int(d["medicine"]))
            if "stone" in d:
                daily_cfg["stone"] = max(0, _safe_int(d["stone"]))
            if "max_push_rounds" in d:
                daily_cfg["max_push_rounds"] = max(1, _safe_int(d["max_push_rounds"], 5))
            if "auto_push" in d:
                daily_cfg["auto_push"] = bool(d["auto_push"])
            if "recruit_preserve_tags" in d:
                daily_cfg["recruit_preserve_tags"] = _parse_list(d["recruit_preserve_tags"])
            if "recruit_times" in d:
                daily_cfg["recruit_times"] = max(1, _safe_int(d["recruit_times"], 4))
            if "penguin_id" in d:
                daily_cfg["penguin_id"] = str(d["penguin_id"]).strip()
            if "report_to_penguin" in d:
                daily_cfg["report_to_penguin"] = bool(d["report_to_penguin"])
            if "report_to_yituliu" in d:
                daily_cfg["report_to_yituliu"] = bool(d["report_to_yituliu"])
            if "skip_tasks" in d:
                daily_cfg["skip_tasks"] = _parse_list(d["skip_tasks"])
            for k, v in d.items():
                if k not in daily_cfg:
                    daily_cfg[k] = v

        if has_fight:
            f = payload["fight"]
            if "fight_mode" in f:
                fight_cfg["fight_mode"] = str(f["fight_mode"]).upper()
            if "override_stage" in f:
                fight_cfg["override_stage"] = str(f["override_stage"]).strip()
            if "medicine" in f:
                fight_cfg["medicine"] = max(0, _safe_int(f["medicine"]))
            if "stone" in f:
                fight_cfg["stone"] = max(0, _safe_int(f["stone"]))
            if "max_push_rounds" in f:
                fight_cfg["max_push_rounds"] = max(1, _safe_int(f["max_push_rounds"], 5))
            if "auto_push" in f:
                fight_cfg["auto_push"] = bool(f["auto_push"])
            for k, v in f.items():
                if k not in fight_cfg:
                    fight_cfg[k] = v

        if has_growth:
            g = payload["growth"]
            if "prefer_event" in g:
                growth_cfg["prefer_event"] = bool(g["prefer_event"])
            if "allow_chip_stages" in g:
                growth_cfg["allow_chip_stages"] = bool(g["allow_chip_stages"])
            if "allow_resource_stages" in g:
                growth_cfg["allow_resource_stages"] = bool(g["allow_resource_stages"])
            if "target_operators" in g:
                growth_cfg["target_operators"] = _parse_list(g["target_operators"])
            if "material_safety_stock" in g and isinstance(g["material_safety_stock"], dict):
                growth_cfg.setdefault("material_safety_stock", {}).update(g["material_safety_stock"])
            elif "t3_stock" in g:
                growth_cfg.setdefault("material_safety_stock", {})["T3_DEFAULT"] = max(0, _safe_int(g["t3_stock"], 30))
            for k, v in g.items():
                if k not in growth_cfg and k not in ("t3_stock",):
                    growth_cfg[k] = v

        if has_rl:
            r = payload["roguelike"]
            if "use_gui_config" in r:
                ugc = bool(r["use_gui_config"])
                rl_cfg["use_gui_config"] = ugc
                ra_cfg["use_gui_config"] = ugc
            if "theme" in r:
                rl_cfg["theme"] = str(r["theme"])
            if "mode" in r:
                rl_cfg["mode"] = str(r["mode"])
            if "squad" in r:
                rl_cfg["squad"] = str(r["squad"]).strip()
            if "roles" in r:
                rl_cfg["roles"] = str(r["roles"]).strip()
            if "core_char" in r:
                rl_cfg["core_char"] = str(r["core_char"]).strip()
            if "starts_count" in r:
                sc = r["starts_count"]
                rl_cfg["starts_count"] = _safe_int(sc) if sc not in (None, "") else None
            if "ai_copilot_enabled" in r:
                rl_cfg["ai_copilot_enabled"] = bool(r["ai_copilot_enabled"])
            if "ai_auto_fallback_on_timeout" in r:
                rl_cfg["ai_auto_fallback_on_timeout"] = bool(r["ai_auto_fallback_on_timeout"])
            if "ai_decision_timeout_sec" in r:
                rl_cfg["ai_decision_timeout_sec"] = _safe_float(r["ai_decision_timeout_sec"], 45.0)
            if "boss_prts_assist" in r:
                rl_cfg["boss_prts_assist"] = bool(r["boss_prts_assist"])
            if "auto_claim_rewards" in r:
                rl_cfg["auto_claim_rewards"] = bool(r["auto_claim_rewards"])
            if "auto_upgrade_tech_tree" in r:
                rl_cfg["auto_upgrade_tech_tree"] = bool(r["auto_upgrade_tech_tree"])
            for k, v in r.items():
                if k not in rl_cfg:
                    rl_cfg[k] = v

        if has_ra:
            ra = payload["reclamation"]
            if "use_gui_config" in ra:
                ugc = bool(ra["use_gui_config"])
                rl_cfg["use_gui_config"] = ugc
                ra_cfg["use_gui_config"] = ugc
            if "theme" in ra:
                ra_cfg["theme"] = str(ra["theme"])
            if "mode" in ra:
                ra_cfg["mode"] = str(ra["mode"])
            if "tools_to_craft" in ra:
                ra_cfg["tools_to_craft"] = _parse_list(ra["tools_to_craft"])
            if "clear_store" in ra:
                ra_cfg["clear_store"] = bool(ra["clear_store"])
            for k, v in ra.items():
                if k not in ra_cfg:
                    ra_cfg[k] = v

        if has_emu:
            em = payload["emulator"]
            if "type" in em:
                emu_cfg["type"] = str(em["type"])
            if "adb_address" in em:
                emu_cfg["adb_address"] = str(em["adb_address"]).strip()
            for k, v in em.items():
                if k not in emu_cfg:
                    emu_cfg[k] = v

        if has_ai:
            a = payload["ai"]
            if "mode" in a:
                m = str(a["mode"])
                ai_cfg["mode"] = m
                ai_cfg["enabled"] = (m == "builtin_api")
            if "base_url" in a:
                ai_cfg["base_url"] = str(a["base_url"]).strip() or "https://api.openai.com/v1"
            if "api_key" in a:
                ai_cfg["api_key"] = str(a["api_key"]).strip()
            if "model_name" in a:
                ai_cfg["model_name"] = str(a["model_name"]).strip() or "gpt-4o-mini"
            if "timeout_sec" in a:
                ai_cfg["timeout_sec"] = max(5, _safe_int(a["timeout_sec"], 30))
            for k, v in a.items():
                if k not in ai_cfg:
                    ai_cfg[k] = v

        if has_notif:
            n = payload["notifications"]
            if "webhook_enabled" in n:
                notif_cfg["webhook_enabled"] = bool(n["webhook_enabled"])
            if "webhook_url" in n:
                notif_cfg["webhook_url"] = str(n["webhook_url"]).strip()
            if "notify_on_daily_finish" in n:
                notif_cfg["notify_on_daily_finish"] = bool(n["notify_on_daily_finish"])
            if "notify_on_recruit_alert" in n:
                notif_cfg["notify_on_recruit_alert"] = bool(n["notify_on_recruit_alert"])
            if "notify_on_agent_task" in n:
                notif_cfg["notify_on_agent_task"] = bool(n["notify_on_agent_task"])
            for k, v in n.items():
                if k not in notif_cfg:
                    notif_cfg[k] = v

        # 2. Apply flat keys ONLY IF section was not provided OR flat key was explicitly mutated
        if "fight_mode" in payload:
            fm = str(payload["fight_mode"]).upper()
            if not has_daily or fm != prev_settings.get("fight_mode"):
                daily_cfg["fight_mode"] = fm
                fight_cfg["fight_mode"] = fm
        if "override_stage" in payload:
            os_val = str(payload["override_stage"]).strip()
            if not has_daily or os_val != prev_settings.get("override_stage"):
                daily_cfg["override_stage"] = os_val
                fight_cfg["override_stage"] = os_val
        if "medicine" in payload:
            med = max(0, _safe_int(payload["medicine"]))
            if not has_daily or med != prev_settings.get("medicine"):
                daily_cfg["medicine"] = med
                fight_cfg["medicine"] = med
        if "stone" in payload:
            st = max(0, _safe_int(payload["stone"]))
            if not has_daily or st != prev_settings.get("stone"):
                daily_cfg["stone"] = st
                fight_cfg["stone"] = st
        if "max_push_rounds" in payload:
            mpr = max(1, _safe_int(payload["max_push_rounds"], 5))
            if not has_daily or mpr != prev_settings.get("max_push_rounds"):
                daily_cfg["max_push_rounds"] = mpr
                fight_cfg["max_push_rounds"] = mpr
        if "auto_push" in payload:
            ap = bool(payload["auto_push"])
            if not has_daily or ap != prev_settings.get("auto_push"):
                daily_cfg["auto_push"] = ap
                fight_cfg["auto_push"] = ap
        if "recruit_preserve_tags" in payload:
            rpt = _parse_list(payload["recruit_preserve_tags"])
            if not has_daily or rpt != prev_settings.get("recruit_preserve_tags"):
                daily_cfg["recruit_preserve_tags"] = rpt
        if "recruit_times" in payload:
            rt = max(1, _safe_int(payload["recruit_times"], 4))
            if not has_daily or rt != prev_settings.get("recruit_times"):
                daily_cfg["recruit_times"] = rt
        if "penguin_id" in payload:
            pid = str(payload["penguin_id"]).strip()
            if not has_daily or pid != prev_settings.get("penguin_id"):
                daily_cfg["penguin_id"] = pid
        if "report_to_penguin" in payload:
            rtp = bool(payload["report_to_penguin"])
            if not has_daily or rtp != prev_settings.get("report_to_penguin"):
                daily_cfg["report_to_penguin"] = rtp
        if "report_to_yituliu" in payload:
            rty = bool(payload["report_to_yituliu"])
            if not has_daily or rty != prev_settings.get("report_to_yituliu"):
                daily_cfg["report_to_yituliu"] = rty
        if "skip_tasks" in payload:
            stk = _parse_list(payload["skip_tasks"])
            if not has_daily or stk != prev_settings.get("skip_tasks"):
                daily_cfg["skip_tasks"] = stk

        if "prefer_event" in payload:
            pe = bool(payload["prefer_event"])
            if not has_growth or pe != prev_settings.get("prefer_event"):
                growth_cfg["prefer_event"] = pe
        if "allow_chip_stages" in payload:
            acs = bool(payload["allow_chip_stages"])
            if not has_growth or acs != prev_settings.get("allow_chip_stages"):
                growth_cfg["allow_chip_stages"] = acs
        if "allow_resource_stages" in payload:
            ars = bool(payload["allow_resource_stages"])
            if not has_growth or ars != prev_settings.get("allow_resource_stages"):
                growth_cfg["allow_resource_stages"] = ars
        if "target_operators" in payload:
            to = _parse_list(payload["target_operators"])
            prev_to = _parse_list(prev_settings.get("target_operators", []))
            if not has_growth or to != prev_to:
                growth_cfg["target_operators"] = to
        if "t3_stock" in payload:
            t3 = max(0, _safe_int(payload["t3_stock"], 30))
            if not has_growth or t3 != prev_settings.get("t3_stock"):
                growth_cfg.setdefault("material_safety_stock", {})["T3_DEFAULT"] = t3

        if "use_gui_config" in payload:
            ugc = bool(payload["use_gui_config"])
            if (not has_rl and not has_ra) or ugc != prev_settings.get("use_gui_config"):
                rl_cfg["use_gui_config"] = ugc
                ra_cfg["use_gui_config"] = ugc
        if "rl_theme" in payload:
            rlt = str(payload["rl_theme"])
            if not has_rl or rlt != prev_settings.get("rl_theme"):
                rl_cfg["theme"] = rlt
        if "rl_mode" in payload:
            rlm = str(payload["rl_mode"])
            if not has_rl or rlm != prev_settings.get("rl_mode"):
                rl_cfg["mode"] = rlm
        if "rl_squad" in payload:
            rls = str(payload["rl_squad"]).strip()
            if not has_rl or rls != prev_settings.get("rl_squad"):
                rl_cfg["squad"] = rls
        if "rl_roles" in payload:
            rlr = str(payload["rl_roles"]).strip()
            if not has_rl or rlr != prev_settings.get("rl_roles"):
                rl_cfg["roles"] = rlr
        if "rl_core_char" in payload:
            rlc = str(payload["rl_core_char"]).strip()
            if not has_rl or rlc != prev_settings.get("rl_core_char"):
                rl_cfg["core_char"] = rlc
        if "rl_starts_count" in payload:
            raw_sc = payload["rl_starts_count"]
            sc_val = _safe_int(raw_sc) if raw_sc not in (None, "") else None
            if not has_rl or sc_val != prev_settings.get("rl_starts_count"):
                rl_cfg["starts_count"] = sc_val
        if "rl_ai_copilot" in payload:
            rac = bool(payload["rl_ai_copilot"])
            if not has_rl or rac != prev_settings.get("rl_ai_copilot"):
                rl_cfg["ai_copilot_enabled"] = rac
        if "rl_timeout_fallback" in payload:
            rtf = bool(payload["rl_timeout_fallback"])
            if not has_rl or rtf != prev_settings.get("rl_timeout_fallback"):
                rl_cfg["ai_auto_fallback_on_timeout"] = rtf
        if "rl_timeout_sec" in payload:
            rts = _safe_float(payload["rl_timeout_sec"], 45.0)
            if not has_rl or rts != prev_settings.get("rl_timeout_sec"):
                rl_cfg["ai_decision_timeout_sec"] = rts
        if "rl_boss_prts" in payload:
            rbp = bool(payload["rl_boss_prts"])
            if not has_rl or rbp != prev_settings.get("rl_boss_prts"):
                rl_cfg["boss_prts_assist"] = rbp
        if "rl_claim_rewards" in payload:
            rcr = bool(payload["rl_claim_rewards"])
            if not has_rl or rcr != prev_settings.get("rl_claim_rewards"):
                rl_cfg["auto_claim_rewards"] = rcr
        if "rl_upgrade_tech" in payload:
            rut = bool(payload["rl_upgrade_tech"])
            if not has_rl or rut != prev_settings.get("rl_upgrade_tech"):
                rl_cfg["auto_upgrade_tech_tree"] = rut

        if "ra_theme" in payload:
            rat = str(payload["ra_theme"])
            if not has_ra or rat != prev_settings.get("ra_theme"):
                ra_cfg["theme"] = rat
        if "ra_mode" in payload:
            ram = str(payload["ra_mode"])
            if not has_ra or ram != prev_settings.get("ra_mode"):
                ra_cfg["mode"] = ram
        if "ra_tools" in payload:
            ratools = _parse_list(payload["ra_tools"])
            prev_ratools = _parse_list(prev_settings.get("ra_tools", []))
            if not has_ra or ratools != prev_ratools:
                ra_cfg["tools_to_craft"] = ratools
        if "ra_clear_store" in payload:
            rcs = bool(payload["ra_clear_store"])
            if not has_ra or rcs != prev_settings.get("ra_clear_store"):
                ra_cfg["clear_store"] = rcs

        if "emu_type" in payload:
            et = str(payload["emu_type"])
            if not has_emu or et != prev_settings.get("emu_type"):
                emu_cfg["type"] = et
        if "adb_address" in payload:
            aa = str(payload["adb_address"]).strip()
            if not has_emu or aa != prev_settings.get("adb_address"):
                emu_cfg["adb_address"] = aa

        if "ai_mode" in payload:
            aim = str(payload["ai_mode"])
            if not has_ai or aim != prev_settings.get("ai_mode"):
                ai_cfg["mode"] = aim
                ai_cfg["enabled"] = (aim == "builtin_api")
        if "base_url" in payload:
            bu = str(payload["base_url"]).strip() or "https://api.openai.com/v1"
            if not has_ai or bu != prev_settings.get("base_url"):
                ai_cfg["base_url"] = bu
        if "api_key" in payload:
            ak = str(payload["api_key"]).strip()
            if not has_ai or ak != prev_settings.get("api_key"):
                ai_cfg["api_key"] = ak
        if "model_name" in payload:
            mn = str(payload["model_name"]).strip() or "gpt-4o-mini"
            if not has_ai or mn != prev_settings.get("model_name"):
                ai_cfg["model_name"] = mn
        if "timeout_sec" in payload:
            ts = max(5, _safe_int(payload["timeout_sec"], 30))
            if not has_ai or ts != prev_settings.get("timeout_sec"):
                ai_cfg["timeout_sec"] = ts

        if "webhook_enabled" in payload:
            we = bool(payload["webhook_enabled"])
            if not has_notif or we != prev_settings.get("webhook_enabled"):
                notif_cfg["webhook_enabled"] = we
        if "webhook_url" in payload:
            wu = str(payload["webhook_url"]).strip()
            if not has_notif or wu != prev_settings.get("webhook_url"):
                notif_cfg["webhook_url"] = wu
        if "notify_on_daily_finish" in payload:
            ndf = bool(payload["notify_on_daily_finish"])
            if not has_notif or ndf != prev_settings.get("notify_on_daily_finish"):
                notif_cfg["notify_on_daily_finish"] = ndf
        if "notify_on_recruit_alert" in payload:
            nra = bool(payload["notify_on_recruit_alert"])
            if not has_notif or nra != prev_settings.get("notify_on_recruit_alert"):
                notif_cfg["notify_on_recruit_alert"] = nra
        if "notify_on_agent_task" in payload:
            nat = bool(payload["notify_on_agent_task"])
            if not has_notif or nat != prev_settings.get("notify_on_agent_task"):
                notif_cfg["notify_on_agent_task"] = nat

        # 3. Synchronize cross-section mirror fields
        for k in ("fight_mode", "override_stage", "medicine", "stone", "max_push_rounds", "auto_push"):
            if k in daily_cfg:
                fight_cfg[k] = daily_cfg[k]
            elif k in fight_cfg:
                daily_cfg[k] = fight_cfg[k]
        if "use_gui_config" in rl_cfg:
            ra_cfg["use_gui_config"] = rl_cfg["use_gui_config"]
        elif "use_gui_config" in ra_cfg:
            rl_cfg["use_gui_config"] = ra_cfg["use_gui_config"]
        ai_cfg["enabled"] = (ai_cfg.get("mode") == "builtin_api")

        # Persist and update snapshots
        self.engine.save_config(cfg)
        self._append_log("全局设置已保存至 config/launcher_config.json。")
        if isinstance(self._snapshot_raw, dict):
            self._snapshot_raw["config"] = self.engine.get_config()
            self._snapshot_formatted = format_snapshot_payload(self._snapshot_raw)
        self._ui_queue.put(("snapshot_ready", dict(self._snapshot_raw)))
        self._refresh_snapshot_async(check_adb=False)
        return {"ok": True, "settings": self.get_settings(), "snapshot": self._snapshot_formatted}

    def sync_exploration_from_maa_gui(self, maa_path: str = "") -> Dict[str, Any]:
        p = maa_path.strip() if maa_path else None
        res = self.engine.sync_exploration_from_maa_gui(maa_dir=p, save=False)
        if not res.get("ok"):
            return res

        cfg = self.engine.get_config()
        cur_rl = dict(cfg.get("roguelike", {}) if isinstance(cfg.get("roguelike"), dict) else {})
        cur_ra = dict(cfg.get("reclamation", {}) if isinstance(cfg.get("reclamation"), dict) else {})
        if res.get("roguelike"):
            cur_rl.update(res["roguelike"])
        if res.get("reclamation"):
            cur_ra.update(res["reclamation"])

        self.engine.save_config({"roguelike": cur_rl, "reclamation": cur_ra})
        self._append_log(f"[设置同步] 已从原版 MAA 同步肉鸽与生息演算配置（来源：{res.get('source', '')}）")
        self._refresh_snapshot_async(check_adb=False)
        return res

    def test_webhook(self, webhook_url: str) -> Dict[str, Any]:
        url = str(webhook_url).strip()
        if not url:
            return {"ok": False, "sent": False, "error": "请先填写 Webhook URL"}
        res = maa_runner.send_webhook_notification(
            {"notifications": {"webhook_enabled": True, "webhook_url": url}},
            "MAA 启动器 Webhook 测试",
            "这是一条来自 MAA 通用智能自主控制台的测试消息。",
            event_type="test",
            force=True,
        )
        return res

    def test_ai_api(self, base_url: str, api_key: str, model_name: str) -> Dict[str, Any]:
        url = str(base_url).strip() or "https://api.openai.com/v1"
        key = str(api_key).strip()
        model = str(model_name).strip() or "gpt-4o-mini"
        client = ai_strategist.OpenAICompatClient(base_url=url, api_key=key, model_name=model, timeout_sec=12)
        ok, msg = client.test_connection()
        return {"ok": ok, "message": msg, "error": "" if ok else msg}

    # ---------- Onboarding Wizard Handlers ----------
    def wizard_scan_maa(self) -> Dict[str, Any]:
        found = maa_installer.resolve_maa_path(self.engine.config_path)
        if found is not None:
            integ = env_doctor.check_maa_integrity(found)
        else:
            integ = {
                "valid": False,
                "maa_dir": "",
                "maa_version": "",
                "reason": "未在同级、父级或常见目录检测到完整 MAA（缺少 MaaCore.dll 或 resource/）。",
            }
        self._ui_queue.put(("wizard_maa_scan", {
            "valid": bool(integ.get("valid")),
            "maa_dir": str(integ.get("maa_dir", "")),
            "maa_version": str(integ.get("maa_version") or integ.get("version_hint") or ""),
            "reason": str(integ.get("reason", "")),
        }))
        return integ

    def wizard_bind_maa(self, folder: str) -> Dict[str, Any]:
        res = self.engine.bind_maa_path(folder)
        if res.get("ok"):
            integ = res.get("integrity", {})
            self._ui_queue.put(("wizard_maa_scan", {
                "valid": bool(integ.get("valid")),
                "maa_dir": str(integ.get("maa_dir", "")),
                "maa_version": str(integ.get("maa_version") or integ.get("version_hint") or ""),
                "reason": str(integ.get("reason", "")),
            }))
            self._refresh_snapshot_async(check_adb=False)
        return res

    def wizard_start_download_maa(self) -> Dict[str, Any]:
        if self._downloading:
            return {"ok": False, "error": "MAA 正在下载中"}
        self._downloading = True
        self._dl_cancel_event.clear()

        def _prog(done: int, total: int, msg: str) -> None:
            ratio = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
            self._ui_queue.put(("wizard_dl_progress", {"done": done, "total": total, "msg": msg, "ratio": ratio}))

        def _worker() -> None:
            res = maa_installer.download_and_install_maa(
                target_dir=self.engine.base_dir / "MAA_Core",
                config_path=self.engine.config_path,
                progress_callback=_prog,
                cancel_event=self._dl_cancel_event,
            )
            self._downloading = False
            self._ui_queue.put(("wizard_dl_finished", {
                "ok": bool(res.get("ok")),
                "version": str(res.get("version", "")),
                "integrity": res.get("integrity", {}),
                "error": str(res.get("error", "")),
            }))
            if res.get("ok"):
                self._refresh_snapshot_async(check_adb=False)

        threading.Thread(target=_worker, daemon=True).start()
        return {"ok": True}

    def wizard_cancel_download_maa(self) -> Dict[str, Any]:
        self._dl_cancel_event.set()
        return {"ok": True}

    def wizard_run_diagnostics(self, emu_type: str, adb_address: str) -> Dict[str, Any]:
        addr = str(adb_address).strip() or "127.0.0.1:16384"
        self.engine.save_config({"emulator": {"type": emu_type, "adb_address": addr}})
        diag = self.engine.check_environment(check_adb=True)
        res = {
            "ok": True,
            "write_permission": diag.get("write_permission", {}),
            "maa_integrity": diag.get("maa_integrity", {}),
            "adb_resolution": diag.get("adb_resolution", {}),
            "base_dir": str(diag.get("base_dir", "")),
        }
        self._ui_queue.put(("wizard_diag_done", res))
        return res

    def wizard_launch_emulator(self, emu_type: str, adb_address: str) -> Dict[str, Any]:
        addr = str(adb_address).strip() or "127.0.0.1:16384"
        cfg = self.engine.save_config({"emulator": {"type": emu_type, "adb_address": addr}})
        try:
            maa_dir = maa_installer.resolve_maa_path(self.engine.config_path)
            maa_runner.ensure_emulator_ready(cfg, maa_dir=maa_dir, timeout_sec=60)
            diag = self.engine.check_environment(check_adb=True)
            res = {"ok": True, "error": "", "diag": diag}
        except Exception as exc:
            diag = self.engine.check_environment(check_adb=True)
            res = {"ok": False, "error": str(exc), "diag": diag}

        self._ui_queue.put(("wizard_launch_done", res))
        return res

    def wizard_finish(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        updates = dict(payload)
        updates["first_run_done"] = True
        if "ai_mode" in payload:
            mode = str(payload.get("ai_mode", "agent_collab") or "agent_collab")
            updates.setdefault("ai", {})["mode"] = mode
            updates["ai"]["enabled"] = (mode == "builtin_api")
        self.engine.save_config(updates)
        self._refresh_snapshot_async(check_adb=True)
        return {"ok": True}

    # ---------- Logs & Metrics ----------
    def get_logs(self) -> Dict[str, Any]:
        with self._lock:
            return {"ok": True, "logs": list(self._logs)}

    def clear_logs(self) -> Dict[str, Any]:
        with self._lock:
            self._logs.clear()
        return {"ok": True}

    def report_smoke_metrics(self, metrics: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "metrics": metrics}

    # ---------- Initial State Handshake ----------
    def get_initial_state(self) -> Dict[str, Any]:
        integ = self._snapshot_raw.get("integrity", {}) or {}
        needs_wizard = self._open_wizard_force or (not integ.get("valid"))
        return {
            "ok": True,
            "bridge_version": contract.BRIDGE_API_VERSION,
            "snapshot": self._snapshot_formatted,
            "task_state": self._last_task_state,
            "agent_banner": self.agent_banner_state,
            "tutorial_banner": self.tutorial_banner_state,
            "options": contract.get_options_bundle(),
            "logs": self.logs,
            "open_wizard": needs_wizard,
        }

    def refresh_snapshot(self, check_adb: bool = False) -> Dict[str, Any]:
        self._refresh_snapshot_async(check_adb=check_adb)
        return {"ok": True}

    # ---------- Report Log Formatters ----------
    def _log_roguelike_report(self, r: Dict[str, Any]) -> None:
        if r.get("error"):
            self._append_log(f"[ERR] 集成战略任务失败: {r.get('error')}")
            return
        m = r.get("metrics") or {}
        post = r.get("post_settlement") or {}
        rew = post.get("rewards") if isinstance(post, dict) else {}
        tech = post.get("tech_tree") if isinstance(post, dict) else {}
        claimed = (
            int((rew or {}).get("level_rewards_claimed", 0) or 0)
            + int((rew or {}).get("commission_rewards_claimed", 0) or 0)
        )
        upgraded = int((tech or {}).get("upgraded_count", 0) or 0)
        self._append_log(
            f"[肉鸽AI] 探索结束(耗时 {r.get('total_elapsed_seconds', 0)}s) | 主题={r.get('theme', '-')} | "
            f"探索={m.get('runs_started', 0)}局 | 结算={m.get('settlements', 0)}次 | 投资={m.get('investments', 0)} | "
            f"局后领奖={claimed}次 · 科技树加点={upgraded}次"
        )

    def _log_roguelike_post_report(self, r: Dict[str, Any]) -> None:
        if r.get("error"):
            self._append_log(f"[ERR] 肉鸽一键领奖与加点失败: {r.get('error')}")
            return
        rew = r.get("rewards") or {}
        tech = r.get("tech_tree") or {}
        self._append_log(
            f"[肉鸽AI] 一键领奖与加点结束(耗时 {r.get('total_elapsed_seconds', 0)}s) | 主题={r.get('theme_display_name') or r.get('theme', '-')} | "
            f"等级奖励={rew.get('level_rewards_claimed', 0)} · 月度委托={rew.get('commission_rewards_claimed', 0)} | "
            f"科技树加点={tech.get('upgraded_count', 0)}项"
        )

    def _log_reclamation_report(self, r: Dict[str, Any]) -> None:
        if r.get("cancelled"):
            self._append_log("[生息演算] 任务已响应停止指令，安全中止。")
            return
        if r.get("error"):
            self._append_log(f"[ERR] 生息演算任务失败: {r.get('error')}")
            return
        m = r.get("metrics") or {}
        stage_s = f" ({r.get('stage_code')})" if r.get("stage_code") else ""
        self._append_log(
            f"[生息演算] 托管结束(耗时 {r.get('total_elapsed_seconds', 0)}s) | 主题={r.get('theme', '-')} | "
            f"模式={r.get('mode_name') or r.get('mode_label', '-')}{stage_s} | 完成周期={m.get('cycles_completed', 0)} | "
            f"造物轮次={m.get('craft_rounds', 0)} | 搬空商店={m.get('store_cleared', False)}"
        )

    def _log_daily_report(self, r: Dict[str, Any]) -> None:
        if r.get("error"):
            self._append_log(f"[ERR] 日常收菜失败: {r.get('error')}")
            return
        tasks = r.get("tasks", {})
        fight = tasks.get("Fight", {})
        annih = tasks.get("Annihilation", {})
        award = tasks.get("Award", {})
        alerts = r.get("recruit_alerts", [])
        sel_stage = fight.get("selected_stage")
        rec_stage = fight.get("recommended_stage")
        runs = int(fight.get("runs_finished", 0) or 0)
        if sel_stage:
            stage_desc = f"{sel_stage} ({runs}场)"
            if rec_stage and rec_stage != sel_stage:
                stage_desc += f"[原推荐 {rec_stage}]"
        elif fight.get("cancelled") or r.get("cancelled"):
            stage_desc = f"已取消 ({runs}场)"
        elif fight:
            stage_desc = f"未完成 ({runs}场)"
        else:
            stage_desc = "跳过 (0场)"
        award_desc = "已领取" if award.get("claimed") else ("未领取" if award else "跳过")
        self._append_log(
            f"[每日战报] 收菜完成(耗时 {r.get('total_elapsed_seconds', 0)}s) | 区服={r.get('server')} | "
            f"刷图={stage_desc} | 领奖={award_desc} | "
            f"剿灭={annih.get('weekly_process') or annih.get('status', '-')} | "
            f"高星公招告警={len(alerts)}条"
        )
