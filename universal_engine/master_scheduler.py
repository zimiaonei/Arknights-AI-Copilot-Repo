"""Universal MAA backend engine facade and headless CLI scheduler."""

from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import sys as _sys
    from pathlib import Path as _Path

    _pkg_dir = _Path(__file__).resolve().parent
    _pkg_parent = str(_pkg_dir.parent)
    if _pkg_parent not in _sys.path:
        _sys.path.insert(0, _pkg_parent)
    __package__ = _pkg_dir.name

import argparse
import copy
import json
import os
import re
import sys
import threading
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import ai_strategist, auto_growth, roguelike_ai_copilot, stage_pilot
from .auto_growth import (
    _extract_active_event,
    decide_optimal_stages,
    is_data_stale,
    load_depot_data,
    sync_my_box,
)
from .maa_runner import (
    MaaSession,
    _probe_adb_address,
    _run_cmd,
    autonomous_clear_stage,
    build_reclamation_task_params,
    build_roguelike_task_params,
    capture_adb_screenshot,
    discover_adb_executable,
    ensure_emulator_ready,
    ensure_game_running,
    execute_post_action,
    get_server_spec,
    init_maa_sdk,
    inspect_maa_exploration_capabilities,
    probe_and_ensure_adb_ready,
    resolve_event_battle_stages_from_prts,
    resolve_reclamation_settings,
    resolve_roguelike_settings,
    run_reclamation_task,
    run_roguelike_task,
    run_task_with_recovery,
    send_webhook_notification,
    sync_exploration_from_maa_gui,
    wait_for_emulator_and_adb,
    write_agent_task,
)
from .utils.engine_lock import (
    EngineLockError,
    EngineTaskLock,
    format_lock_conflict_message,
    inspect_engine_lock,
    is_pid_alive,
    resolve_engine_lock_paths,
)
from .utils.env_doctor import (
    OperationCancelledError,
    _deep_merge,
    ensure_runtime_dirs,
    get_base_dir,
    load_launcher_config,
    run_env_diagnostics,
    save_launcher_config,
    wait_interruptible,
)
from .utils.maa_installer import (
    bind_manual_maa_path,
    check_maa_update,
    download_and_install_maa,
    resolve_maa_path,
    update_maa_core,
)

STAGE_ACTIVITY_URL = "https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json"
OTA_TASKS_URL = "https://api.maa.plus/MaaAssistantArknights/api/resource/tasks.json"


def _state_path(base_dir: Optional[Path] = None) -> Path:
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    return root / "data" / "scheduler_state.json"


def load_state(base_dir: Optional[Path] = None) -> Dict[str, Any]:
    sp = _state_path(base_dir)
    if sp.is_file():
        try:
            data = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"annihilation": {}, "last_daily": {}, "last_battle_is_annihilation": False}


def save_state(state: Dict[str, Any], base_dir: Optional[Path] = None) -> Path:
    sp = _state_path(base_dir)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return sp


def get_arknights_week_key(now: Optional[datetime] = None) -> str:
    """Weekly annihilation resets every Monday 04:00 (UTC+8)."""
    tz_cn = timezone(timedelta(hours=8))
    now_cn = (now or datetime.now(tz_cn)).astimezone(tz_cn)
    shifted = now_cn - timedelta(hours=4)
    iso = shifted.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def refresh_maa_cache(
    base_dir: Optional[Path] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Refresh StageActivityV2.json and OTA tasks.json into <base_dir>/cache/ with curl -4 fallback."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    cache_dir = root / "cache"
    targets = (
        (STAGE_ACTIVITY_URL, cache_dir / "gui" / "StageActivityV2.json"),
        (OTA_TASKS_URL, cache_dir / "resource" / "tasks" / "tasks.json"),
    )
    results: Dict[str, Any] = {}

    for url, dst in targets:
        if cancel_event is not None and cancel_event.is_set():
            results[dst.name] = {"ok": False, "cancelled": True, "using_local_cache": dst.is_file()}
            continue
        raw: Optional[str] = None
        last_err: Any = None
        for _ in range(2):
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "MAA-Universal-Engine/1.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    raw = resp.read().decode("utf-8")
                break
            except Exception as exc:
                last_err = exc
                if wait_interruptible(0.4, cancel_event=cancel_event, step=0.2):
                    break

        if raw is None and not (cancel_event is not None and cancel_event.is_set()):
            try:
                res = _run_cmd(
                    ["curl.exe", "-4", "-fsSL", "--max-time", "5", url],
                    timeout=6.0,
                    cancel_event=cancel_event,
                )
                if res.returncode == 0 and (res.stdout or "").strip():
                    raw = res.stdout
            except OperationCancelledError:
                results[dst.name] = {"ok": False, "cancelled": True, "using_local_cache": dst.is_file()}
                continue
            except Exception as exc:
                last_err = exc

        if raw is not None:
            try:
                json.loads(raw)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(raw, encoding="utf-8")
                results[dst.name] = {"ok": True, "path": str(dst)}
                continue
            except Exception as exc:
                last_err = exc

        results[dst.name] = {"ok": False, "error": str(last_err), "using_local_cache": dst.is_file()}

    return results


def _extract_fight_metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    sanity_before = None
    sanity_after = None
    runs_finished = 0
    annih_process = None
    latest_stats: List[Dict[str, Any]] = []
    stage_codes: List[str] = []
    matched_tasks: List[str] = []
    sub_error_firsts: List[str] = []
    log_text = "\n".join(res.get("log_lines", []))

    for ev in res.get("events", []):
        msg = ev.get("message", "")
        d = ev.get("details", {})
        if msg in ("SubTaskStart", "SubTaskCompleted"):
            det = d.get("details", {})
            if isinstance(det, dict) and det.get("task"):
                matched_tasks.append(str(det["task"]))
            continue
        if msg == "SubTaskError":
            first = d.get("first")
            if isinstance(first, list):
                sub_error_firsts.extend(str(x) for x in first)
            continue
        if msg != "SubTaskExtraInfo":
            continue
        what = d.get("what", "")
        det = d.get("details", {})
        if not isinstance(det, dict):
            continue
        if what == "SanityBeforeStage":
            cur = det.get("current_sanity")
            if sanity_before is None:
                sanity_before = cur
            sanity_after = cur
        elif what == "FightTimes":
            tf = det.get("times_finished")
            if isinstance(tf, int):
                runs_finished = max(runs_finished, tf)
        elif what == "StageDrops":
            sc = det.get("stage", {}).get("stageCode", "")
            if sc:
                stage_codes.append(sc)
            if det.get("annihilation_weekly_process"):
                annih_process = det["annihilation_weekly_process"]
            if det.get("stats"):
                latest_stats = det["stats"]
            runs_finished = max(runs_finished, len(stage_codes))

    m_weekly = re.findall(r"Annihilation weekly limit:\s*(\d+)\s*/\s*(\d+)", log_text)
    if m_weekly:
        annih_process = [int(m_weekly[-1][0]), int(m_weekly[-1][1])]

    weekly_limit_reached = (
        "Annihilation weekly limit reached" in log_text
        or any("AnnihilationWeeklyLimit" in t for t in matched_tasks)
        or (annih_process is not None and len(annih_process) == 2 and annih_process[0] >= annih_process[1])
    )
    annih_entry_missing = "AnnihilationEntryNotFound" in log_text
    out_of_sanity = (
        any(t in ("NoStone", "UseMedicine") for t in matched_tasks)
        or bool(re.search(r"PipelineAnalyzer::analyze.*Fight@(NoStone|UseMedicine)", log_text))
    )
    unable_to_agent = (
        runs_finished == 0
        and not weekly_limit_reached
        and not annih_entry_missing
        and (
            any("UnableToAgent" in t for t in matched_tasks)
            or bool(re.search(r"PipelineAnalyzer::analyze.*UnableToAgent", log_text))
        )
    )
    stage_not_found = res.get("early_stop_reason") == "stage_not_found"
    fight_begin_failed = (
        stage_not_found
        or ("FightBegin" in sub_error_firsts)
        or bool(res.get("chain_errors"))
        or (
            runs_finished == 0
            and not res.get("ok")
            and not out_of_sanity
            and not weekly_limit_reached
            and not unable_to_agent
            and ("LastBattleStageName" in sub_error_firsts)
        )
    )

    return {
        "sanity_before": sanity_before,
        "sanity_after": sanity_after,
        "runs_finished": runs_finished,
        "annihilation_weekly_process": annih_process,
        "stats": latest_stats,
        "stage_codes": stage_codes,
        "unable_to_agent": unable_to_agent,
        "weekly_limit_reached": weekly_limit_reached,
        "annih_entry_missing": annih_entry_missing,
        "out_of_sanity": out_of_sanity,
        "fight_begin_failed": fight_begin_failed,
        "stage_not_found": stage_not_found,
    }


def _detect_current_annihilation_stage(log_lines: List[str], maa_dir: Path) -> str:
    joined = "\n".join(log_lines)
    tile_dir = Path(maa_dir) / "resource" / "Arknights-Tile-Pos"
    if tile_dir.is_dir():
        camps = sorted(
            [fn for fn in os.listdir(tile_dir) if fn.startswith("camp_r_")],
            key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)],
            reverse=True,
        )
        for fn in camps:
            sid = fn.split("-")[0]
            if sid in joined:
                return sid
        if camps:
            return camps[0].split("-")[0]
    m = re.search(r"(camp_r_\d+)", joined)
    return m.group(1) if m else "camp_r_34"


def run_annihilation_pipeline(
    session_holder: Dict[str, MaaSession],
    config: Dict[str, Any],
    state: Dict[str, Any],
    force: bool = False,
    max_push_rounds: int = 5,
    base_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    manual_resume_event: Optional[threading.Event] = None,
    tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    tutorial_resume_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Weekly annihilation check, auto-unlock via copilot if needed, and sweep."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    week_key = get_arknights_week_key()
    cached = state.get("annihilation", {})
    if (
        not force
        and cached.get("week_key") == week_key
        and isinstance(cached.get("weekly_process"), list)
        and len(cached["weekly_process"]) == 2
        and cached["weekly_process"][0] >= cached["weekly_process"][1]
    ):
        return {
            "status": "already_1800_cached",
            "week_key": week_key,
            "weekly_process": cached["weekly_process"],
            "runs_finished": 0,
        }

    spec = get_server_spec(config)
    fight_params = {
        "stage": "Annihilation",
        "medicine": 0,
        "medicine_expire_days": 0,
        "stone": 0,
        "times": 2147483647,
        "series": 0,
        "DrGrandet": False,
        "report_to_penguin": False,
        "report_to_yituliu": False,
        "server": spec["maa_server"],
        "client_type": spec["client_type"],
    }

    res = run_task_with_recovery(session_holder, "Fight", fight_params, config, timeout_sec=2400)
    if res.get("network_recovery_failed") or session_holder.get("network_recovery_failed"):
        return {
            "status": "network_recovery_failed",
            "week_key": week_key,
            "runs_finished": 0,
            "ok": False,
            "network_recovery_failed": True,
            "error": str(res.get("error") or session_holder.get("network_recovery_error") or "[NET][自愈失败] 剿灭期间网络掉线自愈失败"),
        }
    fm = _extract_fight_metrics(res)

    if fm["unable_to_agent"]:
        cur_sess = session_holder["session"]
        maa_dir = getattr(cur_sess, "maa_dir", None)
        ev_cb = getattr(cur_sess, "event_callback", None)
        log_cb = getattr(cur_sess, "log_callback", None)
        cancel_ev = getattr(cur_sess, "cancel_event", None)

        annih_stage = _detect_current_annihilation_stage(res.get("log_lines", []), maa_dir)
        cur_sess.close()

        ok_push, push_summary = autonomous_clear_stage(
            annih_stage,
            maa_dir=maa_dir,
            config=config,
            max_rounds=max_push_rounds,
            base_dir=root,
            progress_callback=progress_callback,
            log_callback=log_callback or log_cb,
            cancel_event=cancel_ev,
            collab_handler=collab_handler,
            manual_resume_event=manual_resume_event,
            tutorial_handler=tutorial_handler,
            tutorial_resume_event=tutorial_resume_event,
        )

        new_sess = MaaSession(
            maa_dir=maa_dir, config=config, base_dir=root,
            event_callback=ev_cb, log_callback=log_cb, cancel_event=cancel_ev,
        )
        if not new_sess.connect():
            session_holder["session_lost"] = True
            return {
                "status": "reconnect_failed_after_unlock",
                "week_key": week_key,
                "unlock_ok": ok_push,
                "unlock_summary": push_summary,
                "session_lost": True,
            }
        session_holder["session"] = new_sess
        new_sess.run_single_task("StartUp", {"start_game_enabled": True, "client_type": spec["client_type"]}, timeout_sec=180)

        if ok_push:
            res2 = run_task_with_recovery(session_holder, "Fight", fight_params, config, timeout_sec=2400)
            fm2 = _extract_fight_metrics(res2)
            proc = fm2["annihilation_weekly_process"] or ([1800, 1800] if fm2["weekly_limit_reached"] else None)
            if proc:
                state["annihilation"] = {
                    "week_key": week_key,
                    "weekly_process": proc,
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                state["last_battle_is_annihilation"] = True
                save_state(state, base_dir=root)
            return {
                "status": "unlocked_and_swept",
                "week_key": week_key,
                "unlock_summary": push_summary,
                "weekly_process": proc,
                "runs_finished": fm2["runs_finished"],
                "stats": fm2["stats"],
            }
        return {
            "status": "unlock_failed",
            "week_key": week_key,
            "unlock_summary": push_summary,
        }

    proc = fm["annihilation_weekly_process"]
    if fm["weekly_limit_reached"] or fm["annih_entry_missing"]:
        proc = [1800, 1800]

    if fm["runs_finished"] > 0:
        state["last_battle_is_annihilation"] = True
    if proc:
        state["annihilation"] = {
            "week_key": week_key,
            "weekly_process": proc,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_state(state, base_dir=root)

    status = "completed_1800" if (proc and proc[0] >= 1800) else ("swept_partial_sanity" if fm["runs_finished"] > 0 else "checked")
    return {
        "status": status,
        "week_key": week_key,
        "weekly_process": proc,
        "runs_finished": fm["runs_finished"],
        "sanity_before": fm["sanity_before"],
        "sanity_after": fm["sanity_after"],
        "stats": fm["stats"],
        "ok": res.get("ok", False),
    }


_PERMANENT_STAGE_PREFIXES = frozenset({"PR", "CE", "LS", "AP", "SK", "CA", "OF"})


def _extract_event_prefix(stage_code: str) -> str:
    """Return SideStory event prefix like 'TL-' for 'TL-9', or '' for main/resource/chip stages."""
    s = str(stage_code or "").strip().upper()
    m = re.match(r"^([A-Z]{2,6})-(\d+)$", s)
    if not m:
        return ""
    pref = m.group(1)
    if pref in _PERMANENT_STAGE_PREFIXES:
        return ""
    return f"{pref}-"


def _resolve_chapter_battle_stages(
    prefix: str,
    maa_dir: Optional[Path],
    base_dir: Optional[Path] = None,
) -> List[str]:
    """Return naturally ordered normal battle stages for prefix from <maa_dir>/resource/stages.json with PRTS fallback."""
    if not prefix:
        return []
    pref_up = prefix.strip().upper().rstrip("-") + "-"
    if maa_dir is not None:
        stages_file = Path(maa_dir) / "resource" / "stages.json"
        if stages_file.is_file():
            try:
                all_stages = json.loads(stages_file.read_text(encoding="utf-8"))
                matched: List[str] = []
                for st in all_stages:
                    if not isinstance(st, dict):
                        continue
                    code = str(st.get("code", "") or "").strip()
                    sid = str(st.get("stageId", "") or "")
                    if not code.upper().startswith(pref_up) or "#f#" in sid.lower():
                        continue
                    if stage_pilot.classify_stage_node(code) != "battle":
                        continue
                    if re.search(r"-(?:EX|MO|S)-\d+$", code, re.IGNORECASE):
                        continue
                    raw_ap = st.get("apCost")
                    if raw_ap is not None and int(raw_ap) <= 0:
                        continue
                    if code not in matched:
                        matched.append(code)
                matched.sort(key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)])
                if matched:
                    return matched
            except Exception:
                pass
    try:
        online = resolve_event_battle_stages_from_prts(prefix, base_dir=base_dir)
        if online:
            return online
    except Exception:
        pass
    return auto_growth.build_dynamic_event_stage_queue(prefix)


def _infrast_needs_reception_fallback(res: Dict[str, Any]) -> bool:
    """Return True if Reception clue quick-insert/unlock fallback should run after MAA Infrast."""
    unlock_completed = False
    unlock_fake_or_err = False
    for ev in res.get("events", []):
        m = ev.get("message", "")
        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
        tname = str(det.get("task", "") or "")
        if m == "SubTaskCompleted" and tname == "UnlockClues":
            unlock_completed = True
        elif m in ("SubTaskStart", "SubTaskCompleted") and tname == "UnlockCluesIsFake":
            unlock_fake_or_err = True
        elif m == "SubTaskError":
            first = d.get("first")
            if isinstance(first, list) and "UnlockClues" in first:
                unlock_fake_or_err = True
    log_text = "\n".join(res.get("log_lines", []))
    skipped_quick_insert = bool(re.search(r"vacancy_cnt:\s*[1-9]\d*\s*,\s*available:\s*[1-9]\d*", log_text))
    if skipped_quick_insert:
        return True
    if unlock_completed and not unlock_fake_or_err:
        return False
    return True


def _build_manual_fight_targets(override_stage: str, skip_last_battle: bool) -> Tuple[List[str], str]:
    if override_stage:
        st = override_stage.strip()
        targets = [st] if st == "1-7" else [st, "1-7"]
        return targets, f"指定关卡 {st}（未解锁或失败自动回退 1-7）"
    if skip_last_battle:
        return ["1-7"], "上一次作战为已满剿灭，跳过 <LastBattle> 直接刷 1-7"
    return ["", "1-7"], "默认策略：优先 <LastBattle>，不可用则回退 1-7"


def run_sanity_fight_pipeline(
    session_holder: Dict[str, MaaSession],
    config: Dict[str, Any],
    state: Optional[Dict[str, Any]] = None,
    candidate_stages: Optional[List[str]] = None,
    reason: str = "",
    override_stage: str = "",
    medicine: int = 0,
    stone: int = 0,
    times: int = 2147483647,
    auto_push: bool = False,
    max_push_rounds: int = 5,
    base_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    manual_resume_event: Optional[threading.Event] = None,
    tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    tutorial_resume_event: Optional[threading.Event] = None,
    daily_mode: bool = False,
    stage_locate_timeout_sec: float = 45.0,
    max_swipe_to_stage: int = 12,
) -> Dict[str, Any]:
    """Execute sanity farming across candidate stages with fast unlock-skip, frontier push, and 1-7 fallback."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    skip_last = bool(state and state.get("last_battle_is_annihilation"))
    if candidate_stages is not None:
        targets = list(candidate_stages)
        if skip_last:
            targets = [t for t in targets if t != ""]
        if "1-7" not in targets:
            targets.append("1-7")
        strategy_reason = reason or "AUTO 决策候选队列"
    else:
        targets, strategy_reason = _build_manual_fight_targets(override_stage, skip_last)

    daily_cfg = config.get("daily", {}) if isinstance(config.get("daily"), dict) else {}
    spec = get_server_spec(config)
    penguin_id = str(daily_cfg.get("penguin_id", "") or "")
    report_penguin = bool(daily_cfg.get("report_to_penguin", False))
    report_yituliu = bool(daily_cfg.get("report_to_yituliu", False))
    recommended_stage = (targets[0] if targets[0] else "<LastBattle>") if targets else None

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
                return
            except Exception:
                pass
        cur_s = session_holder.get("session")
        if cur_s is not None and hasattr(cur_s, "_emit_log"):
            try:
                cur_s._emit_log(msg)
            except Exception:
                pass

    def _is_cancelled() -> bool:
        cur_s = session_holder.get("session")
        cev = getattr(cur_s, "cancel_event", None)
        if cev is None:
            return False
        is_set_fn = getattr(cev, "is_set", None)
        return bool(callable(is_set_fn) and is_set_fn() is True)

    attempted: List[Dict[str, Any]] = []
    locked_event_prefixes: set[str] = set()
    jump_to_fallback_1_7 = False

    for target_idx, stage in enumerate(targets):
        if _is_cancelled():
            return {
                "selected_stage": None,
                "recommended_stage": recommended_stage,
                "strategy": strategy_reason,
                "attempted": attempted,
                "runs_finished": 0,
                "stats": [],
                "cancelled": True,
            }

        if jump_to_fallback_1_7 and stage != "1-7":
            continue

        ev_pref = _extract_event_prefix(stage)
        if ev_pref and ev_pref in locked_event_prefixes:
            _log(f"[FIGHT] 跳过未解锁活动系列关卡 {stage}，顺延尝试后续已解锁关卡。")
            continue

        stage_label = stage if stage else "<LastBattle>"
        params = {
            "stage": stage,
            "medicine": medicine,
            "medicine_expire_days": 0,
            "stone": stone,
            "times": times,
            "series": 0,
            "DrGrandet": False,
            "report_to_penguin": report_penguin,
            "penguin_id": penguin_id,
            "report_to_yituliu": report_yituliu,
            "yituliu_id": penguin_id,
            "server": spec["maa_server"],
            "client_type": spec["client_type"],
        }

        max_stage_attempts = 2 if (stage not in ("", "1-7")) else 1
        res: Dict[str, Any] = {}
        fm: Dict[str, Any] = {}
        is_effective_done = False

        for stage_try in range(1, max_stage_attempts + 1):
            if _is_cancelled():
                return {
                    "selected_stage": None,
                    "recommended_stage": recommended_stage,
                    "strategy": strategy_reason,
                    "attempted": attempted,
                    "runs_finished": 0,
                    "stats": [],
                    "cancelled": True,
                }

            tick_cb: Optional[Callable[[MaaSession, List[Dict[str, Any]]], None]] = None
            if stage not in ("", "1-7") and (stage_locate_timeout_sec > 0 or max_swipe_to_stage > 0):
                fight_t0 = time.time()
                locate_state = {"entered": False, "swipes": 0}

                def _fight_locate_tick(sess: MaaSession, batch_evs: List[Dict[str, Any]]) -> None:
                    for ev in batch_evs:
                        m = ev.get("message", "")
                        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
                        what = d.get("what", "")
                        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
                        tname = str(det.get("task", "") or "")
                        if m == "SubTaskExtraInfo" and what in ("SanityBeforeStage", "StageDrops", "FightTimes"):
                            locate_state["entered"] = True
                        elif m in ("SubTaskStart", "SubTaskCompleted"):
                            if any(
                                k in tname
                                for k in ("SideStoryStage", "UsePrts", "StartButton1", "StartButton2", "BattleOfficiallyBegin")
                            ):
                                locate_state["entered"] = True
                            elif tname == "SwipeToStage":
                                ex = det.get("exec_times")
                                if isinstance(ex, int):
                                    locate_state["swipes"] = max(locate_state["swipes"], ex)
                                else:
                                    locate_state["swipes"] += 1
                    if locate_state["entered"]:
                        return
                    elapsed_loc = time.time() - fight_t0
                    too_many_swipes = max_swipe_to_stage > 0 and locate_state["swipes"] >= max_swipe_to_stage
                    too_long = stage_locate_timeout_sec > 0 and elapsed_loc >= stage_locate_timeout_sec
                    if too_many_swipes or too_long:
                        _log(
                            f"[FIGHT] 关卡 {stage_label} 寻关超时/未解锁"
                            f"（已滑动 {locate_state['swipes']} 次，耗时 {round(elapsed_loc, 1)}s），提前中止等待。"
                        )
                        if hasattr(sess, "request_early_stop"):
                            sess.request_early_stop("stage_not_found")

                tick_cb = _fight_locate_tick

            res = run_task_with_recovery(
                session_holder,
                "Fight",
                params,
                config,
                timeout_sec=1800,
                max_retries=0,
                tick_callback=tick_cb,
            )
            if res.get("cancelled") or _is_cancelled():
                return {
                    "selected_stage": None,
                    "recommended_stage": recommended_stage,
                    "strategy": strategy_reason,
                    "attempted": attempted,
                    "runs_finished": 0,
                    "stats": [],
                    "cancelled": True,
                }
            if res.get("network_recovery_failed") or session_holder.get("network_recovery_failed"):
                return {
                    "selected_stage": None,
                    "recommended_stage": recommended_stage,
                    "strategy": strategy_reason,
                    "attempted": attempted,
                    "runs_finished": 0,
                    "stats": [],
                    "ok": False,
                    "session_lost": True,
                    "network_recovery_failed": True,
                    "error": str(res.get("error") or session_holder.get("network_recovery_error") or "[NET][自愈失败] 刷图期间网络掉线自愈失败"),
                }

            fm = _extract_fight_metrics(res)
            is_effective_done = (
                (res.get("ok") and not fm["weekly_limit_reached"])
                or (fm["runs_finished"] > 0 and not fm["weekly_limit_reached"])
                or fm["out_of_sanity"]
            )
            if is_effective_done:
                break

            is_stage_locked = bool(fm.get("fight_begin_failed") or fm.get("stage_not_found"))

            # Without auto_push: NEVER retry locked/missing stage; immediately skip locked event stages.
            if not auto_push:
                if is_stage_locked and ev_pref:
                    cur_s = session_holder.get("session")
                    adb_p = getattr(cur_s, "adb_path", "") or ""
                    adb_a = getattr(cur_s, "adb_addr", "") or ""
                    maa_d = getattr(cur_s, "maa_dir", None)
                    other_same_pref = [
                        t.upper() for t in targets[target_idx + 1 :] if _extract_event_prefix(t) == ev_pref
                    ]
                    skip_entire_prefix = True
                    if other_same_pref and isinstance(adb_p, str) and adb_p and isinstance(adb_a, str) and adb_a and maa_d:
                        try:
                            f_info = stage_pilot.detect_map_frontier_stage(
                                ev_pref,
                                adb_p,
                                adb_a,
                                Path(maa_d),
                                target_stage=stage,
                                max_swipes=0,
                                log_callback=_log,
                            )
                            cleared_vis = {
                                str(n["code"]).upper()
                                for n in f_info.get("visible_nodes", [])
                                if n.get("cleared")
                            }
                            if any(code in cleared_vis for code in other_same_pref):
                                skip_entire_prefix = False
                        except Exception:
                            pass
                    if skip_entire_prefix:
                        locked_event_prefixes.add(ev_pref)
                        _log(f"[FIGHT] 活动关 {stage_label} 未解锁且未开启自动推图，立即跳过所有未解锁 {ev_pref}* 活动关。")
                if is_stage_locked or fm.get("unable_to_agent") or daily_mode:
                    break
                continue

            # With auto_push=True:
            if stage and auto_push and stage_try == 1:
                cur_sess = session_holder["session"]
                maa_dir = getattr(cur_sess, "maa_dir", None)
                ev_cb = getattr(cur_sess, "event_callback", None)
                log_cb = getattr(cur_sess, "log_callback", None)
                cancel_ev = getattr(cur_sess, "cancel_event", None)
                if not (cancel_ev is not None and callable(getattr(cancel_ev, "is_set", None)) and isinstance(cancel_ev.is_set(), bool)):
                    cancel_ev = None
                adb_path = getattr(cur_sess, "adb_path", "") or ""
                adb_addr = getattr(cur_sess, "adb_addr", "") or ""
                cur_sess.close()

                ok_push = False
                push_cancelled = False
                push_blocked_daily = False

                if is_stage_locked:
                    # NEVER bare-run Copilot on an unlocked target stage: detect frontier first and push sequentially.
                    pref = ev_pref or (stage.rsplit("-", 1)[0] + "-" if "-" in stage else "")
                    chapter_battles = _resolve_chapter_battle_stages(pref, Path(maa_dir) if maa_dir else None, base_dir=root)
                    frontier_info: Dict[str, Any] = {
                        "on_stage_map": False,
                        "frontier_stage": "",
                        "frontier_type": "",
                        "push_sequence": [],
                    }
                    if pref and isinstance(adb_path, str) and adb_path and isinstance(adb_addr, str) and adb_addr and maa_dir:
                        try:
                            frontier_info = stage_pilot.detect_map_frontier_stage(
                                pref,
                                adb_path,
                                adb_addr,
                                Path(maa_dir),
                                target_stage=stage,
                                known_battle_stages=chapter_battles,
                                max_swipes=2,
                                cancel_event=cancel_ev,
                                log_callback=log_callback or log_cb,
                            )
                        except Exception:
                            pass

                    push_seq = list(frontier_info.get("push_sequence") or [])
                    if not frontier_info.get("on_stage_map") or not push_seq:
                        _log(
                            f"[PUSH] 关卡 {stage_label} 未解锁且未能在地图检测到已解锁前沿关卡，禁止对终关裸跑 Copilot。"
                        )
                        push_blocked_daily = daily_mode
                    elif daily_mode and frontier_info.get("frontier_type") == "tutorial":
                        _log(
                            f"[PUSH] 日常模式检测到前沿关卡为未完成教学关 {frontier_info.get('frontier_stage')}，"
                            f"立即中止推图并回退至 1-7 清空理智。"
                        )
                        push_blocked_daily = True
                    else:
                        detected_frontier = str(frontier_info.get("frontier_stage") or "")
                        passed_specials = {
                            str(n.get("code", "")).upper()
                            for n in (frontier_info.get("visible_nodes") or [])
                            if n.get("node_type") in ("story", "tutorial")
                            and (
                                n.get("cleared")
                                or str(n.get("code", "")).upper() != detected_frontier.upper()
                            )
                        }
                        _log(f"[PUSH] 检测到已解锁前沿 {detected_frontier}，开始逐关推进: {push_seq}")
                        ok_push = True
                        for idx_p, pst in enumerate(push_seq):
                            if cancel_ev and cancel_ev.is_set():
                                push_cancelled = True
                                ok_push = False
                                break
                            # Check blocking -ST-/-TR- prerequisites before each combat node
                            if stage_pilot.classify_stage_node(pst) == "battle" and pref and adb_path and adb_addr and maa_dir:
                                try:
                                    prereqs = stage_pilot.discover_unlocked_special_prereqs(
                                        pref,
                                        adb_path,
                                        adb_addr,
                                        Path(maa_dir),
                                        target_combat_stage=pst,
                                        frontier_stage=(detected_frontier if idx_p == 0 else ""),
                                        max_swipes=2,
                                        cancel_event=cancel_ev,
                                    )
                                except Exception:
                                    prereqs = []
                                prereqs = [p for p in prereqs if str(p).upper() not in passed_specials]
                                for pre_st in prereqs:
                                    if stage_pilot.classify_stage_node(pre_st) == "tutorial" and daily_mode:
                                        _log(f"[PUSH] 日常模式遇教学关 {pre_st} 阻塞，立即中止推图并回退 1-7。")
                                        push_blocked_daily = True
                                        ok_push = False
                                        break
                                    ok_pre, sum_pre = autonomous_clear_stage(
                                        pre_st,
                                        maa_dir=maa_dir,
                                        config=config,
                                        max_rounds=max_push_rounds,
                                        base_dir=root,
                                        progress_callback=progress_callback,
                                        log_callback=log_callback or log_cb,
                                        cancel_event=cancel_ev,
                                        collab_handler=collab_handler,
                                        manual_resume_event=manual_resume_event,
                                        next_stage=pst,
                                        tutorial_handler=tutorial_handler,
                                        tutorial_resume_event=tutorial_resume_event,
                                        daily_mode=daily_mode,
                                        skip_if_already_3star=True,
                                    )
                                    if sum_pre.get("cancelled"):
                                        push_cancelled = True
                                        ok_push = False
                                        break
                                    if not ok_pre:
                                        ok_push = False
                                        if daily_mode:
                                            push_blocked_daily = True
                                        break
                                if not ok_push:
                                    break

                            nxt_p = push_seq[idx_p + 1] if idx_p + 1 < len(push_seq) else None
                            ok_step, sum_step = autonomous_clear_stage(
                                pst,
                                maa_dir=maa_dir,
                                config=config,
                                max_rounds=max_push_rounds,
                                base_dir=root,
                                progress_callback=progress_callback,
                                log_callback=log_callback or log_cb,
                                cancel_event=cancel_ev,
                                collab_handler=collab_handler,
                                manual_resume_event=manual_resume_event,
                                next_stage=nxt_p,
                                tutorial_handler=tutorial_handler,
                                tutorial_resume_event=tutorial_resume_event,
                                daily_mode=daily_mode,
                                skip_if_already_3star=True,
                                require_fresh_map_entry=(idx_p > 0),
                            )
                            if ok_step and sum_step.get("map_clear_verified") is False:
                                ok_step = False
                                sum_step["victory"] = False
                                sum_step.setdefault("battle_failed", True)
                                sum_step.setdefault("error", "map_clear_verify_failed")
                            if sum_step.get("cancelled"):
                                push_cancelled = True
                                ok_push = False
                                break
                            if not ok_step:
                                ok_push = False
                                if daily_mode:
                                    push_blocked_daily = True
                                break
                else:
                    # Stage IS unlocked (e.g. UnableToAgent: missing 3-star auto-deploy)
                    ok_push, sum_push = autonomous_clear_stage(
                        stage,
                        maa_dir=maa_dir,
                        config=config,
                        max_rounds=max_push_rounds,
                        base_dir=root,
                        progress_callback=progress_callback,
                        log_callback=log_callback or log_cb,
                        cancel_event=cancel_ev,
                        collab_handler=collab_handler,
                        manual_resume_event=manual_resume_event,
                        tutorial_handler=tutorial_handler,
                        tutorial_resume_event=tutorial_resume_event,
                        daily_mode=daily_mode,
                        skip_if_already_3star=False,
                    )
                    if sum_push.get("cancelled"):
                        push_cancelled = True
                    elif not ok_push and daily_mode:
                        push_blocked_daily = True

                if push_cancelled or (cancel_ev and cancel_ev.is_set()):
                    return {
                        "selected_stage": None,
                        "recommended_stage": recommended_stage,
                        "strategy": strategy_reason,
                        "attempted": attempted,
                        "runs_finished": 0,
                        "stats": [],
                        "cancelled": True,
                    }

                new_sess = MaaSession(
                    maa_dir=maa_dir, config=config, base_dir=root,
                    event_callback=ev_cb, log_callback=log_cb, cancel_event=cancel_ev,
                )
                if new_sess.connect():
                    session_holder["session"] = new_sess
                    new_sess.run_single_task("StartUp", {"start_game_enabled": True, "client_type": spec["client_type"]}, timeout_sec=180)
                    if ok_push:
                        continue
                    if push_blocked_daily:
                        _log("[FIGHT] 日常模式推图受阻，已立即中止推图并回退至 1-7 清空理智。")
                        jump_to_fallback_1_7 = True
                        if ev_pref:
                            locked_event_prefixes.add(ev_pref)
                    break
                session_holder["session_lost"] = True
                return {
                    "selected_stage": None,
                    "recommended_stage": recommended_stage,
                    "strategy": strategy_reason,
                    "attempted": attempted,
                    "runs_finished": 0,
                    "stats": [],
                    "session_lost": True,
                }

        attempted.append({
            "stage": stage_label,
            "ok": res.get("ok", False),
            "runs_finished": fm.get("runs_finished", 0),
            "sanity_before": fm.get("sanity_before"),
            "sanity_after": fm.get("sanity_after"),
            "out_of_sanity": fm.get("out_of_sanity", False),
            "weekly_limit_reached": fm.get("weekly_limit_reached", False),
            "unable_to_agent": fm.get("unable_to_agent", False),
            "stage_not_found": fm.get("stage_not_found", False),
            "fight_begin_failed": fm.get("fight_begin_failed", False),
            "stats": fm.get("stats", []),
            "elapsed": res.get("elapsed", 0.0),
        })

        if is_effective_done:
            if state is not None:
                state["last_battle_is_annihilation"] = False
                save_state(state, base_dir=root)
            reported_codes = fm.get("stage_codes") or []
            actual_stage = str(reported_codes[-1]) if (reported_codes and (not stage or fm.get("runs_finished", 0) > 0)) else stage_label
            return {
                "selected_stage": actual_stage,
                "recommended_stage": recommended_stage,
                "strategy": strategy_reason,
                "attempted": attempted,
                "runs_finished": fm.get("runs_finished", 0),
                "sanity_before": fm.get("sanity_before"),
                "sanity_after": fm.get("sanity_after"),
                "out_of_sanity": fm.get("out_of_sanity", False),
                "stats": fm.get("stats", []),
            }

        if fm.get("weekly_limit_reached") and state is not None:
            state["last_battle_is_annihilation"] = True
            save_state(state, base_dir=root)

    return {
        "selected_stage": None,
        "recommended_stage": recommended_stage,
        "strategy": strategy_reason,
        "attempted": attempted,
        "runs_finished": 0,
        "stats": [],
    }


def _summarize_task_logs(res: Dict[str, Any], task_type: str) -> Dict[str, Any]:
    logs = [l for l in res.get("log_lines", []) if "[TRC]" not in l]
    summary: Dict[str, Any] = {"ok": res.get("ok", False), "elapsed": res.get("elapsed", 0.0)}

    if task_type == "Recruit":
        detected = []
        refreshed = 0
        for ev in res.get("events", []):
            if ev.get("message") == "SubTaskExtraInfo":
                w = ev.get("details", {}).get("what")
                det = ev.get("details", {}).get("details", {})
                if w == "RecruitTagsDetected":
                    detected.append(det.get("tags", []))
                elif w == "RecruitTagsRefreshed":
                    refreshed += 1
        summary.update({"slots_detected": detected, "refreshed": refreshed})
    elif task_type == "Infrast":
        facilities = []
        for ev in res.get("events", []):
            if ev.get("message") == "SubTaskExtraInfo" and ev.get("details", {}).get("what") == "EnterFacility":
                det = ev.get("details", {}).get("details", {})
                facilities.append(f"{det.get('facility')}#{det.get('index')}")
        summary["facilities"] = facilities
    elif task_type == "Mall":
        bought = [l.strip()[-120:] for l in logs if any(k in l for k in ("Buy", "Mall", "Credit", "购买", "信用")) and "[ERR]" not in l]
        summary["highlights"] = bought[-10:]
    elif task_type == "Award":
        summary["claimed"] = res.get("ok", False)

    return summary


def _resolve_auto_push_stages(maa_dir: Path, base_dir: Path, server: str) -> List[str]:
    act_file = base_dir / "cache" / "gui" / "StageActivityV2.json"
    if not act_file.is_file():
        act_file = maa_dir / "cache" / "gui" / "StageActivityV2.json"
    stages_file = maa_dir / "resource" / "stages.json"
    if not act_file.is_file() or not stages_file.is_file():
        return []
    try:
        act_data = json.loads(act_file.read_text(encoding="utf-8"))
        ev = _extract_active_event(act_data, datetime.now(timezone.utc), server)
        if not ev or not ev.get("stages"):
            return []
        first_code = str(ev["stages"][0].get("code", ""))
        if "-" not in first_code:
            return []
        active_prefix = first_code.rsplit("-", 1)[0] + "-"
        all_stages = json.loads(stages_file.read_text(encoding="utf-8"))
        return [
            st["code"]
            for st in all_stages
            if str(st.get("code", "")).startswith(active_prefix) and "#f#" not in str(st.get("stageId", ""))
        ]
    except Exception:
        return []


class MaaEngine:
    """GUI-ready universal backend engine encapsulating diagnostics, installation, AI, growth, and scheduling."""

    def __init__(self, config_path: Optional[Path] = None, launch_source: str = "cli"):
        self.config_path = Path(config_path) if config_path is not None else None
        self.base_dir = get_base_dir()
        self.launch_source = str(launch_source or "cli").strip().lower()

    def _get_lock_maa_dir(self) -> Optional[Path]:
        try:
            found = resolve_maa_path(self.config_path)
            if found is not None and Path(found).is_dir():
                return Path(found).resolve()
        except Exception:
            pass
        return None

    def check_active_lock(self, log_callback: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
        """Check whether another active process/thread currently holds the global engine lock."""
        return inspect_engine_lock(
            self.base_dir,
            maa_dir=self._get_lock_maa_dir(),
            clean_stale=True,
            log_callback=log_callback,
        )

    def acquire_task_lock(
        self,
        mode: str,
        task_name: str = "",
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> EngineTaskLock:
        """Create a cross-process single-instance lock bound to config/engine.lock."""
        return EngineTaskLock(
            base_dir=self.base_dir,
            mode=mode,
            source=self.launch_source,
            task=task_name or mode,
            maa_dir=self._get_lock_maa_dir(),
            log_callback=log_callback,
        )

    def _run_with_lock(
        self,
        mode: str,
        fn: Callable[[], Dict[str, Any]],
        *,
        task_name: str = "",
        log_callback: Optional[Callable[[str], None]] = None,
        locked_extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        try:
            with self.acquire_task_lock(mode=mode, task_name=task_name or mode, log_callback=log_callback):
                return fn()
        except EngineLockError as exc:
            msg = str(exc)
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass
            out: Dict[str, Any] = {
                "ok": False,
                "locked": True,
                "mode": mode,
                "lock_info": exc.lock_info,
                "error": msg,
            }
            if locked_extras:
                out.update(locked_extras)
            return out

    def get_config(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = load_launcher_config(self.config_path)
        if overrides:
            cfg = _deep_merge(cfg, overrides)
        return cfg

    def save_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = self.get_config(updates)
        save_launcher_config(merged, self.config_path)
        return merged

    def check_environment(self, check_adb: bool = True) -> Dict[str, Any]:
        cfg = self.get_config()
        maa_dir = resolve_maa_path(self.config_path)
        return run_env_diagnostics(cfg, maa_dir=maa_dir, check_adb=check_adb)

    def probe_or_launch_emulator(
        self,
        cfg: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 60,
        cancel_event: Optional[threading.Event] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        auto_launch: bool = True,
    ) -> Dict[str, Any]:
        """Unified ADB port probe + adb connect + emulator auto-launch."""
        active_cfg = cfg if cfg is not None else self.get_config()
        maa_dir = resolve_maa_path(self.config_path)
        return probe_and_ensure_adb_ready(
            active_cfg,
            maa_dir=maa_dir,
            timeout_sec=timeout_sec,
            cancel_event=cancel_event,
            log_callback=log_callback,
            auto_launch=auto_launch,
        )

    def bind_maa_path(self, maa_path: Union[str, Path]) -> Dict[str, Any]:
        return bind_manual_maa_path(maa_path, self.config_path)

    def ensure_maa_ready(
        self,
        auto_download: bool = False,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        found = resolve_maa_path(self.config_path)
        if found is not None:
            return {"ok": True, "maa_path": str(found), "installed_now": False}
        if not auto_download:
            return {
                "ok": False,
                "maa_path": "",
                "installed_now": False,
                "error": "未检测到有效 MAA 目录，请手动指定路径或调用一键下载安装。",
            }
        res = download_and_install_maa(
            target_dir=self.base_dir / "MAA_Core",
            config_path=self.config_path,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
        res["installed_now"] = bool(res.get("ok"))
        return res

    def refresh_cache(self, cancel_event: Optional[threading.Event] = None) -> Dict[str, Any]:
        return refresh_maa_cache(self.base_dir, cancel_event=cancel_event)

    def check_maa_update(self, timeout_sec: int = 15) -> Dict[str, Any]:
        maa_dir = resolve_maa_path(self.config_path)
        return check_maa_update(maa_dir=maa_dir, config_path=self.config_path, timeout_sec=timeout_sec)

    def update_maa_core(
        self,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        release_info: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 30,
    ) -> Dict[str, Any]:
        maa_dir = resolve_maa_path(self.config_path)
        res = update_maa_core(
            maa_dir=maa_dir,
            config_path=self.config_path,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            release_info=release_info,
            timeout_sec=timeout_sec,
        )
        if res.get("ok"):
            try:
                self.refresh_cache(cancel_event=cancel_event)
            except Exception:
                pass
        return res

    def sync_box_and_depot(
        self,
        run_maa_tasks: bool = False,
        session_holder: Optional[Dict[str, MaaSession]] = None,
        cfg: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        if run_maa_tasks and session_holder is None:
            return self._run_with_lock(
                "sync",
                lambda: self._sync_box_and_depot_unlocked(
                    run_maa_tasks=run_maa_tasks,
                    session_holder=session_holder,
                    cfg=cfg,
                    log_callback=log_callback,
                    cancel_event=cancel_event,
                ),
                task_name="sync_box_and_depot",
                log_callback=log_callback,
            )
        return self._sync_box_and_depot_unlocked(
            run_maa_tasks=run_maa_tasks,
            session_holder=session_holder,
            cfg=cfg,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )

    def _sync_box_and_depot_unlocked(
        self,
        run_maa_tasks: bool = False,
        session_holder: Optional[Dict[str, MaaSession]] = None,
        cfg: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "cancelled": True}
        ready = self.ensure_maa_ready(auto_download=False)
        if not ready["ok"]:
            return {"ok": False, "error": ready.get("error", "")}
        maa_dir = Path(ready["maa_path"])
        active_cfg = cfg or self.get_config()

        if run_maa_tasks:
            own_session = False
            holder = session_holder
            if holder is None:
                if not init_maa_sdk(maa_dir, base_dir=self.base_dir):
                    return {"ok": False, "error": "MaaCore.dll 初始化失败"}
                try:
                    env_info = ensure_emulator_ready(
                        active_cfg, maa_dir=maa_dir, cancel_event=cancel_event, log_callback=log_callback
                    )
                    ensure_game_running(
                        active_cfg,
                        maa_dir=maa_dir,
                        cancel_event=cancel_event,
                        log_callback=log_callback,
                        env_info=env_info,
                    )
                except OperationCancelledError:
                    return {"ok": False, "cancelled": True}
                sess = MaaSession(
                    maa_dir=maa_dir,
                    config=active_cfg,
                    base_dir=self.base_dir,
                    log_callback=log_callback,
                    cancel_event=cancel_event,
                )
                try:
                    ok_conn = sess.connect(env_info=env_info)
                except TypeError:
                    ok_conn = sess.connect()
                if cancel_event is not None and cancel_event.is_set():
                    sess.close()
                    return {"ok": False, "cancelled": True}
                if not ok_conn:
                    return {"ok": False, "error": "ADB 连接失败"}
                holder = {"session": sess}
                own_session = True
            try:
                run_task_with_recovery(holder, "OperBox", {}, active_cfg, timeout_sec=180)
                if not (cancel_event is not None and cancel_event.is_set()):
                    run_task_with_recovery(holder, "Depot", {}, active_cfg, timeout_sec=120)
            finally:
                if own_session:
                    holder["session"].close()
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "cancelled": True}

        my_box = sync_my_box(maa_dir, base_dir=self.base_dir, force=True)
        depot = load_depot_data(maa_dir, base_dir=self.base_dir)
        return {"ok": True, "my_box": my_box, "depot": depot}

    def plan_auto_growth(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        ready = self.ensure_maa_ready(auto_download=False)
        if not ready["ok"]:
            return {"ok": False, "error": ready.get("error", ""), "recommended_stages": ["1-7"]}
        maa_dir = Path(ready["maa_path"])
        cfg = self.get_config()
        state = load_state(self.base_dir)
        decision = decide_optimal_stages(
            maa_dir=maa_dir,
            config=cfg,
            last_battle_is_annihilation=bool(state.get("last_battle_is_annihilation")),
            base_dir=self.base_dir,
            now=now,
            persist=False,
        )
        out = asdict(decision)
        out["ok"] = True
        return out

    def _prepare_session_and_auto_targets(
        self,
        holder: Dict[str, MaaSession],
        maa_dir: Path,
        cfg: Dict[str, Any],
        state: Dict[str, Any],
        fight_mode: str,
        override_stage: str,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Tuple[Optional[List[str]], str]:
        if override_stage or fight_mode.upper() != "AUTO":
            return None, ""

        growth_cfg = cfg.get("growth", {}) if isinstance(cfg.get("growth"), dict) else {}
        if growth_cfg.get("auto_sync_before_fight", True):
            ttl = float(growth_cfg.get("depot_sync_ttl_hours", 24))
            stale = is_data_stale(maa_dir, base_dir=self.base_dir, ttl_hours=ttl)
            try:
                if stale.get("box_stale"):
                    run_task_with_recovery(holder, "OperBox", {}, cfg, timeout_sec=180)
                if stale.get("depot_stale"):
                    run_task_with_recovery(holder, "Depot", {}, cfg, timeout_sec=120)
            except Exception:
                pass

        my_box = sync_my_box(maa_dir, base_dir=self.base_dir)
        depot = load_depot_data(maa_dir, base_dir=self.base_dir)
        decision = decide_optimal_stages(
            maa_dir=maa_dir,
            config=cfg,
            my_box=my_box,
            depot_data=depot,
            last_battle_is_annihilation=bool(state.get("last_battle_is_annihilation")),
            base_dir=self.base_dir,
        )
        if log_callback:
            try:
                log_callback(f"[AI养成决策] 候选队列={decision.recommended_stages} | {decision.reason}")
            except Exception:
                pass
        return decision.recommended_stages, decision.reason

    def _connect_session(
        self,
        cfg: Dict[str, Any],
        refresh_cache_flag: bool = True,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Tuple[Optional[Path], Optional[MaaSession], str]:
        if cancel_event is not None and cancel_event.is_set():
            return None, None, "__cancelled__"
        ready = self.ensure_maa_ready(auto_download=False)
        if not ready["ok"]:
            return None, None, ready.get("error", "MAA 未就绪")
        maa_dir = Path(ready["maa_path"])
        ensure_runtime_dirs(self.base_dir)
        try:
            env_info = ensure_emulator_ready(
                cfg,
                maa_dir=maa_dir,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )
        except OperationCancelledError:
            return maa_dir, None, "__cancelled__"
        if cancel_event is not None and cancel_event.is_set():
            return maa_dir, None, "__cancelled__"
        if refresh_cache_flag:
            self.refresh_cache(cancel_event=cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            return maa_dir, None, "__cancelled__"
        if not init_maa_sdk(maa_dir, base_dir=self.base_dir, use_cache_incremental=True):
            return maa_dir, None, "MaaCore.dll 初始化失败"
        try:
            ensure_game_running(
                cfg,
                maa_dir=maa_dir,
                cancel_event=cancel_event,
                log_callback=log_callback,
                env_info=env_info,
            )
        except OperationCancelledError:
            return maa_dir, None, "__cancelled__"
        if cancel_event is not None and cancel_event.is_set():
            return maa_dir, None, "__cancelled__"
        sess = MaaSession(
            maa_dir=maa_dir,
            config=cfg,
            base_dir=self.base_dir,
            event_callback=event_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        try:
            ok_conn = sess.connect(env_info=env_info)
        except OperationCancelledError:
            sess.close()
            return maa_dir, None, "__cancelled__"
        except TypeError:
            ok_conn = sess.connect()
        if cancel_event is not None and cancel_event.is_set():
            sess.close()
            return maa_dir, None, "__cancelled__"
        if not ok_conn:
            return maa_dir, None, "无法连接至模拟器 ADB"
        return maa_dir, sess, ""

    def run_daily(
        self,
        overrides: Optional[Dict[str, Any]] = None,
        refresh_cache_flag: bool = True,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "daily",
            lambda: self._run_daily_unlocked(
                overrides=overrides,
                refresh_cache_flag=refresh_cache_flag,
                event_callback=event_callback,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                dry_run_post_action=dry_run_post_action,
                collab_handler=collab_handler,
                manual_resume_event=manual_resume_event,
                tutorial_handler=tutorial_handler,
                tutorial_resume_event=tutorial_resume_event,
            ),
            task_name="daily",
            log_callback=log_callback,
            locked_extras={"tasks": {}, "recruit_alerts": []},
        )

    def _run_daily_unlocked(
        self,
        overrides: Optional[Dict[str, Any]] = None,
        refresh_cache_flag: bool = True,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        t0 = time.time()
        cfg = self.get_config(overrides)
        state = load_state(self.base_dir)
        daily_cfg = cfg.get("daily", {}) if isinstance(cfg.get("daily"), dict) else {}
        spec = get_server_spec(cfg)
        skip_set = {str(s).strip().lower() for s in daily_cfg.get("skip_tasks", []) if str(s).strip()}

        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "tasks": {}, "post_action_result": {"action": str(cfg.get("post_action", "none")), "executed": False, "skipped_reason": "cancelled"}}

        maa_dir, sess, err = self._connect_session(
            cfg, refresh_cache_flag=refresh_cache_flag,
            event_callback=event_callback, log_callback=log_callback, cancel_event=cancel_event,
        )
        if err == "__cancelled__" or (cancel_event and cancel_event.is_set()):
            if sess is not None:
                sess.close()
            return {"ok": False, "cancelled": True, "tasks": {}, "post_action_result": {"action": str(cfg.get("post_action", "none")), "executed": False, "skipped_reason": "cancelled"}}
        if sess is None or maa_dir is None:
            return {"ok": False, "error": err}

        holder: Dict[str, Any] = {"session": sess, "cancelled": False, "session_lost": False}
        report: Dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "daily",
            "server": cfg.get("server", "Official"),
            "tasks": {},
            "recruit_alerts": [],
        }

        def _is_user_cancelled() -> bool:
            if cancel_event and cancel_event.is_set():
                holder["cancelled"] = True
            return bool(holder["cancelled"])

        def _should_stop() -> bool:
            return bool(_is_user_cancelled() or holder["session_lost"] or holder.get("network_recovery_failed"))

        def _propagate_task_flags(task_res: Optional[Dict[str, Any]]) -> None:
            if not isinstance(task_res, dict):
                return
            if task_res.get("cancelled"):
                holder["cancelled"] = True
            if task_res.get("network_recovery_failed"):
                holder["network_recovery_failed"] = True
                holder["session_lost"] = True
                if (task_res.get("error") or task_res.get("error_reason")) and not holder.get("network_recovery_error"):
                    holder["network_recovery_error"] = str(task_res.get("error") or task_res.get("error_reason"))
                if task_res.get("recovery_screenshot") and not holder.get("network_recovery_screenshot"):
                    holder["network_recovery_screenshot"] = str(task_res["recovery_screenshot"])

        try:
            if "startup" not in skip_set and not _should_stop():
                if progress_callback:
                    progress_callback(1, 6, "开始唤醒 (StartUp)")
                res = run_task_with_recovery(
                    holder, "StartUp", {"start_game_enabled": True, "client_type": spec["client_type"]}, cfg, timeout_sec=300
                )
                _propagate_task_flags(res)
                report["tasks"]["StartUp"] = _summarize_task_logs(res, "StartUp")

            if "recruit" not in skip_set and not _should_stop():
                if progress_callback:
                    progress_callback(2, 6, "公开招募 (Recruit)")
                pid = str(daily_cfg.get("penguin_id", "") or "")
                preserve_tags = list(daily_cfg.get("recruit_preserve_tags", ["高级资深干员", "资深干员", "支援机械"]))
                confirm_stars = [int(x) for x in daily_cfg.get("recruit_confirm_stars", [3, 4])]
                rec_times = int(daily_cfg.get("recruit_times", 4))
                rec_expedite = bool(daily_cfg.get("recruit_expedite", False))
                res = run_task_with_recovery(
                    holder,
                    "Recruit",
                    {
                        "refresh": True,
                        "force_refresh": True,
                        "select": [4],
                        "confirm": confirm_stars,
                        "times": rec_times,
                        "set_time": True,
                        "expedite": rec_expedite,
                        "preserve_tags": preserve_tags,
                        "extra_tags_mode": 0,
                        "first_tags": [],
                        "recruitment_time": {"3": 540, "4": 540, "5": 540, "6": 540},
                        "report_to_penguin": bool(daily_cfg.get("report_to_penguin", False)),
                        "penguin_id": pid,
                        "report_to_yituliu": bool(daily_cfg.get("report_to_yituliu", False)),
                        "yituliu_id": pid,
                        "server": spec["maa_server"],
                    },
                    cfg,
                    timeout_sec=300,
                )
                _propagate_task_flags(res)
                report["tasks"]["Recruit"] = _summarize_task_logs(res, "Recruit")

            if "infrast" not in skip_set and not _should_stop():
                if progress_callback:
                    progress_callback(3, 6, "基建换班 (Infrast)")
                res = run_task_with_recovery(
                    holder,
                    "Infrast",
                    {
                        "facility": ["Mfg", "Trade", "Control", "Power", "Reception", "Office", "Dorm", "Processing", "Training"],
                        "drones": daily_cfg.get("drones", "Money"),
                        "continue_training": bool(daily_cfg.get("continue_training", True)),
                        "threshold": float(daily_cfg.get("dorm_threshold", 0.3)),
                        "dorm_notstationed_enabled": True,
                        "dorm_trust_enabled": True,
                        "replenish": True,
                        "reception_message_board": True,
                        "reception_clue_exchange": True,
                        "reception_send_clue": True,
                        "fiammetta_targets": list(daily_cfg.get("fiammetta_targets", [])),
                        "fiammetta_recovery_enabled": bool(daily_cfg.get("fiammetta_enabled", False)),
                        "mode": 0,
                    },
                    cfg,
                    timeout_sec=600,
                )
                _propagate_task_flags(res)
                infrast_summary = _summarize_task_logs(res, "Infrast")
                cur_s = holder.get("session")
                adb_p = getattr(cur_s, "adb_path", "") or ""
                adb_a = getattr(cur_s, "adb_addr", "") or ""
                if (
                    not _should_stop()
                    and _infrast_needs_reception_fallback(res)
                    and isinstance(adb_p, str)
                    and adb_p
                    and isinstance(adb_a, str)
                    and adb_a
                ):
                    try:
                        rec_fb = stage_pilot.ensure_reception_clues_processed(
                            adb_path=adb_p,
                            adb_addr=adb_a,
                            maa_dir=maa_dir,
                            collect_clues=True,
                            quick_insert=True,
                            unlock_clues=True,
                            send_duplicates=False,
                            cancel_event=cancel_event,
                            log_callback=log_callback,
                        )
                        infrast_summary["reception_fallback"] = rec_fb
                    except Exception as exc:
                        infrast_summary["reception_fallback"] = {"ok": False, "error": str(exc)}
                report["tasks"]["Infrast"] = infrast_summary

            if "mall" not in skip_set and not _should_stop():
                if progress_callback:
                    progress_callback(4, 6, "信用商店 (Mall)")
                res = run_task_with_recovery(
                    holder,
                    "Mall",
                    {
                        "credit_fight": False,
                        "formation_index": 0,
                        "visit_friends": True,
                        "shopping": True,
                        "buy_first": list(daily_cfg.get("mall_first", [])),
                        "blacklist": list(daily_cfg.get("mall_blacklist", [])),
                        "force_shopping_if_credit_full": True,
                        "only_buy_discount": True,
                        "reserve_max_credit": False,
                    },
                    cfg,
                    timeout_sec=300,
                )
                _propagate_task_flags(res)
                report["tasks"]["Mall"] = _summarize_task_logs(res, "Mall")

            if "annihilation" not in skip_set and not _should_stop():
                if progress_callback:
                    progress_callback(5, 6, "周常剿灭 (Annihilation)")
                annih_res = run_annihilation_pipeline(
                    holder, cfg, state,
                    force=bool(daily_cfg.get("force_annihilation", False)),
                    max_push_rounds=int(daily_cfg.get("max_push_rounds", 5)),
                    base_dir=self.base_dir,
                    progress_callback=progress_callback,
                    log_callback=log_callback,
                    collab_handler=collab_handler,
                    manual_resume_event=manual_resume_event,
                    tutorial_handler=tutorial_handler,
                    tutorial_resume_event=tutorial_resume_event,
                )
                _propagate_task_flags(annih_res)
                report["tasks"]["Annihilation"] = annih_res

            fight_needs_home_recovery = False
            if "fight" not in skip_set and not _should_stop():
                if progress_callback:
                    progress_callback(5, 6, "理智作战 (Fight)")
                try:
                    fmode = str(daily_cfg.get("fight_mode", "AUTO") or "AUTO")
                    ostage = str(daily_cfg.get("override_stage", "") or "")
                    cand_stages, reason = self._prepare_session_and_auto_targets(
                        holder, maa_dir, cfg, state, fight_mode=fmode, override_stage=ostage, log_callback=log_callback
                    )
                    fight_res = run_sanity_fight_pipeline(
                        holder,
                        cfg,
                        state=state,
                        candidate_stages=cand_stages,
                        reason=reason,
                        override_stage=ostage,
                        medicine=int(daily_cfg.get("medicine", 0)),
                        stone=int(daily_cfg.get("stone", 0)),
                        times=int(daily_cfg.get("times", 2147483647)),
                        auto_push=bool(daily_cfg.get("auto_push", False)),
                        max_push_rounds=int(daily_cfg.get("max_push_rounds", 5)),
                        base_dir=self.base_dir,
                        progress_callback=progress_callback,
                        log_callback=log_callback,
                        collab_handler=collab_handler,
                        manual_resume_event=manual_resume_event,
                        tutorial_handler=tutorial_handler,
                        tutorial_resume_event=tutorial_resume_event,
                        daily_mode=True,
                    )
                    _propagate_task_flags(fight_res)
                    report["tasks"]["Fight"] = fight_res
                    if (
                        fight_res.get("selected_stage") is None
                        or fight_res.get("session_lost")
                        or fight_res.get("error")
                        or any(not a.get("ok") for a in fight_res.get("attempted", []))
                    ):
                        fight_needs_home_recovery = True
                except Exception as exc:
                    fight_needs_home_recovery = True
                    if log_callback:
                        try:
                            log_callback(f"[WARN][FIGHT] 刷图环节异常已隔离，将继续返回主界面领取奖励: {exc}")
                        except Exception:
                            pass
                    report["tasks"]["Fight"] = {
                        "selected_stage": None,
                        "runs_finished": 0,
                        "stats": [],
                        "ok": False,
                        "error": str(exc),
                    }

            if "award" not in skip_set and not _is_user_cancelled() and not holder.get("network_recovery_failed"):
                if progress_callback:
                    progress_callback(6, 6, "返回主界面并领取奖励 (Award)")
                if holder.get("session_lost"):
                    try:
                        ensure_game_running(cfg, maa_dir=maa_dir)
                        rec_sess = MaaSession(
                            maa_dir=maa_dir,
                            config=cfg,
                            base_dir=self.base_dir,
                            event_callback=event_callback,
                            log_callback=log_callback,
                            cancel_event=cancel_event,
                        )
                        if rec_sess.connect():
                            holder["session"] = rec_sess
                            holder["session_lost"] = False
                    except Exception:
                        pass

                if not holder.get("session_lost"):
                    cur_s = holder.get("session")
                    adb_p = getattr(cur_s, "adb_path", "") or ""
                    adb_a = getattr(cur_s, "adb_addr", "") or ""
                    if (fight_needs_home_recovery or "Fight" in report["tasks"]) and isinstance(adb_p, str) and adb_p and isinstance(adb_a, str) and adb_a:
                        try:
                            stage_pilot.return_to_home_screen(
                                adb_p, adb_a, maa_dir, cancel_event=cancel_event, log_callback=log_callback
                            )
                        except Exception:
                            pass
                    if fight_needs_home_recovery and not _is_user_cancelled():
                        try:
                            st_rec = run_task_with_recovery(
                                holder,
                                "StartUp",
                                {"start_game_enabled": True, "client_type": spec["client_type"]},
                                cfg,
                                timeout_sec=90,
                                max_retries=0,
                            )
                            if st_rec.get("cancelled"):
                                holder["cancelled"] = True
                        except Exception:
                            pass
                    if not _is_user_cancelled():
                        try:
                            res = run_task_with_recovery(
                                holder,
                                "Award",
                                {"award": True, "mail": True, "recruit": False, "orundum": False, "mining": False, "specialaccess": True, "signinevent": True},
                                cfg,
                                timeout_sec=240,
                            )
                            _propagate_task_flags(res)
                            report["tasks"]["Award"] = _summarize_task_logs(res, "Award")
                        except Exception as exc:
                            report["tasks"]["Award"] = {"ok": False, "claimed": False, "error": str(exc)}

            if daily_cfg.get("sync_box_after_daily", False) and not _should_stop():
                self.sync_box_and_depot(run_maa_tasks=True, session_holder=holder, cfg=cfg)

            report["recruit_alerts"] = holder["session"].recruit_alerts
            if not _should_stop():
                end_shot = self.base_dir / "reports" / "daily_end_screen.png"
                if holder["session"].adb_path and holder["session"].adb_addr:
                    if capture_adb_screenshot(holder["session"].adb_path, holder["session"].adb_addr, end_shot):
                        report["end_screenshot"] = "reports/daily_end_screen.png"
        finally:
            if holder.get("session") is not None:
                holder["session"].close()

        was_cancelled = bool((cancel_event and cancel_event.is_set()) or holder.get("cancelled"))
        was_lost = bool(holder.get("session_lost"))
        was_net_failed = bool(holder.get("network_recovery_failed"))
        report["cancelled"] = was_cancelled
        report["session_lost"] = was_lost
        if was_net_failed:
            report["network_recovery_failed"] = True
            if holder.get("network_recovery_error"):
                report["error"] = str(holder["network_recovery_error"])
            if holder.get("network_recovery_screenshot"):
                report["network_recovery_screenshot"] = str(holder["network_recovery_screenshot"])
        report["ok"] = (not was_cancelled) and (not was_lost) and (not was_net_failed)
        report["total_elapsed_seconds"] = round(time.time() - t0, 1)
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")

        if was_cancelled:
            report["post_action_result"] = {
                "action": str(cfg.get("post_action", "none")),
                "executed": False,
                "skipped_reason": "cancelled",
            }
        else:
            report["post_action_result"] = execute_post_action(
                str(cfg.get("post_action", "none")), cfg, maa_dir=maa_dir, dry_run=dry_run_post_action
            )

        reports_dir = self.base_dir / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "data").mkdir(parents=True, exist_ok=True)
        ts_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        rel_report = f"reports/daily_{ts_name}.json"
        (self.base_dir / rel_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.base_dir / "data" / "latest_daily_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        state["last_daily"] = {"finished_at": report["finished_at"], "report_path": rel_report}
        save_state(state, base_dir=self.base_dir)

        notif = cfg.get("notifications", {}) if isinstance(cfg.get("notifications"), dict) else {}
        if not was_cancelled and notif.get("webhook_enabled") and notif.get("notify_on_daily_finish", True):
            fight_info = report.get("tasks", {}).get("Fight", {})
            annih_info = report.get("tasks", {}).get("Annihilation", {})
            msg_lines = [
                f"区服: {report.get('server')} | 耗时: {report.get('total_elapsed_seconds')}s",
                f"刷图关卡: {fight_info.get('selected_stage') or '未执行'} (完成 {fight_info.get('runs_finished', 0)} 次)",
                f"剿灭进度: {annih_info.get('weekly_process') or annih_info.get('status', '-')}",
                f"高星公招告警: {len(report.get('recruit_alerts', []))} 条",
            ]
            threading.Thread(
                target=send_webhook_notification,
                args=(cfg, "MAA 每日自动收菜战报", "\n".join(msg_lines), "daily_finish", {"report_path": rel_report}),
                daemon=True,
            ).start()

        return report

    def run_annihilation(
        self,
        force: bool = True,
        max_rounds: int = 5,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "annihilation",
            lambda: self._run_annihilation_unlocked(
                force=force,
                max_rounds=max_rounds,
                refresh_cache_flag=refresh_cache_flag,
                overrides=overrides,
                event_callback=event_callback,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                dry_run_post_action=dry_run_post_action,
                collab_handler=collab_handler,
                manual_resume_event=manual_resume_event,
                tutorial_handler=tutorial_handler,
                tutorial_resume_event=tutorial_resume_event,
            ),
            task_name="annihilation",
            log_callback=log_callback,
        )

    def _run_annihilation_unlocked(
        self,
        force: bool = True,
        max_rounds: int = 5,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        cfg = self.get_config(overrides)
        state = load_state(self.base_dir)
        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True}
        maa_dir, sess, err = self._connect_session(
            cfg,
            refresh_cache_flag=refresh_cache_flag,
            event_callback=event_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        if err == "__cancelled__" or (cancel_event and cancel_event.is_set()):
            if sess is not None:
                sess.close()
            return {"ok": False, "cancelled": True}
        if sess is None or maa_dir is None:
            return {"ok": False, "error": err}
        holder = {"session": sess}
        try:
            res = run_annihilation_pipeline(
                holder,
                cfg,
                state,
                force=force,
                max_push_rounds=max_rounds,
                base_dir=self.base_dir,
                progress_callback=progress_callback,
                log_callback=log_callback,
                collab_handler=collab_handler,
                manual_resume_event=manual_resume_event,
                tutorial_handler=tutorial_handler,
                tutorial_resume_event=tutorial_resume_event,
            )
        finally:
            if holder.get("session") is not None:
                holder["session"].close()
        if cancel_event and cancel_event.is_set():
            res["cancelled"] = True
            res["post_action_result"] = {"action": str(cfg.get("post_action", "none")), "executed": False, "skipped_reason": "cancelled"}
        else:
            res["post_action_result"] = execute_post_action(
                str(cfg.get("post_action", "none")), cfg, maa_dir=maa_dir, dry_run=dry_run_post_action
            )
        return res

    def run_fight(
        self,
        stage: Optional[str] = None,
        fight_mode: Optional[str] = None,
        medicine: Optional[int] = None,
        stone: Optional[int] = None,
        times: Optional[int] = None,
        auto_push: Optional[bool] = None,
        max_rounds: int = 5,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "fight",
            lambda: self._run_fight_unlocked(
                stage=stage,
                fight_mode=fight_mode,
                medicine=medicine,
                stone=stone,
                times=times,
                auto_push=auto_push,
                max_rounds=max_rounds,
                refresh_cache_flag=refresh_cache_flag,
                overrides=overrides,
                event_callback=event_callback,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                dry_run_post_action=dry_run_post_action,
                collab_handler=collab_handler,
                manual_resume_event=manual_resume_event,
                tutorial_handler=tutorial_handler,
                tutorial_resume_event=tutorial_resume_event,
            ),
            task_name=f"fight:{stage or fight_mode or 'auto'}",
            log_callback=log_callback,
            locked_extras={"selected_stage": None},
        )

    def _run_fight_unlocked(
        self,
        stage: Optional[str] = None,
        fight_mode: Optional[str] = None,
        medicine: Optional[int] = None,
        stone: Optional[int] = None,
        times: Optional[int] = None,
        auto_push: Optional[bool] = None,
        max_rounds: int = 5,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        cfg = self.get_config(overrides)
        state = load_state(self.base_dir)
        daily_cfg = cfg.get("daily", {}) if isinstance(cfg.get("daily"), dict) else {}
        fmode = fight_mode or str(daily_cfg.get("fight_mode", "AUTO") or "AUTO")
        eff_stage = stage if stage is not None else str(daily_cfg.get("override_stage", "") or "")
        eff_medicine = medicine if medicine is not None else int(daily_cfg.get("medicine", 0))
        eff_stone = stone if stone is not None else int(daily_cfg.get("stone", 0))
        eff_times = times if times is not None else int(daily_cfg.get("times", 2147483647))
        use_auto_push = bool(daily_cfg.get("auto_push", False)) if auto_push is None else bool(auto_push)

        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "selected_stage": None}

        maa_dir, sess, err = self._connect_session(
            cfg,
            refresh_cache_flag=refresh_cache_flag,
            event_callback=event_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        if err == "__cancelled__" or (cancel_event and cancel_event.is_set()):
            if sess is not None:
                sess.close()
            return {"ok": False, "cancelled": True, "selected_stage": None}
        if sess is None or maa_dir is None:
            return {"ok": False, "error": err, "selected_stage": None}
        holder = {"session": sess}
        try:
            cand_stages, reason = self._prepare_session_and_auto_targets(
                holder, maa_dir, cfg, state, fight_mode=fmode, override_stage=eff_stage, log_callback=log_callback
            )
            res = run_sanity_fight_pipeline(
                holder, cfg, state=state, candidate_stages=cand_stages, reason=reason,
                override_stage=eff_stage, medicine=eff_medicine, stone=eff_stone, times=eff_times,
                auto_push=use_auto_push, max_push_rounds=max_rounds, base_dir=self.base_dir,
                progress_callback=progress_callback, log_callback=log_callback,
                collab_handler=collab_handler, manual_resume_event=manual_resume_event,
                tutorial_handler=tutorial_handler, tutorial_resume_event=tutorial_resume_event,
            )
        finally:
            if holder.get("session") is not None:
                holder["session"].close()
        if cancel_event and cancel_event.is_set():
            res["cancelled"] = True
            res["post_action_result"] = {"action": str(cfg.get("post_action", "none")), "executed": False, "skipped_reason": "cancelled"}
        else:
            res["post_action_result"] = execute_post_action(
                str(cfg.get("post_action", "none")), cfg, maa_dir=maa_dir, dry_run=dry_run_post_action
            )
        return res

    def run_push(
        self,
        stages: List[str],
        max_rounds: int = 5,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "push",
            lambda: self._run_push_unlocked(
                stages=stages,
                max_rounds=max_rounds,
                refresh_cache_flag=refresh_cache_flag,
                overrides=overrides,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                collab_handler=collab_handler,
                manual_resume_event=manual_resume_event,
                tutorial_handler=tutorial_handler,
                tutorial_resume_event=tutorial_resume_event,
            ),
            task_name=f"push:{','.join(stages) if stages else 'auto'}",
            log_callback=log_callback,
            locked_extras={"stages": list(stages), "results": []},
        )

    def _run_push_unlocked(
        self,
        stages: List[str],
        max_rounds: int = 5,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
        tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        tutorial_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        def _log(msg: str) -> None:
            if log_callback:
                try:
                    log_callback(msg)
                except Exception:
                    pass

        cfg = self.get_config(overrides)
        ready = self.ensure_maa_ready(auto_download=False)
        if not ready["ok"]:
            return {"ok": False, "error": ready.get("error", "")}
        maa_dir = Path(ready["maa_path"])
        ensure_runtime_dirs(self.base_dir)
        env_info: Dict[str, Any] = {}
        try:
            raw_env = ensure_emulator_ready(cfg, maa_dir=maa_dir, cancel_event=cancel_event, log_callback=log_callback)
            if isinstance(raw_env, dict):
                env_info = raw_env
        except OperationCancelledError:
            return {"ok": False, "cancelled": True, "stages": list(stages), "results": []}
        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "stages": list(stages), "results": []}
        if refresh_cache_flag:
            self.refresh_cache(cancel_event=cancel_event)
        if not init_maa_sdk(maa_dir, base_dir=self.base_dir, use_cache_incremental=True):
            return {"ok": False, "error": "MaaCore.dll 初始化失败"}

        adb_path = str(env_info.get("adb_path") or "").strip()
        adb_addr = str(env_info.get("adb_addr") or "").strip()

        is_auto_mode = (not stages) or (len(stages) == 1 and stages[0].lower() == "auto")
        target_stages = list(stages)
        detected_frontier = ""
        used_visual_fallback = False
        dynamic_auto_active = False
        initial_dynamic_scan: Dict[str, Any] = {}
        eff_adb_p = adb_path or (discover_adb_executable(cfg, maa_dir=maa_dir) or "")
        eff_adb_a = adb_addr or (_probe_adb_address(eff_adb_p, cfg, maa_dir, cancel_event=cancel_event) if eff_adb_p else "")

        if not is_auto_mode and eff_adb_p and eff_adb_a:
            try:
                stage_pilot.ensure_on_stage_map_before_search(
                    eff_adb_p,
                    eff_adb_a,
                    maa_dir,
                    cancel_event=cancel_event,
                    log_callback=log_callback,
                )
            except Exception:
                pass

        if is_auto_mode:
            # Priority 1: Prefix-agnostic direct visual scan of current emulator map screen
            if eff_adb_p and eff_adb_a:
                try:
                    initial_dynamic_scan = stage_pilot.discover_next_uncleared_stage_on_map(
                        eff_adb_p,
                        eff_adb_a,
                        maa_dir,
                        completed_stages=set(),
                        max_swipes=8,
                        unchanged_swipe_limit=2,
                        cancel_event=cancel_event,
                        log_callback=log_callback,
                    )
                except Exception:
                    initial_dynamic_scan = {}

            if isinstance(initial_dynamic_scan, dict) and initial_dynamic_scan.get("cancelled"):
                return {"ok": False, "cancelled": True, "stages": [], "results": []}

            if isinstance(initial_dynamic_scan, dict) and initial_dynamic_scan.get("on_stage_map") and (
                initial_dynamic_scan.get("found") or initial_dynamic_scan.get("all_cleared")
            ):
                dynamic_auto_active = True
                _log(
                    "[PUSH] 启用全格式看图动态逐关推进模式（去前缀化：主线/活动/TR教学/ST剧情/XX-XX-XX 全兼容，打完回图重扫）"
                )
            else:
                # Fallback when not currently on a recognizable stage map screen
                target_stages = _resolve_auto_push_stages(maa_dir, self.base_dir, str(cfg.get("server", "Official")))
                if not target_stages and eff_adb_p and eff_adb_a:
                    _log("[PUSH] 本地 stages.json 未收录当前活动，启用「看图识新活动」OCR 扫描模拟器画面...")
                    try:
                        vis_ev = stage_pilot.discover_active_event_from_map(
                            eff_adb_p,
                            eff_adb_a,
                            maa_dir,
                            default_max_stage=10,
                            max_swipes=3,
                            known_battle_stages_resolver=lambda pref: resolve_event_battle_stages_from_prts(
                                pref, base_dir=self.base_dir
                            ),
                            cancel_event=cancel_event,
                            log_callback=log_callback,
                        )
                    except Exception:
                        vis_ev = {}
                    if isinstance(vis_ev, dict) and vis_ev.get("found") and vis_ev.get("push_sequence"):
                        target_stages = list(vis_ev["push_sequence"])
                        detected_frontier = str(vis_ev.get("frontier_stage") or target_stages[0])
                        used_visual_fallback = True
                        _log(
                            f"[PUSH] 看图识新活动成功：识别活动前缀={vis_ev.get('prefix')} | "
                            f"前沿={detected_frontier} | 动态推图队列={target_stages}"
                        )
                if not target_stages:
                    return {
                        "ok": False,
                        "error": "当前模拟器画面未识别到关卡地图节点，且本地未检测到进行中的活动列表（请先进入主线章节或活动地图画面后重试，或手动输入关卡名如 16-2 / TR-27 / OS-1）",
                    }

                # Auto-detect the true uncleared frontier on the map so cleared stages with blue hexagon
                # are immediately skipped when later stages are already unlocked.
                if not used_visual_fallback and target_stages and adb_path and adb_addr:
                    pref = _extract_event_prefix(target_stages[0])
                    if pref:
                        try:
                            f_info = stage_pilot.detect_map_frontier_stage(
                                pref,
                                adb_path,
                                adb_addr,
                                maa_dir,
                                target_stage=target_stages[-1],
                                known_battle_stages=target_stages,
                                max_swipes=3,
                                cancel_event=cancel_event,
                                log_callback=log_callback,
                            )
                        except Exception:
                            f_info = {}
                        if isinstance(f_info, dict) and f_info.get("on_stage_map"):
                            if f_info.get("target_visible") and f_info.get("target_cleared"):
                                _log(f"[PUSH] --stages auto 检测到终点关卡 {target_stages[-1]} 已通关，无需继续推图。")
                                out_path = self.base_dir / "reports" / f"push_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                                out_path.parent.mkdir(parents=True, exist_ok=True)
                                out_path.write_text("[]", encoding="utf-8")
                                return {
                                    "ok": True,
                                    "already_cleared": True,
                                    "cancelled": False,
                                    "stages": [],
                                    "results": [],
                                    "report_path": str(out_path),
                                }
                            f_seq = list(f_info.get("push_sequence") or [])
                            if f_seq:
                                detected_frontier = str(f_info.get("frontier_stage") or "")
                                target_stages = f_seq
                                _log(
                                    f"[PUSH] --stages auto 自动锁定前沿关卡={detected_frontier or f_seq[0]}，"
                                    f"跳过前置已通关节点，推进序列={target_stages}"
                                )

        all_ok = True
        cancelled = False
        aborted = False
        abort_stage = ""
        abort_reason = ""
        abort_screenshot = ""
        skipped_stages: List[str] = []
        results: List[Dict[str, Any]] = []
        handled_prereqs: set[str] = set()

        def _capture_abort_screenshot(stage_code: str, summary_obj: Dict[str, Any]) -> str:
            existing_shot = str(summary_obj.get("screenshot") or "").strip()
            if existing_shot and Path(existing_shot).is_file():
                return existing_shot
            for h_item in reversed(summary_obj.get("history") or []):
                if isinstance(h_item, dict):
                    s = str(h_item.get("screenshot") or "").strip()
                    if s and Path(s).is_file():
                        return s
            if adb_path and adb_addr:
                safe_s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(stage_code or "unknown"))
                shot_p = self.base_dir / "reports" / f"push_abort_{safe_s}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                shot_p.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if capture_adb_screenshot(adb_path, adb_addr, shot_p):
                        return str(shot_p.resolve())
                except Exception:
                    pass
            return existing_shot

        def _record_abort_and_notify_agent(stage_code: str, summary_obj: Dict[str, Any], remaining: List[str]) -> None:
            nonlocal aborted, abort_stage, abort_reason, abort_screenshot, skipped_stages
            aborted = True
            abort_stage = stage_code
            skipped_stages = list(remaining)
            abort_screenshot = _capture_abort_screenshot(stage_code, summary_obj)
            is_form_unavail = bool(summary_obj.get("formation_unavailable"))
            is_copilot_file_err = bool(summary_obj.get("copilot_file_error"))
            abort_reason = str(
                summary_obj.get("error")
                or (
                    "copilot_file_error"
                    if is_copilot_file_err
                    else (
                        "formation_unavailable"
                        if is_form_unavail
                        else ("stage_entry_failed" if summary_obj.get("stage_entry_failed") else "stage_clear_failed")
                    )
                )
            )
            if is_form_unavail:
                unavail_ops = list(summary_obj.get("unavailable_opers") or [])
                _log(
                    f"[PUSH] 前置关卡 {stage_code} 在全部替补与备选作业尝试后仍编队不可用"
                    f"（不可用干员: {unavail_ops}），后续未解锁关卡 {remaining} 无法推进，中止推图链！现场截图: {abort_screenshot or '无'}"
                )
            else:
                _log(
                    f"[PUSH] 前置关卡 {stage_code} 在复位重试后仍失败（根因: {abort_reason}），"
                    f"后续未解锁关卡 {remaining} 无法推进，立即中止整条推图链！现场截图: {abort_screenshot or '无'}"
                )

            # If autonomous_clear_stage already wrote agent_task.json (e.g. formation_unavailable exhaustion),
            # preserve its reason & unavailable_opers and only enrich chain abort context.
            if summary_obj.get("agent_task_written") and summary_obj.get("agent_task_path"):
                try:
                    tf = Path(str(summary_obj["agent_task_path"]))
                    if tf.is_file():
                        cur_task = json.loads(tf.read_text(encoding="utf-8"))
                        if isinstance(cur_task, dict):
                            bs = cur_task.get("battle_state") if isinstance(cur_task.get("battle_state"), dict) else {}
                            bs["aborted_chain"] = True
                            bs["remaining_stages"] = list(remaining)
                            cur_task["battle_state"] = bs
                            if abort_screenshot and not cur_task.get("screenshot_path"):
                                cur_task["screenshot_path"] = abort_screenshot
                            tf.write_text(json.dumps(cur_task, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
                return

            # In collab mode (no built-in API key), write agent_task.json on formation_unavailable / stage_entry_failed / stage_mismatch
            # with current screenshot path so external AI Agent can inspect and perform real self-healing.
            client = ai_strategist.OpenAICompatClient.from_config(cfg)
            ai_mode = str((cfg.get("ai") or {}).get("mode", "") or "").strip().lower()
            is_collab_cfg = (not client.is_configured()) and (collab_handler is not None or ai_mode == "agent_collab")
            if is_collab_cfg and (is_form_unavail or summary_obj.get("stage_entry_failed") or summary_obj.get("stage_mismatch")):
                safe_s = re.sub(r"[^A-Za-z0-9_\-]", "_", str(stage_code or "unknown"))
                cop_str = str(summary_obj.get("copilot_file") or "").strip()
                target_cop = Path(cop_str) if cop_str else (self.base_dir / "copilots" / f"push_{safe_s}_r1.json")
                last_hist = (summary_obj.get("history") or [{}])[-1] if isinstance(summary_obj.get("history"), list) else {}
                trig = "formation_unavailable" if is_form_unavail else "stage_entry_failed"
                unavail_ops = list(summary_obj.get("unavailable_opers") or [])
                task_file, payload = write_agent_task(
                    base_dir=self.base_dir,
                    trigger_reason=trig,
                    stage_code=stage_code,
                    stage_id=str(summary_obj.get("stage_id") or ""),
                    round_idx=int(summary_obj.get("rounds_used") or 1),
                    max_rounds=max_rounds,
                    target_copilot_path=target_cop,
                    failed_copilot_path=target_cop if is_form_unavail else None,
                    screenshot_path=abort_screenshot,
                    unavailable_opers=unavail_ops if is_form_unavail else None,
                    battle_state={
                        "formation_unavailable": is_form_unavail,
                        "unavailable_opers": unavail_ops,
                        "unavailable_oper_reasons": dict(summary_obj.get("unavailable_oper_reasons") or {}),
                        "stage_entry_failed": (not is_form_unavail) and bool(summary_obj.get("stage_entry_failed")),
                        "stage_mismatch": bool(summary_obj.get("stage_mismatch")),
                        "displayed_stage": str(summary_obj.get("displayed_stage") or ""),
                        "abort_reason": abort_reason,
                        "aborted_chain": True,
                        "remaining_stages": list(remaining),
                        "recovery_info": summary_obj.get("recovery_info"),
                        "errors": last_hist.get("errors", []) if isinstance(last_hist, dict) else [],
                    },
                    rule_suggestion=(
                        f"关卡 {stage_code} 编队干员不可用 ({unavail_ops}) 且全部替补与备选作业均失败，请查看截图与作业 JSON 处理。"
                        if is_form_unavail
                        else f"前置关卡 {stage_code} 进图失败（{abort_reason}），已中止后续 {remaining}，请查看现场截图定位页面状态并自愈。"
                    ),
                )
                payload["agent_task_path"] = str(task_file)
                if not is_form_unavail:
                    payload["instructions_for_agent"] = (
                        f"顺序推图在前置关卡 {stage_code} 进图失败（{abort_reason}），已中止后续关卡 {remaining}。"
                        f"请先读取 screenshot_path 现场截图确认模拟器当前页面状态，修复界面或作业问题后再恢复推图。"
                    )
                task_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                _log(f"[Agent协同] 已写入 agent_task.json ({trig}) | 现场截图: {abort_screenshot or '无'}")
                if collab_handler is not None:
                    try:
                        collab_handler(payload)
                    except Exception:
                        pass

        def _handle_blocking_prereqs(
            combat_stage: str,
            use_initial_frontier: bool = False,
        ) -> Tuple[bool, bool, Optional[Dict[str, Any]]]:
            if not is_auto_mode or "-" not in combat_stage or stage_pilot.classify_stage_node(combat_stage) != "battle":
                return False, True, None
            prefix = combat_stage.rsplit("-", 1)[0] + "-"
            did_any = False
            for _round in range(3):
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    eff_adb_p = adb_path or (discover_adb_executable(cfg, maa_dir=maa_dir) or "")
                    eff_adb_a = adb_addr or _probe_adb_address(eff_adb_p, cfg, maa_dir)
                    prereqs = stage_pilot.discover_unlocked_special_prereqs(
                        prefix,
                        eff_adb_p,
                        eff_adb_a,
                        maa_dir,
                        target_combat_stage=combat_stage,
                        frontier_stage=(detected_frontier if use_initial_frontier else ""),
                        max_swipes=2,
                        cancel_event=cancel_event,
                    )
                except Exception:
                    prereqs = []
                pending = [p for p in prereqs if p not in handled_prereqs]
                if not pending:
                    break
                _log(
                    f"[PUSH][动态发现] 目标关 {combat_stage} 尚未解锁，地图右侧发现未通关节点 {pending}，自动插入队首优先处理..."
                )
                for idx_p, pst in enumerate(pending):
                    if pst in handled_prereqs or (cancel_event and cancel_event.is_set()):
                        continue
                    handled_prereqs.add(pst)
                    nxt_p = pending[idx_p + 1] if idx_p + 1 < len(pending) else combat_stage
                    ok_p, sum_p = autonomous_clear_stage(
                        pst,
                        maa_dir=maa_dir,
                        config=cfg,
                        max_rounds=max_rounds,
                        base_dir=self.base_dir,
                        progress_callback=progress_callback,
                        log_callback=log_callback,
                        cancel_event=cancel_event,
                        collab_handler=collab_handler,
                        manual_resume_event=manual_resume_event,
                        next_stage=nxt_p,
                        tutorial_handler=tutorial_handler,
                        tutorial_resume_event=tutorial_resume_event,
                        skip_if_already_3star=is_auto_mode,
                    )
                    results.append(sum_p)
                    did_any = True
                    if sum_p.get("cancelled") or not ok_p:
                        return did_any, False, sum_p
            return did_any, True, None

        prereq_aborted = False
        cleared_3star_after_story: set[str] = set()
        executed_stages: List[str] = []
        all_cleared_dynamic = False

        if dynamic_auto_active:
            completed_dynamic: set[str] = set()
            for step_idx in range(80):
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    all_ok = False
                    break
                cur_scan = (
                    initial_dynamic_scan
                    if step_idx == 0
                    else stage_pilot.discover_next_uncleared_stage_on_map(
                        eff_adb_p,
                        eff_adb_a,
                        maa_dir,
                        completed_stages=completed_dynamic,
                        max_swipes=8,
                        unchanged_swipe_limit=2,
                        cancel_event=cancel_event,
                        log_callback=log_callback,
                    )
                )
                if cur_scan.get("cancelled"):
                    cancelled = True
                    all_ok = False
                    break
                if cur_scan.get("all_cleared"):
                    all_cleared_dynamic = True
                    _log("[PUSH] 动态推图闭环检测到当前章节/活动已全部 3 星通关，推图结束！")
                    break
                if not cur_scan.get("found"):
                    if executed_stages and cur_scan.get("reason") == "no_stage_nodes_detected":
                        _log("[PUSH] 返回画面暂未识别到新的未通关节点，结束动态推图。")
                        break
                    all_ok = False
                    _record_abort_and_notify_agent(
                        executed_stages[-1] if executed_stages else "auto",
                        {"error": str(cur_scan.get("reason") or "dynamic_map_scan_failed"), "stage_entry_failed": True},
                        [],
                    )
                    break

                st = str(cur_scan["next_stage"]).strip()
                executed_stages.append(st)
                _log(f"[PUSH][动态推进 #{step_idx + 1}] 锁定当前前沿关卡 {st} ({cur_scan.get('next_type', 'battle')})...")
                ok, summary = autonomous_clear_stage(
                    st,
                    maa_dir=maa_dir,
                    config=cfg,
                    max_rounds=max_rounds,
                    base_dir=self.base_dir,
                    progress_callback=progress_callback,
                    log_callback=log_callback,
                    cancel_event=cancel_event,
                    collab_handler=collab_handler,
                    manual_resume_event=manual_resume_event,
                    next_stage=None,
                    tutorial_handler=tutorial_handler,
                    tutorial_resume_event=tutorial_resume_event,
                    skip_if_already_3star=True,
                    require_fresh_map_entry=(step_idx > 0),
                )
                if ok and summary.get("map_clear_verified") is False:
                    ok = False
                    summary["victory"] = False
                    summary.setdefault("battle_failed", True)
                    summary.setdefault("error", "map_clear_verify_failed")
                results.append(summary)
                if summary.get("cancelled"):
                    cancelled = True
                    all_ok = False
                    break
                if not ok:
                    all_ok = False
                    _record_abort_and_notify_agent(st, summary, [])
                    break
                completed_dynamic.add(st.upper())
            target_stages = list(executed_stages)
        elif not prereq_aborted:
            for idx, st in enumerate(target_stages):
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    all_ok = False
                    break
                if is_auto_mode and st.upper() in cleared_3star_after_story:
                    _log(f"[PUSH] 跳过剧情关返回地图后已确认 {st} 满 3 星通关，自动跳过不再重复进图。")
                    results.append(
                        {
                            "stage": st,
                            "victory": True,
                            "already_cleared": True,
                            "skipped": True,
                            "map_clear_verified": True,
                        }
                    )
                    continue
                if is_auto_mode and stage_pilot.classify_stage_node(st) == "battle":
                    _, prereq_ok, prereq_fail_sum = _handle_blocking_prereqs(st, use_initial_frontier=(idx == 0))
                    if not prereq_ok:
                        all_ok = False
                        if prereq_fail_sum and prereq_fail_sum.get("cancelled"):
                            cancelled = True
                        elif prereq_fail_sum:
                            _record_abort_and_notify_agent(
                                str(prereq_fail_sum.get("stage") or st),
                                prereq_fail_sum,
                                list(target_stages[idx:]),
                            )
                        break
                next_st = target_stages[idx + 1] if idx + 1 < len(target_stages) else None
                ok, summary = autonomous_clear_stage(
                    st,
                    maa_dir=maa_dir,
                    config=cfg,
                    max_rounds=max_rounds,
                    base_dir=self.base_dir,
                    progress_callback=progress_callback,
                    log_callback=log_callback,
                    cancel_event=cancel_event,
                    collab_handler=collab_handler,
                    manual_resume_event=manual_resume_event,
                    next_stage=next_st,
                    tutorial_handler=tutorial_handler,
                    tutorial_resume_event=tutorial_resume_event,
                    skip_if_already_3star=is_auto_mode,
                    require_fresh_map_entry=(idx > 0),
                )
                if ok and summary.get("map_clear_verified") is False:
                    ok = False
                    summary["victory"] = False
                    summary.setdefault("battle_failed", True)
                    summary.setdefault("error", "map_clear_verify_failed")
                if not ok and not summary.get("cancelled") and is_auto_mode and stage_pilot.classify_stage_node(st) == "battle":
                    did_pre, pre_ok, _ = _handle_blocking_prereqs(st, use_initial_frontier=False)
                    if did_pre and pre_ok:
                        ok, summary = autonomous_clear_stage(
                            st,
                            maa_dir=maa_dir,
                            config=cfg,
                            max_rounds=max_rounds,
                            base_dir=self.base_dir,
                            progress_callback=progress_callback,
                            log_callback=log_callback,
                            cancel_event=cancel_event,
                            collab_handler=collab_handler,
                            manual_resume_event=manual_resume_event,
                            next_stage=next_st,
                            tutorial_handler=tutorial_handler,
                            tutorial_resume_event=tutorial_resume_event,
                            skip_if_already_3star=is_auto_mode,
                            require_fresh_map_entry=True,
                        )
                        if ok and summary.get("map_clear_verified") is False:
                            ok = False
                            summary["victory"] = False
                            summary.setdefault("battle_failed", True)
                            summary.setdefault("error", "map_clear_verify_failed")
                results.append(summary)
                if ok and stage_pilot.classify_stage_node(st) == "story":
                    for nd_after in (summary.get("visible_nodes_after") or []):
                        if (
                            isinstance(nd_after, dict)
                            and nd_after.get("node_type") == "battle"
                            and nd_after.get("cleared")
                            and nd_after.get("badge_full_3star")
                            and not nd_after.get("partial_star")
                        ):
                            c_up = str(nd_after.get("code", "")).strip().upper()
                            if c_up:
                                cleared_3star_after_story.add(c_up)
                if summary.get("cancelled"):
                    cancelled = True
                    all_ok = False
                    break
                if not ok:
                    all_ok = False
                    _record_abort_and_notify_agent(st, summary, target_stages[idx + 1 :])
                    break

        out_path = self.base_dir / "reports" / f"push_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        res_payload: Dict[str, Any] = {
            "ok": all_ok,
            "cancelled": cancelled,
            "stages": target_stages,
            "results": results,
            "report_path": str(out_path),
        }
        if dynamic_auto_active:
            res_payload["dynamic_mode"] = True
            res_payload["all_cleared"] = all_cleared_dynamic
            if all_cleared_dynamic and not executed_stages and all_ok:
                res_payload["already_cleared"] = True
        if aborted:
            res_payload["aborted"] = True
            res_payload["aborted_stage"] = abort_stage
            res_payload["abort_reason"] = abort_reason
            res_payload["abort_screenshot"] = abort_screenshot
            res_payload["skipped_stages"] = skipped_stages
        return res_payload

    def inspect_exploration_capabilities(self) -> Dict[str, Any]:
        """Inspect MAA's native Roguelike & Reclamation capabilities (including BlackFlow rewards/tech-tree)."""
        ready = self.ensure_maa_ready(auto_download=False)
        if not ready["ok"]:
            return {"ok": False, "error": ready.get("error", "")}
        caps = inspect_maa_exploration_capabilities(Path(ready["maa_path"]))
        caps["ok"] = True
        return caps

    def sync_exploration_from_maa_gui(
        self,
        maa_dir: Optional[Union[str, Path]] = None,
        save: bool = True,
    ) -> Dict[str, Any]:
        """Sync Roguelike & Reclamation settings from original MAA <maa_dir>/config/gui.new.json."""
        eff_dir: Optional[Path] = None
        if maa_dir:
            eff_dir = Path(maa_dir).resolve()
        else:
            ready = self.ensure_maa_ready(auto_download=False)
            if ready.get("ok") and ready.get("maa_path"):
                eff_dir = Path(ready["maa_path"]).resolve()
        res = sync_exploration_from_maa_gui(eff_dir)
        if res.get("ok") and save:
            updates: Dict[str, Any] = {}
            if res.get("roguelike"):
                updates["roguelike"] = res["roguelike"]
            if res.get("reclamation"):
                updates["reclamation"] = res["reclamation"]
            if updates:
                res["saved_config"] = self.save_config(updates)
        return res

    def run_roguelike_post_settlement(
        self,
        theme: Optional[str] = None,
        auto_claim_rewards: bool = True,
        auto_upgrade_tech_tree: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "roguelike_post_settlement",
            lambda: self._run_roguelike_post_settlement_unlocked(
                theme=theme,
                auto_claim_rewards=auto_claim_rewards,
                auto_upgrade_tech_tree=auto_upgrade_tech_tree,
                overrides=overrides,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            ),
            task_name=f"roguelike_post:{theme or 'auto'}",
            log_callback=log_callback,
        )

    def _run_roguelike_post_settlement_unlocked(
        self,
        theme: Optional[str] = None,
        auto_claim_rewards: bool = True,
        auto_upgrade_tech_tree: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Standalone one-click Roguelike post-settlement reward claiming & tech-tree auto-upgrade."""
        t0 = time.time()
        cfg = self.get_config(overrides)
        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "mode": "roguelike_post_settlement"}

        ready = self.ensure_maa_ready(auto_download=False)
        if not ready["ok"]:
            return {"ok": False, "mode": "roguelike_post_settlement", "error": ready.get("error", "MAA 未就绪")}
        maa_dir = Path(ready["maa_path"])
        resolved = resolve_roguelike_settings(
            config=cfg,
            maa_dir=maa_dir,
            overrides={"theme": theme} if theme else None,
        )
        eff_theme = resolved["theme"]
        profile = roguelike_ai_copilot.get_theme_ui_profile(eff_theme)

        if log_callback:
            try:
                log_callback(
                    f"[肉鸽AI] 启动一键领奖与科技树自动加点 | 主题={eff_theme} ({profile.get('display_name', eff_theme)}) | "
                    f"领奖={auto_claim_rewards} | 加点={auto_upgrade_tech_tree}"
                )
            except Exception:
                pass

        if progress_callback:
            progress_callback(1, 3, "连接模拟器 ADB")
        try:
            env_info = ensure_emulator_ready(cfg, maa_dir=maa_dir, cancel_event=cancel_event, log_callback=log_callback)
            adb_path = str(env_info.get("adb_path", "") or "")
            adb_addr = str(env_info.get("adb_addr", "") or "")
        except OperationCancelledError:
            return {"ok": False, "cancelled": True, "mode": "roguelike_post_settlement", "theme": eff_theme}
        except Exception as exc:
            return {
                "ok": False,
                "cancelled": False,
                "mode": "roguelike_post_settlement",
                "theme": eff_theme,
                "error": str(exc),
            }

        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "mode": "roguelike_post_settlement", "theme": eff_theme}

        post_cfg = copy.deepcopy(cfg)
        post_cfg.setdefault("roguelike", {})
        post_cfg["roguelike"]["auto_claim_rewards"] = bool(auto_claim_rewards)
        post_cfg["roguelike"]["auto_upgrade_tech_tree"] = bool(auto_upgrade_tech_tree)

        if progress_callback:
            progress_callback(2, 3, f"执行 {profile.get('display_name', eff_theme)} 领奖与加点")
        # Pass capabilities=None and navigate_to_lobby=True so standalone one-click trigger automatically navigates
        # to the target Roguelike theme lobby and executes OCR reward claiming & tech-tree auto-upgrade.
        try:
            post_res = roguelike_ai_copilot.run_roguelike_post_settlement(
                adb_path=adb_path,
                adb_addr=adb_addr,
                maa_dir=maa_dir,
                config=post_cfg,
                theme=eff_theme,
                capabilities=None,
                cancel_event=cancel_event,
                log_callback=log_callback,
                navigate_to_lobby=True,
            )
        except TypeError:
            post_res = roguelike_ai_copilot.run_roguelike_post_settlement(
                adb_path=adb_path,
                adb_addr=adb_addr,
                maa_dir=maa_dir,
                config=post_cfg,
                theme=eff_theme,
                capabilities=None,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )
        if progress_callback:
            progress_callback(3, 3, "领奖与加点完成")

        was_cancelled = bool((cancel_event and cancel_event.is_set()) or post_res.get("cancelled"))
        rew_ok = bool((post_res.get("rewards") or {}).get("ok", True))
        tech_ok = bool((post_res.get("tech_tree") or {}).get("ok", True))
        post_ok = bool(post_res.get("ok", True))
        err_text = str(
            post_res.get("error")
            or (post_res.get("rewards") or {}).get("error")
            or (post_res.get("tech_tree") or {}).get("error")
            or ""
        )
        ok = (not was_cancelled) and post_ok and rew_ok and tech_ok and ("skipped_reason" not in post_res) and (not err_text)
        out_res: Dict[str, Any] = {
            "ok": ok,
            "cancelled": was_cancelled,
            "mode": "roguelike_post_settlement",
            "theme": eff_theme,
            "theme_display_name": profile.get("display_name", eff_theme),
            "navigation": post_res.get("navigation", {}),
            "rewards": post_res.get("rewards", {}),
            "tech_tree": post_res.get("tech_tree", {}),
            "total_elapsed_seconds": round(time.time() - t0, 1),
        }
        if err_text:
            out_res["error"] = err_text
        elif post_res.get("skipped_reason"):
            out_res["error"] = str(post_res["skipped_reason"])
        return out_res

    def run_roguelike(
        self,
        theme: Optional[str] = None,
        rl_mode: Optional[Union[str, int]] = None,
        squad: Optional[str] = None,
        roles: Optional[str] = None,
        core_char: Optional[str] = None,
        starts_count: Optional[int] = None,
        difficulty: Optional[int] = None,
        ai_copilot_enabled: Optional[bool] = None,
        auto_claim_rewards: Optional[bool] = None,
        auto_upgrade_tech_tree: Optional[bool] = None,
        timeout_sec: Optional[int] = None,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "roguelike",
            lambda: self._run_roguelike_unlocked(
                theme=theme,
                rl_mode=rl_mode,
                squad=squad,
                roles=roles,
                core_char=core_char,
                starts_count=starts_count,
                difficulty=difficulty,
                ai_copilot_enabled=ai_copilot_enabled,
                auto_claim_rewards=auto_claim_rewards,
                auto_upgrade_tech_tree=auto_upgrade_tech_tree,
                timeout_sec=timeout_sec,
                refresh_cache_flag=refresh_cache_flag,
                overrides=overrides,
                event_callback=event_callback,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                dry_run_post_action=dry_run_post_action,
                collab_handler=collab_handler,
                manual_resume_event=manual_resume_event,
            ),
            task_name=f"roguelike:{theme or 'auto'}",
            log_callback=log_callback,
        )

    def _run_roguelike_unlocked(
        self,
        theme: Optional[str] = None,
        rl_mode: Optional[Union[str, int]] = None,
        squad: Optional[str] = None,
        roles: Optional[str] = None,
        core_char: Optional[str] = None,
        starts_count: Optional[int] = None,
        difficulty: Optional[int] = None,
        ai_copilot_enabled: Optional[bool] = None,
        auto_claim_rewards: Optional[bool] = None,
        auto_upgrade_tech_tree: Optional[bool] = None,
        timeout_sec: Optional[int] = None,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        manual_resume_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Run Integrated Strategies (Roguelike) with gui.new.json inheritance, optional AI copilot & post-settlement."""
        t0 = time.time()
        cfg = self.get_config(overrides)
        state = load_state(self.base_dir)
        spec = get_server_spec(cfg)

        rl_ov: Dict[str, Any] = {}
        if theme:
            rl_ov["theme"] = theme
        if rl_mode is not None and str(rl_mode).strip() != "":
            rl_ov["mode"] = rl_mode
        if squad:
            rl_ov["squad"] = squad
        if roles:
            rl_ov["roles"] = roles
        if core_char:
            rl_ov["core_char"] = core_char
        if starts_count is not None:
            rl_ov["starts_count"] = starts_count
        if difficulty is not None:
            rl_ov["difficulty"] = difficulty
        if ai_copilot_enabled is not None:
            rl_ov["ai_copilot_enabled"] = bool(ai_copilot_enabled)
        if auto_claim_rewards is not None:
            rl_ov["auto_claim_rewards"] = bool(auto_claim_rewards)
        if auto_upgrade_tech_tree is not None:
            rl_ov["auto_upgrade_tech_tree"] = bool(auto_upgrade_tech_tree)
        if timeout_sec is not None:
            rl_ov["timeout_sec"] = int(timeout_sec)

        if cancel_event and cancel_event.is_set():
            return {
                "ok": False,
                "cancelled": True,
                "mode": "roguelike",
                "post_action_result": {
                    "action": str(cfg.get("post_action", "none")),
                    "executed": False,
                    "skipped_reason": "cancelled",
                },
            }

        maa_dir, sess, err = self._connect_session(
            cfg,
            refresh_cache_flag=refresh_cache_flag,
            event_callback=event_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        if err == "__cancelled__" or (cancel_event and cancel_event.is_set()):
            if sess is not None:
                sess.close()
            return {
                "ok": False,
                "cancelled": True,
                "mode": "roguelike",
                "post_action_result": {
                    "action": str(cfg.get("post_action", "none")),
                    "executed": False,
                    "skipped_reason": "cancelled",
                },
            }
        if sess is None or maa_dir is None:
            return {"ok": False, "mode": "roguelike", "error": err}

        holder: Dict[str, Any] = {"session": sess}
        report: Dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "roguelike",
            "server": cfg.get("server", "Official"),
        }
        try:
            if progress_callback:
                progress_callback(1, 3, "唤醒游戏 (StartUp)")
            st_res = run_task_with_recovery(
                holder, "StartUp", {"start_game_enabled": True, "client_type": spec["client_type"]}, cfg, timeout_sec=300
            )
            if st_res.get("cancelled") or (cancel_event and cancel_event.is_set()):
                task_res: Dict[str, Any] = {"ok": False, "cancelled": True}
            else:
                if progress_callback:
                    progress_callback(2, 3, "执行集成战略 (Roguelike)")
                task_res = run_roguelike_task(
                    holder,
                    cfg,
                    maa_dir=maa_dir,
                    overrides=rl_ov or None,
                    base_dir=self.base_dir,
                    log_callback=log_callback,
                    cancel_event=cancel_event,
                    collab_handler=collab_handler,
                    manual_resume_event=manual_resume_event,
                )
            report.update(task_res)
            if not report.get("cancelled") and not (cancel_event and cancel_event.is_set()):
                if progress_callback:
                    progress_callback(3, 3, "生成集成战略战报")
                end_shot = self.base_dir / "reports" / "roguelike_end_screen.png"
                cur_s = holder["session"]
                if getattr(cur_s, "adb_path", "") and getattr(cur_s, "adb_addr", ""):
                    if capture_adb_screenshot(cur_s.adb_path, cur_s.adb_addr, end_shot):
                        report["end_screenshot"] = "reports/roguelike_end_screen.png"
        finally:
            if holder.get("session") is not None:
                holder["session"].close()

        was_cancelled = bool((cancel_event and cancel_event.is_set()) or report.get("cancelled"))
        report["cancelled"] = was_cancelled
        report["ok"] = bool(report.get("ok", False)) and not was_cancelled
        report["total_elapsed_seconds"] = round(time.time() - t0, 1)
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")

        if was_cancelled:
            report["post_action_result"] = {
                "action": str(cfg.get("post_action", "none")),
                "executed": False,
                "skipped_reason": "cancelled",
            }
        else:
            report["post_action_result"] = execute_post_action(
                str(cfg.get("post_action", "none")), cfg, maa_dir=maa_dir, dry_run=dry_run_post_action
            )

        (self.base_dir / "reports").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "data").mkdir(parents=True, exist_ok=True)
        rel_report = f"reports/roguelike_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        (self.base_dir / rel_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.base_dir / "data" / "latest_roguelike_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["report_path"] = rel_report

        state["last_roguelike"] = {
            "finished_at": report["finished_at"],
            "theme": report.get("theme", ""),
            "report_path": rel_report,
        }
        save_state(state, base_dir=self.base_dir)

        notif = cfg.get("notifications", {}) if isinstance(cfg.get("notifications"), dict) else {}
        if not was_cancelled and notif.get("webhook_enabled") and notif.get("notify_on_daily_finish", True):
            m = report.get("metrics", {}) if isinstance(report.get("metrics"), dict) else {}
            post_s = report.get("post_settlement", {}) if isinstance(report.get("post_settlement"), dict) else {}
            msg = (
                f"主题: {report.get('theme')} | 耗时: {report.get('total_elapsed_seconds')}s\n"
                f"探索局数: {m.get('runs_started', 0)} | 结算: {m.get('settlements', 0)} | 投资: {m.get('investments', 0)}\n"
                f"局后领奖/加点: {json.dumps(post_s, ensure_ascii=False)[:180]}"
            )
            threading.Thread(
                target=send_webhook_notification,
                args=(cfg, "MAA 集成战略战报", msg, "roguelike_finish", {"report_path": rel_report}),
                daemon=True,
            ).start()

        return report

    def run_reclamation(
        self,
        theme: Optional[str] = None,
        ra_mode: Optional[Union[str, int]] = None,
        tools_to_craft: Optional[List[str]] = None,
        num_craft_batches: Optional[int] = None,
        increment_mode: Optional[int] = None,
        clear_store: Optional[bool] = None,
        timeout_sec: Optional[int] = None,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
    ) -> Dict[str, Any]:
        return self._run_with_lock(
            "reclamation",
            lambda: self._run_reclamation_unlocked(
                theme=theme,
                ra_mode=ra_mode,
                tools_to_craft=tools_to_craft,
                num_craft_batches=num_craft_batches,
                increment_mode=increment_mode,
                clear_store=clear_store,
                timeout_sec=timeout_sec,
                refresh_cache_flag=refresh_cache_flag,
                overrides=overrides,
                event_callback=event_callback,
                log_callback=log_callback,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                dry_run_post_action=dry_run_post_action,
            ),
            task_name=f"reclamation:{theme or 'auto'}",
            log_callback=log_callback,
        )

    def _run_reclamation_unlocked(
        self,
        theme: Optional[str] = None,
        ra_mode: Optional[Union[str, int]] = None,
        tools_to_craft: Optional[List[str]] = None,
        num_craft_batches: Optional[int] = None,
        increment_mode: Optional[int] = None,
        clear_store: Optional[bool] = None,
        timeout_sec: Optional[int] = None,
        refresh_cache_flag: bool = True,
        overrides: Optional[Dict[str, Any]] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        dry_run_post_action: bool = False,
    ) -> Dict[str, Any]:
        """Run Reclamation Algorithm (生息演算) with gui.new.json inheritance, cancellation & battle report."""
        t0 = time.time()
        cfg = self.get_config(overrides)
        state = load_state(self.base_dir)
        spec = get_server_spec(cfg)

        ra_ov: Dict[str, Any] = {}
        if theme:
            ra_ov["theme"] = theme
        if ra_mode is not None and str(ra_mode).strip() != "":
            ra_ov["mode"] = ra_mode
        if tools_to_craft:
            ra_ov["tools_to_craft"] = list(tools_to_craft)
        if num_craft_batches is not None:
            ra_ov["num_craft_batches"] = int(num_craft_batches)
        if increment_mode is not None:
            ra_ov["increment_mode"] = int(increment_mode)
        if clear_store is not None:
            ra_ov["clear_store"] = bool(clear_store)
        if timeout_sec is not None:
            ra_ov["timeout_sec"] = int(timeout_sec)

        if cancel_event and cancel_event.is_set():
            return {
                "ok": False,
                "cancelled": True,
                "mode": "reclamation",
                "post_action_result": {
                    "action": str(cfg.get("post_action", "none")),
                    "executed": False,
                    "skipped_reason": "cancelled",
                },
            }

        maa_dir, sess, err = self._connect_session(
            cfg,
            refresh_cache_flag=refresh_cache_flag,
            event_callback=event_callback,
            log_callback=log_callback,
            cancel_event=cancel_event,
        )
        if err == "__cancelled__" or (cancel_event and cancel_event.is_set()):
            if sess is not None:
                sess.close()
            return {
                "ok": False,
                "cancelled": True,
                "mode": "reclamation",
                "post_action_result": {
                    "action": str(cfg.get("post_action", "none")),
                    "executed": False,
                    "skipped_reason": "cancelled",
                },
            }
        if sess is None or maa_dir is None:
            return {"ok": False, "mode": "reclamation", "error": err}

        holder: Dict[str, Any] = {"session": sess}
        report: Dict[str, Any] = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "reclamation",
            "server": cfg.get("server", "Official"),
        }
        try:
            resolved_pre = resolve_reclamation_settings(config=cfg, maa_dir=maa_dir, overrides=ra_ov or None)
            already_in_ra = False
            skip_startup_keep_nav = False
            if resolved_pre.get("theme") == "RelaunchAnchor":
                adb_p = getattr(sess, "adb_path", "") or ""
                adb_a = getattr(sess, "adb_addr", "") or ""
                if isinstance(adb_p, str) and adb_p and isinstance(adb_a, str) and adb_a and Path(adb_p).is_file():
                    pre_img = stage_pilot.adb_screencap_image(adb_p, adb_a, timeout=5.0, cancel_event=cancel_event)
                    if pre_img is not None and not (cancel_event and cancel_event.is_set()):
                        pre_st = stage_pilot.detect_reclamation_screen_state(
                            pre_img,
                            maa_dir,
                            target_stage=str(resolved_pre.get("stage_code") or "RA-1"),
                            use_ocr=True,
                            cancel_event=cancel_event,
                        )
                        pre_screen = str(pre_st.get("screen") or "unknown")
                        if pre_st.get("needs_cleanup") or pre_screen in (
                            "relaunch_anchor_residual_settlement",
                            "relaunch_anchor_in_battle",
                            "relaunch_anchor_drawer_open",
                            "guide_popup",
                        ):
                            skip_startup_keep_nav = True
                            if log_callback:
                                try:
                                    log_callback(
                                        f"[RECLAMATION] 检测到当前存在残留状态或教学浮层 ({pre_screen})，跳过 StartUp 并先执行自动清理..."
                                    )
                                except Exception:
                                    pass
                        elif pre_st.get("ready_for_maa"):
                            already_in_ra = True
                            if log_callback:
                                try:
                                    log_callback(
                                        f"[RECLAMATION] 检测到当前已在《重启锚点》({pre_screen}) 界面，跳过 StartUp 直接启动生息演算！"
                                    )
                                except Exception:
                                    pass
                        elif pre_screen in ("relaunch_anchor_lobby", "reclamation_hub", "long_term_exploration"):
                            skip_startup_keep_nav = True
                            if log_callback:
                                try:
                                    log_callback(
                                        f"[RECLAMATION] 检测到当前已在生息演算入口链路 ({pre_screen})，跳过 StartUp 并由 stage_pilot 继续进入大地图..."
                                    )
                                except Exception:
                                    pass

            if cancel_event and cancel_event.is_set():
                task_res: Dict[str, Any] = {"ok": False, "cancelled": True}
            elif already_in_ra or skip_startup_keep_nav:
                if progress_callback:
                    progress_callback(2, 3, "执行生息演算 (Reclamation)")
                task_res = run_reclamation_task(
                    holder,
                    cfg,
                    maa_dir=maa_dir,
                    overrides=ra_ov or None,
                    log_callback=log_callback,
                    cancel_event=cancel_event,
                    skip_entry_navigation=already_in_ra,
                )
            else:
                if progress_callback:
                    progress_callback(1, 3, "唤醒游戏 (StartUp)")
                st_res = run_task_with_recovery(
                    holder, "StartUp", {"start_game_enabled": True, "client_type": spec["client_type"]}, cfg, timeout_sec=300
                )
                if st_res.get("cancelled") or (cancel_event and cancel_event.is_set()):
                    task_res = {"ok": False, "cancelled": True}
                else:
                    if progress_callback:
                        progress_callback(2, 3, "执行生息演算 (Reclamation)")
                    task_res = run_reclamation_task(
                        holder,
                        cfg,
                        maa_dir=maa_dir,
                        overrides=ra_ov or None,
                        log_callback=log_callback,
                        cancel_event=cancel_event,
                    )
            report.update(task_res)
            if not report.get("cancelled") and not (cancel_event and cancel_event.is_set()):
                if progress_callback:
                    progress_callback(3, 3, "生成生息演算战报")
                end_shot = self.base_dir / "reports" / "reclamation_end_screen.png"
                cur_s = holder["session"]
                if getattr(cur_s, "adb_path", "") and getattr(cur_s, "adb_addr", ""):
                    if capture_adb_screenshot(cur_s.adb_path, cur_s.adb_addr, end_shot):
                        report["end_screenshot"] = "reports/reclamation_end_screen.png"
        finally:
            if holder.get("session") is not None:
                holder["session"].close()

        was_cancelled = bool((cancel_event and cancel_event.is_set()) or report.get("cancelled"))
        report["cancelled"] = was_cancelled
        report["ok"] = bool(report.get("ok", False)) and not was_cancelled
        report["total_elapsed_seconds"] = round(time.time() - t0, 1)
        report["finished_at"] = datetime.now().isoformat(timespec="seconds")

        if was_cancelled:
            report["post_action_result"] = {
                "action": str(cfg.get("post_action", "none")),
                "executed": False,
                "skipped_reason": "cancelled",
            }
        else:
            report["post_action_result"] = execute_post_action(
                str(cfg.get("post_action", "none")), cfg, maa_dir=maa_dir, dry_run=dry_run_post_action
            )

        (self.base_dir / "reports").mkdir(parents=True, exist_ok=True)
        (self.base_dir / "data").mkdir(parents=True, exist_ok=True)
        rel_report = f"reports/reclamation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        (self.base_dir / rel_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.base_dir / "data" / "latest_reclamation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["report_path"] = rel_report

        state["last_reclamation"] = {
            "finished_at": report["finished_at"],
            "theme": report.get("theme", ""),
            "report_path": rel_report,
        }
        save_state(state, base_dir=self.base_dir)

        notif = cfg.get("notifications", {}) if isinstance(cfg.get("notifications"), dict) else {}
        if not was_cancelled and notif.get("webhook_enabled") and notif.get("notify_on_daily_finish", True):
            m = report.get("metrics", {}) if isinstance(report.get("metrics"), dict) else {}
            msg = (
                f"主题: {report.get('theme')} | 模式: {report.get('mode_label')} | 耗时: {report.get('total_elapsed_seconds')}s\n"
                f"完成周期: {m.get('cycles_completed', 0)} | 制造轮次: {m.get('craft_rounds', 0)} | 商店搬空: {m.get('store_cleared', False)}"
            )
            threading.Thread(
                target=send_webhook_notification,
                args=(cfg, "MAA 生息演算战报", msg, "reclamation_finish", {"report_path": rel_report}),
                daemon=True,
            ).start()

        return report


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MAA 通用自主后端调度引擎")
    parser.add_argument(
        "--mode",
        choices=["check", "install", "daily", "annihilation", "fight", "push", "roguelike", "reclamation"],
        default="daily",
    )
    parser.add_argument("--config", default="", help="自定义 launcher_config.json 路径")
    parser.add_argument("--server", choices=["Official", "Bilibili"], default="", help="覆盖区服设置")
    parser.add_argument("--fight-mode", choices=["AUTO", "MANUAL"], default="", help="刷理智策略模式")
    parser.add_argument("--post-action", choices=["none", "close_emulator", "shutdown"], default="", help="任务完成后动作")
    parser.add_argument("--stage", default=None, help="手动指定 Fight 刷理智关卡")
    parser.add_argument("--stages", nargs="*", default=[], help="--mode push 关卡列表或 auto")
    parser.add_argument("--max-rounds", type=int, default=5)
    parser.add_argument("--medicine", type=int, default=None)
    parser.add_argument("--stone", type=int, default=None)
    parser.add_argument("--times", type=int, default=None)
    parser.add_argument("--auto-push", action="store_true")
    parser.add_argument("--force-annihilation", action="store_true")
    parser.add_argument("--skip-tasks", default="")
    parser.add_argument("--sync-box", action="store_true")
    parser.add_argument("--no-refresh-cache", action="store_true")
    parser.add_argument("--no-adb-check", action="store_true")
    parser.add_argument("--dry-run-post-action", action="store_true")
    # Long-term exploration overrides (Roguelike / Reclamation)
    parser.add_argument("--theme", default="", help="肉鸽/生息演算主题覆盖（如 BlackFlow / Sarkaz / RelaunchAnchor / Tales）")
    parser.add_argument("--rl-mode", default="", help="肉鸽模式覆盖（Exp/Investment/Collectible 或整数）")
    parser.add_argument("--squad", default="", help="肉鸽开局分队覆盖")
    parser.add_argument("--roles", default="", help="肉鸽开局职业组覆盖")
    parser.add_argument("--core-char", default="", help="肉鸽核心干员覆盖")
    parser.add_argument("--starts-count", type=int, default=None, help="肉鸽探索次数上限")
    parser.add_argument("--difficulty", type=int, default=None, help="肉鸽难度覆盖")
    parser.add_argument("--ai-copilot", action="store_true", help="开启肉鸽 AI 局外关键决策辅助")
    parser.add_argument("--no-claim-rewards", action="store_true", help="禁用肉鸽局后自动领奖")
    parser.add_argument("--no-tech-tree", action="store_true", help="禁用肉鸽局后科技树自动加点")
    parser.add_argument("--ra-mode", default="", help="生息演算模式覆盖（RA1/RA4/RA15/ProsperityNoSave/ProsperityInSave）")
    parser.add_argument("--clear-store", action="store_true", help="生息演算完成后自动搬空商店")
    parser.add_argument("--timeout", type=int, default=None, help="长时任务超时秒数覆盖")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    args = _build_cli_parser().parse_args(argv)
    cfg_path = Path(args.config) if args.config else None
    engine = MaaEngine(config_path=cfg_path, launch_source="cli")

    def _cli_emit_lock_if_needed(res_obj: Dict[str, Any]) -> bool:
        if isinstance(res_obj, dict) and res_obj.get("locked"):
            print(str(res_obj.get("error") or "[LOCK] 检测到已有任务正在运行，请先停止现有任务后再启动"), file=sys.stderr, flush=True)
            return True
        return False

    overrides: Dict[str, Any] = {}
    if args.server:
        overrides["server"] = args.server
    if args.post_action:
        overrides["post_action"] = args.post_action

    daily_over: Dict[str, Any] = {}
    if args.fight_mode:
        daily_over["fight_mode"] = args.fight_mode
    if args.stage is not None:
        daily_over["override_stage"] = args.stage
    if args.medicine is not None:
        daily_over["medicine"] = args.medicine
    if args.stone is not None:
        daily_over["stone"] = args.stone
    if args.auto_push:
        daily_over["auto_push"] = True
    if args.force_annihilation:
        daily_over["force_annihilation"] = True
    if args.sync_box:
        daily_over["sync_box_after_daily"] = True
    if args.skip_tasks:
        daily_over["skip_tasks"] = [s.strip() for s in args.skip_tasks.split(",") if s.strip()]
    if daily_over:
        overrides["daily"] = daily_over

    if args.mode == "check":
        if overrides:
            engine.save_config(overrides)
        if not args.no_refresh_cache:
            engine.refresh_cache()
        env = engine.check_environment(check_adb=not args.no_adb_check)
        growth_plan = engine.plan_auto_growth()
        print(json.dumps({
            "environment": env,
            "week_key": get_arknights_week_key(),
            "growth_plan": growth_plan,
        }, ensure_ascii=False, indent=2))
        return 0 if env.get("healthy") else 1

    if args.mode == "install":
        res = engine.ensure_maa_ready(
            auto_download=True,
            progress_callback=lambda d, t, msg: print(f"[INSTALL] {msg} ({d}/{t})"),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 2

    if args.mode == "daily":
        res = engine.run_daily(
            overrides=overrides or None,
            refresh_cache_flag=not args.no_refresh_cache,
            dry_run_post_action=args.dry_run_post_action,
        )
        locked = _cli_emit_lock_if_needed(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 3 if locked else (0 if res.get("ok") else 2)

    if args.mode == "annihilation":
        res = engine.run_annihilation(
            force=True,
            max_rounds=args.max_rounds,
            refresh_cache_flag=not args.no_refresh_cache,
            overrides=overrides or None,
            dry_run_post_action=args.dry_run_post_action,
        )
        locked = _cli_emit_lock_if_needed(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if locked:
            return 3
        failed_statuses = ("unlock_failed", "reconnect_failed_after_unlock")
        return 0 if ("error" not in res and not res.get("cancelled") and res.get("status") not in failed_statuses) else 2

    if args.mode == "fight":
        res = engine.run_fight(
            stage=args.stage,
            fight_mode=args.fight_mode or None,
            medicine=args.medicine,
            stone=args.stone,
            times=args.times,
            auto_push=args.auto_push or None,
            max_rounds=args.max_rounds,
            refresh_cache_flag=not args.no_refresh_cache,
            overrides=overrides or None,
            dry_run_post_action=args.dry_run_post_action,
        )
        locked = _cli_emit_lock_if_needed(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        if locked:
            return 3
        return 0 if ("error" not in res and not res.get("cancelled") and res.get("selected_stage") is not None) else 2

    if args.mode == "push":
        raw_stages: List[str] = []
        for item in args.stages:
            for s in item.split(","):
                if s.strip():
                    raw_stages.append(s.strip())
        res = engine.run_push(
            stages=raw_stages,
            max_rounds=args.max_rounds,
            refresh_cache_flag=not args.no_refresh_cache,
            overrides=overrides or None,
        )
        locked = _cli_emit_lock_if_needed(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 3 if locked else (0 if res.get("ok") else 1)

    if args.mode == "roguelike":
        res = engine.run_roguelike(
            theme=args.theme or None,
            rl_mode=args.rl_mode or None,
            squad=args.squad or None,
            roles=args.roles or None,
            core_char=args.core_char or None,
            starts_count=args.starts_count,
            difficulty=args.difficulty,
            ai_copilot_enabled=True if args.ai_copilot else None,
            auto_claim_rewards=False if args.no_claim_rewards else None,
            auto_upgrade_tech_tree=False if args.no_tech_tree else None,
            timeout_sec=args.timeout,
            refresh_cache_flag=not args.no_refresh_cache,
            overrides=overrides or None,
            dry_run_post_action=args.dry_run_post_action,
        )
        locked = _cli_emit_lock_if_needed(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 3 if locked else (0 if res.get("ok") else 2)

    if args.mode == "reclamation":
        res = engine.run_reclamation(
            theme=args.theme or None,
            ra_mode=args.ra_mode or None,
            clear_store=True if args.clear_store else None,
            timeout_sec=args.timeout,
            refresh_cache_flag=not args.no_refresh_cache,
            overrides=overrides or None,
            dry_run_post_action=args.dry_run_post_action,
        )
        locked = _cli_emit_lock_if_needed(res)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 3 if locked else (0 if res.get("ok") else 2)

    return 0


if __name__ == "__main__":
    sys.exit(main())
