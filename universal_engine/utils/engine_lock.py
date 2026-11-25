"""Cross-process single-instance mutex lock for MAA Universal Engine (GUI + CLI)."""

from __future__ import annotations

import atexit
import ctypes
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class EngineLockError(RuntimeError):
    """Raised when another active task already holds the global engine lock."""

    def __init__(self, message: str, lock_info: Optional[Dict[str, Any]] = None, lock_path: Optional[Path] = None):
        super().__init__(message)
        self.lock_info: Dict[str, Any] = dict(lock_info or {})
        self.lock_path: Optional[Path] = Path(lock_path) if lock_path is not None else None


_INPROC_MUTEX = threading.RLock()
# Maps normalized lock path string -> {"token": str, "thread_id": int, "depth": int, "info": dict, "path": Path}
_INPROC_HELD: Dict[str, Dict[str, Any]] = {}


def is_pid_alive(pid: int) -> bool:
    """Return True if OS process `pid` is currently alive (Windows kernel32 + POSIX fallback)."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if pid_int == os.getpid():
        return True

    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
        if not handle:
            err = ctypes.get_last_error()
            return err == ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return int(exit_code.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def format_lock_conflict_message(lock_info: Dict[str, Any]) -> str:
    """Format a clear user-facing lock rejection message."""
    pid = lock_info.get("pid", "?")
    mode = str(lock_info.get("mode") or lock_info.get("task") or "unknown")
    source = str(lock_info.get("source") or "").strip()
    if source:
        return f"[LOCK] 检测到已有任务正在运行 (PID={pid}, mode={mode}, source={source})，请先停止现有任务后再启动"
    return f"[LOCK] 检测到已有任务正在运行 (PID={pid}, mode={mode})，请先停止现有任务后再启动"


def resolve_engine_lock_paths(base_dir: Path, maa_dir: Optional[Path] = None) -> List[Path]:
    """Return deterministic, deduplicated config/engine.lock paths covering both GUI & CLI roots.

    When running from source (`<maa_dir>/universal_engine`) vs frozen GUI (`<maa_dir>/Arknights_AI_Launcher.exe`),
    both processes share `<maa_dir>/config/engine.lock`, ensuring GUI and CLI/Agent strictly exclude each other.
    """
    candidates: List[Path] = []
    base_res = Path(base_dir).resolve()
    candidates.append(base_res / "config" / "engine.lock")

    if maa_dir is not None:
        try:
            maa_res = Path(maa_dir).resolve()
            if maa_res.is_dir() and maa_res != base_res:
                candidates.append(maa_res / "config" / "engine.lock")
        except Exception:
            pass
    if base_res.name.lower() == "universal_engine":
        parent = base_res.parent
        if (parent / "MaaCore.dll").is_file():
            candidates.append(parent / "config" / "engine.lock")

    dedup: Dict[str, Path] = {}
    for p in candidates:
        key = os.path.normcase(os.path.abspath(str(p)))
        dedup[key] = p
    return [dedup[k] for k in sorted(dedup.keys())]


def _read_lock_file(path: Path) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Read lock JSON. Returns (parsed_dict_or_none, file_exists)."""
    if not path.exists():
        return None, False
    for attempt in range(2):
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                # Another process may have just created the file via O_EXCL and is writing payload.
                if attempt == 0:
                    try:
                        age = time.time() - path.stat().st_mtime
                    except OSError:
                        age = 999.0
                    if age < 0.35:
                        time.sleep(0.05)
                        continue
                return None, True
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed, True
            return None, True
        except FileNotFoundError:
            return None, False
        except Exception:
            if attempt == 0:
                time.sleep(0.04)
                continue
            return None, True
    return None, path.exists()


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def inspect_engine_lock(
    base_dir: Path,
    maa_dir: Optional[Path] = None,
    clean_stale: bool = True,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Inspect whether another thread or live OS process currently holds an engine lock.

    Automatically cleans up stale locks whose PID is no longer alive when `clean_stale=True`.
    Returns active lock info dict (or None if free / held by current thread).
    """
    cur_tid = threading.get_ident()
    cur_pid = os.getpid()
    lock_paths = resolve_engine_lock_paths(base_dir, maa_dir=maa_dir)

    with _INPROC_MUTEX:
        for lp in lock_paths:
            key = os.path.normcase(os.path.abspath(str(lp)))
            held = _INPROC_HELD.get(key)
            if held is not None:
                if held["thread_id"] != cur_tid:
                    out = dict(held["info"])
                    out["lock_path"] = str(lp)
                    return out
                continue

            data, exists = _read_lock_file(lp)
            if not exists:
                continue

            if data is None:
                if clean_stale:
                    _safe_unlink(lp)
                continue

            pid = data.get("pid")
            alive = is_pid_alive(pid) if pid is not None else False
            if alive:
                # If same PID but NOT in _INPROC_HELD, it's a leftover lock from a previous crash/PID reuse
                # or a foreign file written in a test simulating current PID -> treat as live if not in-process owned
                # unless it's genuinely an active external lock.
                if int(pid) == cur_pid and key in _INPROC_HELD and _INPROC_HELD[key]["thread_id"] == cur_tid:
                    continue
                out = dict(data)
                out["lock_path"] = str(lp)
                return out

            if clean_stale:
                if log_callback:
                    try:
                        log_callback(
                            f"[LOCK] 检测到失效残留锁 (PID={pid}, mode={data.get('mode', '?')})，进程已退出，自动清理死锁文件。"
                        )
                    except Exception:
                        pass
                _safe_unlink(lp)

    return None


class EngineTaskLock:
    """Cross-process & cross-thread single-instance task mutex backed by config/engine.lock."""

    def __init__(
        self,
        base_dir: Path,
        mode: str,
        source: str = "cli",
        task: str = "",
        maa_dir: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.base_dir = Path(base_dir).resolve()
        self.maa_dir = Path(maa_dir).resolve() if maa_dir is not None else None
        self.mode = str(mode or "task").strip()
        self.source = str(source or "cli").strip().lower()
        self.task = str(task or self.mode).strip()
        self.log_callback = log_callback
        self.token = uuid.uuid4().hex
        self.lock_paths: List[Path] = resolve_engine_lock_paths(self.base_dir, maa_dir=self.maa_dir)
        self._acquired_keys: List[str] = []
        self._reentrant_keys: List[str] = []
        self._acquired = False
        self.payload: Dict[str, Any] = {
            "pid": os.getpid(),
            "source": self.source,
            "mode": self.mode,
            "task": self.task,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "timestamp": round(time.time(), 3),
            "token": self.token,
        }

    def _emit(self, msg: str) -> None:
        if self.log_callback:
            try:
                self.log_callback(msg)
            except Exception:
                pass

    def acquire(self) -> "EngineTaskLock":
        if self._acquired:
            return self
        cur_tid = threading.get_ident()
        newly_acquired: List[Tuple[str, Path]] = []
        reentered: List[str] = []

        with _INPROC_MUTEX:
            try:
                for lp in self.lock_paths:
                    key = os.path.normcase(os.path.abspath(str(lp)))
                    held = _INPROC_HELD.get(key)
                    if held is not None:
                        if held["thread_id"] == cur_tid:
                            held["depth"] += 1
                            reentered.append(key)
                            continue
                        conflict_info = dict(held["info"])
                        raise EngineLockError(format_lock_conflict_message(conflict_info), conflict_info, lp)

                    lp.parent.mkdir(parents=True, exist_ok=True)
                    acquired_file = False
                    last_conflict: Optional[Dict[str, Any]] = None

                    for _attempt in range(3):
                        try:
                            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                            try:
                                raw = json.dumps(self.payload, ensure_ascii=False, indent=2).encode("utf-8")
                                os.write(fd, raw)
                            finally:
                                os.close(fd)
                            acquired_file = True
                            break
                        except FileExistsError:
                            existing, exists = _read_lock_file(lp)
                            if not exists:
                                continue
                            if existing is None:
                                self._emit(f"[LOCK] 检测到损坏的空锁文件 ({lp})，自动清理并重试获取锁。")
                                _safe_unlink(lp)
                                continue
                            pid = existing.get("pid")
                            if pid is not None and is_pid_alive(pid):
                                last_conflict = dict(existing)
                                break
                            self._emit(
                                f"[LOCK] 检测到已崩溃进程的残留死锁 (PID={pid}, mode={existing.get('mode', '?')})，自动清理后继续启动。"
                            )
                            _safe_unlink(lp)
                            continue

                    if not acquired_file:
                        info = last_conflict or {"pid": "?", "mode": "unknown", "source": ""}
                        raise EngineLockError(format_lock_conflict_message(info), info, lp)

                    _INPROC_HELD[key] = {
                        "token": self.token,
                        "thread_id": cur_tid,
                        "depth": 1,
                        "info": dict(self.payload),
                        "path": lp,
                    }
                    newly_acquired.append((key, lp))

                self._acquired_keys = [k for k, _ in newly_acquired]
                self._reentrant_keys = reentered
                self._acquired = True
                return self
            except Exception:
                # Roll back any partially acquired lock files or re-entrant depths on failure
                for k in reentered:
                    h = _INPROC_HELD.get(k)
                    if h is not None and h["depth"] > 1:
                        h["depth"] -= 1
                for k, lp in reversed(newly_acquired):
                    _INPROC_HELD.pop(k, None)
                    _unlink_if_owned(lp, self.token)
                raise

    def release(self) -> None:
        if not self._acquired:
            return
        with _INPROC_MUTEX:
            for k in reversed(self._reentrant_keys):
                h = _INPROC_HELD.get(k)
                if h is not None and h["depth"] > 1:
                    h["depth"] -= 1
            for k in reversed(self._acquired_keys):
                h = _INPROC_HELD.get(k)
                if h is not None and h.get("token") == self.token:
                    h["depth"] -= 1
                    if h["depth"] <= 0:
                        lp = h["path"]
                        _INPROC_HELD.pop(k, None)
                        _unlink_if_owned(lp, self.token)
            self._acquired_keys = []
            self._reentrant_keys = []
            self._acquired = False

    def __enter__(self) -> "EngineTaskLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


def _unlink_if_owned(path: Path, token: str) -> None:
    data, exists = _read_lock_file(path)
    if not exists:
        return
    if data is None or data.get("token") == token:
        _safe_unlink(path)


def _cleanup_all_process_locks() -> None:
    with _INPROC_MUTEX:
        for key, entry in list(_INPROC_HELD.items()):
            _unlink_if_owned(entry["path"], entry["token"])
            _INPROC_HELD.pop(key, None)


atexit.register(_cleanup_all_process_locks)
