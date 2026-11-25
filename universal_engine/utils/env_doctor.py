"""Relative path resolution, configuration loading, and environment health checks."""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
DEFAULT_ADB_CMD_TIMEOUT = 5.0


class OperationCancelledError(RuntimeError):
    """Raised when a blocking wait or subprocess is aborted via cancel_event."""


def wait_interruptible(
    seconds: float,
    cancel_event: Optional[threading.Event] = None,
    step: float = 0.5,
) -> bool:
    """Sleep up to `seconds` while checking `cancel_event` at <= `step` intervals.

    Returns True immediately if `cancel_event` is set, False if the full wait elapsed.
    """
    total = max(0.0, float(seconds))
    if cancel_event is None:
        if total > 0:
            time.sleep(total)
        return False
    if cancel_event.is_set():
        return True
    if total <= 0:
        return False
    chunk_max = max(0.05, min(float(step), 0.5))
    deadline = time.monotonic() + total
    while True:
        if cancel_event.is_set():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return cancel_event.is_set()
        if cancel_event.wait(min(chunk_max, remaining)):
            return True


def _terminate_process_fast(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=0.3)
            except Exception:
                pass
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=0.3)
            except Exception:
                pass
    except Exception:
        pass
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


def run_cmd_cancellable(
    cmd: List[str],
    timeout_sec: float = DEFAULT_ADB_CMD_TIMEOUT,
    cancel_event: Optional[threading.Event] = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess with short timeout and immediate terminate() on cancel_event."""
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelledError(f"任务已取消，中止命令: {cmd[0] if cmd else ''}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )

    out_chunks: List[bytes] = []
    err_chunks: List[bytes] = []

    def _drain(stream: Any, bucket: List[bytes]) -> None:
        if stream is None:
            return
        try:
            data = stream.read()
            if data:
                bucket.append(data)
        except Exception:
            pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True)
    t_out.start()
    t_err.start()

    eff_timeout = max(0.2, float(timeout_sec))
    deadline = time.monotonic() + eff_timeout
    try:
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_process_fast(proc)
                raise OperationCancelledError(f"任务已取消，已强制终止子进程: {cmd[0] if cmd else ''}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_fast(proc)
                raise subprocess.TimeoutExpired(cmd, eff_timeout)
            step = min(0.08, remaining)
            if cancel_event is not None:
                if cancel_event.wait(step):
                    _terminate_process_fast(proc)
                    raise OperationCancelledError(f"任务已取消，已强制终止子进程: {cmd[0] if cmd else ''}")
            else:
                time.sleep(step)
    finally:
        t_out.join(timeout=0.35)
        t_err.join(timeout=0.35)
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    raw_out = b"".join(out_chunks)
    raw_err = b"".join(err_chunks)
    if text:
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode if proc.returncode is not None else -1,
            raw_out.decode("utf-8", errors="ignore"),
            raw_err.decode("utf-8", errors="ignore"),
        )
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode if proc.returncode is not None else -1,
        raw_out,
        raw_err,
    )

SERVER_PACKAGES: Dict[str, Dict[str, str]] = {
    "Official": {
        "package": "com.hypergryph.arknights",
        "activity": "com.u8.sdk.U8UnityContext",
        "maa_server": "CN",
        "client_type": "Official",
    },
    "Bilibili": {
        "package": "com.hypergryph.arknights.bilibili",
        "activity": "com.u8.sdk.U8UnityContext",
        "maa_server": "CN",
        "client_type": "Bilibili",
    },
}

DEFAULT_LAUNCHER_CONFIG: Dict[str, Any] = {
    "maa_path": "",
    "maa_path_pinned": False,
    "first_run_done": False,
    "server": "Official",
    "emulator": {
        "type": "auto",
        "path": "",
        "adb_path": "",
        "adb_address": "",
        "instance_index": 0,
        "touch_type": "minitouch",
        "auto_launch": True,
    },
    "ai": {
        "mode": "agent_collab",
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model_name": "gpt-4o-mini",
        "timeout_sec": 30,
    },
    "daily": {
        "fight_mode": "AUTO",
        "override_stage": "",
        "medicine": 0,
        "stone": 0,
        "auto_push": False,
        "max_push_rounds": 5,
        "sync_box_after_daily": False,
        "skip_tasks": [],
        "recruit_preserve_tags": ["高级资深干员", "资深干员", "支援机械"],
        "recruit_confirm_stars": [3, 4],
        "recruit_times": 4,
        "recruit_expedite": False,
        "report_to_penguin": False,
        "penguin_id": "",
        "report_to_yituliu": False,
        "drones": "Money",
        "dorm_threshold": 0.3,
        "fiammetta_enabled": False,
        "fiammetta_targets": [],
        "continue_training": True,
        "mall_first": [
            "招聘许可",
            "赤金",
            "龙门币",
            "技巧概要·卷2",
            "技巧概要·卷1",
            "基础作战记录",
            "初级作战记录",
            "固源岩",
            "糖",
            "酯原料",
            "异铁",
            "酮凝集",
            "装置",
        ],
        "mall_blacklist": ["碳", "家具", "加急许可", "讯使", "嘉维尔", "坚雷"],
    },
    "growth": {
        "prefer_event": True,
        "allow_chip_stages": True,
        "allow_resource_stages": True,
        "auto_sync_before_fight": True,
        "depot_sync_ttl_hours": 24,
        "chip_untargeted_cap": 2,
        "fold_craftable_materials": True,
        "target_operators": [],
        "material_safety_stock": {
            "T3_DEFAULT": 30,
            "CHIP_T1": 5,
            "CHIP_T2": 8,
        },
    },
    "notifications": {
        "webhook_enabled": False,
        "webhook_url": "",
        "notify_on_daily_finish": True,
        "notify_on_recruit_alert": True,
        "notify_on_agent_task": True,
    },
    "roguelike": {
        "use_gui_config": True,
        "theme": "",
        "mode": "",
        "difficulty": None,
        "squad": "",
        "roles": "",
        "core_char": "",
        "starts_count": None,
        "investment_enabled": None,
        "investments_count": None,
        "investment_with_more_score": None,
        "stop_when_investment_full": None,
        "stop_at_final_boss": None,
        "stop_at_max_level": None,
        "use_support": None,
        "use_nonfriend_support": None,
        "refresh_trader_with_dice": None,
        "start_with_elite_two": None,
        "only_start_with_elite_two": None,
        "blackflow_cultivation_target": "",
        "blackflow_strategy": "",
        "ai_copilot_enabled": False,
        "ai_vision_enabled": True,
        "ai_decision_timeout_sec": 45.0,
        "ai_auto_fallback_on_timeout": True,
        "boss_prts_assist": True,
        "auto_claim_rewards": True,
        "auto_upgrade_tech_tree": True,
        "tech_tree_priority": [
            "hope_dp_recruit",
            "combat_stats",
            "shop_refresh",
        ],
        "timeout_sec": 7200,
    },
    "reclamation": {
        "use_gui_config": True,
        "theme": "",
        "mode": "",
        "tools_to_craft": [],
        "num_craft_batches": None,
        "increment_mode": None,
        "clear_store": None,
        "timeout_sec": 7200,
    },
    "post_action": "none",
}


def get_base_dir() -> Path:
    """Return writable base directory compatible with both PyInstaller frozen and source modes."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resolve_path(*parts: str) -> Path:
    """Resolve relative path segments under get_base_dir()."""
    return get_base_dir().joinpath(*parts)


def ensure_runtime_dirs(base_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Ensure writable runtime directories exist inside base_dir."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    dirs = {
        "base": root,
        "config": root / "config",
        "data": root / "data",
        "copilots": root / "copilots",
        "reports": root / "reports",
        "debug": root / "debug",
        "cache": root / "cache",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    return dirs


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = copy.deepcopy(val)
    return merged


def get_launcher_config_path(config_path: Optional[Path] = None) -> Path:
    return Path(config_path) if config_path is not None else (get_base_dir() / "config" / "launcher_config.json")


def load_launcher_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load launcher_config.json merged with safe universal defaults."""
    path = get_launcher_config_path(config_path)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return _deep_merge(DEFAULT_LAUNCHER_CONFIG, raw)
        except Exception:
            pass
    cfg = copy.deepcopy(DEFAULT_LAUNCHER_CONFIG)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cfg


def save_launcher_config(config: Dict[str, Any], config_path: Optional[Path] = None) -> Path:
    """Persist launcher configuration merged against schema defaults."""
    path = get_launcher_config_path(config_path)
    merged = _deep_merge(DEFAULT_LAUNCHER_CONFIG, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def check_write_permission(target_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Verify target directory is writable (guards against read-only Program Files installs)."""
    directory = Path(target_dir) if target_dir is not None else get_base_dir()
    probe = directory / f".write_probe_{os.getpid()}_{uuid.uuid4().hex[:6]}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return {"writable": True, "path": str(directory), "error": ""}
    except Exception as exc:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass
        hint = "当前目录无写入权限（如在 C:\\Program Files 下），请移至用户可写目录或以管理员权限运行。"
        return {"writable": False, "path": str(directory), "error": f"{exc} | {hint}"}


def get_maa_version(maa_dir: Union[str, Path]) -> str:
    """Detect MAA version string from CHANGELOG.md, directory name, or resource/version.json without loading DLLs."""
    root = Path(maa_dir)
    if not root.is_dir():
        return ""
    changelog = root / "data" / "CHANGELOG.md"
    if changelog.is_file():
        try:
            head = changelog.read_text(encoding="utf-8", errors="ignore")[:512]
            m = re.search(r"^##\s*(v?[\d]+(?:\.[\d]+)+(?:-[\w\.]+)?)", head, re.MULTILINE)
            if m:
                ver = m.group(1)
                return ver if ver.startswith("v") else f"v{ver}"
        except Exception:
            pass
    dir_m = re.search(r"(v?[\d]+\.[\d]+\.[\d]+)", root.name)
    if dir_m:
        ver = dir_m.group(1)
        return ver if ver.startswith("v") else f"v{ver}"
    ver_file = root / "resource" / "version.json"
    if ver_file.is_file():
        try:
            ver_data = json.loads(ver_file.read_text(encoding="utf-8"))
            lu = str(ver_data.get("last_updated", "")).strip()
            if lu:
                return lu[:10]
        except Exception:
            pass
    return ""


def check_maa_integrity(maa_dir: Union[str, Path]) -> Dict[str, Any]:
    """Validate MaaCore library, Python bindings, and resource files without loading the DLL."""
    root = Path(maa_dir)
    if not root.is_dir():
        return {
            "valid": False,
            "maa_dir": str(root),
            "missing_items": ["<directory_not_found>"],
            "version_hint": "",
            "maa_version": "",
            "reason": f"目录不存在: {root}",
        }

    sys_name = platform.system().lower()
    core_lib = {"windows": "MaaCore.dll", "darwin": "libMaaCore.dylib"}.get(sys_name, "libMaaCore.so")
    required_files = [
        core_lib,
        "Python/asst/asst.py",
        "resource/stages.json",
        "resource/item_index.json",
        "resource/recruitment.json",
        "resource/config.json",
    ]
    missing: List[str] = [rel for rel in required_files if not (root / rel).is_file()]
    if not (root / "resource" / "tasks").is_dir():
        missing.append("resource/tasks/")

    version_hint = ""
    ver_file = root / "resource" / "version.json"
    if ver_file.is_file():
        try:
            ver_data = json.loads(ver_file.read_text(encoding="utf-8"))
            version_hint = str(ver_data.get("last_updated", ""))
        except Exception:
            pass

    maa_version = get_maa_version(root)
    valid = len(missing) == 0
    reason = "MAA 核心文件完整" if valid else f"缺少核心文件（可能被杀毒软件误删或包不完整）: {', '.join(missing)}"
    return {
        "valid": valid,
        "maa_dir": str(root.resolve()),
        "missing_items": missing,
        "version_hint": version_hint,
        "maa_version": maa_version,
        "reason": reason,
    }


def _candidate_adb_paths() -> List[Path]:
    """Return common emulator ADB locations using environment variables and drive roots (no personal paths)."""
    rel_patterns = [
        "MuMuPlayer/nx_main/adb.exe",
        "MuMuPlayer/nx_device/12.0/shell/adb.exe",
        "MuMuPlayer-12.0/shell/adb.exe",
        "MuMuPlayerGlobal-12.0/shell/adb.exe",
        "Netease/MuMuPlayer/nx_main/adb.exe",
        "Netease/MuMuPlayer-12.0/shell/adb.exe",
        "Netease/MuMuPlayerGlobal-12.0/shell/adb.exe",
        "Program Files/Netease/MuMuPlayer/nx_main/adb.exe",
        "Program Files/Netease/MuMuPlayer-12.0/shell/adb.exe",
        "LDPlayer/LDPlayer9/adb.exe",
        "leidian/LDPlayer9/adb.exe",
        "BlueStacks_nxt/HD-Adb.exe",
        "Program Files/BlueStacks_nxt/HD-Adb.exe",
        "Nox/bin/nox_adb.exe",
        "Program Files (x86)/Nox/bin/nox_adb.exe",
    ]
    roots: List[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(env_key)
        if val:
            roots.append(Path(val))
    for drive in ("C:", "D:", "E:", "F:"):
        dp = Path(drive + os.sep)
        if dp.exists():
            roots.append(dp)

    candidates: List[Path] = []
    for base in roots:
        for rel in rel_patterns:
            candidates.append(base / rel)
    return candidates


def _normalize_exe_path(path_like: Union[str, Path]) -> str:
    try:
        return str(Path(path_like).resolve())
    except Exception:
        return str(path_like)


def _find_adb_under_root(root: Path) -> Optional[str]:
    if root.is_file() and root.name.lower().endswith(".exe") and "adb" in root.name.lower():
        return _normalize_exe_path(root)
    search_dirs = [root]
    if root.is_file():
        search_dirs = [root.parent, root.parent.parent]
    for base in search_dirs:
        for sub in (
            base / "adb.exe",
            base / "nx_main" / "adb.exe",
            base / "shell" / "adb.exe",
            base / "nx_device" / "12.0" / "shell" / "adb.exe",
            base / "HD-Adb.exe",
            base / "nox_adb.exe",
        ):
            if sub.is_file():
                return _normalize_exe_path(sub)
    return None


def _discover_running_emulator_adb() -> Optional[str]:
    """Locate adb.exe bundled with a currently running emulator process on Windows."""
    if sys.platform != "win32":
        return None
    try:
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -match '^(MuMuNxMain|MuMuNxDevice|MuMuPlayer|dnplayer|HD-Player|Nox)\\.exe$' } | "
            "Select-Object -ExpandProperty ExecutablePath"
        )
        res = run_cmd_cancellable(
            ["powershell", "-NoProfile", "-Command", ps],
            timeout_sec=3.0,
            text=True,
        )
        for line in (res.stdout or "").splitlines():
            exe = line.strip()
            if not exe:
                continue
            p = Path(exe)
            if p.is_file():
                found = _find_adb_under_root(p.parent) or _find_adb_under_root(p.parent.parent)
                if found:
                    return found
    except Exception:
        pass
    return None


def _read_maa_gui_connect_settings(maa_dir: Optional[Path]) -> Dict[str, Any]:
    if maa_dir is None:
        return {}
    gui_cfg = Path(maa_dir) / "config" / "gui.new.json"
    if not gui_cfg.is_file():
        return {}
    try:
        data = json.loads(gui_cfg.read_text(encoding="utf-8"))
        cur = data.get("Current", "Default")
        cs = data.get("Configurations", {}).get(cur, {}).get("Gui", {}).get("ConnectSettings", {})
        return cs if isinstance(cs, dict) else {}
    except Exception:
        return {}


def discover_adb_executable(config: Optional[Dict[str, Any]] = None, maa_dir: Optional[Path] = None) -> Optional[str]:
    """Locate a single canonical adb.exe: config -> emulator dir -> MAA gui config -> running emulator -> common dirs -> PATH."""
    cfg = config or {}
    emu = cfg.get("emulator", {}) if isinstance(cfg.get("emulator"), dict) else {}

    cfg_adb = str(emu.get("adb_path", "") or "").strip()
    if cfg_adb and Path(cfg_adb).is_file():
        return _normalize_exe_path(cfg_adb)

    emu_path = str(emu.get("path", "") or "").strip()
    if emu_path:
        ep = Path(emu_path)
        for sub in (
            ep,
            ep / "adb.exe",
            ep / "nx_main" / "adb.exe",
            ep / "shell" / "adb.exe",
            ep / "nx_device" / "12.0" / "shell" / "adb.exe",
            ep / "HD-Adb.exe",
        ):
            if sub.is_file() and sub.name.lower().endswith(".exe"):
                return _normalize_exe_path(sub)

    cs = _read_maa_gui_connect_settings(maa_dir)
    adb_str = str(cs.get("AdbPath", "") or "").strip()
    if adb_str and Path(adb_str).is_file():
        return _normalize_exe_path(adb_str)
    mumu_extra_path = str(
        (cs.get("Extras") or {}).get("MuMuEmulator12", {}).get("EmulatorPath", "")
        if isinstance(cs.get("Extras"), dict)
        else ""
    ).strip()
    if mumu_extra_path:
        found_extra = _find_adb_under_root(Path(mumu_extra_path))
        if found_extra:
            return found_extra

    running_adb = _discover_running_emulator_adb()
    if running_adb:
        return running_adb

    for cand in _candidate_adb_paths():
        if cand.is_file():
            return _normalize_exe_path(cand)

    which_adb = shutil.which("adb")
    if which_adb:
        return _normalize_exe_path(which_adb)
    return None


def discover_mumu_manager(
    config: Optional[Dict[str, Any]] = None,
    adb_path: str = "",
    maa_dir: Optional[Path] = None,
) -> Optional[str]:
    """Locate MuMuManager.exe from config, adb_path parent dirs, or MAA gui.new.json."""
    roots: List[Path] = []
    cfg = config or {}
    emu = cfg.get("emulator", {}) if isinstance(cfg.get("emulator"), dict) else {}
    emu_path = str(emu.get("path", "") or "").strip()
    if emu_path:
        ep = Path(emu_path)
        roots.extend([ep, ep.parent] if ep.is_file() else [ep])
    if adb_path:
        ap = Path(adb_path)
        if ap.is_file():
            roots.extend([ap.parent, ap.parent.parent, ap.parent.parent.parent])
    cs = _read_maa_gui_connect_settings(maa_dir)
    for key_path in (
        cs.get("AdbPath", ""),
        (cs.get("Extras") or {}).get("MuMuEmulator12", {}).get("EmulatorPath", "")
        if isinstance(cs.get("Extras"), dict)
        else "",
    ):
        s = str(key_path or "").strip()
        if s:
            kp = Path(s)
            roots.extend([kp.parent, kp.parent.parent] if kp.is_file() else [kp])

    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        for cand in (
            root / "MuMuManager.exe",
            root / "nx_main" / "MuMuManager.exe",
            root / "shell" / "MuMuManager.exe",
        ):
            if cand.is_file():
                return _normalize_exe_path(cand)
    return None


def parse_mumu_manager_info(raw_stdout: str) -> List[Dict[str, Any]]:
    """Parse `MuMuManager.exe info -v all` output across single-instance and multi-instance JSON layouts."""
    text = str(raw_stdout or "").strip()
    if not text:
        return []
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [i for i in (start_obj, start_arr) if i >= 0]
    if not starts:
        return []
    snippet = text[min(starts) :]
    try:
        parsed = json.loads(snippet)
    except Exception:
        # Trim trailing garbage if any
        end_pos = max(snippet.rfind("}"), snippet.rfind("]"))
        if end_pos < 0:
            return []
        try:
            parsed = json.loads(snippet[: end_pos + 1])
        except Exception:
            return []

    raw_items: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(parsed, list):
        for idx, item in enumerate(parsed):
            if isinstance(item, dict):
                raw_items.append((str(item.get("index", idx)), item))
    elif isinstance(parsed, dict):
        # Single-instance dict has keys like "adb_port", "player_state", "is_process_started", "index"
        if any(k in parsed for k in ("adb_port", "player_state", "is_process_started", "is_android_started", "pid", "name")):
            raw_items.append((str(parsed.get("index", "0")), parsed))
        else:
            for k, v in parsed.items():
                if isinstance(v, dict):
                    raw_items.append((str(v.get("index", k)), v))

    instances: List[Dict[str, Any]] = []
    for idx_str, item in raw_items:
        host = str(item.get("adb_host_ip") or "127.0.0.1").strip() or "127.0.0.1"
        raw_port = item.get("adb_port", 0)
        try:
            port = int(raw_port) if raw_port not in (None, "") else 0
        except Exception:
            port = 0
        pid_raw = item.get("pid", 0)
        try:
            pid = int(pid_raw) if pid_raw not in (None, "") else 0
        except Exception:
            pid = 0
        player_state = str(item.get("player_state") or "").strip()
        is_proc = bool(item.get("is_process_started", False))
        is_android = bool(item.get("is_android_started", False))
        # MuMu NX may report is_android_started=False even when player_state=="start_finished" and process is alive
        process_running = bool(is_proc or is_android or player_state.lower() == "start_finished" or pid > 0)
        instances.append(
            {
                "index": idx_str,
                "name": str(item.get("name") or f"MuMu-{idx_str}"),
                "adb_host_ip": host,
                "adb_port": port,
                "adb_addr": f"{host}:{port}" if port > 0 else "",
                "is_process_started": is_proc,
                "is_android_started": is_android,
                "player_state": player_state,
                "pid": pid,
                "process_running": process_running,
            }
        )
    return instances


def query_mumu_instances(
    mumu_mgr: Optional[str],
    timeout_sec: float = 4.0,
    cancel_event: Optional[threading.Event] = None,
) -> List[Dict[str, Any]]:
    """Query running MuMu instance metadata (including live ADB port) with short cancellable timeout."""
    if not mumu_mgr or not Path(mumu_mgr).is_file():
        return []
    try:
        res = run_cmd_cancellable(
            [mumu_mgr, "info", "-v", "all"],
            timeout_sec=min(float(timeout_sec), 5.0),
            cancel_event=cancel_event,
            text=True,
        )
        return parse_mumu_manager_info(res.stdout or "")
    except OperationCancelledError:
        raise
    except Exception:
        return []


def _run_adb(
    cmd: List[str],
    timeout_sec: float = DEFAULT_ADB_CMD_TIMEOUT,
    cancel_event: Optional[threading.Event] = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    return run_cmd_cancellable(
        cmd,
        timeout_sec=timeout_sec,
        cancel_event=cancel_event,
        text=text,
    )


def _parse_adb_devices(devices_stdout: str) -> Dict[str, str]:
    """Parse `adb devices` stdout into {serial: state} (including device/offline/unauthorized)."""
    result: Dict[str, str] = {}
    for line in (devices_stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            result[parts[0]] = parts[1].lower()
    return result


def _parse_online_devices(devices_stdout: str) -> List[str]:
    return [serial for serial, state in _parse_adb_devices(devices_stdout).items() if state == "device"]


def _parse_wm_size(wm_stdout: str) -> Optional[Tuple[int, int]]:
    override_match = re.search(r"Override size:\s*(\d+)\s*x\s*(\d+)", wm_stdout)
    if override_match:
        return int(override_match.group(1)), int(override_match.group(2))
    physical_match = re.search(r"Physical size:\s*(\d+)\s*x\s*(\d+)", wm_stdout)
    if physical_match:
        return int(physical_match.group(1)), int(physical_match.group(2))
    return None


def _tcp_port_open(addr: str, timeout_sec: float = 0.5) -> bool:
    if ":" not in addr:
        return False
    host, _, port_s = addr.rpartition(":")
    try:
        port = int(port_s)
    except Exception:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except Exception:
        return False


def verify_adb_screencap(
    adb_path: str,
    adb_addr: str,
    timeout_sec: float = DEFAULT_ADB_CMD_TIMEOUT,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Verify ADB device can immediately return a valid PNG screenshot via exec-out screencap -p."""
    if not adb_path or not adb_addr:
        return False
    try:
        res = _run_adb(
            [adb_path, "-s", adb_addr, "exec-out", "screencap", "-p"],
            timeout_sec=min(float(timeout_sec), 5.0),
            cancel_event=cancel_event,
            text=False,
        )
        raw = res.stdout if isinstance(res.stdout, (bytes, bytearray)) else b""
        return res.returncode == 0 and len(raw) > 100 and bytes(raw[:8]) == b"\x89PNG\r\n\x1a\n"
    except OperationCancelledError:
        raise
    except Exception:
        return False


def _build_resolution_ok(
    adb_path: str,
    target: str,
    dims: Tuple[int, int],
    healed: bool = False,
    screencap_ok: Optional[bool] = None,
) -> Dict[str, Any]:
    w, h = dims
    long_side, short_side = max(w, h), min(w, h)
    ratio = (long_side / short_side) if short_side > 0 else 0.0
    is_16_9 = short_side >= 720 and abs(ratio - (16.0 / 9.0)) <= 0.02
    warning = ""
    if not is_16_9:
        warning = f"当前模拟器分辨率 {w}x{h} 不符合 16:9 (>=720p) 推荐比例，建议调整为 1280x720 或 1920x1080。"
    out: Dict[str, Any] = {
        "connected": True,
        "adb_path": adb_path,
        "adb_address": target,
        "width": w,
        "height": h,
        "is_16_9": is_16_9,
        "warning": warning,
        "healed": healed,
        "mumu_adbd_hung": False,
    }
    if screencap_ok is not None:
        out["screencap_ok"] = screencap_ok
    return out


def _safe_adb_call(
    cmd: List[str],
    timeout_sec: float,
    cancel_event: Optional[threading.Event],
) -> Tuple[Optional[subprocess.CompletedProcess], str]:
    try:
        return _run_adb(cmd, timeout_sec=timeout_sec, cancel_event=cancel_event), ""
    except OperationCancelledError:
        raise
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as exc:
        return None, str(exc)


def check_adb_resolution(
    adb_path: str,
    adb_address: str = "",
    timeout_sec: float = DEFAULT_ADB_CMD_TIMEOUT,
    config: Optional[Dict[str, Any]] = None,
    maa_dir: Optional[Path] = None,
    cancel_event: Optional[threading.Event] = None,
    allow_self_heal: bool = True,
    allow_kill_server: bool = True,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Verify emulator ADB connectivity (with offline/port-drift self-healing) and confirm 16:9 (>=720p)."""
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelledError("ADB 检测已取消")
    if not adb_path:
        return {
            "connected": False,
            "adb_path": "",
            "adb_address": adb_address,
            "width": None,
            "height": None,
            "is_16_9": False,
            "warning": "未找到 adb 可执行文件，跳过分辨率检测。",
            "healed": False,
            "mumu_adbd_hung": False,
        }

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    eff_timeout = min(max(1.0, float(timeout_sec)), 5.0)
    target = adb_address.strip()
    last_detail = ""
    saw_offline = False
    saw_timeout = False
    dev_map: Dict[str, str] = {}

    try:
        # ---- 1. Fast path (2 calls when healthy; compatible with mocked unit tests) ----
        if target:
            conn_res, conn_err = _safe_adb_call([adb_path, "connect", target], eff_timeout, cancel_event)
            if conn_err == "timeout":
                saw_timeout = True
            wm_res, wm_err = _safe_adb_call([adb_path, "-s", target, "shell", "wm", "size"], eff_timeout, cancel_event)
            if wm_err == "timeout":
                saw_timeout = True
            if wm_res is not None:
                dims = _parse_wm_size(wm_res.stdout or "")
                if dims:
                    return _build_resolution_ok(adb_path, target, dims, healed=False)
                out_txt = (wm_res.stdout or wm_res.stderr or "").strip()
                last_detail = out_txt
                if "offline" in out_txt.lower():
                    saw_offline = True
        else:
            dev_res, dev_err = _safe_adb_call([adb_path, "devices"], eff_timeout, cancel_event)
            if dev_err == "timeout":
                saw_timeout = True
            if dev_res is not None:
                dev_map = _parse_adb_devices(dev_res.stdout or "")
                online = [s for s, st in dev_map.items() if st == "device"]
                if any(st == "offline" for st in dev_map.values()):
                    saw_offline = True
                if online:
                    cand = online[0]
                    wm_res, wm_err = _safe_adb_call([adb_path, "-s", cand, "shell", "wm", "size"], eff_timeout, cancel_event)
                    if wm_err == "timeout":
                        saw_timeout = True
                    if wm_res is not None:
                        dims = _parse_wm_size(wm_res.stdout or "")
                        if dims:
                            return _build_resolution_ok(adb_path, cand, dims, healed=False)
                        last_detail = (wm_res.stdout or wm_res.stderr or "").strip()
                        if "offline" in last_detail.lower():
                            saw_offline = True
                        target = cand

        if not allow_self_heal:
            return {
                "connected": False,
                "adb_path": adb_path,
                "adb_address": target,
                "width": None,
                "height": None,
                "is_16_9": False,
                "warning": last_detail or "未检测到在线模拟器 ADB 设备。",
                "healed": False,
                "mumu_adbd_hung": False,
            }

        # ---- 2. Self-healing path: inspect devices, disconnect offline, re-probe MuMu port & reconnect ----
        if not dev_map:
            dev_res, dev_err = _safe_adb_call([adb_path, "devices"], eff_timeout, cancel_event)
            if dev_err == "timeout":
                saw_timeout = True
            if dev_res is not None:
                dev_map = _parse_adb_devices(dev_res.stdout or "")

        offline_serials = [s for s, st in dev_map.items() if st == "offline"]
        if offline_serials:
            saw_offline = True

        # Disconnect stale offline/unresponsive TCP serial(s)
        disconnect_targets: List[str] = []
        if target and ":" in target:
            disconnect_targets.append(target)
        for s in offline_serials:
            if ":" in s and s not in disconnect_targets:
                disconnect_targets.append(s)

        for dt in disconnect_targets:
            _log(f"[ADB] 检测到设备 {dt} 不可达或处于 offline，执行 adb disconnect 后重新探测端口并重连...")
            _safe_adb_call([adb_path, "disconnect", dt], min(eff_timeout, 4.0), cancel_event)

        # Query MuMuManager for live running instance port(s)
        mumu_mgr = discover_mumu_manager(config=config, adb_path=adb_path, maa_dir=maa_dir)
        mumu_instances = query_mumu_instances(mumu_mgr, timeout_sec=min(eff_timeout, 4.0), cancel_event=cancel_event)
        mumu_running_addrs = [
            inst["adb_addr"] for inst in mumu_instances if inst.get("process_running") and inst.get("adb_addr")
        ]

        candidates: List[str] = []
        for addr in mumu_running_addrs:
            if addr not in candidates:
                candidates.append(addr)
        if target and target not in candidates:
            candidates.append(target)
        for s, st in dev_map.items():
            if st == "device" and s not in candidates:
                candidates.append(s)
        for common_addr in ("127.0.0.1:16384", "127.0.0.1:5555", "127.0.0.1:7555", "127.0.0.1:62001"):
            if common_addr not in candidates:
                candidates.append(common_addr)

        def _try_candidate(cand_addr: str) -> Optional[Tuple[int, int]]:
            nonlocal saw_timeout, saw_offline, last_detail
            if ":" in cand_addr:
                c_res, c_err = _safe_adb_call([adb_path, "connect", cand_addr], eff_timeout, cancel_event)
                if c_err == "timeout":
                    saw_timeout = True
                    last_detail = f"adb connect {cand_addr} 超时"
                    return None
                if c_res is not None:
                    c_out = (c_res.stdout or c_res.stderr or "").lower()
                    if "cannot connect" in c_out or "unable to connect" in c_out or "refused" in c_out:
                        return None
            w_res, w_err = _safe_adb_call([adb_path, "-s", cand_addr, "shell", "wm", "size"], eff_timeout, cancel_event)
            if w_err == "timeout":
                saw_timeout = True
                last_detail = f"adb shell wm size ({cand_addr}) 超时"
                return None
            if w_res is not None:
                parsed_dims = _parse_wm_size(w_res.stdout or "")
                if parsed_dims:
                    return parsed_dims
                txt = (w_res.stdout or w_res.stderr or "").strip()
                if txt:
                    last_detail = txt
                    if "offline" in txt.lower():
                        saw_offline = True
            return None

        for cand in candidates:
            dims = _try_candidate(cand)
            if dims:
                _log(f"[ADB] 自愈重连成功: {cand} ({dims[0]}x{dims[1]})")
                return _build_resolution_ok(adb_path, cand, dims, healed=True)

        # ---- 3. Local ADB server reset fallback if we saw offline/timeout or running MuMu ----
        if allow_kill_server and (saw_offline or saw_timeout or bool(mumu_running_addrs)):
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelledError("ADB 检测已取消")
            _log("[ADB] 重连未恢复，正在重启本地 ADB 服务 (kill-server -> start-server) 并重试...")
            try:
                _safe_adb_call([adb_path, "kill-server"], 4.0, cancel_event)
            finally:
                # Always ensure adb server is brought back up so cancellation never leaves global adb daemon dead
                _safe_adb_call([adb_path, "start-server"], eff_timeout, None)
            if cancel_event is not None and cancel_event.is_set():
                raise OperationCancelledError("ADB 检测已取消")
            retry_list = list(dict.fromkeys([*mumu_running_addrs, *([target] if target else ["127.0.0.1:16384"])]))
            for cand in retry_list:
                dims = _try_candidate(cand)
                if dims:
                    _log(f"[ADB] 重启 ADB 服务后连接成功: {cand} ({dims[0]}x{dims[1]})")
                    return _build_resolution_ok(adb_path, cand, dims, healed=True)

        # ---- 4. Detect MuMu running yet guest adbd hung (TCP open, zero ADB handshake reply) ----
        primary_addr = mumu_running_addrs[0] if mumu_running_addrs else target
        mumu_running = any(inst.get("process_running") for inst in mumu_instances)
        mumu_adbd_hung = bool(
            mumu_running
            and primary_addr
            and ":" in primary_addr
            and (saw_offline or saw_timeout)
            and _tcp_port_open(primary_addr, timeout_sec=0.5)
        )
        if mumu_adbd_hung:
            warn_msg = (
                f"检测到 MuMu 模拟器已运行但内置 adbd 服务假死（{primary_addr} 端口开放但 ADB 握手/命令无响应），"
                f"请重启 MuMu 模拟器后重试。"
            )
        elif last_detail:
            warn_msg = f"无法连接或解析模拟器分辨率 ({primary_addr or '未指定'}): {last_detail}"
        else:
            warn_msg = "未检测到在线模拟器 ADB 设备（模拟器可能尚未启动）。"

        return {
            "connected": False,
            "adb_path": adb_path,
            "adb_address": primary_addr,
            "width": None,
            "height": None,
            "is_16_9": False,
            "warning": warn_msg,
            "healed": False,
            "mumu_adbd_hung": mumu_adbd_hung,
        }
    except OperationCancelledError:
        raise
    except Exception as exc:
        return {
            "connected": False,
            "adb_path": adb_path,
            "adb_address": target,
            "width": None,
            "height": None,
            "is_16_9": False,
            "warning": f"ADB 分辨率检测异常: {exc}",
            "healed": False,
            "mumu_adbd_hung": False,
        }


def run_env_diagnostics(
    config: Optional[Dict[str, Any]] = None,
    maa_dir: Optional[Path] = None,
    check_adb: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Run full environment health diagnostics for CLI and GUI inspection."""
    cfg = config if config is not None else load_launcher_config()
    base = get_base_dir()
    write_perm = check_write_permission(base)
    maa_check = (
        check_maa_integrity(maa_dir)
        if maa_dir is not None
        else {"valid": False, "maa_dir": "", "missing_items": ["<not_configured>"], "version_hint": "", "reason": "尚未定位 MAA 目录"}
    )
    adb_exe = discover_adb_executable(cfg, maa_dir=maa_dir)
    if check_adb and adb_exe:
        emu_addr = str(cfg.get("emulator", {}).get("adb_address", "") or "")
        adb_check = check_adb_resolution(
            adb_exe,
            adb_address=emu_addr,
            config=cfg,
            maa_dir=maa_dir,
            cancel_event=cancel_event,
        )
    else:
        adb_check = {
            "connected": False,
            "adb_path": adb_exe or "",
            "adb_address": str(cfg.get("emulator", {}).get("adb_address", "") or ""),
            "width": None,
            "height": None,
            "is_16_9": False,
            "warning": "跳过 ADB 检测" if not check_adb else "未发现可用 adb.exe",
            "healed": False,
            "mumu_adbd_hung": False,
        }

    return {
        "base_dir": str(base),
        "frozen": bool(getattr(sys, "frozen", False)),
        "write_permission": write_perm,
        "maa_integrity": maa_check,
        "adb_path": adb_exe or "",
        "adb_resolution": adb_check,
        "healthy": bool(write_perm["writable"] and maa_check["valid"]),
    }
