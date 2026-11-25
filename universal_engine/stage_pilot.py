"""Visual/ADB stage navigation, pre/post-battle story skipping, and tutorial stage handoff."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageOps

from .utils.env_doctor import OperationCancelledError, run_cmd_cancellable, wait_interruptible

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_TEMPLATE_CACHE: Dict[str, Any] = {}


def _run_cmd(
    cmd: List[str],
    timeout: float = 5.0,
    text: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> subprocess.CompletedProcess:
    return run_cmd_cancellable(cmd, timeout_sec=timeout, cancel_event=cancel_event, text=text)


def _emit(log_callback: Optional[Callable[[str], None]], msg: str) -> None:
    if log_callback:
        try:
            log_callback(msg)
            return
        except Exception:
            pass
    print(msg)


def classify_stage_node(stage_query: str, header_hint: str = "") -> str:
    """Classify a stage code/id into 'story' (-ST-/EG/END), 'tutorial' (-TR-/TUTORIAL), or 'battle'."""
    h_up = str(header_hint or "").strip().upper()
    if "TUTOR" in h_up:
        return "tutorial"
    if h_up in ("STORY", "ST0RY", "5TORY"):
        return "story"
    s = str(stage_query or "").strip().upper()
    if not s:
        return "battle"
    if "-TR-" in s or "_TR_" in s or re.match(r"^TR-[A-Z0-9]*\d", s):
        return "tutorial"
    if (
        "-ST-" in s
        or "_ST_" in s
        or re.match(r"^ST-[A-Z0-9]*\d", s)
        or "-EG-" in s
        or "_EG_" in s
        or re.match(r"^EG-[A-Z0-9]*\d", s)
        or "-END-" in s
        or "_END_" in s
        or re.match(r"^END\d*-[A-Z0-9]*\d", s)
    ):
        return "story"
    return "battle"


def norm_to_device_xy(x_1280: float, y_720: float, width: int, height: int) -> Tuple[int, int]:
    """Convert normalized 1280x720 coordinates to device pixel coordinates."""
    w = max(1, int(width))
    h = max(1, int(height))
    dx = max(0, min(w - 1, int(round(float(x_1280) * w / 1280.0))))
    dy = max(0, min(h - 1, int(round(float(y_720) * h / 720.0))))
    return dx, dy


def adb_screencap_image(
    adb_path: str,
    adb_addr: str,
    timeout: float = 5.0,
    cancel_event: Optional[threading.Event] = None,
) -> Optional[Image.Image]:
    """Capture current emulator screen into an RGB PIL.Image via ADB with short timeout & cancellation."""
    if not adb_path or not adb_addr:
        return None
    if cancel_event and cancel_event.is_set():
        return None
    try:
        res = run_cmd_cancellable(
            [adb_path, "-s", adb_addr, "exec-out", "screencap", "-p"],
            timeout_sec=timeout,
            cancel_event=cancel_event,
            text=False,
        )
        raw = res.stdout if isinstance(res.stdout, (bytes, bytearray)) else b""
        if res.returncode == 0 and raw.startswith(b"\x89PNG"):
            with Image.open(io.BytesIO(raw)) as im:
                return im.convert("RGB")
    except OperationCancelledError:
        return None
    except Exception:
        pass

    if cancel_event and cancel_event.is_set():
        return None

    tmp_file = ""
    try:
        fd, tmp_file = tempfile.mkstemp(prefix="maa_pilot_", suffix=".png")
        os.close(fd)
        r1 = run_cmd_cancellable(
            [adb_path, "-s", adb_addr, "shell", "screencap", "-p", "/sdcard/_maa_pilot.png"],
            timeout_sec=timeout,
            cancel_event=cancel_event,
            text=False,
        )
        if r1.returncode != 0:
            return None
        r2 = run_cmd_cancellable(
            [adb_path, "-s", adb_addr, "pull", "/sdcard/_maa_pilot.png", tmp_file],
            timeout_sec=timeout,
            cancel_event=cancel_event,
            text=False,
        )
        if r2.returncode == 0 and os.path.isfile(tmp_file) and os.path.getsize(tmp_file) > 0:
            with Image.open(tmp_file) as im:
                return im.convert("RGB")
    except OperationCancelledError:
        return None
    except Exception:
        pass
    finally:
        if tmp_file and os.path.isfile(tmp_file):
            try:
                os.remove(tmp_file)
            except Exception:
                pass
    return None


def adb_tap_norm(
    adb_path: str,
    adb_addr: str,
    x_1280: float,
    y_720: float,
    width: int = 1280,
    height: int = 720,
    cancel_event: Optional[threading.Event] = None,
) -> bool:
    """Tap a normalized 1280x720 coordinate on the connected ADB device."""
    if not adb_path or not adb_addr:
        return False
    if cancel_event and cancel_event.is_set():
        return False
    dx, dy = norm_to_device_xy(x_1280, y_720, width, height)
    try:
        res = _run_cmd(
            [adb_path, "-s", adb_addr, "shell", "input", "tap", str(dx), str(dy)],
            timeout=5.0,
            cancel_event=cancel_event,
        )
        return res.returncode == 0
    except Exception:
        return False


def adb_swipe_norm(
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
    """Swipe between two normalized 1280x720 coordinates via ADB."""
    if not adb_path or not adb_addr:
        return False
    if cancel_event and cancel_event.is_set():
        return False
    sx, sy = norm_to_device_xy(x1_1280, y1_720, width, height)
    ex, ey = norm_to_device_xy(x2_1280, y2_720, width, height)
    try:
        res = _run_cmd(
            [adb_path, "-s", adb_addr, "shell", "input", "swipe", str(sx), str(sy), str(ex), str(ey), str(int(duration_ms))],
            timeout=5.0,
            cancel_event=cancel_event,
        )
        return res.returncode == 0
    except Exception:
        return False


def _load_template_gray(template_path: Path) -> Optional[Any]:
    key = str(template_path.resolve())
    if key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[key]
    if not template_path.is_file() or np is None:
        return None
    try:
        with Image.open(template_path) as im:
            arr = np.asarray(im.convert("L"), dtype=np.float32)
        _TEMPLATE_CACHE[key] = arr
        return arr
    except Exception:
        return None


def match_template_roi(
    gray_1280: Any,
    template_path: Path,
    roi: Tuple[int, int, int, int],
    threshold: float = 0.72,
    mask_range: Optional[Tuple[int, int]] = None,
) -> Tuple[bool, float, Tuple[int, int]]:
    """Perform masked/unmasked NCC template matching inside a 1280x720 ROI."""
    if np is None or gray_1280 is None:
        return False, 0.0, (0, 0)
    tpl = _load_template_gray(template_path)
    if tpl is None:
        return False, 0.0, (0, 0)

    rx, ry, rw, rh = roi
    h_img, w_img = gray_1280.shape[:2]
    x0 = max(0, int(rx))
    y0 = max(0, int(ry))
    x1 = min(w_img, x0 + int(rw))
    y1 = min(h_img, y0 + int(rh))
    patch = np.asarray(gray_1280[y0:y1, x0:x1], dtype=np.float64)
    th, tw = tpl.shape[:2]
    ph, pw = patch.shape[:2]
    if ph < th or pw < tw:
        return False, 0.0, (0, 0)

    tpl64 = np.asarray(tpl, dtype=np.float64)
    if mask_range is not None:
        lo, hi = mask_range
        mask_bool = (tpl >= lo) & (tpl <= hi)
        valid_cnt = int(mask_bool.sum())
        if valid_cnt >= 16:
            mask = mask_bool.astype(np.float64)
            t_mean = float(tpl64[mask_bool].mean())
            t_zero = (tpl64 - t_mean) * mask
        else:
            mask = None
            valid_cnt = th * tw
            t_zero = tpl64 - float(tpl64.mean())
    else:
        mask = None
        valid_cnt = th * tw
        t_zero = tpl64 - float(tpl64.mean())

    t_denom = float(np.sqrt((t_zero * t_zero).sum()))
    if t_denom < 1e-5:
        return False, 0.0, (0, 0)

    out_h = ph - th + 1
    out_w = pw - tw + 1
    fh = 1 << (ph - 1).bit_length()
    fw = 1 << (pw - 1).bit_length()

    f_patch = np.fft.rfft2(patch, s=(fh, fw))
    num = np.fft.irfft2(f_patch * np.conj(np.fft.rfft2(t_zero, s=(fh, fw))), s=(fh, fw))[:out_h, :out_w]

    if mask is not None:
        f_mask_conj = np.conj(np.fft.rfft2(mask, s=(fh, fw)))
        sum_p = np.fft.irfft2(f_patch * f_mask_conj, s=(fh, fw))[:out_h, :out_w]
        sum_p2 = np.fft.irfft2(np.fft.rfft2(patch * patch, s=(fh, fw)) * f_mask_conj, s=(fh, fw))[:out_h, :out_w]
    else:
        # Exact O(H*W) integral image for unmasked window sums
        ps = np.pad(patch, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0).cumsum(axis=1)
        ps2 = np.pad(patch * patch, ((1, 0), (1, 0)), mode="constant").cumsum(axis=0).cumsum(axis=1)
        sum_p = ps[th:, tw:] - ps[:-th, tw:] - ps[th:, :-tw] + ps[:-th, :-tw]
        sum_p2 = ps2[th:, tw:] - ps2[:-th, tw:] - ps2[th:, :-tw] + ps2[:-th, :-tw]

    var_p = np.maximum(0.0, sum_p2 - (sum_p * sum_p) / float(valid_cnt))
    w_denom = np.sqrt(var_p) + 1e-6
    ncc = num / (w_denom * t_denom)

    best_iy, best_ix = np.unravel_index(int(np.argmax(ncc)), ncc.shape)
    score = float(ncc[best_iy, best_ix])
    cx = int(x0 + best_ix + tw // 2)
    cy = int(y0 + best_iy + th // 2)
    return score >= threshold, score, (cx, cy)


def _match_any_template(
    gray_1280: Any,
    maa_dir: Path,
    candidates: List[Tuple[str, Tuple[int, int, int, int], float, Optional[Tuple[int, int]]]],
) -> Tuple[bool, float, Tuple[int, int], str]:
    tpl_root = Path(maa_dir) / "resource" / "template"
    best_score = 0.0
    best_xy = (0, 0)
    best_name = ""
    for rel, roi, thresh, mrange in candidates:
        matched, score, xy = match_template_roi(gray_1280, tpl_root / rel, roi, threshold=thresh, mask_range=mrange)
        if score > best_score:
            best_score = score
            best_xy = xy
            best_name = rel
        if matched:
            return True, score, xy, rel
    return False, best_score, best_xy, best_name


def run_win_ocr_on_images(
    images: List[Tuple[str, Image.Image]],
    timeout: int = 12,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Run built-in Windows.Media.Ocr across one or more RGB PIL Images in one PowerShell process."""
    if sys.platform != "win32" or not images:
        return {}
    if cancel_event is not None and cancel_event.is_set():
        return {}

    tmp_files: List[Tuple[str, str, int, int]] = []
    try:
        for tag, im in images:
            fd, path = tempfile.mkstemp(prefix=f"maa_ocr_{tag}_", suffix=".png")
            os.close(fd)
            rgb = im.convert("RGB")
            rgb.save(path, format="PNG")
            tmp_files.append((tag, path, rgb.width, rgb.height))

        paths_ps = ", ".join("'" + p.replace("'", "''") + "'" for _, p, _, _ in tmp_files)
        tags_ps = ", ".join("'" + t.replace("'", "''") + "'" for t, _, _, _ in tmp_files)
        D = "$"
        ps = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
{D}OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
{D}null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
{D}null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType=WindowsRuntime]
{D}null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
function Await({D}op, {D}type) {{
    {D}m = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{ {D}_.Name -eq 'AsTask' -and {D}_.GetParameters().Count -eq 1 -and {D}_.GetGenericArguments().Count -eq 1 }} | Select-Object -First 1
    {D}task = {D}m.MakeGenericMethod({D}type).Invoke({D}null, @({D}op))
    {D}task.Wait()
    return {D}task.Result
}}
{D}eng = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
{D}paths = @({paths_ps})
{D}tags = @({tags_ps})
{D}all = @{{}}
for ({D}i = 0; {D}i -lt {D}paths.Count; {D}i++) {{
    {D}file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync({D}paths[{D}i])) ([Windows.Storage.StorageFile])
    {D}stream = Await ({D}file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    {D}dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync({D}stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    {D}bmp = Await ({D}dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    {D}res = Await ({D}eng.RecognizeAsync({D}bmp)) ([Windows.Media.Ocr.OcrResult])
    {D}lines = @()
    foreach ({D}ln in {D}res.Lines) {{
        {D}words = @()
        foreach ({D}w in {D}ln.Words) {{
            {D}r = {D}w.BoundingRect
            {D}words += @{{ text={D}w.Text; x=[int]{D}r.X; y=[int]{D}r.Y; w=[int]{D}r.Width; h=[int]{D}r.Height }}
        }}
        {D}lines += @{{ text={D}ln.Text; words={D}words }}
    }}
    {D}all[{D}tags[{D}i]] = {D}lines
}}
{D}all | ConvertTo-Json -Depth 5 -Compress
"""
        enc = base64.b64encode(ps.encode("utf-16le")).decode("ascii")
        res = run_cmd_cancellable(
            ["powershell", "-NoProfile", "-EncodedCommand", enc],
            timeout_sec=float(timeout),
            cancel_event=cancel_event,
            text=True,
        )
        raw = (res.stdout or "").lstrip("\ufeff").strip()
        if res.returncode != 0 or not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except OperationCancelledError:
        return {}
    except Exception:
        return {}
    finally:
        for _, p, _, _ in tmp_files:
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


_STAGE_PREFIX_CONFUSIONS: Dict[str, str] = {
    "T": "TI17",
    "L": "LI1|",
    "I": "IL1|",
    "O": "O0Q",
    "S": "S5",
    "Z": "Z2",
    "B": "B8",
}

_STAGE_DIGIT_TAIL_CHARS = set("0123456789Il|[]!OQSZ")
_STAGE_DIGIT_TAIL_TRANS = str.maketrans({
    "I": "1",
    "l": "1",
    "|": "1",
    "]": "1",
    "[": "1",
    "!": "1",
    "Z": "2",
    "O": "0",
    "Q": "0",
    "S": "5",
})


UNIVERSAL_STAGE_CODE_RE = re.compile(r"^(?:[A-Z0-9]{1,5}-){1,2}[A-Z0-9]{1,4}$")
_UNIVERSAL_STAGE_SEARCH_RE = re.compile(r"(?<![A-Z0-9])((?:[A-Z0-9]{1,5}-){1,2}[A-Z0-9]{1,4})(?![A-Z0-9])")
_NON_STAGE_FIRST_SEGMENTS = frozenset({
    "VER", "REV", "BUILD", "BETA", "ALPHA", "RC", "UTC", "GMT", "AM", "PM",
    "FPS", "MS", "KB", "MB", "GB", "TB", "HTTP", "HTTPS", "API", "URL",
    "UID", "ID", "NO", "NUM", "LV", "LVL", "EXP", "HP", "ATK", "DEF", "RES",
    "SAN", "PRTS", "MAA", "ADB", "WIN", "CPU", "GPU", "RAM", "DPI",
    "EP", "CH", "VOL", "PART", "DAY", "HOUR", "MIN", "SEC",
    "TUTOR", "TUTORI", "OPERAT", "STORY", "EPISOD", "STAGE", "CHAPTER",
})
_NON_STAGE_ANY_SEGMENT = frozenset({
    "VER", "REV", "UTC", "GMT", "FPS", "KB", "MB", "GB", "TB",
    "HTTP", "HTTPS", "PRTS", "MAA", "ADB", "EXP", "LVL",
    "TUTOR", "OPERAT", "EPISOD",
})


def is_valid_stage_code(code: str) -> bool:
    """Validate whether a normalized token is a legitimate Arknights stage code (mainline, event, or XX-XX-XX)."""
    s = str(code or "").strip().upper()
    if not s or "-" not in s:
        return False
    if not UNIVERSAL_STAGE_CODE_RE.match(s):
        return False
    parts = s.split("-")
    if not (2 <= len(parts) <= 3):
        return False
    if not re.search(r"\d", parts[-1]):
        return False
    if parts[0] in _NON_STAGE_FIRST_SEGMENTS:
        return False
    if any(p in _NON_STAGE_ANY_SEGMENT for p in parts):
        return False
    if re.fullmatch(r"V\d+", parts[0]):
        return False
    if len(parts) == 3 and parts[1].isdigit():
        return False
    if parts[0].isdigit():
        if len(parts[0]) >= 3:
            return False
        if len(parts[0]) == 2 and parts[0].startswith("0"):
            return False
    if parts[-1].isdigit():
        tail_n = int(parts[-1])
        if tail_n <= 0 or tail_n > 99:
            return False
        if parts[0].isdigit() and len(parts[-1]) >= 2 and parts[-1].startswith("0"):
            return False
    return True


def _normalize_stage_prefix(raw_prefix: str, expected_prefix: str = "") -> str:
    """Correct common OCR prefix confusions (e.g. TI/IL -> TL, 05 -> OS) when expected_prefix is known."""
    rp = str(raw_prefix or "").strip().upper()
    ep = str(expected_prefix or "").strip().upper().rstrip("-")
    if not rp or not ep:
        return rp
    if rp == ep:
        return ep
    if len(rp) != len(ep):
        return rp
    exact_hits = 0
    for rc, ec in zip(rp, ep):
        allowed = _STAGE_PREFIX_CONFUSIONS.get(ec, ec)
        if rc not in allowed:
            return rp
        if rc == ec:
            exact_hits += 1
    # For multi-char prefixes require >= 1 exact letter unless rp starts with '0' (05->OS) or contains non-digit (Q5->OS);
    # still reject non-zero-padded numeric mainline prefixes like 11->TL or 15->IS.
    if len(ep) >= 2 and exact_hits < 1:
        if not (rp.startswith("0") or not rp.isdigit()):
            return rp
    if len(ep) == 1 and exact_hits < 1:
        return rp
    return ep


def _normalize_numeric_tail(tail: str) -> str:
    """Normalize OCR digit confusions in stage numeric tail (I/l/|/]->1, Z->2, O/Q->0, S->5)."""
    s = str(tail or "").strip()
    if not s:
        return ""
    if all(ch in _STAGE_DIGIT_TAIL_CHARS for ch in s):
        return s.translate(_STAGE_DIGIT_TAIL_TRANS)
    return s.upper()


def _build_stage_ocr_regex(stage_code: str) -> re.Pattern[str]:
    parts: List[str] = []
    for ch in stage_code.strip().upper():
        if ch == "1":
            parts.append(r"[1Il\|\[\]!]")
        elif ch == "0":
            parts.append(r"[0OQ]")
        elif ch == "2":
            parts.append(r"[2Z]")
        elif ch == "5":
            parts.append(r"[5S]")
        elif ch in ("-", "_"):
            parts.append(r"[-—–−―_·•．.~=\s]*")
        else:
            parts.append(re.escape(ch))
    return re.compile(r"^" + "".join(parts) + r"$")


def _normalize_ocr_stage_token(text: str, expected_prefix: str = "") -> str:
    """Normalize OCR stage tokens: strip wrapper noise, unify separators, fix digit & prefix confusions."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    # Strip surrounding wrapper punctuation (e.g. '） TL-2' from card edge) while keeping trailing ']'/'|' for '-1'
    raw = re.sub(r"^[\s\(\)（）【】\{\}<>《》.,;:：'\"’‘`~·•，。、]+", "", raw)
    raw = re.sub(r"[\s\(\)（）【】\{\}<>《》.,;:：'\"’‘`~·•，。、]+$", "", raw)
    raw = re.sub(r"^[\[\]\|]+\s+", "", raw)
    # Convert CJK '一' (U+4E00) emitted by Chinese WinOCR for Arknights wide hyphens (e.g. 'OS 一 1') into '-'
    raw = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一\s*(?=[A-Za-z0-9\|\]\[!])", "-", raw)
    raw = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一$", "-", raw)
    raw = re.sub(r"^一\s*(?=[A-Za-z0-9\|\]\[!])", "-", raw)
    # Convert full-width/half-width period separators emitted by WinOCR (e.g. '16 ． 5' -> '16-5')
    raw = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*[．.]\s*(?=[A-Za-z0-9\|\]\[!])", "-", raw)
    # Unify dash/separator glyphs
    raw = re.sub(r"[—–−―－_~·•=¯]", "-", raw)
    raw = re.sub(r"\s*-\s*", "-", raw)

    exp_pref = str(expected_prefix or "").strip().upper().rstrip("-")
    if exp_pref and len(exp_pref) >= 2:
        spaced_pat = r"^(" + r"\s+".join(r"[A-Za-z0-9\|]" for _ in range(len(exp_pref))) + r")(?=\s|-|$)"
        m_sp = re.match(spaced_pat, raw)
        if m_sp:
            cand_pref = re.sub(r"\s+", "", m_sp.group(1)).upper()
            if _normalize_stage_prefix(cand_pref, exp_pref) == exp_pref:
                raw = exp_pref + raw[m_sp.end() :]

    # Convert space-separated stage forms ('TL 2', 'DH MO 1', 'OS EX 1', '16 2', 'TR 27', 'JT8 2') to hyphenated form
    m3 = re.match(
        r"^([A-Za-z0-9\|]{1,5})\s+([A-Za-z5][A-Za-z0-9]{0,4})\s+([0-9Il\|\[\]!OQSZ]{1,3})$",
        raw,
        re.IGNORECASE,
    )
    if m3 and (m3.group(2).upper() in ("ST", "5T", "TR", "EX", "MO", "S", "A", "B", "C", "D") or not m3.group(2).isdigit()):
        sub_seg = "ST" if m3.group(2).upper() == "5T" else m3.group(2)
        if not (m3.group(1).upper() in ("D", "I", "O") and sub_seg.isdigit()):
            raw = f"{m3.group(1)}-{sub_seg}-{m3.group(3)}"
    else:
        m2 = re.match(
            r"^([A-Za-z0-9\|]{1,5}(?:-[A-Za-z][A-Za-z0-9]{0,4})?)\s+([0-9Il\|\[\]!OQSZ]{1,3})$",
            raw,
            re.IGNORECASE,
        )
        if m2:
            raw = f"{m2.group(1)}-{m2.group(2)}"
        else:
            m2b = re.match(
                r"^([A-Za-z0-9\|]{1,5})\s+([A-Za-z][A-Za-z0-9]{0,4}-[0-9Il\|\[\]!OQSZ]{1,3})$",
                raw,
                re.IGNORECASE,
            )
            if m2b:
                raw = f"{m2b.group(1)}-{m2b.group(2)}"

    compact = re.sub(r"\s+", "", raw)

    if "-" in compact:
        parts = [p for p in compact.split("-") if p]
        if len(parts) >= 2:
            if len(parts) == 2:
                m_join = re.match(r"^(ST|5T|TR|EX|MO)([0-9Il\|\[\]!OQSZ]{1,3})$", parts[1], re.IGNORECASE)
                if m_join:
                    sub_tag = "ST" if m_join.group(1).upper() == "5T" else m_join.group(1).upper()
                    parts = [parts[0], sub_tag, m_join.group(2)]
            parts[-1] = _normalize_numeric_tail(parts[-1])
            parts = [p.upper() for p in parts]
            if len(parts) == 3 and parts[1] == "5T":
                parts[1] = "ST"
            if exp_pref:
                parts[0] = _normalize_stage_prefix(parts[0], exp_pref)
            return "-".join(parts)

    # Handle missing separator when expected_prefix is known (e.g. 'TL2', 'TI2', 'TLZ')
    if exp_pref and len(exp_pref) >= 2 and len(compact) > len(exp_pref):
        head = compact[: len(exp_pref)].upper()
        tail = compact[len(exp_pref) :]
        if _normalize_stage_prefix(head, exp_pref) == exp_pref:
            if all(ch in _STAGE_DIGIT_TAIL_CHARS for ch in tail) and len(tail) <= 3:
                return f"{exp_pref}-{_normalize_numeric_tail(tail)}"
            m_sub = re.match(r"^(ST|TR|EX|MO|S)-?([0-9Il\|\[\]!OQSZ]{1,3})$", tail, re.IGNORECASE)
            if m_sub:
                return f"{exp_pref}-{m_sub.group(1).upper()}-{_normalize_numeric_tail(m_sub.group(2))}"

    t = compact.upper()
    t = re.sub(r"-\]", "-1", t)
    t = re.sub(r"-I(?=$|-)", "-1", t)
    t = re.sub(r"(?<=-)I(?=\d|$)", "1", t)
    t = re.sub(r"(?<=\d)I(?=$|-|\d)", "1", t)
    t = re.sub(r"(?<=-)O(?=\d|$)", "0", t)
    t = re.sub(r"(?<=\d)O", "0", t)
    return t


def extract_universal_stage_code(raw_text: str, expected_prefix: str = "") -> str:
    """Extract & validate a full-format stage code (mainline, event, or XX-XX-XX) from OCR text."""
    exp_pref = str(expected_prefix or "").strip().upper().rstrip("-")
    pre = str(raw_text or "").strip()
    if not pre:
        return ""
    pre = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一\s*(?=[A-Za-z0-9\|\]\[!])", "-", pre)
    pre = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一(?=\s|$)", "-", pre)
    pre = re.sub(r"^一\s*(?=[A-Za-z0-9\|\]\[!])", "-", pre)
    pre = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*[．.]\s*(?=[A-Za-z0-9\|\]\[!])", "-", pre)
    pre = re.sub(r"[—–−―－_~·•=¯]", "-", pre)
    pre = re.sub(r"\s*-\s*", "-", pre)
    for noise in ("OPERATION", "OPERAT1ON", "TUTORIAL", "STORY", "EPISODE"):
        pre = re.sub(re.escape(noise), " ", pre, flags=re.IGNORECASE)

    # Check whitespace-separated tokens first so icon noise like 'D 16-2' yields '16-2', not 'D16-2'
    ws_tokens = [t for t in pre.split() if t]
    if len(ws_tokens) > 1:
        for tok in ws_tokens:
            tok_norm = _normalize_ocr_stage_token(tok, expected_prefix=exp_pref)
            if tok_norm and "-" in tok_norm:
                parts = tok_norm.split("-")
                if re.fullmatch(r"0[A-Z]{1,3}", parts[0]):
                    parts[0] = "O" + parts[0][1:]
                    tok_norm = "-".join(parts)
                if is_valid_stage_code(tok_norm):
                    return tok_norm

    norm = _normalize_ocr_stage_token(raw_text, expected_prefix=exp_pref)
    if norm and "-" in norm:
        parts = norm.split("-")
        # Safely fix OCR '0S-1' -> 'OS-1' ONLY when prefix matches ^0[A-Z]{1,3}$ (never touch numeric '0-1' or 'S1-1')
        if re.fullmatch(r"0[A-Z]{1,3}", parts[0]):
            parts[0] = "O" + parts[0][1:]
            norm = "-".join(parts)
        if is_valid_stage_code(norm):
            return norm

    cleaned_norm = _normalize_ocr_stage_token(pre, expected_prefix=exp_pref)
    for text_cand in (norm, cleaned_norm, pre.upper()):
        if not text_cand:
            continue
        for m in _UNIVERSAL_STAGE_SEARCH_RE.finditer(text_cand):
            cand = m.group(1).upper()
            parts = cand.split("-")
            if re.fullmatch(r"0[A-Z]{1,3}", parts[0]):
                cand = "O" + cand[1:]
            if is_valid_stage_code(cand):
                return cand
    return ""


def _match_stage_token_fuzzy(raw_text: str, target_stage: str) -> bool:
    """Check whether raw OCR text matches target_stage after separator, digit, and prefix normalization."""
    target = str(target_stage or "").strip().upper()
    if not target:
        return False
    exp_pref = target.split("-", 1)[0] if "-" in target else ""
    norm_pref = _normalize_ocr_stage_token(raw_text, expected_prefix=exp_pref)
    if norm_pref == target:
        return True
    pat = _build_stage_ocr_regex(target)
    if pat.match(norm_pref):
        return True
    compact = re.sub(r"\s+", "", str(raw_text or "").upper())
    compact = re.sub(r"(?<=[A-Z0-9\|\]\[!])[．.](?=[A-Z0-9\|\]\[!])", "-", compact)
    compact = re.sub(r"[—–−―_~·•=]", "-", compact)
    compact = re.sub(r"^[\(\)（）【】\{\}<>《》.,;:：'\"]+", "", compact)
    compact = re.sub(r"[\(\)（）【】\{\}<>《》.,;:：'\"]+$", "", compact)
    return bool(compact == target or pat.match(compact))


def find_text_boxes_via_ocr(
    img: Image.Image,
    roi: Optional[Tuple[int, int, int, int]] = None,
    include_inverted: bool = True,
    upscale_min_width: int = 1280,
    timeout: int = 12,
    cancel_event: Optional[threading.Event] = None,
    include_binarized: bool = False,
    upscale_factor: Optional[float] = None,
    pad_border: int = 0,
) -> List[Dict[str, Any]]:
    """Run WinOCR on full image or 1280x720 ROI and return text boxes normalized to 1280x720."""
    rgb = img.convert("RGB")
    ow, oh = max(1, rgb.width), max(1, rgb.height)

    if roi is not None:
        rx, ry, rw, rh = roi
        x0 = max(0, int(round(rx * ow / 1280.0)))
        y0 = max(0, int(round(ry * oh / 720.0)))
        x1 = min(ow, int(round((rx + rw) * ow / 1280.0)))
        y1 = min(oh, int(round((ry + rh) * oh / 720.0)))
        if x1 <= x0 + 4 or y1 <= y0 + 4:
            return []
        work_rgb = rgb.crop((x0, y0, x1, y1))
        off_x_1280 = float(rx)
        off_y_720 = float(ry)
        span_w_1280 = max(1.0, float(x1 - x0) * 1280.0 / ow)
        span_h_720 = max(1.0, float(y1 - y0) * 720.0 / oh)
    else:
        work_rgb = rgb
        off_x_1280 = 0.0
        off_y_720 = 0.0
        span_w_1280 = 1280.0
        span_h_720 = 720.0

    if upscale_factor is not None:
        scale_up = min(3.0, max(float(upscale_factor), float(upscale_min_width) / max(1, work_rgb.width)))
        ocr_rgb = work_rgb.resize(
            (max(1, int(round(work_rgb.width * scale_up))), max(1, int(round(work_rgb.height * scale_up)))),
            Image.Resampling.BICUBIC,
        )
    elif work_rgb.width < upscale_min_width:
        scale_up = min(2.5, float(upscale_min_width) / max(1, work_rgb.width))
        ocr_rgb = work_rgb.resize(
            (max(1, int(round(work_rgb.width * scale_up))), max(1, int(round(work_rgb.height * scale_up)))),
            Image.Resampling.BICUBIC,
        )
    else:
        ocr_rgb = work_rgb

    ocr_w, ocr_h = max(1, ocr_rgb.width), max(1, ocr_rgb.height)
    sx = span_w_1280 / ocr_w
    sy = span_h_720 / ocr_h

    border = max(0, int(pad_border))
    batch: List[Tuple[str, Image.Image]] = []
    if include_binarized:
        gray_ocr = ocr_rgb.convert("L")
        if np is not None:
            g_arr = np.asarray(gray_ocr, dtype=np.float32)
            hi = float(np.percentile(g_arr, 98))
            lo = float(np.percentile(g_arr, 20))
            thr_lo = int(max(130, min(162, round(lo + (hi - lo) * 0.52))))
            thr_hi = int(max(thr_lo + 20, min(195, round(lo + (hi - lo) * 0.68))))
        else:
            thr_lo, thr_hi = 150, 175
        bw_lo_im = gray_ocr.point(lambda p, t=thr_lo: 0 if p >= t else 255).convert("RGB")
        bw_hi_im = gray_ocr.point(lambda p, t=thr_hi: 0 if p >= t else 255).convert("RGB")
        if border > 0:
            bw_lo_im = ImageOps.expand(bw_lo_im, border=border, fill=(255, 255, 255))
            bw_hi_im = ImageOps.expand(bw_hi_im, border=border, fill=(255, 255, 255))
        batch.append(("bw_lo", bw_lo_im))
        batch.append(("bw_hi", bw_hi_im))
        if include_inverted:
            inv_im = ImageOps.invert(ocr_rgb)
            if border > 0:
                inv_im = ImageOps.expand(inv_im, border=border, fill=(255, 255, 255))
            batch.append(("inv", inv_im))
        norm_im = ImageOps.expand(ocr_rgb, border=border, fill=(0, 0, 0)) if border > 0 else ocr_rgb
        batch.append(("norm", norm_im))
    else:
        norm_im = ImageOps.expand(ocr_rgb, border=border, fill=(0, 0, 0)) if border > 0 else ocr_rgb
        batch.append(("norm", norm_im))
        if include_inverted:
            inv_im = ImageOps.invert(ocr_rgb)
            if border > 0:
                inv_im = ImageOps.expand(inv_im, border=border, fill=(255, 255, 255))
            batch.append(("inv", inv_im))

    def _map_box(x0_p: float, y0_p: float, x1_p: float, y1_p: float) -> Tuple[int, int, int, int]:
        ux0 = max(0.0, min(float(ocr_w), x0_p - float(border)))
        uy0 = max(0.0, min(float(ocr_h), y0_p - float(border)))
        ux1 = max(ux0 + 1.0, min(float(ocr_w), x1_p - float(border)))
        uy1 = max(uy0 + 1.0, min(float(ocr_h), y1_p - float(border)))
        nx = int(round(off_x_1280 + ux0 * sx))
        ny = int(round(off_y_720 + uy0 * sy))
        nw = max(1, int(round((ux1 - ux0) * sx)))
        nh = max(1, int(round((uy1 - uy0) * sy)))
        return nx, ny, nw, nh

    ocr_res = run_win_ocr_on_images(batch, timeout=timeout, cancel_event=cancel_event)
    found: List[Dict[str, Any]] = []
    seen_keys: set[Tuple[Any, ...]] = set()

    for pass_name, _ in batch:
        lines = ocr_res.get(pass_name) or []
        if isinstance(lines, dict):
            lines = [lines]
        for ln in lines:
            words = ln.get("words") or []
            if isinstance(words, dict):
                words = [words]
            line_text = str(ln.get("text", "") or "").strip()
            if words and line_text:
                xs = [int(w.get("x", 0)) for w in words]
                ys = [int(w.get("y", 0)) for w in words]
                x2s = [int(w.get("x", 0)) + int(w.get("w", 0)) for w in words]
                y2s = [int(w.get("y", 0)) + int(w.get("h", 0)) for w in words]
                nx, ny, nw, nh = _map_box(min(xs), min(ys), max(x2s), max(y2s))
                compact = re.sub(r"\s+", "", line_text)
                dedup = (
                    (pass_name, "line", compact, nx // 12, ny // 12)
                    if include_binarized
                    else (compact, nx // 12, ny // 12)
                )
                if dedup not in seen_keys:
                    seen_keys.add(dedup)
                    found.append({
                        "text": line_text,
                        "compact": compact,
                        "x": nx,
                        "y": ny,
                        "w": nw,
                        "h": nh,
                        "cx": nx + nw // 2,
                        "cy": ny + nh // 2,
                        "pass": pass_name,
                        "level": "line",
                    })
            for w in words:
                wt = str(w.get("text", "") or "").strip()
                if not wt:
                    continue
                wx = int(w.get("x", 0))
                wy = int(w.get("y", 0))
                ww = max(1, int(w.get("w", 1)))
                wh = max(1, int(w.get("h", 1)))
                nx, ny, nw, nh = _map_box(wx, wy, wx + ww, wy + wh)
                compact = re.sub(r"\s+", "", wt)
                dedup = (
                    (pass_name, "word", compact, nx // 12, ny // 12)
                    if include_binarized
                    else (compact, nx // 12, ny // 12)
                )
                if dedup not in seen_keys:
                    seen_keys.add(dedup)
                    found.append({
                        "text": wt,
                        "compact": compact,
                        "x": nx,
                        "y": ny,
                        "w": nw,
                        "h": nh,
                        "cx": nx + nw // 2,
                        "cy": ny + nh // 2,
                        "pass": pass_name,
                        "level": "word",
                    })
    return found


def detect_white_stage_cards(img: Image.Image) -> List[Dict[str, Any]]:
    """Detect bright pure-white rectangular stage nameplate cards in the main map area (1280x720 coords).

    Excludes top status bar, bottom border, top-left event stats/pill, and bottom-left currency stats.
    Uses morphological text-filling + thin-line erosion to separate cards from white connecting lines.
    """
    if np is None or img is None:
        return []
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    arr = np.asarray(img_1280, dtype=np.int16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    white = (r >= 208) & (g >= 208) & (b >= 208) & ((mx - mn) <= 26)

    # Downsample 2x (640x360) for fast morphological filtering
    s = white[::2, ::2].copy()
    # Exclude top status bar (y<96), bottom bar (y>624), top-left stats/search pill, bottom-left currency stats
    s[:48, :] = False
    s[312:, :] = False
    s[:115, :118] = False
    s[270:, :145] = False

    h, w = s.shape
    # Vertical close (bridge dark text inside 32px white card)
    up = np.zeros_like(s)
    dn = np.zeros_like(s)
    for d in range(1, 13):
        up[d:, :] |= s[:-d, :]
        dn[:-d, :] |= s[d:, :]
    v_filled = s | (up & dn)

    # Horizontal close (bridge vertical letter strokes / hyphens)
    lf = np.zeros_like(s)
    rt = np.zeros_like(s)
    for d in range(1, 9):
        lf[:, d:] |= v_filled[:, :-d]
        rt[:, :-d] |= v_filled[:, d:]
    filled = v_filled | (lf & rt)

    # Vertical erosion (k=9 in 640p = 18px in 1280p) eliminates thin diagonal white connecting lines
    cs_y = np.pad(filled.astype(np.int32), ((1, 0), (0, 0)), mode="constant").cumsum(axis=0)
    ky = 9
    er_y = np.zeros_like(filled)
    er_y[ky // 2 : h - (ky - 1 - ky // 2), :] = (cs_y[ky:, :] - cs_y[:-ky, :]) >= ky

    # Horizontal erosion (kx=22 in 640p = 44px in 1280p) removes narrow vertical background streaks/trees
    cs_x = np.pad(er_y.astype(np.int32), ((0, 0), (1, 0)), mode="constant").cumsum(axis=1)
    kx = 22
    er = np.zeros_like(er_y)
    er[:, kx // 2 : w - (kx - 1 - kx // 2)] = (cs_x[:, kx:] - cs_x[:, :-kx]) >= kx

    vis = np.zeros((h, w), dtype=bool)
    cards: List[Dict[str, Any]] = []
    ys, xs = np.nonzero(er)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if vis[sy, sx]:
            continue
        stack = [(sy, sx)]
        vis[sy, sx] = True
        minx = maxx = sx
        miny = maxy = sy
        while stack:
            cy, cx = stack.pop()
            if cx < minx:
                minx = cx
            if cx > maxx:
                maxx = cx
            if cy < miny:
                miny = cy
            if cy > maxy:
                maxy = cy
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and er[ny, nx] and not vis[ny, nx]:
                    vis[ny, nx] = True
                    stack.append((ny, nx))

        bx0 = max(0, (minx - kx // 2) * 2)
        bx1 = min(1280, (maxx + kx // 2 + 1) * 2)
        by0 = max(0, (miny - ky // 2) * 2)
        by1 = min(720, (maxy + ky // 2 + 1) * 2)

        # Tighten bounding box to actual pure-white card extent
        pad_x0, pad_x1 = max(0, bx0 - 8), min(1280, bx1 + 8)
        pad_y0, pad_y1 = max(0, by0 - 4), min(720, by1 + 4)
        sub_w = white[pad_y0:pad_y1, pad_x0:pad_x1]
        if sub_w.size == 0:
            continue
        good_rows = np.nonzero(sub_w.mean(axis=1) >= 0.45)[0]
        good_cols = np.nonzero(sub_w.mean(axis=0) >= 0.35)[0]
        if len(good_rows) == 0 or len(good_cols) == 0:
            continue
        ry0 = pad_y0 + int(good_rows[0])
        ry1 = pad_y0 + int(good_rows[-1]) + 1
        rx0 = pad_x0 + int(good_cols[0])
        rx1 = pad_x0 + int(good_cols[-1]) + 1
        rw, rh = rx1 - rx0, ry1 - ry0
        aspect = rw / max(1.0, float(rh))
        if not (65 <= rw <= 230 and 20 <= rh <= 58 and 1.85 <= aspect <= 7.0):
            continue

        card_crop = arr[ry0:ry1, rx0:rx1]
        if card_crop.size == 0:
            continue
        w_ratio = float(white[ry0:ry1, rx0:rx1].mean())
        dark_ratio = float(((card_crop[:, :, 0] < 120) & (card_crop[:, :, 1] < 120) & (card_crop[:, :, 2] < 120)).mean())
        top_strip = arr[max(0, ry0 - 14) : max(1, ry0 - 2), rx0 + 8 : max(rx0 + 10, rx1 - 8)]
        top_dark = float((top_strip.mean(axis=2) < 95).mean()) if top_strip.size else 0.0
        if w_ratio < 0.55 or (dark_ratio < 0.015 and top_dark < 0.35):
            continue

        box_info: Dict[str, Any] = {
            "x": rx0,
            "y": ry0,
            "w": rw,
            "h": rh,
            "cx": rx0 + rw // 2,
            "cy": ry0 + rh // 2,
            "card_style": "white",
            "is_dark_card": False,
            "white_ratio": round(w_ratio, 3),
            "dark_ratio": round(dark_ratio, 3),
            "top_dark_ratio": round(top_dark, 3),
        }
        badge_info = _inspect_stage_node_badge(img_1280, box_info, node_type="battle")
        box_info["cleared"] = bool(badge_info.get("has_badge"))
        box_info["badge_full_3star"] = bool(badge_info.get("badge_full_3star"))
        box_info["partial_star"] = bool(badge_info.get("badge_partial_star"))
        cards.append(box_info)

    cards.sort(key=lambda c: (c["x"], c["y"]))
    return cards


def _detect_left_book_or_film_icon(img_1280: Image.Image, rx0: int, ry0: int, rh: int) -> Tuple[bool, bool]:
    """Check immediately left of a stage nameplate for an open-book (tutorial) or film-reel (story) icon."""
    if np is None or img_1280 is None:
        return False, False
    arr = np.asarray(img_1280.convert("RGB"), dtype=np.int16)
    ix0 = max(0, rx0 - 52)
    ix1 = max(ix0 + 4, min(1280, rx0 + 8))
    iy0 = max(0, ry0 - 20)
    iy1 = min(720, ry0 + rh + 12)
    crop = arr[iy0:iy1, ix0:ix1]
    if crop.size == 0:
        return False, False
    mx = crop.max(axis=2)
    mn = crop.min(axis=2)
    neutral = (mx - mn) <= 28
    dark_px = float((neutral & (mx <= 70)).mean())
    light_px = float((neutral & (mx >= 170)).mean())
    has_book = bool(dark_px >= 0.04 and light_px >= 0.12)
    has_film = bool(dark_px >= 0.08 and light_px >= 0.14)
    return has_book, has_film


def detect_dark_stage_cards(img: Image.Image) -> List[Dict[str, Any]]:
    """Detect dark/black rectangular stage nameplate cards (e.g. TUTORIAL TR-27, OS-TR-1, dark story nodes)."""
    if np is None or img is None:
        return []
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    arr = np.asarray(img_1280, dtype=np.int16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    dark = (mx <= 88) & ((mx - mn) <= 30)

    s = dark[::2, ::2].copy()
    # Exclude top HUD bar, bottom bar, top-left stats/pill, bottom-left currency, bottom-right Episode switcher
    s[:48, :] = False
    s[312:, :] = False
    s[:115, :118] = False
    s[270:, :145] = False
    s[285:, 460:] = False

    h, w = s.shape
    up = np.zeros_like(s)
    dn = np.zeros_like(s)
    for d in range(1, 13):
        up[d:, :] |= s[:-d, :]
        dn[:-d, :] |= s[d:, :]
    v_filled = s | (up & dn)

    lf = np.zeros_like(s)
    rt = np.zeros_like(s)
    for d in range(1, 9):
        lf[:, d:] |= v_filled[:, :-d]
        rt[:, :-d] |= v_filled[:, d:]
    filled = v_filled | (lf & rt)

    cs_y = np.pad(filled.astype(np.int32), ((1, 0), (0, 0)), mode="constant").cumsum(axis=0)
    ky = 9
    er_y = np.zeros_like(filled)
    er_y[ky // 2 : h - (ky - 1 - ky // 2), :] = (cs_y[ky:, :] - cs_y[:-ky, :]) >= ky

    cs_x = np.pad(er_y.astype(np.int32), ((0, 0), (1, 0)), mode="constant").cumsum(axis=1)
    kx = 22
    er = np.zeros_like(er_y)
    er[:, kx // 2 : w - (kx - 1 - kx // 2)] = (cs_x[:, kx:] - cs_x[:, :-kx]) >= kx

    vis = np.zeros((h, w), dtype=bool)
    cards: List[Dict[str, Any]] = []
    ys, xs = np.nonzero(er)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if vis[sy, sx]:
            continue
        stack = [(sy, sx)]
        vis[sy, sx] = True
        minx = maxx = sx
        miny = maxy = sy
        while stack:
            cy, cx = stack.pop()
            if cx < minx:
                minx = cx
            if cx > maxx:
                maxx = cx
            if cy < miny:
                miny = cy
            if cy > maxy:
                maxy = cy
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and er[ny, nx] and not vis[ny, nx]:
                    vis[ny, nx] = True
                    stack.append((ny, nx))

        bx0 = max(0, (minx - kx // 2) * 2)
        bx1 = min(1280, (maxx + kx // 2 + 1) * 2)
        by0 = max(0, (miny - ky // 2) * 2)
        by1 = min(720, (maxy + ky // 2 + 1) * 2)

        pad_x0, pad_x1 = max(0, bx0 - 8), min(1280, bx1 + 8)
        pad_y0, pad_y1 = max(0, by0 - 4), min(720, by1 + 4)
        sub_d = dark[pad_y0:pad_y1, pad_x0:pad_x1]
        if sub_d.size == 0:
            continue
        good_rows = np.nonzero(sub_d.mean(axis=1) >= 0.45)[0]
        good_cols = np.nonzero(sub_d.mean(axis=0) >= 0.35)[0]
        if len(good_rows) == 0 or len(good_cols) == 0:
            continue
        ry0 = pad_y0 + int(good_rows[0])
        ry1 = pad_y0 + int(good_rows[-1]) + 1
        rx0 = pad_x0 + int(good_cols[0])
        rx1 = pad_x0 + int(good_cols[-1]) + 1
        rw, rh = rx1 - rx0, ry1 - ry0
        aspect = rw / max(1.0, float(rh))
        if not (65 <= rw <= 230 and 22 <= rh <= 62 and 1.8 <= aspect <= 6.8):
            continue

        card_crop = arr[ry0:ry1, rx0:rx1]
        if card_crop.size == 0:
            continue
        d_ratio = float(dark[ry0:ry1, rx0:rx1].mean())
        bright_ratio = float(((card_crop[:, :, 0] >= 165) & (card_crop[:, :, 1] >= 165) & (card_crop[:, :, 2] >= 165)).mean())
        top_band = card_crop[: min(11, rh), 8 : max(10, rw - 8)]
        top_dark = float((top_band.mean(axis=2) < 75).mean()) if top_band.size else 0.0
        if d_ratio < 0.52 or not (0.02 <= bright_ratio <= 0.45) or top_dark < 0.45:
            continue

        has_book, _ = _detect_left_book_or_film_icon(img_1280, rx0, ry0, rh)
        box_info: Dict[str, Any] = {
            "x": rx0,
            "y": ry0,
            "w": rw,
            "h": rh,
            "cx": rx0 + rw // 2,
            "cy": ry0 + rh // 2,
            "card_style": "dark",
            "is_dark_card": True,
            "white_ratio": round(bright_ratio, 3),
            "dark_ratio": round(d_ratio, 3),
            "bright_text_ratio": round(bright_ratio, 3),
            "top_dark_ratio": round(top_dark, 3),
            "has_book_icon": has_book,
        }
        badge_info = _inspect_stage_node_badge(img_1280, box_info, node_type="tutorial")
        box_info["cleared"] = bool(badge_info.get("has_badge"))
        box_info["badge_full_3star"] = bool(badge_info.get("badge_full_3star"))
        box_info["partial_star"] = bool(badge_info.get("badge_partial_star"))
        cards.append(box_info)

    cards.sort(key=lambda c: (c["x"], c["y"]))
    return cards


def _detect_header_strip_stage_cards(img: Image.Image) -> List[Dict[str, Any]]:
    """Detect stage cards anchored by thin black OPERATION/TUTORIAL/STORY header bars (works on white snow maps)."""
    if np is None or img is None:
        return []
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    arr = np.asarray(img_1280, dtype=np.int16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    black_hdr = (r <= 36) & (g <= 36) & (b <= 36) & ((np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)) <= 16)
    # Restrict to main map playfield
    mask = np.zeros_like(black_hdr)
    mask[96:600, 12:1268] = black_hdr[96:600, 12:1268]
    mask[:230, :236] = False
    mask[540:, :290] = False
    mask[570:, 920:] = False

    # Horizontal close (bridge small white letters in header bar)
    lf = np.zeros_like(mask)
    rt = np.zeros_like(mask)
    for d in range(1, 5):
        lf[:, d:] |= mask[:, :-d]
        rt[:, :-d] |= mask[:, d:]
    hdr_closed = mask | (lf & rt)

    # Downsample 2x horizontally, keep full Y so thin 9-12px header bars stay crisp
    s = hdr_closed[:, ::2]
    sh, sw = s.shape
    cs_x = np.pad(s.astype(np.int32), ((0, 0), (1, 0)), mode="constant").cumsum(axis=1)
    kx = 18  # 36px min header width
    er = np.zeros_like(s)
    er[:, kx // 2 : sw - (kx - 1 - kx // 2)] = (cs_x[:, kx:] - cs_x[:, :-kx]) >= kx

    vis = np.zeros((sh, sw), dtype=bool)
    cards: List[Dict[str, Any]] = []
    ys, xs = np.nonzero(er)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if vis[sy, sx]:
            continue
        stack = [(sy, sx)]
        vis[sy, sx] = True
        minx = maxx = sx
        miny = maxy = sy
        while stack:
            cy, cx = stack.pop()
            if cx < minx:
                minx = cx
            if cx > maxx:
                maxx = cx
            if cy < miny:
                miny = cy
            if cy > maxy:
                maxy = cy
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < sh and 0 <= nx < sw and er[ny, nx] and not vis[ny, nx]:
                    vis[ny, nx] = True
                    stack.append((ny, nx))

        hx0 = max(0, (minx - kx // 2) * 2)
        hx1 = min(1280, (maxx + kx // 2 + 1) * 2)
        hy0 = max(0, miny)
        hy1 = min(720, maxy + 1)
        hw, hh = hx1 - hx0, hy1 - hy0
        if not (42 <= hw <= 175 and 6 <= hh <= 16):
            continue

        body_y0 = hy1
        body_y1 = min(720, hy1 + 34)
        if body_y1 - body_y0 < 18:
            continue
        body = arr[body_y0:body_y1, hx0:hx1]
        if body.size == 0:
            continue
        w_ratio = float(((body[:, :, 0] >= 200) & (body[:, :, 1] >= 200) & (body[:, :, 2] >= 200)).mean())
        d_ratio = float(((body[:, :, 0] <= 90) & (body[:, :, 1] <= 90) & (body[:, :, 2] <= 90)).mean())
        b_bright = float(((body[:, :, 0] >= 165) & (body[:, :, 1] >= 165) & (body[:, :, 2] >= 165)).mean())

        if w_ratio >= 0.55 and 0.02 <= d_ratio <= 0.45:
            card_style = "white"
            is_dark = False
            ry0, ry1 = body_y0, body_y1
        elif d_ratio >= 0.50 and 0.02 <= b_bright <= 0.45:
            card_style = "dark"
            is_dark = True
            ry0, ry1 = hy0, body_y1
        else:
            continue

        rx0, rx1 = hx0, hx1
        rw, rh = rx1 - rx0, ry1 - ry0
        has_book, has_film = _detect_left_book_or_film_icon(img_1280, rx0, ry0, rh)
        box_info: Dict[str, Any] = {
            "x": rx0,
            "y": ry0,
            "w": rw,
            "h": rh,
            "cx": rx0 + rw // 2,
            "cy": ry0 + rh // 2,
            "card_style": card_style,
            "is_dark_card": is_dark,
            "white_ratio": round(w_ratio if not is_dark else b_bright, 3),
            "dark_ratio": round(d_ratio, 3),
            "has_book_icon": has_book,
            "has_film_icon": has_film,
            "from_header_strip": True,
        }
        badge_info = _inspect_stage_node_badge(img_1280, box_info, node_type="tutorial" if is_dark else "battle")
        box_info["cleared"] = bool(badge_info.get("has_badge"))
        box_info["badge_full_3star"] = bool(badge_info.get("badge_full_3star"))
        box_info["partial_star"] = bool(badge_info.get("badge_partial_star"))
        cards.append(box_info)

    cards.sort(key=lambda c: (c["x"], c["y"]))
    return cards


def detect_stage_cards(img: Image.Image) -> List[Dict[str, Any]]:
    """Detect all stage nameplate cards (white, dark/black tutorial, and header-anchored cards on bright maps)."""
    white_cards = detect_white_stage_cards(img)
    dark_cards = detect_dark_stage_cards(img)
    hdr_cards = _detect_header_strip_stage_cards(img)
    combined: List[Dict[str, Any]] = list(white_cards) + list(dark_cards)
    for hc in hdr_cards:
        hcx, hcy = int(hc["cx"]), int(hc["cy"])
        dup = any(
            abs(hcx - int(c["cx"])) <= 45 and abs(hcy - int(c["cy"])) <= 22
            for c in combined
        )
        if not dup:
            combined.append(hc)
    combined.sort(key=lambda c: (c["x"], c["y"]))
    return combined


def _build_upscaled_binary_roi(
    rgb: Image.Image,
    x_1280: float,
    y_720: float,
    w_1280: float,
    h_720: float,
    border: int = 24,
) -> Optional[Tuple[Image.Image, Image.Image, Tuple[int, int, int, int], int, int, int]]:
    """Crop a 1280x720 ROI, upscale 2~3x, and return (padded_binary, padded_gray, rect, up_w, up_h, border)."""
    ow, oh = max(1, rgb.width), max(1, rgb.height)
    rx0 = max(0, int(round(x_1280 * ow / 1280.0)))
    ry0 = max(0, int(round(y_720 * oh / 720.0)))
    rx1 = min(ow, int(round((x_1280 + w_1280) * ow / 1280.0)))
    ry1 = min(oh, int(round((y_720 + h_720) * oh / 720.0)))
    rw, rh = rx1 - rx0, ry1 - ry0
    if rw < 16 or rh < 10:
        return None
    scale = 3.0 if rw < 320 else 2.0
    up_w = max(1, int(round(rw * scale)))
    up_h = max(1, int(round(rh * scale)))
    up = rgb.crop((rx0, ry0, rx1, ry1)).resize((up_w, up_h), Image.Resampling.BICUBIC)
    gray_im = up.convert("L")
    if np is not None:
        g_arr = np.asarray(gray_im, dtype=np.float32)
        hi = float(np.percentile(g_arr, 85))
        lo = float(np.percentile(g_arr, 2))
        thr = int(max(115, min(152, round((hi + lo) * 0.47))))
    else:
        thr = 145
    bw_im = gray_im.point(lambda p, t=thr: 255 if p > t else 0).convert("RGB")
    padded_bw = ImageOps.expand(bw_im, border=border, fill=(255, 255, 255))
    padded_gr = ImageOps.expand(gray_im.convert("RGB"), border=border, fill=(255, 255, 255))
    return padded_bw, padded_gr, (rx0, ry0, rw, rh), up_w, up_h, border


def _build_upscaled_inverted_binary_roi(
    rgb: Image.Image,
    x_1280: float,
    y_720: float,
    w_1280: float,
    h_720: float,
    border: int = 24,
) -> Optional[Tuple[Image.Image, Image.Image, Tuple[int, int, int, int], int, int, int]]:
    """Crop a dark-card 1280x720 ROI, upscale 2~3x, invert so white text becomes black-on-white, and binarize."""
    ow, oh = max(1, rgb.width), max(1, rgb.height)
    rx0 = max(0, int(round(x_1280 * ow / 1280.0)))
    ry0 = max(0, int(round(y_720 * oh / 720.0)))
    rx1 = min(ow, int(round((x_1280 + w_1280) * ow / 1280.0)))
    ry1 = min(oh, int(round((y_720 + h_720) * oh / 720.0)))
    rw, rh = rx1 - rx0, ry1 - ry0
    if rw < 16 or rh < 10:
        return None
    scale = 3.0 if rw < 320 else 2.0
    up_w = max(1, int(round(rw * scale)))
    up_h = max(1, int(round(rh * scale)))
    up_inv = ImageOps.invert(rgb.crop((rx0, ry0, rx1, ry1)).resize((up_w, up_h), Image.Resampling.BICUBIC))
    gray_im = up_inv.convert("L")
    if np is not None:
        g_arr = np.asarray(gray_im, dtype=np.float32)
        hi = float(np.percentile(g_arr, 88))
        lo = float(np.percentile(g_arr, 2))
        thr = int(max(105, min(160, round((hi + lo) * 0.45))))
    else:
        thr = 135
    bw_im = gray_im.point(lambda p, t=thr: 255 if p > t else 0).convert("RGB")
    padded_bw = ImageOps.expand(bw_im, border=border, fill=(255, 255, 255))
    padded_gr = ImageOps.expand(gray_im.convert("RGB"), border=border, fill=(255, 255, 255))
    return padded_bw, padded_gr, (rx0, ry0, rw, rh), up_w, up_h, border


def _parse_roi_ocr_lines_to_boxes(
    lines_raw: Any,
    crop_rect: Tuple[int, int, int, int],
    up_w: int,
    up_h: int,
    border: int,
    ow: int,
    oh: int,
    pass_name: str,
) -> List[Dict[str, Any]]:
    """Map WinOCR results from a padded/upscaled ROI image back to 1280x720 stage boxes."""
    lines = lines_raw or []
    if isinstance(lines, dict):
        lines = [lines]
    rx0, ry0, rw, rh = crop_rect
    out: List[Dict[str, Any]] = []

    def _map_rect(x0_p: float, y0_p: float, x1_p: float, y1_p: float) -> Tuple[int, int, int, int]:
        ux0 = max(0.0, min(float(up_w), x0_p - float(border)))
        uy0 = max(0.0, min(float(up_h), y0_p - float(border)))
        ux1 = max(ux0 + 1.0, min(float(up_w), x1_p - float(border)))
        uy1 = max(uy0 + 1.0, min(float(up_h), y1_p - float(border)))
        bx = int(round((rx0 + ux0 * rw / max(1, up_w)) * 1280.0 / max(1, ow)))
        by = int(round((ry0 + uy0 * rh / max(1, up_h)) * 720.0 / max(1, oh)))
        bw = max(1, int(round(((ux1 - ux0) * rw / max(1, up_w)) * 1280.0 / max(1, ow))))
        bh = max(1, int(round(((uy1 - uy0) * rh / max(1, up_h)) * 720.0 / max(1, oh))))
        return bx, by, bw, bh

    for ln in lines:
        line_text = str(ln.get("text", "") or "").strip()
        words = ln.get("words") or []
        if isinstance(words, dict):
            words = [words]
        if words and line_text:
            xs = [int(w.get("x", 0)) for w in words]
            ys = [int(w.get("y", 0)) for w in words]
            x2s = [int(w.get("x", 0)) + int(w.get("w", 0)) for w in words]
            y2s = [int(w.get("y", 0)) + int(w.get("h", 0)) for w in words]
            bx, by, bw, bh = _map_rect(min(xs), min(ys), max(x2s), max(y2s))
            out.append({
                "text": line_text,
                "norm": _normalize_ocr_stage_token(line_text),
                "x": bx,
                "y": by,
                "w": bw,
                "h": bh,
                "pass": pass_name,
            })
        elif line_text:
            bx = int(round(rx0 * 1280.0 / max(1, ow)))
            by = int(round(ry0 * 720.0 / max(1, oh)))
            bw = max(1, int(round(rw * 1280.0 / max(1, ow))))
            bh = max(1, int(round(rh * 720.0 / max(1, oh))))
            out.append({
                "text": line_text,
                "norm": _normalize_ocr_stage_token(line_text),
                "x": bx,
                "y": by,
                "w": bw,
                "h": bh,
                "pass": pass_name,
            })
        for w in words:
            wt = str(w.get("text", "") or "").strip()
            if not wt:
                continue
            wx = int(w.get("x", 0))
            wy = int(w.get("y", 0))
            ww = max(1, int(w.get("w", 1)))
            wh = max(1, int(w.get("h", 1)))
            bx, by, bw, bh = _map_rect(wx, wy, wx + ww, wy + wh)
            out.append({
                "text": wt,
                "norm": _normalize_ocr_stage_token(wt),
                "x": bx,
                "y": by,
                "w": bw,
                "h": bh,
                "pass": pass_name,
            })
    return out


def _dedup_stage_ocr_boxes(boxes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge & deduplicate stage OCR boxes from full-screen and zoomed ROI passes."""
    pass_priority = {
        "white_card_roi": 3,
        "dark_card_roi": 3,
        "op_roi": 3,
        "tut_inv": 2,
        "norm": 1,
        "inv": 1,
    }
    merged: List[Dict[str, Any]] = []
    for b in boxes:
        norm = str(b.get("norm", "") or "").strip()
        if not norm:
            continue
        cx = int(b.get("x", 0)) + int(b.get("w", 0)) // 2
        cy = int(b.get("y", 0)) + int(b.get("h", 0)) // 2
        raw_clean = re.sub(r"\s+", "", str(b.get("text", "") or "").upper())
        score = (
            1 if raw_clean == norm else 0,
            pass_priority.get(str(b.get("pass", "")), 0),
        )
        matched_idx: Optional[int] = None
        for idx, prev in enumerate(merged):
            if prev["norm"] != norm:
                continue
            pcx = int(prev["x"]) + int(prev["w"]) // 2
            pcy = int(prev["y"]) + int(prev["h"]) // 2
            if abs(cx - pcx) <= 28 and abs(cy - pcy) <= 20:
                matched_idx = idx
                break
        if matched_idx is None:
            merged.append(b)
        else:
            prev = merged[matched_idx]
            prev_clean = re.sub(r"\s+", "", str(prev.get("text", "") or "").upper())
            prev_score = (
                1 if prev_clean == prev["norm"] else 0,
                pass_priority.get(str(prev.get("pass", "")), 0),
            )
            if score > prev_score:
                merged[matched_idx] = b
    return merged


def find_stage_boxes_via_ocr(img: Image.Image) -> List[Dict[str, Any]]:
    """Detect stage-like tokens and bounding boxes normalized to 1280x720 via full-screen + zoomed white-card ROI OCR."""
    rgb = img.convert("RGB")
    ow, oh = max(1, rgb.width), max(1, rgb.height)
    # Keep native resolution (or upscale if smaller than 1920px wide) so small stage fonts stay crisp
    if ow < 1920:
        scale_up = 1920.0 / ow
        ocr_rgb = rgb.resize((1920, max(1, int(round(oh * scale_up)))), Image.Resampling.BICUBIC)
    else:
        ocr_rgb = rgb
    ocr_w, ocr_h = max(1, ocr_rgb.width), max(1, ocr_rgb.height)
    sx = 1280.0 / ocr_w
    sy = 720.0 / ocr_h

    inv_rgb = ImageOps.invert(ocr_rgb)
    batch: List[Tuple[str, Image.Image]] = [("norm", ocr_rgb), ("inv", inv_rgb)]

    # Visual white-card & dark-card ROI crops (upscaled 2~3x + binarized) bundled into the SAME WinOCR call
    white_roi_meta: List[Tuple[str, Tuple[int, int, int, int], int, int, int, str]] = []
    all_cards = detect_stage_cards(rgb)
    white_cards = [c for c in all_cards if not c.get("is_dark_card")]
    dark_cards = [c for c in all_cards if c.get("is_dark_card")]

    for idx, card in enumerate(white_cards[:12]):
        inset_l = 4 if card["w"] >= 80 else 2
        roi_pack = _build_upscaled_binary_roi(
            rgb,
            card["x"] + inset_l,
            card["y"] + 1,
            max(10, card["w"] - inset_l - 2),
            max(8, card["h"] - 2),
            border=24,
        )
        if roi_pack is not None:
            pad_bw, pad_gr, rect, up_w, up_h, border = roi_pack
            tag_bw = f"wc_{idx}_bw"
            tag_gr = f"wc_{idx}_gr"
            batch.append((tag_bw, pad_bw))
            batch.append((tag_gr, pad_gr))
            white_roi_meta.append((tag_bw, rect, up_w, up_h, border, "white_card_roi"))
            white_roi_meta.append((tag_gr, rect, up_w, up_h, border, "white_card_roi"))
        # Wide white cards (e.g. story node with attached left book icon) also get a right-trimmed text crop
        if card["w"] > 145:
            right_w = min(125, card["w"] - 40)
            roi_right = _build_upscaled_binary_roi(
                rgb,
                card["x"] + card["w"] - right_w,
                card["y"] + 1,
                max(10, right_w - 2),
                max(8, card["h"] - 2),
                border=24,
            )
            if roi_right is not None:
                pad_bw, _, rect, up_w, up_h, border = roi_right
                tag = f"wc_{idx}_rbw"
                batch.append((tag, pad_bw))
                white_roi_meta.append((tag, rect, up_w, up_h, border, "white_card_roi"))

    for idx, dcard in enumerate(dark_cards[:8]):
        inset_l = 4 if dcard["w"] >= 80 else 2
        # Skip top ~10px black TUTORIAL header strip so OCR locks squarely on the stage code
        body_off = 10 if dcard["h"] >= 32 else 2
        roi_dark = _build_upscaled_inverted_binary_roi(
            rgb,
            dcard["x"] + inset_l,
            dcard["y"] + body_off,
            max(10, dcard["w"] - inset_l - 2),
            max(10, dcard["h"] - body_off - 2),
            border=24,
        )
        if roi_dark is not None:
            pad_bw, pad_gr, rect, up_w, up_h, border = roi_dark
            tag_bw = f"dc_{idx}_bw"
            tag_gr = f"dc_{idx}_gr"
            batch.append((tag_bw, pad_bw))
            batch.append((tag_gr, pad_gr))
            white_roi_meta.append((tag_bw, rect, up_w, up_h, border, "dark_card_roi"))
            white_roi_meta.append((tag_gr, rect, up_w, up_h, border, "dark_card_roi"))
        roi_full_dark = _build_upscaled_inverted_binary_roi(
            rgb,
            dcard["x"] + inset_l,
            dcard["y"] + 1,
            max(10, dcard["w"] - inset_l - 2),
            max(10, dcard["h"] - 2),
            border=24,
        )
        if roi_full_dark is not None:
            pad_bw_f, _, rect_f, up_w_f, up_h_f, border_f = roi_full_dark
            tag_fbw = f"dc_{idx}_fbw"
            batch.append((tag_fbw, pad_bw_f))
            white_roi_meta.append((tag_fbw, rect_f, up_w_f, up_h_f, border_f, "dark_card_roi"))

    ocr_res = run_win_ocr_on_images(batch)

    found: List[Dict[str, Any]] = []
    for pass_name in ("norm", "inv"):
        lines = ocr_res.get(pass_name) or []
        if isinstance(lines, dict):
            lines = [lines]
        for ln in lines:
            words = ln.get("words") or []
            if isinstance(words, dict):
                words = [words]
            line_text = str(ln.get("text", "") or "")
            if words:
                xs = [int(w.get("x", 0)) for w in words]
                ys = [int(w.get("y", 0)) for w in words]
                x2s = [int(w.get("x", 0)) + int(w.get("w", 0)) for w in words]
                y2s = [int(w.get("y", 0)) + int(w.get("h", 0)) for w in words]
                nx = int(round(min(xs) * sx))
                ny = int(round(min(ys) * sy))
                nw = max(1, int(round((max(x2s) - min(xs)) * sx)))
                nh = max(1, int(round((max(y2s) - min(ys)) * sy)))
                found.append({
                    "text": line_text,
                    "norm": _normalize_ocr_stage_token(line_text),
                    "x": nx,
                    "y": ny,
                    "w": nw,
                    "h": nh,
                    "pass": pass_name,
                })
            for w in words:
                wt = str(w.get("text", "") or "")
                found.append({
                    "text": wt,
                    "norm": _normalize_ocr_stage_token(wt),
                    "x": int(round(int(w.get("x", 0)) * sx)),
                    "y": int(round(int(w.get("y", 0)) * sy)),
                    "w": max(1, int(round(int(w.get("w", 1)) * sx))),
                    "h": max(1, int(round(int(w.get("h", 1)) * sy))),
                    "pass": pass_name,
                })

    for tag, rect, up_w, up_h, border, roi_pname in white_roi_meta:
        found.extend(
            _parse_roi_ocr_lines_to_boxes(
                ocr_res.get(tag),
                rect,
                up_w,
                up_h,
                border,
                ow,
                oh,
                pass_name=roi_pname,
            )
        )

    # Secondary zoomed crops for dark TUTORIAL cards or uncovered OPERATION/STORY headers
    extra_crops: List[Tuple[str, Image.Image, Tuple[int, int, int, int], int, int, int, str]] = []
    seen_tut_centers: List[Tuple[int, int]] = []
    seen_op_centers: List[Tuple[int, int]] = []
    for b in list(found):
        up = b["text"].strip().upper()
        cx_1280 = b["x"] + b["w"] // 2
        cy_1280 = b["y"] + b["h"] // 2
        if "TUTOR" in up or up in ("TUTORIAL", "TUTORIR"):
            if any(abs(cx_1280 - px) < 60 and abs(cy_1280 - py) < 40 for px, py in seen_tut_centers):
                continue
            seen_tut_centers.append((cx_1280, cy_1280))
            rx0 = max(0, int((cx_1280 - 95) * ow / 1280.0))
            ry0 = max(0, int((cy_1280 - 14) * oh / 720.0))
            rx1 = min(ow, int((cx_1280 + 95) * ow / 1280.0))
            ry1 = min(oh, int((cy_1280 + 52) * oh / 720.0))
            if rx1 > rx0 + 20 and ry1 > ry0 + 15:
                sub = ImageOps.invert(rgb.crop((rx0, ry0, rx1, ry1)))
                sub = sub.resize((sub.width * 2, sub.height * 2), Image.Resampling.BICUBIC)
                extra_crops.append(
                    (f"tut_{len(extra_crops)}", sub, (rx0, ry0, rx1 - rx0, ry1 - ry0), sub.width, sub.height, 0, "tut_inv")
                )
        elif up in ("OPERATION", "OPERAT1ON", "STORY"):
            if any(abs(cx_1280 - px) < 60 and abs(cy_1280 - py) < 40 for px, py in seen_op_centers):
                continue
            seen_op_centers.append((cx_1280, cy_1280))
            # Skip if a stage code token was already read directly below this header
            has_child = any(
                "-" in str(ob.get("norm", ""))
                and abs((ob["x"] + ob["w"] // 2) - cx_1280) < 65
                and 2 <= ((ob["y"] + ob["h"] // 2) - cy_1280) <= 46
                for ob in found
            )
            if not has_child:
                roi_op = _build_upscaled_binary_roi(rgb, cx_1280 - 62, cy_1280 + 5, 124, 34, border=24)
                if roi_op is not None:
                    pad_bw, _, rect, up_w, up_h, border = roi_op
                    extra_crops.append((f"op_{len(extra_crops)}", pad_bw, rect, up_w, up_h, border, "op_roi"))

    if extra_crops:
        sub_ocr = run_win_ocr_on_images([(tag, im) for tag, im, _, _, _, _, _ in extra_crops])
        for tag, _, rect, up_w, up_h, border, pname in extra_crops:
            found.extend(
                _parse_roi_ocr_lines_to_boxes(
                    sub_ocr.get(tag),
                    rect,
                    up_w,
                    up_h,
                    border,
                    ow,
                    oh,
                    pass_name=pname,
                )
            )
    return _dedup_stage_ocr_boxes(found)


def _match_stage_center_from_boxes(
    boxes: List[Dict[str, Any]],
    target_stage: str,
    max_right_x: Optional[int] = None,
    max_cx: Optional[int] = None,
) -> Optional[Tuple[int, int, Dict[str, Any]]]:
    """Match target_stage from OCR boxes, optionally restricting to unobstructed left-map coordinates."""
    target = str(target_stage or "").strip().upper()
    if not target or not boxes:
        return None
    exp_pref = target.split("-", 1)[0] if "-" in target else ""

    def _box_in_bounds(b: Dict[str, Any]) -> bool:
        bx = int(b.get("x", 0))
        by = int(b.get("y", 0))
        bw = max(1, int(b.get("w", 0)))
        bh = max(1, int(b.get("h", 0)))
        cx = bx + bw // 2
        if max_right_x is not None:
            if bx < 10 or by < 72 or (bx + bw) > max_right_x or (by + bh) > 655:
                return False
        if max_cx is not None and cx > max_cx:
            return False
        return True

    best_hit: Optional[Tuple[int, int, Dict[str, Any]]] = None
    best_rank = (-1, -1)
    for b in boxes:
        if not _box_in_bounds(b):
            continue
        raw_t = str(b.get("text", "") or "")
        norm_b = str(b.get("norm", "") or "")
        norm_pref = _normalize_ocr_stage_token(raw_t, expected_prefix=exp_pref)
        extracted_b = extract_universal_stage_code(raw_t, expected_prefix=exp_pref)
        if norm_b == target or norm_pref == target or extracted_b == target or _match_stage_token_fuzzy(raw_t, target):
            cx = int(b["x"] + b["w"] // 2)
            cy = int(b["y"] + b["h"] // 2)
            rank = (
                1 if raw_t.strip().upper() == target else 0,
                1 if b.get("pass") in ("white_card_roi", "dark_card_roi", "op_roi", "tut_inv") else 0,
            )
            if rank > best_rank:
                best_rank = rank
                best_hit = (cx, cy, b)
    if best_hit is not None:
        return best_hit

    # Fallback: if target is a tutorial stage and OCR read 'TUTORIAL' header without bottom text, target sits right below header
    if classify_stage_node(target) == "tutorial":
        for b in boxes:
            if not _box_in_bounds(b):
                continue
            up = str(b.get("text", "") or "").strip().upper()
            if up in ("TUTORIAL", "TUTORIR", "TUTORIAT") or "TUTOR" in up:
                cx = int(b["x"] + b["w"] // 2)
                cy = int(min(700, b["y"] + b["h"] + 16))
                if max_cx is not None and cx > max_cx:
                    continue
                return cx, cy, b
    return None


def find_stage_center_on_image(img: Image.Image, target_stage: str) -> Optional[Tuple[int, int, Dict[str, Any]]]:
    """Find a stage label center in 1280x720 coordinates via WinOCR (with fuzzy normalization & white-card ROI)."""
    target = str(target_stage or "").strip().upper()
    if not target:
        return None
    boxes = find_stage_boxes_via_ocr(img)
    return _match_stage_center_from_boxes(boxes, target)


def _is_stage_like_obstacle_box(box: Dict[str, Any]) -> bool:
    """Return True if an OCR box in the left map region belongs to a clickable stage card or node label."""
    txt = str(box.get("text", "") or "").strip()
    norm = str(box.get("norm", "") or "").strip()
    up = txt.upper()
    if not txt and not norm:
        return False
    if "-" in norm or "-" in txt:
        return True
    if any(k in up for k in ("TUTOR", "OPERAT", "STORY", "EPISODE")):
        return True
    if _DETAIL_STAGE_CODE_RE.search(norm) or _DETAIL_STAGE_CODE_RE.search(up):
        return True
    # Edge-clipped stage card text such as '-1' or standalone prefix like 'OS'
    if re.match(r"^(?:-?\d{1,2}|[A-Z]{2,4})$", up) and int(box.get("h", 0)) >= 10:
        return True
    return False


def find_safe_blank_tap_point(
    img: Optional[Image.Image],
    stage_boxes: Optional[List[Dict[str, Any]]] = None,
    white_cards: Optional[List[Dict[str, Any]]] = None,
    extra_bboxes: Optional[List[Any]] = None,
    min_margin: int = 48,
) -> Tuple[int, int]:
    """Dynamically pick an unobstructed blank tap point (1280x720) to close the right detail drawer safely."""
    obstacles: List[Tuple[int, int, int, int]] = []

    def _add_inflated(x0: int, y0: int, x1: int, y1: int, pl: int, pt: int, pr: int, pb: int) -> None:
        obstacles.append((max(0, x0 - pl), max(0, y0 - pt), min(1280, x1 + pr), min(720, y1 + pb)))

    cards = list(white_cards) if white_cards is not None else (detect_stage_cards(img) if img is not None else [])
    for c in cards:
        cx0 = int(c.get("x", 0))
        cy0 = int(c.get("y", 0))
        cw = max(1, int(c.get("w", 0)))
        ch = max(1, int(c.get("h", 0)))
        if cx0 < 810:
            _add_inflated(cx0, cy0, cx0 + cw, cy0 + ch, 45, 24, 24, 18)

    boxes = list(stage_boxes) if stage_boxes is not None else (find_stage_boxes_via_ocr(img) if img is not None else [])
    for b in boxes:
        bx0 = int(b.get("x", 0))
        by0 = int(b.get("y", 0))
        bw = max(1, int(b.get("w", 0)))
        bh = max(1, int(b.get("h", 0)))
        if bx0 >= 810 or by0 < 68 or by0 > 655:
            continue
        if not _is_stage_like_obstacle_box(b):
            continue
        _add_inflated(bx0, by0, bx0 + bw, by0 + bh, 85, 28, 35, 22)

    for eb in (extra_bboxes or []):
        if isinstance(eb, dict):
            ex0 = int(eb.get("x", 0))
            ey0 = int(eb.get("y", 0))
            ew = max(1, int(eb.get("w", 0)))
            eh = max(1, int(eb.get("h", 0)))
            _add_inflated(ex0, ey0, ex0 + ew, ey0 + eh, 65, 28, 35, 22)
        elif isinstance(eb, (list, tuple)) and len(eb) == 4:
            ex0, ey0, ex1, ey1 = (int(eb[0]), int(eb[1]), int(eb[2]), int(eb[3]))
            _add_inflated(ex0, ey0, ex1, ey1, 45, 24, 24, 18)

    def _is_forbidden_ui(px: int, py: int) -> bool:
        if px < 45 or py < 82 or px > 720 or py > 665:
            return True
        if px < 395 and py < 82:
            return True
        if px < 320 and py > 545:
            return True
        if px < 145 and 115 <= py <= 355:
            return True
        return False

    def _min_dist_to_obstacles(px: int, py: int) -> float:
        if not obstacles:
            return 999.0
        best = 9999.0
        for x0, y0, x1, y1 in obstacles:
            dx = 0 if x0 <= px <= x1 else (x0 - px if px < x0 else px - x1)
            dy = 0 if y0 <= py <= y1 else (y0 - py if py < y0 else py - y1)
            d = math.hypot(dx, dy)
            if d < best:
                best = d
                if best == 0.0:
                    return 0.0
        return best

    candidates: List[Tuple[int, int, int]] = []
    # Tier 1: Preferred edge blank areas (far-left mid-lower ground, top nav-below corridor, mid-bottom open area)
    for px in (70, 120, 175, 235):
        for py in (375, 435, 495, 535):
            candidates.append((1, px, py))
    for px in (420, 485, 550, 615, 675, 710):
        for py in (86, 98):
            candidates.append((1, px, py))
    for px in (345, 420, 495, 570, 645, 705):
        for py in (575, 630):
            candidates.append((1, px, py))

    # Tier 2: General left-map fallback grid
    for px in range(80, 721, 55):
        for py in range(110, 650, 50):
            candidates.append((2, px, py))

    best_score: Optional[Tuple[int, int, float, float]] = None
    best_pt: Tuple[int, int] = (120, 435)
    for tier, px, py in candidates:
        if _is_forbidden_ui(px, py):
            continue
        d_min = _min_dist_to_obstacles(px, py)
        score = (
            1 if d_min >= float(min_margin) else 0,
            1 if tier == 1 else 0,
            round(min(d_min, 260.0), 1),
            d_min,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_pt = (px, py)
    return best_pt


def find_unknown_white_card_candidates(
    img: Image.Image,
    target_stage: str,
    known_nodes: Optional[List[Dict[str, Any]]] = None,
    white_cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Find unlabelled, uncleared white/dark stage cards to the right of cleared anchor node(s), nearest first."""
    target_up = str(target_stage or "").strip().upper()
    if not target_up or "-" not in target_up:
        return []
    target_type = classify_stage_node(target_up)

    if white_cards is not None:
        cards = list(white_cards)
    elif target_type == "tutorial":
        cards = [c for c in detect_stage_cards(img) if c.get("is_dark_card")]
    else:
        cards = detect_white_stage_cards(img)
        if not cards:
            cards = [c for c in detect_stage_cards(img) if not c.get("is_dark_card")]

    uncleared_cards = [c for c in cards if not c.get("cleared")]
    if not uncleared_cards:
        return []

    pref = target_up.rsplit("-", 1)[0]
    if known_nodes is not None:
        nodes = list(known_nodes)
    else:
        nodes = inspect_map_stage_nodes(img, pref + "-")
        if not any(n.get("cleared") for n in nodes):
            nodes = inspect_map_stage_nodes(img, "")
    cleared_nodes = [n for n in nodes if n.get("cleared")]
    if not cleared_nodes:
        return []

    # Choose best anchor cleared node: immediate predecessor (e.g. TL-1 for TL-2), else rightmost cleared node
    anchor: Optional[Dict[str, Any]] = None
    target_seq = _extract_stage_seq_num(target_up)
    if target_seq is not None and target_seq > 1 and classify_stage_node(target_up) == "battle":
        prev_code = f"{pref}-{target_seq - 1}"
        for nd in cleared_nodes:
            if str(nd.get("code", "")).upper() == prev_code:
                anchor = nd
                break
    if anchor is None:
        earlier_cleared = [
            nd for nd in cleared_nodes
            if target_seq is None
            or nd.get("node_type") != "battle"
            or (_extract_stage_seq_num(str(nd.get("code", ""))) or 0) < target_seq
        ]
        pool = earlier_cleared if earlier_cleared else cleared_nodes
        anchor = max(pool, key=lambda n: int(n.get("cx", n.get("x", 0))))

    anchor_cx = int(anchor.get("cx", anchor.get("x", 0)))
    anchor_cy = int(anchor.get("cy", 360))
    anchor_code = str(anchor.get("code", ""))

    # Exclude cards that already overlap an OCR-identified stage node whose code != target_up
    other_nodes = [n for n in nodes if str(n.get("code", "")).upper() != target_up]
    candidates: List[Dict[str, Any]] = []
    for c in uncleared_cards:
        ccx = int(c.get("cx", int(c["x"]) + int(c["w"]) // 2))
        ccy = int(c.get("cy", int(c["y"]) + int(c["h"]) // 2))
        if ccx <= anchor_cx + 20:
            continue
        overlaps_other = any(
            abs(ccx - int(on.get("cx", on.get("x", 0)))) < 55
            and abs(ccy - int(on.get("cy", 360))) < 30
            for on in other_nodes
        )
        if overlaps_other:
            continue
        dist = math.hypot(ccx - anchor_cx, ccy - anchor_cy)
        item = dict(c)
        item["anchor_code"] = anchor_code
        item["distance"] = round(dist, 1)
        candidates.append(item)

    candidates.sort(key=lambda c: (float(c["distance"]), int(c["cx"])))
    return candidates


def _rgb_to_hsv_np(rgb: Any) -> Tuple[Any, Any, Any]:
    """Convert uint8/float RGB array (H, W, 3) into Hue [0, 360), Saturation [0, 1], Value [0, 1]."""
    arr = np.asarray(rgb, dtype=np.float32) / 255.0
    r = arr[:, :, 0]
    g = arr[:, :, 1]
    b = arr[:, :, 2]
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    df = mx - mn
    h = np.zeros_like(mx)
    nz = df > 1e-6
    safe_df = np.where(nz, df, 1.0)
    mr = nz & (mx == r)
    mg = nz & (mx == g)
    mb = nz & (mx == b)
    h[mr] = (60.0 * ((g[mr] - b[mr]) / safe_df[mr]) + 360.0) % 360.0
    h[mg] = (60.0 * ((b[mg] - r[mg]) / safe_df[mg]) + 120.0) % 360.0
    h[mb] = (60.0 * ((r[mb] - g[mb]) / safe_df[mb]) + 240.0) % 360.0
    safe_mx = np.where(mx > 1e-6, mx, 1.0)
    s = np.where(mx > 1e-6, df / safe_mx, 0.0)
    v = mx
    return h, s, v


def _inspect_stage_node_badge(
    img_1280: Image.Image,
    box: Dict[str, Any],
    node_type: str = "",
) -> Dict[str, Any]:
    """Inspect left edge of a stage nameplate for a genuine cyan/gold clear hexagon badge (resistant to blue map bg)."""
    empty_res: Dict[str, Any] = {
        "has_badge": False,
        "badge_full_3star": False,
        "badge_partial_star": False,
        "cleared_3star": False,
        "badge_area": 0,
        "outer_ratio": 0.0,
    }
    if np is None or img_1280 is None:
        return empty_res

    arr = np.asarray(img_1280.convert("RGB"), dtype=np.uint8)
    bx = int(box.get("x", 0))
    by = int(box.get("y", 0))
    bw_box = max(1, int(box.get("w", 0)))
    bh_box = max(1, int(box.get("h", 0)))

    x0 = max(0, bx - 80)
    x1 = max(x0 + 4, min(1280, bx + 6))
    y0 = max(0, by - 22)
    y1 = min(720, by + bh_box + 20)
    crop = arr[y0:y1, x0:x1]
    if crop.size == 0:
        return empty_res

    H, S, V = _rgb_to_hsv_np(crop)
    # Genuine Arknights cyan clear hexagon sits around H=198-199 (synthetic test cyan ~206.5);
    # bright blue map backgrounds sit at H>=213 and are strictly excluded.
    cyan_mask = (H >= 182.0) & (H <= 211.5) & (S >= 0.50) & (V >= 0.55)
    gold_mask = (H >= 28.0) & (H <= 58.0) & (S >= 0.45) & (V >= 0.62)

    # Outer ambient background bands above/below the badge ROI for contrast verification
    outer_slices = []
    if y0 >= 6:
        outer_slices.append(arr[max(0, y0 - 20) : y0, x0:x1])
    if y1 <= 714:
        outer_slices.append(arr[y1 : min(720, y1 + 20), x0:x1])

    def _outer_ratio_for(kind: str) -> float:
        if not outer_slices:
            return 0.0
        ratios: List[float] = []
        for sl in outer_slices:
            if sl.size == 0:
                continue
            oH, oS, oV = _rgb_to_hsv_np(sl)
            if kind == "cyan":
                om = (oH >= 182.0) & (oH <= 211.5) & (oS >= 0.50) & (oV >= 0.55)
            else:
                om = (oH >= 28.0) & (oH <= 58.0) & (oS >= 0.45) & (oV >= 0.62)
            ratios.append(float(om.mean()))
        return max(ratios) if ratios else 0.0

    best_comp: Optional[Tuple[int, int, int, int, int, str, Any]] = None
    for kind, mask in (("cyan", cyan_mask), ("gold", gold_mask)):
        if int(mask.sum()) < 120:
            continue
        mh, mw = mask.shape
        visited = np.zeros((mh, mw), dtype=bool)
        ys_idx, xs_idx = np.nonzero(mask)
        for sy, sx in zip(ys_idx.tolist(), xs_idx.tolist()):
            if visited[sy, sx]:
                continue
            stack = [(sy, sx)]
            visited[sy, sx] = True
            minx = maxx = sx
            miny = maxy = sy
            area = 0
            while stack:
                cy, cx = stack.pop()
                area += 1
                if cx < minx:
                    minx = cx
                if cx > maxx:
                    maxx = cx
                if cy < miny:
                    miny = cy
                if cy > maxy:
                    maxy = cy
                for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                    if 0 <= ny < mh and 0 <= nx < mw and mask[ny, nx] and not visited[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
            cbw = maxx - minx + 1
            cbh = maxy - miny + 1
            if area < 120 or not (14 <= cbw <= 56 and 14 <= cbh <= 56):
                continue
            aspect = cbw / max(1.0, float(cbh))
            fill = float(area) / float(cbw * cbh)
            if not (0.45 <= aspect <= 2.1 and fill >= 0.35):
                continue
            # Must hug the left edge of the stage nameplate
            abs_minx = x0 + minx
            abs_maxx = x0 + maxx
            if abs_maxx < bx - 68 or abs_minx > bx + 2:
                continue
            if best_comp is None or area > best_comp[0]:
                best_comp = (area, minx, miny, maxx, maxy, kind, mask)

    if best_comp is None:
        return empty_res

    area, minx, miny, maxx, maxy, kind, mask = best_comp
    cbw = maxx - minx + 1
    cbh = maxy - miny + 1
    fill = float(area) / float(cbw * cbh)
    outer_r = _outer_ratio_for(kind)
    if (fill - outer_r) < 0.25:
        return empty_res

    sub_m = mask[miny : maxy + 1, minx : maxx + 1]
    sub_v = V[miny : maxy + 1, minx : maxx + 1]
    my, mx_q = max(1, cbh // 2), max(1, cbw // 2)
    quads = (
        (sub_m[:my, :mx_q], sub_v[:my, :mx_q]),
        (sub_m[:my, mx_q:], sub_v[:my, mx_q:]),
        (sub_m[my:, :mx_q], sub_v[my:, :mx_q]),
        (sub_m[my:, mx_q:], sub_v[my:, mx_q:]),
    )
    is_partial = False
    if str(node_type or "").lower() not in ("story", "tutorial"):
        for qm, qv in quads:
            if qm.size == 0:
                continue
            q_cov = float(qm.mean())
            q_dark = float((qv < 0.30).mean())
            if q_cov < 0.28 and q_dark >= 0.30:
                is_partial = True
                break

    return {
        "has_badge": True,
        "badge_full_3star": not is_partial,
        "badge_partial_star": is_partial,
        "cleared_3star": not is_partial,
        "badge_area": int(area),
        "outer_ratio": round(outer_r, 3),
    }


def _is_stage_node_unlocked_visual(
    img_1280: Image.Image,
    box: Dict[str, Any],
    node_type: str = "",
) -> Optional[bool]:
    """Inspect pixels immediately left of stage box for cyan/gold cleared hexagon icon (True = cleared)."""
    if np is None:
        return None
    info = _inspect_stage_node_badge(img_1280, box, node_type=node_type)
    return bool(info.get("has_badge"))


def _measure_stage_card_white_ratio(img_1280: Image.Image, box: Dict[str, Any]) -> float:
    """Measure bright white background ratio of a stage node card (high ratio = unlocked white battle card)."""
    if np is None:
        return 0.0
    arr = np.asarray(img_1280.convert("RGB"), dtype=np.int16)
    x0 = max(0, int(box["x"]) - 16)
    x1 = min(1280, int(box["x"]) + int(box["w"]) + 16)
    y0 = max(0, int(box["y"]) - 6)
    y1 = min(720, int(box["y"]) + int(box["h"]) + 6)
    crop = arr[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0
    white_mask = (crop[:, :, 0] > 195) & (crop[:, :, 1] > 195) & (crop[:, :, 2] > 195)
    return float(white_mask.mean())


def _measure_stage_card_dark_ratio(img_1280: Image.Image, box: Dict[str, Any]) -> float:
    """Measure dark nameplate background ratio around a stage token (high ratio = dark TUTORIAL/special card)."""
    if np is None or img_1280 is None:
        return 0.0
    arr = np.asarray(img_1280.convert("RGB"), dtype=np.int16)
    x0 = max(0, int(box["x"]) - 16)
    x1 = min(1280, int(box["x"]) + int(box["w"]) + 16)
    y0 = max(0, int(box["y"]) - 10)
    y1 = min(720, int(box["y"]) + int(box["h"]) + 8)
    crop = arr[y0:y1, x0:x1]
    if crop.size == 0:
        return 0.0
    mx = crop.max(axis=2)
    mn = crop.min(axis=2)
    dark_mask = (mx <= 92) & ((mx - mn) <= 30)
    return float(dark_mask.mean())


def _count_lit_stars_in_roi(crop_rgb: Any) -> int:
    """Count lit cyan/gold stars inside an RGB crop (supports real hexagonal stars and synthetic star icons)."""
    if np is None or crop_rgb is None or crop_rgb.size == 0:
        return 0
    H, S, V = _rgb_to_hsv_np(crop_rgb)
    cyan_star = (H >= 176.0) & (H <= 216.0) & (S >= 0.42) & (V >= 0.45)
    gold_star = (H >= 24.0) & (H <= 62.0) & (S >= 0.42) & (V >= 0.58)
    mask = cyan_star | gold_star
    total_lit = int(mask.sum())
    if total_lit < 36 or total_lit > int(mask.size * 0.60):
        return 0

    h, w = mask.shape
    row_counts = mask.sum(axis=1)
    active_rows = np.nonzero(row_counts >= 3)[0]
    if len(active_rows) == 0:
        return 0
    y0 = int(active_rows[0])
    y1 = int(active_rows[-1])
    bh = y1 - y0 + 1

    if bh >= 28:
        band = mask[y0 : y1 + 1, :]
        col_counts = band.sum(axis=0)
        active_cols = np.nonzero(col_counts >= 4)[0]
        if len(active_cols) == 0:
            return 0
        x_start = max(0, int(active_cols[0]) - 2)
        slot_w = max(44, min(68, int(round(bh * 1.04))))
        lit_slots = 0
        for idx in range(3):
            sx0 = x_start + idx * slot_w
            sx1 = min(w, sx0 + slot_w)
            if sx0 >= w or sx1 - sx0 < 12:
                break
            if float(band[:, sx0:sx1].mean()) >= 0.16:
                lit_slots += 1
        return min(3, lit_slots)

    # Smaller star icons (e.g. synthetic test stars ~17px tall): connected components merged horizontally
    visited = np.zeros((h, w), dtype=bool)
    blobs: List[Tuple[float, float, int, int, int]] = []
    ys_idx, xs_idx = np.nonzero(mask)
    for sy, sx in zip(ys_idx.tolist(), xs_idx.tolist()):
        if visited[sy, sx]:
            continue
        stack = [(sy, sx)]
        visited[sy, sx] = True
        min_x = max_x = sx
        min_y = max_y = sy
        area = 0
        sum_x = 0
        sum_y = 0
        while stack:
            cy, cx = stack.pop()
            area += 1
            sum_x += cx
            sum_y += cy
            if cx < min_x:
                min_x = cx
            if cx > max_x:
                max_x = cx
            if cy < min_y:
                min_y = cy
            if cy > max_y:
                max_y = cy
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        bw_c = max_x - min_x + 1
        bh_c = max_y - min_y + 1
        if area >= 12 and 4 <= bw_c <= 55 and 4 <= bh_c <= 55:
            ratio = bw_c / max(1.0, float(bh_c))
            if 0.35 <= ratio <= 2.6:
                blobs.append((sum_x / area, sum_y / area, bw_c, bh_c, area))

    if not blobs:
        return 0
    best_count = 0
    for _, ref_y, _, _, _ in blobs:
        row_xs = sorted(bx for bx, by, _, _, _ in blobs if abs(by - ref_y) <= 18.0)
        distinct: List[float] = []
        for bx in row_xs:
            if not distinct or (bx - distinct[-1]) >= 24.0:
                distinct.append(bx)
        if len(distinct) > best_count:
            best_count = len(distinct)
    return min(3, best_count)


def _detect_settlement_stars(
    img_1280_rgb: Image.Image,
    gray_1280: Any,
    maa_dir: Path,
    has_end_of_action: bool = False,
) -> Tuple[Optional[int], bool]:
    """Detect exact lit star count (1..3) on battle victory settlement screen using RGB slot verification."""
    if np is None or img_1280_rgb is None or gray_1280 is None:
        return None, False

    arr = np.asarray(img_1280_rgb, dtype=np.uint8)
    tpl_root = Path(maa_dir) / "resource" / "template"

    adv_ok, _, adv_xy = match_template_roi(
        gray_1280,
        tpl_root / "Battle/StageDrops/StageDrops-Stars-Adverse.png",
        (40, 250, 160, 160),
        threshold=0.78,
    )
    if adv_ok:
        ax, ay = adv_xy
        adv_crop = arr[max(0, ay - 46) : min(720, ay + 46), max(0, ax - 44) : min(1280, ax + 44)]
        if adv_crop.size > 0:
            H, S, V = _rgb_to_hsv_np(adv_crop)
            lit_adv = ((S >= 0.40) & (V >= 0.55)) | ((V >= 0.85) & (S < 0.25))
            if float(lit_adv.mean()) >= 0.12:
                return 3, True

    best_score = 0.0
    best_xy = (0, 0)
    best_size = (205, 58)
    for rel in ("Battle/StageDrops/StageDrops-Stars-3.png", "Battle/StageDrops/StageDrops-Stars-2.png"):
        tp = tpl_root / rel
        tpl_arr = _load_template_gray(tp)
        tw, th = (int(tpl_arr.shape[1]), int(tpl_arr.shape[0])) if tpl_arr is not None else (205, 58)
        _, sc, xy = match_template_roi(gray_1280, tp, (20, 235, 340, 170), threshold=0.68)
        if sc > best_score:
            best_score = sc
            best_xy = xy
            best_size = (tw, th)

    if best_score >= 0.68:
        tw, th = best_size
        cx, cy = best_xy
        x0 = max(0, cx - tw // 2)
        y0 = max(0, cy - th // 2)
        x1 = min(1280, x0 + tw)
        y1 = min(720, y0 + th)
        bar = arr[y0:y1, x0:x1]
        if bar.size > 0 and (x1 - x0) >= 30:
            H, S, V = _rgb_to_hsv_np(bar)
            bg_dark = float(np.median(V)) < 0.45
            cyan_lit = (H >= 176.0) & (H <= 216.0) & (S >= 0.42) & (V >= 0.45)
            gold_lit = (H >= 24.0) & (H <= 62.0) & (S >= 0.42) & (V >= 0.58)
            white_lit = (S < 0.20) & (V >= 0.82) if bg_dark else np.zeros_like(cyan_lit)
            lit_mask = cyan_lit | gold_lit | white_lit
            slot_w = max(1, (x1 - x0) // 3)
            lit_count = 0
            for idx in range(3):
                sx0 = idx * slot_w
                sx1 = (x1 - x0) if idx == 2 else (idx + 1) * slot_w
                slot_m = lit_mask[:, sx0:sx1]
                if slot_m.size > 0 and float(slot_m.mean()) >= 0.14:
                    lit_count += 1
            return (lit_count if lit_count > 0 else None), True

    if has_end_of_action:
        fallback_crop = arr[255:385, 35:335]
        cnt = _count_lit_stars_in_roi(fallback_crop)
        if cnt > 0:
            return cnt, True

    return None, False


STORY_CARD_MODAL_ROI: Tuple[int, int, int, int] = (230, 252, 820, 230)       # x∈[0.18, 0.82], y∈[0.35, 0.67]
STORY_CARD_LEFT_ROI: Tuple[int, int, int, int] = (220, 248, 276, 238)        # x∈[0.172, 0.388], y∈[0.344, 0.675]
STORY_CARD_RIGHT_BTN_ROI: Tuple[int, int, int, int] = (896, 252, 168, 230)   # x∈[0.70, 0.831], y∈[0.35, 0.67]
STORY_CARD_START_FALLBACK_XY: Tuple[int, int] = (979, 367)                   # normalized (0.765, 0.51)
STORY_CARD_DISMISS_XY: Tuple[int, int] = (110, 360)                          # blurred backdrop left of card & below '<'


def analyze_screen_state(
    img: Image.Image,
    maa_dir: Path,
    use_ocr_fallback: bool = True,
) -> Dict[str, Any]:
    """Analyze a screenshot (any 16:9 size) into normalized screen state flags and click anchors."""
    img_1280_rgb = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    gray_1280 = np.asarray(img_1280_rgb.convert("L"), dtype=np.float32) if np is not None else None

    in_formation, form_score, form_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/StartButton/StartButton2.png", (960, 320, 300, 390), 0.74, None),
            ("Battle/StartButton/StartButton2Adverse.png", (960, 320, 300, 390), 0.74, None),
        ],
    )
    in_formation_select, form_sel_score, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/Formation/BattleQuickFormation/BattleQuickFormationClear.png", (300, 595, 340, 125), 0.74, None),
            ("Battle/Formation/ClearSelection.png", (250, 580, 380, 140), 0.74, None),
            ("Battle/Formation/BattleQuickFormation/BattleQuickFormationCollapseRole.png", (1130, 0, 150, 140), 0.76, None),
            ("Battle/Formation/BattleQuickFormation/BattleQuickFormationExpandRole.png", (1130, 0, 150, 140), 0.76, None),
            ("Battle/Formation/BattleQuickFormation/BattleQuickFormationConfirm.png", (840, 595, 380, 125), 0.74, None),
            ("Battle/Formation/BattleQuickFormation/BattleQuickFormationRoleFiltering.png", (1130, 60, 150, 660), 0.76, None),
        ],
    )
    in_battle, battle_score, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/BattleFlag/BattleOfficiallyBegin.png", (1160, 15, 85, 75), 0.72, (100, 255))],
    )
    has_return, ret_score, ret_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("ReturnButton/Return.png", (0, 0, 180, 80), 0.72, None),
            ("ReturnButton/Return-White.png", (0, 0, 180, 80), 0.72, None),
        ],
    )
    has_home_menu, home_menu_score, home_menu_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("QuickSwitch/Menu.png", (150, 0, 220, 80), 0.72, None)],
    )
    has_skip_confirm, conf_score, conf_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/Plot/SkipThePreBattlePlotConfirm.png", (700, 270, 190, 280), 0.72, (35, 255)),
            ("MiniGame/RebuildingMandate/MiniGame@RM-TR-1@SkipConfirm.png", (680, 270, 220, 280), 0.72, None),
        ],
    )
    has_skip_btn, skip_score, skip_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/Plot/SkipThePreBattlePlot.png", (1080, 0, 200, 140), 0.72, (100, 255)),
            ("Roguelike/base/Roguelike@DialogSkip.png", (1080, 0, 200, 140), 0.72, (100, 255)),
            ("MiniGame/RebuildingMandate/MiniGame@RM-TR-1@Skip.png", (1080, 0, 200, 140), 0.72, None),
        ],
    )
    has_end_plot, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/Plot/EndOfPlot.png", (510, 0, 260, 220), 0.72, None)],
    )
    has_start_btn, start_score, start_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/StartButton/BattleStartNormal.png", (860, 240, 410, 470), 0.74, None),
            ("Battle/StartButton/BattleStartAdverse.png", (860, 240, 410, 470), 0.74, None),
            ("Battle/StartButton/BattleStartTrials.png", (860, 240, 410, 470), 0.74, None),
            ("Battle/StartButton/StartButton1.png", (980, 560, 290, 150), 0.74, None),
        ],
    )
    has_story_start_btn, story_start_score, story_start_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/StartButton/BattleStartPlot.png", (870, 240, 230, 260), 0.72, None)],
    )
    has_avatar_dialog, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/Plot/BattleAvatarDialog.png", (0, 350, 450, 370), 0.72, None)],
    )
    has_end_of_action, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/BattleFlag/EndOfAction.png", (0, 260, 120, 120), 0.72, None),
            ("Battle/BattleFlag/EndOfActionAnnihilation.png", (30, 30, 210, 220), 0.72, None),
        ],
    )
    settlement_stars, stars_matched = _detect_settlement_stars(
        img_1280_rgb, gray_1280, maa_dir, has_end_of_action=bool(has_end_of_action)
    )

    is_battle_failed = False
    is_battle_victory = bool(has_end_of_action or stars_matched or settlement_stars is not None)
    has_story_dialog = bool(has_avatar_dialog and not is_battle_victory)

    if (
        use_ocr_fallback
        and not in_battle
        and not in_formation
        and not in_formation_select
        and not has_return
        and not has_start_btn
        and not has_story_start_btn
    ):
        top_right = img_1280_rgb.crop((1060, 0, 1280, 130))
        top_right_up = top_right.resize((top_right.width * 2, top_right.height * 2), Image.Resampling.BICUBIC)
        center_pop = img_1280_rgb.crop((420, 240, 900, 560))
        banner_crop = img_1280_rgb.crop((20, 170, 920, 540))
        ocr_map = run_win_ocr_on_images(
            [("tr", top_right_up), ("mid", center_pop), ("banner", banner_crop)],
            timeout=8,
        )
        tr_text = " ".join(str(ln.get("text", "")) for ln in (ocr_map.get("tr") or [])).upper()
        mid_text = " ".join(str(ln.get("text", "")) for ln in (ocr_map.get("mid") or [])).upper()
        banner_text = " ".join(str(ln.get("text", "")) for ln in (ocr_map.get("banner") or [])).upper()
        settle_compact = re.sub(r"\s+", "", f"{banner_text} {mid_text}")

        # Priority guard: detect battle defeat settlement FIRST so it is NEVER mistaken for story dialog
        if any(
            tok in settle_compact
            for tok in (
                "行动失败",
                "任务失败",
                "作战失败",
                "MISSIONFAILED",
                "OPERATIONFAILED",
                "理智返还",
            )
        ):
            is_battle_failed = True
            is_battle_victory = False
            has_story_dialog = False
            has_skip_confirm = False
            # Only keep skip button if explicit template matched on a non-defeat frame
            has_skip_btn = False
        elif any(
            tok in settle_compact
            for tok in (
                "行动结束",
                "行动完成",
                "MISSIONACCOMPLISHED",
                "MISSIONRESULTS",
                "获得报酬",
            )
        ):
            is_battle_victory = True
            has_end_of_action = True
            has_story_dialog = False
            if settlement_stars is None:
                settlement_stars, _ = _detect_settlement_stars(
                    img_1280_rgb, gray_1280, maa_dir, has_end_of_action=True
                )

        if not is_battle_failed and not is_battle_victory:
            if not has_skip_btn and ("SKIP" in tr_text or "跳过" in tr_text):
                has_skip_btn = True
                skip_xy = (1200, 48)
            if not has_skip_confirm and (
                ("跳过" in mid_text and ("剧情" in mid_text or "是否" in mid_text or "确认" in mid_text))
                or ("SKIP" in mid_text and ("STORY" in mid_text or "CONFIRM" in mid_text or "剧情" in mid_text))
            ):
                has_skip_confirm = True
                conf_xy = (795, 495)
            # Tightened story-dialog detection: require explicit story HUD controls (AUTO/LOG/HIDE/剧情)
            # instead of loose dark-bottom-band luminance heuristics that misfire on dark defeat screens.
            if any(k in tr_text for k in ("AUTO", "LOG", "HIDE", "剧情")):
                has_story_dialog = True

    if is_battle_failed or is_battle_victory or has_end_of_action:
        has_story_dialog = False

    on_stage_map = bool(
        has_return
        and not in_formation
        and not in_formation_select
        and not in_battle
        and not has_skip_btn
        and not has_skip_confirm
        and not has_end_plot
        and not is_battle_failed
        and not is_battle_victory
        and not has_story_start_btn
    )

    return {
        "in_formation": bool(in_formation),
        "formation_xy": form_xy if in_formation else (1105, 515),
        "formation_score": round(form_score, 3),
        "in_formation_select": bool(in_formation_select),
        "formation_select_score": round(form_sel_score, 3),
        "in_battle": bool(in_battle),
        "battle_score": round(battle_score, 3),
        "has_return": bool(has_return),
        "return_xy": ret_xy if has_return else (89, 38),
        "return_score": round(ret_score, 3),
        "has_home_menu": bool(has_home_menu),
        "home_menu_xy": home_menu_xy if has_home_menu else (269, 37),
        "home_menu_score": round(home_menu_score, 3),
        "has_skip_button": bool(has_skip_btn),
        "skip_xy": skip_xy if has_skip_btn else (1200, 48),
        "skip_score": round(skip_score, 3),
        "has_skip_confirm": bool(has_skip_confirm),
        "confirm_xy": conf_xy if has_skip_confirm else (795, 495),
        "confirm_score": round(conf_score, 3),
        "has_end_of_plot": bool(has_end_plot),
        "has_stage_start_btn": bool(has_start_btn),
        "stage_start_xy": start_xy if has_start_btn else (1110, 650),
        "stage_start_score": round(start_score, 3),
        "has_story_start_btn": bool(has_story_start_btn),
        "story_start_xy": story_start_xy if has_story_start_btn else STORY_CARD_START_FALLBACK_XY,
        "story_start_score": round(story_start_score, 3),
        "has_story_dialog": bool(has_story_dialog),
        "is_battle_failed": bool(is_battle_failed),
        "is_battle_victory": bool(is_battle_victory),
        "has_end_of_action": bool(has_end_of_action),
        "settlement_stars": settlement_stars,
        "on_stage_map": on_stage_map,
        "orig_size": img.size,
    }


DETAIL_PANEL_TITLE_ROI: Tuple[int, int, int, int] = (835, 72, 230, 41)
DETAIL_PANEL_TITLE_FALLBACK_ROI: Tuple[int, int, int, int] = (805, 65, 290, 52)
_DETAIL_STAGE_CODE_RE = _UNIVERSAL_STAGE_SEARCH_RE


def _extract_stage_code_from_title_text(raw_text: str, expected_stage: str = "") -> str:
    """Extract normalized stage code (e.g. 'TL-2', 'OS-1', '14-20') from right detail panel header OCR text."""
    exp_up = str(expected_stage or "").strip().upper()
    exp_pref = exp_up.split("-", 1)[0] if "-" in exp_up else ""
    cleaned = str(raw_text or "")
    cleaned = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一\s*(?=[A-Za-z0-9\|\]\[!])", "-", cleaned)
    cleaned = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一(?=\s|$)", "-", cleaned)
    cleaned = re.sub(r"[—–−―－_¯]", "-", cleaned)
    for noise in ("OPERATION", "OPERAT1ON", "TUTORIAL", "STORY", "EPISODE"):
        cleaned = re.sub(re.escape(noise), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*-\s*", "-", cleaned)

    raw_tokens = [t for t in re.split(r"[\s:：/]+", cleaned) if t]
    tokens: List[str] = []
    for t in raw_tokens:
        st = re.sub(r"^[，,·•'’`\"\.]+|[，,·•'’`\"\.]+$", "", t)
        if st:
            tokens.append(st)
    cleaned_tok = " ".join(tokens)
    cleaned_tok = re.sub(r"(?<=[A-Za-z0-9\|\]\[!])\s*一\s*(?=[A-Za-z0-9\|\]\[!])", "-", cleaned_tok)
    cleaned_tok = re.sub(r"\s*-\s*", "-", cleaned_tok)
    tokens = [t for t in cleaned_tok.split() if t]

    # Combine adjacent split tokens (e.g. ['TL', '2'] -> 'TL 2', ['TL', 'TR', '1'] -> 'TL TR 1', ['O', 'S', '1'])
    chunks: List[str] = list(tokens)
    for i in range(len(tokens) - 1):
        chunks.append(f"{tokens[i]} {tokens[i + 1]}")
    for i in range(len(tokens) - 2):
        chunks.append(f"{tokens[i]} {tokens[i + 1]} {tokens[i + 2]}")
    if len(tokens) >= 4:
        for i in range(len(tokens) - 3):
            chunks.append(f"{tokens[i]} {tokens[i + 1]} {tokens[i + 2]} {tokens[i + 3]}")

    candidates: List[str] = []
    for tok in chunks:
        norm = _normalize_ocr_stage_token(tok, expected_prefix=exp_pref)
        for m in _DETAIL_STAGE_CODE_RE.finditer(norm):
            cand_c = m.group(1).upper()
            if is_valid_stage_code(cand_c) and cand_c not in candidates:
                candidates.append(cand_c)
    compact_all = _normalize_ocr_stage_token(cleaned_tok, expected_prefix=exp_pref)
    for m in _DETAIL_STAGE_CODE_RE.finditer(compact_all):
        cand_c = m.group(1).upper()
        if is_valid_stage_code(cand_c) and cand_c not in candidates:
            candidates.append(cand_c)

    if exp_up:
        for cand in candidates:
            if cand == exp_up or _match_stage_token_fuzzy(cand, exp_up):
                return exp_up
        for tok in chunks + [compact_all]:
            if _match_stage_token_fuzzy(tok, exp_up):
                return exp_up
    return candidates[0] if candidates else ""


def _extract_stage_code_from_title_boxes(
    title_boxes: List[Dict[str, Any]],
    expected_stage: str = "",
) -> Tuple[str, str]:
    """Assemble detail panel header OCR boxes left-to-right per pass and vote across passes."""
    exp_up = str(expected_stage or "").strip().upper()
    if not title_boxes:
        return "", ""

    has_coords = any("x" in b and "y" in b for b in title_boxes)
    if not has_coords:
        raw = " ".join(str(b.get("text", "")).strip() for b in title_boxes if str(b.get("text", "")).strip())
        return _extract_stage_code_from_title_text(raw, expected_stage=exp_up), raw

    label_words = {"OPERATION", "OPERAT1ON", "TUTORIAL", "STORY", "EPISODE"}
    op_bboxes: List[Tuple[int, int, int, int]] = []
    by_pass: Dict[str, List[Dict[str, Any]]] = {}

    for b in title_boxes:
        txt = str(b.get("text", "") or "").strip()
        if not txt:
            continue
        up_clean = re.sub(r"[^A-Z0-9]", "", txt.upper())
        if up_clean in label_words or "OPERA" in up_clean:
            bx = int(b.get("x", 0))
            by = int(b.get("y", 0))
            bw = max(1, int(b.get("w", 1)))
            bh = max(1, int(b.get("h", 1)))
            op_bboxes.append((bx, by, bx + bw, by + bh))
        p_name = str(b.get("pass", "norm") or "norm")
        by_pass.setdefault(p_name, []).append(b)

    pass_order = ["bw_lo", "bw_hi", "inv", "norm"]
    ordered_passes = [p for p in pass_order if p in by_pass] + [p for p in by_pass if p not in pass_order]
    pass_priority = {"bw_lo": 4, "bw_hi": 3, "inv": 2, "norm": 1}

    def _overlaps_label_bbox(b: Dict[str, Any]) -> bool:
        if not op_bboxes:
            return False
        bx = int(b.get("x", 0))
        by = int(b.get("y", 0))
        bw = max(1, int(b.get("w", 1)))
        bh = max(1, int(b.get("h", 1)))
        cy = by + bh // 2
        for ox0, oy0, ox1, oy1 in op_bboxes:
            x_ov = max(0, min(bx + bw, ox1) - max(bx, ox0))
            y_ov = max(0, min(by + bh, oy1) - max(by, oy0))
            if x_ov / float(bw) >= 0.55 and (y_ov / float(bh) >= 0.35 or cy >= oy0 - 2):
                return True
        return False

    votes: Dict[str, Dict[str, Any]] = {}
    rendered_lines: List[str] = []

    for p_name in ordered_passes:
        p_boxes = by_pass[p_name]
        word_boxes = [b for b in p_boxes if b.get("level") == "word"]
        use_boxes = word_boxes if word_boxes else p_boxes

        filtered: List[Dict[str, Any]] = []
        for b in use_boxes:
            txt = str(b.get("text", "") or "").strip()
            if not txt:
                continue
            up_clean = re.sub(r"[^A-Z0-9]", "", txt.upper())
            if up_clean in label_words:
                continue
            if re.fullmatch(r"[·•，,\.'’\"`~]+", txt):
                continue
            if _overlaps_label_bbox(b):
                continue
            filtered.append(b)

        if not filtered:
            continue

        # Cluster boxes into horizontal rows by Y-center, then sort each row strictly left-to-right by (x, y)
        filtered.sort(key=lambda b: (int(b.get("cy", int(b.get("y", 0)) + int(b.get("h", 1)) // 2)), int(b.get("x", 0))))
        rows: List[Dict[str, Any]] = []
        for b in filtered:
            bh = max(1, int(b.get("h", 1)))
            bcy = int(b.get("cy", int(b.get("y", 0)) + bh // 2))
            placed = False
            for r in rows:
                tol = max(8, int(round(0.65 * max(bh, r["max_h"]))))
                if abs(bcy - r["cy"]) <= tol:
                    r["items"].append(b)
                    r["max_h"] = max(r["max_h"], bh)
                    r["cy"] = int(round(sum(int(it.get("cy", int(it.get("y", 0)) + int(it.get("h", 1)) // 2)) for it in r["items"]) / len(r["items"])))
                    placed = True
                    break
            if not placed:
                rows.append({"cy": bcy, "max_h": bh, "items": [b]})

        pass_best: Optional[Tuple[int, int, int, str, str]] = None
        for r in rows:
            items = sorted(r["items"], key=lambda it: (int(it.get("x", 0)), int(it.get("y", 0))))
            row_text = " ".join(str(it.get("text", "")).strip() for it in items if str(it.get("text", "")).strip())
            if not row_text:
                continue
            clean_parts = [
                re.sub(r"^[，,·•'’`\"\.]+|[，,·•'’`\"\.]+$", "", str(it.get("text", "")).strip())
                for it in items
            ]
            clean_row = " ".join(p for p in clean_parts if p)
            code = (
                _extract_stage_code_from_title_text(clean_row, expected_stage=exp_up)
                or _extract_stage_code_from_title_text(row_text, expected_stage=exp_up)
            )
            if not rendered_lines or row_text not in rendered_lines:
                rendered_lines.append(row_text)
            if not code:
                continue
            cand_rank = (
                1 if (exp_up and code == exp_up) else 0,
                len(code),
                -int(r["cy"]),
            )
            if pass_best is None or cand_rank > pass_best[:3]:
                pass_best = (*cand_rank, code, row_text)

        if pass_best is not None:
            win_code = pass_best[3]
            entry = votes.setdefault(
                win_code,
                {"count": 0, "best_prio": 0, "sample_row": pass_best[4]},
            )
            entry["count"] += 1
            entry["best_prio"] = max(entry["best_prio"], pass_priority.get(p_name, 0))

    raw_title_text = " | ".join(rendered_lines)
    if not votes:
        fallback_raw = " ".join(str(b.get("text", "")).strip() for b in title_boxes if str(b.get("text", "")).strip())
        return _extract_stage_code_from_title_text(fallback_raw, expected_stage=exp_up), (raw_title_text or fallback_raw)

    best_code = max(
        votes.keys(),
        key=lambda c: (
            votes[c]["count"],
            1 if (exp_up and c == exp_up) else 0,
            votes[c]["best_prio"],
            len(c),
        ),
    )
    return best_code, raw_title_text


def _count_detail_panel_stars(img_1280: Image.Image) -> int:
    """Count lit cyan/gold stars in the stage detail drawer top-right corner (excluding top sanity HUD)."""
    if np is None or img_1280 is None:
        return 0
    arr = np.asarray(img_1280.convert("RGB"), dtype=np.uint8)
    # y=78..162 excludes top sanity white text (y<75); x=1000..1245 covers all 3 star slots on both real & synthetic screens
    crop = arr[78:162, 1000:1245]
    return _count_lit_stars_in_roi(crop)


def _detect_detail_panel_three_stars(img_1280: Image.Image) -> bool:
    """Detect whether the stage detail drawer top-right corner displays 3 lit clear stars."""
    return _count_detail_panel_stars(img_1280) >= 3


def inspect_stage_detail_panel(
    img: Image.Image,
    maa_dir: Path,
    expected_stage: str = "",
    use_ocr: bool = True,
) -> Dict[str, Any]:
    """Verify the right-side stage detail panel title (OPERATION TL-X), 3-star status, and Auto-Deploy checkbox."""
    exp_up = str(expected_stage or "").strip().upper()
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    gray_1280 = np.asarray(img_1280.convert("L"), dtype=np.float32) if np is not None else None

    raw_title_text = ""
    displayed_stage = ""
    used_roi: Optional[Tuple[int, int, int, int]] = None
    if use_ocr:
        used_roi = DETAIL_PANEL_TITLE_ROI
        title_boxes = find_text_boxes_via_ocr(
            img,
            roi=DETAIL_PANEL_TITLE_ROI,
            include_inverted=True,
            include_binarized=True,
            upscale_factor=2.5,
            pad_border=32,
            upscale_min_width=760,
            timeout=10,
        )
        displayed_stage, raw_title_text = _extract_stage_code_from_title_boxes(title_boxes, expected_stage=exp_up)
        has_coord_boxes = any("x" in b and "y" in b for b in title_boxes)
        if not displayed_stage and (not title_boxes or has_coord_boxes):
            used_roi = DETAIL_PANEL_TITLE_FALLBACK_ROI
            fb_boxes = find_text_boxes_via_ocr(
                img,
                roi=DETAIL_PANEL_TITLE_FALLBACK_ROI,
                include_inverted=True,
                include_binarized=True,
                upscale_factor=2.5,
                pad_border=32,
                upscale_min_width=760,
                timeout=10,
            )
            fb_stage, fb_raw = _extract_stage_code_from_title_boxes(fb_boxes, expected_stage=exp_up)
            if fb_stage:
                displayed_stage = fb_stage
            if fb_raw and not raw_title_text:
                raw_title_text = fb_raw

    title_matches = bool(exp_up and displayed_stage and displayed_stage.upper() == exp_up)

    auto_deploy_locked, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/UsePrts/UnableToAgent2.png", (930, 540, 350, 120), 0.75, None)],
    )
    has_first_clear_reward, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/UsePrts/UnableToAgent.png", (800, 240, 480, 340), 0.76, None)],
    )
    has_prts_tpl, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/UsePrts/UsePrts.png", (930, 540, 350, 120), 0.74, None),
            ("Battle/UsePrts/NotUsePrts.png", (930, 540, 350, 120), 0.74, None),
            ("Battle/UsePrts/UsePrtsSuccess.png", (930, 540, 350, 120), 0.74, None),
        ],
    )
    has_prts_ocr = False
    if use_ocr and not has_prts_tpl and not auto_deploy_locked:
        prts_boxes = find_text_boxes_via_ocr(
            img,
            roi=(950, 540, 330, 120),
            include_inverted=True,
            upscale_min_width=480,
            timeout=8,
        )
        prts_text = "".join(str(b.get("compact", "")) for b in prts_boxes)
        if "代理指挥" in prts_text and "无法代理" not in prts_text and "不可" not in prts_text:
            has_prts_ocr = True

    has_auto_deploy = bool((has_prts_tpl or has_prts_ocr) and not auto_deploy_locked)

    auto_deploy_checked = False
    auto_deploy_toggle_xy: Optional[Tuple[int, int]] = None
    if has_auto_deploy and not auto_deploy_locked:
        chk_box_ok, _, chk_box_xy = match_template_roi(
            gray_1280,
            Path(maa_dir) / "resource" / "template" / "Battle/UsePrts/UsePrtsSuccess.png",
            (985, 545, 145, 95),
            threshold=0.75,
        )
        if chk_box_ok and gray_1280 is not None:
            bx0 = max(0, chk_box_xy[0] - 25)
            bx1 = min(1280, chk_box_xy[0] + 25)
            by0 = max(0, chk_box_xy[1] - 25)
            by1 = min(720, chk_box_xy[1] + 25)
            if float(gray_1280[by0:by1, bx0:bx1].mean()) >= 115.0:
                auto_deploy_checked = True
                auto_deploy_toggle_xy = chk_box_xy
        if not auto_deploy_checked:
            chk_bar_ok, _, chk_bar_xy = match_template_roi(
                gray_1280,
                Path(maa_dir) / "resource" / "template" / "Battle/UsePrts/NotUsePrts.png",
                (1000, 545, 280, 95),
                threshold=0.75,
            )
            if chk_bar_ok and gray_1280 is not None:
                bx0 = max(0, chk_bar_xy[0] - 50)
                bx1 = min(1280, chk_bar_xy[0] + 50)
                by0 = max(0, chk_bar_xy[1] - 18)
                by1 = min(720, chk_bar_xy[1] + 18)
                if float(gray_1280[by0:by1, bx0:bx1].mean()) >= 110.0:
                    auto_deploy_checked = True
                    auto_deploy_toggle_xy = chk_bar_xy

    star_count = _count_detail_panel_stars(img_1280)
    has_three_stars = star_count >= 3
    already_cleared_3star = bool(
        has_auto_deploy
        and not auto_deploy_locked
        and not has_first_clear_reward
        and has_three_stars
    )

    return {
        "displayed_stage": displayed_stage,
        "raw_title_text": raw_title_text,
        "expected_stage": exp_up,
        "title_matches": title_matches,
        "title_roi": used_roi,
        "star_count": star_count,
        "has_three_stars": has_three_stars,
        "has_auto_deploy": has_auto_deploy,
        "auto_deploy_locked": bool(auto_deploy_locked),
        "auto_deploy_checked": auto_deploy_checked,
        "auto_deploy_toggle_xy": auto_deploy_toggle_xy,
        "has_first_clear_reward": bool(has_first_clear_reward),
        "already_cleared_3star": already_cleared_3star,
    }


def detect_story_card_modal(
    img: Image.Image,
    maa_dir: Optional[Path] = None,
    expected_stage: str = "",
    use_ocr: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Detect the centered horizontal pure-story card modal (REC/code/STORY | synopsis | play + 开始剧情)."""
    exp_up = str(expected_stage or "").strip().upper()
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    gray_1280 = np.asarray(img_1280.convert("L"), dtype=np.float32) if np is not None else None

    left_dark_ratio = 0.0
    has_red_rec_accent = False
    mid_gray_ratio = 0.0
    right_blue_ratio = 0.0
    has_blue_start_block = False

    if np is not None:
        arr = np.asarray(img_1280, dtype=np.uint8)
        l_crop = arr[259:475, 230:486]
        if l_crop.size > 0:
            _, _, lV = _rgb_to_hsv_np(l_crop)
            left_dark_ratio = float((lV < 0.35).mean())
        rec_crop = arr[259:345, 230:420]
        if rec_crop.size > 0:
            rH, rS, rV = _rgb_to_hsv_np(rec_crop)
            has_red_rec_accent = bool(
                (((rH <= 20.0) | (rH >= 340.0)) & (rS >= 0.45) & (rV >= 0.45)).mean() >= 0.002
            )
        m_crop = arr[265:470, 495:895]
        if m_crop.size > 0:
            _, mS, mV = _rgb_to_hsv_np(m_crop)
            mid_gray_ratio = float(((mS < 0.22) & (mV >= 0.50) & (mV <= 0.96)).mean())
        r_crop = arr[262:472, 910:1048]
        if r_crop.size > 0:
            bH, bS, bV = _rgb_to_hsv_np(r_crop)
            right_blue_ratio = float(
                ((bH >= 182.0) & (bH <= 228.0) & (bS >= 0.42) & (bV >= 0.38)).mean()
            )
            has_blue_start_block = bool(right_blue_ratio >= 0.35)

    has_play_icon_tpl = False
    play_icon_score = 0.0
    play_icon_xy: Optional[Tuple[int, int]] = None
    if maa_dir is not None:
        has_play_icon_tpl, play_icon_score, pxy, _ = _match_any_template(
            gray_1280,
            Path(maa_dir),
            [("Battle/StartButton/BattleStartPlot.png", (870, 240, 230, 260), 0.72, None)],
        )
        if has_play_icon_tpl:
            play_icon_xy = pxy

    displayed_stage = ""
    raw_title_text = ""
    has_story_marker = False
    story_name = ""
    has_start_story_btn_ocr = False
    ocr_start_xy: Optional[Tuple[int, int]] = None
    raw_button_text = ""

    if use_ocr:
        left_boxes = find_text_boxes_via_ocr(
            img,
            roi=STORY_CARD_LEFT_ROI,
            include_inverted=True,
            include_binarized=True,
            upscale_factor=2.5,
            pad_border=24,
            upscale_min_width=640,
            timeout=10,
            cancel_event=cancel_event,
        )
        displayed_stage, raw_title_text = _extract_stage_code_from_title_boxes(left_boxes, expected_stage=exp_up)
        has_rec_word = False
        for b in left_boxes:
            txt = str(b.get("text", "") or "").strip()
            if not txt:
                continue
            up_alnum = re.sub(r"[^A-Z0-9]", "", txt.upper())
            if any(tok in up_alnum for tok in ("STORY", "ST0RY", "5TORY")):
                has_story_marker = True
            if "REC" in up_alnum:
                has_rec_word = True
            if not story_name and re.search(r"[\u4e00-\u9fff]{2,}", txt):
                story_name = txt
        if not has_story_marker and has_rec_word and displayed_stage and classify_stage_node(displayed_stage) == "story":
            has_story_marker = True

        right_boxes = find_text_boxes_via_ocr(
            img,
            roi=STORY_CARD_RIGHT_BTN_ROI,
            include_inverted=True,
            include_binarized=True,
            upscale_factor=2.5,
            pad_border=24,
            upscale_min_width=480,
            timeout=8,
            cancel_event=cancel_event,
        )
        raw_btn_parts: List[str] = []
        best_btn: Optional[Tuple[int, int, int, Dict[str, Any]]] = None
        for b in right_boxes:
            txt = str(b.get("text", "") or "").strip()
            if txt and txt not in raw_btn_parts:
                raw_btn_parts.append(txt)
            compact = re.sub(r"\s+", "", str(b.get("compact") or txt)).upper()
            if not compact:
                continue
            prio = 0
            if "开始剧情" in compact:
                prio = 3
            elif any(tok in compact for tok in ("开始剧", "始剧情", "STARTSTORY")):
                prio = 2
            elif "剧情" in compact and (has_blue_start_block or has_play_icon_tpl or has_story_marker):
                prio = 1
            if prio > 0:
                cand_rank = (prio, 1 if b.get("level") == "line" else 0, int(b.get("w", 0)))
                if best_btn is None or cand_rank > best_btn[:3]:
                    best_btn = (*cand_rank, b)
        raw_button_text = " | ".join(raw_btn_parts)
        if best_btn is not None:
            btn_box = best_btn[3]
            has_start_story_btn_ocr = True
            bx_c = int(btn_box.get("cx", int(btn_box.get("x", 979)) + int(btn_box.get("w", 0)) // 2))
            by_c = int(btn_box.get("cy", int(btn_box.get("y", 367)) + int(btn_box.get("h", 0)) // 2))
            ocr_start_xy = (max(908, min(1050, bx_c)), max(258, min(476, by_c)))

    if ocr_start_xy is not None:
        start_story_xy = ocr_start_xy
        start_story_xy_source = "ocr"
    elif has_play_icon_tpl and play_icon_xy is not None:
        start_story_xy = play_icon_xy
        start_story_xy_source = "template"
    else:
        start_story_xy = STORY_CARD_START_FALLBACK_XY
        start_story_xy_source = "fallback"

    has_right_start_cue = bool(has_start_story_btn_ocr or has_play_icon_tpl or has_blue_start_block)
    has_left_story_cue = bool(
        has_story_marker
        or (displayed_stage and classify_stage_node(displayed_stage) == "story")
        or (bool(displayed_stage) and left_dark_ratio >= 0.35)
        or (left_dark_ratio >= 0.40 and mid_gray_ratio >= 0.35)
    )
    is_story_card_modal = bool(
        has_right_start_cue and (has_start_story_btn_ocr or has_play_icon_tpl or has_left_story_cue)
    )
    title_matches = bool(
        exp_up
        and displayed_stage
        and (displayed_stage.upper() == exp_up or _match_stage_token_fuzzy(displayed_stage, exp_up))
    )

    return {
        "is_story_card_modal": is_story_card_modal,
        "displayed_stage": displayed_stage,
        "raw_title_text": raw_title_text,
        "expected_stage": exp_up,
        "title_matches": title_matches,
        "has_story_marker": has_story_marker,
        "has_start_story_btn": bool(has_start_story_btn_ocr or has_play_icon_tpl or has_blue_start_block),
        "has_start_story_btn_ocr": has_start_story_btn_ocr,
        "has_play_icon_tpl": bool(has_play_icon_tpl),
        "play_icon_score": round(play_icon_score, 3),
        "has_blue_start_block": has_blue_start_block,
        "right_blue_ratio": round(right_blue_ratio, 3),
        "left_dark_ratio": round(left_dark_ratio, 3),
        "mid_gray_ratio": round(mid_gray_ratio, 3),
        "has_red_rec_accent": has_red_rec_accent,
        "start_story_xy": start_story_xy,
        "start_story_xy_source": start_story_xy_source,
        "raw_button_text": raw_button_text,
        "story_name": story_name,
    }


def detect_abnormal_subpage(
    img: Image.Image,
    maa_dir: Path,
    stage_code: str = "",
    use_ocr: bool = True,
) -> Dict[str, Any]:
    """Detect whether the game is stuck on formation operator selection, operator detail, or other subpages."""
    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    gray_1280 = np.asarray(img_1280.convert("L"), dtype=np.float32) if np is not None else None

    base_info: Dict[str, Any] = {
        "is_abnormal": False,
        "page_type": "unknown",
        "reasons": [],
        "has_return": bool(st.get("has_return")),
        "return_xy": tuple(st.get("return_xy") or (89, 38)),
        "has_home_menu": bool(st.get("has_home_menu")),
        "home_menu_xy": tuple(st.get("home_menu_xy") or (269, 37)),
        "on_stage_map": bool(st.get("on_stage_map")),
    }

    if st.get("in_battle"):
        base_info["page_type"] = "battle"
        return base_info

    # 1. Formation operator selection page (清空选择 / 职业筛选栏)
    if st.get("in_formation_select"):
        base_info["is_abnormal"] = True
        base_info["page_type"] = "formation_select"
        base_info["on_stage_map"] = False
        base_info["reasons"].append("template:formation_select(清空选择/职业筛选栏)")
        return base_info

    # 2. Support operator selection page
    in_support_sel, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("Battle/Formation/SupportList/SupportList-Refresh.png", (980, 0, 300, 140), 0.74, None),
            ("Battle/Formation/SupportList/SupportList-RefreshCooldown.png", (980, 0, 300, 140), 0.74, None),
            ("Battle/Formation/SupportList/SupportList-DetailPanel-Flag.png", (0, 80, 420, 620), 0.74, None),
        ],
    )
    if in_support_sel:
        base_info["is_abnormal"] = True
        base_info["page_type"] = "support_select"
        base_info["on_stage_map"] = False
        base_info["reasons"].append("template:support_select")
        return base_info

    # 3. Formation squad overview page (stuck after BattleStartPre/BattleStartAll failure)
    if st.get("in_formation"):
        base_info["is_abnormal"] = True
        base_info["page_type"] = "formation_overview"
        base_info["on_stage_map"] = False
        base_info["reasons"].append("template:formation_overview(StartButton2)")
        return base_info

    # 4. OCR check for formation selection / operator detail / support selection
    if use_ocr and (st.get("has_return") or st.get("has_home_menu")):
        bottom_boxes = find_text_boxes_via_ocr(img, roi=(280, 610, 920, 110), include_inverted=False, timeout=8)
        bottom_txt = "".join(str(b.get("compact", "")) for b in bottom_boxes)
        left_boxes = find_text_boxes_via_ocr(img, roi=(0, 100, 420, 560), include_inverted=False, timeout=8)
        left_txt = "".join(str(b.get("compact", "")) for b in left_boxes)
        right_boxes = find_text_boxes_via_ocr(img, roi=(720, 0, 560, 460), include_inverted=False, timeout=8)
        right_txt = "".join(str(b.get("compact", "")) for b in right_boxes)
        all_txt = f"{bottom_txt} {left_txt} {right_txt}"

        if (
            "清空选择" in bottom_txt
            or "轻触右侧干员" in left_txt
            or "分支+" in left_txt
            or ("确认" in bottom_txt and any(k in all_txt for k in ("清空", "尖兵", "冲锋手", "战术家", "执旗手", "情报官", "策士", "收起", "稀有度", "信赖值")))
        ):
            base_info["is_abnormal"] = True
            base_info["page_type"] = "formation_select"
            base_info["on_stage_map"] = False
            base_info["reasons"].append("ocr:formation_select(清空选择/职业筛选栏)")
            return base_info

        if "助战干员" in all_txt or "刷新列表" in all_txt:
            base_info["is_abnormal"] = True
            base_info["page_type"] = "support_select"
            base_info["on_stage_map"] = False
            base_info["reasons"].append("ocr:support_select")
            return base_info

        detail_hits = [k for k in ("精英化", "技能专精", "干员档案", "模组", "天赋", "潜能", "信赖", "升级") if k in all_txt]
        if any(k in detail_hits for k in ("精英化", "技能专精", "干员档案")) or len(detail_hits) >= 2:
            base_info["is_abnormal"] = True
            base_info["page_type"] = "operator_detail"
            base_info["on_stage_map"] = False
            base_info["reasons"].append(f"ocr:operator_detail({','.join(detail_hits[:3])})")
            return base_info

    # 5. Stage map vs other unknown subpage vs Home screen
    if st.get("has_return"):
        if st.get("has_stage_start_btn"):
            base_info["page_type"] = "stage_map"
            base_info["on_stage_map"] = True
            return base_info
        if use_ocr:
            pref = ""
            if stage_code and "-" in str(stage_code):
                pref = str(stage_code).strip().upper().rsplit("-", 1)[0] + "-"
            map_nodes = inspect_map_stage_nodes(img, pref) if pref else []
            if not map_nodes:
                map_nodes = inspect_map_stage_nodes(img, "")
            if map_nodes:
                base_info["page_type"] = "stage_map"
                base_info["on_stage_map"] = True
                return base_info
            base_info["is_abnormal"] = True
            base_info["page_type"] = "other_subpage"
            base_info["on_stage_map"] = False
            base_info["reasons"].append("subpage_without_stage_nodes")
            return base_info
        base_info["page_type"] = "stage_map"
        return base_info

    if not st.get("has_home_menu"):
        base_info["page_type"] = "home"
        base_info["on_stage_map"] = False
        return base_info

    base_info["is_abnormal"] = True
    base_info["page_type"] = "other_subpage"
    base_info["on_stage_map"] = False
    base_info["reasons"].append("has_home_menu_without_map")
    return base_info


def recover_from_abnormal_subpage(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    stage_code: str = "",
    max_back_clicks: int = 3,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Detect if stuck on an abnormal subpage and reset back to stage map or Home screen."""
    if not adb_path or not adb_addr:
        return {"needed_recovery": False, "recovered": False, "reason": "no_adb", "actions": []}

    img = adb_screencap_image(adb_path, adb_addr)
    if img is None:
        return {"needed_recovery": False, "recovered": False, "reason": "screencap_failed", "actions": []}

    info = detect_abnormal_subpage(img, maa_dir, stage_code=stage_code, use_ocr=True)
    if not info.get("is_abnormal"):
        return {
            "needed_recovery": False,
            "recovered": False,
            "initial_page": info.get("page_type", "unknown"),
            "final_page": info.get("page_type", "unknown"),
            "actions": [],
        }

    initial_page = str(info.get("page_type") or "other_subpage")
    reasons_s = ", ".join(info.get("reasons") or [])
    _emit(
        log_callback,
        f"[RECOVER] 检测到滞留异常子页面 ({initial_page}: {reasons_s or 'unknown'})，"
        f"开始执行页面复位退回游戏主界面/活动地图...",
    )

    actions: List[str] = []
    cur_img = img
    cur_info = info

    # Step 1: Click '<' return button up to max_back_clicks (default 3) times until reaching map or home
    for click_idx in range(1, max(1, int(max_back_clicks)) + 1):
        if cancel_event and cancel_event.is_set():
            return {
                "needed_recovery": True,
                "recovered": False,
                "cancelled": True,
                "initial_page": initial_page,
                "final_page": cur_info.get("page_type", "unknown"),
                "actions": actions,
            }
        if not cur_info.get("has_return"):
            break
        rx, ry = cur_info.get("return_xy") or (89, 38)
        adb_tap_norm(adb_path, adb_addr, rx, ry, cur_img.width, cur_img.height)
        actions.append(f"tap_return_{click_idx}({rx},{ry})")
        _emit(log_callback, f"[RECOVER] 点击左上角返回按钮 '<' ({click_idx}/{max_back_clicks})...")
        time.sleep(0.9)

        next_img = adb_screencap_image(adb_path, adb_addr)
        if next_img is None:
            continue
        cur_img = next_img
        cur_info = detect_abnormal_subpage(cur_img, maa_dir, stage_code=stage_code, use_ocr=True)
        if not cur_info.get("is_abnormal"):
            _emit(log_callback, f"[RECOVER] 页面复位成功，已退回至 {cur_info.get('page_type')}。")
            return {
                "needed_recovery": True,
                "recovered": True,
                "initial_page": initial_page,
                "final_page": cur_info.get("page_type", "unknown"),
                "actions": actions,
            }

    # Step 2: Still abnormal -> click top-left Home house button (QuickSwitch) to return to Home screen
    if cancel_event and cancel_event.is_set():
        return {
            "needed_recovery": True,
            "recovered": False,
            "cancelled": True,
            "initial_page": initial_page,
            "final_page": cur_info.get("page_type", "unknown"),
            "actions": actions,
        }

    _emit(log_callback, "[RECOVER] 连续返回后仍滞留子页面，点击左上角主页按钮（小房子）强制返回游戏主界面...")
    if cur_info.get("has_home_menu"):
        hx, hy = cur_info.get("home_menu_xy") or (269, 37)
        adb_tap_norm(adb_path, adb_addr, hx, hy, cur_img.width, cur_img.height)
        actions.append(f"tap_home_menu({hx},{hy})")
        time.sleep(0.8)

    home_res = return_to_home_screen(
        adb_path,
        adb_addr,
        maa_dir,
        max_steps=4,
        cancel_event=cancel_event,
        log_callback=log_callback,
    )
    actions.append(f"return_to_home({home_res.get('method') or home_res.get('reason')})")

    final_img = adb_screencap_image(adb_path, adb_addr)
    if final_img is not None:
        cur_info = detect_abnormal_subpage(final_img, maa_dir, stage_code=stage_code, use_ocr=True)

    recovered = bool(home_res.get("ok") or not cur_info.get("is_abnormal"))
    final_page = "home" if home_res.get("ok") and cur_info.get("is_abnormal") else str(cur_info.get("page_type", "unknown"))
    if recovered:
        _emit(log_callback, f"[RECOVER] 已通过主页按钮完成页面复位，当前状态={final_page}。")
    else:
        _emit(log_callback, f"[RECOVER] 页面复位未完全成功，当前仍停留在 {final_page}。")
    return {
        "needed_recovery": True,
        "recovered": recovered,
        "initial_page": initial_page,
        "final_page": final_page,
        "actions": actions,
    }


def _classify_non_map_page(
    img: Image.Image,
    st: Dict[str, Any],
    *,
    use_ocr_fallback: bool = True,
) -> Tuple[str, bool]:
    """Classify whether current screen is on map/battle or stuck on formation/settlement subpage.

    Returns (page_type, needs_reset).
    """
    if st.get("in_battle"):
        return "battle", False
    if st.get("in_formation_select"):
        return "formation_select", True
    if st.get("in_formation"):
        return "formation_overview", True
    if st.get("is_battle_failed"):
        return "settlement_failed", True
    if st.get("is_battle_victory") or st.get("has_end_of_action") or st.get("settlement_stars") is not None:
        return "settlement_victory", True
    if st.get("on_stage_map") or st.get("has_stage_start_btn"):
        return "stage_map", False
    if st.get("has_story_start_btn") or st.get("has_skip_button") or st.get("has_skip_confirm") or st.get("has_story_dialog"):
        return "story_or_modal", False

    if use_ocr_fallback:
        top_boxes = find_text_boxes_via_ocr(img, roi=(760, 0, 520, 120), include_inverted=False, timeout=6)
        bottom_boxes = find_text_boxes_via_ocr(img, roi=(240, 580, 960, 140), include_inverted=False, timeout=6)
        banner_boxes = find_text_boxes_via_ocr(img, roi=(40, 170, 900, 380), include_inverted=False, timeout=6)
        top_txt = "".join(str(b.get("compact", "")) for b in top_boxes)
        bot_txt = "".join(str(b.get("compact", "")) for b in bottom_boxes)
        ban_txt = "".join(str(b.get("compact", "")) for b in banner_boxes).upper()
        all_txt = f"{top_txt} {bot_txt} {ban_txt}"

        if "清空选择" in bot_txt or (
            "确认" in bot_txt and any(k in all_txt for k in ("清空", "尖兵", "冲锋手", "战术家", "执旗手", "信赖值"))
        ):
            return "formation_select", True
        if "快捷编队" in top_txt or ("助战干员" in all_txt and ("开始行动" in bot_txt or "编队" in bot_txt)):
            return "formation_overview", True
        if any(tok in ban_txt for tok in ("行动失败", "任务失败", "作战失败", "MISSIONFAILED", "理智返还")):
            return "settlement_failed", True
        if any(tok in ban_txt for tok in ("行动结束", "行动完成", "获得报酬", "MISSIONACCOMPLISHED")):
            return "settlement_victory", True

    return "unknown", False


def ensure_on_stage_map_before_search(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    *,
    max_steps: int = 4,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    initial_img: Optional[Image.Image] = None,
    initial_state: Optional[Dict[str, Any]] = None,
    use_ocr_fallback: bool = True,
) -> Dict[str, Any]:
    """Ensure emulator is back on the chapter/event stage map before searching for stage nodes."""
    cur_img = initial_img
    if cur_img is None:
        if not adb_path or not adb_addr:
            return {"reset_needed": False, "recovered": False, "on_stage_map": False, "initial_page": "unknown", "final_page": "unknown", "actions": [], "image": None, "state": initial_state}
        cur_img = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
        if cur_img is None:
            return {"reset_needed": False, "recovered": False, "on_stage_map": False, "initial_page": "unknown", "final_page": "unknown", "actions": [], "image": None, "state": initial_state}

    cur_st = initial_state if initial_state is not None else analyze_screen_state(cur_img, maa_dir, use_ocr_fallback=False)
    page_type, needs_reset = _classify_non_map_page(cur_img, cur_st, use_ocr_fallback=use_ocr_fallback)
    initial_page = page_type
    if not needs_reset:
        return {
            "reset_needed": False,
            "recovered": False,
            "on_stage_map": bool(cur_st.get("on_stage_map") or page_type == "stage_map"),
            "initial_page": initial_page,
            "final_page": page_type,
            "actions": [],
            "image": cur_img,
            "state": cur_st,
        }

    if not adb_path or not adb_addr:
        return {
            "reset_needed": True,
            "recovered": False,
            "on_stage_map": False,
            "initial_page": initial_page,
            "final_page": page_type,
            "actions": [],
            "image": cur_img,
            "state": cur_st,
        }

    actions: List[str] = []
    for step_idx in range(max(1, int(max_steps))):
        if cancel_event and cancel_event.is_set():
            break
        if page_type in ("formation_overview", "formation_select", "support_select"):
            rx, ry = cur_st.get("return_xy") or (89, 38)
            _emit(
                log_callback,
                f"[RESET] 推图启动前检测到画面停留在非地图页 ({page_type})，自动点击左上角 '<' ({rx}, {ry}) 返回章节/活动地图...",
            )
            adb_tap_norm(adb_path, adb_addr, rx, ry, cur_img.width, cur_img.height)
            actions.append(f"tap_return:{page_type}({rx},{ry})")
            time.sleep(0.9)
        elif page_type in ("settlement_victory", "settlement_failed"):
            tx, ty = (120, 640) if step_idx == 0 else (640, 560)
            _emit(
                log_callback,
                f"[RESET] 推图启动前检测到画面停留在结算页 ({page_type})，点击安全角落 ({tx}, {ty}) 退出结算返回地图...",
            )
            adb_tap_norm(adb_path, adb_addr, tx, ty, cur_img.width, cur_img.height)
            actions.append(f"tap_settle_dismiss:{page_type}({tx},{ty})")
            time.sleep(1.0)
        else:
            break

        next_img = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
        if next_img is None:
            break
        cur_img = next_img
        cur_st = analyze_screen_state(cur_img, maa_dir, use_ocr_fallback=False)
        page_type, needs_reset = _classify_non_map_page(cur_img, cur_st, use_ocr_fallback=use_ocr_fallback)
        if not needs_reset:
            break

    on_map = bool(cur_st.get("on_stage_map") or page_type == "stage_map")
    if on_map and actions:
        _emit(log_callback, f"[RESET] 已成功从 {initial_page} 自动复位至关卡地图页（步骤={actions}）。")
    return {
        "reset_needed": True,
        "recovered": bool(on_map and not needs_reset),
        "on_stage_map": on_map,
        "initial_page": initial_page,
        "final_page": page_type,
        "actions": actions,
        "image": cur_img,
        "state": cur_st,
    }


def check_and_skip_story_once(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Single non-blocking ADB screenshot probe + skip action for in-flight Copilot coroutine."""
    img = adb_screencap_image(adb_path, adb_addr)
    if img is None:
        return {"acted": False, "reason": "screencap_failed"}
    w, h = img.size
    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=True)
    if st.get("in_battle"):
        return {"acted": False, "state": st, "reason": "in_battle"}

    if st.get("is_battle_failed"):
        _emit(log_callback, "[STORY] 剧情跳过协程：检测到「行动失败 / MISSION FAILED」结算界面，严禁误判为剧情跳过，保留失败现场！")
        return {"acted": False, "battle_failed": True, "state": st, "reason": "battle_failed_screen"}

    if st.get("is_battle_victory") or st.get("has_end_of_action"):
        return {
            "acted": False,
            "battle_victory": True,
            "settlement_stars": st.get("settlement_stars"),
            "state": st,
            "reason": "battle_victory_screen",
        }

    if st.get("has_skip_confirm"):
        cx, cy = st["confirm_xy"]
        adb_tap_norm(adb_path, adb_addr, cx, cy, w, h)
        _emit(log_callback, "[STORY] 剧情跳过协程：检测到确认跳过弹窗，已自动点击确认跳过。")
        return {"acted": True, "action": "confirm_skip", "state": st}

    if st.get("has_skip_button"):
        sx, sy = st["skip_xy"]
        adb_tap_norm(adb_path, adb_addr, sx, sy, w, h)
        _emit(log_callback, "[STORY] 剧情跳过协程：检测到右上角 SKIP/跳过 按钮，自动点击跳过并确认...")
        time.sleep(0.55)
        img2 = adb_screencap_image(adb_path, adb_addr)
        if img2 is not None:
            st2 = analyze_screen_state(img2, maa_dir, use_ocr_fallback=True)
            if st2.get("is_battle_failed"):
                _emit(log_callback, "[STORY] 剧情跳过协程：二次确认时检测到「行动失败」界面，立即中止点击。")
                return {"acted": False, "battle_failed": True, "state": st2, "reason": "battle_failed_screen"}
            cx, cy = st2["confirm_xy"] if st2.get("has_skip_confirm") else (795, 495)
            adb_tap_norm(adb_path, adb_addr, cx, cy, img2.width, img2.height)
        else:
            adb_tap_norm(adb_path, adb_addr, 795, 495, w, h)
        return {"acted": True, "action": "skip_and_confirm", "state": st}

    if st.get("has_end_of_plot"):
        adb_tap_norm(adb_path, adb_addr, 1260, 100, w, h)
        _emit(log_callback, "[STORY] 剧情跳过协程：关闭剧情结算弹窗。")
        return {"acted": True, "action": "end_of_plot", "state": st}

    if st.get("has_story_dialog"):
        adb_tap_norm(adb_path, adb_addr, 640, 480, w, h)
        _emit(log_callback, "[STORY] 剧情跳过协程：检测到剧情对话框，推进剧情以触发 SKIP。")
        return {"acted": True, "action": "advance_dialog", "state": st}

    return {"acted": False, "state": st, "reason": "no_story"}


def dismiss_battle_failed_to_map(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    max_taps: int = 4,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Safely dismiss the Mission Failed settlement overlay back to the stage map AFTER failure evidence is captured."""
    if not adb_path or not adb_addr:
        return False
    for idx in range(max(1, int(max_taps))):
        if cancel_event and cancel_event.is_set():
            return False
        img = adb_screencap_image(adb_path, adb_addr)
        if img is None:
            return False
        st = analyze_screen_state(img, maa_dir, use_ocr_fallback=True)
        if st.get("on_stage_map") or st.get("in_formation") or st.get("has_stage_start_btn"):
            return True
        if idx == 0:
            _emit(log_callback, "[SETTLE] 战败现场截图已保存，正在点击「行动失败」界面返回活动地图以准备重试...")
        adb_tap_norm(adb_path, adb_addr, 640, 560, img.width, img.height)
        time.sleep(0.9)
    img_end = adb_screencap_image(adb_path, adb_addr)
    if img_end is not None:
        st_end = analyze_screen_state(img_end, maa_dir, use_ocr_fallback=False)
        return bool(st_end.get("on_stage_map") or st_end.get("in_formation") or st_end.get("has_stage_start_btn"))
    return False


def run_story_skip_loop(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    target_states: Tuple[str, ...] = ("formation", "map"),
    max_duration_sec: float = 45.0,
    poll_interval: float = 0.8,
    min_probe_sec: float = 1.5,
    require_story_seen: bool = False,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    stop_on_battle_failed: bool = True,
    dismiss_victory_settlement: bool = True,
    expected_stage: str = "",
    uncheck_auto_deploy: bool = False,
    max_start_retries: int = 0,
) -> Dict[str, Any]:
    """Poll ADB screenshots to skip genuine story dialogs / victory settlement until reaching formation or stage map."""
    t0 = time.time()
    skips_clicked = 0
    story_seen = False
    stable_map_hits = 0
    battle_victory_seen = False
    settlement_stars: Optional[int] = None
    settle_wait_polls = 0
    start_retries = 0
    exp_stage_up = str(expected_stage or "").strip().upper()

    while (time.time() - t0) < max_duration_sec:
        if cancel_event and cancel_event.is_set():
            return {
                "ok": False,
                "outcome": "cancelled",
                "skips_clicked": skips_clicked,
                "battle_failed": False,
                "battle_victory_seen": battle_victory_seen,
                "settlement_stars": settlement_stars,
            }

        img = adb_screencap_image(adb_path, adb_addr)
        if img is None:
            time.sleep(poll_interval)
            continue

        w, h = img.size
        st = analyze_screen_state(img, maa_dir, use_ocr_fallback=True)

        # Priority 0: NEVER misidentify Mission Failed as story dialog or click it away silently!
        if st.get("is_battle_failed"):
            _emit(log_callback, "[STORY] 检测到「行动失败 / MISSION FAILED」结算界面，停止剧情跳过并标记为战斗失败！")
            if stop_on_battle_failed:
                return {
                    "ok": False,
                    "outcome": "battle_failed",
                    "battle_failed": True,
                    "battle_victory_seen": False,
                    "settlement_stars": None,
                    "skips_clicked": skips_clicked,
                    "story_seen": story_seen,
                }
            adb_tap_norm(adb_path, adb_addr, 640, 560, w, h)
            time.sleep(poll_interval)
            continue

        # Priority 1: Genuine victory settlement screen (EndOfAction / 行动结束 / 3-star drops)
        if st.get("is_battle_victory") or st.get("has_end_of_action"):
            battle_victory_seen = True
            if st.get("settlement_stars") is not None:
                cur_stars = int(st.get("settlement_stars"))
                settlement_stars = cur_stars if settlement_stars is None else max(int(settlement_stars), cur_stars)
            stable_map_hits = 0
            if (settlement_stars is None or settlement_stars < 3) and settle_wait_polls < 2:
                settle_wait_polls += 1
                time.sleep(min(0.65, poll_interval))
                continue
            if dismiss_victory_settlement and "map" in target_states:
                adb_tap_norm(adb_path, adb_addr, 1260, 100, w, h)
                _emit(
                    log_callback,
                    f"[SETTLE] 检测到通关结算界面 (stars={settlement_stars or '?'})，点击安全角落返回地图...",
                )
            time.sleep(poll_interval)
            continue

        if st.get("has_skip_confirm"):
            story_seen = True
            stable_map_hits = 0
            cx, cy = st["confirm_xy"]
            adb_tap_norm(adb_path, adb_addr, cx, cy, w, h)
            skips_clicked += 1
            _emit(log_callback, "[STORY] 检测到「确认跳过」弹窗，已点击屏幕中央偏右确认跳过。")
            time.sleep(poll_interval)
            continue

        if st.get("has_skip_button"):
            story_seen = True
            stable_map_hits = 0
            sx, sy = st["skip_xy"]
            adb_tap_norm(adb_path, adb_addr, sx, sy, w, h)
            skips_clicked += 1
            _emit(log_callback, "[STORY] 检测到右上角 SKIP/跳过 按钮，自动点击跳过剧情...")
            time.sleep(0.55)
            img2 = adb_screencap_image(adb_path, adb_addr)
            if img2 is not None:
                st2 = analyze_screen_state(img2, maa_dir, use_ocr_fallback=True)
                if st2.get("is_battle_failed") and stop_on_battle_failed:
                    _emit(log_callback, "[STORY] 跳过确认阶段检测到「行动失败」界面，立即停止并标记战斗失败！")
                    return {
                        "ok": False,
                        "outcome": "battle_failed",
                        "battle_failed": True,
                        "battle_victory_seen": False,
                        "settlement_stars": None,
                        "skips_clicked": skips_clicked,
                        "story_seen": story_seen,
                    }
                if not st2.get("in_formation") and not st2.get("in_battle") and not st2.get("on_stage_map"):
                    cx, cy = st2["confirm_xy"] if st2.get("has_skip_confirm") else (795, 495)
                    adb_tap_norm(adb_path, adb_addr, cx, cy, img2.width, img2.height)
                    _emit(log_callback, "[STORY] 自动点击屏幕中央偏右「确认跳过」按钮。")
            else:
                adb_tap_norm(adb_path, adb_addr, 795, 495, w, h)
            time.sleep(poll_interval)
            continue

        if st.get("has_end_of_plot"):
            story_seen = True
            stable_map_hits = 0
            adb_tap_norm(adb_path, adb_addr, 1260, 100, w, h)
            _emit(log_callback, "[STORY] 检测到剧情结束奖励弹窗，自动点击关闭。")
            time.sleep(poll_interval)
            continue

        if st.get("in_formation") and "formation" in target_states:
            return {
                "ok": True,
                "outcome": "formation",
                "skips_clicked": skips_clicked,
                "story_seen": story_seen,
                "battle_failed": False,
                "battle_victory_seen": battle_victory_seen,
                "settlement_stars": settlement_stars,
            }

        if st.get("in_battle") and "battle" in target_states:
            return {
                "ok": True,
                "outcome": "battle",
                "skips_clicked": skips_clicked,
                "story_seen": story_seen,
                "battle_failed": False,
                "battle_victory_seen": battle_victory_seen,
                "settlement_stars": settlement_stars,
            }

        if (
            max_start_retries > 0
            and "formation" in target_states
            and "map" not in target_states
            and st.get("has_stage_start_btn")
        ):
            stable_map_hits = 0
            if start_retries < max_start_retries:
                if exp_stage_up:
                    panel_retry = inspect_stage_detail_panel(img, maa_dir, expected_stage=exp_stage_up, use_ocr=True)
                    if not panel_retry.get("title_matches"):
                        disp_r = panel_retry.get("displayed_stage") or "未识别"
                        return {
                            "ok": False,
                            "outcome": "stage_mismatch",
                            "stage_mismatch": True,
                            "displayed_stage": panel_retry.get("displayed_stage", ""),
                            "skips_clicked": skips_clicked,
                            "story_seen": story_seen,
                            "battle_failed": False,
                            "battle_victory_seen": battle_victory_seen,
                            "settlement_stars": settlement_stars,
                            "reason": f"点击开始行动后右侧详情面板显示 '{disp_r}' ≠ '{exp_stage_up}'，已中止以防打错关",
                        }
                    if (
                        uncheck_auto_deploy
                        and classify_stage_node(exp_stage_up) == "battle"
                        and panel_retry.get("auto_deploy_checked")
                    ):
                        tx, ty = panel_retry.get("auto_deploy_toggle_xy") or (1120, 592)
                        _emit(
                            log_callback,
                            f"[PUSH] 补点前检测到 {exp_stage_up}「代理指挥」仍勾选，自动取消勾选...",
                        )
                        adb_tap_norm(adb_path, adb_addr, tx, ty, w, h)
                        time.sleep(0.45)
                start_retries += 1
                sx, sy = st["stage_start_xy"]
                _emit(
                    log_callback,
                    f"[PUSH] 点击「开始行动」后仍停留在 {exp_stage_up or '当前'} 详情面板，自动补点第 {start_retries}/{max_start_retries} 次...",
                )
                adb_tap_norm(adb_path, adb_addr, sx, sy, w, h)
                time.sleep(max(0.85, poll_interval))
                continue
            return {
                "ok": False,
                "outcome": "start_button_unresponsive",
                "skips_clicked": skips_clicked,
                "story_seen": story_seen,
                "battle_failed": False,
                "battle_victory_seen": battle_victory_seen,
                "settlement_stars": settlement_stars,
                "reason": (
                    f"已校验并点击关卡 {exp_stage_up or '目标'}「开始行动」按钮（含 {max_start_retries} 次补点），"
                    f"但画面仍停留在详情面板（可能理智不足或点击未响应）"
                ),
            }

        if (
            max_start_retries > 0
            and "map" in target_states
            and st.get("has_story_start_btn")
        ):
            stable_map_hits = 0
            if start_retries < max_start_retries:
                modal_retry = detect_story_card_modal(
                    img,
                    maa_dir=maa_dir,
                    expected_stage=exp_stage_up,
                    use_ocr=True,
                    cancel_event=cancel_event,
                )
                if exp_stage_up and modal_retry.get("displayed_stage") and not modal_retry.get("title_matches"):
                    disp_sr = modal_retry.get("displayed_stage") or "未识别"
                    return {
                        "ok": False,
                        "outcome": "stage_mismatch",
                        "stage_mismatch": True,
                        "displayed_stage": modal_retry.get("displayed_stage", ""),
                        "skips_clicked": skips_clicked,
                        "story_seen": story_seen,
                        "battle_failed": False,
                        "battle_victory_seen": battle_victory_seen,
                        "settlement_stars": settlement_stars,
                        "reason": f"点击「开始剧情」后居中剧情卡片显示 '{disp_sr}' ≠ '{exp_stage_up}'，已中止以防点错剧情关",
                    }
                start_retries += 1
                sx, sy = modal_retry.get("start_story_xy") or st.get("story_start_xy") or STORY_CARD_START_FALLBACK_XY
                _emit(
                    log_callback,
                    f"[STORY] 点击「开始剧情」后仍停留在居中剧情卡片，自动补点第 {start_retries}/{max_start_retries} 次 ({sx}, {sy})...",
                )
                adb_tap_norm(adb_path, adb_addr, sx, sy, w, h)
                time.sleep(max(0.85, poll_interval))
                continue
            return {
                "ok": False,
                "outcome": "start_button_unresponsive",
                "skips_clicked": skips_clicked,
                "story_seen": story_seen,
                "battle_failed": False,
                "battle_victory_seen": battle_victory_seen,
                "settlement_stars": settlement_stars,
                "reason": f"已点击纯剧情关 {exp_stage_up or '目标'}「开始剧情」按钮（含 {max_start_retries} 次补点），但仍停留在居中剧情卡片",
            }

        if st.get("has_story_dialog"):
            story_seen = True
            stable_map_hits = 0
            adb_tap_norm(adb_path, adb_addr, 640, 480, w, h)
            time.sleep(poll_interval)
            continue

        if st.get("on_stage_map") and "map" in target_states:
            elapsed = time.time() - t0
            if (not require_story_seen or story_seen) and (elapsed >= min_probe_sec or skips_clicked > 0 or battle_victory_seen):
                stable_map_hits += 1
                if stable_map_hits >= 2 or (skips_clicked == 0 and elapsed >= min_probe_sec):
                    return {
                        "ok": True,
                        "outcome": "map",
                        "skips_clicked": skips_clicked,
                        "story_seen": story_seen,
                        "battle_failed": False,
                        "battle_victory_seen": battle_victory_seen,
                        "settlement_stars": settlement_stars,
                    }
            time.sleep(poll_interval)
            continue

        stable_map_hits = 0
        time.sleep(poll_interval)

    return {
        "ok": False,
        "outcome": "timeout",
        "skips_clicked": skips_clicked,
        "story_seen": story_seen,
        "battle_failed": False,
        "battle_victory_seen": battle_victory_seen,
        "settlement_stars": settlement_stars,
    }


def enter_stage_node(
    stage_code: str,
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    click_start: bool = True,
    max_swipes: int = 5,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    skip_if_already_3star: bool = False,
    max_title_mismatches: int = 2,
    uncheck_auto_deploy: bool = True,
    reset_non_map: bool = True,
) -> Dict[str, Any]:
    """Locate a stage node (e.g. TL-ST-1 / TL-TR-1 / TL-2) on the map, verify right detail title, and click Start."""
    target_up = str(stage_code or "").strip().upper()
    node_type = classify_stage_node(target_up)
    img = adb_screencap_image(adb_path, adb_addr)
    if img is None:
        return {"entered": False, "reason": "screencap_failed"}

    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    if st.get("in_battle"):
        return {"entered": True, "already_inside": True, "clicked_start": False}
    if reset_non_map:
        reset_res = ensure_on_stage_map_before_search(
            adb_path,
            adb_addr,
            maa_dir,
            cancel_event=cancel_event,
            log_callback=log_callback,
            initial_img=img,
            initial_state=st,
        )
        if reset_res.get("image") is not None:
            img = reset_res["image"]
            st = reset_res["state"]
    if st.get("in_formation") or st.get("in_battle"):
        return {"entered": True, "already_inside": True, "clicked_start": False}

    def _close_detail_drawer(
        cur_img: Image.Image,
        stage_boxes: Optional[List[Dict[str, Any]]] = None,
        extra_bboxes: Optional[List[Any]] = None,
    ) -> Tuple[int, int]:
        bx, by = find_safe_blank_tap_point(cur_img, stage_boxes=stage_boxes, extra_bboxes=extra_bboxes)
        _emit(log_callback, f"[PUSH] 动态选择安全空白点 ({bx}, {by}) 关闭右侧旧详情栏...")
        adb_tap_norm(adb_path, adb_addr, bx, by, cur_img.width, cur_img.height)
        time.sleep(0.6)
        return bx, by

    def _is_unobstructed_left_hit(hit_item: Optional[Tuple[int, int, Dict[str, Any]]]) -> bool:
        if hit_item is None:
            return False
        hx, _, hbox = hit_item
        bx = int(hbox.get("x", 0))
        by = int(hbox.get("y", 0))
        bw = max(1, int(hbox.get("w", 0)))
        bh = max(1, int(hbox.get("h", 0)))
        return 10 <= bx and 72 <= by and (bx + bw) <= 810 and (by + bh) <= 655 and hx <= 760

    mismatch_limit = max(1, int(max_title_mismatches))
    mismatch_count = 0

    def _tap_start_after_panel_check(
        cur_img: Image.Image,
        cur_st: Dict[str, Any],
        cur_panel: Dict[str, Any],
    ) -> None:
        if not click_start:
            return
        if uncheck_auto_deploy and node_type == "battle" and cur_panel.get("auto_deploy_checked"):
            tx, ty = cur_panel.get("auto_deploy_toggle_xy") or (1120, 592)
            _emit(
                log_callback,
                f"[PUSH] 检测到 {target_up} 右侧详情面板「代理指挥」已勾选，推图/补星模式下自动取消勾选...",
            )
            adb_tap_norm(adb_path, adb_addr, tx, ty, cur_img.width, cur_img.height)
            time.sleep(0.5)
        sx, sy = cur_st["stage_start_xy"]
        adb_tap_norm(adb_path, adb_addr, sx, sy, cur_img.width, cur_img.height)
        _emit(log_callback, f"[PUSH] 右侧详情面板标题校验通过 ({target_up})，已点击「开始行动」按钮。")

    def _close_story_card_modal(cur_img: Image.Image) -> Tuple[int, int]:
        bx, by = STORY_CARD_DISMISS_XY
        _emit(log_callback, f"[STORY] 点击背景安全空白点 ({bx}, {by}) 关闭居中剧情卡片...")
        adb_tap_norm(adb_path, adb_addr, bx, by, cur_img.width, cur_img.height)
        time.sleep(0.6)
        return bx, by

    def _verify_and_enter_story_card(
        cur_img: Image.Image,
        *,
        switched_in_place: bool = False,
        located_by: str = "",
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        nonlocal mismatch_count
        modal = detect_story_card_modal(
            cur_img,
            maa_dir=maa_dir,
            expected_stage=target_up,
            use_ocr=True,
            cancel_event=cancel_event,
        )
        if not modal.get("is_story_card_modal"):
            return False, None
        if modal.get("title_matches"):
            if click_start:
                sx, sy = modal["start_story_xy"]
                _emit(
                    log_callback,
                    f"[STORY] 居中剧情卡片标题校验通过 ({target_up}，定位={modal['start_story_xy_source']})，"
                    f"点击右区蓝色「开始剧情」按钮 ({sx}, {sy})...",
                )
                adb_tap_norm(adb_path, adb_addr, sx, sy, cur_img.width, cur_img.height)
                time.sleep(1.0)
            res_ok: Dict[str, Any] = {
                "entered": True,
                "clicked_start": click_start,
                "displayed_stage": modal.get("displayed_stage", target_up),
                "title_verified": True,
                "story_card_modal": True,
                "start_story_xy": modal["start_story_xy"],
                "start_story_xy_source": modal["start_story_xy_source"],
            }
            if switched_in_place:
                res_ok["switched_in_place"] = True
            if located_by:
                res_ok["located_by"] = located_by
            return True, res_ok

        mismatch_count += 1
        disp = modal.get("displayed_stage") or "未识别"
        _emit(
            log_callback,
            f"[STORY][防错关] 居中剧情卡片标题校验不符：目标 '{target_up}'，实际显示 '{disp}'"
            f"（raw={modal.get('raw_title_text', '')!r}，第 {mismatch_count}/{mismatch_limit} 次）！严禁启动剧情，关闭弹窗...",
        )
        _close_story_card_modal(cur_img)
        if mismatch_count >= mismatch_limit:
            return True, {
                "entered": False,
                "stage_mismatch": True,
                "story_card_modal": True,
                "expected_stage": target_up,
                "displayed_stage": modal.get("displayed_stage", ""),
                "mismatch_count": mismatch_count,
                "reason": (
                    f"连续 {mismatch_count} 次居中剧情卡片标题与目标 {target_up} 不符"
                    f"（卡片显示 {disp}），已中止以防点错剧情关"
                ),
            }
        return True, None

    # Check if centered story card modal is already open
    if st.get("has_story_start_btn") or (
        node_type == "story"
        and not st.get("has_stage_start_btn")
        and detect_story_card_modal(
            img, maa_dir=maa_dir, expected_stage=target_up, use_ocr=False, cancel_event=cancel_event
        ).get("has_blue_start_block")
    ):
        handled_init, res_init = _verify_and_enter_story_card(img)
        if res_init is not None:
            return res_init
        if handled_init:
            img_r = adb_screencap_image(adb_path, adb_addr)
            if img_r is not None:
                img = img_r
                st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)

    # Check if stage detail drawer is already open
    if st.get("has_stage_start_btn"):
        if node_type == "story":
            handled_s, res_s = _verify_and_enter_story_card(img)
            if res_s is not None:
                return res_s
            if not handled_s:
                open_boxes = find_stage_boxes_via_ocr(img)
                direct_hit = _match_stage_center_from_boxes(open_boxes, target_up, max_right_x=810, max_cx=760)
                if direct_hit is not None:
                    tcx, tcy, tbox = direct_hit
                    _emit(
                        log_callback,
                        f"[STORY] 当前已展开旧战斗详情栏，目标剧情节点 {target_up} 在左侧可见 ({tcx}, {tcy})，直接点击切换...",
                    )
                    adb_tap_norm(adb_path, adb_addr, tcx, tcy, img.width, img.height)
                    time.sleep(1.0)
                    after_switch = adb_screencap_image(adb_path, adb_addr)
                    if after_switch is None:
                        return {"entered": False, "reason": "screencap_failed_after_inplace_switch"}
                    st_switch = analyze_screen_state(after_switch, maa_dir, use_ocr_fallback=False)
                    if st_switch.get("has_skip_button") or st_switch.get("has_skip_confirm") or st_switch.get("has_story_dialog"):
                        return {"entered": True, "clicked_start": False, "switched_in_place": True}
                    handled_sw, res_sw = _verify_and_enter_story_card(after_switch, switched_in_place=True)
                    if res_sw is not None:
                        return res_sw
                    if not handled_sw and st_switch.get("has_stage_start_btn"):
                        _close_detail_drawer(after_switch, extra_bboxes=[tbox])
                else:
                    _close_detail_drawer(img, stage_boxes=open_boxes)
        else:
            panel = inspect_stage_detail_panel(img, maa_dir, expected_stage=target_up, use_ocr=True)
            if panel.get("title_matches"):
                if skip_if_already_3star and node_type == "battle" and panel.get("already_cleared_3star"):
                    _emit(
                        log_callback,
                        f"[PUSH] 右侧详情面板校验为 {target_up} 且已 3 星通关并解锁「代理指挥」，自动跳过不再浪费理智！",
                    )
                    _close_detail_drawer(img)
                    return {
                        "entered": False,
                        "already_cleared": True,
                        "skipped": True,
                        "displayed_stage": panel.get("displayed_stage", target_up),
                        "reason": "already_cleared_3star",
                    }
                if click_start:
                    _tap_start_after_panel_check(img, st, panel)
                    time.sleep(0.8)
                return {
                    "entered": True,
                    "clicked_start": click_start,
                    "displayed_stage": panel.get("displayed_stage", target_up),
                    "title_verified": True,
                }
            disp = panel.get("displayed_stage") or "未识别"
            open_boxes = find_stage_boxes_via_ocr(img)
            direct_hit = _match_stage_center_from_boxes(open_boxes, target_up, max_right_x=810, max_cx=760)
            if direct_hit is None and not open_boxes:
                cand_hit = find_stage_center_on_image(img, target_up)
                if _is_unobstructed_left_hit(cand_hit):
                    direct_hit = cand_hit
            if direct_hit is not None:
                tcx, tcy, tbox = direct_hit
                _emit(
                    log_callback,
                    f"[PUSH] 当前已展开 '{disp}' 详情栏，但目标节点 {target_up} 已在画面左侧清晰可见 ({tcx}, {tcy})，"
                    f"直接点击原地切换面板...",
                )
                adb_tap_norm(adb_path, adb_addr, tcx, tcy, img.width, img.height)
                time.sleep(1.0)
                after_switch = adb_screencap_image(adb_path, adb_addr)
                if after_switch is None:
                    return {"entered": False, "reason": "screencap_failed_after_inplace_switch"}
                st_switch = analyze_screen_state(after_switch, maa_dir, use_ocr_fallback=False)
                if st_switch.get("has_story_start_btn"):
                    handled_sw, res_sw = _verify_and_enter_story_card(after_switch, switched_in_place=True)
                    if res_sw is not None:
                        return res_sw
                elif st_switch.get("has_stage_start_btn"):
                    panel_after = inspect_stage_detail_panel(after_switch, maa_dir, expected_stage=target_up, use_ocr=True)
                    if panel_after.get("title_matches"):
                        if skip_if_already_3star and node_type == "battle" and panel_after.get("already_cleared_3star"):
                            _emit(
                                log_callback,
                                f"[PUSH] 关卡 {target_up} 右侧详情面板已 3 星通关且出现「代理指挥」勾选框，自动判定已完成并跳过！",
                            )
                            _close_detail_drawer(after_switch)
                            return {
                                "entered": False,
                                "already_cleared": True,
                                "skipped": True,
                                "displayed_stage": panel_after.get("displayed_stage", target_up),
                                "switched_in_place": True,
                                "reason": "already_cleared_3star",
                            }
                        if click_start:
                            _tap_start_after_panel_check(after_switch, st_switch, panel_after)
                            time.sleep(1.0)
                        return {
                            "entered": True,
                            "clicked_start": click_start,
                            "displayed_stage": panel_after.get("displayed_stage", target_up),
                            "title_verified": True,
                            "switched_in_place": True,
                        }
                    mismatch_count += 1
                    disp_after = panel_after.get("displayed_stage") or "未识别"
                    _emit(
                        log_callback,
                        f"[PUSH][防错关] 原地切换后右侧详情面板标题校验不符：目标 '{target_up}'，实际显示 '{disp_after}'"
                        f"（raw={panel_after.get('raw_title_text', '')!r}，第 {mismatch_count}/{mismatch_limit} 次）！",
                    )
                    _close_detail_drawer(after_switch, extra_bboxes=[tbox])
                    if mismatch_count >= mismatch_limit:
                        return {
                            "entered": False,
                            "stage_mismatch": True,
                            "expected_stage": target_up,
                            "displayed_stage": panel_after.get("displayed_stage", ""),
                            "mismatch_count": mismatch_count,
                            "reason": f"连续 {mismatch_count} 次右侧详情面板标题与目标 {target_up} 不符（右侧显示 {disp_after}），已中止以防打错关",
                        }
                elif st_switch.get("has_skip_button") or st_switch.get("has_story_dialog"):
                    return {"entered": True, "clicked_start": False, "switched_in_place": True}
            else:
                _emit(
                    log_callback,
                    f"[PUSH][防错关] 当前已展开的右侧详情栏显示 '{disp}'（raw={panel.get('raw_title_text', '')!r}），"
                    f"与目标关卡 '{target_up}' 不符！严禁点击开始行动，正在避开节点关闭旧详情栏并重新选关...",
                )
                _close_detail_drawer(img, stage_boxes=open_boxes)

    def _try_unknown_white_card_candidates(cur_img: Image.Image) -> Optional[Dict[str, Any]]:
        """Fallback: tap unlabelled uncleared white cards right of cleared anchor & verify right detail title."""
        candidates = find_unknown_white_card_candidates(cur_img, target_up)
        if not candidates:
            return None
        for idx, cand in enumerate(candidates, 1):
            if cancel_event and cancel_event.is_set():
                return {"entered": False, "cancelled": True}
            ccx, ccy = int(cand["cx"]), int(cand["cy"])
            anchor_code = str(cand.get("anchor_code") or "")
            _emit(
                log_callback,
                f"[PUSH][白框兜底] OCR 未直接读出 {target_up}，在已通关节点 ({anchor_code}) 右侧发现未带蓝标纯白框 "
                f"#{idx}/{len(candidates)} ({ccx}, {ccy})，尝试点击并核验...",
            )
            adb_tap_norm(adb_path, adb_addr, ccx, ccy, cur_img.width, cur_img.height)
            time.sleep(1.0)
            after = adb_screencap_image(adb_path, adb_addr)
            if after is None:
                return {"entered": False, "reason": "screencap_failed_after_white_card_tap"}
            st_after = analyze_screen_state(after, maa_dir, use_ocr_fallback=False)
            if node_type == "story" or st_after.get("has_story_start_btn"):
                handled_s, res_s = _verify_and_enter_story_card(after, located_by="white_card_fallback")
                if res_s is not None:
                    return res_s
                if handled_s:
                    continue
                if st_after.get("has_stage_start_btn"):
                    _close_detail_drawer(after, extra_bboxes=[cand])
                continue
            if st_after.get("has_stage_start_btn"):
                panel = inspect_stage_detail_panel(after, maa_dir, expected_stage=target_up, use_ocr=True)
                if panel.get("title_matches"):
                    _emit(
                        log_callback,
                        f"[PUSH][白框兜底] 右侧详情面板大字标题核验命中 {target_up}"
                        f"（raw={panel.get('raw_title_text', '')!r}），定位成功！",
                    )
                    if skip_if_already_3star and node_type == "battle" and panel.get("already_cleared_3star"):
                        _emit(
                            log_callback,
                            f"[PUSH] 关卡 {target_up} 右侧详情面板已 3 星通关且出现「代理指挥」勾选框，自动判定已完成并跳过！",
                        )
                        _close_detail_drawer(after)
                        return {
                            "entered": False,
                            "already_cleared": True,
                            "skipped": True,
                            "displayed_stage": panel.get("displayed_stage", target_up),
                            "located_by": "white_card_fallback",
                            "reason": "already_cleared_3star",
                        }
                    if click_start:
                        _tap_start_after_panel_check(after, st_after, panel)
                        time.sleep(1.0)
                    return {
                        "entered": True,
                        "clicked_start": click_start,
                        "displayed_stage": panel.get("displayed_stage", target_up),
                        "title_verified": True,
                        "located_by": "white_card_fallback",
                        "candidate_xy": (ccx, ccy),
                        "anchor_code": anchor_code,
                    }
                disp = panel.get("displayed_stage") or "未识别"
                _emit(
                    log_callback,
                    f"[PUSH][白框兜底] 候选白框 ({ccx}, {ccy}) 右侧详情显示 '{disp}' ≠ '{target_up}'，关闭详情栏继续探测下一个候选...",
                )
                _close_detail_drawer(after, extra_bboxes=[cand])
        return None

    def _search_and_click(allow_reselect_retry: bool = True) -> Optional[Dict[str, Any]]:
        nonlocal mismatch_count
        max_local_tries = (mismatch_limit + 1) if allow_reselect_retry else 1
        for local_try in range(max_local_tries):
            if cancel_event and cancel_event.is_set():
                return {"entered": False, "cancelled": True}
            cur_img = adb_screencap_image(adb_path, adb_addr)
            if cur_img is None:
                return None
            st_cur = analyze_screen_state(cur_img, maa_dir, use_ocr_fallback=False)
            if st_cur.get("has_story_start_btn"):
                handled_cur, res_cur = _verify_and_enter_story_card(cur_img)
                if res_cur is not None:
                    return res_cur
                if handled_cur:
                    cur_img = adb_screencap_image(adb_path, adb_addr)
                    if cur_img is None:
                        return None
                    st_cur = analyze_screen_state(cur_img, maa_dir, use_ocr_fallback=False)

            hit: Optional[Tuple[int, int, Dict[str, Any]]] = None
            if st_cur.get("has_stage_start_btn"):
                cur_boxes = find_stage_boxes_via_ocr(cur_img)
                if local_try == 0:
                    direct_hit = _match_stage_center_from_boxes(cur_boxes, target_up, max_right_x=810, max_cx=760)
                    if direct_hit is not None:
                        hit = direct_hit
                if hit is None:
                    # Close stale drawer before OCR so we never click drawer text as a map node
                    _close_detail_drawer(cur_img, stage_boxes=cur_boxes)
                    cur_img = adb_screencap_image(adb_path, adb_addr)
                    if cur_img is None:
                        return None
                    hit = find_stage_center_on_image(cur_img, target_up)
            else:
                hit = find_stage_center_on_image(cur_img, target_up)

            if hit is None:
                return _try_unknown_white_card_candidates(cur_img)
            cx, cy, hit_box = hit
            _emit(log_callback, f"[PUSH] 已在地图定位节点 {target_up} ({cx}, {cy})，自动点击选中...")
            adb_tap_norm(adb_path, adb_addr, cx, cy, cur_img.width, cur_img.height)
            time.sleep(1.0)
            after = adb_screencap_image(adb_path, adb_addr)
            if after is None:
                return {"entered": False, "reason": "screencap_failed_after_tap"}
            st_after = analyze_screen_state(after, maa_dir, use_ocr_fallback=False)
            if st_after.get("has_skip_button") or st_after.get("has_skip_confirm") or st_after.get("has_story_dialog"):
                return {"entered": True, "clicked_start": False}
            if node_type == "story" or st_after.get("has_story_start_btn"):
                handled_s, res_s = _verify_and_enter_story_card(after)
                if res_s is not None:
                    return res_s
                if handled_s:
                    continue
                if st_after.get("has_stage_start_btn"):
                    mismatch_count += 1
                    _emit(
                        log_callback,
                        f"[STORY][防错关] 点击目标 '{target_up}' 后展开了战斗详情栏而非居中剧情卡片"
                        f"（第 {mismatch_count}/{mismatch_limit} 次），关闭详情栏...",
                    )
                    _close_detail_drawer(after, extra_bboxes=[hit_box])
                    if mismatch_count >= mismatch_limit:
                        return {
                            "entered": False,
                            "stage_mismatch": True,
                            "expected_stage": target_up,
                            "displayed_stage": "",
                            "mismatch_count": mismatch_count,
                            "reason": f"连续 {mismatch_count} 次点击 {target_up} 后未出现居中剧情卡片，已中止以防点错关卡",
                        }
                    continue
                return {
                    "entered": False,
                    "locked": True,
                    "reason": f"点击纯剧情节点 {target_up} 后未检测到居中剧情卡片（「STORY」/「开始剧情」），可能尚未解锁前置关卡",
                }
            if st_after.get("has_stage_start_btn"):
                panel = inspect_stage_detail_panel(after, maa_dir, expected_stage=target_up, use_ocr=True)
                if not panel.get("title_matches"):
                    mismatch_count += 1
                    disp = panel.get("displayed_stage") or "未识别"
                    _emit(
                        log_callback,
                        f"[PUSH][防错关] 右侧详情面板标题校验不符：目标 '{target_up}'，实际显示 '{disp}'"
                        f"（raw={panel.get('raw_title_text', '')!r}，第 {mismatch_count}/{mismatch_limit} 次）！"
                        f"严禁启动战斗，动态避开节点关闭旧详情栏...",
                    )
                    _close_detail_drawer(after, extra_bboxes=[hit_box])
                    if mismatch_count >= mismatch_limit:
                        return {
                            "entered": False,
                            "stage_mismatch": True,
                            "expected_stage": target_up,
                            "displayed_stage": panel.get("displayed_stage", ""),
                            "mismatch_count": mismatch_count,
                            "reason": f"连续 {mismatch_count} 次右侧详情面板标题与目标 {target_up} 不符（右侧显示 {disp}），已中止以防打错关",
                        }
                    # Swipe map slightly left if node sat near the right edge or on retry so target node is unobstructed
                    if cx > 720 or local_try > 0:
                        adb_swipe_norm(
                            adb_path, adb_addr, 860, 360, 480, 360,
                            duration_ms=320, width=after.width, height=after.height,
                        )
                        time.sleep(0.6)
                    continue

                if skip_if_already_3star and node_type == "battle" and panel.get("already_cleared_3star"):
                    _emit(
                        log_callback,
                        f"[PUSH] 关卡 {target_up} 右侧详情面板已 3 星通关且出现「代理指挥」勾选框，自动判定已完成并跳过！",
                    )
                    _close_detail_drawer(after)
                    return {
                        "entered": False,
                        "already_cleared": True,
                        "skipped": True,
                        "displayed_stage": panel.get("displayed_stage", target_up),
                        "reason": "already_cleared_3star",
                    }

                if click_start:
                    _tap_start_after_panel_check(after, st_after, panel)
                    time.sleep(1.0)
                return {
                    "entered": True,
                    "clicked_start": click_start,
                    "displayed_stage": panel.get("displayed_stage", target_up),
                    "title_verified": True,
                }
            return {"entered": False, "locked": True, "reason": f"点击 {target_up} 后未出现「开始行动」，可能尚未解锁前置关卡"}

        return {
            "entered": False,
            "stage_mismatch": True,
            "expected_stage": target_up,
            "mismatch_count": mismatch_count,
            "reason": f"选关重试后右侧标题仍与目标 {target_up} 不符，已中止以防打错关",
        }

    direct = _search_and_click(allow_reselect_retry=True)
    if direct is not None:
        return direct

    # Swipe left (toward earlier nodes) then right across map
    for _direction, sx1, sx2 in (("left", 282, 998), ("right", 998, 282)):
        for swipe_try in range(max_swipes):
            if cancel_event and cancel_event.is_set():
                return {"entered": False, "cancelled": True}
            sy = 396 if (swipe_try % 2 == 0) else 252
            sdur = 650 if (swipe_try % 2 == 0) else 800
            adb_swipe_norm(adb_path, adb_addr, sx1, sy, sx2, sy, duration_ms=sdur, width=img.width, height=img.height)
            time.sleep(1.0)
            res = _search_and_click(allow_reselect_retry=True)
            if res is not None:
                return res

    return {"entered": False, "reason": f"未能在当前地图识别到关卡节点 {target_up}"}


def prepare_battle_entry_if_on_map(
    stage_code: str,
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    skip_if_already_3star: bool = False,
    require_fresh_map_entry: bool = False,
    uncheck_auto_deploy: bool = True,
) -> Dict[str, Any]:
    """If emulator is currently on the stage map (or has a stage detail drawer open), verify/enter target stage."""
    img = adb_screencap_image(adb_path, adb_addr)
    if img is None:
        return {"handled": False, "entered": False, "reason": "screencap_failed"}
    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    if st.get("in_battle"):
        return {
            "handled": True,
            "entered": True,
            "outcome": "battle",
        }

    # If still on settlement screen (victory/defeat), always dismiss back to map before stage search
    if st.get("is_battle_failed") or st.get("is_battle_victory") or st.get("has_end_of_action") or st.get("settlement_stars") is not None:
        reset_settle = ensure_on_stage_map_before_search(
            adb_path,
            adb_addr,
            maa_dir,
            cancel_event=cancel_event,
            log_callback=log_callback,
            initial_img=img,
            initial_state=st,
        )
        if reset_settle.get("image") is not None:
            img = reset_settle["image"]
            st = reset_settle["state"]

    # For any newly advanced subsequent stage (e.g. TL-3 after TL-2), if unexpectedly still inside a previous
    # stage's formation page on round 1, step back to the stage map so we MUST locate & title-verify target stage.
    if st.get("in_formation"):
        if not require_fresh_map_entry:
            return {
                "handled": True,
                "entered": True,
                "outcome": "formation",
            }
        _emit(log_callback, f"[PUSH][选关保护] 切关至 {stage_code} 时仍停留在旧编队页，先退回地图重新定位选关并校验标题...")
        reset_form = ensure_on_stage_map_before_search(
            adb_path,
            adb_addr,
            maa_dir,
            cancel_event=cancel_event,
            log_callback=log_callback,
            initial_img=img,
            initial_state=st,
        )
        if reset_form.get("image") is not None:
            img = reset_form["image"]
            st = reset_form["state"]

    # If left on quick-formation operator selection subpage (e.g. after a BattleFormationTask Unavailable retry),
    # tap top-left '<' once to return to the squad overview screen so the next Copilot run can re-open formation cleanly.
    if st.get("in_formation_select"):
        if require_fresh_map_entry:
            reset_sel = ensure_on_stage_map_before_search(
                adb_path,
                adb_addr,
                maa_dir,
                cancel_event=cancel_event,
                log_callback=log_callback,
                initial_img=img,
                initial_state=st,
            )
            if reset_sel.get("image") is not None:
                img = reset_sel["image"]
                st = reset_sel["state"]
        else:
            rx, ry = st.get("return_xy", (89, 38))
            _emit(log_callback, "[PUSH] 检测到当前停留在快捷编队选人界面 (formation_select)，点击左上角 '<' 返回编队总览页...")
            adb_tap_norm(adb_path, adb_addr, rx, ry, img.width, img.height)
            time.sleep(0.8)
            img_after = adb_screencap_image(adb_path, adb_addr)
            if img_after is not None:
                st_after = analyze_screen_state(img_after, maa_dir, use_ocr_fallback=False)
                if st_after.get("in_formation"):
                    return {
                        "handled": True,
                        "entered": True,
                        "outcome": "formation",
                        "exited_formation_select": True,
                    }
                img = img_after
                st = st_after

    if st["has_skip_button"] or st["has_skip_confirm"] or st["has_story_dialog"]:
        res = run_story_skip_loop(
            adb_path,
            adb_addr,
            maa_dir,
            target_states=("formation", "battle", "map"),
            max_duration_sec=35.0,
            min_probe_sec=0.8,
            cancel_event=cancel_event,
            log_callback=log_callback,
        )
        entered_after_story = bool(res.get("ok") and res.get("outcome") in ("formation", "battle"))
        return {"handled": True, "entered": entered_after_story, **res}

    if st.get("has_story_start_btn") and classify_stage_node(stage_code) != "story":
        _emit(log_callback, "[PUSH] 检测到残留居中剧情卡片弹窗，先点击空白处关闭后再定位战斗关卡...")
        adb_tap_norm(adb_path, adb_addr, STORY_CARD_DISMISS_XY[0], STORY_CARD_DISMISS_XY[1], img.width, img.height)
        time.sleep(0.6)
        img_cls = adb_screencap_image(adb_path, adb_addr)
        if img_cls is not None:
            img = img_cls
            st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)

    if not st["on_stage_map"] and not st["has_stage_start_btn"]:
        return {"handled": False, "entered": False, "reason": "not_on_stage_map"}

    # If a right detail drawer is already open (possibly for a wrong stage like TL-1 covering TL-2),
    # MUST run enter_stage_node to inspect/close/re-select before Copilot can accidentally tap Start!
    drawer_open = bool(st["has_stage_start_btn"])
    target_visible = False
    if not drawer_open:
        hit = find_stage_center_on_image(img, stage_code)
        if hit is not None:
            target_visible = True
        else:
            # Check if we're on a chapter/event stage map (prefix-agnostic so cross-prefix nodes like TR-27 on Ch.16 work)
            pref = str(stage_code or "").strip().upper().rsplit("-", 1)[0] + "-" if "-" in str(stage_code or "") else ""
            same_chapter_nodes = inspect_map_stage_nodes(img, pref) if pref else []
            if not same_chapter_nodes:
                same_chapter_nodes = inspect_map_stage_nodes(img, "")
            if not same_chapter_nodes:
                return {"handled": False, "entered": False, "reason": "stage_not_visible_on_current_screen"}

    entry = enter_stage_node(
        stage_code,
        adb_path,
        adb_addr,
        maa_dir,
        click_start=True,
        max_swipes=(0 if (target_visible and not drawer_open) else 2),
        cancel_event=cancel_event,
        log_callback=log_callback,
        skip_if_already_3star=skip_if_already_3star,
        uncheck_auto_deploy=uncheck_auto_deploy,
    )
    if entry.get("already_cleared"):
        return {"handled": True, **entry}
    if entry.get("stage_mismatch"):
        return {"handled": True, **entry}
    if not entry.get("entered"):
        return {"handled": False, **entry}

    skip_res = run_story_skip_loop(
        adb_path,
        adb_addr,
        maa_dir,
        target_states=("formation", "battle"),
        max_duration_sec=35.0,
        min_probe_sec=2.0,
        cancel_event=cancel_event,
        log_callback=log_callback,
        expected_stage=stage_code,
        uncheck_auto_deploy=uncheck_auto_deploy,
        max_start_retries=2,
    )
    entered_ok = bool(skip_res.get("ok") and skip_res.get("outcome") in ("formation", "battle"))
    return {
        "handled": True,
        "entered": entered_ok,
        "clicked_start": bool(entry.get("clicked_start", True)),
        "title_verified": bool(entry.get("title_verified", False)),
        "displayed_stage": entry.get("displayed_stage", ""),
        **skip_res,
    }


def handle_story_stage(
    stage_code: str,
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    next_stage: Optional[str] = None,
    wait_map_refresh_sec: float = 6.0,
) -> Tuple[bool, Dict[str, Any]]:
    """Enter a pure story node (-ST-) and automatically skip dialogue back to the stage map."""
    _emit(log_callback, f"[STORY] 遇到纯剧情节点 {stage_code}（-ST-），自动进入并跳过剧情（不拉取战斗作业）...")
    entry = enter_stage_node(
        stage_code,
        adb_path,
        adb_addr,
        maa_dir,
        click_start=True,
        cancel_event=cancel_event,
        log_callback=log_callback,
    )
    if entry.get("cancelled"):
        return False, {"stage": stage_code, "node_type": "story", "victory": False, "cancelled": True}
    if not entry.get("entered"):
        err_out: Dict[str, Any] = {
            "stage": stage_code,
            "node_type": "story",
            "victory": False,
            "error": entry.get("reason", "进入剧情关失败"),
        }
        if entry.get("stage_mismatch"):
            err_out["stage_mismatch"] = True
            err_out["displayed_stage"] = entry.get("displayed_stage", "")
        return False, err_out

    skip_res = run_story_skip_loop(
        adb_path,
        adb_addr,
        maa_dir,
        target_states=("map",),
        max_duration_sec=60.0,
        poll_interval=0.8,
        min_probe_sec=2.5,
        require_story_seen=False,
        cancel_event=cancel_event,
        log_callback=log_callback,
        expected_stage=stage_code,
        max_start_retries=2,
    )
    if skip_res.get("outcome") == "cancelled":
        return False, {"stage": stage_code, "node_type": "story", "victory": False, "cancelled": True}
    ok = skip_res.get("outcome") == "map"

    map_refreshed = False
    unlocked_next: List[str] = []
    vis_nodes: List[Dict[str, Any]] = []
    if ok and adb_path and adb_addr:
        cur_up = str(stage_code or "").strip().upper()
        nxt_up = str(next_stage or "").strip().upper()
        pref = cur_up.split("-", 1)[0] if "-" in cur_up else ""
        t_wait = time.time()
        for poll_i in range(4):
            if cancel_event and cancel_event.is_set():
                break
            if poll_i > 0 and (time.time() - t_wait) >= max(0.0, float(wait_map_refresh_sec)):
                break
            img_m = adb_screencap_image(adb_path, adb_addr)
            if img_m is None:
                break
            st_m = analyze_screen_state(img_m, maa_dir, use_ocr_fallback=False)
            if st_m.get("has_end_of_plot"):
                adb_tap_norm(adb_path, adb_addr, 1260, 100, img_m.width, img_m.height)
                time.sleep(0.6)
                continue
            if st_m.get("on_stage_map"):
                vis_nodes = inspect_map_stage_nodes(img_m, "")
                if not vis_nodes and pref:
                    vis_nodes = inspect_map_stage_nodes(img_m, pref)
                if not vis_nodes:
                    break
                cur_nd = next((n for n in vis_nodes if n["code"] == cur_up), None)
                story_cleared = bool(
                    cur_nd and (cur_nd.get("cleared") or _is_node_bypassed_by_later_unlocked(cur_nd, vis_nodes))
                )
                unlocked_next = [
                    n["code"]
                    for n in vis_nodes
                    if (nxt_up and n["code"] == nxt_up)
                    or (cur_nd is not None and n["node_type"] == "battle" and int(n.get("cx", 0)) > int(cur_nd.get("cx", 0)) + 20)
                ]
                if story_cleared or unlocked_next:
                    map_refreshed = True
                    _emit(
                        log_callback,
                        f"[STORY] 剧情关 {stage_code} 已跳过并返回活动地图，地图已刷新解锁（unlocked={unlocked_next}）。",
                    )
                    break
            time.sleep(0.6)

    out_res: Dict[str, Any] = {
        "stage": stage_code,
        "node_type": "story",
        "victory": ok,
        "skipped_story": True,
        "skips_clicked": skip_res.get("skips_clicked", 0),
        "outcome": skip_res.get("outcome"),
        "story_card_modal": bool(entry.get("story_card_modal")),
        "start_story_xy": entry.get("start_story_xy"),
        "map_refreshed": map_refreshed,
        "unlocked_next_stages": unlocked_next,
        "visible_nodes_after": vis_nodes,
    }
    if not ok:
        if skip_res.get("stage_mismatch"):
            out_res["stage_mismatch"] = True
            out_res["displayed_stage"] = skip_res.get("displayed_stage", "")
        if skip_res.get("reason"):
            out_res["error"] = skip_res["reason"]
    return ok, out_res


def wait_for_tutorial_completion(
    stage_code: str,
    next_stage: Optional[str],
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    poll_interval: float = 3.0,
    cancel_event: Optional[threading.Event] = None,
    manual_resume_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """Silently poll every ~3s until the user finishes -TR- tutorial and returns to unlocked map."""
    left_map_seen = False
    map_hits = 0
    t0 = time.time()

    while True:
        if cancel_event and cancel_event.is_set():
            return "cancelled"

        if manual_resume_event and manual_resume_event.is_set():
            manual_resume_event.clear()
            return "manual_resumed"

        img = adb_screencap_image(adb_path, adb_addr)
        if img is not None:
            st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
            if not st["on_stage_map"]:
                left_map_seen = True
                map_hits = 0
            else:
                img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
                unlocked_confirmed = False
                clearly_locked = False
                if next_stage:
                    hit = find_stage_center_on_image(img, next_stage)
                    if hit is not None:
                        vis = _is_stage_node_unlocked_visual(img_1280, hit[2])
                        if vis is True:
                            unlocked_confirmed = True
                        elif vis is False:
                            clearly_locked = True

                if not next_stage and not unlocked_confirmed:
                    # Also check if tutorial node itself now shows a cleared cyan badge or a new node appeared to its right
                    cur_up = str(stage_code or "").strip().upper()
                    vis_nodes_t = inspect_map_stage_nodes(img, "")
                    cur_nd_t = next((n for n in vis_nodes_t if n["code"] == cur_up), None)
                    if cur_nd_t and (cur_nd_t.get("cleared") or _is_node_bypassed_by_later_unlocked(cur_nd_t, vis_nodes_t)):
                        unlocked_confirmed = True

                if unlocked_confirmed:
                    _emit(
                        log_callback,
                        f"[教学关交接] 检测到 {stage_code} 已通关返回地图"
                        + (f"且下一关 {next_stage} 已解锁" if next_stage else "")
                        + "，自动收起提醒并恢复后续推图！",
                    )
                    return "auto_resumed"

                if left_map_seen and not clearly_locked:
                    map_hits += 1
                    if map_hits >= 2:
                        _emit(
                            log_callback,
                            f"[教学关交接] 检测到已从教学关 {stage_code} 返回关卡地图，自动收起提醒并恢复后续推图！",
                        )
                        return "auto_resumed"
                elif (time.time() - t0) >= 6.0 and not next_stage and left_map_seen:
                    return "auto_resumed"

        # Sleep in short slices so stop/manual-continue button reacts immediately
        slept = 0.0
        step = min(0.25, max(0.05, poll_interval))
        while slept < poll_interval:
            if cancel_event and cancel_event.is_set():
                return "cancelled"
            if manual_resume_event and manual_resume_event.is_set():
                manual_resume_event.clear()
                return "manual_resumed"
            time.sleep(step)
            slept += step


def _extract_stage_seq_num(code: str) -> Optional[int]:
    """Extract trailing numeric index from stage code (e.g. 'TL-2' -> 2, 'TL-TR-1' -> 1)."""
    m = re.search(r"-(\d+)$", str(code or "").strip().upper())
    return int(m.group(1)) if m else None


def _extract_battle_prefix(code: str) -> str:
    s = str(code or "").strip().upper()
    return s.rsplit("-", 1)[0] if "-" in s else ""


def _is_node_bypassed_by_later_unlocked(
    node: Dict[str, Any],
    all_nodes: List[Dict[str, Any]],
    *,
    x_tol: int = 20,
) -> bool:
    """Return True if another visible unlocked node proves this node has already been passed on the map."""
    code = str(node.get("code", "")).upper()
    node_type = str(node.get("node_type", "") or classify_stage_node(code))
    cx = int(node.get("cx", node.get("x", 0)))
    frame_idx = int(node.get("_frame_idx", 0))
    seq_num = _extract_stage_seq_num(code)
    battle_pref = _extract_battle_prefix(code)

    for other in all_nodes:
        o_code = str(other.get("code", "")).upper()
        if not o_code or o_code == code:
            continue
        o_frame = int(other.get("_frame_idx", 0))
        o_cx = int(other.get("cx", other.get("x", 0)))
        o_type = str(other.get("node_type", "") or classify_stage_node(o_code))
        o_num = _extract_stage_seq_num(o_code)

        # Any unlocked node on a subsequent rightward swipe frame or strictly to the right on the same frame
        if o_frame > frame_idx:
            return True
        if o_frame == frame_idx and o_cx > cx + x_tol:
            return True

        # For battle stages in the same chapter (e.g. TL-1 when TL-2 is already visibly unlocked):
        # a higher-numbered unlocked battle node proves the earlier battle node has already been cleared.
        if (
            node_type == "battle"
            and o_type == "battle"
            and battle_pref
            and _extract_battle_prefix(o_code) == battle_pref
            and seq_num is not None
            and o_num is not None
            and o_num > seq_num
        ):
            return True

        # For -ST- / -TR- nodes: if a combat stage is already unlocked at/right of this column
        if node_type in ("story", "tutorial") and o_type == "battle":
            if o_frame == frame_idx and o_cx >= cx - x_tol:
                return True
            if (
                o_frame == frame_idx
                and abs(o_cx - cx) <= x_tol
                and seq_num is not None
                and o_num is not None
                and o_num >= seq_num
            ):
                return True
    return False


def discover_unlocked_special_prereqs(
    prefix: str,
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    target_combat_stage: str = "",
    frontier_stage: str = "",
    max_swipes: int = 0,
    cancel_event: Optional[threading.Event] = None,
) -> List[str]:
    """Scan current map (and optionally swipe right) for blocking uncleared -ST- / -TR- nodes before target_combat_stage."""
    pref_clean = str(prefix or "").strip().upper().rstrip("-")
    if not pref_clean:
        return []
    target_up = str(target_combat_stage or "").strip().upper()
    frontier_up = str(frontier_stage or "").strip().upper()
    target_num = _extract_stage_seq_num(target_up) if target_up and classify_stage_node(target_up) == "battle" else None
    frontier_num = (
        _extract_stage_seq_num(frontier_up) if frontier_up and classify_stage_node(frontier_up) == "battle" else None
    )

    # Rule 1: If caller already confirmed a combat frontier at or past target_combat_stage, no prereq blocks it.
    if frontier_up and target_up and frontier_up == target_up and classify_stage_node(frontier_up) == "battle":
        return []
    if frontier_num is not None and target_num is not None and frontier_num >= target_num:
        return []

    img = adb_screencap_image(adb_path, adb_addr)
    if img is None:
        return []
    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    if not st["on_stage_map"]:
        return []

    merged_nodes: Dict[str, Dict[str, Any]] = {}
    cur_img = img
    for swipe_idx in range(max(0, int(max_swipes)) + 1):
        if cancel_event and cancel_event.is_set():
            break
        frame_nodes = inspect_map_stage_nodes(cur_img, pref_clean)
        new_added = 0
        for nd in frame_nodes:
            c = nd["code"]
            if c not in merged_nodes:
                item = dict(nd)
                item["_frame_idx"] = swipe_idx
                merged_nodes[c] = item
                new_added += 1
            else:
                merged_nodes[c]["cleared"] = bool(merged_nodes[c]["cleared"] or nd["cleared"])
                merged_full = bool(merged_nodes[c].get("badge_full_3star") or nd.get("badge_full_3star"))
                merged_nodes[c]["badge_full_3star"] = merged_full
                merged_nodes[c]["cleared_3star"] = merged_full
                merged_nodes[c]["partial_star"] = bool(
                    (merged_nodes[c].get("partial_star") or nd.get("partial_star")) and not merged_full
                )

        nodes = list(merged_nodes.values())
        battle_nodes = [n for n in nodes if n["node_type"] == "battle"]

        # Rule 1: In Arknights maps, visible nodes are already unlocked. If target_combat_stage (or any
        # battle stage at/beyond it) is already visibly unlocked on screen, all earlier -ST-/-TR- nodes are passed.
        if target_up and target_up in merged_nodes:
            return []
        if target_num is not None:
            for bn in battle_nodes:
                bn_num = _extract_stage_seq_num(bn["code"])
                if bn_num is not None and bn_num >= target_num:
                    return []

        has_visible_special = any(
            nd["node_type"] in ("story", "tutorial")
            and (not nd["cleared"])
            and (not _is_node_bypassed_by_later_unlocked(nd, nodes))
            for nd in nodes
        )
        if has_visible_special:
            break
        if swipe_idx >= max_swipes or (swipe_idx > 0 and new_added == 0):
            break
        adb_swipe_norm(adb_path, adb_addr, 960, 360, 320, 360, duration_ms=350, width=cur_img.width, height=cur_img.height)
        time.sleep(0.6)
        next_img = adb_screencap_image(adb_path, adb_addr)
        if next_img is None:
            break
        cur_img = next_img

    nodes = list(merged_nodes.values())
    if not nodes:
        return []

    blocking: List[Dict[str, Any]] = []
    for nd in nodes:
        if nd["node_type"] not in ("story", "tutorial"):
            continue
        # Rule 2: Already-cleared (cyan/gold hexagon) -ST- / -TR- nodes are NEVER blocking prerequisites.
        if nd["cleared"]:
            continue
        # Rule 1 & Rule 2: Only treat as blocking when it sits at the rightmost uncleared frontier
        # (no unlocked next stage to its right and no unlocked combat stage at/right of it).
        if _is_node_bypassed_by_later_unlocked(nd, nodes):
            continue
        blocking.append(nd)

    blocking.sort(key=lambda n: (int(n.get("_frame_idx", 0)), n["cx"], 0 if n["node_type"] == "story" else 1, n["code"]))
    return [n["code"] for n in blocking]


GENERIC_EVENT_STAGE_RE = _UNIVERSAL_STAGE_SEARCH_RE
_GENERIC_IGNORED_PREFIXES = frozenset({"PR", "CE", "LS", "AP", "SK", "CA", "TR", "ST", "EG", "END", "EX", "MO"})


def _extract_nodes_from_ocr_boxes(
    img_1280: Image.Image,
    boxes: List[Dict[str, Any]],
    prefix: str = "",
    dark_cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Extract ordered stage nodes from WinOCR boxes (prefix='' scans all valid stage formats across the map)."""
    pref_clean = str(prefix or "").strip().upper().rstrip("-")
    pat = (
        re.compile(rf"^({re.escape(pref_clean)}-(?:[A-Z0-9]{{1,5}}-)?[A-Z0-9]*\d[A-Z0-9]*)$")
        if pref_clean
        else None
    )

    header_hints: List[Tuple[int, int, str]] = []
    for hb in boxes:
        h_up = re.sub(r"[^A-Z0-9]", "", str(hb.get("text", "") or "").strip().upper())
        if "TUTOR" in h_up or h_up in ("STORY", "ST0RY", "5TORY"):
            hcx = int(hb.get("x", 0)) + int(hb.get("w", 0)) // 2
            hcy = int(hb.get("y", 0)) + int(hb.get("h", 0)) // 2
            header_hints.append((hcx, hcy, h_up))

    d_cards = list(dark_cards) if dark_cards is not None else []
    nodes: Dict[str, Dict[str, Any]] = {}
    for b in boxes:
        x_pos = int(b.get("x", 0))
        bw = max(1, int(b.get("w", 0)))
        y_pos = int(b.get("y", 0))
        bh = max(1, int(b.get("h", 0)))
        cx = int(x_pos + bw // 2)
        cy = int(y_pos + bh // 2)

        raw_txt = str(b.get("text", "") or "")
        norm_txt = str(b.get("norm", "") or "")

        if pat is not None:
            cand = _normalize_ocr_stage_token(raw_txt, expected_prefix=pref_clean)
            m = pat.match(cand) or pat.match(norm_txt)
            if not m:
                continue
            code = m.group(1).upper()
            if not is_valid_stage_code(code):
                continue
        else:
            # Playfield guard: exclude top status bar (y<68) and bottom episode switcher / currency bar (y>630)
            if not (10 <= cx <= 1270 and 68 <= cy <= 630):
                continue
            code = extract_universal_stage_code(raw_txt) or extract_universal_stage_code(norm_txt)
            if not code or not is_valid_stage_code(code):
                continue

        matched_hdr = ""
        for hcx, hcy, h_up in header_hints:
            if abs(cx - hcx) <= 75 and 4 <= (cy - hcy) <= 36:
                matched_hdr = h_up
                break

        overlaps_dark_card = any(
            (int(dc["x"]) - 12) <= cx <= (int(dc["x"]) + int(dc["w"]) + 12)
            and (int(dc["y"]) - 8) <= cy <= (int(dc["y"]) + int(dc["h"]) + 8)
            for dc in d_cards
        )
        node_type = classify_stage_node(code, header_hint=matched_hdr)
        badge_info = _inspect_stage_node_badge(img_1280, b, node_type=node_type)
        cleared = bool(badge_info.get("has_badge"))
        full_3 = bool(badge_info.get("badge_full_3star"))
        partial = bool(badge_info.get("badge_partial_star"))
        white_ratio = _measure_stage_card_white_ratio(img_1280, b)
        dark_ratio = _measure_stage_card_dark_ratio(img_1280, b)
        is_dark = bool(overlaps_dark_card or (dark_ratio >= 0.48 and white_ratio < 0.25) or b.get("pass") in ("dark_card_roi", "tut_inv"))
        card_style = "dark" if is_dark else "white"
        is_white_unlocked = bool((not cleared) and (not is_dark) and white_ratio >= 0.45)
        is_unlocked = bool((not cleared) and (white_ratio >= 0.45 or is_dark))

        if code not in nodes:
            nodes[code] = {
                "code": code,
                "node_type": node_type,
                "cleared": cleared,
                "has_cleared_badge": cleared,
                "cleared_3star": full_3,
                "badge_full_3star": full_3,
                "partial_star": partial,
                "white_card_ratio": round(white_ratio, 3),
                "dark_card_ratio": round(dark_ratio, 3),
                "card_style": card_style,
                "is_dark_card": is_dark,
                "is_white_unlocked_card": is_white_unlocked,
                "is_unlocked_card": is_unlocked,
                "header_hint": matched_hdr,
                "x": x_pos,
                "cx": cx,
                "cy": cy,
            }
        else:
            merged_cleared = bool(nodes[code]["cleared"] or cleared)
            merged_full = bool(nodes[code].get("badge_full_3star") or full_3)
            merged_partial = bool((nodes[code].get("partial_star") or partial) and not merged_full)
            best_white = max(float(nodes[code].get("white_card_ratio", 0.0)), white_ratio)
            best_dark = max(float(nodes[code].get("dark_card_ratio", 0.0)), dark_ratio)
            merged_dark = bool(nodes[code].get("is_dark_card") or is_dark)
            nodes[code]["cleared"] = merged_cleared
            nodes[code]["has_cleared_badge"] = merged_cleared
            nodes[code]["cleared_3star"] = merged_full
            nodes[code]["badge_full_3star"] = merged_full
            nodes[code]["partial_star"] = merged_partial
            nodes[code]["white_card_ratio"] = round(best_white, 3)
            nodes[code]["dark_card_ratio"] = round(best_dark, 3)
            nodes[code]["is_dark_card"] = merged_dark
            nodes[code]["card_style"] = "dark" if merged_dark else "white"
            nodes[code]["is_white_unlocked_card"] = bool((not merged_cleared) and (not merged_dark) and best_white >= 0.45)
            nodes[code]["is_unlocked_card"] = bool((not merged_cleared) and (best_white >= 0.45 or merged_dark))
            if matched_hdr and not nodes[code].get("header_hint"):
                nodes[code]["header_hint"] = matched_hdr
                nodes[code]["node_type"] = classify_stage_node(code, header_hint=matched_hdr)

    return sorted(
        nodes.values(),
        key=lambda n: (n["x"], 0 if n["node_type"] == "story" else (1 if n["node_type"] == "tutorial" else 2), n["code"]),
    )


def inspect_map_stage_nodes(img: Image.Image, prefix: str = "") -> List[Dict[str, Any]]:
    """Detect visible stage nodes on a map screenshot (prefix='' scans all valid formats across the screen)."""
    pref_clean = str(prefix or "").strip().upper().rstrip("-")
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    dark_cards = detect_dark_stage_cards(img)
    boxes = find_stage_boxes_via_ocr(img)
    return _extract_nodes_from_ocr_boxes(img_1280, boxes, pref_clean, dark_cards=dark_cards)


def scan_all_map_stage_nodes(img: Image.Image) -> List[Dict[str, Any]]:
    """Prefix-agnostic full-screen stage node scanner (mainline, SideStory, TR/ST, XX-XX-XX)."""
    return inspect_map_stage_nodes(img, prefix="")


def select_next_frontier_node(
    nodes: List[Dict[str, Any]],
    completed_stages: Optional[Iterable[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Pick the leftmost uncleared / non-3-star frontier stage node from scanned map nodes."""
    done_set = {str(s or "").strip().upper() for s in (completed_stages or []) if str(s or "").strip()}
    pending: List[Dict[str, Any]] = []
    for nd in nodes:
        code_u = str(nd.get("code", "")).strip().upper()
        if not code_u or code_u in done_set:
            continue
        ntype = str(nd.get("node_type", "") or classify_stage_node(code_u))
        cleared = bool(nd.get("cleared"))
        if ntype == "battle":
            is_full_3star = bool(cleared and nd.get("badge_full_3star") and not nd.get("partial_star"))
            if is_full_3star:
                continue
        else:
            if cleared:
                continue
            cx_i = int(nd.get("cx", nd.get("x", 0)))
            f_i = int(nd.get("_frame_idx", 0))
            bypassed_by_cleared = any(
                str(o.get("code", "")).strip().upper() != code_u
                and bool(o.get("cleared"))
                and (
                    int(o.get("_frame_idx", 0)) > f_i
                    or (int(o.get("_frame_idx", 0)) == f_i and int(o.get("cx", o.get("x", 0))) > cx_i + 20)
                )
                for o in nodes
            )
            if bypassed_by_cleared:
                continue
        pending.append(nd)

    if not pending:
        return None
    pending.sort(
        key=lambda n: (
            int(n.get("_frame_idx", 0)),
            int(n.get("cx", n.get("x", 0))),
            0 if n.get("node_type") == "story" else (1 if n.get("node_type") == "tutorial" else 2),
            n["code"],
        )
    )
    return pending[0]


def discover_next_uncleared_stage_on_map(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    completed_stages: Optional[Iterable[str]] = None,
    max_swipes: int = 8,
    unchanged_swipe_limit: int = 2,
    mse_threshold: float = 12.0,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Prefix-agnostic visual scan -> select leftmost non-3-star node -> auto swipe right -> stop after 2 unchanged swipes."""
    if not adb_path or not adb_addr:
        return {
            "found": False,
            "on_stage_map": False,
            "all_cleared": False,
            "next_stage": "",
            "next_type": "",
            "visible_nodes": [],
            "swipes_done": 0,
            "reason": "missing_adb_connection",
        }
    if cancel_event and cancel_event.is_set():
        return {
            "found": False,
            "on_stage_map": False,
            "all_cleared": False,
            "next_stage": "",
            "next_type": "",
            "visible_nodes": [],
            "swipes_done": 0,
            "cancelled": True,
            "reason": "cancelled",
        }

    img = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
    if img is None:
        return {
            "found": False,
            "on_stage_map": False,
            "all_cleared": False,
            "next_stage": "",
            "next_type": "",
            "visible_nodes": [],
            "swipes_done": 0,
            "reason": "screencap_failed",
        }

    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    reset_res = ensure_on_stage_map_before_search(
        adb_path,
        adb_addr,
        maa_dir,
        cancel_event=cancel_event,
        log_callback=log_callback,
        initial_img=img,
        initial_state=st,
    )
    if reset_res.get("image") is not None:
        img = reset_res["image"]
        st = reset_res["state"]
    if st.get("has_story_start_btn"):
        adb_tap_norm(adb_path, adb_addr, STORY_CARD_DISMISS_XY[0], STORY_CARD_DISMISS_XY[1], img.width, img.height)
        time.sleep(0.6)
        img2 = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
        if img2 is not None:
            img = img2
            st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    elif st.get("has_stage_start_btn"):
        bx, by = find_safe_blank_tap_point(img)
        adb_tap_norm(adb_path, adb_addr, bx, by, img.width, img.height)
        time.sleep(0.6)
        img2 = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
        if img2 is not None:
            img = img2
            st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)

    if not st.get("on_stage_map"):
        return {
            "found": False,
            "on_stage_map": False,
            "all_cleared": False,
            "next_stage": "",
            "next_type": "",
            "visible_nodes": [],
            "swipes_done": 0,
            "reason": "not_on_stage_map",
        }

    cur_img = img
    seen_codes: set[str] = set()
    last_nodes: List[Dict[str, Any]] = []
    unchanged_swipes = 0
    swipes_done = 0

    for swipe_idx in range(max(0, int(max_swipes)) + 1):
        if cancel_event and cancel_event.is_set():
            return {
                "found": False,
                "on_stage_map": True,
                "all_cleared": False,
                "next_stage": "",
                "next_type": "",
                "visible_nodes": last_nodes,
                "swipes_done": swipes_done,
                "cancelled": True,
                "reason": "cancelled",
            }
        frame_nodes = inspect_map_stage_nodes(cur_img, "")
        last_nodes = frame_nodes
        for nd in frame_nodes:
            seen_codes.add(str(nd.get("code", "")).upper())

        picked = select_next_frontier_node(frame_nodes, completed_stages=completed_stages)
        if picked is not None:
            vis_brief = [
                f"{n['code']}("
                + ("非满星" if n.get("partial_star") else ("已通" if n.get("cleared") else ("黑底教学" if n.get("is_dark_card") else "未通")))
                + ")"
                for n in frame_nodes
            ]
            _emit(
                log_callback,
                f"[MAP][动态看图] 扫描节点={vis_brief} -> 锁定前沿={picked['code']} ({picked['node_type']})",
            )
            return {
                "found": True,
                "on_stage_map": True,
                "all_cleared": False,
                "next_stage": picked["code"],
                "next_type": picked["node_type"],
                "next_node": picked,
                "visible_nodes": frame_nodes,
                "swipes_done": swipes_done,
                "reason": "frontier_found",
            }

        if swipe_idx >= max(0, int(max_swipes)):
            break

        # All visible nodes on this screen are cleared -> ensure detail drawer closed, then swipe right-to-left
        cur_st = analyze_screen_state(cur_img, maa_dir, use_ocr_fallback=False)
        if cur_st.get("has_story_start_btn"):
            _emit(log_callback, "[MAP][动态看图] 左滑前检测到剧情卡片弹窗，先点击空白处关闭后再左滑...")
            adb_tap_norm(adb_path, adb_addr, STORY_CARD_DISMISS_XY[0], STORY_CARD_DISMISS_XY[1], cur_img.width, cur_img.height)
            time.sleep(0.6)
            refreshed = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
            if refreshed is not None:
                cur_img = refreshed
        elif cur_st.get("has_stage_start_btn"):
            bx, by = find_safe_blank_tap_point(cur_img)
            _emit(log_callback, f"[MAP][动态看图] 左滑前检测到右侧详情面板处于展开状态，先点击安全空白点 ({bx}, {by}) 关闭避免拦截拖拽手势...")
            adb_tap_norm(adb_path, adb_addr, bx, by, cur_img.width, cur_img.height)
            time.sleep(0.6)
            refreshed = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
            if refreshed is not None:
                cur_img = refreshed

        swipe_tracks = (
            (396, 650, "0.55"),
            (252, 800, "0.35"),
            (468, 850, "0.65"),
        )
        swipe_y, swipe_dur, y_label = swipe_tracks[min(unchanged_swipes, len(swipe_tracks) - 1)]
        if unchanged_swipes > 0:
            _emit(
                log_callback,
                f"[MAP][动态看图] 上次左滑画面几乎未动，切换纵坐标安全带 y={y_label} ({swipe_y}) 并加大耗时 ({swipe_dur}ms) 重试左滑...",
            )
        else:
            _emit(
                log_callback,
                f"[MAP][动态看图] 当前画面可见节点 {sorted(c for c in seen_codes)} 已全部 3 星通关，沿安全带 y={y_label} 左滑翻页探索右侧新关...",
            )
        adb_swipe_norm(
            adb_path,
            adb_addr,
            998,
            swipe_y,
            282,
            swipe_y,
            duration_ms=swipe_dur,
            width=cur_img.width,
            height=cur_img.height,
            cancel_event=cancel_event,
        )
        swipes_done += 1
        time.sleep(1.0)
        next_img = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
        if next_img is None:
            break
        mse = _compute_image_diff_mse(cur_img, next_img)
        cur_img = next_img
        if mse < float(mse_threshold):
            unchanged_swipes += 1
            if unchanged_swipes >= max(1, int(unchanged_swipe_limit)):
                _emit(
                    log_callback,
                    f"[MAP][动态看图] 连续 {unchanged_swipes} 次左滑画面无变化（MSE={mse:.1f}）且无新的未通节点，判定当前章节/活动已全通。",
                )
                break
        else:
            unchanged_swipes = 0

    if seen_codes:
        return {
            "found": False,
            "on_stage_map": True,
            "all_cleared": True,
            "next_stage": "",
            "next_type": "",
            "visible_nodes": last_nodes,
            "swipes_done": swipes_done,
            "reason": "all_cleared",
        }
    return {
        "found": False,
        "on_stage_map": True,
        "all_cleared": False,
        "next_stage": "",
        "next_type": "",
        "visible_nodes": last_nodes,
        "swipes_done": swipes_done,
        "reason": "no_stage_nodes_detected",
    }


def _match_generic_event_token(raw_text: str) -> Optional[Tuple[str, str, int]]:
    """Match any valid stage token (mainline, SideStory, TR/ST, XX-XX-XX) -> (prefix, code, num)."""
    cand = extract_universal_stage_code(raw_text)
    if not cand or not is_valid_stage_code(cand):
        return None
    pref = cand.split("-", 1)[0]
    m_num = re.search(r"(\d+)$", cand)
    num = int(m_num.group(1)) if m_num else 0
    return pref, cand, num


def discover_event_from_map_image(
    img: Image.Image,
    maa_dir: Optional[Path] = None,
    default_max_stage: int = 10,
    known_battle_stages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Scan a map screenshot prefix-agnostically (mainline, SideStory, TR/ST, XX-XX-XX) to detect frontier & queue."""
    if img is None:
        return {"found": False, "on_stage_map": False, "prefix": "", "visible_nodes": [], "frontier_stage": "", "frontier_type": "", "push_sequence": [], "reason": "no_image"}
    if maa_dir is not None:
        st = analyze_screen_state(img, Path(maa_dir), use_ocr_fallback=False)
        if not st.get("on_stage_map"):
            return {"found": False, "on_stage_map": False, "prefix": "", "visible_nodes": [], "frontier_stage": "", "frontier_type": "", "push_sequence": [], "reason": "not_on_stage_map"}

    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    dark_cards = detect_dark_stage_cards(img)
    boxes = find_stage_boxes_via_ocr(img)
    all_nodes = _extract_nodes_from_ocr_boxes(img_1280, boxes, prefix="", dark_cards=dark_cards)

    if not all_nodes:
        return {"found": False, "on_stage_map": True, "prefix": "", "visible_nodes": [], "frontier_stage": "", "frontier_type": "", "push_sequence": [], "reason": "no_event_nodes_detected"}

    # Determine dominant chapter/event prefix (prefer non-TR/ST/EG prefixes when mixed like 16-2 + TR-27)
    pref_counts: Dict[str, int] = {}
    for nd in all_nodes:
        p0 = str(nd.get("code", "")).split("-", 1)[0]
        if p0:
            pref_counts[p0] = pref_counts.get(p0, 0) + 1
    non_special_prefs = [p for p in pref_counts if p not in ("TR", "ST", "EG")]
    best_pref = max(non_special_prefs or list(pref_counts.keys()), key=lambda p: (pref_counts[p], p))

    picked = select_next_frontier_node(all_nodes)
    if picked is not None:
        frontier_stage = picked["code"]
    else:
        battle_nums = [
            _extract_stage_seq_num(nd["code"]) or 0
            for nd in all_nodes
            if nd.get("node_type") == "battle"
        ]
        next_num = (max(battle_nums) + 1) if battle_nums else 1
        frontier_stage = f"{best_pref}-{next_num}"

    known_up = [str(s).strip().upper() for s in (known_battle_stages or []) if str(s).strip()]
    if known_up:
        battle_seq = known_up
    elif best_pref and all(
        str(n.get("code", "")).startswith(best_pref + "-") for n in all_nodes if n.get("node_type") == "battle"
    ) and best_pref not in ("TR", "ST", "EG") and not best_pref.isdigit():
        max_seen_battle = max(
            [_extract_stage_seq_num(nd["code"]) or 0 for nd in all_nodes if nd.get("node_type") == "battle"] or [0]
        )
        end_num = max(int(default_max_stage), max_seen_battle)
        battle_seq = [f"{best_pref}-{i}" for i in range(1, end_num + 1)]
    else:
        battle_seq = [nd["code"] for nd in all_nodes if nd.get("node_type") == "battle" and not nd.get("cleared")]
        if not battle_seq and classify_stage_node(frontier_stage) == "battle":
            battle_seq = [frontier_stage]

    target_up = battle_seq[-1] if battle_seq else ""
    push_seq = _build_sequential_push_chain(frontier_stage, target_up, battle_seq, visible_nodes=all_nodes)
    return {
        "found": True,
        "on_stage_map": True,
        "prefix": best_pref,
        "visible_nodes": all_nodes,
        "frontier_stage": frontier_stage,
        "frontier_type": classify_stage_node(frontier_stage, header_hint=(picked or {}).get("header_hint", "")),
        "push_sequence": push_seq,
    }


def discover_active_event_from_map(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    default_max_stage: int = 10,
    max_swipes: int = 3,
    known_battle_stages_resolver: Optional[Callable[[str], List[str]]] = None,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Capture emulator map screen via ADB, OCR-detect current SideStory prefix & build sequential push queue."""
    img = adb_screencap_image(adb_path, adb_addr, cancel_event=cancel_event)
    if img is None:
        return {"found": False, "on_stage_map": False, "prefix": "", "visible_nodes": [], "frontier_stage": "", "frontier_type": "", "push_sequence": [], "reason": "screencap_failed"}

    info = discover_event_from_map_image(img, maa_dir=maa_dir, default_max_stage=default_max_stage)
    if not info.get("found"):
        return info

    pref = str(info["prefix"])
    known_stages: List[str] = []
    if known_battle_stages_resolver is not None:
        try:
            known_stages = [str(s).strip().upper() for s in (known_battle_stages_resolver(pref) or []) if str(s).strip()]
        except Exception:
            known_stages = []

    if known_stages:
        info = discover_event_from_map_image(
            img,
            maa_dir=maa_dir,
            default_max_stage=default_max_stage,
            known_battle_stages=known_stages,
        )

    # If all nodes on initial frame are already cleared, swipe right via detect_map_frontier_stage
    has_visible_uncleared = any(
        (not nd["cleared"]) and (not _is_node_bypassed_by_later_unlocked(nd, info.get("visible_nodes") or []))
        for nd in (info.get("visible_nodes") or [])
    )
    if not has_visible_uncleared and max_swipes > 0:
        battle_seq = known_stages or [f"{pref}-{i}" for i in range(1, max(1, int(default_max_stage)) + 1)]
        f_info = detect_map_frontier_stage(
            pref + "-",
            adb_path,
            adb_addr,
            maa_dir,
            target_stage=battle_seq[-1],
            known_battle_stages=battle_seq,
            max_swipes=max_swipes,
            cancel_event=cancel_event,
            log_callback=log_callback,
        )
        if f_info.get("on_stage_map") and f_info.get("push_sequence"):
            info["frontier_stage"] = f_info.get("frontier_stage", info["frontier_stage"])
            info["frontier_type"] = f_info.get("frontier_type", info["frontier_type"])
            info["push_sequence"] = f_info.get("push_sequence", info["push_sequence"])
            info["visible_nodes"] = f_info.get("visible_nodes", info["visible_nodes"])

    if log_callback:
        vis_brief = [
            (
                f"{n['code']}"
                + (
                    "(非满星缺口)"
                    if n.get("partial_star")
                    else (
                        "(已通蓝标)"
                        if n.get("cleared")
                        else ("(未通白框)" if n.get("is_white_unlocked_card") else "(前沿)")
                    )
                )
            )
            for n in (info.get("visible_nodes") or [])
        ]
        _emit(
            log_callback,
            f"[MAP][看图识新活动] OCR 识别到当期活动前缀={pref} | 可见节点={vis_brief} | 前沿={info.get('frontier_stage') or '未知'} | 构造推进队列={info.get('push_sequence')}",
        )
    return info


def _build_sequential_push_chain(
    frontier_stage: str,
    target_stage: str,
    known_battle_stages: Optional[List[str]] = None,
    visible_nodes: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Build ordered stage list from frontier_stage up to target_stage, skipping 3-star cleared battle nodes."""
    f_code = str(frontier_stage or "").strip().upper()
    t_code = str(target_stage or "").strip().upper()
    if not f_code:
        return []

    battle_list = [str(s).strip().upper() for s in (known_battle_stages or []) if str(s).strip()]
    partial_set: set[str] = set()
    full_3star_set: set[str] = set()
    cleared_battle_indices: List[int] = []
    partial_visible_order: List[str] = []

    for nd in (visible_nodes or []):
        if nd.get("node_type") != "battle":
            continue
        code_u = str(nd.get("code", "")).strip().upper()
        if not code_u:
            continue
        is_partial = bool(nd.get("partial_star")) and not bool(nd.get("badge_full_3star"))
        if is_partial:
            partial_set.add(code_u)
            if code_u not in partial_visible_order:
                partial_visible_order.append(code_u)
        elif nd.get("cleared"):
            full_3star_set.add(code_u)
        if nd.get("cleared") and code_u in battle_list:
            cleared_battle_indices.append(battle_list.index(code_u))

    partial_ordered = [st for st in battle_list if st in partial_set] + [
        st for st in partial_visible_order if st not in battle_list
    ]

    f_type = classify_stage_node(f_code)
    chain: List[str] = []

    if f_type != "battle":
        chain.append(f_code)
        for p_st in partial_ordered:
            if p_st not in chain:
                chain.append(p_st)
        if battle_list:
            start_idx = (max(cleared_battle_indices) + 1) if cleared_battle_indices else 0
            for st in battle_list[start_idx:]:
                if st in full_3star_set:
                    continue
                if st not in chain:
                    chain.append(st)
                if t_code and st == t_code:
                    break
        elif t_code and t_code != f_code and t_code not in full_3star_set:
            chain.append(t_code)
        return chain

    if battle_list and f_code in battle_list:
        start_i = battle_list.index(f_code)
        if t_code and t_code in battle_list:
            end_i = battle_list.index(t_code)
            tail = battle_list[start_i : end_i + 1] if end_i >= start_i else battle_list[start_i:]
        else:
            tail = battle_list[start_i:]
    else:
        tail = [f_code]
        if t_code and t_code != f_code:
            tail.append(t_code)

    for p_st in partial_ordered:
        if p_st not in tail and p_st not in chain:
            chain.append(p_st)
    for st in tail:
        if st != f_code and st in full_3star_set:
            continue
        if st not in chain:
            chain.append(st)
    return chain


def detect_map_frontier_stage(
    prefix: str,
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    target_stage: str = "",
    known_battle_stages: Optional[List[str]] = None,
    max_swipes: int = 3,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Scan current event/chapter map to identify the actual unlocked frontier stage and push sequence."""
    pref_clean = str(prefix or "").strip().upper().rstrip("-")
    target_up = str(target_stage or "").strip().upper()
    battle_seq = [str(s).strip().upper() for s in (known_battle_stages or []) if str(s).strip()]

    img = adb_screencap_image(adb_path, adb_addr)
    if img is None:
        return {
            "on_stage_map": False,
            "visible_nodes": [],
            "target_visible": False,
            "target_cleared": False,
            "frontier_stage": "",
            "frontier_type": "",
            "push_sequence": [],
            "reason": "screencap_failed",
        }

    st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    if not st["on_stage_map"]:
        return {
            "on_stage_map": False,
            "visible_nodes": [],
            "target_visible": False,
            "target_cleared": False,
            "frontier_stage": "",
            "frontier_type": "",
            "push_sequence": [],
            "reason": "not_on_stage_map",
        }

    merged_nodes: Dict[str, Dict[str, Any]] = {}
    cur_img = img
    for swipe_idx in range(max(0, int(max_swipes)) + 1):
        if cancel_event and cancel_event.is_set():
            break
        frame_nodes = inspect_map_stage_nodes(cur_img, pref_clean)
        if not frame_nodes and pref_clean:
            frame_nodes = inspect_map_stage_nodes(cur_img, "")
        new_added = 0
        for nd in frame_nodes:
            c = nd["code"]
            if c not in merged_nodes:
                item = dict(nd)
                item["_frame_idx"] = swipe_idx
                merged_nodes[c] = item
                new_added += 1
            else:
                merged_nodes[c]["cleared"] = bool(merged_nodes[c]["cleared"] or nd["cleared"])
                merged_full = bool(merged_nodes[c].get("badge_full_3star") or nd.get("badge_full_3star"))
                merged_nodes[c]["badge_full_3star"] = merged_full
                merged_nodes[c]["cleared_3star"] = merged_full
                merged_nodes[c]["partial_star"] = bool(
                    (merged_nodes[c].get("partial_star") or nd.get("partial_star")) and not merged_full
                )

        all_merged = list(merged_nodes.values())
        has_uncleared = any(
            (not nd["cleared"]) and (not _is_node_bypassed_by_later_unlocked(nd, all_merged))
            for nd in all_merged
        )
        if (target_up and target_up in merged_nodes) or has_uncleared:
            break
        if swipe_idx >= max_swipes or (swipe_idx > 0 and new_added == 0):
            break
        adb_swipe_norm(adb_path, adb_addr, 960, 360, 320, 360, duration_ms=350, width=cur_img.width, height=cur_img.height)
        time.sleep(0.6)
        next_img = adb_screencap_image(adb_path, adb_addr)
        if next_img is None:
            break
        cur_img = next_img

    all_merged = list(merged_nodes.values())
    ordered_nodes = sorted(
        all_merged,
        key=lambda n: (
            int(n.get("_frame_idx", 0)),
            int(n.get("cx", n.get("x", 0))),
            0 if n["node_type"] == "story" else (1 if n["node_type"] == "tutorial" else 2),
            battle_seq.index(n["code"]) if n["code"] in battle_seq else 9999,
            n["code"],
        ),
    )

    target_node = merged_nodes.get(target_up) if target_up else None
    target_visible = target_node is not None
    target_cleared = bool(target_node and target_node["cleared"])

    uncleared = [
        nd for nd in ordered_nodes
        if (not nd["cleared"]) and (not _is_node_bypassed_by_later_unlocked(nd, all_merged))
    ]
    frontier_stage = ""
    if uncleared:
        frontier_stage = uncleared[-1]["code"]
    elif target_visible and not target_cleared:
        frontier_stage = target_up
    elif battle_seq:
        cleared_battles = [nd["code"] for nd in ordered_nodes if nd["node_type"] == "battle" and nd["cleared"]]
        if cleared_battles:
            last_idx = max((battle_seq.index(c) for c in cleared_battles if c in battle_seq), default=-1)
            if 0 <= last_idx + 1 < len(battle_seq):
                frontier_stage = battle_seq[last_idx + 1]
        elif ordered_nodes:
            frontier_stage = battle_seq[0]

    frontier_type = classify_stage_node(frontier_stage) if frontier_stage else ""
    if target_visible and target_cleared:
        push_seq: List[str] = []
    elif frontier_stage:
        push_seq = _build_sequential_push_chain(frontier_stage, target_up, battle_seq, visible_nodes=ordered_nodes)
    else:
        push_seq = []

    clean_visible_nodes = [{k: v for k, v in nd.items() if k != "_frame_idx"} for nd in ordered_nodes]

    if log_callback:
        vis_brief = [
            (
                f"{n['code']}"
                + (
                    "(非满星缺口)"
                    if n.get("partial_star")
                    else (
                        "(已通蓝标)"
                        if n.get("cleared")
                        else ("(未通白框)" if n.get("is_white_unlocked_card") else "(前沿)")
                    )
                )
            )
            for n in clean_visible_nodes
        ]
        _emit(
            log_callback,
            f"[MAP] 探测地图节点({pref_clean}): {vis_brief or '无'} | 前沿={frontier_stage or '未知'} | 推进序列={push_seq}",
        )

    return {
        "on_stage_map": True,
        "visible_nodes": clean_visible_nodes,
        "target_visible": target_visible,
        "target_cleared": target_cleared,
        "frontier_stage": frontier_stage,
        "frontier_type": frontier_type,
        "push_sequence": push_seq,
    }


def verify_stage_cleared_on_map(
    stage_code: str,
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    next_stage: Optional[str] = None,
    max_swipes: int = 2,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    settlement_stars: Optional[int] = None,
    require_three_stars: bool = True,
) -> Dict[str, Any]:
    """Closed-loop post-battle map verification: require 3-star clear (no non-3-star pass) and badge/next-unlock."""
    cur_up = str(stage_code or "").strip().upper()
    nxt_up = str(next_stage or "").strip().upper()
    node_type = classify_stage_node(cur_up)
    if not cur_up or cur_up.startswith("CAMP_") or "-" not in cur_up:
        return {
            "verified": True,
            "skipped": True,
            "on_stage_map": False,
            "stage_code": cur_up,
            "next_stage": nxt_up,
            "current_stage_cleared": False,
            "next_stage_unlocked": False,
            "three_star_verified": True,
            "reason": "non_chapter_stage",
        }

    if not adb_path or not adb_addr:
        return {
            "verified": False,
            "skipped": False,
            "on_stage_map": False,
            "stage_code": cur_up,
            "next_stage": nxt_up,
            "current_stage_cleared": False,
            "next_stage_unlocked": False,
            "reason": "missing_adb_connection",
        }

    pref_clean = cur_up.rsplit("-", 1)[0]
    cur_seq = _extract_stage_seq_num(cur_up)
    inferred_next = f"{pref_clean}-{cur_seq + 1}" if (cur_seq is not None and node_type == "battle") else ""
    eff_next = nxt_up or inferred_next

    if require_three_stars and node_type == "battle" and settlement_stars is not None and int(settlement_stars) < 3:
        _emit(
            log_callback,
            f"[MAP][非满星拦截] {cur_up} 结算星级仅为 {settlement_stars} 星（漏怪未满 3 星），严禁判胜！",
        )
        return {
            "verified": False,
            "skipped": False,
            "on_stage_map": True,
            "stage_code": cur_up,
            "next_stage": eff_next,
            "current_stage_cleared": True,
            "next_stage_unlocked": False,
            "non_3_star": True,
            "three_star_verified": False,
            "verified_stars": int(settlement_stars),
            "reason": f"battle_failed (non_3_star, stars={int(settlement_stars)})",
        }

    # If a right detail drawer is still open on the map, inspect it first or close it to reveal the map nodes
    img0 = adb_screencap_image(adb_path, adb_addr)
    if img0 is None:
        return {
            "verified": False,
            "skipped": False,
            "on_stage_map": False,
            "stage_code": cur_up,
            "next_stage": eff_next,
            "current_stage_cleared": False,
            "next_stage_unlocked": False,
            "reason": "screencap_failed",
        }
    st0 = analyze_screen_state(img0, maa_dir, use_ocr_fallback=False)
    if st0.get("is_battle_failed"):
        _emit(log_callback, f"[MAP][闭环校验失败] 当前画面仍为「行动失败」界面，确认 {cur_up} 未通关！")
        return {
            "verified": False,
            "skipped": False,
            "on_stage_map": False,
            "battle_failed_screen": True,
            "stage_code": cur_up,
            "next_stage": eff_next,
            "current_stage_cleared": False,
            "next_stage_unlocked": False,
            "reason": "battle_failed_screen",
        }
    if st0.get("has_story_start_btn"):
        adb_tap_norm(adb_path, adb_addr, STORY_CARD_DISMISS_XY[0], STORY_CARD_DISMISS_XY[1], img0.width, img0.height)
        time.sleep(0.6)
    elif node_type != "story" and st0.get("has_stage_start_btn"):
        panel0 = inspect_stage_detail_panel(img0, maa_dir, expected_stage=cur_up, use_ocr=True)
        if panel0.get("title_matches"):
            sc0 = int(panel0.get("star_count") or 0)
            if require_three_stars and node_type == "battle" and not panel0.get("has_three_stars"):
                _emit(
                    log_callback,
                    f"[MAP][非满星拦截] 详情面板显示 {cur_up} 仅 {sc0} 颗亮星（未满 3 星），严禁放行！",
                )
                return {
                    "verified": False,
                    "skipped": False,
                    "on_stage_map": True,
                    "stage_code": cur_up,
                    "next_stage": eff_next,
                    "current_stage_cleared": sc0 > 0,
                    "next_stage_unlocked": False,
                    "non_3_star": True,
                    "three_star_verified": False,
                    "verified_stars": sc0,
                    "detail_panel_stars": sc0,
                    "visible_nodes": [],
                    "reason": f"battle_failed (non_3_star, stars={sc0})",
                }
            if panel0.get("already_cleared_3star") or panel0.get("has_three_stars"):
                _emit(log_callback, f"[MAP][闭环校验通过] 详情面板确认 {cur_up} 已 3 星通关。")
                return {
                    "verified": True,
                    "skipped": False,
                    "on_stage_map": True,
                    "stage_code": cur_up,
                    "next_stage": eff_next,
                    "current_stage_cleared": True,
                    "next_stage_unlocked": False,
                    "non_3_star": False,
                    "three_star_verified": True,
                    "verified_stars": 3,
                    "detail_panel_stars": sc0 or 3,
                    "visible_nodes": [],
                    "reason": "verified_by_detail_panel_3star",
                }
        # Close detail drawer dynamically avoiding visible map nodes so blue hexagon / next stage are unobstructed
        bx, by = find_safe_blank_tap_point(img0)
        adb_tap_norm(adb_path, adb_addr, bx, by, img0.width, img0.height)
        time.sleep(0.6)

    info = detect_map_frontier_stage(
        "",
        adb_path,
        adb_addr,
        maa_dir,
        target_stage=eff_next or cur_up,
        max_swipes=max_swipes,
        cancel_event=cancel_event,
        log_callback=log_callback,
    )
    if not info.get("on_stage_map"):
        _emit(log_callback, f"[MAP][闭环校验失败] 未处于活动地图页面，无法确认 {cur_up} 是否已通关。")
        return {
            "verified": False,
            "skipped": False,
            "on_stage_map": False,
            "stage_code": cur_up,
            "next_stage": eff_next,
            "current_stage_cleared": False,
            "next_stage_unlocked": False,
            "visible_nodes": [],
            "reason": f"未回到活动地图 ({info.get('reason', 'not_on_stage_map')})",
        }

    visible_nodes = list(info.get("visible_nodes") or [])
    nodes_by_code = {str(n.get("code", "")).upper(): n for n in visible_nodes}
    cur_node = nodes_by_code.get(cur_up)
    current_stage_cleared = bool(cur_node and cur_node.get("cleared"))

    next_stage_unlocked = False
    if eff_next and eff_next in nodes_by_code:
        next_stage_unlocked = True
    elif cur_node is not None:
        cur_cx = int(cur_node.get("cx", cur_node.get("x", 0)))
        for code_k, nd in nodes_by_code.items():
            if code_k != cur_up and int(nd.get("cx", nd.get("x", 0))) > cur_cx + 25:
                next_stage_unlocked = True
                break
    elif cur_seq is not None and node_type == "battle":
        for code_k, nd in nodes_by_code.items():
            if nd.get("node_type") == "battle" and _extract_battle_prefix(code_k) == pref_clean:
                seq_k = _extract_stage_seq_num(code_k)
                if seq_k is not None and seq_k > cur_seq:
                    next_stage_unlocked = True
                    break

    base_cleared = bool(current_stage_cleared or next_stage_unlocked)
    if not base_cleared:
        _emit(
            log_callback,
            f"[MAP][闭环校验失败] 地图上 {cur_up} 仍未变蓝标（cleared={current_stage_cleared}），"
            f"且下一关 {eff_next or '-'} 未解锁！严禁推进下一关，强制判定为 battle_failed！",
        )
        return {
            "verified": False,
            "skipped": False,
            "on_stage_map": True,
            "stage_code": cur_up,
            "next_stage": eff_next,
            "current_stage_cleared": current_stage_cleared,
            "next_stage_unlocked": next_stage_unlocked,
            "frontier_stage": info.get("frontier_stage", ""),
            "visible_nodes": visible_nodes,
            "reason": f"地图校验未通过：{cur_up} 未通关且 {eff_next or '下一关'} 未解锁",
        }

    # Hard 3-star verification for combat stages
    if require_three_stars and node_type == "battle":
        if cur_node and cur_node.get("partial_star"):
            _emit(
                log_callback,
                f"[MAP][非满星拦截] 地图节点 {cur_up} 左侧六边形存在暗色缺口（仅 1~2 星，未满 3 星），严禁推进！",
            )
            return {
                "verified": False,
                "skipped": False,
                "on_stage_map": True,
                "stage_code": cur_up,
                "next_stage": eff_next,
                "current_stage_cleared": current_stage_cleared,
                "next_stage_unlocked": next_stage_unlocked,
                "non_3_star": True,
                "three_star_verified": False,
                "verified_stars": 2,
                "frontier_stage": info.get("frontier_stage", ""),
                "visible_nodes": visible_nodes,
                "reason": "battle_failed (non_3_star, stars=2): 地图六边形徽章存在缺口，未达 3 星",
            }

        badge_full = bool(cur_node and cur_node.get("badge_full_3star"))
        need_detail_check = (settlement_stars is None) or (not badge_full)
        if need_detail_check:
            detail_panel: Optional[Dict[str, Any]] = None
            tapped_for_check = False
            if cur_node and int(cur_node.get("cx", 0)) > 0 and int(cur_node.get("cy", 0)) > 0:
                img_now = adb_screencap_image(adb_path, adb_addr)
                if img_now is not None:
                    adb_tap_norm(adb_path, adb_addr, int(cur_node["cx"]), int(cur_node["cy"]), img_now.width, img_now.height)
                    tapped_for_check = True
                    time.sleep(0.9)
                    img_det = adb_screencap_image(adb_path, adb_addr)
                    if img_det is not None:
                        st_det = analyze_screen_state(img_det, maa_dir, use_ocr_fallback=False)
                        if st_det.get("has_stage_start_btn"):
                            detail_panel = inspect_stage_detail_panel(img_det, maa_dir, expected_stage=cur_up, use_ocr=True)
                            bx, by = find_safe_blank_tap_point(img_det)
                            adb_tap_norm(adb_path, adb_addr, bx, by, img_det.width, img_det.height)
                            time.sleep(0.5)
            if (detail_panel is None or not detail_panel.get("title_matches")) and not tapped_for_check:
                probe_entry = enter_stage_node(
                    cur_up,
                    adb_path,
                    adb_addr,
                    maa_dir,
                    click_start=False,
                    max_swipes=min(1, max(0, int(max_swipes))),
                    cancel_event=cancel_event,
                    log_callback=log_callback,
                    skip_if_already_3star=False,
                    uncheck_auto_deploy=False,
                )
                if probe_entry.get("entered") and probe_entry.get("title_verified"):
                    img_det2 = adb_screencap_image(adb_path, adb_addr)
                    if img_det2 is not None:
                        st_det2 = analyze_screen_state(img_det2, maa_dir, use_ocr_fallback=False)
                        if st_det2.get("has_stage_start_btn"):
                            detail_panel = inspect_stage_detail_panel(img_det2, maa_dir, expected_stage=cur_up, use_ocr=True)
                            bx, by = find_safe_blank_tap_point(img_det2)
                            adb_tap_norm(adb_path, adb_addr, bx, by, img_det2.width, img_det2.height)
                            time.sleep(0.5)

            if detail_panel and detail_panel.get("title_matches"):
                sc = int(detail_panel.get("star_count") or 0)
                if not detail_panel.get("has_three_stars"):
                    _emit(
                        log_callback,
                        f"[MAP][非满星兜底拦截] 结算未抓到满星且详情面板仅 {sc} 颗亮星（{cur_up}），严禁放行！",
                    )
                    return {
                        "verified": False,
                        "skipped": False,
                        "on_stage_map": True,
                        "stage_code": cur_up,
                        "next_stage": eff_next,
                        "current_stage_cleared": current_stage_cleared,
                        "next_stage_unlocked": next_stage_unlocked,
                        "non_3_star": True,
                        "three_star_verified": False,
                        "verified_stars": sc,
                        "detail_panel_stars": sc,
                        "frontier_stage": info.get("frontier_stage", ""),
                        "visible_nodes": visible_nodes,
                        "reason": f"battle_failed (non_3_star, stars={sc}): 详情面板右上角仅 {sc}/3 颗亮星",
                    }
                _emit(
                    log_callback,
                    f"[MAP][闭环校验通过] 详情面板兜底核验确认 {cur_up} 右上角 3 颗蓝星全部亮起。",
                )
                return {
                    "verified": True,
                    "skipped": False,
                    "on_stage_map": True,
                    "stage_code": cur_up,
                    "next_stage": eff_next,
                    "current_stage_cleared": True,
                    "next_stage_unlocked": next_stage_unlocked,
                    "non_3_star": False,
                    "three_star_verified": True,
                    "verified_stars": 3,
                    "detail_panel_stars": sc or 3,
                    "frontier_stage": info.get("frontier_stage", ""),
                    "visible_nodes": visible_nodes,
                    "reason": "verified_cleared",
                }

            if not badge_full:
                _emit(
                    log_callback,
                    f"[MAP][闭环校验失败] 结算未确认 3 星且未能打开 {cur_up} 详情面板核对满星（badge_full={badge_full}），禁止放行！",
                )
                return {
                    "verified": False,
                    "skipped": False,
                    "on_stage_map": True,
                    "stage_code": cur_up,
                    "next_stage": eff_next,
                    "current_stage_cleared": current_stage_cleared,
                    "next_stage_unlocked": next_stage_unlocked,
                    "non_3_star": True,
                    "three_star_verified": False,
                    "frontier_stage": info.get("frontier_stage", ""),
                    "visible_nodes": visible_nodes,
                    "reason": f"未能确认 {cur_up} 3 星满星状态（stars=? 且详情面板核验失败）",
                }

    _emit(
        log_callback,
        f"[MAP][闭环校验通过] 确认 {cur_up} 已 3 星通关"
        f"（当前关蓝标={current_stage_cleared}，下一关 {eff_next or '-'} 已解锁={next_stage_unlocked}）。",
    )
    return {
        "verified": True,
        "skipped": False,
        "on_stage_map": True,
        "stage_code": cur_up,
        "next_stage": eff_next,
        "current_stage_cleared": current_stage_cleared,
        "next_stage_unlocked": next_stage_unlocked,
        "non_3_star": False,
        "three_star_verified": True,
        "verified_stars": 3 if (settlement_stars == 3 or (cur_node and cur_node.get("badge_full_3star"))) else settlement_stars,
        "frontier_stage": info.get("frontier_stage", ""),
        "visible_nodes": visible_nodes,
        "reason": "verified_cleared",
    }


def return_to_home_screen(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    max_steps: int = 6,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Reliably navigate back to the Arknights Home screen from activity maps or sub-menus."""
    if not adb_path or not adb_addr:
        return {"ok": False, "steps": 0, "reason": "no_adb"}

    steps = 0
    used_quick_switch = False
    for _ in range(max(1, int(max_steps))):
        if cancel_event and cancel_event.is_set():
            return {"ok": False, "steps": steps, "cancelled": True}

        img = adb_screencap_image(adb_path, adb_addr)
        if img is None:
            time.sleep(0.5)
            continue

        w, h = img.size
        img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
        gray_1280 = np.asarray(img_1280.convert("L"), dtype=np.float32) if np is not None else None

        # 1. If QuickSwitch Home entry is already open, click it immediately.
        has_qs_home, _, qs_home_xy, _ = _match_any_template(
            gray_1280,
            maa_dir,
            [("QuickSwitch/Home.png", (20, 210, 160, 140), 0.72, None)],
        )
        if has_qs_home:
            adb_tap_norm(adb_path, adb_addr, qs_home_xy[0], qs_home_xy[1], w, h)
            steps += 1
            used_quick_switch = True
            _emit(log_callback, "[NAV] 点击顶部快捷导航「首页」返回主界面...")
            time.sleep(1.2)
            # Handle optional confirmation modal (e.g., leaving RIIC base)
            img_cf = adb_screencap_image(adb_path, adb_addr)
            if img_cf is not None:
                cf_boxes = find_text_boxes_via_ocr(img_cf, roi=(420, 320, 520, 240), include_inverted=False, timeout=6)
                cf_text = " ".join(b["compact"] for b in cf_boxes)
                if any(k in cf_text for k in ("离开", "返回", "确认", "首页")):
                    adb_tap_norm(adb_path, adb_addr, 860, 495, img_cf.width, img_cf.height)
                    time.sleep(1.5)
            return {"ok": True, "steps": steps, "method": "quick_switch"}

        # 2. Check if already on Home (has Home UI elements or no Return & no QuickSwitch Menu)
        has_menu, _, menu_xy, _ = _match_any_template(
            gray_1280,
            maa_dir,
            [("QuickSwitch/Menu.png", (215, 5, 120, 70), 0.72, None)],
        )
        has_return, _, ret_xy, _ = _match_any_template(
            gray_1280,
            maa_dir,
            [
                ("ReturnButton/Return.png", (0, 0, 180, 80), 0.72, None),
                ("ReturnButton/Return-White.png", (0, 0, 180, 80), 0.72, None),
            ],
        )
        if not has_menu and not has_return:
            if used_quick_switch or steps > 0:
                return {"ok": True, "steps": steps, "method": "already_home"}
            # Confirm we're not stuck in a skip-story overlay
            st = analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
            if st["has_skip_confirm"]:
                adb_tap_norm(adb_path, adb_addr, st["confirm_xy"][0], st["confirm_xy"][1], w, h)
                steps += 1
                time.sleep(0.8)
                continue
            if st["has_skip_button"]:
                adb_tap_norm(adb_path, adb_addr, st["skip_xy"][0], st["skip_xy"][1], w, h)
                steps += 1
                time.sleep(0.8)
                continue
            return {"ok": True, "steps": steps, "method": "already_home"}

        # 3. Prefer top-left QuickSwitch Menu button (fastest path from any activity/stage map to Home)
        if has_menu:
            adb_tap_norm(adb_path, adb_addr, menu_xy[0], menu_xy[1], w, h)
            steps += 1
            time.sleep(0.8)
            continue

        # 4. Fallback: tap Return arrow
        if has_return:
            adb_tap_norm(adb_path, adb_addr, ret_xy[0], ret_xy[1], w, h)
            steps += 1
            time.sleep(0.9)

    return {"ok": False, "steps": steps, "reason": "max_steps_reached"}


def analyze_reception_clue_screen(
    img: Image.Image,
    maa_dir: Path,
    use_ocr: bool = True,
) -> Dict[str, Any]:
    """Analyze RIIC Reception Clue Exchange screen for Quick Insert (快捷置入) and Unlock Clues (解锁线索)."""
    img_1280_rgb = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    arr_rgb = np.asarray(img_1280_rgb, dtype=np.int16) if np is not None else None
    gray_1280 = np.asarray(img_1280_rgb.convert("L"), dtype=np.float32) if np is not None else None

    has_flag, flag_score, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Reception/ReceptionFlag.png", (350, 0, 200, 100), 0.72, None)],
    )
    has_end_exchange, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Clue/EndOfClueExchange.png", (0, 0, 400, 200), 0.70, None)],
    )
    has_self_new, _, self_new_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Clue/ClueNew.png", (1150, 100, 130, 80), 0.75, None)],
    )
    has_friend_new, _, friend_new_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Clue/ClueNew.png", (1150, 220, 130, 80), 0.75, None)],
    )
    has_send_clues, _, send_clues_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Clue/SendClues.png", (1160, 320, 80, 90), 0.75, (1, 255))],
    )

    # Bottom-right 「快捷置入」 button
    qi_tpl_matched, qi_tpl_score, qi_tpl_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Clue/InfrastClueQuickInsert.png", (1058, 572, 160, 148), 0.72, None)],
    )
    qi_ocr_matched = False
    qi_ocr_xy: Optional[Tuple[int, int]] = None
    badge_count: Optional[int] = None

    # Bottom-center 「解锁线索」 button
    unlock_tpl_matched, unlock_tpl_score, unlock_tpl_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("InfrastPic/Clue/UnlockClues.png", (200, 600, 700, 120), 0.78, None)],
    )
    unlock_ocr_matched = False
    unlock_ocr_xy: Optional[Tuple[int, int]] = None
    is_seven_of_seven = False
    is_partial_clues = False
    exchange_in_progress = False

    if use_ocr:
        qi_boxes = find_text_boxes_via_ocr(img, roi=(1020, 565, 260, 155), include_inverted=True, timeout=8)
        for b in qi_boxes:
            txt = b.get("compact", "")
            if any(k in txt for k in ("快捷置入", "快速置入", "快捷", "置入")):
                qi_ocr_matched = True
                qi_ocr_xy = (int(b["cx"]), int(b["cy"]))
            # Red badge sits in upper-right corner of the Quick Insert button (x >= 1220, y <= 655)
            if int(b.get("cx", 0)) >= 1215 and int(b.get("cy", 0)) <= 660:
                m_num = re.search(r"(\d+)", txt)
                if m_num:
                    val = int(m_num.group(1))
                    if 0 <= val <= 99:
                        badge_count = val if badge_count is None else max(badge_count, val)

        unlock_boxes = find_text_boxes_via_ocr(img, roi=(480, 600, 420, 110), include_inverted=True, timeout=8)
        unlock_joined = " ".join(b.get("compact", "") for b in unlock_boxes)
        if any(k in unlock_joined for k in ("交流中", "进行中", "剩余")):
            exchange_in_progress = True
        m_ratio = re.search(r"([0-7])\s*/\s*7", unlock_joined)
        if m_ratio:
            if m_ratio.group(1) == "7":
                is_seven_of_seven = True
            else:
                is_partial_clues = True
        for b in unlock_boxes:
            txt = b.get("compact", "")
            if any(k in txt for k in ("解锁线索", "解锁", "7/7")):
                unlock_ocr_matched = True
                if unlock_ocr_xy is None:
                    unlock_ocr_xy = (int(b["cx"]), int(b["cy"]))

    has_red_badge = False
    qi_active_orange = False
    unlock_highlighted = False
    unlock_mean_luma = 0.0
    if arr_rgb is not None:
        # Check red dot/number badge near [1220..1280, 595..655]
        badge_crop = arr_rgb[595:655, 1220:1280]
        if badge_crop.size > 0:
            red_mask = (badge_crop[:, :, 0] >= 170) & (badge_crop[:, :, 1] <= 95) & (badge_crop[:, :, 2] <= 95)
            has_red_badge = int(red_mask.sum()) >= 12

        # Check warm orange active color around Quick Insert button
        qi_cx = qi_tpl_xy[0] if qi_tpl_matched else (qi_ocr_xy[0] if qi_ocr_xy else 1132)
        qi_cy = qi_tpl_xy[1] if qi_tpl_matched else (qi_ocr_xy[1] if qi_ocr_xy else 655)
        qx0, qx1 = max(0, qi_cx - 45), min(1280, qi_cx + 65)
        qy0, qy1 = max(0, qi_cy - 30), min(720, qi_cy + 30)
        qi_crop = arr_rgb[qy0:qy1, qx0:qx1]
        if qi_crop.size > 0:
            orange_mask = (qi_crop[:, :, 0] >= 150) & (qi_crop[:, :, 1] >= 45) & (qi_crop[:, :, 1] <= 155) & (qi_crop[:, :, 2] <= 65)
            qi_active_orange = int(orange_mask.sum()) >= 120

        # Check bright white highlight on Unlock Clues button (clickable 7/7 button is bright white vs dark grey)
        ux = unlock_tpl_xy[0] if unlock_tpl_matched else (unlock_ocr_xy[0] if unlock_ocr_xy else 682)
        uy = unlock_tpl_xy[1] if unlock_tpl_matched else (unlock_ocr_xy[1] if unlock_ocr_xy else 649)
        ux0, ux1 = max(460, ux - 130), min(880, ux + 130)
        uy0, uy1 = max(605, uy - 28), min(700, uy + 28)
        u_crop = arr_rgb[uy0:uy1, ux0:ux1]
        if u_crop.size > 0:
            luma = 0.299 * u_crop[:, :, 0] + 0.587 * u_crop[:, :, 1] + 0.114 * u_crop[:, :, 2]
            unlock_mean_luma = float(luma.mean())
            bright_ratio = float(((u_crop[:, :, 0] >= 205) & (u_crop[:, :, 1] >= 205) & (u_crop[:, :, 2] >= 205)).mean())
            unlock_highlighted = (unlock_mean_luma >= 180.0) or (bright_ratio >= 0.42)

    quick_insert_detected = bool(qi_tpl_matched or qi_ocr_matched)
    if badge_count is not None and badge_count <= 0 and not has_red_badge:
        quick_insert_available = False
    else:
        quick_insert_available = bool(
            quick_insert_detected
            and (has_red_badge or (badge_count is not None and badge_count >= 1) or qi_active_orange)
        )
    quick_insert_xy = qi_tpl_xy if qi_tpl_matched else (qi_ocr_xy if qi_ocr_xy else (1145, 655))

    unlock_clues_ready = bool(
        (not exchange_in_progress)
        and (not is_partial_clues)
        and (
            is_seven_of_seven
            or (unlock_tpl_score >= 0.88 and unlock_highlighted)
            or (unlock_ocr_matched and unlock_highlighted)
        )
    )
    unlock_clues_xy = unlock_tpl_xy if unlock_tpl_matched else (unlock_ocr_xy if unlock_ocr_xy else (682, 650))

    on_clue_board = bool(has_flag or quick_insert_detected or unlock_tpl_matched or unlock_ocr_matched or has_send_clues)

    return {
        "on_clue_board": on_clue_board,
        "flag_score": round(flag_score, 3),
        "has_end_of_exchange": bool(has_end_exchange),
        "has_self_clue_new": bool(has_self_new),
        "self_clue_new_xy": self_new_xy if has_self_new else (1210, 145),
        "has_friend_clue_new": bool(has_friend_new),
        "friend_clue_new_xy": friend_new_xy if has_friend_new else (1210, 260),
        "has_send_clues": bool(has_send_clues),
        "send_clues_xy": send_clues_xy if has_send_clues else (1198, 370),
        "quick_insert_detected": quick_insert_detected,
        "quick_insert_available": quick_insert_available,
        "quick_insert_xy": quick_insert_xy,
        "quick_insert_score": round(qi_tpl_score, 3),
        "quick_insert_red_badge": bool(has_red_badge),
        "quick_insert_badge_count": badge_count,
        "unlock_clues_ready": unlock_clues_ready,
        "unlock_clues_xy": unlock_clues_xy,
        "unlock_clues_score": round(unlock_tpl_score, 3),
        "unlock_highlighted": bool(unlock_highlighted),
        "unlock_mean_luma": round(unlock_mean_luma, 1),
        "is_seven_of_seven": bool(is_seven_of_seven),
        "is_partial_clues": bool(is_partial_clues),
        "exchange_in_progress": bool(exchange_in_progress),
    }


def ensure_reception_clues_processed(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    collect_clues: bool = True,
    quick_insert: bool = True,
    unlock_clues: bool = True,
    send_duplicates: bool = False,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Visual/OCR fallback for RIIC Reception: collect clues, click 「快捷置入」, and click 「7/7 解锁线索」."""
    summary: Dict[str, Any] = {
        "ok": False,
        "entered_clue_board": False,
        "collected_friend_clues": False,
        "collected_self_clues": False,
        "quick_insert_clicked": 0,
        "unlock_clues_clicked": False,
        "sent_duplicates": False,
        "reason": "",
    }
    if not adb_path or not adb_addr:
        summary["reason"] = "no_adb"
        return summary
    if cancel_event and cancel_event.is_set():
        summary["reason"] = "cancelled"
        return summary

    def _cap_and_analyze(use_ocr: bool = True) -> Tuple[Optional[Image.Image], Optional[Dict[str, Any]]]:
        im = adb_screencap_image(adb_path, adb_addr)
        if im is None:
            return None, None
        return im, analyze_reception_clue_screen(im, maa_dir, use_ocr=use_ocr)

    def _enter_reception_clue_board() -> Tuple[Optional[Image.Image], Optional[Dict[str, Any]]]:
        img, st = _cap_and_analyze(use_ocr=True)
        if img is None or st is None:
            return None, None
        if st["has_end_of_exchange"]:
            adb_tap_norm(adb_path, adb_addr, 80, 40, img.width, img.height)
            time.sleep(1.5)
            img, st = _cap_and_analyze(use_ocr=True)
            if img is None or st is None:
                return None, None
        if st["on_clue_board"]:
            return img, st

        # Check if already inside Reception room (has bottom-left interactive icon)
        gray = np.asarray(img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
        in_reception_room, _, _, _ = _match_any_template(
            gray,
            maa_dir,
            [("InfrastPic/MessageBoard/InfrastReceptionInteractive.png", (400, 610, 100, 110), 0.72, None)],
        )
        if not in_reception_room:
            # Try locating Reception facility on RIIC Base Overview (up to 2 swipes)
            entered_room = False
            cur = img
            for attempt in range(3):
                if cancel_event and cancel_event.is_set():
                    return None, None
                c_gray = np.asarray(cur.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
                hit_rec, _, rec_xy, _ = _match_any_template(
                    c_gray,
                    maa_dir,
                    [
                        ("InfrastPic/Facilities/ReceptionMini.png", (627, 160, 653, 404), 0.78, (1, 255)),
                        ("InfrastPic/Facilities/Reception.png", (378, 94, 900, 586), 0.78, (1, 255)),
                    ],
                )
                if not hit_rec:
                    boxes = find_text_boxes_via_ocr(cur, roi=(550, 120, 720, 500), include_inverted=False, timeout=6)
                    for b in boxes:
                        if "会客室" in b.get("compact", ""):
                            hit_rec = True
                            rec_xy = (int(b["cx"]), int(b["cy"]))
                            break
                if hit_rec:
                    _emit(log_callback, f"[INFRA:Reception] 进入会客室执行线索「快捷置入」与「解锁线索」核查 ({rec_xy[0]}, {rec_xy[1]})...")
                    adb_tap_norm(adb_path, adb_addr, rec_xy[0], rec_xy[1], cur.width, cur.height)
                    time.sleep(2.0)
                    entered_room = True
                    break
                if attempt == 0:
                    adb_swipe_norm(adb_path, adb_addr, 240, 340, 1040, 340, duration_ms=350, width=cur.width, height=cur.height)
                elif attempt == 1:
                    adb_swipe_norm(adb_path, adb_addr, 1040, 340, 240, 340, duration_ms=350, width=cur.width, height=cur.height)
                time.sleep(0.7)
                nxt = adb_screencap_image(adb_path, adb_addr)
                if nxt is None:
                    break
                cur = nxt
            if not entered_room:
                return cur, st

        # Inside Reception room -> click bottom-left clue panel to open Clue Board
        cur2 = adb_screencap_image(adb_path, adb_addr)
        if cur2 is None:
            return None, None
        adb_tap_norm(adb_path, adb_addr, 120, 670, cur2.width, cur2.height)
        time.sleep(2.0)
        img_board, st_board = _cap_and_analyze(use_ocr=True)
        if img_board is not None and st_board is not None and st_board["has_end_of_exchange"]:
            adb_tap_norm(adb_path, adb_addr, 80, 40, img_board.width, img_board.height)
            time.sleep(1.5)
            img_board, st_board = _cap_and_analyze(use_ocr=True)
        return img_board, st_board

    img, st = _enter_reception_clue_board()
    if img is None or st is None or not st.get("on_clue_board"):
        summary["reason"] = "clue_board_not_reached"
        return summary

    summary["entered_clue_board"] = True
    did_enter_from_outside = True

    # 1. Preserve receiving friend & self clues if still marked NEW
    if collect_clues and st.get("has_friend_clue_new"):
        fx, fy = st["friend_clue_new_xy"]
        adb_tap_norm(adb_path, adb_addr, max(10, fx - 45), min(710, fy + 15), img.width, img.height)
        time.sleep(1.2)
        im_f = adb_screencap_image(adb_path, adb_addr)
        if im_f is not None:
            g_f = np.asarray(im_f.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
            hit_g2, _, g2_xy, _ = _match_any_template(
                g_f, maa_dir, [("InfrastPic/Clue/GetClue2.png", (800, 600, 480, 120), 0.72, None)]
            )
            if hit_g2:
                adb_tap_norm(adb_path, adb_addr, g2_xy[0], g2_xy[1], im_f.width, im_f.height)
                summary["collected_friend_clues"] = True
                _emit(log_callback, "[INFRA:Reception] 已自动收取好友赠送的新线索。")
                time.sleep(1.5)
            adb_tap_norm(adb_path, adb_addr, 350, 108, im_f.width, im_f.height)
            time.sleep(1.2)
        img, st = _cap_and_analyze(use_ocr=True)
        if img is None or st is None:
            summary["ok"] = True
            return summary

    if collect_clues and st.get("has_self_clue_new"):
        sx, sy = st["self_clue_new_xy"]
        adb_tap_norm(adb_path, adb_addr, max(10, sx - 45), min(710, sy + 15), img.width, img.height)
        time.sleep(1.2)
        im_s = adb_screencap_image(adb_path, adb_addr)
        if im_s is not None:
            g_s = np.asarray(im_s.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
            hit_g1, _, g1_xy, _ = _match_any_template(
                g_s, maa_dir, [("InfrastPic/Clue/GetClue1.png", (680, 540, 250, 80), 0.72, None)]
            )
            if hit_g1:
                adb_tap_norm(adb_path, adb_addr, g1_xy[0], g1_xy[1], im_s.width, im_s.height)
                summary["collected_self_clues"] = True
                _emit(log_callback, "[INFRA:Reception] 已自动收取自产新线索。")
                time.sleep(1.2)
            # Close self-clue drawer if still open
            im_sc = adb_screencap_image(adb_path, adb_addr)
            if im_sc is not None:
                g_sc = np.asarray(im_sc.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
                hit_cl, _, cl_xy, _ = _match_any_template(
                    g_sc, maa_dir, [("InfrastPic/Clue/CloseCluePage.png", (930, 50, 120, 120), 0.72, None)]
                )
                if hit_cl:
                    adb_tap_norm(adb_path, adb_addr, cl_xy[0], cl_xy[1], im_sc.width, im_sc.height)
                    time.sleep(0.8)
        img, st = _cap_and_analyze(use_ocr=True)
        if img is None or st is None:
            summary["ok"] = True
            return summary

    # 2. Detect & click bottom-right 「快捷置入」 (especially when red badge / available clues exist)
    if quick_insert and st.get("quick_insert_available"):
        qx, qy = st["quick_insert_xy"]
        badge_info = st.get("quick_insert_badge_count")
        _emit(
            log_callback,
            f"[INFRA:Reception] 检测到右下角「快捷置入」按钮（红点={st.get('quick_insert_red_badge')}, 数量={badge_info}），自动点击置入已有线索...",
        )
        adb_tap_norm(adb_path, adb_addr, qx, qy, img.width, img.height)
        summary["quick_insert_clicked"] += 1
        time.sleep(1.6)
        img, st = _cap_and_analyze(use_ocr=True)
        if img is None or st is None:
            summary["ok"] = True
            return summary

    # 3. Immediately check bottom-center 「解锁线索」 (only when 7/7 & highlighted clickable)
    if unlock_clues and st.get("unlock_clues_ready"):
        ux, uy = st["unlock_clues_xy"]
        _emit(log_callback, "[INFRA:Reception] 检测到「7/7 解锁线索」高亮可点击，自动点击开启线索交流！")
        adb_tap_norm(adb_path, adb_addr, ux, uy, img.width, img.height)
        summary["unlock_clues_clicked"] = True
        time.sleep(2.5)
        img_post, st_post = _cap_and_analyze(use_ocr=True)
        if img_post is not None and st_post is not None and st_post.get("unlock_clues_ready"):
            adb_tap_norm(adb_path, adb_addr, st_post["unlock_clues_xy"][0], st_post["unlock_clues_xy"][1], img_post.width, img_post.height)
            time.sleep(2.0)
            img, st = _cap_and_analyze(use_ocr=False)
        else:
            img, st = img_post, st_post

    # 4. Optional quick-send duplicate clues (preserves sending redundant clues when requested or after new collection)
    if (send_duplicates or summary["collected_friend_clues"] or summary["collected_self_clues"]) and img is not None and st is not None and st.get("has_send_clues"):
        scx, scy = st["send_clues_xy"]
        adb_tap_norm(adb_path, adb_addr, scx, scy, img.width, img.height)
        time.sleep(1.2)
        im_sd = adb_screencap_image(adb_path, adb_addr)
        if im_sd is not None:
            g_sd = np.asarray(im_sd.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
            hit_qsd, _, qsd_xy, _ = _match_any_template(
                g_sd, maa_dir, [("InfrastPic/Clue/InfrastClueQuickSendDuplicates.png", (34, 559, 154, 161), 0.75, None)]
            )
            if hit_qsd:
                adb_tap_norm(adb_path, adb_addr, qsd_xy[0], qsd_xy[1], im_sd.width, im_sd.height)
                summary["sent_duplicates"] = True
                _emit(log_callback, "[INFRA:Reception] 已自动点击「传递重复线索」。")
                time.sleep(1.5)
            im_cl = adb_screencap_image(adb_path, adb_addr)
            if im_cl is not None:
                g_cl = np.asarray(im_cl.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
                hit_x, _, x_xy, _ = _match_any_template(
                    g_cl, maa_dir, [("InfrastPic/Clue/CloseSendClue.png", (1225, 16, 41, 41), 0.72, None)]
                )
                if hit_x:
                    adb_tap_norm(adb_path, adb_addr, x_xy[0], x_xy[1], im_cl.width, im_cl.height)
                    time.sleep(1.0)

    # 5. Clean exit back to Reception -> Base Overview
    if did_enter_from_outside:
        im_ex = adb_screencap_image(adb_path, adb_addr)
        if im_ex is not None:
            st_ex = analyze_reception_clue_screen(im_ex, maa_dir, use_ocr=False)
            if st_ex.get("on_clue_board"):
                adb_tap_norm(adb_path, adb_addr, 350, 108, im_ex.width, im_ex.height)
                time.sleep(1.2)
            im_ex2 = adb_screencap_image(adb_path, adb_addr)
            if im_ex2 is not None:
                g_ex2 = np.asarray(im_ex2.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"), dtype=np.float32) if np is not None else None
                has_ret, _, ret_xy, _ = _match_any_template(
                    g_ex2,
                    maa_dir,
                    [("ReturnButton/Return.png", (0, 0, 180, 80), 0.72, None)],
                )
                if has_ret:
                    adb_tap_norm(adb_path, adb_addr, ret_xy[0], ret_xy[1], im_ex2.width, im_ex2.height)
                    time.sleep(1.0)

    summary["ok"] = True
    return summary


# ======================================================================
# Universal Full-Screen Guide / Mechanism Tutorial Popup Dismisser
# ======================================================================

# Normalized 1280x720 ROI covering top-left "× 关闭" button across Roguelike & Reclamation
# Covers x in [0.02, 0.22] (~25..281), y in [0.08, 0.30] (~58..216)
_GUIDE_CLOSE_ROI_1280: Tuple[int, int, int, int] = (25, 58, 256, 160)


def _compute_image_diff_mse(img_a: Image.Image, img_b: Image.Image) -> float:
    """Compute lightweight grayscale MSE between two screenshots (downscaled to 320x180)."""
    try:
        a_im = img_a.convert("L").resize((320, 180), Image.Resampling.BILINEAR)
        b_im = img_b.convert("L").resize((320, 180), Image.Resampling.BILINEAR)
        if np is not None:
            a = np.asarray(a_im, dtype=np.float32)
            b = np.asarray(b_im, dtype=np.float32)
            diff = a - b
            return float(np.mean(diff * diff))
        from PIL import ImageChops

        diff_im = ImageChops.difference(a_im, b_im)
        hist = diff_im.histogram()
        total = sum(hist) or 1
        return float(sum((i * i) * cnt for i, cnt in enumerate(hist)) / float(total))
    except Exception:
        return 9999.0


def _detect_guide_carousel_visual_features(img_1280: Image.Image) -> Dict[str, Any]:
    """Inspect 1280x720 screenshot for Arknights full-screen mechanism guide carousel cues."""
    out: Dict[str, Any] = {
        "dark_backdrop": False,
        "has_pagination": False,
        "pagination_segments": 0,
        "has_right_arrow": False,
        "close_box_present": False,
        "estimated_close_xy": (156, 108),
    }
    if np is None:
        return out
    try:
        gray = np.asarray(img_1280.convert("L"), dtype=np.float32)
        if gray.shape[0] < 700 or gray.shape[1] < 1200:
            return out

        top_mean = float(gray[0:22, :].mean())
        left_mean = float(gray[180:680, 0:72].mean())
        # Full-screen guide popups dim the entire viewport behind a dark vignette
        dark_backdrop = (top_mean < 38.0) and (left_mean < 58.0)

        # Top-center pagination horizontal bars (y in [20, 56], x in [340, 940]):
        # Require a dark empty margin above the thin pill bars (rows 0..6, y in [20, 26])
        # so top-bar HUD text (e.g. Sarkaz floor title / level bar) is never miscounted.
        pag_strip = gray[20:56, 340:940]
        pag_top_band = pag_strip[0:7, :]
        pag_mid_band = pag_strip[7:25, :]
        pag_top_quiet = (float(pag_top_band.max()) < 150.0) and (float(pag_top_band.mean()) < 45.0)
        pag_thresh = max(75.0, top_mean + 45.0)
        col_max = pag_mid_band.max(axis=0)
        segs = 0
        in_seg = False
        cur_len = 0
        for v in col_max:
            if float(v) >= pag_thresh:
                cur_len += 1
                if not in_seg:
                    in_seg = True
            else:
                if in_seg and 16 <= cur_len <= 100:
                    segs += 1
                in_seg = False
                cur_len = 0
        if in_seg and 16 <= cur_len <= 100:
            segs += 1
        has_pagination = bool(dark_backdrop and pag_top_quiet and segs >= 2)

        # Right-edge '>' next-page arrow (x in [1140, 1245], y in [300, 425])
        arrow_patch = gray[300:425, 1140:1245]
        arrow_bright = int((arrow_patch >= 190.0).sum())
        arrow_bg = float(arrow_patch.mean())
        has_right_arrow = bool(dark_backdrop and 25 <= arrow_bright <= 1800 and arrow_bg < 72.0)

        # Top-left "× 关闭" pill bright text/icon pixels strictly inside x in [65, 235], y in [78, 156]
        # (excludes lower UI labels such as Sarkaz "思维负荷" at y ~ 171)
        btn_patch = gray[78:156, 65:235]
        bright_mask = btn_patch >= 180.0
        bright_cnt = int(bright_mask.sum())
        close_box_present = bool(dark_backdrop and 25 <= bright_cnt <= 2200 and float(np.median(btn_patch)) < 92.0)
        est_xy = (156, 108)
        if close_box_present:
            ys, xs = np.where(bright_mask)
            if len(xs) > 0:
                est_xy = (int(round(65 + float(xs.mean()))), int(round(78 + float(ys.mean()))))

        out.update(
            {
                "dark_backdrop": dark_backdrop,
                "has_pagination": has_pagination,
                "pagination_segments": segs,
                "has_right_arrow": has_right_arrow,
                "close_box_present": close_box_present,
                "estimated_close_xy": est_xy,
            }
        )
    except Exception:
        pass
    return out


def detect_guide_popup(
    img: Image.Image,
    maa_dir: Optional[Path] = None,
    use_ocr: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Detect full-screen mechanism guide/tutorial carousel popup and locate top-left '× 关闭' button."""
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    vis = _detect_guide_carousel_visual_features(img_1280)

    ocr_close_xy: Optional[Tuple[int, int]] = None
    ocr_text = ""
    ocr_weak_x_xy: Optional[Tuple[int, int]] = None
    if use_ocr:
        try:
            boxes = find_text_boxes_via_ocr(
                img,
                roi=_GUIDE_CLOSE_ROI_1280,
                include_inverted=True,
                timeout=6,
                cancel_event=cancel_event,
            )
        except Exception:
            boxes = []
        for b in boxes:
            comp = str(b.get("compact", "") or "").strip()
            if not comp:
                continue
            cx, cy = int(b.get("cx", 0)), int(b.get("cy", 0))
            if not (25 <= cx <= 285 and 55 <= cy <= 225):
                continue
            up = comp.upper()
            if "关闭" in comp or up in ("CLOSE", "XCLOSE", "×CLOSE"):
                ocr_close_xy = (cx, cy)
                ocr_text = comp
                if b.get("level", "line") == "line" or "×" in comp or "关闭" == comp:
                    break
            elif comp in ("×", "X", "x", "✕", "✖") and ocr_weak_x_xy is None:
                ocr_weak_x_xy = (cx, cy)
                if not ocr_text:
                    ocr_text = comp

    # Strict visual-only fallback: require ALL strong carousel cues simultaneously
    # (dark vignette + top-left close pill + top horizontal pagination strip + right '>' arrow)
    has_visual_carousel = bool(
        vis["dark_backdrop"]
        and vis["close_box_present"]
        and vis["has_pagination"]
        and vis["has_right_arrow"]
    )

    if ocr_close_xy is not None:
        return {
            "detected": True,
            "close_xy": ocr_close_xy,
            "method": "ocr+visual" if has_visual_carousel else "ocr",
            "has_pagination": vis["has_pagination"],
            "has_right_arrow": vis["has_right_arrow"],
            "ocr_text": ocr_text,
        }

    if ocr_weak_x_xy is not None and vis["dark_backdrop"] and (vis["has_pagination"] or vis["has_right_arrow"]):
        return {
            "detected": True,
            "close_xy": ocr_weak_x_xy,
            "method": "ocr_x+visual",
            "has_pagination": vis["has_pagination"],
            "has_right_arrow": vis["has_right_arrow"],
            "ocr_text": ocr_text,
        }

    if has_visual_carousel:
        return {
            "detected": True,
            "close_xy": vis["estimated_close_xy"],
            "method": "visual",
            "has_pagination": vis["has_pagination"],
            "has_right_arrow": vis["has_right_arrow"],
            "ocr_text": ocr_text,
        }

    return {
        "detected": False,
        "close_xy": None,
        "method": "",
        "has_pagination": vis["has_pagination"],
        "has_right_arrow": vis["has_right_arrow"],
        "ocr_text": ocr_text,
    }


def dismiss_guide_popups(
    adb_path: str,
    adb_addr: str,
    maa_dir: Optional[Path] = None,
    max_popups: int = 8,
    min_change_mse: float = 12.0,
    max_no_change_clicks: int = 2,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Scan and close one or more stacked full-screen mechanism guide popups via top-left '× 关闭'."""
    summary: Dict[str, Any] = {
        "ok": True,
        "dismissed_count": 0,
        "clicks": [],
        "cancelled": False,
        "aborted_no_change": False,
        "no_change_streak": 0,
    }
    if not adb_path or not adb_addr:
        summary["ok"] = False
        return summary

    prev_img: Optional[Image.Image] = None
    no_change_streak = 0
    max_stale = max(1, int(max_no_change_clicks))

    for _ in range(max(1, int(max_popups))):
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary

        img = adb_screencap_image(adb_path, adb_addr, timeout=5.0, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary
        if img is None:
            if summary["dismissed_count"] == 0:
                summary["ok"] = False
            return summary

        if prev_img is not None:
            mse = _compute_image_diff_mse(prev_img, img)
            if mse < float(min_change_mse):
                no_change_streak += 1
                summary["no_change_streak"] = no_change_streak
                if no_change_streak >= max_stale:
                    # Roll back consecutive no-effect taps so callers don't mistake blind clicks for real dismissals
                    summary["dismissed_count"] = max(0, summary["dismissed_count"] - no_change_streak)
                    summary["aborted_no_change"] = True
                    _emit(
                        log_callback,
                        f"[GUIDE] 连续 {no_change_streak} 次点击后画面无明显变化（MSE={mse:.2f} < {float(min_change_mse):.1f}），"
                        f"判定为非有效弹窗并中止循环。",
                    )
                    return summary
            else:
                no_change_streak = 0
                summary["no_change_streak"] = 0

        det = detect_guide_popup(img, maa_dir=maa_dir, use_ocr=True, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary
        if not det.get("detected") or not det.get("close_xy"):
            return summary

        cx, cy = det["close_xy"]
        _emit(
            log_callback,
            f"[GUIDE] 检测到全屏机制教程弹窗（方式={det.get('method')}，文本='{det.get('ocr_text') or '视觉特征'}'），"
            f"自动点击左上角「× 关闭」({cx}, {cy})...",
        )
        adb_tap_norm(adb_path, adb_addr, cx, cy, img.width, img.height, cancel_event=cancel_event)
        summary["dismissed_count"] += 1
        summary["clicks"].append((cx, cy))
        prev_img = img

        if wait_interruptible(0.85, cancel_event=cancel_event, step=0.25):
            summary["cancelled"] = True
            return summary

    return summary


# ======================================================================
# Sarkaz / Roguelike Subpanel Escape (思绪-构想 / 思维负荷 -> ↓ 收起 / <)
# ======================================================================

_SARKAZ_BOTTOM_BAR_ROI_1280: Tuple[int, int, int, int] = (540, 625, 320, 95)
_SARKAZ_PANEL_BODY_ROI_1280: Tuple[int, int, int, int] = (0, 80, 1280, 480)
_SARKAZ_COLLAPSE_FALLBACK_XY: Tuple[int, int] = (624, 678)
_SARKAZ_RETURN_FALLBACK_XY: Tuple[int, int] = (45, 42)


def detect_roguelike_subpanel_state(
    img: Image.Image,
    maa_dir: Optional[Path] = None,
    use_ocr: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Detect whether screen is stuck inside a Roguelike subpanel (e.g. Sarkaz 思绪-构想 / 思维负荷)."""
    state: Dict[str, Any] = {
        "in_subpanel": False,
        "subpanel_type": "",
        "collapse_xy": None,
        "return_xy": None,
        "markers": [],
    }
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    gray_1280 = np.asarray(img_1280.convert("L"), dtype=np.float32) if np is not None else None

    if maa_dir is not None and gray_1280 is not None:
        has_ret, _, ret_xy, _ = _match_any_template(
            gray_1280,
            Path(maa_dir),
            [
                ("ReturnButton/Return.png", (0, 0, 180, 90), 0.70, None),
                ("ReturnButton/Return-White.png", (0, 0, 180, 90), 0.70, None),
            ],
        )
        if has_ret:
            state["return_xy"] = ret_xy

    collapse_xy: Optional[Tuple[int, int]] = None
    markers: List[str] = []
    has_thought_cue = False
    has_burden_cue = False

    if use_ocr:
        try:
            bottom_boxes = find_text_boxes_via_ocr(
                img,
                roi=_SARKAZ_BOTTOM_BAR_ROI_1280,
                include_inverted=True,
                timeout=6,
                cancel_event=cancel_event,
            )
        except Exception:
            bottom_boxes = []

        for b in bottom_boxes:
            comp = str(b.get("compact", "") or "").strip()
            if not comp:
                continue
            cx, cy = int(b.get("cx", 0)), int(b.get("cy", 0))
            if "收起" in comp and 520 <= cx <= 740 and 615 <= cy <= 720:
                collapse_xy = (cx, min(max(cy, 655), 695))
                if "收起" not in markers:
                    markers.append("收起")
            if "思维负荷" in comp or "维负荷" in comp:
                has_burden_cue = True
                if "思维负荷" not in markers:
                    markers.append("思维负荷")

        # Only scan panel body if bottom bar didn't already confirm "收起", or to enrich markers
        if collapse_xy is None:
            try:
                body_boxes = find_text_boxes_via_ocr(
                    img,
                    roi=_SARKAZ_PANEL_BODY_ROI_1280,
                    include_inverted=False,
                    timeout=6,
                    cancel_event=cancel_event,
                )
            except Exception:
                body_boxes = []

            for b in body_boxes:
                comp = str(b.get("compact", "") or "").strip()
                if not comp:
                    continue
                cx, cy = int(b.get("cx", 0)), int(b.get("cy", 0))
                if "收起" in comp and 520 <= cx <= 740 and 615 <= cy <= 720 and collapse_xy is None:
                    collapse_xy = (cx, min(max(cy, 655), 695))
                    if "收起" not in markers:
                        markers.append("收起")
                if any(k in comp for k in ("思绪", "构想", "印象重建", "灵感", "遗愿")):
                    has_thought_cue = True
                    if comp not in markers:
                        markers.append(comp)
                if any(k in comp for k in ("思维负荷", "维负荷", "负荷干员", "思维清晰", "思维混乱", "思维迟缓")):
                    has_burden_cue = True
                    if comp not in markers:
                        markers.append(comp)

    if collapse_xy is not None or (has_thought_cue and has_burden_cue):
        state["in_subpanel"] = True
        state["subpanel_type"] = "sarkaz_thought_panel"
        state["collapse_xy"] = collapse_xy or _SARKAZ_COLLAPSE_FALLBACK_XY
        if state["return_xy"] is None:
            state["return_xy"] = _SARKAZ_RETURN_FALLBACK_XY
        state["markers"] = markers[:12]

    return state


def escape_roguelike_subpanel(
    adb_path: str,
    adb_addr: str,
    maa_dir: Optional[Path] = None,
    max_steps: int = 3,
    min_change_mse: float = 12.0,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Escape Sarkaz '思绪-构想 / 思维负荷' subpanel back to main map via '↓ 收起' (fallback '<') and clear popups."""
    summary: Dict[str, Any] = {
        "ok": True,
        "escaped": False,
        "actions": [],
        "clicks": [],
        "guide_popups_dismissed": 0,
        "cancelled": False,
    }
    if not adb_path or not adb_addr:
        summary["ok"] = False
        return summary

    collapse_failed = False

    for _ in range(max(1, int(max_steps))):
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary

        img = adb_screencap_image(adb_path, adb_addr, timeout=5.0, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary
        if img is None:
            if not summary["actions"]:
                summary["ok"] = False
            return summary

        st = detect_roguelike_subpanel_state(img, maa_dir=maa_dir, use_ocr=True, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary
        if not st.get("in_subpanel"):
            break

        if st.get("collapse_xy") and not collapse_failed:
            tx, ty = st["collapse_xy"]
            action_name = "tap_collapse_thought_panel"
            _emit(
                log_callback,
                f"[ROGUELIKE:ESCAPE] 检测到画面滞留在萨卡兹「思绪-构想 / 思维负荷」子界面"
                f"（命中={st.get('markers')}），点击底部「↓ 收起」({tx}, {ty}) 退回主地图...",
            )
        elif st.get("return_xy"):
            tx, ty = st["return_xy"]
            action_name = "tap_subpanel_return"
            _emit(
                log_callback,
                f"[ROGUELIKE:ESCAPE] 底部「↓ 收起」未退出子界面，尝试点击左上角「<」返回 ({tx}, {ty})...",
            )
        else:
            break

        adb_tap_norm(adb_path, adb_addr, tx, ty, img.width, img.height, cancel_event=cancel_event)
        summary["actions"].append(action_name)
        summary["clicks"].append((tx, ty))

        if wait_interruptible(0.85, cancel_event=cancel_event, step=0.25):
            summary["cancelled"] = True
            return summary

        img_after = adb_screencap_image(adb_path, adb_addr, timeout=5.0, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            return summary
        if img_after is None:
            summary["escaped"] = True
            break

        mse = _compute_image_diff_mse(img, img_after)
        if mse >= float(min_change_mse):
            summary["escaped"] = True
            st_after = detect_roguelike_subpanel_state(img_after, maa_dir=maa_dir, use_ocr=True, cancel_event=cancel_event)
            if not st_after.get("in_subpanel"):
                break
        else:
            if action_name == "tap_collapse_thought_panel":
                collapse_failed = True
            else:
                # Both collapse and return produced no visible screen change; avoid blind looping
                break

    # Clean up any newly popped tutorial guide overlay after leaving the subpanel
    if summary["escaped"] and not (cancel_event and cancel_event.is_set()):
        post_guide = dismiss_guide_popups(
            adb_path,
            adb_addr,
            maa_dir=maa_dir,
            cancel_event=cancel_event,
            log_callback=log_callback,
        )
        summary["guide_popups_dismissed"] = int(post_guide.get("dismissed_count", 0) or 0)
        if post_guide.get("cancelled"):
            summary["cancelled"] = True

    return summary


# ======================================================================
# Reclamation Algorithm (生息演算 · 《重启锚点》) Visual Navigation & Cleanup
# ======================================================================


def _normalize_ra_stage_token(text: str) -> str:
    s = re.sub(r"[\s_]+", "", str(text or "").upper())
    s = s.replace("—", "-").replace("–", "-")
    return s


def detect_reclamation_screen_state(
    img: Image.Image,
    maa_dir: Path,
    target_stage: str = "RA-1",
    use_ocr: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Inspect current screen to determine Reclamation (《重启锚点》) navigation & residual state."""
    target_up = _normalize_ra_stage_token(target_stage or "RA-1")
    target_nodash = target_up.replace("-", "")
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    gray_1280 = np.asarray(img_1280.convert("L"), dtype=np.float32) if np is not None else None

    state: Dict[str, Any] = {
        "screen": "unknown",
        "ready_for_maa": False,
        "needs_cleanup": False,
        "has_residual_task": False,
        "drawer_expanded": False,
        "in_battle": False,
        "is_confirm_modal": False,
        "target_stage": target_up,
        "target_stage_xy": None,
        "open_construction_xy": None,
        "start_construction_xy": None,
        "terminal_xy": None,
        "long_term_xy": None,
        "reclamation_xy": None,
        "relaunch_anchor_xy": None,
        "enter_theme_xy": None,
        "guide_popup_xy": None,
        "settle_xy": None,
        "abandon_xy": None,
        "confirm_xy": None,
        "continue_tap_xy": None,
        "return_xy": None,
        "ocr_texts": [],
    }

    has_lte_tpl, _, lte_tpl_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Roguelike/base/Roguelike@IntegratedStrategies.png", (0, 560, 1280, 160), 0.74, None)],
    )
    if has_lte_tpl:
        state["long_term_xy"] = lte_tpl_xy

    has_in_battle_tpl, _, _, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [("Battle/BattleFlag/BattleOfficiallyBegin.png", (1160, 15, 85, 75), 0.72, (100, 255))],
    )
    if has_in_battle_tpl:
        state["in_battle"] = True

    has_ret_tpl, _, ret_tpl_xy, _ = _match_any_template(
        gray_1280,
        maa_dir,
        [
            ("ReturnButton/Return.png", (0, 0, 180, 80), 0.72, None),
            ("ReturnButton/Return-White.png", (0, 0, 180, 80), 0.72, None),
        ],
    )
    if has_ret_tpl:
        state["return_xy"] = ret_tpl_xy

    boxes: List[Dict[str, Any]] = []
    if use_ocr:
        try:
            boxes = find_text_boxes_via_ocr(
                img,
                roi=(0, 0, 1280, 720),
                include_inverted=False,
                timeout=6,
                cancel_event=cancel_event,
            )
        except Exception:
            boxes = []

    line_texts: List[str] = []
    home_markers = 0
    terminal_markers = 0
    lobby_markers = 0
    map_markers = 0
    hub_markers = 0
    long_term_markers = 0
    loading_markers = 0
    drawer_markers = 0
    in_battle_markers = 0
    confirm_prompt_markers = 0
    generic_confirm_xy: Optional[Tuple[int, int]] = None
    add_to_terminal_xy: Optional[Tuple[int, int]] = None

    for b in boxes:
        comp = str(b.get("compact", "") or "")
        if not comp:
            continue
        if b.get("level", "line") == "line":
            line_texts.append(comp)
        cx, cy = int(b.get("cx", 0)), int(b.get("cy", 0))
        norm = _normalize_ra_stage_token(comp)

        # Top-left full-screen guide popup close button
        if ("关闭" in comp or norm in ("CLOSE", "×CLOSE", "XCLOSE")) and 25 <= cx <= 285 and 55 <= cy <= 225:
            if state["guide_popup_xy"] is None:
                state["guide_popup_xy"] = (cx, cy)

        # Top-left Return button
        if "返回" in comp and cx <= 220 and cy <= 100 and state["return_xy"] is None:
            state["return_xy"] = (cx, cy)

        # 0. RelaunchAnchor theme lobby markers (must click 「开始建设」 to enter map!)
        if any(k in comp for k in ("罗德岛采购平台", "采购平台", "信息记录中心", "记录中心", "可露希尔手册", "蚀刻章套组")):
            lobby_markers += 1
        if ("开始建设" in comp or "继续建设" in comp) and cy >= 520 and cx >= 850:
            state["start_construction_xy"] = (cx, cy)

        # 1. Clean RelaunchAnchor pre-start / strategy controls ready for MAA
        if any(k in comp for k in ("开启建设", "精耕细作", "筹划经营", "确认选择")):
            state["ready_for_maa"] = True
            if "开启建设" in comp:
                state["open_construction_xy"] = (cx, cy)

        # 1b. Residual / unsettled / abandon / confirmation controls
        if any(k in comp for k in ("前往结算", "立即结算", "完成建设", "结束建设", "结算建设")) or (
            comp == "结算" and cy >= 450 and cx >= 600
        ):
            if state["settle_xy"] is None:
                state["settle_xy"] = (cx, cy)
        if any(k in comp for k in ("放弃本次行动", "放弃行动", "放弃建设", "退出建设", "退出行动")) or (
            comp in ("放弃", "退出", "撤离") and cy >= 180
        ):
            if state["abandon_xy"] is None:
                state["abandon_xy"] = (cx, cy)
        if any(
            k in comp
            for k in ("是否放弃", "确认放弃", "是否退出", "确认退出", "是否结算", "确认结算", "放弃当前", "结束当前", "退出后将", "立即放弃")
        ):
            confirm_prompt_markers += 1
        if comp in ("确认", "确定", "确认放弃", "确认退出", "确认结算") and cy >= 300:
            if "确认选择" not in comp and generic_confirm_xy is None:
                generic_confirm_xy = (cx, cy)
        if any(k in comp for k in ("点击任意处继续", "点击空白处继续", "点击继续", "结算完成", "本次结算", "建设完成", "行动结束")):
            if state["continue_tap_xy"] is None:
                state["continue_tap_xy"] = (cx, cy)

        # 1c. Active in-battle / in-stage construction markers
        if any(k in comp for k in ("请部署罗德岛", "采集基站", "交付木材", "交付石材", "请部署", "部署费用", "任务目标")):
            in_battle_markers += 1

        # 1d. Right-hand stage detail drawer expanded markers (shifts/blocks map)
        if any(k in comp for k in ("关卡地图", "敌人详情", "建设详情", "建设指标", "剩余天数")) or "MISSIONINFORMATION" in norm:
            drawer_markers += 1

        # 2. RelaunchAnchor stage map markers & target stage node (RA-1 / RA-4 / RA-15)
        if any(k in comp for k in ("建设元件", "建设装置", "统筹系统", "驻扎点")):
            map_markers += 1
        if norm == target_up or norm == target_nodash or target_up in norm:
            # Prefer map node label over drawer header when both appear (drawer header sits at cx >= 760, cy <= 180)
            if state["target_stage_xy"] is None or (cx < 760 and state["target_stage_xy"][0] >= 760):
                state["target_stage_xy"] = (cx, cy)

        # 3. Home screen markers
        if "终端" in comp and "添加到终端" not in comp and cx >= 620 and cy <= 320:
            state["terminal_xy"] = (cx, cy)
            home_markers += 1
        elif any(k in comp for k in ("理智/", "干员", "角色管理", "基建", "采购中心", "公开招募", "编队", "干员寻访", "好友", "档案")):
            home_markers += 1

        # 4. Terminal screen markers (new icon-only bottom bar & sub-tabs)
        if any(k in comp for k in ("长期探索", "长朗探索")) and cy >= 500:
            state["long_term_xy"] = (cx, cy)
            long_term_markers += 1
        if any(k in comp for k in ("前往上一次作战", "剿灭战模拟", "卫戍协议", "全息作战矩阵", "周期挑战", "晶体合约赏金", "作战回顾")):
            terminal_markers += 1

        # 5. Long-term exploration -> 生息演算
        if "生息演算" in comp:
            state["reclamation_xy"] = (cx, cy)
            long_term_markers += 1
        if "集成战略" in comp or "查看全部主题" in comp:
            long_term_markers += 1

        # 6. Reclamation theme selector hub -> 重启锚点
        if "重启锚点" in comp:
            state["relaunch_anchor_xy"] = (cx, cy)
            hub_markers += 1
        if any(k in comp for k in ("新基地等级", "尚待探索", "主题更新进行中", "沙洲遗闻", "生息日")):
            hub_markers += 1
        if "添加到终端" in comp or "添加到终师" in comp:
            hub_markers += 1
            add_to_terminal_xy = (cx, cy)

        # 7. Enter button inside Reclamation hub
        if any(k in comp for k in ("进入主题", "进入演算", "开始演算", "继续演算", "开启演算")) and cy >= 420 and cx >= 680:
            if state["enter_theme_xy"] is None:
                state["enter_theme_xy"] = (cx, cy)

        # 8. Loading splash screen
        if "RECLAMATION" in norm or "RELAUNCH" in norm:
            loading_markers += 1

    # White-on-dark bottom-right action button fallback (e.g. 「前往结算」 / 「开启建设」 inside expanded drawer)
    if (
        use_ocr
        and drawer_markers >= 1
        and not state["ready_for_maa"]
        and state["settle_xy"] is None
        and state["abandon_xy"] is None
        and gray_1280 is not None
    ):
        btn_roi = gray_1280[580:670, 920:1240]
        if btn_roi.size > 0 and float(np.median(btn_roi)) < 120.0 and int((btn_roi >= 180.0).sum()) >= 30:
            try:
                inv_boxes = find_text_boxes_via_ocr(
                    img,
                    roi=(860, 560, 420, 150),
                    include_inverted=True,
                    timeout=6,
                    cancel_event=cancel_event,
                )
            except Exception:
                inv_boxes = []
            for b in inv_boxes:
                comp = str(b.get("compact", "") or "")
                if not comp:
                    continue
                cx, cy = int(b.get("cx", 0)), int(b.get("cy", 0))
                if any(k in comp for k in ("前往结算", "立即结算", "完成建设", "结束建设", "结算")):
                    if state["settle_xy"] is None:
                        state["settle_xy"] = (cx, cy)
                elif any(k in comp for k in ("放弃本次行动", "放弃行动", "放弃建设", "放弃", "退出")):
                    if state["abandon_xy"] is None:
                        state["abandon_xy"] = (cx, cy)
                elif "开启建设" in comp:
                    state["ready_for_maa"] = True
                    state["open_construction_xy"] = (cx, cy)

    state["ocr_texts"] = (line_texts or [str(b.get("compact", "")) for b in boxes if b.get("compact")])[:40]
    state["drawer_expanded"] = drawer_markers >= 1
    if in_battle_markers >= 1:
        state["in_battle"] = True
    if confirm_prompt_markers >= 1:
        state["is_confirm_modal"] = True
        state["confirm_xy"] = generic_confirm_xy or (860, 495)
    elif generic_confirm_xy is not None and (state["abandon_xy"] is not None or state["settle_xy"] is not None):
        state["confirm_xy"] = generic_confirm_xy

    # Classify screen in strict priority order
    if state["guide_popup_xy"] is not None:
        state["screen"] = "guide_popup"
        state["needs_cleanup"] = True
        state["ready_for_maa"] = False
    elif state["is_confirm_modal"] or state["abandon_xy"] is not None or state["settle_xy"] is not None or state["continue_tap_xy"] is not None:
        state["screen"] = "relaunch_anchor_residual_settlement"
        state["has_residual_task"] = True
        state["needs_cleanup"] = True
        state["ready_for_maa"] = False
    elif state["in_battle"]:
        state["screen"] = "relaunch_anchor_in_battle"
        state["has_residual_task"] = True
        state["needs_cleanup"] = True
        state["ready_for_maa"] = False
    elif state["ready_for_maa"]:
        state["screen"] = "relaunch_anchor_ready"
    elif state["drawer_expanded"] and (map_markers >= 1 or state["target_stage_xy"] is not None):
        state["screen"] = "relaunch_anchor_drawer_open"
        state["needs_cleanup"] = True
        state["ready_for_maa"] = False
    elif map_markers >= 2 or state["target_stage_xy"] is not None:
        state["screen"] = "relaunch_anchor_map"
        state["ready_for_maa"] = True
    elif lobby_markers >= 1:
        state["screen"] = "relaunch_anchor_lobby"
        if state["start_construction_xy"] is None:
            state["start_construction_xy"] = (1125, 636)
    elif state["start_construction_xy"] is not None and home_markers == 0 and terminal_markers == 0 and hub_markers == 0:
        # Standalone 「开始建设」 button without lobby markers -> strategy screen ready for MAA
        state["screen"] = "relaunch_anchor_ready"
        state["ready_for_maa"] = True
    elif state["reclamation_xy"] is not None or (long_term_markers >= 2 and hub_markers == 0):
        state["screen"] = "long_term_exploration"
        if state["reclamation_xy"] is None:
            state["reclamation_xy"] = (826, 499)
    elif hub_markers >= 1:
        state["screen"] = "reclamation_hub"
        if state["enter_theme_xy"] is None:
            if add_to_terminal_xy is not None:
                state["enter_theme_xy"] = (min(1220, add_to_terminal_xy[0] + 210), add_to_terminal_xy[1])
            else:
                state["enter_theme_xy"] = (1150, 632)
    elif terminal_markers >= 1 or (state["long_term_xy"] is not None and home_markers < 2):
        state["screen"] = "terminal"
        if state["long_term_xy"] is None:
            state["long_term_xy"] = (822, 670)
    elif (state["terminal_xy"] is not None and home_markers >= 2) or home_markers >= 3:
        state["screen"] = "home"
        if state["terminal_xy"] is None:
            state["terminal_xy"] = (940, 185)
    elif state["enter_theme_xy"] is not None:
        state["screen"] = "reclamation_hub"
    elif loading_markers >= 1 or not state["ocr_texts"]:
        state["screen"] = "loading"
    return state


def cleanup_reclamation_residual_state(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    target_stage: str = "RA-1",
    reset_drawer: bool = False,
    max_steps: int = 10,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Detect and clear unfinished Reclamation tasks, active battles, guide popups, or blocking detail drawers."""
    target_up = _normalize_ra_stage_token(target_stage or "RA-1")
    summary: Dict[str, Any] = {
        "ok": True,
        "cleaned": False,
        "cancelled": False,
        "actions": [],
        "final_screen": "unknown",
    }
    if not adb_path or not adb_addr:
        summary["ok"] = False
        return summary

    drawer_collapse_attempts = 0
    unknown_after_action = 0
    for _ in range(max(1, int(max_steps))):
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            summary["ok"] = False
            return summary

        img = adb_screencap_image(adb_path, adb_addr, timeout=5.0, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            summary["ok"] = False
            return summary
        if img is None:
            summary["ok"] = len(summary["actions"]) > 0
            return summary

        st = detect_reclamation_screen_state(
            img,
            maa_dir,
            target_stage=target_up,
            use_ocr=True,
            cancel_event=cancel_event,
        )
        if cancel_event and cancel_event.is_set():
            summary["cancelled"] = True
            summary["ok"] = False
            return summary

        screen = str(st.get("screen") or "unknown")
        summary["final_screen"] = screen

        # Also check visual guide carousel if OCR missed the close label
        if screen != "guide_popup" and not st.get("needs_cleanup") and screen in ("unknown", "loading"):
            g_det = detect_guide_popup(img, maa_dir=maa_dir, use_ocr=False, cancel_event=cancel_event)
            if g_det.get("detected") and g_det.get("close_xy"):
                screen = "guide_popup"
                st["guide_popup_xy"] = g_det["close_xy"]

        if screen == "guide_popup" and st.get("guide_popup_xy"):
            gx, gy = st["guide_popup_xy"]
            _emit(log_callback, f"[RECLAMATION:CLEANUP] 检测到机制教程弹窗，点击左上角「× 关闭」({gx}, {gy})...")
            adb_tap_norm(adb_path, adb_addr, gx, gy, img.width, img.height, cancel_event=cancel_event)
            summary["actions"].append("dismiss_guide_popup")
            summary["cleaned"] = True
            wait_sec = 0.85
        elif screen == "relaunch_anchor_residual_settlement":
            if st.get("is_confirm_modal"):
                cx, cy = st.get("confirm_xy") or (860, 495)
                _emit(log_callback, f"[RECLAMATION:CLEANUP] 检测到放弃/结算确认框，点击确认 ({cx}, {cy})...")
                adb_tap_norm(adb_path, adb_addr, cx, cy, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("confirm_modal")
            elif st.get("abandon_xy"):
                ax, ay = st["abandon_xy"]
                _emit(log_callback, f"[RECLAMATION:CLEANUP] 检测到残留任务「放弃/退出」按钮 ({ax}, {ay})，点击清理...")
                adb_tap_norm(adb_path, adb_addr, ax, ay, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("tap_abandon")
            elif st.get("settle_xy"):
                sx, sy = st["settle_xy"]
                _emit(log_callback, f"[RECLAMATION:CLEANUP] 检测到未结算残留任务（「前往结算/立即结算」 {sx}, {sy}），点击进入结算...")
                adb_tap_norm(adb_path, adb_addr, sx, sy, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("tap_settle")
            else:
                tx, ty = st.get("continue_tap_xy") or (640, 620)
                _emit(log_callback, f"[RECLAMATION:CLEANUP] 检测到结算完成画面，点击继续 ({tx}, {ty})...")
                adb_tap_norm(adb_path, adb_addr, tx, ty, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("tap_settlement_continue")
            summary["cleaned"] = True
            wait_sec = 1.3
        elif screen == "relaunch_anchor_in_battle":
            _emit(log_callback, "[RECLAMATION:CLEANUP] 检测到仍处于执行中的生息演算关卡/战斗内，点击左上角设置菜单准备放弃退出...")
            adb_tap_norm(adb_path, adb_addr, 68, 48, img.width, img.height, cancel_event=cancel_event)
            summary["actions"].append("open_battle_menu")
            summary["cleaned"] = True
            wait_sec = 1.0
        elif screen == "relaunch_anchor_drawer_open" or (
            reset_drawer and st.get("drawer_expanded") and screen in ("relaunch_anchor_ready", "relaunch_anchor_map")
        ):
            drawer_collapse_attempts += 1
            if drawer_collapse_attempts == 1:
                _emit(log_callback, "[RECLAMATION:CLEANUP] 检测到右侧关卡详情抽屉已展开遮挡地图，点击左侧地图空白处收起抽屉...")
                adb_tap_norm(adb_path, adb_addr, 240, 360, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("collapse_drawer_blank")
            else:
                rx, ry = st.get("return_xy") or (75, 42)
                _emit(log_callback, f"[RECLAMATION:CLEANUP] 右侧详情抽屉仍未收起，点击左上角「返回」({rx}, {ry}) 重置界面...")
                adb_tap_norm(adb_path, adb_addr, rx, ry, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("collapse_drawer_return")
            summary["cleaned"] = True
            wait_sec = 1.0
        elif screen in (
            "relaunch_anchor_map",
            "relaunch_anchor_lobby",
            "relaunch_anchor_ready",
            "home",
            "terminal",
            "long_term_exploration",
            "reclamation_hub",
        ):
            return summary
        elif screen == "loading":
            wait_sec = 1.4
        else:
            if summary["actions"] and unknown_after_action < 2:
                unknown_after_action += 1
                _emit(log_callback, "[RECLAMATION:CLEANUP] 清理过渡画面中，轻点屏幕中央跳过结算动画...")
                adb_tap_norm(adb_path, adb_addr, 640, 620, img.width, img.height, cancel_event=cancel_event)
                summary["actions"].append("tap_transition")
                wait_sec = 1.2
            else:
                return summary

        if wait_interruptible(wait_sec, cancel_event=cancel_event, step=0.25):
            summary["cancelled"] = True
            summary["ok"] = False
            return summary

    return summary


def navigate_to_reclamation_relaunch_anchor(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    target_stage: str = "RA-1",
    max_steps: int = 14,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Navigate from Home/Terminal/Exploration into Reclamation 《重启锚点》 stage screen via ADB+OCR."""
    target_up = _normalize_ra_stage_token(target_stage or "RA-1")
    result: Dict[str, Any] = {
        "ok": False,
        "cancelled": False,
        "already_in_ra": False,
        "target_stage": target_up,
        "last_screen": "unknown",
        "steps": 0,
        "reason": "",
    }
    if not adb_path or not adb_addr:
        result["reason"] = "ADB 未就绪"
        return result

    tapped_relaunch_once = False
    loading_waits = 0
    unknown_waits = 0
    for step in range(max(1, int(max_steps))):
        if cancel_event and cancel_event.is_set():
            result["cancelled"] = True
            result["reason"] = "cancelled"
            return result

        img = adb_screencap_image(adb_path, adb_addr, timeout=5.0, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            result["cancelled"] = True
            result["reason"] = "cancelled"
            return result
        if img is None:
            result["reason"] = "ADB 截屏失败，无法识别当前游戏界面"
            return result

        st = detect_reclamation_screen_state(
            img,
            maa_dir,
            target_stage=target_up,
            use_ocr=True,
            cancel_event=cancel_event,
        )
        if cancel_event and cancel_event.is_set():
            result["cancelled"] = True
            result["reason"] = "cancelled"
            return result
        screen = st["screen"]
        result["last_screen"] = screen
        result["steps"] = step

        if screen == "guide_popup" and st.get("guide_popup_xy"):
            gx, gy = st["guide_popup_xy"]
            _emit(log_callback, f"[RECLAMATION] 导航中检测到全屏机制教程浮层，点击「× 关闭」({gx}, {gy})...")
            adb_tap_norm(adb_path, adb_addr, gx, gy, img.width, img.height, cancel_event=cancel_event)
            if wait_interruptible(0.85, cancel_event=cancel_event, step=0.25):
                result["cancelled"] = True
                result["reason"] = "cancelled"
                return result
            continue

        if screen in ("relaunch_anchor_residual_settlement", "relaunch_anchor_in_battle", "relaunch_anchor_drawer_open"):
            _emit(log_callback, f"[RECLAMATION] 检测到残留状态 ({screen})，自动执行状态清理以恢复干净大地图...")
            cln = cleanup_reclamation_residual_state(
                adb_path,
                adb_addr,
                maa_dir,
                target_stage=target_up,
                reset_drawer=True,
                max_steps=8,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )
            if cln.get("cancelled") or (cancel_event and cancel_event.is_set()):
                result["cancelled"] = True
                result["reason"] = "cancelled"
                return result
            continue

        if screen == "relaunch_anchor_ready":
            if step == 0:
                result["already_in_ra"] = True
            result["ok"] = True
            result["reason"] = "已就绪于《重启锚点》开启建设/关卡界面"
            return result

        if screen == "relaunch_anchor_map":
            if st.get("target_stage_xy"):
                sx, sy = st["target_stage_xy"]
                _emit(log_callback, f"[RECLAMATION] 在《重启锚点》大地图识别到 {target_up} ({sx}, {sy})，点击打开关卡准备界面...")
                adb_tap_norm(adb_path, adb_addr, sx, sy, img.width, img.height, cancel_event=cancel_event)
                if wait_interruptible(1.2, cancel_event=cancel_event, step=0.25):
                    result["cancelled"] = True
                    result["reason"] = "cancelled"
                    return result
            else:
                _emit(log_callback, f"[RECLAMATION] 已到达《重启锚点》大地图，移交 MAA 识别 {target_up} 并开启建设...")
            result["ok"] = True
            result["already_in_ra"] = step == 0
            result["reason"] = f"已到达《重启锚点》大地图，准备执行 {target_up}"
            return result

        if screen == "loading":
            loading_waits += 1
            if loading_waits <= 4:
                _emit(log_callback, "[RECLAMATION] 检测到界面加载过渡动画，等待画面就绪...")
                if wait_interruptible(1.8, cancel_event=cancel_event, step=0.25):
                    result["cancelled"] = True
                    result["reason"] = "cancelled"
                    return result
                continue
        else:
            loading_waits = 0
            if screen != "unknown":
                unknown_waits = 0

        if screen == "relaunch_anchor_lobby":
            sx, sy = st.get("start_construction_xy") or (1125, 636)
            _emit(log_callback, f"[RECLAMATION] 《重启锚点》主题大厅 -> 点击「开始建设」({sx}, {sy}) 进入关卡大地图...")
            adb_tap_norm(adb_path, adb_addr, sx, sy, img.width, img.height, cancel_event=cancel_event)
            wait_sec = 2.6
        elif screen == "home" and st.get("terminal_xy"):
            tx, ty = st["terminal_xy"]
            _emit(log_callback, f"[RECLAMATION] 主界面 -> 点击「终端」({tx}, {ty})...")
            adb_tap_norm(adb_path, adb_addr, tx, ty, img.width, img.height, cancel_event=cancel_event)
            wait_sec = 1.6
        elif screen == "terminal" and st.get("long_term_xy"):
            lx, ly = st["long_term_xy"]
            _emit(log_callback, f"[RECLAMATION] 终端 -> 点击「长期探索」({lx}, {ly})...")
            adb_tap_norm(adb_path, adb_addr, lx, ly, img.width, img.height, cancel_event=cancel_event)
            wait_sec = 1.6
        elif screen == "long_term_exploration" and st.get("reclamation_xy"):
            rx, ry = st["reclamation_xy"]
            _emit(log_callback, f"[RECLAMATION] 长期探索 -> 点击「生息演算」({rx}, {ry})...")
            adb_tap_norm(adb_path, adb_addr, rx, ry, img.width, img.height, cancel_event=cancel_event)
            wait_sec = 1.8
        elif screen == "reclamation_hub":
            if st.get("relaunch_anchor_xy") and not tapped_relaunch_once:
                ax, ay = st["relaunch_anchor_xy"]
                _emit(log_callback, f"[RECLAMATION] 生息演算主题页 -> 选中「重启锚点」({ax}, {ay})...")
                adb_tap_norm(adb_path, adb_addr, ax, ay, img.width, img.height, cancel_event=cancel_event)
                tapped_relaunch_once = True
                wait_sec = 0.9
            elif st.get("enter_theme_xy"):
                ex, ey = st["enter_theme_xy"]
                _emit(log_callback, f"[RECLAMATION] 生息演算主题页 -> 点击「进入主题」({ex}, {ey})...")
                adb_tap_norm(adb_path, adb_addr, ex, ey, img.width, img.height, cancel_event=cancel_event)
                wait_sec = 3.0
            else:
                _emit(log_callback, "[RECLAMATION] 生息演算主题页 -> 点击右下角「进入主题」(1150, 632)...")
                adb_tap_norm(adb_path, adb_addr, 1150, 632, img.width, img.height, cancel_event=cancel_event)
                wait_sec = 3.0
        else:
            if step == 0:
                _emit(log_callback, "[RECLAMATION] 当前未在《重启锚点》或主界面，尝试返回主界面后再进入终端...")
                ret = return_to_home_screen(
                    adb_path,
                    adb_addr,
                    maa_dir,
                    max_steps=4,
                    cancel_event=cancel_event,
                    log_callback=log_callback,
                )
                if ret.get("reason") == "cancelled" or (cancel_event and cancel_event.is_set()):
                    result["cancelled"] = True
                    result["reason"] = "cancelled"
                    return result
                continue
            unknown_waits += 1
            if unknown_waits <= 2:
                _emit(log_callback, f"[RECLAMATION] 画面切换中 ({unknown_waits}/2)，等待 1.6s 后重新识别...")
                if wait_interruptible(1.6, cancel_event=cancel_event, step=0.25):
                    result["cancelled"] = True
                    result["reason"] = "cancelled"
                    return result
                continue
            result["reason"] = (
                f"未能在当前画面（{screen}）定位《重启锚点》{target_up} 入口；"
                f"请手动进入「终端 -> 长期探索 -> 生息演算（重启锚点）」大地图并确保 {target_up} 已通关后再启动。"
            )
            return result

        if wait_interruptible(wait_sec, cancel_event=cancel_event, step=0.25):
            result["cancelled"] = True
            result["reason"] = "cancelled"
            return result

    result["reason"] = (
        f"自动导航超过最大步数（最后识别界面：{result['last_screen']}），未能到达《重启锚点》{target_up} 界面；"
        f"请手动打开 {target_up} 直到右下角出现「开启建设」后启动，或更新 MAA 内核至最新版。"
    )
    return result
