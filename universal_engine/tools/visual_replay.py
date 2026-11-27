"""Visual replay: run the REAL online CV/OCR decision functions on a static screenshot and
render a debug overlay (matched / filtered / forbidden zones / intended taps / swipes).

CLI::

    python -m universal_engine.tools.visual_replay --image shot.png --mode rogue_tech --theme BlackFlow
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from ..utils import runtime_guard

MODES = ("stage_map", "rogue_lobby", "rogue_tech", "offline_popup")

COLOR_MATCH = (0x2E, 0xCC, 0x71)
COLOR_FILTER = (0xA0, 0xA5, 0xAF)
COLOR_FORBID = (0xEB, 0x40, 0x40)
COLOR_TAP = (255, 215, 0)
COLOR_SWIPE_A = (0, 210, 255)
COLOR_SWIPE_B = (255, 220, 0)

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_FONT_CACHE: Dict[int, Any] = {}


def _font(size: int) -> Any:
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    f: Any = None
    for fp in _FONT_CANDIDATES:
        if os.path.isfile(fp):
            try:
                f = ImageFont.truetype(fp, size=size)
                break
            except Exception:
                continue
    if f is None:
        try:
            f = ImageFont.load_default()
        except Exception:
            f = None
    _FONT_CACHE[size] = f
    return f


def _text(draw: ImageDraw.ImageDraw, xy: Tuple[float, float], s: str, fill: Tuple[int, ...], font: Any) -> Tuple[int, int, int, int]:
    s = str(s)
    try:
        draw.text(xy, s, fill=fill, font=font)
        return tuple(int(v) for v in draw.textbbox(xy, s, font=font))  # type: ignore[return-value]
    except Exception:
        safe = s.encode("ascii", "replace").decode("ascii")
        try:
            draw.text(xy, safe, fill=fill, font=font)
        except Exception:
            pass
        return (int(xy[0]), int(xy[1]), int(xy[0]) + 8 * len(safe), int(xy[1]) + 14)


def _label(draw: ImageDraw.ImageDraw, x: float, y: float, s: str, fg: Tuple[int, ...], font: Any) -> None:
    if not s:
        return
    try:
        l, t, r, b = draw.textbbox((x, y), s, font=font)
    except Exception:
        l, t, r, b = int(x), int(y), int(x) + 8 * len(s), int(y) + 14
    draw.rectangle([l - 2, t - 1, r + 2, b + 1], fill=(14, 18, 24, 205))
    _text(draw, (x, y), s, fg, font)


def _rect(bbox: Any) -> Optional[Tuple[int, int, int, int]]:
    try:
        x1, y1, x2, y2 = [int(round(float(v))) for v in bbox]
        return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    except Exception:
        return None


def render_debug_overlay(img: Optional[Image.Image], annotations: Optional[Dict[str, Any]], title: str = "") -> Image.Image:
    """Render annotated 1280x720 RGB debug image.

    annotations keys (all optional): target_boxes / filtered_boxes / forbidden_zones
    (each item ``{"bbox": [x1,y1,x2,y2], "label": str}``), taps (``{"cx","cy","label"}``),
    swipes (``{"x1","y1","x2","y2","label"}``), banner_lines (list[str]).
    """
    ann = annotations or {}
    if img is None:
        canvas = Image.new("RGBA", (1280, 720), (24, 28, 36, 255))
    else:
        canvas = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("RGBA")
    ov = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    f14, f12, f16 = _font(14), _font(12), _font(16)

    for z in ann.get("forbidden_zones") or []:
        r = _rect(z.get("bbox")) if isinstance(z, dict) else None
        if r:
            d.rectangle(r, fill=COLOR_FORBID + (56,), outline=COLOR_FORBID + (230,), width=2)
            _label(d, r[0] + 4, r[1] + 4, str(z.get("label") or ""), (255, 215, 215, 255), f12)
    for b in ann.get("filtered_boxes") or []:
        r = _rect(b.get("bbox")) if isinstance(b, dict) else None
        if r:
            d.rectangle(r, outline=COLOR_FILTER + (230,), width=2)
            _label(d, r[0], max(0, r[1] - 16), str(b.get("label") or ""), (225, 228, 232, 255), f12)
    for b in ann.get("target_boxes") or []:
        r = _rect(b.get("bbox")) if isinstance(b, dict) else None
        if r:
            d.rectangle(r, outline=COLOR_MATCH + (255,), width=3)
            _label(d, r[0], max(0, r[1] - 18), str(b.get("label") or ""), (190, 255, 210, 255), f12)
    for s in ann.get("swipes") or []:
        try:
            x1, y1, x2, y2 = [float(s[k]) for k in ("x1", "y1", "x2", "y2")]
        except Exception:
            continue
        d.line([(x1, y1), (x2, y2)], fill=COLOR_SWIPE_A + (255,), width=4)
        d.ellipse([x1 - 7, y1 - 7, x1 + 7, y1 + 7], fill=COLOR_SWIPE_A + (255,))
        import math

        ang = math.atan2(y2 - y1, x2 - x1)
        for da in (2.6, -2.6):
            d.line(
                [(x2, y2), (x2 + 18 * math.cos(ang + da), y2 + 18 * math.sin(ang + da))],
                fill=COLOR_SWIPE_B + (255,),
                width=4,
            )
        _label(d, (x1 + x2) / 2, (y1 + y2) / 2 - 20, str(s.get("label") or f"SWIPE ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f})"), (150, 240, 255, 255), f12)
    for t in ann.get("taps") or []:
        try:
            cx, cy = float(t["cx"]), float(t["cy"])
        except Exception:
            continue
        d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], outline=COLOR_TAP + (255,), width=3)
        d.line([(cx - 28, cy), (cx + 28, cy)], fill=(255, 80, 80, 255), width=2)
        d.line([(cx, cy - 28), (cx, cy + 28)], fill=(255, 80, 80, 255), width=2)
        _label(d, cx + 20, cy - 10, str(t.get("label") or f"({cx:.0f}, {cy:.0f})"), (255, 235, 130, 255), f12)

    lines = [str(x) for x in (ann.get("banner_lines") or [])]
    if title:
        lines.insert(0, str(title))
    if lines:
        bar_h = min(200, 10 + 20 * min(len(lines), 9))
        d.rectangle([0, 0, 1280, bar_h], fill=(18, 22, 30, 205))
        for i, ln in enumerate(lines[:9]):
            _text(d, (14, 6 + i * 20), ln[:130], (255, 220, 120, 255) if i == 0 else (235, 242, 248, 255), f14)

    # bottom legend
    d.rectangle([0, 694, 1280, 720], fill=(18, 22, 30, 215))
    lx = 14
    for col, name in (
        (COLOR_MATCH, "绿=命中/候选"),
        (COLOR_FILTER, "灰=被过滤+原因"),
        (COLOR_FORBID, "红=禁区/ROI"),
        (COLOR_TAP, "十字=拟点击"),
        (COLOR_SWIPE_A, "箭头=拟滑动"),
    ):
        d.rectangle([lx, 702, lx + 14, 714], fill=col + (255,))
        bb = _text(d, (lx + 20, 700), name, (230, 235, 240, 255), f14)
        lx = bb[2] + 24
    return Image.alpha_composite(canvas, ov).convert("RGB")


runtime_guard.register_overlay_renderer(render_debug_overlay)


# ---------------------------------------------------------------- helpers
def _jsonable(o: Any, depth: int = 0) -> Any:
    if depth > 6:
        return str(o)
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in o]
    if isinstance(o, Path):
        return str(o)
    if hasattr(o, "item"):
        try:
            return o.item()
        except Exception:
            pass
    if hasattr(o, "tolist"):
        try:
            return o.tolist()
        except Exception:
            pass
    if hasattr(o, "size") and hasattr(o, "mode"):
        return f"<Image {o.size}>"
    return str(o)


def _box_rect(b: Any) -> Optional[Tuple[int, int, int, int]]:
    if not isinstance(b, dict):
        return None
    if all(k in b for k in ("x", "y", "w", "h")):
        try:
            return (int(b["x"]), int(b["y"]), int(b["x"]) + int(b["w"]), int(b["y"]) + int(b["h"]))
        except Exception:
            pass
    for k in ("bbox", "rect", "roi"):
        v = b.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 4:
            try:
                a = [int(round(float(t))) for t in v]
                # bbox as x1,y1,x2,y2 (if x2>x1 and y2>y1 and looks like corners) else x,y,w,h
                if k == "bbox" and a[2] > a[0] and a[3] > a[1]:
                    return (a[0], a[1], a[2], a[3])
                return (a[0], a[1], a[0] + a[2], a[1] + a[3])
            except Exception:
                pass
    pt = _box_point(b)
    if pt:
        return (pt[0] - 26, pt[1] - 16, pt[0] + 26, pt[1] + 16)
    return None


def _box_point(b: Any) -> Optional[Tuple[int, int]]:
    if isinstance(b, dict):
        if "cx" in b and "cy" in b:
            try:
                return (int(b["cx"]), int(b["cy"]))
            except Exception:
                pass
        for k in ("xy", "tap", "point", "center"):
            v = b.get(k)
            if isinstance(v, (list, tuple)) and len(v) == 2:
                try:
                    return (int(v[0]), int(v[1]))
                except Exception:
                    pass
        if "tap_x" in b and "tap_y" in b:
            try:
                return (int(b["tap_x"]), int(b["tap_y"]))
            except Exception:
                pass
    elif isinstance(b, (list, tuple)) and len(b) == 2:
        try:
            return (int(b[0]), int(b[1]))
        except Exception:
            pass
    return None


class _Ctx:
    def __init__(self, log_fn: Callable[[str], None]) -> None:
        self.log = log_fn
        self.errors: List[Dict[str, str]] = []
        self.ann: Dict[str, Any] = {
            "target_boxes": [], "filtered_boxes": [], "forbidden_zones": [], "taps": [], "swipes": [], "banner_lines": [],
        }
        self.result: Dict[str, Any] = {}

    def call(self, name: str, fn: Optional[Callable[..., Any]], *a: Any, **kw: Any) -> Any:
        if fn is None:
            self.errors.append({"fn": name, "error": "function not found"})
            self.log(f"[REPLAY][SKIP] {name}: 函数不存在")
            return None
        try:
            t0 = time.time()
            out = fn(*a, **kw)
            self.log(f"[REPLAY] {name} -> ok ({time.time() - t0:.2f}s)")
            return out
        except Exception as exc:
            self.errors.append({"fn": name, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=3)})
            self.log(f"[REPLAY][ERR] {name}: {type(exc).__name__}: {exc}")
            return None

    def target(self, b: Any, label: str) -> None:
        r = _box_rect(b)
        if r:
            self.ann["target_boxes"].append({"bbox": list(r), "label": label})

    def filtered(self, b: Any, label: str) -> None:
        r = _box_rect(b)
        if r:
            self.ann["filtered_boxes"].append({"bbox": list(r), "label": label})

    def tap(self, pt: Any, label: str) -> None:
        p = _box_point(pt)
        if p:
            self.ann["taps"].append({"cx": p[0], "cy": p[1], "label": f"{label} ({p[0]},{p[1]})"})

    def banner(self, s: str) -> None:
        self.ann["banner_lines"].append(s)
        self.log(f"[REPLAY][DECISION] {s}")


def _sp() -> Any:
    from .. import stage_pilot

    return stage_pilot


def _rl() -> Any:
    from .. import roguelike_ai_copilot

    return roguelike_ai_copilot


def _short(s: Any, n: int = 24) -> str:
    return str(s or "").replace("\n", " ")[:n]


def _log_boxes(c: _Ctx, boxes: Any, tag: str) -> None:
    c.log(f"[REPLAY][OCR] {tag}: {len(boxes or [])} 个框")
    for b in (boxes or [])[:80]:
        r = _box_rect(b)
        c.log(f"    text={_short(b.get('text') if isinstance(b, dict) else b, 40)!r} rect={r}")


# ---------------------------------------------------------------- modes
def _mode_stage_map(img: Image.Image, maa_dir: Path, theme: str, target_stage: str, c: _Ctx) -> None:
    sp = _sp()
    state = c.call("analyze_screen_state", getattr(sp, "analyze_screen_state", None), img, maa_dir, use_ocr_fallback=True)
    c.result["screen_state"] = state
    boxes = c.call("find_stage_boxes_via_ocr", getattr(sp, "find_stage_boxes_via_ocr", None), img) or []
    _log_boxes(c, boxes, "stage_boxes")
    cards = c.call("detect_stage_cards", getattr(sp, "detect_stage_cards", None), img) or []
    nodes = c.call("scan_all_map_stage_nodes", getattr(sp, "scan_all_map_stage_nodes", None), img) or []
    c.result.update({"ocr_boxes": boxes, "cards": cards, "nodes": nodes})
    frontier = c.call("select_next_frontier_node", getattr(sp, "select_next_frontier_node", None), nodes) if nodes else None
    c.result["frontier"] = frontier

    matched = None
    if target_stage:
        m = c.call("_match_stage_center_from_boxes", getattr(sp, "_match_stage_center_from_boxes", None), boxes, target_stage)
        if m:
            matched = {"cx": m[0], "cy": m[1], "box": m[2]}
        c.result["target_match"] = matched
        c.result["detail_panel"] = c.call("inspect_stage_detail_panel", getattr(sp, "inspect_stage_detail_panel", None), img, maa_dir, expected_stage=target_stage)
        c.result["story_modal"] = c.call("detect_story_card_modal", getattr(sp, "detect_story_card_modal", None), img, maa_dir, expected_stage=target_stage)

    for b in boxes:
        is_t = bool(matched and isinstance(b, dict) and b is matched.get("box"))
        if is_t:
            c.target(b, f"TARGET {_short(b.get('text'))}")
        elif target_stage:
            c.filtered(b, f"{_short(b.get('text'))} | 非目标 {target_stage}")
        else:
            c.target(b, _short(b.get("text")))
    for cd in cards:
        c.filtered(cd, "card") if target_stage else c.target(cd, "card")
    for nd in nodes:
        is_f = bool(frontier and nd is frontier)
        if is_f:
            c.target(nd, f"FRONTIER {nd.get('code', '')}")
        else:
            c.filtered(nd, f"{nd.get('code', '')} | 非前沿节点")

    blank = c.call("find_safe_blank_tap_point", getattr(sp, "find_safe_blank_tap_point", None), img, stage_boxes=boxes, white_cards=cards)
    c.result["safe_blank_tap"] = blank
    if matched:
        c.tap(matched, "目标关卡")
        c.banner(f"决策: 目标 {target_stage} 命中 ({matched['cx']},{matched['cy']})")
    elif frontier:
        c.tap(frontier, "前沿节点")
        c.banner(f"决策: 选择前沿节点 {frontier.get('code')} ({frontier.get('cx')},{frontier.get('cy')})")
    else:
        c.banner(f"决策: 未命中目标/前沿节点 (target={target_stage or '-'})")
    if blank:
        c.tap(blank, "安全空白点")
    c.result["decision"] = c.ann["banner_lines"][-1] if c.ann["banner_lines"] else ""


def _mode_rogue_lobby(img: Image.Image, maa_dir: Path, theme: str, target_stage: str, c: _Ctx) -> None:
    rl, sp = _rl(), _sp()
    cls = c.call("classify_roguelike_screen", getattr(rl, "classify_roguelike_screen", None), img, maa_dir, theme=theme)
    c.result["classify"] = cls
    boxes = c.call("find_text_boxes_via_ocr", getattr(sp, "find_text_boxes_via_ocr", None), img) or []
    _log_boxes(c, boxes, "lobby_text")
    c.result["ocr_boxes"] = boxes
    is_sel = c.call("is_roguelike_theme_select_page", getattr(rl, "is_roguelike_theme_select_page", None), boxes)
    is_lob = c.call("is_roguelike_theme_lobby", getattr(rl, "is_roguelike_theme_lobby", None), boxes, theme)
    c.result.update({"is_theme_select_page": is_sel, "is_theme_lobby": is_lob})
    tech = c.call("resolve_lobby_tech_tree_entry", getattr(rl, "resolve_lobby_tech_tree_entry", None), boxes, theme)
    tech_ocr = c.call("resolve_lobby_tech_tree_via_local_ocr", getattr(rl, "resolve_lobby_tech_tree_via_local_ocr", None), img, theme)
    reward = c.call("resolve_lobby_level_reward_entry", getattr(rl, "resolve_lobby_level_reward_entry", None), boxes, theme)
    comm = c.call("resolve_lobby_monthly_commission_entry", getattr(rl, "resolve_lobby_monthly_commission_entry", None), boxes, theme)
    anchor = c.call("_resolve_theme_anchor_fallback", getattr(rl, "_resolve_theme_anchor_fallback", None), img, theme if is_lob else None, boxes)
    c.result.update({"tech_entry": tech, "tech_entry_local_ocr": tech_ocr, "level_reward_entry": reward, "monthly_commission_entry": comm, "theme_anchor_fallback": anchor})

    hits = {id(x) for x in (tech, tech_ocr, reward, comm, anchor) if isinstance(x, dict)}
    for b in boxes:
        if id(b) in hits:
            continue
        c.filtered(b, _short(b.get("text") if isinstance(b, dict) else b, 18))
    for name, ent in (("科技树", tech), ("科技树(本地OCR)", tech_ocr), ("等级奖励", reward), ("月度委托", comm), ("主题锚点兜底", anchor)):
        if isinstance(ent, dict):
            c.target(ent, name)
            c.tap(ent, name)
    scr = (cls or {}).get("screen") if isinstance(cls, dict) else None
    c.banner(f"决策: screen={scr} theme_select={is_sel} theme_lobby={is_lob} 科技树={'Y' if (tech or tech_ocr or anchor) else 'N'} 奖励={'Y' if reward else 'N'} 委托={'Y' if comm else 'N'}")
    c.result["decision"] = c.ann["banner_lines"][-1]


def _mode_rogue_tech(img: Image.Image, maa_dir: Path, theme: str, target_stage: str, c: _Ctx) -> None:
    rl, sp = _rl(), _sp()
    prof = c.call("get_theme_ui_profile", getattr(rl, "get_theme_ui_profile", None), theme) or {}
    boxes = c.call("find_text_boxes_via_ocr", getattr(sp, "find_text_boxes_via_ocr", None), img) or []
    _log_boxes(c, boxes, "tech_text")
    c.result["ocr_boxes"] = boxes
    guard = c.call("classify_tech_tree_page_guard", getattr(rl, "classify_tech_tree_page_guard", None), img, boxes, maa_dir, prof)
    points = c.call("extract_tech_tree_remaining_points", getattr(rl, "extract_tech_tree_remaining_points", None), img, boxes)
    badge = c.call("_detect_tech_tree_progress_badge", getattr(rl, "_detect_tech_tree_progress_badge", None), boxes)
    nodes = c.call("detect_upgradeable_tech_nodes", getattr(rl, "detect_upgradeable_tech_nodes", None), img) or []
    confirm = c.call("evaluate_tech_tree_confirm_button", getattr(rl, "evaluate_tech_tree_confirm_button", None), img, boxes, remaining_points=points)
    c.result.update({"page_guard": guard, "remaining_points": points, "progress_badge": badge, "upgradeable_nodes": nodes, "confirm_button": confirm})

    zones = getattr(rl, "TECH_TREE_FORBIDDEN_ZONES_NORM", None) or (
        (0.0, 0.0, 0.08, 1.0), (0.93, 0.0, 1.0, 1.0), (0.0, 0.0, 1.0, 0.16), (0.0, 0.80, 1.0, 1.0), (0.0, 0.63, 0.175, 1.0), (0.78, 0.0, 1.0, 0.20),
    )
    for i, z in enumerate(zones):
        x1, y1, x2, y2 = z
        c.ann["forbidden_zones"].append({"bbox": [x1 * 1280, y1 * 720, x2 * 1280, y2 * 720], "label": f"禁区#{i + 1}"})
    c.ann["forbidden_zones"].append({"bbox": [0.08 * 1280, 0.60 * 720, 0.25 * 1280, 0.80 * 720], "label": "弧形边缘(易误触总览)"})

    in_zone = getattr(rl, "is_point_in_tech_tree_forbidden_zone", None)
    near_arch = getattr(rl, "is_point_near_tech_tree_arch_edge", None)
    safe: List[Dict[str, Any]] = []
    for n in nodes:
        pt = _box_point(n)
        reason = ""
        if pt and in_zone and in_zone(pt[0], pt[1]):
            reason = "命中禁区"
        elif pt and near_arch and near_arch(pt[0], pt[1]):
            reason = "弧形边缘"
        kind = n.get("kind") or n.get("type") or "" if isinstance(n, dict) else ""
        if reason:
            c.filtered(n, f"{kind} {reason}")
        else:
            safe.append(n)
            c.target(n, f"节点 {kind}")
    if safe:
        c.tap(safe[0], "拟点节点")
    cb = (confirm or {}).get("button_status") if isinstance(confirm, dict) else None
    gs = (guard or {}).get("page") or (guard or {}).get("status") or (guard or {}).get("kind") if isinstance(guard, dict) else None
    c.banner(f"决策: guard={gs} 剩余点数={points} 可升级候选={len(safe)}/{len(nodes)} 确认按钮={cb}")
    c.result["decision"] = c.ann["banner_lines"][-1]


def _mode_offline_popup(img: Image.Image, maa_dir: Path, theme: str, target_stage: str, c: _Ctx) -> None:
    sp = _sp()
    pop = c.call("detect_network_disconnect_popup", getattr(sp, "detect_network_disconnect_popup", None), img, maa_dir)
    home = c.call("is_game_home_or_online_screen", getattr(sp, "is_game_home_or_online_screen", None), img, maa_dir)
    guide = c.call("detect_guide_popup", getattr(sp, "detect_guide_popup", None), img, maa_dir)
    c.result.update({"disconnect_popup": pop, "home_or_online": home, "guide_popup": guide})
    for name, res in (("断线弹窗", pop), ("引导弹窗", guide)):
        if isinstance(res, dict):
            for k in ("confirm_xy", "close_xy", "tap_xy", "xy", "button_xy"):
                if res.get(k):
                    c.tap(res[k], f"{name}:{k}")
            for k in ("bbox", "rect", "confirm_bbox"):
                if res.get(k):
                    (c.target if res.get("detected") else c.filtered)({"bbox": res[k]}, name)
    c.banner(
        f"决策: 断线弹窗={bool((pop or {}).get('detected'))} 回到主页/在线={bool((home or {}).get('online'))} 引导弹窗={bool((guide or {}).get('detected'))}"
    )
    c.result["decision"] = c.ann["banner_lines"][-1]


_MODE_FN = {
    "stage_map": _mode_stage_map,
    "rogue_lobby": _mode_rogue_lobby,
    "rogue_tech": _mode_rogue_tech,
    "offline_popup": _mode_offline_popup,
}


def _resolve_maa_dir(maa_dir: Optional[Any]) -> Path:
    if maa_dir:
        return Path(maa_dir)
    try:
        from ..utils import maa_installer

        p = maa_installer.resolve_maa_path()
        if p:
            return Path(p)
    except Exception:
        pass
    return Path.cwd()


def run_visual_replay(
    image_path: Any,
    mode: str,
    theme: str = "BlackFlow",
    target_stage: str = "",
    maa_dir: Optional[Any] = None,
    out_dir: Any = "reports/replay_debug",
    json_out: Optional[Any] = None,
    log_fn: Callable[[str], None] = print,
) -> Dict[str, Any]:
    if mode not in _MODE_FN:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    log = log_fn or print
    ip = Path(image_path)
    with Image.open(str(ip)) as im:
        img = im.convert("RGB")
    maa = _resolve_maa_dir(maa_dir)
    log(f"[REPLAY] image={ip} size={img.size} mode={mode} theme={theme} target={target_stage or '-'} maa_dir={maa}")
    c = _Ctx(log)
    try:
        _MODE_FN[mode](img, maa, theme, target_stage, c)
    except Exception as exc:  # never crash the CLI
        c.errors.append({"fn": f"mode:{mode}", "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=4)})
        log(f"[REPLAY][ERR] mode {mode}: {exc}")
    for k in ("ocr_boxes",):
        pass
    overlay = render_debug_overlay(img, c.ann, title=f"[REPLAY] {mode} · {theme} · {ip.name}")
    od = Path(out_dir)
    od.mkdir(parents=True, exist_ok=True)
    png = od / f"replay_{mode}_{ip.stem}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    overlay.save(str(png))
    log(f"[REPLAY] overlay saved: {png}")
    result: Dict[str, Any] = {
        "ok": not c.errors,
        "mode": mode,
        "theme": theme,
        "target_stage": target_stage,
        "image": str(ip),
        "maa_dir": str(maa),
        "overlay_path": str(png),
        "decision": c.result.get("decision", ""),
        "annotations": c.ann,
        "errors": c.errors,
        **{k: v for k, v in c.result.items() if k != "decision"},
    }
    result = _jsonable(result)
    if json_out:
        jp = Path(json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[REPLAY] json saved: {jp}")
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    p = argparse.ArgumentParser(description="Offline visual replay of online CV/OCR decisions")
    p.add_argument("--image", required=True)
    p.add_argument("--mode", required=True, choices=MODES)
    p.add_argument("--theme", default="BlackFlow")
    p.add_argument("--target-stage", default="")
    p.add_argument("--maa-dir", default=None)
    p.add_argument("--out-dir", default="reports/replay_debug")
    p.add_argument("--json-out", default=None)
    a = p.parse_args(argv)
    run_visual_replay(
        a.image, a.mode, theme=a.theme, target_stage=a.target_stage, maa_dir=a.maa_dir, out_dir=a.out_dir, json_out=a.json_out
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
