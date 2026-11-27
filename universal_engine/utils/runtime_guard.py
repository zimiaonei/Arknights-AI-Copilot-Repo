"""Runtime Dry-Run Guard and ADB Transport Hook Registry.

Provides thread-safe, zero-GUI-dependency hooks for:
1. Intercepting ADB screencap/tap/swipe primitives in `stage_pilot` (used by both
   FakeAdbReplayHarness and global Dry-Run mode).
2. Intercepting `Asst.append_task`, post-actions, game-client restarts, and webhooks
   during Dry-Run without touching production business algorithms or persisting state.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union


class DryRunBudgetExceeded(Exception):
    """Raised when a Dry-Run context with raise_on_budget=True hits max_actions."""


DEFAULT_DRY_RUN_OUT_DIR = Path("reports") / "replay_debug"

_PROCESS_DRY_RUN_LOCK = threading.RLock()
_PROCESS_DRY_RUN_ENABLED: bool = False
_PROCESS_DRY_RUN_CTX: Optional["DryRunContext"] = None

_LOCAL = threading.local()
_GLOBAL_LAST_IMAGE: Optional[Any] = None
_GLOBAL_ADB_HOOKS: Optional["AdbTransportHooks"] = None
_OVERLAY_RENDERER: Optional[Callable[..., Any]] = None


def _norm_to_device_xy(x_1280: float, y_720: float, width: int, height: int) -> Tuple[int, int]:
    w = max(1, int(width))
    h = max(1, int(height))
    dx = max(0, min(w - 1, int(round(float(x_1280) * w / 1280.0))))
    dy = max(0, min(h - 1, int(round(float(y_720) * h / 720.0))))
    return dx, dy


def _env_dry_run_enabled() -> bool:
    raw = os.environ.get("MAA_DRY_RUN", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _get_dry_run_stack() -> List["DryRunContext"]:
    stack = getattr(_LOCAL, "dry_run_stack", None)
    if stack is None:
        stack = []
        _LOCAL.dry_run_stack = stack
    return stack


def _get_adb_hooks_stack() -> List[Optional["AdbTransportHooks"]]:
    stack = getattr(_LOCAL, "adb_hooks_stack", None)
    if stack is None:
        stack = []
        _LOCAL.adb_hooks_stack = stack
    return stack


class AdbTransportHooks:
    """Base hook interface for intercepting low-level ADB primitives in stage_pilot."""

    def screencap(
        self,
        adb_path: str,
        adb_addr: str,
        timeout: float = 5.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> Optional[Any]:
        return None

    def tap(
        self,
        adb_path: str,
        adb_addr: str,
        x_1280: float,
        y_720: float,
        width: int = 1280,
        height: int = 720,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        return False

    def swipe(
        self,
        adb_path: str,
        adb_addr: str,
        x1_1280: float,
        y1_720: float,
        x2_1280: float,
        y2_720: float,
        duration_ms: int = 350,
        width: int = 1280,
        height: int = 720,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        return False


def register_overlay_renderer(fn: Optional[Callable[..., Any]]) -> None:
    """Register rich Pillow overlay renderer (typically from visual_replay.py)."""
    global _OVERLAY_RENDERER
    _OVERLAY_RENDERER = fn


def set_last_captured_image(img: Optional[Any]) -> None:
    """Cache latest captured RGB PIL Image in thread-local and process fallback slots."""
    global _GLOBAL_LAST_IMAGE
    if img is None:
        return
    try:
        copied = img.copy() if hasattr(img, "copy") else img
    except Exception:
        copied = img
    _LOCAL.last_captured_image = copied
    with _PROCESS_DRY_RUN_LOCK:
        _GLOBAL_LAST_IMAGE = copied


def get_last_captured_image() -> Optional[Any]:
    """Retrieve latest cached screenshot from thread-local or process fallback."""
    img = getattr(_LOCAL, "last_captured_image", None)
    if img is not None:
        return img
    with _PROCESS_DRY_RUN_LOCK:
        return _GLOBAL_LAST_IMAGE


def set_adb_hooks(hooks: Optional[AdbTransportHooks]) -> None:
    """Set process-wide fallback ADB transport hooks (or clear with None)."""
    global _GLOBAL_ADB_HOOKS
    with _PROCESS_DRY_RUN_LOCK:
        _GLOBAL_ADB_HOOKS = hooks


def get_adb_hooks() -> Optional[AdbTransportHooks]:
    """Return active ADB transport hooks for current thread or process fallback."""
    stack = _get_adb_hooks_stack()
    if stack:
        return stack[-1]
    with _PROCESS_DRY_RUN_LOCK:
        return _GLOBAL_ADB_HOOKS


@contextmanager
def adb_hooks_scope(hooks: Optional[AdbTransportHooks]) -> Iterator[Optional[AdbTransportHooks]]:
    """Temporarily install AdbTransportHooks for current thread and spawned workers."""
    global _GLOBAL_ADB_HOOKS
    stack = _get_adb_hooks_stack()
    with _PROCESS_DRY_RUN_LOCK:
        prev_global = _GLOBAL_ADB_HOOKS
        _GLOBAL_ADB_HOOKS = hooks
    stack.append(hooks)
    try:
        yield hooks
    finally:
        stack.pop()
        with _PROCESS_DRY_RUN_LOCK:
            _GLOBAL_ADB_HOOKS = prev_global


def _load_fallback_font(size: int = 16) -> Any:
    from PIL import ImageFont

    candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for fp in candidates:
        if os.path.isfile(fp):
            try:
                return ImageFont.truetype(fp, size=size)
            except Exception:
                continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _render_builtin_overlay(
    img: Optional[Any],
    annotations: Dict[str, Any],
    title: str = "",
) -> Any:
    """Lightweight fallback 1280x720 Pillow annotation drawer."""
    from PIL import Image, ImageDraw

    if img is None:
        canvas = Image.new("RGBA", (1280, 720), (24, 28, 36, 255))
    else:
        canvas = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("RGBA")

    overlay = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_fallback_font(16)
    font_sm = _load_fallback_font(13)

    for z in annotations.get("forbidden_zones", []) or []:
        bbox = z.get("bbox") if isinstance(z, dict) else None
        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            draw.rectangle([x1, y1, x2, y2], fill=(220, 48, 48, 45), outline=(235, 64, 64, 220), width=2)
            lbl = str(z.get("label") or "")
            if lbl and font_sm:
                draw.text((x1 + 4, y1 + 4), lbl, fill=(255, 210, 210, 255), font=font_sm)

    for b in annotations.get("filtered_boxes", []) or []:
        bbox = b.get("bbox") if isinstance(b, dict) else None
        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            draw.rectangle([x1, y1, x2, y2], outline=(160, 165, 175, 220), width=2)
            lbl = str(b.get("label") or "")
            if lbl and font_sm:
                draw.text((x1 + 2, max(0, y1 - 16)), lbl, fill=(220, 225, 230, 255), font=font_sm)

    for b in annotations.get("target_boxes", []) or []:
        bbox = b.get("bbox") if isinstance(b, dict) else None
        if bbox and len(bbox) == 4:
            x1, y1, x2, y2 = [int(v) for v in bbox]
            draw.rectangle([x1, y1, x2, y2], outline=(46, 204, 113, 255), width=3)
            lbl = str(b.get("label") or "")
            if lbl and font_sm:
                draw.text((x1 + 2, max(0, y1 - 18)), lbl, fill=(180, 255, 200, 255), font=font_sm)

    for t in annotations.get("taps", []) or []:
        cx = int(round(float(t.get("cx", 640))))
        cy = int(round(float(t.get("cy", 360))))
        r = 18
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 215, 0, 255), width=3)
        draw.line([(cx - 28, cy), (cx + 28, cy)], fill=(255, 80, 80, 255), width=2)
        draw.line([(cx, cy - 28), (cx, cy + 28)], fill=(255, 80, 80, 255), width=2)
        lbl = str(t.get("label") or f"({cx}, {cy})")
        if font_sm:
            draw.rectangle([cx + 20, cy - 12, min(1275, cx + 20 + len(lbl) * 10 + 12), cy + 12], fill=(20, 20, 24, 210))
            draw.text((cx + 25, cy - 9), lbl, fill=(255, 235, 130, 255), font=font_sm)

    for s in annotations.get("swipes", []) or []:
        x1 = int(round(float(s.get("x1", 0))))
        y1 = int(round(float(s.get("y1", 0))))
        x2 = int(round(float(s.get("x2", 0))))
        y2 = int(round(float(s.get("y2", 0))))
        draw.line([(x1, y1), (x2, y2)], fill=(0, 210, 255, 255), width=4)
        draw.ellipse([x1 - 7, y1 - 7, x1 + 7, y1 + 7], fill=(0, 210, 255, 255))
        draw.ellipse([x2 - 9, y2 - 9, x2 + 9, y2 + 9], outline=(255, 215, 0, 255), width=3)
        lbl = str(s.get("label") or f"SWIPE ({x1},{y1})->({x2},{y2})")
        if font_sm:
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            draw.rectangle([mx - 6, my - 22, min(1275, mx + len(lbl) * 9 + 12), my], fill=(20, 20, 24, 210))
            draw.text((mx, my - 19), lbl, fill=(140, 240, 255, 255), font=font_sm)

    lines = list(annotations.get("banner_lines", []) or [])
    if title:
        lines.insert(0, title)
    if lines:
        bar_h = min(220, 12 + 22 * len(lines))
        draw.rectangle([0, 0, 1280, bar_h], fill=(18, 22, 30, 205))
        for idx, line in enumerate(lines[:9]):
            if font:
                draw.text((16, 8 + idx * 22), str(line)[:120], fill=(255, 220, 120, 255) if idx == 0 else (235, 242, 248, 255), font=font)

    return Image.alpha_composite(canvas, overlay).convert("RGB")


class DryRunContext:
    """Thread-safe audit & interception context for global Dry-Run execution."""

    def __init__(
        self,
        enabled: bool = True,
        max_actions: int = 3,
        out_dir: Optional[Union[Path, str]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        base_dir: Optional[Union[Path, str]] = None,
        raise_on_budget: bool = False,
    ) -> None:
        self.enabled: bool = bool(enabled)
        self.max_actions: int = int(max_actions)
        if out_dir is not None:
            self.out_dir: Path = Path(out_dir)
        elif base_dir is not None:
            self.out_dir = Path(base_dir) / DEFAULT_DRY_RUN_OUT_DIR
        else:
            self.out_dir = DEFAULT_DRY_RUN_OUT_DIR
        self.log_callback: Optional[Callable[[str], None]] = log_callback
        self.base_dir: Optional[Path] = Path(base_dir) if base_dir is not None else None
        self.raise_on_budget: bool = bool(raise_on_budget)

        self._lock = threading.RLock()
        self.records: List[Dict[str, Any]] = []
        self.action_count: int = 0
        self.budget_exceeded: bool = False
        self._seq: int = 0
        self._session_tag: str = datetime.now().strftime("%Y%m%d_%H%M%S")

    def _emit(self, msg: str, log_callback: Optional[Callable[[str], None]] = None) -> None:
        cb = log_callback or self.log_callback
        if cb is not None:
            try:
                cb(msg)
                return
            except Exception:
                pass
        print(msg, flush=True)

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _save_overlay(
        self,
        img: Optional[Any],
        annotations: Dict[str, Any],
        title: str,
        filename_prefix: str,
    ) -> Optional[str]:
        if img is None and not annotations.get("allow_blank_canvas"):
            return None
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            renderer = _OVERLAY_RENDERER
            if callable(renderer):
                rendered = renderer(img, annotations, title=title)
            else:
                rendered = _render_builtin_overlay(img, annotations, title=title)
            seq = self._next_seq()
            safe_prefix = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in filename_prefix)
            out_path = self.out_dir / f"dryrun_{safe_prefix}_{self._session_tag}_{seq:03d}.png"
            rendered.save(str(out_path))
            return str(out_path)
        except Exception:
            return None

    def intercept_task(
        self,
        task_type: str,
        params: Dict[str, Any],
        *,
        adb_path: str = "",
        adb_addr: str = "",
        screenshot_img: Optional[Any] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        safe_params = copy.deepcopy(params if isinstance(params, dict) else {})
        pretty_json = json.dumps(safe_params, ensure_ascii=False, indent=2)
        self._emit(
            f"[DRY-RUN][TASK] 拦截 Asst.append_task: task_type={task_type}\n{pretty_json}",
            log_callback=log_callback,
        )

        img = screenshot_img if screenshot_img is not None else get_last_captured_image()
        summary_lines = [f"参数 JSON ({task_type}):"] + pretty_json.splitlines()[:6]
        overlay_path = self._save_overlay(
            img,
            {
                "banner_lines": summary_lines,
                "target_boxes": [],
                "filtered_boxes": [],
                "forbidden_zones": [],
                "taps": [],
                "swipes": [],
            },
            title=f"[DRY-RUN] 拟下发任务: {task_type}",
            filename_prefix=f"task_{task_type}",
        )

        rec = {
            "kind": "task",
            "ts": time.time(),
            "task_type": str(task_type),
            "params": safe_params,
            "adb_path": str(adb_path or ""),
            "adb_addr": str(adb_addr or ""),
            "overlay_path": overlay_path,
        }
        with self._lock:
            self.records.append(rec)
            self.dump_summary_json()

        return {
            "ok": True,
            "dry_run": True,
            "task_type": task_type,
            "task_id": -1,
            "params": safe_params,
            "overlay_path": overlay_path,
            "timed_out": False,
            "cancelled": False,
            "elapsed": 0.0,
            "events": [],
            "chain_errors": [],
            "sub_errors": [],
            "log_lines": [f"[DRY-RUN] 拦截任务 {task_type}"],
            "error_reason": "",
            "network_disconnect_detected": False,
            "network_disconnect_info": None,
        }

    def intercept_tap(
        self,
        x_1280: float,
        y_720: float,
        width: int = 1280,
        height: int = 720,
        *,
        caller: str = "",
        cancel_event: Optional[threading.Event] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        dx, dy = _norm_to_device_xy(x_1280, y_720, width, height)
        with self._lock:
            self.action_count += 1
            n = self.action_count

        img = get_last_captured_image()
        overlay_path = self._save_overlay(
            img,
            {
                "taps": [
                    {
                        "cx": float(x_1280),
                        "cy": float(y_720),
                        "label": f"TAP #{n} ({x_1280:.0f},{y_720:.0f})",
                    }
                ],
                "banner_lines": [f"拦截点击 #{n}: norm=({x_1280:.1f},{y_720:.1f}) dev=({dx},{dy}) caller={caller}"],
            },
            title=f"[DRY-RUN] 拟点击坐标 #{n}",
            filename_prefix=f"tap_{n}",
        )
        self._emit(
            f"[DRY-RUN][TAP #{n}] 拦截点击 norm=({float(x_1280):.1f},{float(y_720):.1f}) "
            f"dev=({dx},{dy}) caller={caller or '-'} overlay={overlay_path or 'none'}",
            log_callback=log_callback,
        )

        rec = {
            "kind": "tap",
            "index": n,
            "ts": time.time(),
            "x_1280": float(x_1280),
            "y_720": float(y_720),
            "dev_xy": [dx, dy],
            "width": int(width),
            "height": int(height),
            "caller": str(caller or ""),
            "overlay_path": overlay_path,
        }
        with self._lock:
            self.records.append(rec)
            hit_budget = self.max_actions > 0 and self.action_count >= self.max_actions
            if hit_budget:
                self.budget_exceeded = True
            self.dump_summary_json()

        if hit_budget:
            self._emit(
                f"[DRY-RUN] 已达演习动作上限 ({self.max_actions})，安全收敛停止",
                log_callback=log_callback,
            )
            if cancel_event is not None:
                try:
                    cancel_event.set()
                except Exception:
                    pass
            if self.raise_on_budget:
                raise DryRunBudgetExceeded(f"Dry-run budget reached ({self.max_actions})")
            return False
        return True

    def intercept_swipe(
        self,
        x1_1280: float,
        y1_720: float,
        x2_1280: float,
        y2_720: float,
        duration_ms: int = 350,
        width: int = 1280,
        height: int = 720,
        *,
        caller: str = "",
        cancel_event: Optional[threading.Event] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> bool:
        sx, sy = _norm_to_device_xy(x1_1280, y1_720, width, height)
        ex, ey = _norm_to_device_xy(x2_1280, y2_720, width, height)
        with self._lock:
            self.action_count += 1
            n = self.action_count

        img = get_last_captured_image()
        overlay_path = self._save_overlay(
            img,
            {
                "swipes": [
                    {
                        "x1": float(x1_1280),
                        "y1": float(y1_720),
                        "x2": float(x2_1280),
                        "y2": float(y2_720),
                        "label": f"SWIPE #{n} ({x1_1280:.0f},{y1_720:.0f})->({x2_1280:.0f},{y2_720:.0f})",
                    }
                ],
                "banner_lines": [
                    f"拦截滑动 #{n}: ({x1_1280:.1f},{y1_720:.1f})->({x2_1280:.1f},{y2_720:.1f}) "
                    f"dev=({sx},{sy})->({ex},{ey}) {duration_ms}ms caller={caller}"
                ],
            },
            title=f"[DRY-RUN] 拟滑动轨迹 #{n}",
            filename_prefix=f"swipe_{n}",
        )
        self._emit(
            f"[DRY-RUN][SWIPE #{n}] 拦截滑动 norm=({float(x1_1280):.1f},{float(y1_720):.1f})->"
            f"({float(x2_1280):.1f},{float(y2_720):.1f}) dev=({sx},{sy})->({ex},{ey}) "
            f"duration={int(duration_ms)}ms caller={caller or '-'} overlay={overlay_path or 'none'}",
            log_callback=log_callback,
        )

        rec = {
            "kind": "swipe",
            "index": n,
            "ts": time.time(),
            "start_1280": [float(x1_1280), float(y1_720)],
            "end_1280": [float(x2_1280), float(y2_720)],
            "dev_start": [sx, sy],
            "dev_end": [ex, ey],
            "duration_ms": int(duration_ms),
            "caller": str(caller or ""),
            "overlay_path": overlay_path,
        }
        with self._lock:
            self.records.append(rec)
            hit_budget = self.max_actions > 0 and self.action_count >= self.max_actions
            if hit_budget:
                self.budget_exceeded = True
            self.dump_summary_json()

        if hit_budget:
            self._emit(
                f"[DRY-RUN] 已达演习动作上限 ({self.max_actions})，安全收敛停止",
                log_callback=log_callback,
            )
            if cancel_event is not None:
                try:
                    cancel_event.set()
                except Exception:
                    pass
            if self.raise_on_budget:
                raise DryRunBudgetExceeded(f"Dry-run budget reached ({self.max_actions})")
            return False
        return True

    def intercept_webhook(
        self,
        url: str,
        title: str,
        content: str,
        event_type: str = "info",
        body: Optional[Dict[str, Any]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        safe_body = copy.deepcopy(body if isinstance(body, dict) else {})
        pretty_body = json.dumps(safe_body, ensure_ascii=False, indent=2)
        self._emit(
            f"[DRY-RUN][WEBHOOK] 拦截 Webhook 推送: title={title} event={event_type} url={url}\n{pretty_body}",
            log_callback=log_callback,
        )
        rec = {
            "kind": "webhook",
            "ts": time.time(),
            "url": str(url or ""),
            "title": str(title or ""),
            "content": str(content or ""),
            "event_type": str(event_type or "info"),
            "payload": safe_body,
        }
        with self._lock:
            self.records.append(rec)
            self.dump_summary_json()

        return {
            "sent": False,
            "dry_run": True,
            "skipped": "dry_run",
            "title": title,
            "event_type": event_type,
            "payload": safe_body,
        }

    def intercept_control(
        self,
        action: str,
        detail: str = "",
        command: Optional[List[str]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        cmd_list = list(command or [])
        self._emit(
            f"[DRY-RUN][BLOCKED] 拦截高危控制操作: {action} ({detail or '无额外说明'})",
            log_callback=log_callback,
        )
        rec = {
            "kind": "control",
            "ts": time.time(),
            "action": str(action),
            "detail": str(detail or ""),
            "command": cmd_list,
        }
        with self._lock:
            self.records.append(rec)
            self.dump_summary_json()

        return {
            "action": action,
            "executed": False,
            "dry_run": True,
            "command": cmd_list,
            "detail": detail or f"Dry-run 已拦截 {action}",
        }

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "max_actions": self.max_actions,
                "action_count": self.action_count,
                "budget_exceeded": self.budget_exceeded,
                "out_dir": str(self.out_dir),
                "records_count": len(self.records),
                "records": copy.deepcopy(self.records),
            }

    def dump_summary_json(self) -> Optional[Path]:
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out_file = self.out_dir / f"dryrun_summary_{self._session_tag}.json"
            out_file.write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
            return out_file
        except Exception:
            return None


def set_process_dry_run(
    enabled: bool,
    max_actions: int = 3,
    out_dir: Optional[Union[Path, str]] = None,
    base_dir: Optional[Union[Path, str]] = None,
) -> bool:
    """Toggle process-wide runtime Dry-Run flag in memory only (never persisted)."""
    global _PROCESS_DRY_RUN_ENABLED, _PROCESS_DRY_RUN_CTX
    with _PROCESS_DRY_RUN_LOCK:
        _PROCESS_DRY_RUN_ENABLED = bool(enabled)
        if _PROCESS_DRY_RUN_ENABLED:
            _PROCESS_DRY_RUN_CTX = DryRunContext(
                enabled=True,
                max_actions=max_actions,
                out_dir=out_dir,
                base_dir=base_dir,
            )
        else:
            _PROCESS_DRY_RUN_CTX = None
        return _PROCESS_DRY_RUN_ENABLED


def get_process_dry_run() -> bool:
    """Return process-wide in-memory Dry-Run toggle state."""
    with _PROCESS_DRY_RUN_LOCK:
        return bool(_PROCESS_DRY_RUN_ENABLED)


def is_dry_run_enabled() -> bool:
    """Return True when Dry-Run is active in current scope, process flag, or MAA_DRY_RUN env."""
    stack = _get_dry_run_stack()
    if stack:
        return bool(stack[-1].enabled)
    with _PROCESS_DRY_RUN_LOCK:
        if _PROCESS_DRY_RUN_ENABLED:
            return True
    return _env_dry_run_enabled()


def get_active_dry_run_context() -> Optional[DryRunContext]:
    """Return active DryRunContext if Dry-Run is currently enabled, else None."""
    global _PROCESS_DRY_RUN_CTX
    stack = _get_dry_run_stack()
    if stack:
        ctx = stack[-1]
        return ctx if ctx.enabled else None
    with _PROCESS_DRY_RUN_LOCK:
        if _PROCESS_DRY_RUN_ENABLED:
            if _PROCESS_DRY_RUN_CTX is None:
                _PROCESS_DRY_RUN_CTX = DryRunContext(enabled=True)
            return _PROCESS_DRY_RUN_CTX
        if _env_dry_run_enabled():
            if _PROCESS_DRY_RUN_CTX is None:
                _PROCESS_DRY_RUN_CTX = DryRunContext(enabled=True)
            return _PROCESS_DRY_RUN_CTX
    return None


@contextmanager
def dry_run_scope(
    enabled: bool = True,
    max_actions: int = 3,
    out_dir: Optional[Union[Path, str]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    base_dir: Optional[Union[Path, str]] = None,
    raise_on_budget: bool = False,
    process_wide: bool = False,
) -> Iterator[DryRunContext]:
    """Context manager enabling/disabling Dry-Run for current thread or process-wide workers."""
    global _PROCESS_DRY_RUN_ENABLED, _PROCESS_DRY_RUN_CTX
    ctx = DryRunContext(
        enabled=bool(enabled),
        max_actions=max_actions,
        out_dir=out_dir,
        log_callback=log_callback,
        base_dir=base_dir,
        raise_on_budget=raise_on_budget,
    )
    stack = _get_dry_run_stack()
    stack.append(ctx)
    prev_proc_enabled: Optional[bool] = None
    prev_proc_ctx: Optional[DryRunContext] = None
    if process_wide:
        with _PROCESS_DRY_RUN_LOCK:
            prev_proc_enabled = _PROCESS_DRY_RUN_ENABLED
            prev_proc_ctx = _PROCESS_DRY_RUN_CTX
            _PROCESS_DRY_RUN_ENABLED = bool(enabled)
            _PROCESS_DRY_RUN_CTX = ctx if enabled else None
    try:
        yield ctx
    finally:
        stack.pop()
        if process_wide:
            with _PROCESS_DRY_RUN_LOCK:
                _PROCESS_DRY_RUN_ENABLED = bool(prev_proc_enabled)
                _PROCESS_DRY_RUN_CTX = prev_proc_ctx
