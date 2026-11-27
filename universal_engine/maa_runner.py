"""Dynamic MAA Core SDK runner, generic emulator/ADB bridge, and autonomous copilot executor."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ai_strategist, auto_growth, roguelike_ai_copilot, stage_pilot
from .utils import runtime_guard
from .utils.env_doctor import (
    DEFAULT_ADB_CMD_TIMEOUT,
    SERVER_PACKAGES,
    OperationCancelledError,
    check_adb_resolution,
    discover_adb_executable,
    discover_mumu_manager,
    ensure_runtime_dirs,
    get_base_dir,
    get_maa_version,
    query_mumu_instances,
    run_cmd_cancellable,
    verify_adb_screencap,
    wait_interruptible,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_LOADED_MAA_DIR: Optional[Path] = None
POLL_INTERVAL = 3.0


def _run_cmd(
    cmd: List[str],
    timeout: float = DEFAULT_ADB_CMD_TIMEOUT,
    cancel_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess:
    return run_cmd_cancellable(cmd, timeout_sec=timeout, cancel_event=cancel_event, text=True)


def _import_asst_classes(maa_dir: Path) -> Tuple[Any, Any, Any]:
    """Import Asst, InstanceOptionType, Message from preloaded modules or <maa_dir>/Python."""
    if "asst.asst" not in sys.modules or "asst.utils" not in sys.modules:
        py_dir = str(Path(maa_dir) / "Python")
        if py_dir not in sys.path:
            sys.path.insert(0, py_dir)
    from asst.asst import Asst  # type: ignore
    from asst.utils import InstanceOptionType, Message  # type: ignore

    return Asst, InstanceOptionType, Message


def init_maa_sdk(
    maa_dir: Path,
    base_dir: Optional[Path] = None,
    use_cache_incremental: bool = True,
) -> bool:
    """Dynamically load MaaCore.dll and resources with isolated user_dir under base_dir."""
    global _LOADED_MAA_DIR
    maa_resolved = Path(maa_dir).resolve()
    if _LOADED_MAA_DIR == maa_resolved:
        return True

    root = Path(base_dir) if base_dir is not None else get_base_dir()
    ensure_runtime_dirs(root)
    Asst, _, _ = _import_asst_classes(maa_resolved)

    inc_dir: Optional[Path] = None
    if use_cache_incremental:
        local_cache = root / "cache"
        ext_cache = maa_resolved / "cache"
        if (local_cache / "resource").is_dir():
            inc_dir = local_cache
        elif (ext_cache / "resource").is_dir():
            inc_dir = ext_cache

    ok = Asst.load(
        path=str(maa_resolved),
        incremental_path=str(inc_dir) if inc_dir is not None else None,
        user_dir=str(root),
    )
    if ok:
        _LOADED_MAA_DIR = maa_resolved
    return bool(ok)


def get_server_spec(config: Dict[str, Any]) -> Dict[str, str]:
    server = str(config.get("server", "Official") or "Official")
    return SERVER_PACKAGES.get(server, SERVER_PACKAGES["Official"])


def _detect_connection_config(emu_cfg: Dict[str, Any], adb_path: str, adb_addr: str) -> str:
    emu_type = str(emu_cfg.get("type", "auto") or "auto").strip().lower()
    mapping = {
        "mumu12": "MuMuEmulator12",
        "mumu": "MuMuEmulator12",
        "ldplayer": "LDPlayer",
        "bluestacks": "BlueStacks",
        "nox": "Nox",
        "xyaz": "XYAZ",
        "memu": "XYAZ",
    }
    if emu_type in mapping:
        return mapping[emu_type]

    hint = f"{emu_cfg.get('path', '')} {adb_path} {adb_addr}".lower()
    if "mumu" in hint or ":16384" in hint or ":7555" in hint:
        return "MuMuEmulator12"
    if "ldplayer" in hint or "leidian" in hint:
        return "LDPlayer"
    if "bluestacks" in hint or "hd-adb" in hint:
        return "BlueStacks"
    if "nox" in hint or ":62001" in hint:
        return "Nox"
    return "General"


def _adb_device_ready(
    adb_path: str,
    adb_addr: str,
    cancel_event: Optional[threading.Event] = None,
    verify_screencap: bool = True,
) -> bool:
    if not adb_path or not adb_addr:
        return False
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelledError("等待 ADB 就绪已取消")
    try:
        _run_cmd([adb_path, "connect", adb_addr], timeout=DEFAULT_ADB_CMD_TIMEOUT, cancel_event=cancel_event)
        st = _run_cmd([adb_path, "-s", adb_addr, "get-state"], timeout=4.0, cancel_event=cancel_event)
        if "device" not in (st.stdout or ""):
            return False
        if verify_screencap and verify_adb_screencap(
            adb_path, adb_addr, timeout_sec=DEFAULT_ADB_CMD_TIMEOUT, cancel_event=cancel_event
        ):
            return True
        boot = _run_cmd(
            [adb_path, "-s", adb_addr, "shell", "getprop", "sys.boot_completed"],
            timeout=4.0,
            cancel_event=cancel_event,
        )
        return (boot.stdout or "").strip() == "1"
    except OperationCancelledError:
        raise
    except Exception:
        return False


def _probe_adb_address(
    adb_path: str,
    config: Dict[str, Any],
    maa_dir: Optional[Path],
    cancel_event: Optional[threading.Event] = None,
) -> str:
    emu_cfg = config.get("emulator", {}) if isinstance(config.get("emulator"), dict) else {}
    addr = str(emu_cfg.get("adb_address", "") or "").strip()
    if addr:
        return addr

    if maa_dir is not None:
        gui_cfg = Path(maa_dir) / "config" / "gui.new.json"
        if gui_cfg.is_file():
            try:
                data = json.loads(gui_cfg.read_text(encoding="utf-8"))
                cur = data.get("Current", "Default")
                saved_addr = (
                    data.get("Configurations", {})
                    .get(cur, {})
                    .get("Gui", {})
                    .get("ConnectSettings", {})
                    .get("Address", "")
                )
                if saved_addr:
                    return str(saved_addr).strip()
            except Exception:
                pass

    mumu_mgr = discover_mumu_manager(config=config, adb_path=adb_path, maa_dir=maa_dir)
    if mumu_mgr:
        for inst in query_mumu_instances(mumu_mgr, timeout_sec=4.0, cancel_event=cancel_event):
            if inst.get("process_running") and inst.get("adb_addr"):
                return str(inst["adb_addr"])

    if adb_path:
        try:
            res = _run_cmd([adb_path, "devices"], timeout=4.0, cancel_event=cancel_event)
            for line in (res.stdout or "").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "device":
                    return parts[0]
        except OperationCancelledError:
            raise
        except Exception:
            pass
        for candidate in ("127.0.0.1:16384", "127.0.0.1:5555", "127.0.0.1:7555", "127.0.0.1:62001"):
            try:
                conn = _run_cmd([adb_path, "connect", candidate], timeout=3.5, cancel_event=cancel_event)
                if "connected" in (conn.stdout or "").lower():
                    return candidate
            except OperationCancelledError:
                raise
            except Exception:
                pass

    return "127.0.0.1:16384"


def _find_emulator_launcher(emu_cfg: Dict[str, Any], adb_path: str) -> Optional[Tuple[str, List[str]]]:
    idx = str(emu_cfg.get("instance_index", 0))
    search_roots: List[Path] = []
    cfg_path = str(emu_cfg.get("path", "") or "").strip()
    if cfg_path:
        p = Path(cfg_path)
        if p.is_file():
            return "direct", [str(p)]
        search_roots.append(p)
    if adb_path:
        ap = Path(adb_path)
        if ap.is_file():
            search_roots.extend([ap.parent, ap.parent.parent, ap.parent.parent.parent])

    for root in search_roots:
        for mumu_mgr in (root / "MuMuManager.exe", root / "nx_main" / "MuMuManager.exe", root / "shell" / "MuMuManager.exe"):
            if mumu_mgr.is_file():
                return "mumu", [str(mumu_mgr), "control", "-v", idx, "launch"]
        for dn in (root / "dnconsole.exe",):
            if dn.is_file():
                return "ldplayer", [str(dn), "launch", "--index", idx]
        for exe in (root / "MuMuNxMain.exe", root / "nx_main" / "MuMuNxMain.exe", root / "HD-Player.exe", root / "Nox.exe"):
            if exe.is_file():
                return "direct", [str(exe)]
    return None


def wait_for_emulator_and_adb(
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    timeout_sec: int = 120,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    verify_screencap: bool = True,
) -> Dict[str, Any]:
    """Wait for emulator & ADB readiness with self-healing, screencap fast-path, and <=0.5s cancel polling."""
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelledError("等待模拟器与 ADB 就绪已取消")

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    emu_cfg = config.get("emulator", {}) if isinstance(config.get("emulator"), dict) else {}
    adb_path = discover_adb_executable(config, maa_dir=maa_dir)
    if not adb_path:
        raise RuntimeError("未找到可用的 adb 可执行文件，请在配置中指定 emulator.adb_path 或先安装模拟器")
    adb_addr = _probe_adb_address(adb_path, config, maa_dir, cancel_event=cancel_event)

    def _check_ready_once(cur_addr: str, allow_heal: bool = True) -> Tuple[bool, str, bool, str]:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelledError("等待模拟器与 ADB 就绪已取消")
        if _adb_device_ready(adb_path, cur_addr, cancel_event=cancel_event, verify_screencap=verify_screencap):
            return True, cur_addr, False, ""
        if not allow_heal:
            return False, cur_addr, False, ""

        heal = check_adb_resolution(
            adb_path,
            adb_address=cur_addr,
            timeout_sec=DEFAULT_ADB_CMD_TIMEOUT,
            config=config,
            maa_dir=maa_dir,
            cancel_event=cancel_event,
            allow_self_heal=True,
            allow_kill_server=True,
            log_callback=log_callback,
        )
        healed_addr = str(heal.get("adb_address") or cur_addr)
        if heal.get("connected"):
            if not verify_screencap or verify_adb_screencap(
                adb_path, healed_addr, timeout_sec=DEFAULT_ADB_CMD_TIMEOUT, cancel_event=cancel_event
            ):
                return True, healed_addr, False, ""
            if _adb_device_ready(adb_path, healed_addr, cancel_event=cancel_event, verify_screencap=False):
                return True, healed_addr, False, ""
        if heal.get("mumu_adbd_hung"):
            return False, healed_addr, True, str(heal.get("warning") or "")
        return False, healed_addr, False, str(heal.get("warning") or "")

    ready_now, active_addr, adbd_hung, diag_warn = _check_ready_once(adb_addr, allow_heal=True)
    if ready_now:
        conn_cfg = _detect_connection_config(emu_cfg, adb_path, active_addr)
        return {
            "ready": True,
            "launched_by_us": False,
            "adb_path": adb_path,
            "adb_addr": active_addr,
            "connection_config": conn_cfg,
        }

    # If MuMu is already running (`start_finished`) yet guest adbd is hung even after disconnect + kill-server,
    # fail fast with actionable diagnosis instead of blindly blocking 120s.
    if adbd_hung:
        _log(f"[WARN][ADB] {diag_warn}")
        raise RuntimeError(diag_warn)

    launched = False
    if emu_cfg.get("auto_launch", True):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelledError("等待模拟器与 ADB 就绪已取消")
        launcher = _find_emulator_launcher(emu_cfg, adb_path)
        if launcher is not None:
            kind, cmd = launcher
            _log(f"[ADB] 正在拉起模拟器 ({kind})...")
            if kind == "direct":
                subprocess.Popen(cmd, creationflags=_NO_WINDOW)
            else:
                try:
                    _run_cmd(cmd, timeout=8.0, cancel_event=cancel_event)
                except OperationCancelledError:
                    raise
                except Exception:
                    pass
            launched = True

    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    attempt = 0
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelledError("等待模拟器与 ADB 就绪已取消")

        attempt += 1
        ok, active_addr, hung_now, warn_now = _check_ready_once(active_addr, allow_heal=(attempt % 3 == 0))
        if ok:
            conn_cfg = _detect_connection_config(emu_cfg, adb_path, active_addr)
            return {
                "ready": True,
                "launched_by_us": launched,
                "adb_path": adb_path,
                "adb_addr": active_addr,
                "connection_config": conn_cfg,
            }
        if hung_now and not launched:
            _log(f"[WARN][ADB] {warn_now}")
            raise RuntimeError(warn_now)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if wait_interruptible(min(2.0, remaining), cancel_event=cancel_event, step=0.5):
            raise OperationCancelledError("等待模拟器与 ADB 就绪已取消")

    raise RuntimeError(f"等待模拟器与 ADB ({active_addr}) 就绪超时 ({timeout_sec}s)")


def ensure_emulator_ready(
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    timeout_sec: int = 120,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    verify_screencap: bool = True,
) -> Dict[str, Any]:
    """Verify emulator/ADB readiness; launch configured/detected emulator if auto_launch is enabled."""
    return wait_for_emulator_and_adb(
        config=config,
        maa_dir=maa_dir,
        timeout_sec=timeout_sec,
        cancel_event=cancel_event,
        log_callback=log_callback,
        verify_screencap=verify_screencap,
    )


def probe_and_ensure_adb_ready(
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    timeout_sec: int = 60,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    auto_launch: bool = True,
) -> Dict[str, Any]:
    """Unified port probe + adb connect + emulator auto-launch shared by GUI probe button and all task entrypoints."""
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelledError("ADB 探测与自动重连已取消")

    emu_cfg = config.get("emulator", {}) if isinstance(config.get("emulator"), dict) else {}
    adb_exe = discover_adb_executable(config, maa_dir=maa_dir)
    emu_addr = str(emu_cfg.get("adb_address", "") or "")
    res = check_adb_resolution(
        adb_exe or "",
        adb_address=emu_addr,
        config=config,
        maa_dir=maa_dir,
        cancel_event=cancel_event,
        allow_self_heal=True,
        allow_kill_server=False,
        log_callback=log_callback,
    )
    if res.get("connected"):
        addr = str(res.get("adb_address") or emu_addr)
        path = str(res.get("adb_path") or adb_exe or "")
        return {
            **res,
            "ready": True,
            "launched_by_us": False,
            "adb_path": path,
            "adb_addr": addr,
            "connection_config": _detect_connection_config(emu_cfg, path, addr),
        }

    if auto_launch and not res.get("mumu_adbd_hung"):
        if cancel_event is not None and cancel_event.is_set():
            raise OperationCancelledError("ADB 探测与自动重连已取消")
        try:
            env_info = ensure_emulator_ready(
                config,
                maa_dir=maa_dir,
                timeout_sec=timeout_sec,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )
            res2 = check_adb_resolution(
                env_info["adb_path"],
                adb_address=env_info["adb_addr"],
                config=config,
                maa_dir=maa_dir,
                cancel_event=cancel_event,
                allow_self_heal=False,
                allow_kill_server=False,
            )
            merged = dict(res2)
            merged["connected"] = True
            merged["ready"] = True
            merged["adb_path"] = env_info["adb_path"]
            merged["adb_address"] = env_info["adb_addr"]
            merged["adb_addr"] = env_info["adb_addr"]
            merged["connection_config"] = env_info["connection_config"]
            merged["launched_by_us"] = bool(env_info.get("launched_by_us", False))
            return merged
        except OperationCancelledError:
            raise
        except Exception as exc:
            res = dict(res)
            res["warning"] = str(exc)

    addr_fallback = str(res.get("adb_address") or emu_addr)
    path_fallback = str(res.get("adb_path") or adb_exe or "")
    return {
        **res,
        "ready": bool(res.get("connected")),
        "launched_by_us": False,
        "adb_path": path_fallback,
        "adb_addr": addr_fallback,
        "connection_config": _detect_connection_config(emu_cfg, path_fallback, addr_fallback),
    }


def ensure_game_running(
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    wait_after_launch: int = 8,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    env_info: Optional[Dict[str, Any]] = None,
) -> str:
    """Ensure Arknights (Official or Bilibili) is in foreground on connected device."""
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelledError("启动游戏前置检测已取消")
    _dry_ctx = runtime_guard.get_active_dry_run_context()
    if _dry_ctx is not None:
        _spec = get_server_spec(config)
        _dry_ctx.intercept_control(
            "ensure_game_running",
            detail=f"跳过 dumpsys/am start ({_spec['package']}/{_spec['activity']})",
            command=["am", "start", "-n", f"{_spec['package']}/{_spec['activity']}"],
            log_callback=log_callback,
        )
        return _spec["package"]
    info = env_info or ensure_emulator_ready(
        config, maa_dir=maa_dir, cancel_event=cancel_event, log_callback=log_callback
    )
    adb_path = info["adb_path"]
    adb_addr = info["adb_addr"]
    spec = get_server_spec(config)
    pkg = spec["package"]
    act = spec["activity"]

    res = _run_cmd(
        [adb_path, "-s", adb_addr, "shell", "dumpsys", "window", "windows"],
        timeout=5.0,
        cancel_event=cancel_event,
    )
    if pkg not in (res.stdout or ""):
        _run_cmd(
            [adb_path, "-s", adb_addr, "shell", "am", "start", "-n", f"{pkg}/{act}"],
            timeout=5.0,
            cancel_event=cancel_event,
        )
        if wait_after_launch > 0:
            if wait_interruptible(wait_after_launch, cancel_event=cancel_event, step=0.5):
                raise OperationCancelledError("等待游戏前台启动已取消")
    return pkg


def capture_adb_screenshot(
    adb_path: str,
    adb_addr: str,
    out_path: Path,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Capture emulator screenshot via ADB to out_path."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _run_cmd([adb_path, "connect", adb_addr], timeout=5.0, cancel_event=cancel_event)
        r1 = run_cmd_cancellable(
            [adb_path, "-s", adb_addr, "exec-out", "screencap", "-p"],
            timeout_sec=5.0,
            cancel_event=cancel_event,
            text=False,
        )
        raw = r1.stdout if isinstance(r1.stdout, (bytes, bytearray)) else b""
        if r1.returncode == 0 and len(raw) > 100 and bytes(raw[:8]) == b"\x89PNG\r\n\x1a\n":
            out_path.write_bytes(bytes(raw))
            return out_path.is_file()
        run_cmd_cancellable(
            [adb_path, "-s", adb_addr, "shell", "screencap", "-p", "/sdcard/_maa_end.png"],
            timeout_sec=5.0,
            cancel_event=cancel_event,
            text=False,
        )
        run_cmd_cancellable(
            [adb_path, "-s", adb_addr, "pull", "/sdcard/_maa_end.png", str(out_path)],
            timeout_sec=5.0,
            cancel_event=cancel_event,
            text=False,
        )
        return out_path.is_file()
    except Exception:
        return False


def execute_post_action(
    action: str,
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute configured post-task action: none, close_emulator, or shutdown."""
    act = str(action or config.get("post_action") or "none").strip().lower()
    if act not in ("none", "close_emulator", "shutdown"):
        act = "none"
    _dry_ctx = runtime_guard.get_active_dry_run_context()
    if dry_run or runtime_guard.is_dry_run_enabled():
        dry_run = True

    if act == "none":
        return {"action": "none", "executed": False, "dry_run": dry_run, "command": [], "detail": "无后续动作"}

    if act == "shutdown":
        if sys.platform == "win32":
            cmd = ["shutdown", "/s", "/t", "30", "/c", "MAA 任务已完成，将在 30 秒后关机（可执行 shutdown /a 取消）"]
        else:
            cmd = ["shutdown", "-h", "+1"]
        if dry_run:
            if _dry_ctx is not None:
                return _dry_ctx.intercept_control("shutdown", detail="Dry-run 关机命令已就绪", command=cmd)
            return {"action": "shutdown", "executed": False, "dry_run": True, "command": cmd, "detail": "Dry-run 关机命令已就绪"}
        res = _run_cmd(cmd, timeout=10)
        return {"action": "shutdown", "executed": res.returncode == 0, "dry_run": False, "command": cmd, "detail": (res.stdout or res.stderr or "").strip()}

    # close_emulator
    emu_cfg = config.get("emulator", {}) if isinstance(config.get("emulator"), dict) else {}
    adb_path = discover_adb_executable(config, maa_dir=maa_dir) or "adb"
    adb_addr = _probe_adb_address(adb_path, config, maa_dir)
    idx = str(emu_cfg.get("instance_index", 0))

    cmd: List[str] = [adb_path, "-s", adb_addr, "shell", "reboot", "-p"]
    launcher = _find_emulator_launcher(emu_cfg, adb_path)
    if launcher is not None:
        kind, launch_cmd = launcher
        exe_path = launch_cmd[0]
        if kind == "mumu":
            cmd = [exe_path, "control", "-v", idx, "shutdown"]
        elif kind == "ldplayer":
            cmd = [exe_path, "quit", "--index", idx]

    if dry_run:
        if _dry_ctx is not None:
            return _dry_ctx.intercept_control("close_emulator", detail="Dry-run 关闭模拟器命令已就绪", command=cmd)
        return {"action": "close_emulator", "executed": False, "dry_run": True, "command": cmd, "detail": "Dry-run 关闭模拟器命令已就绪"}

    res = _run_cmd(cmd, timeout=15)
    return {
        "action": "close_emulator",
        "executed": res.returncode == 0,
        "dry_run": False,
        "command": cmd,
        "detail": (res.stdout or res.stderr or "").strip(),
    }


def _notify_windows_alert(title: str, message: str) -> None:
    if runtime_guard.is_dry_run_enabled():
        return
    if sys.platform != "win32":
        return
    try:
        safe_title = title.replace("'", " ")
        safe_msg = message.replace("'", " ")
        ps_cmd = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; "
            "$n.Visible = $true; "
            f"$n.ShowBalloonTip(8000, '{safe_title}', '{safe_msg}', [System.Windows.Forms.ToolTipIcon]::Warning); "
            "Start-Sleep -Seconds 2; $n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps_cmd],
            creationflags=_NO_WINDOW,
        )
    except Exception:
        pass


def send_webhook_notification(
    config: Dict[str, Any],
    title: str,
    content: str,
    event_type: str = "info",
    extra: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Send non-fatal JSON webhook notification (Feishu, DingTalk, Discord, or generic POST)."""
    notif = config.get("notifications", {}) if isinstance(config.get("notifications"), dict) else {}
    if not force and not notif.get("webhook_enabled", False):
        return {"sent": False, "skipped": "webhook_disabled"}
    url = str(notif.get("webhook_url", "") or "").strip()
    if not url:
        return {"sent": False, "error": "Webhook URL 为空"}

    lower_url = url.lower()
    if "feishu.cn" in lower_url or "larksuite.com" in lower_url:
        body: Dict[str, Any] = {"msg_type": "text", "content": {"text": f"{title}\n{content}"}}
    elif "dingtalk.com" in lower_url:
        body = {"msgtype": "text", "text": {"content": f"{title}\n{content}"}}
    elif "discord.com/api/webhooks" in lower_url:
        body = {"content": f"**{title}**\n{content}"}
    else:
        body = {
            "title": title,
            "content": content,
            "text": f"{title}\n{content}",
            "event": event_type,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "extra": extra or {},
        }

    _dry_ctx = runtime_guard.get_active_dry_run_context()
    if _dry_ctx is not None:
        return _dry_ctx.intercept_webhook(url, title, content, event_type=event_type, body=body)

    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "MAA-Universal-Engine/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return {"sent": True, "status": getattr(resp, "status", 200)}
    except Exception as exc:
        return {"sent": False, "error": str(exc)}


class MaaSession:
    """Single MaaCore.dll Asst session with live callback streaming, log slicing, and cancellation."""

    def __init__(
        self,
        maa_dir: Path,
        config: Dict[str, Any],
        base_dir: Optional[Path] = None,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self.maa_dir = Path(maa_dir).resolve()
        self.config = config
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else get_base_dir()
        self.event_callback = event_callback
        self.log_callback = log_callback
        self.cancel_event = cancel_event

        emu_cfg = config.get("emulator", {}) if isinstance(config.get("emulator"), dict) else {}
        self.touch_type = str(emu_cfg.get("touch_type", "minitouch") or "minitouch")
        self.deployment_with_pause = "0"
        self.events: List[Dict[str, Any]] = []
        self.recruit_alerts: List[Dict[str, Any]] = []
        self._asst: Any = None
        self._cb_ref: Any = None
        self.adb_path: str = ""
        self.adb_addr: str = ""
        self._early_stop_reason: str = ""
        self._dry_run_mode: bool = False

    def request_early_stop(self, reason: str) -> None:
        """Signal the active run_single_task loop to stop MaaCore gracefully without marking user-cancelled."""
        self._early_stop_reason = str(reason or "early_stop")

    def _emit_log(self, text: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(text)
            except Exception:
                pass
        else:
            try:
                print(text)
            except UnicodeEncodeError:
                print(text.encode("ascii", errors="replace").decode("ascii"))

    def _call_asst_connect_cancellable(self, conn_cfg: str) -> bool:
        """Invoke self._asst.connect in a helper thread so cancel_event aborts within <=0.5s."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            return False
        if self.cancel_event is None:
            return bool(self._asst.connect(self.adb_path, self.adb_addr, conn_cfg))

        done = threading.Event()
        box: Dict[str, Any] = {"ok": False}

        def _do_connect() -> None:
            try:
                box["ok"] = bool(self._asst.connect(self.adb_path, self.adb_addr, conn_cfg))
            except Exception:
                box["ok"] = False
            finally:
                done.set()

        t = threading.Thread(target=_do_connect, daemon=True)
        t.start()
        while not done.is_set():
            if self.cancel_event.is_set():
                self._emit_log("[STOP] 连接阶段收到取消信号，正在中止 MaaCore 连接...")
                try:
                    self._asst.stop()
                except Exception:
                    pass
                return False
            done.wait(0.25)
        return bool(box["ok"])

    def connect(self, env_info: Optional[Dict[str, Any]] = None) -> bool:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return False
        if runtime_guard.is_dry_run_enabled():
            self._dry_run_mode = True
            try:
                probe = env_info or probe_and_ensure_adb_ready(
                    self.config,
                    maa_dir=self.maa_dir,
                    timeout_sec=5,
                    cancel_event=self.cancel_event,
                    log_callback=self._emit_log,
                    auto_launch=False,
                )
                if probe.get("connected", probe.get("ready")) or env_info:
                    self.adb_path = str(probe.get("adb_path") or "")
                    self.adb_addr = str(probe.get("adb_addr") or probe.get("adb_address") or "")
                else:
                    self._emit_log("[DRY-RUN] 模拟器未连接，将以离线参数演习模式继续")
            except OperationCancelledError:
                return False
            except Exception:
                self._emit_log("[DRY-RUN] 模拟器未连接，将以离线参数演习模式继续")
            return True
        info = env_info or ensure_emulator_ready(
            self.config,
            maa_dir=self.maa_dir,
            cancel_event=self.cancel_event,
            log_callback=self._emit_log,
        )
        if self.cancel_event is not None and self.cancel_event.is_set():
            return False
        self.adb_path = info["adb_path"]
        self.adb_addr = info["adb_addr"]
        conn_cfg = info["connection_config"]

        Asst, InstanceOptionType, Message = _import_asst_classes(self.maa_dir)

        @Asst.CallBackType
        def on_callback(msg, details_bytes, _arg):
            try:
                m = Message(msg).name
            except Exception:
                m = str(msg)
            try:
                d = json.loads(details_bytes.decode("utf-8")) if details_bytes else {}
            except Exception:
                d = {"raw": str(details_bytes)}

            ev = {"ts": time.time(), "message": m, "details": d}
            self.events.append(ev)
            self._handle_live_event(m, d)
            if self.event_callback:
                try:
                    self.event_callback(ev)
                except Exception:
                    pass

        self._cb_ref = on_callback
        self._asst = Asst(callback=on_callback)
        self._asst.set_instance_option(InstanceOptionType.touch_type, self.touch_type)
        self._asst.set_instance_option(InstanceOptionType.deployment_with_pause, self.deployment_with_pause)

        ok = self._call_asst_connect_cancellable(conn_cfg)
        if not ok and not (self.cancel_event is not None and self.cancel_event.is_set()):
            self._emit_log("[ADB] 首次 MaaCore connect 失败，尝试 disconnect + connect 自愈后重连...")
            try:
                if ":" in self.adb_addr:
                    _run_cmd([self.adb_path, "disconnect", self.adb_addr], timeout=4.0, cancel_event=self.cancel_event)
                _run_cmd([self.adb_path, "connect", self.adb_addr], timeout=5.0, cancel_event=self.cancel_event)
            except OperationCancelledError:
                return False
            except Exception:
                pass
            if not (self.cancel_event is not None and self.cancel_event.is_set()):
                ok = self._call_asst_connect_cancellable(conn_cfg)
        return bool(ok)

    def _handle_live_event(self, m: str, d: Dict[str, Any]) -> None:
        if m in ("TaskChainError", "SubTaskError", "InternalError", "InitFailed",
                 "TaskChainStart", "TaskChainCompleted", "TaskChainStopped", "AllTasksCompleted"):
            self._emit_log(f"[MAA:{m}] {json.dumps(d, ensure_ascii=False)[:220]}")
            return
        if m != "SubTaskExtraInfo":
            return

        what = d.get("what", "")
        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}

        if what in ("RecruitTagsDetected", "RecruitSpecialTag", "RecruitResult", "RecruitTagsSelected"):
            tags = det.get("tags", [])
            special_tag = det.get("tag", "")
            level = det.get("level", 0)
            hit_rare = [t for t in ("高级资深干员", "资深干员") if t in tags or t == special_tag]
            if not hit_rare and isinstance(level, int) and level >= 5:
                hit_rare = [f"{level}星词条组合"]
            if hit_rare:
                alert = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "what": what,
                    "rare_tags": hit_rare,
                    "all_tags": tags,
                    "level": level,
                }
                if alert not in self.recruit_alerts:
                    self.recruit_alerts.append(alert)
                    msg_text = f"公招发现稀有词条 {hit_rare} (全部词条: {tags})，已自动锁定跳过！"
                    self._emit_log(f"[ALERT][RECRUIT] {msg_text}")
                    _notify_windows_alert("MAA 公招高星词条提醒", msg_text)
                    notif = self.config.get("notifications", {}) if isinstance(self.config.get("notifications"), dict) else {}
                    if notif.get("webhook_enabled") and notif.get("notify_on_recruit_alert", True):
                        threading.Thread(
                            target=send_webhook_notification,
                            args=(self.config, "MAA 公招高星词条提醒", msg_text, "recruit_alert", alert),
                            daemon=True,
                        ).start()
        elif what == "SanityBeforeStage":
            self._emit_log(f"[FIGHT] 当前理智: {det.get('current_sanity')}/{det.get('max_sanity')}")
        elif what == "StageDrops":
            stage_code = det.get("stage", {}).get("stageCode", "")
            drops = [(x.get("itemName"), x.get("quantity")) for x in det.get("drops", [])]
            annih = det.get("annihilation_weekly_process")
            extra = f" | 本周剿灭={annih[0]}/{annih[1]}" if annih else ""
            self._emit_log(f"[DROP] {stage_code}: {drops}{extra}")
        elif what in ("EnterFacility", "ProductOfFacility"):
            fac = det.get("facility", "")
            idx = det.get("index", "")
            prod = det.get("product", "")
            if prod:
                self._emit_log(f"[INFRA] {fac}#{idx} -> {prod}")
        elif what in (
            "RoguelikeInvestment",
            "RoguelikeInvestmentReachFull",
            "RoguelikeInvestmentReachLimit",
            "BlackFlowStrategyStarted",
            "BlackFlowStrategyResult",
            "BlackFlowMilestoneChanged",
            "MonthlySquadCompleted",
            "DeepExplorationCompleted",
        ):
            self._emit_log(f"[ROGUELIKE:{what}] {json.dumps(det, ensure_ascii=False)[:200]}")
        elif what in ("ReclamationCraft", "Reclamation"):
            self._emit_log(f"[RECLAMATION:{what}] {json.dumps(det, ensure_ascii=False)[:200]}")

    def close(self) -> None:
        """Gracefully stop Asst and release MaaCore handle without disturbing the global ADB server."""
        asst = self._asst
        if asst is not None:
            try:
                if asst.running():
                    asst.stop()
                    deadline = time.monotonic() + 0.8
                    while time.monotonic() < deadline:
                        try:
                            if not asst.running():
                                break
                        except Exception:
                            break
                        time.sleep(0.05)
            except Exception:
                pass
            self._asst = None
            self._cb_ref = None

    def run_single_task(
        self,
        task_type: str,
        params: Dict[str, Any],
        timeout_sec: int = 1800,
        story_skip_on_copilot: bool = False,
        tick_callback: Optional[Callable[["MaaSession", List[Dict[str, Any]]], None]] = None,
        detect_network_disconnect: bool = True,
    ) -> Dict[str, Any]:
        _dry_ctx = runtime_guard.get_active_dry_run_context()
        if _dry_ctx is not None or self._dry_run_mode:
            if _dry_ctx is None:
                _dry_ctx = runtime_guard.DryRunContext(enabled=True, base_dir=self.base_dir, log_callback=self._emit_log)
            shot_img = None
            if self.adb_path and self.adb_addr:
                try:
                    shot_img = stage_pilot.adb_screencap_image(
                        self.adb_path, self.adb_addr, timeout=3.0, cancel_event=self.cancel_event
                    )
                except Exception:
                    shot_img = None
            return _dry_ctx.intercept_task(
                task_type,
                params,
                adb_path=self.adb_path,
                adb_addr=self.adb_addr,
                screenshot_img=shot_img,
                log_callback=self._emit_log,
            )
        if self._asst is None:
            raise RuntimeError("MaaSession 尚未 connect")

        asst_log_path = self.base_dir / "debug" / "asst.log"
        start_ev_idx = len(self.events)
        log_offset = asst_log_path.stat().st_size if asst_log_path.is_file() else 0
        t0 = time.time()

        task_id = self._asst.append_task(task_type, params)
        if not self._asst.start():
            return {
                "task_type": task_type,
                "task_id": task_id,
                "ok": False,
                "timed_out": False,
                "cancelled": False,
                "elapsed": 0.0,
                "events": [],
                "chain_errors": [],
                "sub_errors": [],
                "log_lines": [],
                "error_reason": "asst.start() 返回 False",
                "network_disconnect_detected": False,
                "network_disconnect_info": None,
            }

        timed_out = False
        cancelled = False
        early_stop_reason = ""
        self._early_stop_reason = ""
        ev_cursor = start_ev_idx
        tick_ev_cursor = start_ev_idx
        net_ev_cursor = start_ev_idx
        recent_sub_err_ts: List[float] = []
        last_net_probe = 0.0
        network_disconnect_detected = False
        network_disconnect_info: Optional[Dict[str, Any]] = None
        battle_active = False
        pre_battle_ready = False
        post_battle_ready = False
        last_story_probe = 0.0
        story_probe_battle_failed = False
        story_probe_battle_victory = False
        story_probe_stars: Optional[int] = None
        while self._asst.running():
            if self.cancel_event and self.cancel_event.is_set():
                self._emit_log(f"[STOP] 收到取消信号，停止任务 {task_type}...")
                self._asst.stop()
                cancelled = True
                break
            if self._early_stop_reason:
                early_stop_reason = self._early_stop_reason
                self._emit_log(f"[STOP] 触发快速中止 ({early_stop_reason})，停止任务 {task_type}...")
                self._asst.stop()
                break
            if time.time() - t0 > timeout_sec:
                self._emit_log(f"[WARN] 任务 {task_type} 超时 ({timeout_sec}s)，主动停止。")
                self._asst.stop()
                timed_out = True
                break

            if detect_network_disconnect and task_type != "StartUp":
                cur_net_len = len(self.events)
                net_trigger = ""
                now_ts = time.time()
                for ev in self.events[net_ev_cursor:cur_net_len]:
                    m = ev.get("message", "")
                    d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
                    det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
                    tname = str(det.get("task", "") or "")
                    if m in ("SubTaskStart", "SubTaskCompleted") and "OfflineConfirm" in tname:
                        net_trigger = "offline_confirm_callback"
                    elif m == "ConnectionInfo" and d.get("what") in ("Disconnect", "ScreencapFailed"):
                        net_trigger = "connection_disconnect"
                    elif m == "SubTaskExtraInfo" and d.get("what") in ("StageDrops", "ProductOfFacility", "FightTimes"):
                        recent_sub_err_ts.clear()
                    elif m == "SubTaskError":
                        ev_ts = float(ev.get("ts") or now_ts)
                        recent_sub_err_ts = [ts for ts in recent_sub_err_ts if (now_ts - ts) <= 45.0]
                        recent_sub_err_ts.append(ev_ts)
                        first_list = d.get("first") if isinstance(d.get("first"), list) else []
                        sub_blob = f"{d.get('subtask', '')} {d.get('pre_task', '')} {' '.join(str(x) for x in first_list)}"
                        if any(k in sub_blob for k in ("OfflineConfirm", "Disconnect", "Network")):
                            net_trigger = "network_subtask_error"
                        elif len(recent_sub_err_ts) >= 2:
                            net_trigger = "consecutive_subtask_errors"
                net_ev_cursor = cur_net_len

                if net_trigger and (now_ts - last_net_probe) >= 3.5:
                    last_net_probe = now_ts
                    if _is_real_adb_str(self.adb_path) and _is_real_adb_str(self.adb_addr):
                        probe = stage_pilot.probe_network_disconnect_via_adb(
                            self.adb_path,
                            self.adb_addr,
                            maa_dir=self.maa_dir,
                            cancel_event=self.cancel_event,
                        )
                        if probe.get("detected"):
                            network_disconnect_detected = True
                            network_disconnect_info = probe
                            kws = probe.get("matched_keywords") or ["OfflineConfirm"]
                            self._emit_log(
                                f"[NET][掉线拦截] 任务 {task_type} 触发断线检测 ({net_trigger})，"
                                f"OCR/视觉确认网络掉线弹窗（命中={kws}），立即中止当前任务链进入自愈重连！"
                            )
                            self.request_early_stop("network_disconnect")
                            early_stop_reason = self._early_stop_reason
                            self._asst.stop()
                            break
                    elif net_trigger in ("offline_confirm_callback", "network_subtask_error", "connection_disconnect"):
                        network_disconnect_detected = True
                        network_disconnect_info = {"detected": True, "trigger": net_trigger}
                        self._emit_log(
                            f"[NET][掉线拦截] 任务 {task_type} 回调命中网络断线信号 ({net_trigger})，立即中止当前任务链进入自愈重连！"
                        )
                        self.request_early_stop("network_disconnect")
                        early_stop_reason = self._early_stop_reason
                        self._asst.stop()
                        break

            if story_skip_on_copilot and task_type == "Copilot":
                cur_len = len(self.events)
                for ev in self.events[ev_cursor:cur_len]:
                    m = ev.get("message", "")
                    d = ev.get("details", {})
                    sub = str(d.get("subtask", "") or "")
                    det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
                    tname = str(det.get("task", "") or "")
                    if m == "SubTaskStart" and sub == "BattleProcessTask":
                        battle_active = True
                        pre_battle_ready = False
                    elif m == "SubTaskCompleted" and sub == "BattleProcessTask":
                        battle_active = False
                        post_battle_ready = True
                    elif m in ("SubTaskCompleted", "SubTaskStart") and (
                        "BattleStart" in tname or "StartButton" in tname or tname == "SkipThePreBattlePlot"
                    ):
                        pre_battle_ready = True
                ev_cursor = cur_len

                if (pre_battle_ready or post_battle_ready) and not battle_active and self.adb_path and self.adb_addr:
                    if (time.time() - last_story_probe) >= 2.0:
                        last_story_probe = time.time()
                        probe = stage_pilot.check_and_skip_story_once(
                            self.adb_path,
                            self.adb_addr,
                            self.maa_dir,
                            log_callback=self._emit_log,
                        )
                        if isinstance(probe, dict):
                            if probe.get("battle_failed"):
                                story_probe_battle_failed = True
                            if probe.get("battle_victory"):
                                story_probe_battle_victory = True
                                if probe.get("settlement_stars") is not None:
                                    story_probe_stars = probe.get("settlement_stars")

            if tick_callback is not None:
                cur_len = len(self.events)
                batch_evs = self.events[tick_ev_cursor:cur_len]
                tick_ev_cursor = cur_len
                try:
                    tick_callback(self, batch_evs)
                except Exception:
                    pass
                if self._early_stop_reason:
                    early_stop_reason = self._early_stop_reason
                    self._emit_log(f"[STOP] 触发快速中止 ({early_stop_reason})，停止任务 {task_type}...")
                    self._asst.stop()
                    break

            if wait_interruptible(0.5, cancel_event=self.cancel_event, step=0.25):
                self._emit_log(f"[STOP] 收到取消信号，停止任务 {task_type}...")
                self._asst.stop()
                cancelled = True
                break

        elapsed = round(time.time() - t0, 1)
        new_events = self.events[start_ev_idx:]
        new_log_lines: List[str] = []
        if asst_log_path.is_file():
            with open(asst_log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(log_offset)
                new_log_lines = f.read().splitlines()

        chain_completed = any(
            ev["message"] == "TaskChainCompleted" and ev["details"].get("taskchain") == task_type
            for ev in new_events
        )
        chain_errors = [
            ev["details"]
            for ev in new_events
            if ev["message"] in ("TaskChainError", "InternalError", "InitFailed")
        ]
        sub_errors = [
            ev["details"]
            for ev in new_events
            if ev["message"] == "SubTaskError"
        ]

        return {
            "task_type": task_type,
            "task_id": task_id,
            "ok": chain_completed and not chain_errors and not timed_out and not cancelled and not early_stop_reason,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "early_stopped": bool(early_stop_reason),
            "early_stop_reason": early_stop_reason,
            "elapsed": elapsed,
            "events": new_events,
            "chain_errors": chain_errors,
            "sub_errors": sub_errors,
            "log_lines": new_log_lines,
            "story_probe_battle_failed": story_probe_battle_failed,
            "story_probe_battle_victory": story_probe_battle_victory,
            "story_probe_stars": story_probe_stars,
            "network_disconnect_detected": network_disconnect_detected,
            "network_disconnect_info": network_disconnect_info,
        }


def _is_real_adb_str(val: Any) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _extract_real_cancel_event(ev: Any) -> Optional[threading.Event]:
    if ev is not None and callable(getattr(ev, "is_set", None)) and isinstance(ev.is_set(), bool):
        return ev
    return None


def _force_restart_game_client(
    config: Dict[str, Any],
    adb_path: str,
    adb_addr: str,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    wait_after_stop: float = 2.0,
    wait_after_start: float = 6.0,
) -> bool:
    """Force-stop and relaunch Arknights client on the last disconnect self-healing attempt."""
    spec = get_server_spec(config)
    pkg = spec["package"]
    act = spec["activity"]
    _dry_ctx = runtime_guard.get_active_dry_run_context()
    if _dry_ctx is not None:
        _dry_ctx.intercept_control(
            "force_restart_game_client",
            detail=f"跳过 am force-stop/start {pkg}",
            command=["am", "force-stop", pkg],
            log_callback=log_callback,
        )
        return True
    if log_callback:
        try:
            log_callback(f"[NET] 正在强制重启游戏客户端 ({pkg}) 以清除异常网络状态...")
        except Exception:
            pass
    try:
        _run_cmd([adb_path, "-s", adb_addr, "shell", "am", "force-stop", pkg], timeout=6.0, cancel_event=cancel_event)
    except OperationCancelledError:
        raise
    except Exception:
        pass
    if wait_after_stop > 0 and wait_interruptible(wait_after_stop, cancel_event=cancel_event, step=0.25):
        raise OperationCancelledError("强制重启客户端等待阶段已取消")
    try:
        res = _run_cmd(
            [adb_path, "-s", adb_addr, "shell", "am", "start", "-n", f"{pkg}/{act}"],
            timeout=6.0,
            cancel_event=cancel_event,
        )
        ok_start = res.returncode == 0
    except OperationCancelledError:
        raise
    except Exception:
        ok_start = False
    if wait_after_start > 0 and wait_interruptible(wait_after_start, cancel_event=cancel_event, step=0.25):
        raise OperationCancelledError("等待游戏重启加载阶段已取消")
    return ok_start


def _has_real_disconnect_error(res: Dict[str, Any]) -> bool:
    if res.get("network_disconnect_detected") or res.get("early_stop_reason") == "network_disconnect":
        return True
    for ev in res.get("events", []):
        m = ev.get("message", "")
        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
        if m in ("InternalError", "InitFailed"):
            return True
        if m == "ConnectionInfo" and d.get("what") in ("Disconnect", "ScreencapFailed"):
            return True
        if m in ("SubTaskStart", "SubTaskCompleted"):
            det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
            tname = str(det.get("task", "") or "")
            if tname in ("OfflineConfirm", "OfflineConfirmImpl") or "OfflineConfirm" in tname:
                return True
        if m == "SubTaskError":
            first = d.get("first") if isinstance(d.get("first"), list) else []
            blob = f"{d.get('subtask', '')} {d.get('pre_task', '')} {' '.join(str(x) for x in first)}"
            if "OfflineConfirm" in blob:
                return True
    log_text = "\n".join(res.get("log_lines", []))
    return bool(re.search(r"PipelineAnalyzer::analyze.*OfflineConfirm|OfflineConfirm\.png|ScreencapFailed|ConnectionInfo.*Disconnect", log_text))


def _inspect_task_network_disconnect(
    res: Dict[str, Any],
    cur_sess: Any,
    task_type: str = "",
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Determine whether a task failure/completion was interrupted by a real network disconnect popup."""
    if res.get("network_disconnect_detected") or res.get("early_stop_reason") == "network_disconnect":
        return True, "live_network_disconnect_intercept", res.get("network_disconnect_info")

    adb_path = getattr(cur_sess, "adb_path", "")
    adb_addr = getattr(cur_sess, "adb_addr", "")
    maa_dir = getattr(cur_sess, "maa_dir", None)
    has_real_adb = _is_real_adb_str(adb_path) and _is_real_adb_str(adb_addr)

    direct_signal = _has_real_disconnect_error(res)
    if direct_signal:
        # For StartUp that returned ok=True after clicking OfflineConfirmImpl, only treat as stuck
        # if a fresh screenshot proves the disconnect popup is still on screen.
        if task_type == "StartUp" and res.get("ok"):
            if has_real_adb:
                probe = stage_pilot.probe_network_disconnect_via_adb(
                    adb_path, adb_addr, maa_dir=maa_dir, cancel_event=cancel_event
                )
                if probe.get("detected"):
                    return True, "startup_still_on_disconnect_popup", probe
            return False, "", None

        # When real ADB is present, enrich with OCR popup inspection (if screen shows popup)
        probe_info: Optional[Dict[str, Any]] = None
        if has_real_adb:
            probe_info = stage_pilot.probe_network_disconnect_via_adb(
                adb_path, adb_addr, maa_dir=maa_dir, cancel_event=cancel_event
            )
            if probe_info.get("detected") and log_callback:
                try:
                    log_callback(
                        f"[NET][掉线确认] 任务 {task_type} 截屏 OCR 命中掉线弹窗关键字: {probe_info.get('matched_keywords')}"
                    )
                except Exception:
                    pass
        return True, "callback_or_log_disconnect_signal", probe_info

    # Trigger active OCR probe when a task chain hits >= 2 SubTaskErrors (e.g. consecutive InfrastMfgTask / Trade / Reception)
    sub_errors = res.get("sub_errors") if isinstance(res.get("sub_errors"), list) else []
    if not res.get("ok") and len(sub_errors) >= 2 and has_real_adb:
        probe = stage_pilot.probe_network_disconnect_via_adb(
            adb_path, adb_addr, maa_dir=maa_dir, cancel_event=cancel_event
        )
        if probe.get("detected"):
            if log_callback:
                try:
                    log_callback(
                        f"[NET][掉线拦截] 任务 {task_type} 连续出现 {len(sub_errors)} 次子任务异常，"
                        f"主动截屏 OCR 命中掉线弹窗（关键字={probe.get('matched_keywords')}）！"
                    )
                except Exception:
                    pass
            return True, "consecutive_subtask_errors_ocr_popup", probe

    return False, "", None


def recover_from_network_disconnect(
    session_holder: Dict[str, Any],
    config: Dict[str, Any],
    *,
    max_attempts: int = 3,
    backoff_seconds: Tuple[float, ...] = (3.0, 6.0, 10.0),
    force_restart_on_last: bool = True,
    task_type: str = "",
    reason: str = "",
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    verify_online: bool = True,
) -> Dict[str, Any]:
    """Closed-loop network disconnect self-healing (max 3 attempts with backoff + strict online verification).

    Steps per attempt:
      1. Dismiss disconnect popup by clicking the white circular confirm button (visual/template/fallback (0.50, 0.70))
         or force-restart the game client on the final attempt if previous attempts stayed stuck.
      2. Wait for transition back to login/title, reconnect MaaSession, and invoke MAA StartUp.
      3. Strictly re-verify via screenshot + OCR/template that the screen left the disconnect popup and reached
         the game Home / online UI (never trusting a false-positive StartUp TaskChainCompleted).
      4. After exhausting max_attempts, capture an evidence screenshot, log [NET][自愈失败], and abort gracefully.
    """
    cur_sess = session_holder.get("session")
    maa_dir = getattr(cur_sess, "maa_dir", None)
    base_dir = getattr(cur_sess, "base_dir", None)
    ev_cb = getattr(cur_sess, "event_callback", None)
    log_cb = log_callback or getattr(cur_sess, "log_callback", None)
    raw_cancel = cancel_event if cancel_event is not None else getattr(cur_sess, "cancel_event", None)
    eff_cancel = _extract_real_cancel_event(raw_cancel)
    spec = get_server_spec(config)

    def _log(msg: str) -> None:
        if log_cb:
            try:
                log_cb(msg)
                return
            except Exception:
                pass
        print(msg)

    def _is_cancelled() -> bool:
        return bool(eff_cancel is not None and eff_cancel.is_set())

    eff_max = max(1, int(max_attempts))
    last_adb_path = getattr(cur_sess, "adb_path", "") if cur_sess is not None else ""
    last_adb_addr = getattr(cur_sess, "adb_addr", "") if cur_sess is not None else ""
    last_err = reason or "network_disconnect"
    taps_total = 0
    restarted_client = False
    last_verify_img: Any = None

    for attempt in range(1, eff_max + 1):
        if _is_cancelled():
            return {"ok": False, "cancelled": True, "attempts": attempt - 1, "taps": taps_total}

        if attempt > 1:
            idx = min(attempt - 2, max(0, len(backoff_seconds) - 1))
            delay = float(backoff_seconds[idx]) if backoff_seconds else 3.0
            if delay > 0:
                _log(f"[NET][自愈 {attempt}/{eff_max}] 退避等待 {delay:.1f}s 后开始下一轮断线恢复...")
                if wait_interruptible(delay, cancel_event=eff_cancel, step=0.25):
                    return {"ok": False, "cancelled": True, "attempts": attempt - 1, "taps": taps_total}

        _log(f"[NET][自愈 {attempt}/{eff_max}] 启动网络掉线闭环恢复（任务={task_type or '-'}，触发原因={reason or 'disconnect'}）...")

        active_sess = session_holder.get("session")
        if active_sess is not None:
            cand_p = getattr(active_sess, "adb_path", "")
            cand_a = getattr(active_sess, "adb_addr", "")
            if _is_real_adb_str(cand_p):
                last_adb_path = cand_p
            if _is_real_adb_str(cand_a):
                last_adb_addr = cand_a
            try:
                active_sess.close()
            except Exception:
                pass

        if _is_cancelled():
            return {"ok": False, "cancelled": True, "attempts": attempt - 1, "taps": taps_total}

        has_real_adb = _is_real_adb_str(last_adb_path) and _is_real_adb_str(last_adb_addr)
        is_last_escalate = bool(force_restart_on_last and eff_max >= 2 and attempt == eff_max)

        try:
            if is_last_escalate and has_real_adb:
                restarted_client = True
                _force_restart_game_client(
                    config,
                    last_adb_path,
                    last_adb_addr,
                    cancel_event=eff_cancel,
                    log_callback=_log,
                    wait_after_stop=2.0,
                    wait_after_start=6.0,
                )
            else:
                if has_real_adb:
                    dismiss_res = stage_pilot.dismiss_network_disconnect_popup(
                        last_adb_path,
                        last_adb_addr,
                        maa_dir=maa_dir,
                        max_taps=2,
                        wait_after_tap_sec=2.0,
                        cancel_event=eff_cancel,
                        log_callback=_log,
                    )
                    taps_total += int(dismiss_res.get("taps", 0) or 0)
                    if dismiss_res.get("cancelled") or _is_cancelled():
                        return {"ok": False, "cancelled": True, "attempts": attempt, "taps": taps_total}
                    if dismiss_res.get("taps", 0) > 0 and dismiss_res.get("dismissed"):
                        _log("[NET] 断线弹窗已关闭，等待游戏退回登录/唤醒界面...")
                        if wait_interruptible(2.0, cancel_event=eff_cancel, step=0.25):
                            return {"ok": False, "cancelled": True, "attempts": attempt, "taps": taps_total}
                    elif dismiss_res.get("still_popup"):
                        _log("[NET] 点击确认按钮后掉线弹窗仍未消退，继续尝试 StartUp 与后续升级恢复...")
                ensure_game_running(config, maa_dir=maa_dir, cancel_event=eff_cancel, log_callback=_log)
        except OperationCancelledError:
            return {"ok": False, "cancelled": True, "attempts": attempt, "taps": taps_total}
        except Exception as exc:
            last_err = f"游戏/弹窗前置恢复异常: {exc}"
            _log(f"[WARN][NET] 第 {attempt}/{eff_max} 次恢复前置步骤异常: {exc}")
            continue

        new_sess = MaaSession(
            maa_dir=maa_dir,
            config=config,
            base_dir=base_dir,
            event_callback=ev_cb,
            log_callback=log_cb,
            cancel_event=raw_cancel,
        )
        if not new_sess.connect():
            last_err = "MaaSession.connect 重连失败"
            _log(f"[WARN][NET] 第 {attempt}/{eff_max} 次 MaaSession 重连失败。")
            continue

        session_holder["session"] = new_sess
        new_adb_p = getattr(new_sess, "adb_path", "")
        new_adb_a = getattr(new_sess, "adb_addr", "")
        if _is_real_adb_str(new_adb_p) and _is_real_adb_str(new_adb_a):
            last_adb_path, last_adb_addr = new_adb_p, new_adb_a
            has_real_adb = True

        # Invoke MAA StartUp to drive login/re-entry
        try:
            st_res = new_sess.run_single_task(
                "StartUp",
                {"start_game_enabled": True, "client_type": spec["client_type"]},
                timeout_sec=180,
                detect_network_disconnect=False,
            )
        except TypeError:
            st_res = new_sess.run_single_task(
                "StartUp",
                {"start_game_enabled": True, "client_type": spec["client_type"]},
                timeout_sec=180,
            )

        if st_res.get("cancelled") or _is_cancelled():
            return {"ok": False, "cancelled": True, "attempts": attempt, "taps": taps_total}

        # Mandatory secondary screen verification when real ADB is available
        if verify_online and has_real_adb:
            verified_online = False
            last_verify: Dict[str, Any] = {}
            for poll_i in range(3):
                if _is_cancelled():
                    return {"ok": False, "cancelled": True, "attempts": attempt, "taps": taps_total}
                img_v = stage_pilot.adb_screencap_image(last_adb_path, last_adb_addr, timeout=5.0, cancel_event=eff_cancel)
                if img_v is None:
                    last_verify = {"online": False, "disconnect_popup": False, "reason": "verify_screencap_failed"}
                    break
                last_verify_img = img_v
                last_verify = stage_pilot.is_game_home_or_online_screen(
                    img_v, maa_dir=maa_dir, cancel_event=eff_cancel
                )
                if last_verify.get("disconnect_popup"):
                    last_err = (
                        f"StartUp 返回后二次核验仍停留在掉线弹窗"
                        f"（命中={last_verify.get('disconnect', {}).get('matched_keywords', [])}）"
                    )
                    _log(f"[WARN][NET][自愈 {attempt}/{eff_max}] {last_err}，拒绝误判恢复成功！")
                    break
                if last_verify.get("online"):
                    verified_online = True
                    break
                if last_verify.get("is_login_or_loading") and poll_i < 2:
                    _log(f"[NET][自愈 {attempt}/{eff_max}] 画面仍在连接/加载过渡中，等待 3.0s 后二次核验...")
                    if wait_interruptible(3.0, cancel_event=eff_cancel, step=0.25):
                        return {"ok": False, "cancelled": True, "attempts": attempt, "taps": taps_total}
                    continue
                last_err = f"StartUp 后二次核验未确认回到在线主界面 ({last_verify.get('reason', 'unknown')})"
                _log(f"[WARN][NET][自愈 {attempt}/{eff_max}] {last_err}")
                break

            if verified_online:
                _log(
                    f"[NET][自愈成功] 第 {attempt}/{eff_max} 次重连与画面二次核验通过"
                    f"（状态={last_verify.get('reason')}，累计点击确认={taps_total}次，强制重启={restarted_client}）。"
                )
                return {
                    "ok": True,
                    "cancelled": False,
                    "attempts": attempt,
                    "taps": taps_total,
                    "restarted_client": restarted_client,
                    "verified": True,
                    "screen_state": last_verify,
                }
        else:
            if st_res.get("ok", True):
                _log(f"[NET][自愈成功] 第 {attempt}/{eff_max} 次 StartUp 重连完成。")
                return {
                    "ok": True,
                    "cancelled": False,
                    "attempts": attempt,
                    "taps": taps_total,
                    "restarted_client": restarted_client,
                    "verified": False,
                    "screen_state": {},
                }
            last_err = str(st_res.get("error_reason") or "StartUp 返回失败")
            _log(f"[WARN][NET][自愈 {attempt}/{eff_max}] StartUp 未成功 ({last_err})。")

    # All recovery attempts exhausted -> capture evidence screenshot and abort gracefully
    shot_str = ""
    if _is_real_adb_str(last_adb_path) and _is_real_adb_str(last_adb_addr):
        root_dir = Path(base_dir) if base_dir is not None else get_base_dir()
        shot_path = root_dir / "reports" / f"net_recovery_failed_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        if capture_adb_screenshot(last_adb_path, last_adb_addr, shot_path, cancel_event=eff_cancel):
            shot_str = str(shot_path)
        elif last_verify_img is not None:
            try:
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                last_verify_img.save(shot_path)
                shot_str = str(shot_path)
            except Exception:
                pass

    fail_msg = (
        f"[NET][自愈失败] 连续 {eff_max} 次网络掉线自愈均失败（任务={task_type or '-'}，原因={last_err}"
        + (f"，现场截图={shot_str}" if shot_str else "")
        + "），请检查本机网络或游戏服务器状态，任务已优雅中止。"
    )
    _log(fail_msg)
    session_holder["session_lost"] = True
    session_holder["network_recovery_failed"] = True
    session_holder["network_recovery_error"] = fail_msg
    session_holder["network_recovery_screenshot"] = shot_str
    return {
        "ok": False,
        "cancelled": False,
        "failed": True,
        "attempts": eff_max,
        "taps": taps_total,
        "restarted_client": restarted_client,
        "screenshot": shot_str,
        "error": fail_msg,
    }


def run_task_with_recovery(
    session_holder: Dict[str, MaaSession],
    task_type: str,
    params: Dict[str, Any],
    config: Dict[str, Any],
    timeout_sec: int = 1800,
    max_retries: int = 1,
    tick_callback: Optional[Callable[[MaaSession, List[Dict[str, Any]]], None]] = None,
    max_network_attempts: int = 3,
    allow_network_recovery: bool = True,
    network_backoff_seconds: Tuple[float, ...] = (3.0, 6.0, 10.0),
) -> Dict[str, Any]:
    """Execute task with unified network disconnect detection & closed-loop recovery (never loops on stage errors)."""
    res: Dict[str, Any] = {}
    max_net_cycles = max(1, int(max_retries)) if allow_network_recovery else 0
    net_cycles_used = 0
    recovered_once = False
    max_loop = max(int(max_retries), max_net_cycles) + 1

    for _attempt in range(max_loop):
        cur_sess = session_holder["session"]
        log_cb = getattr(cur_sess, "log_callback", None)
        eff_cancel = _extract_real_cancel_event(getattr(cur_sess, "cancel_event", None))

        if tick_callback is not None:
            try:
                res = cur_sess.run_single_task(task_type, params, timeout_sec=timeout_sec, tick_callback=tick_callback)
            except TypeError:
                res = cur_sess.run_single_task(task_type, params, timeout_sec=timeout_sec)
        else:
            res = cur_sess.run_single_task(task_type, params, timeout_sec=timeout_sec)

        if res.get("cancelled") or (eff_cancel is not None and eff_cancel.is_set()):
            return res

        is_disconnect, disc_reason, disc_probe = _inspect_task_network_disconnect(
            res, cur_sess, task_type=task_type, cancel_event=eff_cancel, log_callback=log_cb
        )
        if res.get("ok") and not is_disconnect:
            if recovered_once:
                out_ok = dict(res)
                out_ok["network_recovered"] = True
                return out_ok
            return res

        if is_disconnect and allow_network_recovery and net_cycles_used < max_net_cycles:
            net_cycles_used += 1
            rec = recover_from_network_disconnect(
                session_holder,
                config,
                max_attempts=max_network_attempts,
                backoff_seconds=network_backoff_seconds,
                task_type=task_type,
                reason=disc_reason,
                log_callback=log_cb,
                cancel_event=eff_cancel,
            )
            if rec.get("cancelled"):
                out_c = dict(res)
                out_c["ok"] = False
                out_c["cancelled"] = True
                return out_c
            if rec.get("ok"):
                recovered_once = True
                if log_cb:
                    try:
                        log_cb(f"[NET] 已确认恢复在线，自动续跑被中断的任务 {task_type}...")
                    except Exception:
                        pass
                continue
            out_fail = dict(res)
            out_fail["ok"] = False
            out_fail["network_recovery_failed"] = True
            out_fail["network_recovery"] = rec
            out_fail["error"] = str(rec.get("error") or "[NET][自愈失败] 网络掉线自愈失败")
            if rec.get("screenshot"):
                out_fail["recovery_screenshot"] = rec["screenshot"]
            if disc_probe is not None:
                out_fail["network_disconnect_info"] = disc_probe
            return out_fail

        return res
    return res


PRTS_LEVELS_URL = "https://prts.maa.plus/arknights/level"
PRTS_LEVELS_CACHE_TTL_SEC = 6 * 3600
_PRTS_LEVELS_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "levels": []}


def _fetch_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "MAA-Universal-Engine/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_prts_levels(
    base_dir: Optional[Path] = None,
    force_refresh: bool = False,
    timeout: int = 12,
) -> List[Dict[str, Any]]:
    """Load PRTS online stage table (/arknights/level) with memory + 6h disk cache."""
    now = time.time()
    cached_levels = _PRTS_LEVELS_CACHE.get("levels")
    fetched_at = float(_PRTS_LEVELS_CACHE.get("fetched_at") or 0.0)
    if not force_refresh and isinstance(cached_levels, list) and cached_levels and (now - fetched_at) < PRTS_LEVELS_CACHE_TTL_SEC:
        return cached_levels

    root = Path(base_dir) if base_dir is not None else get_base_dir()
    cache_file = root / "cache" / "prts_levels.json"
    stale_disk: List[Dict[str, Any]] = []
    if cache_file.is_file():
        try:
            disk_raw = json.loads(cache_file.read_text(encoding="utf-8"))
            disk_levels = disk_raw.get("data", []) if isinstance(disk_raw, dict) else disk_raw
            if isinstance(disk_levels, list) and disk_levels:
                stale_disk = disk_levels
                age = now - cache_file.stat().st_mtime
                if not force_refresh and age < PRTS_LEVELS_CACHE_TTL_SEC:
                    _PRTS_LEVELS_CACHE["fetched_at"] = now
                    _PRTS_LEVELS_CACHE["levels"] = disk_levels
                    return disk_levels
        except Exception:
            pass

    try:
        raw = _fetch_json(PRTS_LEVELS_URL, timeout=timeout)
        levels = raw.get("data", []) if isinstance(raw, dict) else raw
        if isinstance(levels, list) and levels:
            _PRTS_LEVELS_CACHE["fetched_at"] = now
            _PRTS_LEVELS_CACHE["levels"] = levels
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps({"data": levels}, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return levels
    except Exception:
        pass

    if stale_disk:
        _PRTS_LEVELS_CACHE["fetched_at"] = now
        _PRTS_LEVELS_CACHE["levels"] = stale_disk
        return stale_disk
    return cached_levels if isinstance(cached_levels, list) else []


def _strip_stage_difficulty_suffix(code: str) -> str:
    """Strip trailing PRTS difficulty tags like '16-2（标准）' or 'H12-1(标准)'."""
    return re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", str(code or "").strip()).strip()


def _match_level_entry(levels: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    q = str(query or "").strip()
    if not q:
        return None
    q_up = _strip_stage_difficulty_suffix(q).upper()
    q_raw_up = q.upper()
    q_low = q.lower()
    want_hard = "#f#" in q_low

    def _pick_best(cands: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not cands:
            return None
        if want_hard:
            return cands[0]

        def _rank(c: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
            sid = str(c.get("stage_id", "") or "").strip().lower()
            cat3 = str(c.get("cat_three", "") or "").strip()
            return (
                0 if "#f#" in sid else 1,
                0 if sid.startswith(("easy_", "tough_")) or any(k in cat3 for k in ("简单", "磨难")) else 1,
                0 if sid.endswith("#s") else 1,
                1 if cat3.upper() == q_raw_up else 0,
                1 if "标准" in cat3 else 0,
            )

        return max(cands, key=_rank)

    by_sid = [
        x for x in levels
        if isinstance(x, dict)
        and (
            str(x.get("stage_id", "") or "").strip().lower() == q_low
            or str(x.get("level_id", "") or "").strip().lower() == q_low
        )
    ]
    hit = _pick_best(by_sid)
    if hit is not None:
        return hit

    by_code = [
        x for x in levels
        if isinstance(x, dict)
        and (
            str(x.get("cat_three", "") or "").strip().upper() == q_raw_up
            or _strip_stage_difficulty_suffix(str(x.get("cat_three", "") or "")).upper() == q_up
        )
    ]
    hit = _pick_best(by_code)
    if hit is not None:
        return hit

    by_name = [
        x for x in levels
        if isinstance(x, dict) and str(x.get("name", "") or "").strip().lower() == q_low
    ]
    return _pick_best(by_name)


def lookup_prts_stage_online(
    query: str,
    base_dir: Optional[Path] = None,
    force_refresh: bool = False,
) -> Optional[Dict[str, Any]]:
    """Resolve stage_id/code/level_id dynamically from prts.maa.plus/arknights/level."""
    q = str(query or "").strip()
    if not q:
        return None
    levels = load_prts_levels(base_dir=base_dir, force_refresh=force_refresh)
    hit = _match_level_entry(levels, q)
    if hit is None and not force_refresh:
        fetched_at = float(_PRTS_LEVELS_CACHE.get("fetched_at") or 0.0)
        if (time.time() - fetched_at) >= 60.0 or not levels:
            levels = load_prts_levels(base_dir=base_dir, force_refresh=True)
            hit = _match_level_entry(levels, q)
    if hit is None:
        return None
    clean_code = _strip_stage_difficulty_suffix(str(hit.get("cat_three") or q)) or q
    return {
        "code": clean_code,
        "stage_id": str(hit.get("stage_id") or q),
        "level_id": str(hit.get("level_id") or ""),
        "name": str(hit.get("name") or ""),
        "cat_two": str(hit.get("cat_two") or ""),
        "source": "prts_online",
    }


def resolve_event_battle_stages_from_prts(
    prefix: str,
    base_dir: Optional[Path] = None,
) -> List[str]:
    """Return naturally sorted normal battle stages (e.g. ['OS-1', ..., 'OS-10']) from PRTS online levels."""
    pref_clean = str(prefix or "").strip().upper().rstrip("-")
    if not pref_clean:
        return []
    pat = re.compile(rf"^{re.escape(pref_clean)}-(\d+)$")
    levels = load_prts_levels(base_dir=base_dir)
    matched: List[str] = []
    seen: set[str] = set()
    for item in levels:
        if not isinstance(item, dict):
            continue
        code = str(item.get("cat_three", "") or "").strip().upper()
        sid = str(item.get("stage_id", "") or "").strip().lower()
        if not code or "#f#" in sid or not pat.match(code):
            continue
        if code not in seen:
            seen.add(code)
            matched.append(code)
    matched.sort(key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s)])
    return matched


def resolve_stage_info(
    query: str,
    maa_dir: Path,
    base_dir: Optional[Path] = None,
    allow_online: bool = True,
) -> Dict[str, Any]:
    """Resolve stage code, stageId, apCost, and tile file from <maa_dir>/resource/ with PRTS online fallback."""
    q = query.strip()
    q_upper = q.upper()
    q_low = q.lower()
    want_hard = "#f#" in q_low
    maa_path = Path(maa_dir)
    stages_path = maa_path / "resource" / "stages.json"
    tile_dir = maa_path / "resource" / "Arknights-Tile-Pos"

    matched: Dict[str, Any] = {
        "query": q,
        "code": q,
        "stage_id": q,
        "level_id": "",
        "stage_name_cn": "",
        "stage_id_source": "raw",
        "ap_cost": None,
        "tile_file": None,
    }

    if stages_path.is_file():
        try:
            stages = json.loads(stages_path.read_text(encoding="utf-8"))
            if isinstance(stages, list):
                local_hits: List[Dict[str, Any]] = []
                for st in stages:
                    if not isinstance(st, dict):
                        continue
                    code = str(st.get("code", ""))
                    sid = str(st.get("stageId", ""))
                    if code.upper() == q_upper or sid.lower() == q_low:
                        local_hits.append(st)
                if local_hits:
                    chosen = local_hits[0]
                    if not want_hard:
                        normal = [st for st in local_hits if "#f#" not in str(st.get("stageId", "")).lower()]
                        if normal:
                            chosen = normal[0]
                    matched["code"] = str(chosen.get("code") or q)
                    matched["stage_id"] = str(chosen.get("stageId") or q)
                    matched["ap_cost"] = chosen.get("apCost")
                    matched["stage_id_source"] = "local_stages_json"
        except Exception:
            pass

    def _scan_tile(cur_sid: str) -> None:
        if not tile_dir.is_dir():
            return
        sid_lower = cur_sid.lower()
        for fn in sorted(os.listdir(tile_dir)):
            fl = fn.lower()
            if fl.startswith(sid_lower + "-") or fl.startswith(sid_lower + "#") or q_low in fl:
                matched["tile_file"] = str(tile_dir / fn)
                if matched["stage_id"] == q and "-" in fn:
                    matched["stage_id"] = fn.split("-")[0]
                    matched["stage_id_source"] = "local_tile_pos"
                break

    _scan_tile(matched["stage_id"])

    if allow_online and matched["stage_id"].lower() == q_low:
        online = lookup_prts_stage_online(q, base_dir=base_dir)
        if online and online.get("stage_id"):
            matched["code"] = online.get("code") or matched["code"]
            matched["stage_id"] = online["stage_id"]
            matched["level_id"] = online.get("level_id", "")
            matched["stage_name_cn"] = online.get("name", "")
            matched["stage_id_source"] = "prts_online"
            if not matched["tile_file"]:
                _scan_tile(matched["stage_id"])

    return matched


def _guide_matches_stage(
    content: Dict[str, Any],
    stage_id: str,
    stage_code: str,
    level_id: str = "",
    allow_doc_match: bool = False,
) -> bool:
    """Strictly verify whether a PRTS copilot entry targets the requested stage."""
    sid_low = str(stage_id or "").strip().lower()
    code_low = str(stage_code or "").strip().lower()
    lid_low = str(level_id or "").strip().lower()
    want_hard = ("#f#" in sid_low) or ("#f#" in code_low)

    st_name = str(content.get("stage_name", "") or "").strip()
    st_low = st_name.lower()
    if st_low:
        if not want_hard and "#f#" in st_low:
            return False
        targets = {t for t in (sid_low, code_low, lid_low) if t}
        if st_low in targets:
            return True
        if lid_low and st_low == lid_low.rsplit("/", 1)[-1]:
            return True
        if sid_low and sid_low != code_low and st_low.startswith(sid_low + "_"):
            return True
        if not allow_doc_match:
            return False
        # When keyword/document fallback runs and stage_id is unresolved (stage_id == stage_code),
        # only allow title match if stage_name is NOT clearly another numbered stage of the same prefix
        if sid_low and sid_low != code_low:
            return False
        if re.match(r"^[a-z0-9]+_(?:\d+|ex\d+|s\d+|tr\d+|st\d+)(?:#f#)?$", st_low):
            # If stage_name is an internal ID of a DIFFERENT stage number/suffix, check exact stage token in title
            pass

    if not allow_doc_match:
        return not st_low

    doc = content.get("doc")
    title = str(doc.get("title", "") if isinstance(doc, dict) else "").strip()
    details = str(doc.get("details", "") if isinstance(doc, dict) else "").strip()
    probe_text = title if title else details[:120]
    if not probe_text or not stage_code:
        return False

    code_clean = stage_code.strip()
    token_pat = re.compile(
        rf"(?<![A-Za-z0-9\-]){re.escape(code_clean)}(?![A-Za-z0-9\-])",
        re.IGNORECASE,
    )
    if not token_pat.search(probe_text):
        return False
    # Guard against range summaries like "OS-1~OS-10" or "16-1至16-9" when searching a single stage
    range_pat = re.compile(
        rf"(?<![A-Za-z0-9\-]){re.escape(code_clean)}\s*[~～\-至到]\s*(?:[A-Za-z0-9]{{1,5}}-){{0,2}}[A-Za-z0-9]*\d[A-Za-z0-9]*",
        re.IGNORECASE,
    )
    stripped = range_pat.sub("", probe_text)
    if not token_pat.search(stripped):
        return False
    # If title explicitly mentions another stage of the same prefix (e.g. "OS-2" without "OS-1" standalone)
    if "-" in code_clean:
        pref = code_clean.rsplit("-", 1)[0]
        other_same_pref = re.findall(
            rf"(?<![A-Za-z0-9\-]){re.escape(pref)}-(?:[A-Za-z0-9]{{1,5}}-)?[A-Za-z0-9]*\d[A-Za-z0-9]*(?![A-Za-z0-9\-])",
            stripped,
            re.IGNORECASE,
        )
        if other_same_pref and not any(m.upper() == code_clean.upper() for m in other_same_pref):
            return False
    return True


def fetch_prts_guides(
    stage_id: str,
    stage_code: str,
    max_details: int = 8,
    base_dir: Optional[Path] = None,
    allow_online_resolve: bool = True,
) -> List[Dict[str, Any]]:
    """Fetch top-rated copilot guides from prts.maa.plus with online stageId lookup & keyword fallback."""
    eff_stage_id = str(stage_id or "").strip()
    eff_code = str(stage_code or "").strip()
    eff_level_id = ""

    # Stage 1: If local stages.json didn't resolve an internal stage_id (e.g. OS-1 -> OS-1),
    # dynamically look up the real stageId from prts.maa.plus/arknights/level first.
    if allow_online_resolve and eff_code and (not eff_stage_id or eff_stage_id.lower() == eff_code.lower()):
        online = lookup_prts_stage_online(eff_code, base_dir=base_dir)
        if online and online.get("stage_id"):
            eff_stage_id = str(online["stage_id"]).strip()
            eff_level_id = str(online.get("level_id") or "").strip()

    raw_items: Dict[int, Dict[str, Any]] = {}

    def _query_copilot(params_list: List[Dict[str, Any]], max_pages: int = 2) -> None:
        for base_p in params_list:
            for page in range(1, max_pages + 1):
                q_params = dict(base_p, page=page, limit=50 if "document" in base_p or "keyword" in base_p else 30, order_by="views", desc="true")
                qs = urllib.parse.urlencode(q_params)
                try:
                    res = _fetch_json(f"https://prts.maa.plus/copilot/query?{qs}")
                    items = res.get("data", {}).get("data", [])
                    if not items:
                        break
                    for it in items:
                        cid = it.get("id")
                        if cid is not None and cid not in raw_items:
                            raw_items[cid] = it
                except Exception:
                    break

    # Stage 2: Exact level_keyword query using resolved internal stage_id (or stage_code)
    has_resolved_sid = bool(eff_stage_id and eff_stage_id.lower() != eff_code.lower())
    if has_resolved_sid:
        _query_copilot([{"level_keyword": eff_stage_id}], max_pages=2)
    elif eff_stage_id or eff_code:
        kw = eff_stage_id or eff_code
        _query_copilot([{"level_keyword": kw}], max_pages=1)

    def _collect_candidates(allow_doc_match: bool) -> List[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
        out: List[Tuple[int, Dict[str, Any], Dict[str, Any]]] = []
        for it in raw_items.values():
            try:
                content = json.loads(it.get("content", "{}"))
            except Exception:
                continue
            if not isinstance(content, dict):
                continue
            if not _guide_matches_stage(
                content,
                eff_stage_id,
                eff_code,
                level_id=eff_level_id,
                allow_doc_match=allow_doc_match,
            ):
                continue
            like = it.get("like", 0) or 0
            dislike = it.get("dislike", 0) or 0
            views = it.get("views", 0) or 0
            pop_score = like * 50 + views - dislike * 40
            out.append((pop_score, it, content))
        out.sort(key=lambda x: -x[0])
        return out

    candidates = _collect_candidates(allow_doc_match=False)

    # Stage 3: Keyword/document fallback when exact stageId lookup returned 0 valid guides
    if not candidates and eff_code:
        before_cnt = len(raw_items)
        _query_copilot([{"document": eff_code}], max_pages=3)
        if len(raw_items) == before_cnt:
            _query_copilot([{"keyword": eff_code}], max_pages=2)
        candidates = _collect_candidates(allow_doc_match=True)

    detailed: List[Dict[str, Any]] = []
    for pop_score, it, content in candidates[:max_details]:
        cid = it["id"]
        try:
            full = _fetch_json(f"https://prts.maa.plus/copilot/get/{cid}")
            full_content = json.loads(full["data"]["content"])
        except Exception:
            full_content = content
        detailed.append({
            "id": cid,
            "like": it.get("like", 0) or 0,
            "dislike": it.get("dislike", 0) or 0,
            "views": it.get("views", 0) or 0,
            "pop_score": pop_score,
            "content": full_content,
        })
    return detailed


def _run_copilot_round(
    round_idx: int,
    copilot_file: Path,
    maa_dir: Path,
    config: Dict[str, Any],
    base_dir: Path,
    deployment_with_pause: str = "0",
    ignore_requirements: bool = False,
    report_prefix: str = "",
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    stage_code: str = "",
    story_skip: bool = True,
    skip_if_already_3star: bool = False,
    next_stage: Optional[str] = None,
    require_fresh_map_entry: bool = False,
    verify_map_clear: bool = True,
) -> Tuple[int, Dict[str, Any]]:
    def _emit_round_log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    ensure_game_running(config, maa_dir=maa_dir, cancel_event=cancel_event, log_callback=log_callback)
    if not init_maa_sdk(maa_dir, base_dir=base_dir):
        return 2, {"victory": False, "metrics": {"errors": ["Asst.load failed"]}}

    sess = MaaSession(
        maa_dir=maa_dir,
        config=config,
        base_dir=base_dir,
        log_callback=log_callback,
        cancel_event=cancel_event,
    )
    sess.deployment_with_pause = str(deployment_with_pause)
    if not sess.connect():
        return 2, {"victory": False, "metrics": {"errors": ["asst.connect failed"]}}

    reports_dir = base_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{report_prefix}_" if report_prefix else ""
    report_path = reports_dir / f"{prefix}round_{round_idx}_report.json"
    is_chapter_stage = bool(stage_code and "-" in stage_code and not str(stage_code).upper().startswith("CAMP_"))
    resolved_copilot = Path(copilot_file).expanduser().resolve()

    prep_res: Dict[str, Any] = {}
    res: Dict[str, Any] = {}
    post_battle_res: Dict[str, Any] = {}
    try:
        if stage_code and sess.adb_path and sess.adb_addr:
            prep_res = stage_pilot.prepare_battle_entry_if_on_map(
                stage_code,
                sess.adb_path,
                sess.adb_addr,
                maa_dir,
                cancel_event=cancel_event,
                log_callback=log_callback,
                skip_if_already_3star=skip_if_already_3star,
                require_fresh_map_entry=require_fresh_map_entry,
                uncheck_auto_deploy=True,
            ) or {}
            if prep_res.get("already_cleared"):
                adb_path_ok, adb_addr_ok = sess.adb_path, sess.adb_addr
                report_skip = {
                    "round": round_idx,
                    "elapsed_seconds": 0.0,
                    "victory": True,
                    "already_cleared": True,
                    "skipped": True,
                    "map_clear_verified": True,
                    "reason": prep_res.get("reason", "already_cleared_3star"),
                    "displayed_stage": prep_res.get("displayed_stage", stage_code),
                    "copilot_file": str(copilot_file.resolve()),
                    "report_path": str(report_path.resolve()),
                    "metrics": {
                        "kills": None,
                        "costs": None,
                        "last_3_actions": [],
                        "errors": [],
                        "completed_flag": True,
                        "failed_flag": False,
                    },
                    "screenshot": "",
                    "adb_path": adb_path_ok or "",
                    "adb_addr": adb_addr_ok or "",
                    "events_tail": [],
                    "log_tail": [],
                }
                report_path.write_text(json.dumps(report_skip, ensure_ascii=False, indent=2), encoding="utf-8")
                return 0, report_skip

            if prep_res.get("stage_mismatch"):
                adb_path_mm, adb_addr_mm = sess.adb_path, sess.adb_addr
                shot_path_mm = reports_dir / f"{prefix}fail_round_{round_idx}.png"
                if adb_path_mm and adb_addr_mm:
                    capture_adb_screenshot(adb_path_mm, adb_addr_mm, shot_path_mm)
                reason_mm = str(prep_res.get("reason") or f"选关标题校验不符: {stage_code}")
                report_mm = {
                    "round": round_idx,
                    "elapsed_seconds": 0.0,
                    "victory": False,
                    "stage_mismatch": True,
                    "stage_entry_failed": True,
                    "displayed_stage": prep_res.get("displayed_stage", ""),
                    "error": reason_mm,
                    "copilot_file": str(copilot_file.resolve()),
                    "report_path": str(report_path.resolve()),
                    "metrics": {
                        "kills": None,
                        "costs": None,
                        "last_3_actions": [],
                        "errors": [reason_mm],
                        "completed_flag": False,
                        "failed_flag": False,
                        "stage_entry_failed": True,
                        "entry_failed_tasks": ["StageTitleMismatch"],
                    },
                    "prep_entry": prep_res,
                    "screenshot": str(shot_path_mm) if shot_path_mm.is_file() else "",
                    "adb_path": adb_path_mm or "",
                    "adb_addr": adb_addr_mm or "",
                    "events_tail": [],
                    "log_tail": [],
                }
                report_path.write_text(json.dumps(report_mm, ensure_ascii=False, indent=2), encoding="utf-8")
                return 1, report_mm

            # Hard stage-entry consistency guard: NEVER bare-run Copilot if stage_pilot failed to locate/verify/enter!
            if is_chapter_stage and not prep_res.get("entered"):
                adb_path_ef, adb_addr_ef = sess.adb_path, sess.adb_addr
                shot_path_ef = reports_dir / f"{prefix}fail_round_{round_idx}.png"
                if adb_path_ef and adb_addr_ef:
                    capture_adb_screenshot(adb_path_ef, adb_addr_ef, shot_path_ef)
                reason_ef = str(
                    prep_res.get("reason")
                    or f"未能通过 stage_pilot 地图定位并校验进入关卡 {stage_code}，禁止跳过定位空跑 Copilot"
                )
                _emit_round_log(f"[PUSH][进图拦截] {reason_ef}")
                report_ef = {
                    "round": round_idx,
                    "elapsed_seconds": 0.0,
                    "victory": False,
                    "stage_entry_failed": True,
                    "stage_mismatch": False,
                    "error": reason_ef,
                    "copilot_file": str(copilot_file.resolve()),
                    "report_path": str(report_path.resolve()),
                    "metrics": {
                        "kills": None,
                        "costs": None,
                        "last_3_actions": [],
                        "errors": [reason_ef],
                        "completed_flag": False,
                        "failed_flag": False,
                        "stage_entry_failed": True,
                        "entry_failed_tasks": ["StagePilotEntryFailed"],
                    },
                    "prep_entry": prep_res,
                    "screenshot": str(shot_path_ef) if shot_path_ef.is_file() else "",
                    "adb_path": adb_path_ef or "",
                    "adb_addr": adb_addr_ef or "",
                    "events_tail": [],
                    "log_tail": [],
                }
                report_path.write_text(json.dumps(report_ef, ensure_ascii=False, indent=2), encoding="utf-8")
                return 1, report_ef

        ok_file, file_err, _ = ai_strategist.validate_copilot_file(resolved_copilot, maa_dir=maa_dir)
        if not ok_file:
            _emit_round_log(f"[COPILOT][文件校验失败] 路径={resolved_copilot} | 摘要={file_err}")
            adb_path_cf, adb_addr_cf = sess.adb_path, sess.adb_addr
            shot_path_cf = reports_dir / f"{prefix}fail_round_{round_idx}.png"
            if adb_path_cf and adb_addr_cf:
                capture_adb_screenshot(adb_path_cf, adb_addr_cf, shot_path_cf)
            report_cf = {
                "round": round_idx,
                "elapsed_seconds": 0.0,
                "victory": False,
                "battle_failed": False,
                "copilot_file_error": True,
                "copilot_file_error_detail": file_err,
                "error": f"CopilotFileReadError: {file_err}",
                "copilot_file": str(resolved_copilot),
                "report_path": str(report_path.resolve()),
                "metrics": {
                    "kills": None,
                    "costs": None,
                    "last_3_actions": [],
                    "errors": [f"SubTaskError:CopilotTask:CopilotFileReadError:{file_err}"],
                    "failed_flag": False,
                    "battle_failed": False,
                    "completed_flag": False,
                    "copilot_file_error": True,
                    "copilot_file_error_detail": file_err,
                    "stage_entry_failed": False,
                    "formation_unavailable": False,
                },
                "prep_entry": prep_res,
                "screenshot": str(shot_path_cf) if shot_path_cf.is_file() else "",
                "adb_path": adb_path_cf or "",
                "adb_addr": adb_addr_cf or "",
                "events_tail": [],
                "log_tail": [],
            }
            report_path.write_text(json.dumps(report_cf, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1, report_cf

        res = sess.run_single_task(
            "Copilot",
            {
                "filename": str(resolved_copilot),
                "formation": True,
                "add_trust": False,
                "ignore_requirements": ignore_requirements,
            },
            timeout_sec=1800,
            story_skip_on_copilot=story_skip,
        )
        if not res.get("cancelled"):
            is_net_disc, net_reason, _net_probe = _inspect_task_network_disconnect(
                res,
                sess,
                task_type="Copilot",
                cancel_event=_extract_real_cancel_event(cancel_event),
                log_callback=_emit_round_log,
            )
            if is_net_disc:
                net_holder: Dict[str, Any] = {"session": sess}
                rec = recover_from_network_disconnect(
                    net_holder,
                    config,
                    max_attempts=3,
                    task_type=f"Copilot:{stage_code or resolved_copilot.name}",
                    reason=net_reason,
                    log_callback=_emit_round_log,
                    cancel_event=_extract_real_cancel_event(cancel_event),
                )
                sess = net_holder.get("session", sess)
                if rec.get("cancelled"):
                    res = dict(res)
                    res["cancelled"] = True
                elif rec.get("ok"):
                    adb_path_ok, adb_addr_ok = getattr(sess, "adb_path", ""), getattr(sess, "adb_addr", "")
                    report_net_rec = {
                        "round": round_idx,
                        "elapsed_seconds": res.get("elapsed", 0.0),
                        "victory": False,
                        "battle_failed": False,
                        "network_recovered": True,
                        "network_recovery": rec,
                        "copilot_file": str(resolved_copilot),
                        "report_path": str(report_path.resolve()),
                        "metrics": {
                            "kills": None,
                            "costs": None,
                            "last_3_actions": [],
                            "errors": [f"NetworkDisconnectRecovered:{net_reason}"],
                            "completed_flag": False,
                            "failed_flag": False,
                        },
                        "screenshot": "",
                        "adb_path": adb_path_ok or "",
                        "adb_addr": adb_addr_ok or "",
                        "events_tail": res.get("events", [])[-15:],
                        "log_tail": res.get("log_lines", [])[-40:],
                    }
                    report_path.write_text(json.dumps(report_net_rec, ensure_ascii=False, indent=2), encoding="utf-8")
                    return 1, report_net_rec
                else:
                    adb_path_nf, adb_addr_nf = getattr(sess, "adb_path", ""), getattr(sess, "adb_addr", "")
                    err_nf = str(rec.get("error") or "[NET][自愈失败] 推图期间网络掉线自愈失败")
                    report_nf = {
                        "round": round_idx,
                        "elapsed_seconds": res.get("elapsed", 0.0),
                        "victory": False,
                        "battle_failed": False,
                        "network_recovery_failed": True,
                        "network_recovery": rec,
                        "error": err_nf,
                        "copilot_file": str(resolved_copilot),
                        "report_path": str(report_path.resolve()),
                        "metrics": {
                            "kills": None,
                            "costs": None,
                            "last_3_actions": [],
                            "errors": [err_nf],
                            "completed_flag": False,
                            "failed_flag": False,
                        },
                        "screenshot": str(rec.get("screenshot") or ""),
                        "adb_path": adb_path_nf or "",
                        "adb_addr": adb_addr_nf or "",
                        "events_tail": res.get("events", [])[-15:],
                        "log_tail": res.get("log_lines", [])[-40:],
                    }
                    report_path.write_text(json.dumps(report_nf, ensure_ascii=False, indent=2), encoding="utf-8")
                    return 2, report_nf
        entered_formation_pre = bool(
            prep_res.get("outcome") == "formation"
            or prep_res.get("entered")
            or prep_res.get("exited_formation_select")
        )
        prelim_metrics = ai_strategist.parse_battle_metrics(
            res.get("log_lines", []),
            res.get("events", []),
            entered_formation=entered_formation_pre,
        )
        battle_reached = bool(
            prelim_metrics.get("completed_flag")
            or prelim_metrics.get("battle_started")
            or prelim_metrics.get("failed_flag")
            or res.get("story_probe_battle_failed")
            or res.get("story_probe_battle_victory")
        )
        if battle_reached and not res.get("cancelled") and sess.adb_path and sess.adb_addr:
            post_battle_res = stage_pilot.run_story_skip_loop(
                sess.adb_path,
                sess.adb_addr,
                maa_dir,
                target_states=("map",),
                max_duration_sec=22.0,
                poll_interval=0.8,
                min_probe_sec=0.8,
                require_story_seen=False,
                cancel_event=cancel_event,
                log_callback=log_callback,
                stop_on_battle_failed=True,
                dismiss_victory_settlement=True,
            ) or {}
    finally:
        adb_path, adb_addr = sess.adb_path, sess.adb_addr
        sess.close()

    entered_formation = bool(
        prep_res.get("outcome") == "formation"
        or prep_res.get("entered")
        or prep_res.get("exited_formation_select")
    )
    metrics = ai_strategist.parse_battle_metrics(
        res.get("log_lines", []),
        res.get("events", []),
        entered_formation=entered_formation,
    )
    if post_battle_res.get("settlement_stars") is not None and metrics.get("settlement_stars") is None:
        metrics["settlement_stars"] = post_battle_res.get("settlement_stars")
    elif res.get("story_probe_stars") is not None and metrics.get("settlement_stars") is None:
        metrics["settlement_stars"] = res.get("story_probe_stars")

    detected_stars = metrics.get("settlement_stars")
    early_non_3_star = bool(detected_stars is not None and int(detected_stars) < 3)
    if early_non_3_star:
        metrics["non_3_star"] = True
        metrics["battle_failed"] = True
        metrics["failed_flag"] = True
        _emit_round_log(
            f"[VERIFY][非满星拦截] {stage_code or 'Copilot'} Round-{round_idx} 结算仅获得 {detected_stars} 星"
            f"（battle_failed (non_3_star, stars={detected_stars})），严禁放行，将换高赞作业/修轴重打！"
        )

    detected_defeat_early = bool(
        post_battle_res.get("battle_failed")
        or res.get("story_probe_battle_failed")
        or metrics.get("failed_flag")
        or metrics.get("battle_failed")
        or early_non_3_star
    )

    shot_captured = False
    shot_path = reports_dir / f"{prefix}fail_round_{round_idx}.png"
    if detected_defeat_early and not early_non_3_star and adb_path and adb_addr:
        # Preserve defeat screenshot BEFORE tapping Mission Failed back to map!
        shot_captured = capture_adb_screenshot(adb_path, adb_addr, shot_path)
        if post_battle_res.get("battle_failed") or res.get("story_probe_battle_failed"):
            stage_pilot.dismiss_battle_failed_to_map(
                adb_path,
                adb_addr,
                maa_dir,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )

    map_verify: Optional[Dict[str, Any]] = None
    if early_non_3_star:
        map_verify = {
            "verified": False,
            "skipped": False,
            "on_stage_map": True,
            "stage_code": stage_code,
            "non_3_star": True,
            "three_star_verified": False,
            "verified_stars": int(detected_stars),
            "reason": f"battle_failed (non_3_star, stars={detected_stars})",
        }
    elif (
        verify_map_clear
        and is_chapter_stage
        and adb_path
        and adb_addr
        and not res.get("cancelled")
        and metrics.get("completed_flag")
        and not detected_defeat_early
    ):
        map_verify = stage_pilot.verify_stage_cleared_on_map(
            stage_code,
            adb_path,
            adb_addr,
            maa_dir,
            next_stage=next_stage,
            max_swipes=2,
            cancel_event=cancel_event,
            log_callback=log_callback,
            settlement_stars=detected_stars,
            require_three_stars=True,
        )

    verdict = ai_strategist.evaluate_copilot_victory(
        metrics,
        post_battle_state=post_battle_res,
        map_verify=map_verify,
        require_map_verify=bool(verify_map_clear and is_chapter_stage),
        story_probe_battle_failed=bool(res.get("story_probe_battle_failed")),
        story_probe_battle_victory=bool(res.get("story_probe_battle_victory")),
    )
    is_victory = bool(verdict["victory"] and not res.get("cancelled"))

    if not is_victory and verdict.get("battle_failed"):
        metrics["failed_flag"] = True
        metrics["battle_failed"] = True
        reason_join = "; ".join(verdict.get("reasons") or [])
        if reason_join:
            errs = list(metrics.get("errors") or [])
            dual_err = f"DualVerifyFailed: {reason_join}"
            if dual_err not in errs:
                errs.append(dual_err)
            metrics["errors"] = errs[-5:]
        _emit_round_log(
            f"[VERIFY][战败拦截] {stage_code or 'Copilot'} Round-{round_idx} 未通过双重通关硬校验"
            f"（{reason_join or 'battle_failed'}），已强制判定为 battle_failed，严禁推进下一关！"
        )

    if not shot_captured:
        shot_name = f"{prefix}victory_round_{round_idx}.png" if is_victory else f"{prefix}fail_round_{round_idx}.png"
        shot_path = reports_dir / shot_name
        if adb_path and adb_addr:
            capture_adb_screenshot(adb_path, adb_addr, shot_path)

    # Secondary safety check on failure screenshot: if emulator is inside formation_select,
    # the stage is proven unlocked & entered, so never misclassify as stage_entry_failed.
    in_formation_select = bool(metrics.get("formation_select_entered"))
    if (
        not is_victory
        and not in_formation_select
        and shot_path.is_file()
        and not metrics.get("battle_started")
        and not metrics.get("failed_flag")
        and metrics.get("kills") is None
    ):
        try:
            from PIL import Image as _PILImage

            with _PILImage.open(shot_path) as _im:
                _st_fail = stage_pilot.analyze_screen_state(_im, maa_dir, use_ocr_fallback=False)
            if _st_fail.get("in_formation_select"):
                in_formation_select = True
                metrics["formation_select_entered"] = True
                metrics["formation_entered"] = True
                if "BattleStartPre" in (metrics.get("entry_failed_tasks") or []):
                    metrics["entry_failed_tasks"] = [
                        t for t in metrics["entry_failed_tasks"] if t != "BattleStartPre"
                    ]
                    probes = list(metrics.get("harmless_entry_probes") or [])
                    if "BattleStartPre" not in probes:
                        probes.append("BattleStartPre")
                    metrics["harmless_entry_probes"] = probes
                if not any(t in ("BattleStartAll", "FightBegin") for t in (metrics.get("entry_failed_tasks") or [])):
                    metrics["formation_unavailable"] = True
                    metrics["stage_entry_failed"] = False
        except Exception:
            pass

    map_clear_verified = (
        bool(map_verify.get("verified"))
        if isinstance(map_verify, dict)
        else (bool(is_victory) if not (verify_map_clear and is_chapter_stage) else False)
    )
    report = {
        "round": round_idx,
        "elapsed_seconds": res.get("elapsed", 0.0),
        "victory": is_victory,
        "battle_failed": bool(verdict.get("battle_failed") and not is_victory and not metrics.get("copilot_file_error")),
        "copilot_file_error": bool(metrics.get("copilot_file_error")),
        "copilot_file_error_detail": str(metrics.get("copilot_file_error_detail") or ""),
        "non_3_star": bool(verdict.get("non_3_star") and not is_victory),
        "map_clear_verified": map_clear_verified,
        "dual_victory_check": verdict,
        "post_battle_state": post_battle_res,
        "map_verify": map_verify,
        "copilot_file": str(resolved_copilot),
        "report_path": str(report_path.resolve()),
        "metrics": metrics,
        "prep_entry": prep_res,
        "in_formation_select": in_formation_select,
        "screenshot": str(shot_path) if shot_path.is_file() else "",
        "adb_path": adb_path or "",
        "adb_addr": adb_addr or "",
        "events_tail": res.get("events", [])[-20:],
        "log_tail": [l for l in res.get("log_lines", []) if "[TRC]" not in l][-60:],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return (0 if is_victory else 1), report


def _file_signature(path: Path) -> Tuple[int, str]:
    if not path.is_file():
        return (0, "")
    st = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return (st.st_mtime_ns, digest)


def _try_load_copilot_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    for _ in range(3):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and ("actions" in data or "opers" in data):
                return data
            return None
        except Exception:
            time.sleep(0.2)
    return None


def write_agent_task(
    base_dir: Path,
    trigger_reason: str,
    stage_code: str,
    stage_id: str,
    round_idx: int,
    max_rounds: int,
    target_copilot_path: Path,
    failed_copilot_path: Optional[Path] = None,
    screenshot_path: str = "",
    report_path: str = "",
    tile_pos_file: Optional[str] = None,
    battle_state: Optional[Dict[str, Any]] = None,
    rule_suggestion: str = "",
    unavailable_opers: Optional[List[str]] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Write <base_dir>/agent_task.json for external desktop AI Agents (Antigravity / Claude Code / Cursor)."""
    task_file = Path(base_dir).resolve() / "agent_task.json"
    my_box_file = Path(base_dir).resolve() / "data" / "my_box.json"
    unavail_list = [str(x).strip() for x in (unavailable_opers or []) if str(x).strip()]
    instructions = (
        "请结合 battle_state（击杀数 kills、费用 costs、最后动作与错误日志）、screenshot_path 现场截图与 my_box_path 干员库，"
        "直接修改并保存 target_copilot_path 指定的作业 JSON。保存后启动器将自动监听文件变化并立即继续下一轮测试。"
    )
    if trigger_reason == "formation_unavailable":
        instructions = (
            f"关卡 {stage_code} 编队阶段发生干员不可用（不可用干员: {unavail_list or '见 battle_state'}），"
            "自动替补与 PRTS 备选作业均已尝试失败。请查看 screenshot_path 现场截图、battle_state 与 my_box_path 干员库，"
            "修改并保存 target_copilot_path 作业 JSON（剔除不可用干员并调整练度/技能要求或替补干员）后再恢复推图。"
        )
    payload: Dict[str, Any] = {
        "task_id": uuid.uuid4().hex[:12],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "waiting_for_agent",
        "trigger_reason": trigger_reason,
        "reason": trigger_reason,
        "stage_code": stage_code,
        "stage_id": stage_id,
        "round": round_idx,
        "max_rounds": max_rounds,
        "target_copilot_path": str(Path(target_copilot_path).resolve()),
        "failed_copilot_path": str(Path(failed_copilot_path).resolve()) if failed_copilot_path else "",
        "screenshot_path": screenshot_path,
        "report_path": report_path,
        "my_box_path": str(my_box_file),
        "tile_pos_file": tile_pos_file or "",
        "battle_state": battle_state or {},
        "rule_baseline_suggestion": rule_suggestion,
        "instructions_for_agent": instructions,
    }
    if unavail_list or trigger_reason == "formation_unavailable":
        payload["unavailable_opers"] = unavail_list
    task_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return task_file, payload


def _update_agent_task_status(task_file: Path, status: str) -> None:
    try:
        if task_file.is_file():
            data = json.loads(task_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["status"] = status
                data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                task_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def wait_for_agent_copilot_update(
    target_copilot_path: Path,
    task_file: Path,
    cancel_event: Optional[threading.Event] = None,
    manual_resume_event: Optional[threading.Event] = None,
    timeout_sec: float = 0.0,
    poll_interval: float = 0.6,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Watch target_copilot_path until modified by external AI Agent, manually resumed, or cancelled."""
    initial_sig = _file_signature(target_copilot_path)
    t0 = time.time()

    while True:
        if cancel_event and cancel_event.is_set():
            _update_agent_task_status(task_file, "cancelled")
            return "cancelled", None

        if manual_resume_event and manual_resume_event.is_set():
            manual_resume_event.clear()
            cur_sig = _file_signature(target_copilot_path)
            if cur_sig != initial_sig:
                parsed = _try_load_copilot_json(target_copilot_path)
                if parsed is not None:
                    _update_agent_task_status(task_file, "applied")
                    return "agent_updated", parsed
            _update_agent_task_status(task_file, "fallback_rule")
            return "fallback_rule", None

        try:
            if task_file.is_file():
                task_data = json.loads(task_file.read_text(encoding="utf-8"))
                st = str(task_data.get("status", "")).lower()
                if st in ("skip", "fallback", "fallback_rule"):
                    return "fallback_rule", None
                if st == "resolved":
                    parsed = _try_load_copilot_json(target_copilot_path)
                    if parsed is not None:
                        _update_agent_task_status(task_file, "applied")
                        return "agent_updated", parsed
        except Exception:
            pass

        cur_sig = _file_signature(target_copilot_path)
        if cur_sig != initial_sig and cur_sig[0] != 0:
            parsed = _try_load_copilot_json(target_copilot_path)
            if parsed is not None:
                _update_agent_task_status(task_file, "applied")
                return "agent_updated", parsed

        if timeout_sec > 0 and (time.time() - t0) >= timeout_sec:
            _update_agent_task_status(task_file, "timeout_fallback")
            return "timeout_fallback", None

        time.sleep(poll_interval)


def autonomous_clear_stage(
    stage_query: str,
    maa_dir: Path,
    config: Dict[str, Any],
    max_rounds: int = 5,
    base_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    manual_resume_event: Optional[threading.Event] = None,
    agent_wait_timeout_sec: float = 0.0,
    next_stage: Optional[str] = None,
    tutorial_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    tutorial_resume_event: Optional[threading.Event] = None,
    tutorial_poll_interval: float = 3.0,
    daily_mode: bool = False,
    skip_if_already_3star: bool = False,
    require_fresh_map_entry: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """Autonomously clear a stage (story -ST-, tutorial -TR- handoff, or PRTS Copilot battle)."""
    root = Path(base_dir).resolve() if base_dir is not None else get_base_dir()
    maa_path = Path(maa_dir).resolve()
    ensure_runtime_dirs(root)

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    pre_node_type = stage_pilot.classify_stage_node(stage_query)
    stage_info = resolve_stage_info(
        stage_query,
        maa_path,
        base_dir=root,
        allow_online=(pre_node_type == "battle"),
    )
    stage_id = stage_info["stage_id"]
    stage_code = stage_info["code"]
    node_type = stage_pilot.classify_stage_node(stage_code or stage_query)

    if node_type == "story":
        if progress_callback:
            progress_callback(1, 1, f"{stage_code}: 纯剧情节点自动跳过")
        ensure_game_running(config, maa_dir=maa_path, cancel_event=cancel_event, log_callback=_log)
        adb_path = discover_adb_executable(config, maa_dir=maa_path) or ""
        adb_addr = _probe_adb_address(adb_path, config, maa_path, cancel_event=cancel_event)
        ok_story, story_res = stage_pilot.handle_story_stage(
            stage_code,
            adb_path=adb_path,
            adb_addr=adb_addr,
            maa_dir=maa_path,
            cancel_event=cancel_event,
            log_callback=_log,
            next_stage=next_stage,
        )
        story_res.setdefault("stage_id", stage_id)
        return ok_story, story_res

    if node_type == "tutorial":
        if daily_mode:
            msg = f"[PUSH] 日常模式遇到未完成教学关 {stage_code}，立即中止推图以回退 1-7 清空理智（不阻塞日常收菜）。"
            _log(msg)
            return False, {
                "stage": stage_code,
                "stage_id": stage_id,
                "node_type": "tutorial",
                "victory": False,
                "blocked_by_tutorial": True,
                "error": msg,
            }
        handoff_msg = (
            f"[教学关交接] 当前遇到无法跳过的教学关卡（{stage_code}），"
            f"请在模拟器中手动点完本关教学，通关返回地图后系统将自动恢复后续推图！"
        )
        _log(handoff_msg)
        if progress_callback:
            progress_callback(0, 1, f"{stage_code}: 等待教学关手动通关...")
        ensure_game_running(config, maa_dir=maa_path, cancel_event=cancel_event, log_callback=_log)
        adb_path = discover_adb_executable(config, maa_dir=maa_path) or ""
        adb_addr = _probe_adb_address(adb_path, config, maa_path, cancel_event=cancel_event)

        stage_pilot.enter_stage_node(
            stage_code,
            adb_path=adb_path,
            adb_addr=adb_addr,
            maa_dir=maa_path,
            click_start=True,
            cancel_event=cancel_event,
            log_callback=_log,
        )
        if cancel_event and cancel_event.is_set():
            return False, {"stage": stage_code, "stage_id": stage_id, "node_type": "tutorial", "victory": False, "cancelled": True}

        if tutorial_handler is not None:
            try:
                tutorial_handler({
                    "action": "show",
                    "stage_code": stage_code,
                    "next_stage": next_stage or "",
                    "message": handoff_msg,
                })
            except Exception:
                pass
        _notify_windows_alert("MAA 教学关交接提醒", handoff_msg)

        handoff_status = "cancelled"
        try:
            handoff_status = stage_pilot.wait_for_tutorial_completion(
                stage_code,
                next_stage=next_stage,
                adb_path=adb_path,
                adb_addr=adb_addr,
                maa_dir=maa_path,
                poll_interval=tutorial_poll_interval,
                cancel_event=cancel_event,
                manual_resume_event=tutorial_resume_event,
                log_callback=_log,
            )
        finally:
            if tutorial_handler is not None:
                try:
                    tutorial_handler({
                        "action": "hide",
                        "stage_code": stage_code,
                        "next_stage": next_stage or "",
                        "status": handoff_status,
                    })
                except Exception:
                    pass

        if handoff_status == "cancelled":
            return False, {
                "stage": stage_code,
                "stage_id": stage_id,
                "node_type": "tutorial",
                "victory": False,
                "cancelled": True,
                "handoff_status": handoff_status,
            }
        return True, {
            "stage": stage_code,
            "stage_id": stage_id,
            "node_type": "tutorial",
            "victory": True,
            "handoff_status": handoff_status,
            "next_stage": next_stage or "",
        }

    safe_tag = re.sub(r"[^A-Za-z0-9_\-]", "_", stage_code)

    my_box = auto_growth.sync_my_box(maa_path, base_dir=root)
    guides = fetch_prts_guides(stage_id, stage_code, max_details=12, base_dir=root)
    if not guides:
        return False, {
            "stage": stage_code,
            "stage_id": stage_id,
            "victory": False,
            "no_prts_guide": True,
            "error": f"PRTS 作业站暂未收录该新关作业（{stage_code} / {stage_id}），请稍后再试或交由桌面 Agent 处理",
        }

    if stage_id.lower() == stage_code.lower():
        first_sn = str((guides[0].get("content") or {}).get("stage_name", "") or "").strip()
        if first_sn and first_sn.lower() != stage_code.lower() and "#f#" not in first_sn.lower():
            stage_id = first_sn

    client = ai_strategist.OpenAICompatClient.from_config(config)
    raw_guides_by_id: Dict[Any, Dict[str, Any]] = {g.get("id"): g for g in guides}
    adapted_list = [
        ai_strategist.adapt_copilot(g, my_box, stage_id, maa_dir=maa_path, client=client)
        for g in guides
    ]
    adapted_list.sort(key=lambda x: -x["compat_score"])

    out_dir = root / "copilots"
    out_dir.mkdir(parents=True, exist_ok=True)
    cand_idx = 0
    cur_copilot = copy.deepcopy(adapted_list[cand_idx]["copilot"])
    strategy: Dict[str, Any] = {
        "deployment_with_pause": "1" if (adapted_list[cand_idx].get("substitutions") or adapted_list[cand_idx].get("e1_downgrades")) else "0",
        "ignore_requirements": False,
        "reason": "初始高赞最佳匹配作业",
        "mode": adapted_list[cand_idx].get("mode", "pure_maa"),
    }
    history: List[Dict[str, Any]] = []
    use_agent_collab = (not daily_mode) and (not client.is_configured()) and (collab_handler is not None)
    oper_blacklist: List[str] = []
    oper_reasons: Dict[str, str] = {}
    tried_guide_indices: set[int] = {cand_idx}
    same_guide_sub_attempts: Dict[int, int] = {}
    same_guide_relax_attempts: Dict[int, int] = {}

    def _emit_formation_unavailable_agent_task(
        target_path: Path,
        round_num: int,
        report_data: Dict[str, Any],
        rule_hint: str,
    ) -> Tuple[Optional[Path], Optional[Dict[str, Any]]]:
        if daily_mode:
            return None, None
        metrics = (report_data or {}).get("metrics", {})
        shot_str = str((report_data or {}).get("screenshot", "") or "")
        tried_ids = [adapted_list[i].get("guide_id") for i in sorted(tried_guide_indices) if 0 <= i < len(adapted_list)]
        battle_state = {
            "formation_unavailable": True,
            "stage_entry_failed": False,
            "unavailable_opers": list(oper_blacklist),
            "unavailable_oper_reasons": dict(oper_reasons),
            "oper_blacklist": list(oper_blacklist),
            "tried_guide_ids": tried_ids,
            "kills": metrics.get("kills"),
            "costs": metrics.get("costs"),
            "last_3_actions": metrics.get("last_3_actions", []),
            "errors": metrics.get("errors", []),
            "log_tail": (report_data or {}).get("log_tail", [])[-30:],
        }
        task_file, payload = write_agent_task(
            base_dir=root,
            trigger_reason="formation_unavailable",
            stage_code=stage_code,
            stage_id=stage_id,
            round_idx=round_num,
            max_rounds=max_rounds,
            target_copilot_path=target_path,
            failed_copilot_path=target_path,
            screenshot_path=shot_str,
            report_path=str((report_data or {}).get("report_path", "")),
            tile_pos_file=stage_info.get("tile_file"),
            battle_state=battle_state,
            rule_suggestion=rule_hint,
            unavailable_opers=list(oper_blacklist),
        )
        payload["agent_task_path"] = str(task_file)
        _log(
            f"[Agent协同] 已写入 agent_task.json (formation_unavailable) | 关卡={stage_code} | "
            f"不可用干员={oper_blacklist} | 当前作业={target_path} | 现场截图={shot_str or '无'}"
        )
        notif = config.get("notifications", {}) if isinstance(config.get("notifications"), dict) else {}
        if notif.get("webhook_enabled") and notif.get("notify_on_agent_task", True):
            threading.Thread(
                target=send_webhook_notification,
                args=(
                    config,
                    f"MAA 编队干员不可用需外部 Agent 处理 ({stage_code})",
                    f"原因: formation_unavailable\n不可用干员: {oper_blacklist}\n当前作业: {target_path}\n现场截图: {shot_str or '无'}",
                    "agent_task",
                    payload,
                ),
                daemon=True,
            ).start()
        if collab_handler is not None:
            try:
                collab_handler(payload)
            except Exception:
                pass
        return task_file, payload

    def _build_formation_exhausted_result(
        copilot_path: Path,
        round_num: int,
        report_data: Dict[str, Any],
        reason_text: str,
    ) -> Tuple[bool, Dict[str, Any]]:
        task_file, _ = _emit_formation_unavailable_agent_task(
            target_path=copilot_path,
            round_num=round_num,
            report_data=report_data,
            rule_hint=reason_text,
        )
        out_sum: Dict[str, Any] = {
            "stage": stage_code,
            "stage_id": stage_id,
            "victory": False,
            "stage_entry_failed": False,
            "formation_unavailable": True,
            "unavailable_opers": list(oper_blacklist),
            "unavailable_oper_reasons": dict(oper_reasons),
            "copilot_file": str(copilot_path),
            "screenshot": str(report_data.get("screenshot", "") or ""),
            "rounds_used": len(history),
            "history": history,
            "error": "formation_unavailable",
            "reason": reason_text,
            "agent_task_written": bool(task_file is not None),
        }
        if task_file is not None:
            out_sum["agent_task_path"] = str(task_file)
        return False, out_sum

    def _request_external_agent(
        trigger_reason: str,
        target_path: Path,
        round_num: int,
        failed_path: Optional[Path] = None,
        report_data: Optional[Dict[str, Any]] = None,
        rule_hint: str = "",
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        metrics = (report_data or {}).get("metrics", {})
        battle_state = {
            "kills": metrics.get("kills"),
            "costs": metrics.get("costs"),
            "last_3_actions": metrics.get("last_3_actions", []),
            "errors": metrics.get("errors", []),
            "missing_ops": adapted_list[cand_idx].get("missing_ops", []),
            "substitutions": adapted_list[cand_idx].get("substitutions", {}),
            "log_tail": (report_data or {}).get("log_tail", [])[-30:],
        }
        task_file, payload = write_agent_task(
            base_dir=root,
            trigger_reason=trigger_reason,
            stage_code=stage_code,
            stage_id=stage_id,
            round_idx=round_num,
            max_rounds=max_rounds,
            target_copilot_path=target_path,
            failed_copilot_path=failed_path,
            screenshot_path=str((report_data or {}).get("screenshot", "")),
            report_path=str((report_data or {}).get("report_path", "")),
            tile_pos_file=stage_info.get("tile_file"),
            battle_state=battle_state,
            rule_suggestion=rule_hint,
        )
        payload["agent_task_path"] = str(task_file)
        _log(
            f"[Agent协同] 已生成 agent_task.json ({trigger_reason}) | 关卡={stage_code} | "
            f"击杀数={battle_state.get('kills')} | 等待外部 AI Agent 修改 {target_path.name}..."
        )
        notif = config.get("notifications", {}) if isinstance(config.get("notifications"), dict) else {}
        if notif.get("webhook_enabled") and notif.get("notify_on_agent_task", True):
            threading.Thread(
                target=send_webhook_notification,
                args=(
                    config,
                    f"MAA 需要 AI Agent 协同修轴 ({stage_code})",
                    f"原因: {trigger_reason}\n击杀数: {battle_state.get('kills')}\n目标作业: {target_path}",
                    "agent_task",
                    payload,
                ),
                daemon=True,
            ).start()
        try:
            collab_handler(payload)
        except Exception:
            pass
        status_res, updated = wait_for_agent_copilot_update(
            target_copilot_path=target_path,
            task_file=task_file,
            cancel_event=cancel_event,
            manual_resume_event=manual_resume_event,
            timeout_sec=agent_wait_timeout_sec,
        )
        if status_res == "agent_updated":
            _log(f"[Agent协同] 检测到 {target_path.name} 已被外部 AI Agent 修改保存，自动继续执行！")
        elif status_res in ("fallback_rule", "timeout_fallback"):
            _log(f"[Agent协同] 切换为本地规则基线继续执行 ({status_res})。")
        return status_res, updated

    if use_agent_collab and adapted_list[cand_idx].get("missing_ops"):
        r1_path = (out_dir / f"push_{safe_tag}_r1.json").resolve()
        try:
            r1_path, cur_copilot = ai_strategist.write_validated_copilot_file(r1_path, cur_copilot, maa_dir=maa_path)
        except Exception:
            r1_path.write_text(json.dumps(cur_copilot, ensure_ascii=False, indent=2), encoding="utf-8")
        st_code, updated_json = _request_external_agent(
            trigger_reason="missing_operators",
            target_path=r1_path,
            round_num=1,
            rule_hint=f"存在缺失干员 {adapted_list[cand_idx]['missing_ops']}，请根据 my_box.json 替补改写作业。",
        )
        if st_code == "cancelled":
            return False, {"stage": stage_code, "stage_id": stage_id, "victory": False, "cancelled": True, "rounds_used": 0, "history": history}
        if st_code == "agent_updated" and updated_json is not None:
            cur_copilot = updated_json
            strategy["mode"] = "external_agent"
            strategy["reason"] = "外部 AI Agent 已改写初始作业 JSON"

    entry_recovery_attempted = False
    for round_idx in range(1, max_rounds + 1):
        if cancel_event and cancel_event.is_set():
            break
        if progress_callback:
            progress_callback(round_idx, max_rounds, f"{stage_code} 第 {round_idx}/{max_rounds} 轮: {strategy['reason']}")

        copilot_path = (out_dir / f"push_{safe_tag}_r{round_idx}.json").resolve()
        pre_write_err = ""
        try:
            copilot_path, cur_copilot = ai_strategist.write_validated_copilot_file(
                copilot_path, cur_copilot, maa_dir=maa_path
            )
        except Exception as exc:
            pre_write_err = str(exc)

        if pre_write_err:
            _log(f"[COPILOT][落盘强校验失败] 路径={copilot_path} | 摘要={pre_write_err}")
            rc = 1
            report = {
                "round": round_idx,
                "elapsed_seconds": 0.0,
                "victory": False,
                "battle_failed": False,
                "copilot_file_error": True,
                "copilot_file_error_detail": pre_write_err,
                "error": f"CopilotFileReadError: {pre_write_err}",
                "copilot_file": str(copilot_path),
                "screenshot": "",
                "metrics": {
                    "kills": None,
                    "costs": None,
                    "last_3_actions": [],
                    "errors": [f"SubTaskError:CopilotTask:CopilotFileReadError:{pre_write_err}"],
                    "failed_flag": False,
                    "battle_failed": False,
                    "completed_flag": False,
                    "copilot_file_error": True,
                    "copilot_file_error_detail": pre_write_err,
                },
                "events_tail": [],
                "log_tail": [],
            }
        else:
            rc, report = _run_copilot_round(
                round_idx=round_idx,
                copilot_file=copilot_path,
                maa_dir=maa_path,
                config=config,
                base_dir=root,
                deployment_with_pause=strategy["deployment_with_pause"],
                ignore_requirements=strategy["ignore_requirements"],
                report_prefix=f"push_{safe_tag}",
                cancel_event=cancel_event,
                log_callback=log_callback,
                stage_code=stage_code,
                story_skip=True,
                skip_if_already_3star=skip_if_already_3star,
                next_stage=next_stage,
                require_fresh_map_entry=(round_idx == 1 and require_fresh_map_entry),
            )
        history.append({
            "round": round_idx,
            "guide_id": adapted_list[cand_idx]["guide_id"],
            "mode": strategy.get("mode", "pure_maa"),
            "reason": strategy["reason"],
            "victory": bool(report.get("victory")),
            "battle_failed": bool(report.get("battle_failed")),
            "copilot_file_error": bool(report.get("copilot_file_error") or (report.get("metrics") or {}).get("copilot_file_error")),
            "non_3_star": bool(report.get("non_3_star")),
            "settlement_stars": (report.get("dual_victory_check") or {}).get("settlement_stars"),
            "map_clear_verified": report.get("map_clear_verified"),
            "already_cleared": bool(report.get("already_cleared")),
            "elapsed_seconds": report.get("elapsed_seconds"),
            "errors": report.get("metrics", {}).get("errors", []),
            "screenshot": report.get("screenshot", ""),
        })
        if rc == 0 and report.get("already_cleared"):
            _log(f"[PUSH] 关卡 {stage_code} 已 3 星通关且解锁代理指挥，自动跳过（不消耗理智）。")
            return True, {
                "stage": stage_code,
                "stage_id": stage_id,
                "victory": True,
                "already_cleared": True,
                "map_clear_verified": True,
                "skipped": True,
                "rounds_used": round_idx,
                "history": history,
            }
        if rc == 0 and report.get("victory"):
            return True, {
                "stage": stage_code,
                "stage_id": stage_id,
                "victory": True,
                "map_clear_verified": report.get("map_clear_verified", True),
                "dual_victory_check": report.get("dual_victory_check", {}),
                "rounds_used": round_idx,
                "final_guide_id": adapted_list[cand_idx]["guide_id"],
                "copilot_file": str(copilot_path),
                "history": history,
            }

        if report.get("network_recovery_failed"):
            err_msg = str(report.get("error") or f"[NET][自愈失败] 推图关卡 {stage_code} 网络掉线自愈失败")
            _log(err_msg)
            return False, {
                "stage": stage_code,
                "stage_id": stage_id,
                "victory": False,
                "network_recovery_failed": True,
                "screenshot": report.get("screenshot", ""),
                "rounds_used": len(history),
                "history": history,
                "error": err_msg,
            }

        if report.get("network_recovered"):
            _log(f"[NET] 关卡 {stage_code} 第 {round_idx} 轮网络掉线已自愈恢复在线，下一轮将自动重进关卡续跑当前作业。")
            strategy["reason"] = "网络掉线自愈恢复后重进关卡续跑"
            continue

        if report.get("stage_mismatch"):
            err_msg = str(report.get("error") or f"选关标题校验不符: {stage_code}")
            _log(f"[PUSH][防错关] {err_msg}")
            return False, {
                "stage": stage_code,
                "stage_id": stage_id,
                "victory": False,
                "stage_entry_failed": True,
                "stage_mismatch": True,
                "displayed_stage": report.get("displayed_stage", ""),
                "screenshot": report.get("screenshot", ""),
                "rounds_used": len(history),
                "history": history,
                "error": err_msg,
            }

        # Priority 0: CopilotFileReadError is a JSON/file read/parse error, NEVER battle_failed!
        cf_info = ai_strategist.extract_copilot_file_error_info(report)
        if cf_info["is_copilot_file_error"]:
            cf_path_str = str(cf_info["copilot_file"] or copilot_path.resolve())
            cf_detail = str(cf_info["detail"] or "CopilotFileReadError")
            _log(
                f"[PUSH][CopilotFileReadError] 作业文件读取/解析失败（非战斗翻车）| 路径={cf_path_str} | "
                f"摘要={cf_detail}，禁止误判 battle_failed 或原地重试同一坏文件，尝试自动切换下一份合法 PRTS 备选作业..."
            )
            if round_idx < max_rounds and not (cancel_event and cancel_event.is_set()):
                new_idx, new_cop, strategy = ai_strategist.repair_copilot(
                    adapted_list,
                    cand_idx,
                    cur_copilot,
                    report,
                    my_box,
                    round_idx,
                    client=None,
                    oper_blacklist=oper_blacklist,
                    tried_indices=tried_guide_indices,
                    maa_dir=maa_path,
                    raw_guides_by_id=raw_guides_by_id,
                )
                if strategy.get("repair_action") == "switch_guide" and new_idx != cand_idx:
                    cand_idx = new_idx
                    cur_copilot = new_cop
                    tried_guide_indices.add(cand_idx)
                    _log(f"[PUSH][CopilotFileReadError] {strategy.get('reason', '')}")
                    continue

            _log(
                f"[PUSH][CopilotFileReadError] 路径={cf_path_str} | 摘要={cf_detail} | "
                f"所有合法 PRTS 备选作业已耗尽或达到最大轮次 ({max_rounds})，中止推图（未触发战斗翻车协同）。"
            )
            return False, {
                "stage": stage_code,
                "stage_id": stage_id,
                "victory": False,
                "battle_failed": False,
                "copilot_file_error": True,
                "copilot_file_error_detail": cf_detail,
                "copilot_file": cf_path_str,
                "screenshot": str(report.get("screenshot", "") or ""),
                "rounds_used": len(history),
                "history": history,
                "error": "copilot_file_error",
                "reason": cf_detail,
            }

        # Priority 1: Check BattleFormationTask / formation_select failure BEFORE stage_entry_failed!
        # Entering formation proves the stage IS unlocked; harmless leading BattleStartPre must be ignored.
        form_info = ai_strategist.extract_formation_unavailable_info(report)
        if not form_info["is_formation_unavailable"] and ai_strategist.is_stage_entry_failure(report):
            recovery_info: Optional[Dict[str, Any]] = None
            if not entry_recovery_attempted and not (cancel_event and cancel_event.is_set()):
                entry_recovery_attempted = True
                emu_cfg = config.get("emulator", {}) if isinstance(config.get("emulator"), dict) else {}
                rec_adb_path = str(report.get("adb_path") or emu_cfg.get("adb_path") or "").strip()
                rec_adb_addr = str(report.get("adb_addr") or emu_cfg.get("adb_address") or "").strip()
                if rec_adb_path and rec_adb_addr:
                    try:
                        recovery_info = stage_pilot.recover_from_abnormal_subpage(
                            rec_adb_path,
                            rec_adb_addr,
                            maa_path,
                            stage_code=stage_code,
                            max_back_clicks=3,
                            cancel_event=cancel_event,
                            log_callback=_log,
                        )
                    except Exception as exc:
                        recovery_info = {"needed_recovery": False, "recovered": False, "error": str(exc)}

                    if recovery_info and recovery_info.get("needed_recovery") and recovery_info.get("recovered") and not (cancel_event and cancel_event.is_set()):
                        _log(
                            f"[RECOVER] 已从异常子页面 ({recovery_info.get('initial_page')}) 复位至 "
                            f"{recovery_info.get('final_page')}，正在重新尝试进图 {stage_code}（仅重试 1 次）..."
                        )
                        rc2, report2 = _run_copilot_round(
                            round_idx=round_idx,
                            copilot_file=copilot_path,
                            maa_dir=maa_path,
                            config=config,
                            base_dir=root,
                            deployment_with_pause=strategy["deployment_with_pause"],
                            ignore_requirements=strategy["ignore_requirements"],
                            report_prefix=f"push_{safe_tag}_recover",
                            cancel_event=cancel_event,
                            log_callback=log_callback,
                            stage_code=stage_code,
                            story_skip=True,
                            skip_if_already_3star=skip_if_already_3star,
                            next_stage=next_stage,
                            require_fresh_map_entry=True,
                        )
                        history.append({
                            "round": round_idx,
                            "recovery_retry": True,
                            "recovered_from": recovery_info.get("initial_page", ""),
                            "guide_id": adapted_list[cand_idx]["guide_id"],
                            "mode": strategy.get("mode", "pure_maa"),
                            "reason": "异常子页面复位后重新进图重试",
                            "victory": bool(report2.get("victory")),
                            "battle_failed": bool(report2.get("battle_failed")),
                            "map_clear_verified": report2.get("map_clear_verified"),
                            "already_cleared": bool(report2.get("already_cleared")),
                            "elapsed_seconds": report2.get("elapsed_seconds"),
                            "errors": report2.get("metrics", {}).get("errors", []),
                            "screenshot": report2.get("screenshot", ""),
                        })
                        if rc2 == 0 and report2.get("already_cleared"):
                            _log(f"[PUSH] 复位重进后确认关卡 {stage_code} 已 3 星通关且解锁代理指挥，自动跳过。")
                            return True, {
                                "stage": stage_code,
                                "stage_id": stage_id,
                                "victory": True,
                                "already_cleared": True,
                                "map_clear_verified": True,
                                "skipped": True,
                                "recovered_from": recovery_info.get("initial_page", ""),
                                "recovery_info": recovery_info,
                                "rounds_used": len(history),
                                "history": history,
                            }
                        if rc2 == 0 and report2.get("victory"):
                            return True, {
                                "stage": stage_code,
                                "stage_id": stage_id,
                                "victory": True,
                                "map_clear_verified": report2.get("map_clear_verified", True),
                                "dual_victory_check": report2.get("dual_victory_check", {}),
                                "recovered_from": recovery_info.get("initial_page", ""),
                                "recovery_info": recovery_info,
                                "rounds_used": len(history),
                                "final_guide_id": adapted_list[cand_idx]["guide_id"],
                                "copilot_file": str(copilot_path),
                                "history": history,
                            }
                        report = report2
                        form_info = ai_strategist.extract_formation_unavailable_info(report)

            if not form_info["is_formation_unavailable"]:
                entry_tasks_fail = [str(t) for t in (report.get("metrics", {}).get("entry_failed_tasks") or [])]
                prep_entry_info = report.get("prep_entry") if isinstance(report.get("prep_entry"), dict) else {}
                intercepted_pre_copilot = bool(
                    any(t in ("StagePilotEntryFailed", "StageTitleMismatch") for t in entry_tasks_fail)
                    or (prep_entry_info and not prep_entry_info.get("entered") and not report.get("events_tail"))
                )
                if intercepted_pre_copilot:
                    real_reason = str(
                        report.get("error")
                        or prep_entry_info.get("reason")
                        or f"未能通过 stage_pilot 定位并校验进入关卡 {stage_code}"
                    )
                    _log(
                        f"[PUSH] 关卡 {stage_code} 在进图前被拦截（尚未启动 Copilot）：{real_reason}，"
                        f"禁止触发战斗翻车修轴与原地重试，立即中止当前关卡。"
                    )
                else:
                    _log(
                        f"[PUSH] 关卡 {stage_code} 未解锁或进图失败（BattleStartPre/BattleStartAll 且 kills=None），"
                        f"禁止触发战斗翻车修轴与原地重试，立即中止当前关卡。"
                    )
                fail_out: Dict[str, Any] = {
                    "stage": stage_code,
                    "stage_id": stage_id,
                    "victory": False,
                    "stage_entry_failed": True,
                    "stage_mismatch": bool(report.get("stage_mismatch")),
                    "screenshot": report.get("screenshot", ""),
                    "rounds_used": len(history),
                    "history": history,
                    "error": str(report.get("error") or "stage_entry_failed"),
                }
                if recovery_info is not None:
                    fail_out["recovery_info"] = recovery_info
                return False, fail_out

        # Automatic self-healing chain for formation_unavailable (no human/Agent interruption until all options exhausted)
        if form_info["is_formation_unavailable"]:
            for op_k, op_r in form_info["oper_reasons"].items():
                if op_k not in oper_reasons or (oper_reasons[op_k] == "Unavailable" and op_r != "Unavailable"):
                    oper_reasons[op_k] = op_r

            history[-1]["formation_unavailable"] = True
            history[-1]["unavailable_opers"] = list(form_info["unavailable_opers"])
            _log(
                f"[PUSH][编队自愈] 关卡 {stage_code} 已成功进图，但 BattleFormationTask 报干员不可用: "
                f"{form_info['unavailable_opers']}（详情: {form_info['oper_reasons']}），"
                f"优先检查非必要练度限制 / 全匹配 PRTS 作业 / 同分支替补..."
            )

            if cancel_event and cancel_event.is_set():
                break

            if round_idx < max_rounds:
                prev_idx = cand_idx
                cand_idx, cur_copilot, strategy = ai_strategist.repair_copilot(
                    adapted_list,
                    cand_idx,
                    cur_copilot,
                    report,
                    my_box,
                    round_idx,
                    client=None,
                    oper_blacklist=oper_blacklist,
                    tried_indices=tried_guide_indices,
                    same_guide_sub_attempts=same_guide_sub_attempts.get(prev_idx, 0),
                    same_guide_relax_attempts=same_guide_relax_attempts.get(prev_idx, 0),
                    maa_dir=maa_path,
                    raw_guides_by_id=raw_guides_by_id,
                )
                action_kind = strategy.get("repair_action", "")
                if action_kind == "relax_requirements":
                    same_guide_relax_attempts[cand_idx] = same_guide_relax_attempts.get(cand_idx, 0) + 1
                    _log(f"[PUSH][编队自愈] {strategy.get('reason', '')}")
                    continue

                for op_u in (strategy.get("oper_blacklist") or form_info["unavailable_opers"]):
                    if op_u not in oper_blacklist:
                        oper_blacklist.append(op_u)

                if action_kind == "substitute_operator":
                    same_guide_sub_attempts[cand_idx] = same_guide_sub_attempts.get(cand_idx, 0) + 1
                    _log(f"[PUSH][编队自愈] {strategy.get('reason', '')}")
                    continue
                if action_kind == "switch_guide":
                    tried_guide_indices.add(cand_idx)
                    _log(f"[PUSH][编队自愈] {strategy.get('reason', '')}")
                    continue

                reason_ex = str(
                    strategy.get("reason")
                    or f"所有同分支替补与 PRTS 备选作业均失败（不可用干员: {oper_blacklist}）"
                )
                _log(f"[PUSH][编队自愈] {reason_ex}")
                return _build_formation_exhausted_result(copilot_path, round_idx, report, reason_ex)

            for op_u in form_info["unavailable_opers"]:
                if op_u not in oper_blacklist:
                    oper_blacklist.append(op_u)
            reason_max = f"已达最大重试轮次 ({max_rounds})，编队干员仍不可用（黑名单: {oper_blacklist}）"
            _log(f"[PUSH][编队自愈] {reason_max}")
            return _build_formation_exhausted_result(copilot_path, round_idx, report, reason_max)

        if round_idx < max_rounds and not (cancel_event and cancel_event.is_set()):
            cand_idx, cur_copilot, strategy = ai_strategist.repair_copilot(
                adapted_list,
                cand_idx,
                cur_copilot,
                report,
                my_box,
                round_idx,
                client=client,
                oper_blacklist=oper_blacklist,
                tried_indices=tried_guide_indices,
                maa_dir=maa_path,
                raw_guides_by_id=raw_guides_by_id,
            )
            tried_guide_indices.add(cand_idx)
            if strategy.get("abort_retry"):
                _log(f"[PUSH] {stage_code}: {strategy.get('reason', '中止重试')}")
                break
            if use_agent_collab:
                next_path = (out_dir / f"push_{safe_tag}_r{round_idx + 1}.json").resolve()
                try:
                    next_path, cur_copilot = ai_strategist.write_validated_copilot_file(
                        next_path, cur_copilot, maa_dir=maa_path
                    )
                except Exception:
                    next_path.write_text(json.dumps(cur_copilot, ensure_ascii=False, indent=2), encoding="utf-8")
                st_code, updated_json = _request_external_agent(
                    trigger_reason="battle_failed",
                    target_path=next_path,
                    round_num=round_idx,
                    failed_path=copilot_path,
                    report_data=report,
                    rule_hint=str(strategy.get("reason", "")),
                )
                if st_code == "cancelled":
                    break
                if st_code == "agent_updated" and updated_json is not None:
                    cur_copilot = updated_json
                    strategy["mode"] = "external_agent"
                    strategy["reason"] = "外部 AI Agent 已修轴并更新作业 JSON"

    return False, {
        "stage": stage_code,
        "stage_id": stage_id,
        "victory": False,
        "rounds_used": len(history),
        "history": history,
    }


# ======================================================================
# Long-Term Exploration: Roguelike (集成战略) & Reclamation (生息演算)
# ======================================================================

ROGUELIKE_THEMES: Tuple[str, ...] = (
    "BlackFlow",
    "JieGarden",
    "Sarkaz",
    "Sami",
    "Mizuki",
    "Phantom",
)

ROGUELIKE_THEME_ALIASES: Dict[str, str] = {
    "blackflow": "BlackFlow",
    "drowning": "BlackFlow",
    "沉沦者的黑流树海": "BlackFlow",
    "黑流树海": "BlackFlow",
    "jiegarden": "JieGarden",
    "sui": "JieGarden",
    "岁的界园志异": "JieGarden",
    "界园志异": "JieGarden",
    "sarkaz": "Sarkaz",
    "萨卡兹的无终奇语": "Sarkaz",
    "无终奇语": "Sarkaz",
    "sami": "Sami",
    "探索者的银凇止境": "Sami",
    "银凇止境": "Sami",
    "mizuki": "Mizuki",
    "水月与深蓝之树": "Mizuki",
    "深蓝之树": "Mizuki",
    "phantom": "Phantom",
    "傀影与猩红孤钻": "Phantom",
    "猩红孤钻": "Phantom",
}


def _normalize_roguelike_theme_name(raw_theme: str) -> str:
    cleaned = str(raw_theme or "").strip()
    if not cleaned:
        return ""
    alias_hit = ROGUELIKE_THEME_ALIASES.get(cleaned.lower())
    if alias_hit:
        return alias_hit
    for canonical in ROGUELIKE_THEMES:
        if canonical.lower() == cleaned.lower():
            return canonical
    return cleaned

ROGUELIKE_THEME_DEFAULTS: Dict[str, Dict[str, Any]] = {
    "BlackFlow": {"squad": "指挥分队", "roles": "取长补短", "core_char": "", "mode": 0},
    "JieGarden": {"squad": "指挥分队", "roles": "取长补短", "core_char": "", "mode": 0},
    "Sarkaz": {"squad": "指挥分队", "roles": "取长补短", "core_char": "", "mode": 0},
    "Sami": {"squad": "指挥分队", "roles": "取长补短", "core_char": "", "mode": 0},
    "Mizuki": {"squad": "心胜于物分队", "roles": "取长补短", "core_char": "", "mode": 0},
    "Phantom": {"squad": "指挥分队", "roles": "取长补短", "core_char": "", "mode": 0},
}

ROGUELIKE_MODE_MAP: Dict[str, int] = {
    "exp": 0,
    "level": 0,
    "score": 0,
    "0": 0,
    "investment": 1,
    "invest": 1,
    "1": 1,
    "collectible": 4,
    "burn": 4,
    "4": 4,
    "collapsalparadigms": 5,
    "5": 5,
    "monthlysquad": 6,
    "monthly": 6,
    "6": 6,
    "deepexploration": 7,
    "deep": 7,
    "7": 7,
    "sarkazfastinvestment": 10001,
    "10001": 10001,
    "blackflowcultivation": 30001,
    "cultivation": 30001,
    "30001": 30001,
}

# Verified against MaaCore.dll (v6.22.0 / MAA v6.20.1+):
# - Real MAA task type name is "Reclamation" (NOT "ReclamationAlgorithm").
# - Theme "RelaunchAnchor" (《重启锚点》, Reclamation3.json) accepts int modes:
#     16 = RA1 (RA-1 关卡循环 · 精耕细作), 32 = RA4 (RA-4 关卡循环 · 筹划经营), 48 = RA15 (RA-15 关卡循环 · 筹划经营)
# - Theme "Tales" (《沙洲遗闻》, plugin/Tales.json) accepts int modes:
#     0 = ProsperityNoSave (无存档刷繁荣度), 1 = ProsperityInSave (存档内造物刷繁荣度)
RECLAMATION_MODE_MAP: Dict[str, Dict[str, int]] = {
    "RelaunchAnchor": {
        "ra1": 16,
        "ra-1": 16,
        "1": 16,
        "16": 16,
        "ra4": 32,
        "ra-4": 32,
        "4": 32,
        "32": 32,
        "ra15": 48,
        "ra-15": 48,
        "15": 48,
        "48": 48,
    },
    "Tales": {
        "prosperitynosave": 0,
        "nosave": 0,
        "0": 0,
        "prosperityinsave": 1,
        "insave": 1,
        "craft": 1,
        "1": 1,
    },
}

RECLAMATION_RELAUNCH_STAGE_MAP: Dict[int, Tuple[str, str]] = {
    16: ("RA1", "RA-1"),
    32: ("RA4", "RA-4"),
    48: ("RA15", "RA-15"),
}

RECLAMATION_TALES_MODE_NAMES: Dict[int, str] = {
    0: "ProsperityNoSave",
    1: "ProsperityInSave",
}

_COLLECTIBLE_AWARD_KEY_MAP: Dict[str, str] = {
    "hotwater": "hot_water",
    "hot_water": "hot_water",
    "shield": "shield",
    "ingot": "ingot",
    "random": "random",
    "dice": "dice",
    "idea": "ideas",
    "ideas": "ideas",
    "hope": "ticket",
    "ticket": "ticket",
}


def inspect_maa_exploration_capabilities(maa_dir: Optional[Path]) -> Dict[str, Any]:
    """Inspect MAA resource/tasks for Roguelike & Reclamation support, including BlackFlow rewards/tech-tree gaps."""
    out: Dict[str, Any] = {
        "maa_version": "",
        "roguelike_task_type": "Roguelike",
        "reclamation_task_type": "Reclamation",
        "roguelike_themes": [],
        "reclamation_themes": [],
        "roguelike_theme_capabilities": {},
        "reclamation_theme_capabilities": {},
    }
    if maa_dir is None or not Path(maa_dir).is_dir():
        return out

    root = Path(maa_dir).resolve()
    out["maa_version"] = get_maa_version(root)

    rl_dir = root / "resource" / "tasks" / "Roguelike"
    if rl_dir.is_dir():
        for fn in sorted(rl_dir.glob("*.json")):
            stem = fn.stem
            if stem.lower() in ("base", "routing"):
                continue
            out["roguelike_themes"].append(stem)
            try:
                raw_text = fn.read_text(encoding="utf-8", errors="ignore")
                data = json.loads(raw_text)
                keys = list(data.keys()) if isinstance(data, dict) else []
                has_level_check = any(k.endswith("@CheckLevel") or "CheckLevel" in k for k in keys)
                has_reward_claim = any(
                    any(tok in k.lower() for tok in ("claimreward", "claimlevel", "levelreward", "monthlycommission", "commissionclaim"))
                    for k in keys
                ) or ("本月委托栏" in raw_text and "一键领取" in raw_text)
                has_tech_tree = any(
                    any(tok in k.lower() for tok in ("techtree", "lifegame", "upgradetech", "skilltree", "cognitive"))
                    for k in keys
                ) or ("生命游戏" in raw_text)
                out["roguelike_theme_capabilities"][stem] = {
                    "task_count": len(keys),
                    "native_level_check": has_level_check,
                    "native_reward_claim": has_reward_claim,
                    "native_tech_tree_upgrade": has_tech_tree,
                    "requires_adb_post_settlement": not (has_reward_claim and has_tech_tree),
                }
            except Exception:
                out["roguelike_theme_capabilities"][stem] = {
                    "task_count": 0,
                    "native_level_check": False,
                    "native_reward_claim": False,
                    "native_tech_tree_upgrade": False,
                    "requires_adb_post_settlement": True,
                }

    ra_dir = root / "resource" / "tasks" / "RA"
    if ra_dir.is_dir():
        ra3_file = ra_dir / "Reclamation3.json"
        if ra3_file.is_file():
            out["reclamation_themes"].append("RelaunchAnchor")
            try:
                ra3_raw = ra3_file.read_text(encoding="utf-8", errors="ignore")
                ra3_data = json.loads(ra3_raw)
                ra3_keys = list(ra3_data.keys()) if isinstance(ra3_data, dict) else []
                supported_modes: Dict[str, int] = {}
                if "RelaunchAnchor@RA@RA1-Entry" in ra3_keys:
                    supported_modes["RA1"] = 16
                if "RelaunchAnchor@RA@RA4-Entry" in ra3_keys:
                    supported_modes["RA4"] = 32
                if "RelaunchAnchor@RA@RA15-Entry" in ra3_keys:
                    supported_modes["RA15"] = 48
                native_nav = any(
                    k.startswith("RelaunchAnchor@RA@Navigation") or "Terminal-Entry" in k
                    for k in ra3_keys
                ) or ("重启锚点" in ra3_raw and "长期探索" in ra3_raw)
                out["reclamation_theme_capabilities"]["RelaunchAnchor"] = {
                    "task_type": "Reclamation",
                    "supported_modes": supported_modes,
                    "native_home_navigation": native_nav,
                    "requires_stage_pilot_entry": not native_nav,
                    "entry_requirement": "需位于《重启锚点》大地图或目标关卡「开启建设」界面",
                }
            except Exception:
                out["reclamation_theme_capabilities"]["RelaunchAnchor"] = {
                    "task_type": "Reclamation",
                    "supported_modes": {"RA1": 16, "RA4": 32, "RA15": 48},
                    "native_home_navigation": False,
                    "requires_stage_pilot_entry": True,
                    "entry_requirement": "需位于《重启锚点》大地图或目标关卡「开启建设」界面",
                }

        tales_file = ra_dir / "plugin" / "Tales.json"
        base_ra_file = ra_dir / "plugin" / "base.json"
        if tales_file.is_file() or (ra_dir / "Reclamation2.json").is_file():
            out["reclamation_themes"].append("Tales")
            has_base_nav = base_ra_file.is_file() and ("RA@Navigation" in base_ra_file.read_text(encoding="utf-8", errors="ignore"))
            out["reclamation_theme_capabilities"]["Tales"] = {
                "task_type": "Reclamation",
                "supported_modes": {"ProsperityNoSave": 0, "ProsperityInSave": 1},
                "native_home_navigation": bool(has_base_nav),
                "requires_stage_pilot_entry": not bool(has_base_nav),
                "entry_requirement": "支持主界面自动导航（建议在生息演算主题界面启动）",
            }

    return out


def load_gui_task_config(maa_dir: Optional[Path], task_type: str) -> Dict[str, Any]:
    """Read a TaskQueue entry (e.g. 'Roguelike' or 'Reclamation') from <maa_dir>/config/gui.new.json."""
    if maa_dir is None:
        return {}
    gui_cfg = Path(maa_dir) / "config" / "gui.new.json"
    if not gui_cfg.is_file():
        return {}
    try:
        data = json.loads(gui_cfg.read_text(encoding="utf-8"))
        cur = data.get("Current", "Default")
        queue = data.get("Configurations", {}).get(cur, {}).get("TaskQueue", [])
        if isinstance(queue, list):
            for item in queue:
                if isinstance(item, dict) and str(item.get("TaskType", "")).lower() == task_type.lower():
                    return dict(item)
    except Exception:
        pass
    return {}


def sync_exploration_from_maa_gui(maa_dir: Optional[Path]) -> Dict[str, Any]:
    """Extract normalized Roguelike & Reclamation config fields from original MAA <maa_dir>/config/gui.new.json."""
    if maa_dir is None:
        return {"ok": False, "error": "尚未定位 MAA 目录，无法同步原版配置。"}
    gui_cfg = Path(maa_dir) / "config" / "gui.new.json"
    if not gui_cfg.is_file():
        return {"ok": False, "error": f"未找到原版 MAA 配置文件：{gui_cfg}"}

    gui_rl = load_gui_task_config(maa_dir, "Roguelike")
    gui_ra = load_gui_task_config(maa_dir, "Reclamation")
    if not gui_rl and not gui_ra:
        return {"ok": False, "error": f"原版配置文件 {gui_cfg.name} 中未包含肉鸽或生息演算任务配置。"}

    rl_out: Dict[str, Any] = {}
    if gui_rl:
        rl_out["use_gui_config"] = True
        raw_theme = _normalize_roguelike_theme_name(str(gui_rl.get("Theme", "") or ""))
        if raw_theme:
            rl_out["theme"] = raw_theme
        if str(gui_rl.get("Mode", "") or "").strip():
            rl_out["mode"] = str(gui_rl.get("Mode")).strip()
        if isinstance(gui_rl.get("Difficulty"), int) and not isinstance(gui_rl.get("Difficulty"), bool):
            rl_out["difficulty"] = gui_rl["Difficulty"]
        rl_out["squad"] = str(gui_rl.get("Squad", "") or "").strip()
        rl_out["roles"] = str(gui_rl.get("Roles", "") or "").strip()
        rl_out["core_char"] = str(gui_rl.get("CoreChar", "") or "").strip()
        if isinstance(gui_rl.get("StartCount"), int) and not isinstance(gui_rl.get("StartCount"), bool):
            rl_out["starts_count"] = gui_rl["StartCount"]
        for gui_k, cfg_k in (
            ("Investment", "investment_enabled"),
            ("InvestWithMoreScore", "investment_with_more_score"),
            ("StopWhenDepositFull", "stop_when_investment_full"),
            ("StopAtFinalBoss", "stop_at_final_boss"),
            ("StopWhenLevelMax", "stop_at_max_level"),
            ("UseSupport", "use_support"),
            ("UseSupportNonFriend", "use_nonfriend_support"),
            ("RefreshTraderWithDice", "refresh_trader_with_dice"),
            ("StartWithEliteTwo", "start_with_elite_two"),
            ("StartWithEliteTwoOnly", "only_start_with_elite_two"),
        ):
            if isinstance(gui_rl.get(gui_k), bool):
                rl_out[cfg_k] = gui_rl[gui_k]
        if isinstance(gui_rl.get("InvestCount"), int) and not isinstance(gui_rl.get("InvestCount"), bool):
            rl_out["investments_count"] = gui_rl["InvestCount"]
        if str(gui_rl.get("BlackFlowCultivationTarget", "") or "").strip():
            rl_out["blackflow_cultivation_target"] = str(gui_rl.get("BlackFlowCultivationTarget")).strip()

    ra_out: Dict[str, Any] = {}
    if gui_ra:
        ra_out["use_gui_config"] = True
        raw_ra_theme = str(gui_ra.get("Theme", "") or "").strip()
        if raw_ra_theme.lower() == "relaunchanchor":
            raw_ra_theme = "RelaunchAnchor"
        elif raw_ra_theme.lower() == "tales":
            raw_ra_theme = "Tales"
        elif raw_ra_theme.lower() == "fire":
            raw_ra_theme = "RelaunchAnchor"
        if raw_ra_theme:
            ra_out["theme"] = raw_ra_theme
        if str(gui_ra.get("Mode", "") or "").strip():
            ra_out["mode"] = str(gui_ra.get("Mode")).strip()
        ra_out["tools_to_craft"] = _parse_tools_to_craft(gui_ra.get("ToolToCraft", ""))
        if isinstance(gui_ra.get("MaxCraftCountPerRound"), int) and not isinstance(gui_ra.get("MaxCraftCountPerRound"), bool):
            ra_out["num_craft_batches"] = gui_ra["MaxCraftCountPerRound"]
        if isinstance(gui_ra.get("IncrementMode"), int) and not isinstance(gui_ra.get("IncrementMode"), bool):
            ra_out["increment_mode"] = gui_ra["IncrementMode"]
        if isinstance(gui_ra.get("ClearStore"), bool):
            ra_out["clear_store"] = gui_ra["ClearStore"]

    return {
        "ok": True,
        "source": str(gui_cfg.resolve()),
        "has_roguelike": bool(gui_rl),
        "has_reclamation": bool(gui_ra),
        "roguelike": rl_out,
        "reclamation": ra_out,
    }


def _pick_first_present(*values: Any, default: Any = None) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        return v
    return default


def _resolve_roguelike_mode(raw_mode: Any, theme: str) -> int:
    if isinstance(raw_mode, int) and not isinstance(raw_mode, bool):
        mode_int = raw_mode
    else:
        key = re.sub(r"[\s_\-]+", "", str(raw_mode or "Exp")).lower()
        mode_int = ROGUELIKE_MODE_MAP.get(key, 0)

    # MAA v6.22.0 BlackFlow accepts 0 (Exp), 1 (Investment), 30001 (Cultivation); reject mode 4 gracefully
    if theme == "BlackFlow" and mode_int == 4:
        return 30001
    return mode_int


def _parse_collectible_start_awards(raw: Any) -> Dict[str, bool]:
    if isinstance(raw, dict):
        return {str(k): bool(v) for k, v in raw.items()}
    tokens = [re.sub(r"[\s\-]+", "", t).lower() for t in re.split(r"[,;，；\s]+", str(raw or "")) if t.strip()]
    out: Dict[str, bool] = {}
    for tok in tokens:
        mapped = _COLLECTIBLE_AWARD_KEY_MAP.get(tok)
        if mapped:
            out[mapped] = True
    return out


def resolve_roguelike_settings(
    config: Optional[Dict[str, Any]] = None,
    maa_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve effective Roguelike settings: built-in defaults < gui.new.json < launcher_config < overrides."""
    cfg = config or {}
    rl_cfg = cfg.get("roguelike", {}) if isinstance(cfg.get("roguelike"), dict) else {}
    ov = overrides if isinstance(overrides, dict) else {}
    use_gui = bool(ov.get("use_gui_config", rl_cfg.get("use_gui_config", True)))
    gui_task = load_gui_task_config(maa_dir, "Roguelike") if use_gui else {}

    theme = _normalize_roguelike_theme_name(
        str(_pick_first_present(ov.get("theme"), rl_cfg.get("theme"), gui_task.get("Theme"), default="BlackFlow"))
    ) or "BlackFlow"
    theme_def = ROGUELIKE_THEME_DEFAULTS.get(theme, ROGUELIKE_THEME_DEFAULTS["BlackFlow"])
    # Prevent cross-theme config leakage from gui.new.json (e.g. Phantom/Mizuki squad/difficulty leaking into Sarkaz)
    gui_theme = _normalize_roguelike_theme_name(str(gui_task.get("Theme", "") or ""))
    gui_same_theme = (not gui_theme) or (gui_theme.lower() == theme.lower())
    gui_theme_task = gui_task if gui_same_theme else {}

    raw_mode = _pick_first_present(ov.get("mode"), rl_cfg.get("mode"), gui_theme_task.get("Mode"), default=theme_def["mode"])
    mode_int = _resolve_roguelike_mode(raw_mode, theme)

    difficulty = int(_pick_first_present(ov.get("difficulty"), rl_cfg.get("difficulty"), gui_theme_task.get("Difficulty"), default=2147483647))
    squad = str(_pick_first_present(ov.get("squad"), rl_cfg.get("squad"), gui_theme_task.get("Squad"), default=theme_def["squad"])).strip()
    roles = str(_pick_first_present(ov.get("roles"), rl_cfg.get("roles"), gui_theme_task.get("Roles"), default=theme_def["roles"])).strip()
    core_char = str(_pick_first_present(ov.get("core_char"), rl_cfg.get("core_char"), gui_theme_task.get("CoreChar"), default=theme_def["core_char"])).strip()

    starts_count = int(_pick_first_present(ov.get("starts_count"), rl_cfg.get("starts_count"), gui_task.get("StartCount"), default=999999))
    investment_enabled = bool(_pick_first_present(ov.get("investment_enabled"), rl_cfg.get("investment_enabled"), gui_task.get("Investment"), default=True))
    investments_count = int(_pick_first_present(ov.get("investments_count"), rl_cfg.get("investments_count"), gui_task.get("InvestCount"), default=999))
    invest_more_score = bool(_pick_first_present(ov.get("investment_with_more_score"), rl_cfg.get("investment_with_more_score"), gui_task.get("InvestWithMoreScore"), default=False))
    stop_invest_full = bool(_pick_first_present(ov.get("stop_when_investment_full"), rl_cfg.get("stop_when_investment_full"), gui_task.get("StopWhenDepositFull"), default=False))
    stop_final_boss = bool(_pick_first_present(ov.get("stop_at_final_boss"), rl_cfg.get("stop_at_final_boss"), gui_task.get("StopAtFinalBoss"), default=False))
    stop_max_level = bool(_pick_first_present(ov.get("stop_at_max_level"), rl_cfg.get("stop_at_max_level"), gui_task.get("StopWhenLevelMax"), default=False))
    use_support = bool(_pick_first_present(ov.get("use_support"), rl_cfg.get("use_support"), gui_task.get("UseSupport"), default=False))
    use_nonfriend = bool(_pick_first_present(ov.get("use_nonfriend_support"), rl_cfg.get("use_nonfriend_support"), gui_task.get("UseSupportNonFriend"), default=False))
    refresh_dice = bool(_pick_first_present(ov.get("refresh_trader_with_dice"), rl_cfg.get("refresh_trader_with_dice"), gui_task.get("RefreshTraderWithDice"), default=False))
    start_e2 = bool(_pick_first_present(ov.get("start_with_elite_two"), rl_cfg.get("start_with_elite_two"), gui_task.get("StartWithEliteTwo"), default=False))
    only_e2 = bool(_pick_first_present(ov.get("only_start_with_elite_two"), rl_cfg.get("only_start_with_elite_two"), gui_task.get("StartWithEliteTwoOnly"), default=False))
    if only_e2 and not start_e2:
        only_e2 = False

    maa_params: Dict[str, Any] = {
        "theme": theme,
        "mode": mode_int,
        "difficulty": difficulty,
        "starts_count": starts_count,
        "investment_enabled": investment_enabled,
        "investments_count": investments_count,
        "investment_with_more_score": invest_more_score,
        "stop_when_investment_full": stop_invest_full,
        "stop_at_final_boss": stop_final_boss,
        "stop_at_max_level": stop_max_level,
        "squad": squad,
        "roles": roles,
        "use_support": use_support,
        "use_nonfriend_support": use_nonfriend,
        "refresh_trader_with_dice": refresh_dice,
    }
    if core_char:
        maa_params["core_char"] = core_char
    if start_e2:
        maa_params["start_with_elite_two"] = True
        if only_e2:
            maa_params["only_start_with_elite_two"] = True

    if mode_int == 4:
        maa_params["collectible_mode_shopping"] = bool(
            _pick_first_present(ov.get("collectible_mode_shopping"), rl_cfg.get("collectible_mode_shopping"), gui_theme_task.get("CollectibleShopping"), default=False)
        )
        col_squad = str(_pick_first_present(ov.get("collectible_mode_squad"), rl_cfg.get("collectible_mode_squad"), gui_theme_task.get("SquadCollectible"), default="")).strip()
        if col_squad:
            maa_params["collectible_mode_squad"] = col_squad
        start_awards = _parse_collectible_start_awards(
            _pick_first_present(ov.get("collectible_mode_start_list"), rl_cfg.get("collectible_mode_start_list"), gui_theme_task.get("CollectibleStartAwards"), default="")
        )
        if start_awards:
            maa_params["collectible_mode_start_list"] = start_awards

    if mode_int == 6:
        maa_params["monthly_squad_auto_iterate"] = bool(gui_task.get("MonthlySquadAutoIterate", True))
        maa_params["monthly_squad_check_comms"] = bool(gui_task.get("MonthlySquadCheckComms", True))
    if mode_int == 7:
        maa_params["deep_exploration_auto_iterate"] = bool(gui_task.get("DeepExplorationAutoIterate", True))

    if theme == "BlackFlow":
        bf_target = str(
            _pick_first_present(
                ov.get("blackflow_cultivation_target"),
                rl_cfg.get("blackflow_cultivation_target"),
                gui_theme_task.get("BlackFlowCultivationTarget"),
                default="",
            )
        ).strip()
        if bf_target and mode_int == 30001:
            maa_params["blackflow_cultivation_target"] = bf_target
        bf_strat = str(_pick_first_present(ov.get("blackflow_strategy"), rl_cfg.get("blackflow_strategy"), default="")).strip()
        if bf_strat:
            maa_params["blackflow_strategy"] = bf_strat

    if bool(_pick_first_present(ov.get("start_with_seed"), rl_cfg.get("start_with_seed"), gui_task.get("StartWithSeed"), default=False)):
        seed_val = str(_pick_first_present(ov.get("seed"), rl_cfg.get("seed"), gui_task.get("Seed"), default="")).strip()
        if seed_val:
            maa_params["start_with_seed"] = True
            maa_params["seed"] = seed_val

    return {
        "task_type": "Roguelike",
        "theme": theme,
        "mode_label": str(raw_mode),
        "params": maa_params,
        "loaded_from_gui_config": bool(gui_task),
        "ai_copilot_enabled": bool(_pick_first_present(ov.get("ai_copilot_enabled"), rl_cfg.get("ai_copilot_enabled"), default=False)),
        "boss_prts_assist": bool(_pick_first_present(ov.get("boss_prts_assist"), rl_cfg.get("boss_prts_assist"), default=True)),
        "auto_claim_rewards": bool(_pick_first_present(ov.get("auto_claim_rewards"), rl_cfg.get("auto_claim_rewards"), default=True)),
        "auto_upgrade_tech_tree": bool(_pick_first_present(ov.get("auto_upgrade_tech_tree"), rl_cfg.get("auto_upgrade_tech_tree"), default=True)),
        "timeout_sec": int(_pick_first_present(ov.get("timeout_sec"), rl_cfg.get("timeout_sec"), default=7200)),
    }


def build_roguelike_task_params(
    config: Optional[Dict[str, Any]] = None,
    maa_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return MAA Core v6.22.0 compatible Roguelike append_task params dict."""
    return resolve_roguelike_settings(config=config, maa_dir=maa_dir, overrides=overrides)["params"]


def _resolve_reclamation_mode(raw_mode: Any, theme: str) -> Tuple[int, str]:
    """Return (mode_int, warning_msg) validated against MaaCore ReclamationConfig."""
    table = RECLAMATION_MODE_MAP.get(theme, RECLAMATION_MODE_MAP["RelaunchAnchor"])
    default_int = 16 if theme == "RelaunchAnchor" else 0
    if isinstance(raw_mode, int) and not isinstance(raw_mode, bool):
        if raw_mode in table.values():
            return raw_mode, ""
        mapped = table.get(str(raw_mode))
        if mapped is not None:
            return mapped, ""
        return default_int, f"生息演算主题 {theme} 不支持模式值 {raw_mode}，已自动修正为默认模式 {default_int}"
    raw_str = str(raw_mode or "").strip()
    if not raw_str:
        return default_int, ""
    key = re.sub(r"[\s_]+", "", raw_str).lower()
    if key in table:
        return table[key], ""
    return default_int, f"生息演算主题 {theme} 不支持模式 '{raw_str}'，已自动修正为默认模式 {default_int}"


def _parse_tools_to_craft(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in re.split(r"[,;，；\s]+", raw) if t.strip()]
    return []


def resolve_reclamation_settings(
    config: Optional[Dict[str, Any]] = None,
    maa_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve effective Reclamation settings: built-in defaults < gui.new.json < launcher_config < overrides."""
    cfg = config or {}
    ra_cfg = cfg.get("reclamation", {}) if isinstance(cfg.get("reclamation"), dict) else {}
    ov = overrides if isinstance(overrides, dict) else {}
    use_gui = bool(ov.get("use_gui_config", ra_cfg.get("use_gui_config", True)))
    gui_task = load_gui_task_config(maa_dir, "Reclamation") if use_gui else {}

    theme = str(_pick_first_present(ov.get("theme"), ra_cfg.get("theme"), gui_task.get("Theme"), default="RelaunchAnchor")).strip()
    if theme.lower() in ("relaunchanchor", "ra3", "reclamation3", "重启锚点"):
        theme = "RelaunchAnchor"
    elif theme.lower() in ("tales", "ra2", "reclamation2", "沙洲遗闻"):
        theme = "Tales"
    elif theme.lower() in ("fire", "沙中之火"):
        # Deprecated/closed in MAA v6.20+
        theme = "RelaunchAnchor"

    default_mode = "RA1" if theme == "RelaunchAnchor" else "ProsperityNoSave"
    raw_mode = _pick_first_present(ov.get("mode"), ra_cfg.get("mode"), gui_task.get("Mode"), default=default_mode)
    mode_int, mode_warning = _resolve_reclamation_mode(raw_mode, theme)

    if theme == "RelaunchAnchor":
        mode_name, stage_code = RECLAMATION_RELAUNCH_STAGE_MAP.get(mode_int, ("RA1", "RA-1"))
    else:
        mode_name = RECLAMATION_TALES_MODE_NAMES.get(mode_int, "ProsperityNoSave")
        stage_code = ""

    tools = _parse_tools_to_craft(
        _pick_first_present(ov.get("tools_to_craft"), ra_cfg.get("tools_to_craft"), gui_task.get("ToolToCraft"), default=[])
    )
    if theme == "Tales" and mode_int == 1 and not tools:
        tools = ["荧光棒"]

    num_batches = int(_pick_first_present(ov.get("num_craft_batches"), ra_cfg.get("num_craft_batches"), gui_task.get("MaxCraftCountPerRound"), default=16))
    inc_mode = int(_pick_first_present(ov.get("increment_mode"), ra_cfg.get("increment_mode"), gui_task.get("IncrementMode"), default=0))
    clear_store = bool(_pick_first_present(ov.get("clear_store"), ra_cfg.get("clear_store"), gui_task.get("ClearStore"), default=False))

    maa_params: Dict[str, Any] = {
        "theme": theme,
        "mode": mode_int,
        "clear_store": clear_store,
    }
    if theme == "Tales":
        maa_params["increment_mode"] = inc_mode
        maa_params["num_craft_batches"] = num_batches
        if tools:
            maa_params["tools_to_craft"] = tools

    return {
        "task_type": "Reclamation",
        "theme": theme,
        "mode_name": mode_name,
        "stage_code": stage_code,
        "mode_label": mode_name if mode_warning else str(raw_mode),
        "mode_warning": mode_warning,
        "params": maa_params,
        "loaded_from_gui_config": bool(gui_task),
        "timeout_sec": int(_pick_first_present(ov.get("timeout_sec"), ra_cfg.get("timeout_sec"), default=7200)),
    }


def build_reclamation_task_params(
    config: Optional[Dict[str, Any]] = None,
    maa_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return MAA Core v6.22.0 compatible Reclamation append_task params dict."""
    return resolve_reclamation_settings(config=config, maa_dir=maa_dir, overrides=overrides)["params"]


def extract_roguelike_metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    """Parse battle report metrics from Roguelike MAA callback events & asst.log."""
    runs_started = 0
    settlements = 0
    investments = 0
    investment_full = False
    max_level_reached = False
    stopped_at_final_boss = False
    monthly_squad_completed = False
    deep_exploration_completed = False
    blackflow_profile = ""
    blackflow_result: Optional[Dict[str, Any]] = None
    milestones: List[Any] = []

    log_text = "\n".join(res.get("log_lines", []))
    for ev in res.get("events", []):
        m = ev.get("message", "")
        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
        what = d.get("what", "")
        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
        tname = str(det.get("task", "") or "")

        if m in ("SubTaskStart", "SubTaskCompleted"):
            if "StartExplore" in tname and "CD" not in tname and "EnterCheck" not in tname:
                runs_started += 1
            if "GamePass" in tname or "MissionFailedFlag2" in tname:
                settlements += 1
            if "StageDreadfulFoe-5" in tname or "RoguelikeControlTaskPlugin-Stop" in tname:
                stopped_at_final_boss = True
            if "CheckLevelMax" in tname:
                max_level_reached = True
        elif m == "SubTaskExtraInfo":
            if what == "RoguelikeInvestment":
                investments += int(det.get("count", 1) or 1)
            elif what in ("RoguelikeInvestmentReachFull", "RoguelikeInvestmentReachLimit"):
                investment_full = True
            elif what == "BlackFlowStrategyStarted":
                blackflow_profile = str(det.get("profile", "") or "")
            elif what == "BlackFlowStrategyResult":
                blackflow_result = det
            elif what == "BlackFlowMilestoneChanged":
                milestones.append(det)
            elif what == "MonthlySquadCompleted":
                monthly_squad_completed = True
            elif what == "DeepExplorationCompleted":
                deep_exploration_completed = True

    if "RoguelikeInvestmentReachFull" in log_text or "deposit full" in log_text:
        investment_full = True
    if "StageDreadfulFoe-5" in log_text and "RoguelikeControlTaskPlugin-Stop" in log_text:
        stopped_at_final_boss = True

    return {
        "ok": bool(res.get("ok", False)),
        "cancelled": bool(res.get("cancelled", False)),
        "timed_out": bool(res.get("timed_out", False)),
        "elapsed": res.get("elapsed", 0.0),
        "runs_started": runs_started,
        "settlements": settlements,
        "investments": investments,
        "investment_full": investment_full,
        "max_level_reached": max_level_reached,
        "stopped_at_final_boss": stopped_at_final_boss,
        "monthly_squad_completed": monthly_squad_completed,
        "deep_exploration_completed": deep_exploration_completed,
        "blackflow_profile": blackflow_profile,
        "blackflow_result": blackflow_result,
        "milestones": milestones[-15:],
        "chain_errors": res.get("chain_errors", []),
    }


def extract_reclamation_metrics(res: Dict[str, Any]) -> Dict[str, Any]:
    """Parse battle report metrics from Reclamation MAA callback events & asst.log."""
    craft_rounds = 0
    cycles_completed = 0
    store_cleared = False
    prosperity_limit_reached = False

    log_text = "\n".join(res.get("log_lines", []))
    for ev in res.get("events", []):
        m = ev.get("message", "")
        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
        what = d.get("what", "")
        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
        tname = str(det.get("task", "") or "")

        if m in ("SubTaskStart", "SubTaskCompleted"):
            if "FinishConstruction" in tname or "FinishCalc" in tname:
                cycles_completed += 1
            if "ProsperityReachLimit" in tname or "ProsperityMax" in tname:
                prosperity_limit_reached = True
            if "RA@Store@Underfunded" in tname or "RA@Store@Swipe" in tname:
                store_cleared = True
            if "PIS-Craft" in tname and "Confirm" not in tname:
                craft_rounds += 1
        elif m == "SubTaskExtraInfo":
            if what == "ReclamationCraft":
                craft_rounds += 1

    if "ProsperityReachLimit" in log_text:
        prosperity_limit_reached = True

    return {
        "ok": bool(res.get("ok", False)),
        "cancelled": bool(res.get("cancelled", False)),
        "timed_out": bool(res.get("timed_out", False)),
        "elapsed": res.get("elapsed", 0.0),
        "cycles_completed": cycles_completed,
        "craft_rounds": craft_rounds,
        "store_cleared": store_cleared,
        "prosperity_limit_reached": prosperity_limit_reached,
        "chain_errors": res.get("chain_errors", []),
    }


def _is_roguelike_begin_error(res: Dict[str, Any]) -> bool:
    """Return True when Roguelike failed with a recoverable SubTaskError (Begin / StrategyChange / Stages / Burden)."""
    if not isinstance(res, dict) or res.get("ok") or res.get("cancelled") or res.get("timed_out"):
        return False

    def _matches_recoverable_token(val: Any) -> bool:
        s = str(val or "").strip().lower()
        return any(
            tok in s
            for tok in (
                "roguelikebegin",
                "roguelike@begin",
                "roguelike2begin",
                "strategychange",
                "roguelike@stages",
                "roguelike2stages",
                "chooseburdenoperator",
                "decreaseburden",
            )
        )

    for se in res.get("sub_errors", []) or []:
        if isinstance(se, dict):
            first = se.get("first")
            if isinstance(first, list) and any(_matches_recoverable_token(x) for x in first):
                return True
            if _matches_recoverable_token(se.get("pre_task")):
                return True

    for ev in res.get("events", []) or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("message") == "SubTaskError":
            d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
            first = d.get("first")
            if isinstance(first, list) and any(_matches_recoverable_token(x) for x in first):
                return True
            if _matches_recoverable_token(d.get("pre_task")):
                return True

    log_text = "\n".join(str(x) for x in (res.get("log_lines") or []))
    if "SubTaskError" in log_text and any(
        k in log_text
        for k in (
            "Roguelike@Begin",
            "RoguelikeBegin",
            "StrategyChange",
            "Roguelike@Stages",
            "Roguelike2Stages",
            "ChooseBurdenOperator",
        )
    ):
        return True
    return False


def _is_reclamation_entry_error(res: Dict[str, Any], metrics: Dict[str, Any]) -> bool:
    """Return True when Reclamation failed entry recognition (e.g. RelaunchAnchor@MapRA1-Entry / RA1-Entry)."""
    if not isinstance(res, dict) or res.get("ok") or res.get("cancelled") or res.get("timed_out"):
        return False
    if int(metrics.get("cycles_completed", 0) or 0) > 0:
        return False

    def _matches_entry_token(val: Any) -> bool:
        s = str(val or "").strip().lower()
        return any(
            k in s
            for k in (
                "mapra1-entry",
                "mapra4-entry",
                "mapra15-entry",
                "ra1-entry",
                "ra4-entry",
                "ra15-entry",
                "ra1-prepare",
                "ra4-prepare",
                "ra15-prepare",
                "openconstruction",
                "relaunchanchor@",
            )
        )

    for se in res.get("sub_errors", []) or []:
        if isinstance(se, dict):
            first = se.get("first")
            if isinstance(first, list) and any(_matches_entry_token(x) for x in first):
                return True
            if _matches_entry_token(se.get("pre_task")):
                return True

    for ev in res.get("events", []) or []:
        if not isinstance(ev, dict):
            continue
        if ev.get("message") == "SubTaskError":
            d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
            first = d.get("first")
            if isinstance(first, list) and any(_matches_entry_token(x) for x in first):
                return True
            if _matches_entry_token(d.get("pre_task")):
                return True

    log_text = "\n".join(str(x) for x in (res.get("log_lines") or []))
    if any(k in log_text for k in ("MapRA1-Entry", "MapRA4-Entry", "MapRA15-Entry", "RA1-Entry", "RA4-Entry", "RA15-Entry")):
        return True
    return bool(res.get("chain_errors") or res.get("sub_errors"))


def run_roguelike_task(
    session_holder: Dict[str, MaaSession],
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
    base_dir: Optional[Path] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
    manual_resume_event: Optional[threading.Event] = None,
    boss_stage_hint: str = "",
    max_recovery_retries: int = 3,
) -> Dict[str, Any]:
    """Execute native MAA Roguelike task with guide-popup auto-dismiss, optional hybrid AI Copilot & post-settlement."""
    cur_sess = session_holder["session"]
    eff_maa_dir = Path(maa_dir).resolve() if maa_dir is not None else getattr(cur_sess, "maa_dir", None)
    eff_base = Path(base_dir).resolve() if base_dir is not None else getattr(cur_sess, "base_dir", get_base_dir())
    eff_cancel = cancel_event or getattr(cur_sess, "cancel_event", None)

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
                return
            except Exception:
                pass
        if hasattr(cur_sess, "_emit_log"):
            cur_sess._emit_log(msg)

    caps = inspect_maa_exploration_capabilities(eff_maa_dir)
    resolved = resolve_roguelike_settings(config=config, maa_dir=eff_maa_dir, overrides=overrides)
    theme = resolved["theme"]
    params = copy.deepcopy(resolved["params"])
    ai_enabled = resolved["ai_copilot_enabled"]
    boss_prts = resolved["boss_prts_assist"]
    timeout_sec = resolved["timeout_sec"]

    if eff_cancel and eff_cancel.is_set():
        return {"ok": False, "cancelled": True, "theme": theme, "params": params, "capabilities": caps}

    # In hybrid AI mode with Boss PRTS assist, pause natively before 5F Final Boss for PRTS/AI handoff
    user_forced_stop_boss = params.get("stop_at_final_boss", False)
    if ai_enabled and boss_prts and not user_forced_stop_boss:
        params["stop_at_final_boss"] = True

    _log(
        f"[ROGUELIKE] 启动集成战略任务 | 主题={theme} | 模式={params['mode']}({resolved['mode_label']}) | "
        f"分队={params.get('squad')} | 职业组={params.get('roles')} | 核心干员={params.get('core_char') or '自动'} | "
        f"AI辅助={ai_enabled} | 继承gui.new.json={resolved['loaded_from_gui_config']}"
    )

    guide_popups_dismissed = 0
    guide_retry_triggered = False
    recovery_retries = 0
    subpanel_escapes = 0

    # Pre-handoff scan: dismiss any full-screen mechanism guide popup before MAA takes over
    adb_p_pre = getattr(cur_sess, "adb_path", "") or ""
    adb_a_pre = getattr(cur_sess, "adb_addr", "") or ""
    if isinstance(adb_p_pre, str) and adb_p_pre and isinstance(adb_a_pre, str) and adb_a_pre:
        pre_guide = stage_pilot.dismiss_guide_popups(
            adb_p_pre,
            adb_a_pre,
            maa_dir=eff_maa_dir,
            cancel_event=eff_cancel,
            log_callback=_log,
        )
        guide_popups_dismissed += int(pre_guide.get("dismissed_count", 0) or 0)
        if pre_guide.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
            return {"ok": False, "cancelled": True, "theme": theme, "params": params, "capabilities": caps}

    run_ctx = roguelike_ai_copilot.RoguelikeRunContext(theme=theme)
    copilot: Optional[roguelike_ai_copilot.RoguelikeAICopilot] = None
    tick_cb: Optional[Callable[[MaaSession, List[Dict[str, Any]]], None]] = None

    if ai_enabled:
        copilot = roguelike_ai_copilot.RoguelikeAICopilot(
            config=config,
            maa_dir=eff_maa_dir,
            base_dir=eff_base,
            collab_handler=collab_handler,
            cancel_event=eff_cancel,
            manual_resume_event=manual_resume_event,
            log_callback=_log,
        )

        def _on_tick(_sess: MaaSession, batch_evs: List[Dict[str, Any]]) -> None:
            for ev in batch_evs:
                if ev.get("message") == "SubTaskExtraInfo":
                    det = ev.get("details", {}).get("details", {})
                    if isinstance(det, dict) and isinstance(det.get("floor"), int):
                        run_ctx.floor = det["floor"]

        tick_cb = _on_tick

    res = run_task_with_recovery(
        session_holder,
        "Roguelike",
        params,
        config,
        timeout_sec=timeout_sec,
        tick_callback=tick_cb,
    )

    if res.get("network_recovery_failed") or session_holder.get("network_recovery_failed"):
        return {
            "ok": False,
            "cancelled": False,
            "network_recovery_failed": True,
            "error": str(res.get("error") or session_holder.get("network_recovery_error") or "[NET][自愈失败] 集成战略期间网络掉线自愈失败"),
            "recovery_screenshot": str(res.get("recovery_screenshot") or session_holder.get("network_recovery_screenshot") or ""),
            "theme": theme,
            "mode_label": resolved["mode_label"],
            "params": params,
            "capabilities": caps,
            "metrics": extract_roguelike_metrics(res),
        }

    # Mount 2: Self-healing recovery loop (up to max_recovery_retries=3) when Roguelike fails at
    # initial / stage-selection / strategy-change SubTaskError (e.g. Sarkaz@Roguelike@Begin,
    # Sarkaz@Roguelike@StrategyChange, Sarkaz@Roguelike@Stages).
    # Scans & closes newly popped mechanism tutorial overlays AND escapes Sarkaz '思绪-构想 / 思维负荷' subpanels.
    max_rec = max(0, int(max_recovery_retries))
    for attempt in range(1, max_rec + 1):
        if (
            res.get("ok")
            or res.get("cancelled")
            or res.get("network_recovery_failed")
            or session_holder.get("network_recovery_failed")
            or (eff_cancel and eff_cancel.is_set())
            or not _is_roguelike_begin_error(res)
        ):
            break

        active_sess = session_holder["session"]
        adb_p = getattr(active_sess, "adb_path", "") or ""
        adb_a = getattr(active_sess, "adb_addr", "") or ""
        if not (isinstance(adb_p, str) and adb_p and isinstance(adb_a, str) and adb_a):
            break

        _log(
            f"[ROGUELIKE] 检测到可自愈 SubTaskError（主题={theme}，尝试 {attempt}/{max_rec}），"
            f"正在扫描教程弹窗并检查是否滞留在子界面（如萨卡兹思绪-构想）..."
        )
        guide_res = stage_pilot.dismiss_guide_popups(
            adb_p,
            adb_a,
            maa_dir=eff_maa_dir,
            cancel_event=eff_cancel,
            log_callback=_log,
        )
        dismissed_now = int(guide_res.get("dismissed_count", 0) or 0)
        guide_popups_dismissed += dismissed_now
        if guide_res.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
            res = dict(res)
            res["cancelled"] = True
            break

        escape_res = stage_pilot.escape_roguelike_subpanel(
            adb_p,
            adb_a,
            maa_dir=eff_maa_dir,
            cancel_event=eff_cancel,
            log_callback=_log,
        )
        escaped_now = bool(escape_res.get("escaped", False))
        extra_dismissed = int(escape_res.get("guide_popups_dismissed", 0) or 0)
        guide_popups_dismissed += extra_dismissed
        dismissed_total_step = dismissed_now + extra_dismissed
        if escaped_now:
            subpanel_escapes += 1

        if escape_res.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
            res = dict(res)
            res["cancelled"] = True
            break

        if dismissed_total_step > 0 or escaped_now:
            recovery_retries += 1
            guide_retry_triggered = True
            _log(
                f"[ROGUELIKE] 自愈恢复完成（关闭教程弹窗={dismissed_total_step}，子界面脱困={escaped_now}），"
                f"开始第 {recovery_retries}/{max_rec} 次自动重试集成战略 (Roguelike) 任务..."
            )
            res = run_task_with_recovery(
                session_holder,
                "Roguelike",
                params,
                config,
                timeout_sec=timeout_sec,
                tick_callback=tick_cb,
            )
        else:
            break

    metrics = extract_roguelike_metrics(res)
    boss_decision_dict: Optional[Dict[str, Any]] = None

    # Decision Point 4 handoff if MAA paused at Final Boss in hybrid AI mode
    if (
        ai_enabled
        and copilot is not None
        and metrics.get("stopped_at_final_boss")
        and not metrics.get("cancelled")
        and not (eff_cancel and eff_cancel.is_set())
    ):
        boss_dec = copilot.decide_boss_strategy(
            boss_stage_name=boss_stage_hint or f"{theme}_Boss_5F",
            run_context=run_ctx,
        )
        boss_decision_dict = boss_dec.to_dict()
        if boss_dec.action == "prts_copilot" and boss_dec.extra.get("copilot_path"):
            _log(f"[ROGUELIKE] Boss 战前交由适配 PRTS 作业接管: {boss_dec.extra['copilot_path']}")
            run_task_with_recovery(
                session_holder,
                "Copilot",
                {
                    "filename": str(boss_dec.extra["copilot_path"]),
                    "formation": True,
                    "add_trust": False,
                    "ignore_requirements": True,
                },
                config,
                timeout_sec=900,
            )
        elif boss_dec.action == "maa_native_battle" and not user_forced_stop_boss:
            _log("[ROGUELIKE] 无适配 PRTS Boss 作业，交回 MAA 肉鸽原生战斗引擎继续通关...")
            resume_params = dict(params)
            resume_params["stop_at_final_boss"] = False
            res_resume = run_task_with_recovery(
                session_holder, "Roguelike", resume_params, config, timeout_sec=timeout_sec
            )
            metrics["boss_resume"] = extract_roguelike_metrics(res_resume)
            metrics["ok"] = bool(res_resume.get("ok", False))
            metrics["cancelled"] = bool(res_resume.get("cancelled", False))

    # Post-settlement reward claiming & tech-tree auto-upgrade
    post_settlement: Dict[str, Any] = {"skipped": True}
    if (
        not metrics.get("cancelled")
        and not (eff_cancel and eff_cancel.is_set())
        and (resolved["auto_claim_rewards"] or resolved["auto_upgrade_tech_tree"])
    ):
        active_sess = session_holder["session"]
        adb_path = getattr(active_sess, "adb_path", "") or ""
        adb_addr = getattr(active_sess, "adb_addr", "") or ""
        if eff_maa_dir is not None and adb_path and adb_addr:
            post_cfg = copy.deepcopy(config)
            post_cfg.setdefault("roguelike", {})
            post_cfg["roguelike"]["auto_claim_rewards"] = resolved["auto_claim_rewards"]
            post_cfg["roguelike"]["auto_upgrade_tech_tree"] = resolved["auto_upgrade_tech_tree"]
            post_settlement = roguelike_ai_copilot.run_roguelike_post_settlement(
                adb_path=adb_path,
                adb_addr=adb_addr,
                maa_dir=eff_maa_dir,
                config=post_cfg,
                theme=theme,
                capabilities=caps,
                cancel_event=eff_cancel,
                log_callback=_log,
            )

    return {
        "ok": bool(metrics["ok"]) and not bool(metrics["cancelled"]),
        "cancelled": bool(metrics["cancelled"]) or bool(eff_cancel and eff_cancel.is_set()),
        "theme": theme,
        "mode_label": resolved["mode_label"],
        "params": params,
        "loaded_from_gui_config": resolved["loaded_from_gui_config"],
        "capabilities": caps,
        "metrics": metrics,
        "guide_popups_dismissed": guide_popups_dismissed,
        "guide_retry_triggered": guide_retry_triggered,
        "recovery_retries": recovery_retries,
        "subpanel_escapes": subpanel_escapes,
        "ai_copilot_enabled": ai_enabled,
        "boss_decision": boss_decision_dict,
        "ai_decisions": list(copilot.decision_history) if copilot is not None else [],
        "post_settlement": post_settlement,
    }


def run_reclamation_task(
    session_holder: Dict[str, MaaSession],
    config: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    skip_entry_navigation: bool = False,
    max_entry_retries: int = 2,
) -> Dict[str, Any]:
    """Execute native MAA Reclamation (生息演算) task with residual-state cleanup, entry navigation & self-healing retry."""
    cur_sess = session_holder["session"]
    eff_maa_dir = Path(maa_dir).resolve() if maa_dir is not None else getattr(cur_sess, "maa_dir", None)
    eff_cancel = cancel_event or getattr(cur_sess, "cancel_event", None)

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
                return
            except Exception:
                pass
        if hasattr(cur_sess, "_emit_log"):
            cur_sess._emit_log(msg)

    caps = inspect_maa_exploration_capabilities(eff_maa_dir)
    resolved = resolve_reclamation_settings(config=config, maa_dir=eff_maa_dir, overrides=overrides)
    params = resolved["params"]
    theme = resolved["theme"]
    stage_code = str(resolved.get("stage_code") or "")

    def _cancelled_result(entry_nav: Optional[Dict[str, Any]] = None, cleanup_hist: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        out_c: Dict[str, Any] = {
            "ok": False,
            "cancelled": True,
            "theme": theme,
            "mode_label": resolved["mode_label"],
            "mode_name": resolved["mode_name"],
            "stage_code": stage_code,
            "params": params,
            "capabilities": caps,
        }
        if entry_nav is not None:
            out_c["entry_navigation"] = entry_nav
        if cleanup_hist is not None:
            out_c["cleanup_history"] = cleanup_hist
        return out_c

    if eff_cancel and eff_cancel.is_set():
        return _cancelled_result()

    if resolved.get("mode_warning"):
        _log(f"[WARN][RECLAMATION] {resolved['mode_warning']}")

    theme_cn = "重启锚点" if theme == "RelaunchAnchor" else ("沙洲遗闻" if theme == "Tales" else theme)
    stage_desc = f"{stage_code} 关卡循环 ({resolved['mode_name']})" if stage_code else str(resolved["mode_label"])
    _log(
        f"[RECLAMATION] 启动生息演算任务 | MAA任务名=Reclamation | 主题={theme_cn}({theme}) | "
        f"目标={stage_desc} [mode={params['mode']}] | 搬空商店={params.get('clear_store')} | "
        f"继承gui.new.json={resolved['loaded_from_gui_config']}"
    )

    cleanup_history: List[Dict[str, Any]] = []
    guide_popups_dismissed = 0

    def _get_adb() -> Tuple[str, str]:
        s = session_holder.get("session") or cur_sess
        ap = getattr(s, "adb_path", "") or ""
        aa = getattr(s, "adb_addr", "") or ""
        if isinstance(ap, str) and ap and isinstance(aa, str) and aa:
            return ap, aa
        return "", ""

    # Pre-handoff cleanup: dismiss guide popups & clear any residual battle/settlement/drawer state
    adb_p, adb_a = _get_adb()
    if eff_maa_dir is not None and adb_p and adb_a:
        pre_guide = stage_pilot.dismiss_guide_popups(
            adb_p,
            adb_a,
            maa_dir=eff_maa_dir,
            cancel_event=eff_cancel,
            log_callback=_log,
        )
        guide_popups_dismissed += int(pre_guide.get("dismissed_count", 0) or 0)
        if pre_guide.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
            return _cancelled_result(cleanup_hist=cleanup_history)

        if theme == "RelaunchAnchor":
            pre_cleanup = stage_pilot.cleanup_reclamation_residual_state(
                adb_p,
                adb_a,
                eff_maa_dir,
                target_stage=stage_code or "RA-1",
                reset_drawer=False,
                cancel_event=eff_cancel,
                log_callback=_log,
            )
            if pre_cleanup.get("cleaned") or pre_cleanup.get("actions"):
                cleanup_history.append({"phase": "pre_handoff", **pre_cleanup})
            if pre_cleanup.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
                return _cancelled_result(cleanup_hist=cleanup_history)

    entry_navigation: Dict[str, Any] = {"skipped": True}
    ra_caps = (caps.get("reclamation_theme_capabilities") or {}).get(theme, {})
    if (
        theme == "RelaunchAnchor"
        and not skip_entry_navigation
        and ra_caps.get("requires_stage_pilot_entry", False)
    ):
        maa_ver = caps.get("maa_version") or "当前版本"
        _log(
            f"[RECLAMATION] 提示：MAA ({maa_ver}) 的《重启锚点》原生资源模板（Reclamation3.json）仅覆盖 "
            f"{stage_code or 'RA-1'} 关卡大地图/「开启建设」起跑点，尚未内置主界面自动入口；"
            f"将先由 stage_pilot 自动核查并执行「终端 -> 长期探索 -> 生息演算（重启锚点）」入口导航。"
        )
        adb_p, adb_a = _get_adb()
        if eff_maa_dir is not None and adb_p and adb_a:
            entry_navigation = stage_pilot.navigate_to_reclamation_relaunch_anchor(
                adb_path=adb_p,
                adb_addr=adb_a,
                maa_dir=eff_maa_dir,
                target_stage=stage_code or "RA-1",
                cancel_event=eff_cancel,
                log_callback=_log,
            )
            if entry_navigation.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
                return _cancelled_result(entry_nav=entry_navigation, cleanup_hist=cleanup_history)
            if entry_navigation.get("ok"):
                # Scan once more for guide popup that may appear right after entering the theme/map
                if not entry_navigation.get("already_in_ra") and int(entry_navigation.get("steps", 0) or 0) > 0:
                    post_nav_guide = stage_pilot.dismiss_guide_popups(
                        adb_p,
                        adb_a,
                        maa_dir=eff_maa_dir,
                        cancel_event=eff_cancel,
                        log_callback=_log,
                    )
                    guide_popups_dismissed += int(post_nav_guide.get("dismissed_count", 0) or 0)
                    if post_nav_guide.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
                        return _cancelled_result(entry_nav=entry_navigation, cleanup_hist=cleanup_history)
                _log(f"[RECLAMATION] 入口就绪：{entry_navigation.get('reason', '')}，交由 MAA 原生 Reclamation 任务接管循环...")
            elif entry_navigation.get("reason"):
                _log(f"[WARN][RECLAMATION] {entry_navigation['reason']}")

    eff_max_retries = max(0, int(max_entry_retries))
    entry_retries_used = 0
    res: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}

    for attempt_idx in range(eff_max_retries + 1):
        if eff_cancel and eff_cancel.is_set():
            return _cancelled_result(entry_nav=entry_navigation, cleanup_hist=cleanup_history)

        res = run_task_with_recovery(
            session_holder,
            "Reclamation",
            params,
            config,
            timeout_sec=resolved["timeout_sec"],
        )
        metrics = extract_reclamation_metrics(res)
        if res.get("network_recovery_failed") or session_holder.get("network_recovery_failed"):
            return {
                "ok": False,
                "cancelled": False,
                "network_recovery_failed": True,
                "error": str(res.get("error") or session_holder.get("network_recovery_error") or "[NET][自愈失败] 生息演算期间网络掉线自愈失败"),
                "recovery_screenshot": str(res.get("recovery_screenshot") or session_holder.get("network_recovery_screenshot") or ""),
                "theme": theme,
                "mode_label": resolved["mode_label"],
                "mode_name": resolved["mode_name"],
                "stage_code": stage_code,
                "params": params,
                "capabilities": caps,
                "entry_navigation": entry_navigation,
                "entry_retries": entry_retries_used,
                "cleanup_history": cleanup_history,
                "guide_popups_dismissed": guide_popups_dismissed,
                "metrics": metrics,
            }
        if metrics.get("ok") or metrics.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
            break

        adb_p, adb_a = _get_adb()
        if (
            attempt_idx < eff_max_retries
            and _is_reclamation_entry_error(res, metrics)
            and eff_maa_dir is not None
            and adb_p
            and adb_a
        ):
            entry_retries_used = attempt_idx + 1
            _log(
                f"[RECLAMATION] 检测到入口识别失败（如 RelaunchAnchor@MapRA1-Entry / RA1-Entry），"
                f"启动第 {entry_retries_used}/{eff_max_retries} 次残留任务/抽屉状态清理与自愈重试..."
            )
            g_retry = stage_pilot.dismiss_guide_popups(
                adb_p,
                adb_a,
                maa_dir=eff_maa_dir,
                cancel_event=eff_cancel,
                log_callback=_log,
            )
            guide_popups_dismissed += int(g_retry.get("dismissed_count", 0) or 0)
            if g_retry.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
                return _cancelled_result(entry_nav=entry_navigation, cleanup_hist=cleanup_history)

            cln = stage_pilot.cleanup_reclamation_residual_state(
                adb_p,
                adb_a,
                eff_maa_dir,
                target_stage=stage_code or "RA-1",
                reset_drawer=True,
                cancel_event=eff_cancel,
                log_callback=_log,
            )
            cleanup_history.append({"phase": f"entry_retry_{entry_retries_used}", **cln})
            if cln.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
                return _cancelled_result(entry_nav=entry_navigation, cleanup_hist=cleanup_history)

            if (
                theme == "RelaunchAnchor"
                and ra_caps.get("requires_stage_pilot_entry", False)
                and cln.get("final_screen") not in ("relaunch_anchor_map", "relaunch_anchor_ready")
            ):
                nav_retry = stage_pilot.navigate_to_reclamation_relaunch_anchor(
                    adb_path=adb_p,
                    adb_addr=adb_a,
                    maa_dir=eff_maa_dir,
                    target_stage=stage_code or "RA-1",
                    cancel_event=eff_cancel,
                    log_callback=_log,
                )
                entry_navigation = nav_retry
                if nav_retry.get("cancelled") or (eff_cancel and eff_cancel.is_set()):
                    return _cancelled_result(entry_nav=entry_navigation, cleanup_hist=cleanup_history)
            continue
        break

    return {
        "ok": bool(metrics.get("ok")) and not bool(metrics.get("cancelled")),
        "cancelled": bool(metrics.get("cancelled")) or bool(eff_cancel and eff_cancel.is_set()),
        "theme": theme,
        "mode_label": resolved["mode_label"],
        "mode_name": resolved["mode_name"],
        "stage_code": stage_code,
        "params": params,
        "loaded_from_gui_config": resolved["loaded_from_gui_config"],
        "capabilities": caps,
        "entry_navigation": entry_navigation,
        "entry_retries": entry_retries_used,
        "cleanup_history": cleanup_history,
        "guide_popups_dismissed": guide_popups_dismissed,
        "metrics": metrics,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """Direct CLI entrypoint supporting --mode roguelike / --mode reclamation via master_scheduler."""
    from . import master_scheduler

    return master_scheduler.main(argv)


if __name__ == "__main__":
    sys.exit(main())
