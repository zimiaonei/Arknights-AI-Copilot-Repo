"""Fake ADB replay harness: feed recorded screenshots to stage_pilot and record taps/swipes.

Usage::

    harness = FakeAdbReplayHarness.from_frames([img1, img2])
    with harness.attach(fast_sleep=True):
        ...  # call real stage_pilot / roguelike_ai_copilot logic with any adb_path/adb_addr
    harness.assert_no_forbidden_taps([[0, 0, 100, 100]])
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from ..utils import runtime_guard

try:  # pragma: no cover - PIL is a hard dependency of the engine
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore


@dataclass
class ReplayFrame:
    """One recorded screen state. `image` is a PIL image or a path (loaded lazily)."""

    image: Any = None
    name: str = ""
    # Conditional routing: list of {"kind": "tap"|"swipe"|"any", "rect": [x1,y1,x2,y2], "next": idx}
    routes: List[Dict[str, Any]] = field(default_factory=list)
    # Linear progression override: frame index after any action (None => idx+1)
    next_index: Optional[int] = None

    def load(self) -> Any:
        img = self.image
        if isinstance(img, (str, Path)):
            with Image.open(str(img)) as im:
                img = im.convert("RGB")
            self.image = img
        elif img is not None and hasattr(img, "convert"):
            img = img.convert("RGB")
        return img


@dataclass
class RecordedAdbAction:
    kind: str  # "tap" | "swipe"
    index: int
    frame_index: int
    x: float = 0.0
    y: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    duration_ms: int = 0
    ts: float = 0.0

    @property
    def point(self) -> Tuple[float, float]:
        return (self.x, self.y)


def _point_in_rect(x: float, y: float, rect: Sequence[float]) -> bool:
    x1, y1, x2, y2 = [float(v) for v in rect]
    return min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2)


class FakeAdbReplayHarness(runtime_guard.AdbTransportHooks):
    def __init__(
        self,
        frames: Sequence[Union[ReplayFrame, Any]],
        *,
        stay_on_last: bool = True,
        strict_overflow: bool = False,
    ) -> None:
        self.frames: List[ReplayFrame] = [f if isinstance(f, ReplayFrame) else ReplayFrame(image=f) for f in frames]
        if not self.frames:
            raise ValueError("FakeAdbReplayHarness requires at least one frame")
        self.stay_on_last = bool(stay_on_last)
        self.strict_overflow = bool(strict_overflow)
        self.current_index: int = 0
        self.actions: List[RecordedAdbAction] = []
        self.screencap_count: int = 0
        self._lock = threading.RLock()
        self._out_dir: Optional[Path] = None

    # ---------------- constructors ----------------
    @classmethod
    def from_frames(cls, frames: Sequence[Any], *, stay_on_last: bool = True, strict_overflow: bool = False) -> "FakeAdbReplayHarness":
        return cls(frames, stay_on_last=stay_on_last, strict_overflow=strict_overflow)

    @classmethod
    def from_scenario_json(
        cls, json_path: Union[str, Path], *, stay_on_last: bool = True, strict_overflow: bool = False
    ) -> "FakeAdbReplayHarness":
        """Scenario JSON: {"frames": [{"image": "a.png", "name": "x", "next": 2,
        "routes": [{"kind": "tap", "rect": [x1,y1,x2,y2], "next": 1}]}, ...]}
        (a bare list of frames, or of image paths, is also accepted)."""
        jp = Path(json_path)
        data = json.loads(jp.read_text(encoding="utf-8"))
        raw_frames = data.get("frames", []) if isinstance(data, dict) else data
        base = jp.parent
        frames: List[ReplayFrame] = []
        for i, item in enumerate(raw_frames or []):
            if isinstance(item, str):
                item = {"image": item}
            img_ref = str(item.get("image") or item.get("path") or "")
            p = Path(img_ref)
            if not p.is_absolute():
                p = base / p
            routes = []
            for r in item.get("routes", []) or []:
                rect = r.get("rect") or r.get("rect_1280")
                nxt = r.get("next", r.get("next_frame", r.get("next_frame_index")))
                if rect and len(rect) == 4 and nxt is not None:
                    routes.append({"kind": str(r.get("kind") or r.get("action_kind") or "any").lower(), "rect": [float(v) for v in rect], "next": int(nxt)})
            nxt_idx = item.get("next", item.get("next_index"))
            frames.append(
                ReplayFrame(
                    image=str(p),
                    name=str(item.get("name") or f"frame_{i}"),
                    routes=routes,
                    next_index=int(nxt_idx) if nxt_idx is not None else None,
                )
            )
        return cls(frames, stay_on_last=stay_on_last, strict_overflow=strict_overflow)

    # ---------------- state machine ----------------
    def _advance(self, kind: str, rect_pt: Tuple[float, float]) -> None:
        cur = self.frames[self.current_index]
        for r in cur.routes:
            if r["kind"] in (kind, "any") and _point_in_rect(rect_pt[0], rect_pt[1], r["rect"]):
                self.current_index = max(0, min(len(self.frames) - 1, int(r["next"])))
                return
        if cur.routes and cur.next_index is None:
            return  # conditional frame: unmatched actions do not advance
        nxt = cur.next_index if cur.next_index is not None else self.current_index + 1
        if nxt >= len(self.frames):
            if self.strict_overflow:
                raise IndexError(f"ReplayHarness frame overflow after {len(self.actions)} actions")
            nxt = len(self.frames) - 1
        self.current_index = nxt

    def screencap(self, adb_path, adb_addr, timeout=5.0, cancel_event=None):
        with self._lock:
            self.screencap_count += 1
            img = self.frames[self.current_index].load()
            return img.copy() if img is not None and hasattr(img, "copy") else img

    def tap(self, adb_path, adb_addr, x_1280, y_720, width=1280, height=720, cancel_event=None):
        with self._lock:
            self.actions.append(
                RecordedAdbAction("tap", len(self.actions) + 1, self.current_index, float(x_1280), float(y_720), ts=time.time())
            )
            self._advance("tap", (float(x_1280), float(y_720)))
        return True

    def swipe(self, adb_path, adb_addr, x1_1280, y1_720, x2_1280, y2_720, duration_ms=350, width=1280, height=720, cancel_event=None):
        with self._lock:
            self.actions.append(
                RecordedAdbAction(
                    "swipe", len(self.actions) + 1, self.current_index, float(x1_1280), float(y1_720),
                    float(x2_1280), float(y2_720), int(duration_ms), ts=time.time(),
                )
            )
            self._advance("swipe", (float(x1_1280), float(y1_720)))
        return True

    # ---------------- attach ----------------
    @contextmanager
    def attach(self, fast_sleep: bool = True, out_dir: Optional[Union[str, Path]] = None) -> Iterator["FakeAdbReplayHarness"]:
        self._out_dir = Path(out_dir) if out_dir else None
        real_sleep = time.sleep
        patched: List[Tuple[Any, str, Any]] = []
        if fast_sleep:
            def _fast_sleep(secs: float = 0.0) -> None:
                real_sleep(0 if secs is None else min(max(float(secs), 0.0), 0.001))

            patched.append((time, "sleep", real_sleep))
            time.sleep = _fast_sleep  # type: ignore[assignment]
        try:
            with runtime_guard.adb_hooks_scope(self):
                yield self
        finally:
            for mod, name, orig in reversed(patched):
                setattr(mod, name, orig)
            if self._out_dir is not None:
                try:
                    self.dump_timeline_overlays(self._out_dir)
                except Exception:
                    pass

    # ---------------- assertions ----------------
    def assert_no_forbidden_taps(self, forbidden_rects: Sequence[Sequence[float]]) -> None:
        bad = []
        for a in self.actions:
            pts = [(a.x, a.y)] if a.kind == "tap" else [(a.x, a.y), (a.x2, a.y2)]
            for px, py in pts:
                for rect in forbidden_rects:
                    if _point_in_rect(px, py, rect):
                        bad.append((a.index, a.kind, (px, py), list(rect)))
        if bad:
            raise AssertionError(f"Forbidden-zone actions detected: {bad}")

    def assert_action_count(self, min_count: Optional[int] = None, max_count: Optional[int] = None, kind: Optional[str] = None) -> None:
        n = len([a for a in self.actions if kind is None or a.kind == kind])
        if min_count is not None and n < min_count:
            raise AssertionError(f"Expected >= {min_count} {kind or 'actions'}, got {n}")
        if max_count is not None and n > max_count:
            raise AssertionError(f"Expected <= {max_count} {kind or 'actions'}, got {n}")

    # ---------------- overlays ----------------
    def dump_timeline_overlays(self, out_dir: Union[str, Path]) -> List[str]:
        from . import visual_replay

        od = Path(out_dir)
        od.mkdir(parents=True, exist_ok=True)
        paths: List[str] = []
        for a in self.actions:
            fr = self.frames[max(0, min(len(self.frames) - 1, a.frame_index))]
            img = fr.load()
            ann: Dict[str, Any] = {"banner_lines": [f"frame={a.frame_index} {fr.name}"]}
            if a.kind == "tap":
                ann["taps"] = [{"cx": a.x, "cy": a.y, "label": f"TAP #{a.index} ({a.x:.0f},{a.y:.0f})"}]
            else:
                ann["swipes"] = [{"x1": a.x, "y1": a.y, "x2": a.x2, "y2": a.y2, "label": f"SWIPE #{a.index}"}]
            out = visual_replay.render_debug_overlay(img, ann, title=f"Replay action #{a.index} {a.kind}")
            p = od / f"timeline_{a.index:03d}_{a.kind}.png"
            out.save(str(p))
            paths.append(str(p))
        return paths
