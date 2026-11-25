"""MAA core directory auto-discovery, manual path binding, and release installer."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .env_doctor import (
    check_maa_integrity,
    check_write_permission,
    get_base_dir,
    get_maa_version,
    load_launcher_config,
    save_launcher_config,
)

GITHUB_MAIN_LATEST_API = "https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases/latest"
GITHUB_RELEASE_LATEST_API = "https://api.github.com/repos/MaaAssistantArknights/MaaRelease/releases/latest"
MAA_PLUS_SUMMARY_API = "https://api.maa.plus/MaaAssistantArknights/api/version/summary.json"
GH_PROXY_PREFIXES = (
    "https://ghfast.top/",
    "https://gh-proxy.com/",
    "https://mirror.ghproxy.com/",
)
WIN_X64_ASSET_RE = re.compile(r"win-x64\.zip$", re.IGNORECASE)


def _common_install_candidates() -> List[Path]:
    """Return common Windows MAA installation paths without any personal usernames."""
    candidates: List[Path] = []
    for drive in ("C:", "D:", "E:", "F:"):
        dp = Path(drive + os.sep)
        if dp.exists():
            candidates.append(dp / "MAA")
            candidates.append(dp / "MAA_Core")
            for child in _safe_iterdir(dp):
                if child.is_dir() and "maa" in child.name.lower():
                    candidates.append(child)
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        val = os.environ.get(env_key)
        if not val:
            continue
        ep = Path(val)
        candidates.append(ep / "MAA")
        if env_key == "USERPROFILE":
            desktop = ep / "Desktop"
            candidates.append(desktop / "MAA")
            for child in _safe_iterdir(desktop):
                if child.is_dir() and "maa" in child.name.lower():
                    candidates.append(child)
    return candidates


def _safe_iterdir(directory: Path) -> List[Path]:
    try:
        if directory.is_dir():
            return sorted(directory.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        pass
    return []


def _resolve_maybe_relative(path_str: str, base_dir: Path) -> Optional[Path]:
    s = path_str.strip()
    if not s:
        return None
    p = Path(s)
    return p if p.is_absolute() else (base_dir / p)


def find_existing_maa(
    config_path: Optional[Path] = None,
    extra_search_dirs: Optional[List[Path]] = None,
) -> Optional[Path]:
    """Search MAA directory in priority order: sibling -> parent -> common -> config."""
    base = get_base_dir()
    cfg = load_launcher_config(config_path)

    ordered_candidates: List[Path] = []

    # 1. 同级目录（引擎目录自身、MAA_Core 及子级含 MAA 目录）
    ordered_candidates.append(base)
    ordered_candidates.append(base / "MAA_Core")
    for child in _safe_iterdir(base):
        if child.is_dir() and "maa" in child.name.lower():
            ordered_candidates.append(child)

    # 2. 父级目录（父级自身及其下含 MAA 的兄弟目录）
    parent = base.parent
    ordered_candidates.append(parent)
    for child in _safe_iterdir(parent):
        if child.is_dir() and child.resolve() != base.resolve() and "maa" in child.name.lower():
            ordered_candidates.append(child)

    # 3. 常见安装路径（含调用方追加路径）
    if extra_search_dirs:
        ordered_candidates.extend(extra_search_dirs)
    ordered_candidates.extend(_common_install_candidates())

    # 4. 配置文件记录的 maa_path
    cfg_path_val = _resolve_maybe_relative(str(cfg.get("maa_path", "") or ""), base)
    if cfg_path_val is not None:
        ordered_candidates.append(cfg_path_val)

    seen = set()
    for cand in ordered_candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if check_maa_integrity(resolved)["valid"]:
            return resolved
    return None


def resolve_maa_path(config_path: Optional[Path] = None) -> Optional[Path]:
    """Honor pinned manual maa_path first; otherwise run 4-step auto-discovery."""
    base = get_base_dir()
    cfg = load_launcher_config(config_path)
    if cfg.get("maa_path_pinned") and cfg.get("maa_path"):
        pinned = _resolve_maybe_relative(str(cfg["maa_path"]), base)
        if pinned is not None and check_maa_integrity(pinned)["valid"]:
            return pinned.resolve()
    return find_existing_maa(config_path)


def _to_storable_path(path: Path, base_dir: Path) -> str:
    resolved = path.resolve()
    base_resolved = base_dir.resolve()
    try:
        rel = resolved.relative_to(base_resolved)
        return str(rel) if str(rel) != "." else "."
    except ValueError:
        return str(resolved)


def bind_manual_maa_path(
    user_path: Union[str, Path],
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Validate and pin a user-selected MAA directory into launcher_config.json."""
    target = Path(user_path)
    integrity = check_maa_integrity(target)
    if not integrity["valid"]:
        return {
            "ok": False,
            "maa_path": str(target),
            "missing_items": integrity["missing_items"],
            "reason": integrity["reason"],
        }

    base = get_base_dir()
    cfg = load_launcher_config(config_path)
    resolved = target.resolve()
    cfg["maa_path"] = _to_storable_path(resolved, base)
    cfg["maa_path_pinned"] = True
    save_launcher_config(cfg, config_path)
    return {
        "ok": True,
        "maa_path": str(resolved),
        "stored_path": cfg["maa_path"],
        "integrity": integrity,
    }


def build_mirror_urls(github_url: str, official_mirrors: Optional[List[str]] = None) -> List[str]:
    """Build ordered download URL list with official CDN mirrors, China GitHub proxies, and direct fallback."""
    urls: List[str] = []
    for m in official_mirrors or []:
        if m and m not in urls:
            urls.append(m)
    if github_url:
        if "github.com/" in github_url or "githubusercontent.com/" in github_url:
            for prefix in GH_PROXY_PREFIXES:
                proxied = f"{prefix}{github_url}"
                if proxied not in urls:
                    urls.append(proxied)
        if github_url not in urls:
            urls.append(github_url)
    return urls


def _http_get_json(url: str, timeout_sec: int = 15) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MAA-Universal-Engine/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _match_github_win64(release_data: Dict[str, Any], source_label: str) -> Optional[Dict[str, Any]]:
    assets = release_data.get("assets", [])
    if not isinstance(assets, list):
        return None
    for asset in assets:
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if name and url and WIN_X64_ASSET_RE.search(name):
            return {
                "ok": True,
                "version": str(release_data.get("tag_name") or release_data.get("name") or ""),
                "asset_name": name,
                "total_size": int(asset.get("size") or 0),
                "urls": build_mirror_urls(url),
                "source": source_label,
            }
    return None


def fetch_latest_release_info(timeout_sec: int = 15) -> Dict[str, Any]:
    """Fetch latest win-x64 release metadata from GitHub API with MaaRelease and api.maa.plus fallbacks."""
    errors: List[str] = []

    for api_url, label in (
        (GITHUB_MAIN_LATEST_API, "github_main"),
        (GITHUB_RELEASE_LATEST_API, "github_maarelease"),
    ):
        try:
            data = _http_get_json(api_url, timeout_sec=timeout_sec)
            matched = _match_github_win64(data, label)
            if matched:
                return matched
            errors.append(f"{label}: no win-x64.zip asset in release")
        except Exception as exc:
            errors.append(f"{label}: {exc}")

    try:
        summary = _http_get_json(MAA_PLUS_SUMMARY_API, timeout_sec=timeout_sec)
        stable = summary.get("stable", {})
        detail_url = stable.get("detail")
        if detail_url:
            detail = _http_get_json(detail_url, timeout_sec=timeout_sec)
            assets = detail.get("details", {}).get("assets", [])
            for asset in assets:
                name = str(asset.get("name", ""))
                gh_url = str(asset.get("browser_download_url", ""))
                mirrors = [str(m) for m in asset.get("mirrors", []) if m]
                if name and WIN_X64_ASSET_RE.search(name):
                    return {
                        "ok": True,
                        "version": str(stable.get("version", "")),
                        "asset_name": name,
                        "total_size": int(asset.get("size") or 0),
                        "urls": build_mirror_urls(gh_url, official_mirrors=mirrors),
                        "source": "api_maa_plus",
                    }
        errors.append("api_maa_plus: no win-x64.zip asset in stable detail")
    except Exception as exc:
        errors.append(f"api_maa_plus: {exc}")

    raise RuntimeError("获取最新版 MAA 下载信息失败: " + " | ".join(errors))


def _detect_single_root_prefix(names: List[str]) -> Optional[str]:
    top_dirs = set()
    for raw in names:
        clean = raw.replace("\\", "/").lstrip("./")
        if not clean:
            continue
        parts = clean.split("/")
        if len(parts) == 1 and not raw.endswith("/"):
            return None
        top_dirs.add(parts[0])
        if len(top_dirs) > 1:
            return None
    return next(iter(top_dirs)) if len(top_dirs) == 1 else None


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_resolved = dest_dir.resolve()
    dest_resolved.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        prefix = _detect_single_root_prefix([info.filename for info in infos])
        for info in infos:
            norm = info.filename.replace("\\", "/")
            while norm.startswith("./"):
                norm = norm[2:]
            if not norm:
                continue

            parts = [p for p in norm.split("/") if p]
            if any(p == ".." for p in parts) or norm.startswith("/") or (len(parts) > 0 and ":" in parts[0]):
                raise ValueError(f"Unsafe zip path traversal entry: {info.filename}")

            if prefix and parts and parts[0] == prefix:
                parts = parts[1:]
            if not parts:
                continue

            target = (dest_resolved.joinpath(*parts)).resolve()
            if os.path.commonpath([str(dest_resolved), str(target)]) != str(dest_resolved):
                raise ValueError(f"Zip entry escapes destination directory: {info.filename}")

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=65536)


def _is_valid_zip_prefix(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _download_with_resume(
    urls: List[str],
    part_path: Path,
    total_size: int,
    timeout_sec: int,
    progress_callback: Optional[Callable[[int, int, str], None]],
    cancel_event: Optional[threading.Event],
) -> Tuple[bool, str]:
    part_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = "未提供有效下载地址"

    for idx, url in enumerate(urls, 1):
        if cancel_event and cancel_event.is_set():
            return False, "下载已取消"

        if part_path.is_file():
            existing = part_path.stat().st_size
            if (total_size > 0 and existing > total_size) or (existing >= 2 and not _is_valid_zip_prefix(part_path)):
                part_path.unlink(missing_ok=True)
                existing = 0
            elif total_size > 0 and existing == total_size and zipfile.is_zipfile(part_path):
                return True, ""
        else:
            existing = 0

        headers = {"User-Agent": "MAA-Universal-Engine/1.0"}
        if existing > 0:
            headers["Range"] = f"bytes={existing}-"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                status = getattr(resp, "status", 200)
                resp_headers = getattr(resp, "headers", {}) or {}
                ct = str(resp_headers.get("Content-Type", "")).lower()
                if any(bad in ct for bad in ("text/html", "application/json", "text/plain")):
                    raise ValueError(f"镜像返回非压缩包内容: {ct}")

                resuming = existing > 0 and (status == 206 or "Content-Range" in resp_headers)
                mode = "ab" if resuming else "wb"
                downloaded = existing if resuming else 0

                expected_total = total_size
                if expected_total <= 0:
                    cr = str(resp_headers.get("Content-Range", ""))
                    m_cr = re.search(r"/(\d+)$", cr)
                    if m_cr:
                        expected_total = int(m_cr.group(1))
                    else:
                        cl = resp_headers.get("Content-Length")
                        if cl and str(cl).isdigit():
                            expected_total = int(cl) + (downloaded if resuming else 0)

                if progress_callback:
                    progress_callback(downloaded, expected_total, f"正在从镜像源 {idx}/{len(urls)} 下载...")

                with open(part_path, mode) as out_f:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            return False, "下载已取消"
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, expected_total, "下载中...")

            actual_size = part_path.stat().st_size if part_path.is_file() else 0
            if expected_total > 0 and actual_size != expected_total:
                if actual_size > expected_total:
                    part_path.unlink(missing_ok=True)
                last_error = f"下载体积不一致 ({actual_size}/{expected_total})"
                continue
            if not zipfile.is_zipfile(part_path):
                part_path.unlink(missing_ok=True)
                last_error = "下载内容非有效 ZIP 压缩包"
                continue
            return True, ""
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and total_size > 0 and part_path.is_file() and part_path.stat().st_size == total_size and zipfile.is_zipfile(part_path):
                return True, ""
            last_error = f"HTTP {exc.code}: {exc.reason}"
        except Exception as exc:
            last_error = str(exc)

    return False, last_error


def download_and_install_maa(
    target_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    release_info: Optional[Dict[str, Any]] = None,
    timeout_sec: int = 30,
) -> Dict[str, Any]:
    """Download latest MAA win-x64 release with mirror fallback & resume, extract to MAA_Core/, and save config."""
    if cancel_event and cancel_event.is_set():
        return {"ok": False, "maa_path": "", "error": "下载已取消"}

    base = get_base_dir()
    dest = Path(target_dir) if target_dir is not None else (base / "MAA_Core")

    try:
        info = release_info if release_info is not None else fetch_latest_release_info(timeout_sec=min(timeout_sec, 15))
    except Exception as exc:
        return {"ok": False, "maa_path": str(dest), "error": str(exc)}

    asset_name = str(info.get("asset_name") or "MAA-win-x64.zip")
    total_size = int(info.get("total_size") or 0)
    urls = list(info.get("urls") or [])
    if not urls:
        return {"ok": False, "maa_path": str(dest), "error": "未解析到可用下载链接"}

    dl_dir = dest / "_downloads"
    part_path = dl_dir / f"{asset_name}.part"
    zip_path = dl_dir / asset_name

    ok_dl, dl_err = _download_with_resume(
        urls=urls,
        part_path=part_path,
        total_size=total_size,
        timeout_sec=timeout_sec,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    if not ok_dl:
        return {"ok": False, "maa_path": str(dest), "error": dl_err}

    if progress_callback:
        progress_callback(total_size, total_size, "正在解压安装 MAA 内核...")

    try:
        if zip_path.exists():
            zip_path.unlink()
        part_path.replace(zip_path)
        _safe_extract_zip(zip_path, dest)
    except Exception as exc:
        return {"ok": False, "maa_path": str(dest), "error": f"解压失败: {exc}"}
    finally:
        zip_path.unlink(missing_ok=True)
        try:
            if dl_dir.is_dir() and not any(dl_dir.iterdir()):
                dl_dir.rmdir()
        except OSError:
            pass

    integrity = check_maa_integrity(dest)
    if not integrity["valid"]:
        return {
            "ok": False,
            "maa_path": str(dest.resolve()),
            "error": integrity["reason"],
            "missing_items": integrity["missing_items"],
        }

    cfg = load_launcher_config(config_path)
    cfg["maa_path"] = _to_storable_path(dest, base)
    cfg["maa_path_pinned"] = False
    save_launcher_config(cfg, config_path)

    if progress_callback:
        progress_callback(total_size, total_size, "安装完成")

    return {
        "ok": True,
        "maa_path": str(dest.resolve()),
        "stored_path": cfg["maa_path"],
        "version": info.get("version", ""),
        "integrity": integrity,
    }


PRESERVED_TOP_DIRS = frozenset({"config", "universal_engine", "reports", "copilots", "debug", "cache"})
PRESERVED_TOP_FILES = frozenset({"arknights_ai_launcher.exe", "agent_task.json", "autonomous_agent.py"})
PRESERVED_DATA_FILES = frozenset({"operboxdata.json", "depotdata.json", "my_box.json", "scheduler_state.json"})


def parse_version_tuple(version_str: str) -> Tuple[Tuple[int, ...], int, str]:
    """Parse semver tag like 'v6.23.0' or 'v6.23.0-beta.1' into ((6,23,0), stability, pre_tag)."""
    raw = str(version_str or "").strip()
    if not raw or re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", raw):
        return (), 0, ""
    m = re.match(r"^v?(\d+(?:\.\d+)+)(?:[-_]?([A-Za-z0-9\.\-]+))?$", raw, re.IGNORECASE)
    if not m:
        return (), 0, ""
    nums = tuple(int(p) for p in m.group(1).split("."))
    pre = str(m.group(2) or "").strip().lower()
    stability = 1 if not pre else 0
    return nums, stability, pre


def is_newer_version(remote_ver: str, local_ver: str) -> bool:
    """Return True if remote_ver is a valid semantic version strictly newer than local_ver."""
    r_nums, r_stab, r_pre = parse_version_tuple(remote_ver)
    if not r_nums:
        return False
    l_nums, l_stab, l_pre = parse_version_tuple(local_ver)
    if not l_nums:
        return True
    max_len = max(len(r_nums), len(l_nums))
    r_pad = r_nums + (0,) * (max_len - len(r_nums))
    l_pad = l_nums + (0,) * (max_len - len(l_nums))
    if r_pad != l_pad:
        return r_pad > l_pad
    if r_stab != l_stab:
        return r_stab > l_stab
    if r_stab == 0 and l_stab == 0:
        return r_pre > l_pre
    return False


def check_maa_update(
    maa_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    timeout_sec: int = 15,
    release_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Check whether a newer MAA release is available compared to local MAA version."""
    target = Path(maa_dir).resolve() if maa_dir is not None else resolve_maa_path(config_path)
    if target is None or not target.is_dir():
        return {
            "ok": False,
            "maa_path": "",
            "local_version": "",
            "remote_version": "",
            "has_update": False,
            "release_info": None,
            "error": "未检测到有效的本地 MAA 目录，请先定位或安装 MAA。",
        }

    local_ver = get_maa_version(target)
    try:
        info = release_info if release_info is not None else fetch_latest_release_info(timeout_sec=timeout_sec)
    except Exception as exc:
        return {
            "ok": False,
            "maa_path": str(target),
            "local_version": local_ver,
            "remote_version": "",
            "has_update": False,
            "release_info": None,
            "error": str(exc),
        }

    remote_ver = str(info.get("version") or "").strip()
    has_upd = is_newer_version(remote_ver, local_ver)
    return {
        "ok": True,
        "maa_path": str(target),
        "local_version": local_ver,
        "remote_version": remote_ver,
        "has_update": has_upd,
        "release_info": info,
        "error": "",
    }


def _is_preserved_rel_path(rel_path: Path) -> bool:
    parts = [p.lower() for p in rel_path.parts if p]
    if not parts:
        return True
    if parts[0] in PRESERVED_TOP_DIRS:
        return True
    if len(parts) == 1 and parts[0] in PRESERVED_TOP_FILES:
        return True
    if len(parts) >= 2 and parts[0] == "data" and parts[1] in PRESERVED_DATA_FILES:
        return True
    return False


def _safe_replace_file(src: Path, dst: Path, dest_root: Path, rel_path: Path) -> Tuple[bool, bool]:
    """Copy `src` -> `dst`; if locked on Windows, move old `dst` into `.old/` first. Returns (ok, moved_to_old)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dst)
        return True, False
    except (PermissionError, OSError):
        if dst.exists():
            old_dst = dest_root / ".old" / rel_path
            old_dst.parent.mkdir(parents=True, exist_ok=True)
            if old_dst.exists():
                try:
                    old_dst.unlink()
                except OSError:
                    old_dst = old_dst.with_name(f"{old_dst.name}.{os.getpid()}")
            try:
                dst.replace(old_dst)
                shutil.copy2(src, dst)
                return True, True
            except OSError:
                return False, False
        return False, False


def update_maa_core(
    maa_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    release_info: Optional[Dict[str, Any]] = None,
    timeout_sec: int = 30,
) -> Dict[str, Any]:
    """Incrementally update MAA core & resource/ while strictly preserving config/, universal_engine/, and user data."""
    if cancel_event and cancel_event.is_set():
        return {"ok": False, "maa_path": "", "error": "更新已取消"}

    dest = Path(maa_dir).resolve() if maa_dir is not None else resolve_maa_path(config_path)
    if dest is None or not dest.is_dir():
        return {"ok": False, "maa_path": "", "error": "未检测到有效本地 MAA 目录"}

    wp = check_write_permission(dest)
    if not wp.get("writable"):
        return {"ok": False, "maa_path": str(dest), "error": str(wp.get("error") or "目标目录不可写")}

    old_ver = get_maa_version(dest)
    try:
        info = release_info if release_info is not None else fetch_latest_release_info(timeout_sec=min(timeout_sec, 15))
    except Exception as exc:
        return {"ok": False, "maa_path": str(dest), "old_version": old_ver, "error": str(exc)}

    asset_name = str(info.get("asset_name") or "MAA-win-x64.zip")
    total_size = int(info.get("total_size") or 0)
    urls = list(info.get("urls") or [])
    if not urls:
        return {"ok": False, "maa_path": str(dest), "old_version": old_ver, "error": "未解析到可用更新包链接"}

    dl_dir = dest / "_downloads"
    part_path = dl_dir / f"{asset_name}.part"
    zip_path = dl_dir / asset_name
    staging_dir = dest / "_update_staging"

    ok_dl, dl_err = _download_with_resume(
        urls=urls,
        part_path=part_path,
        total_size=total_size,
        timeout_sec=timeout_sec,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    if not ok_dl:
        return {"ok": False, "maa_path": str(dest), "old_version": old_ver, "error": dl_err}

    if progress_callback:
        progress_callback(total_size, total_size, "正在解压更新包至临时目录...")

    updated_files = 0
    skipped_preserved = 0
    locked_files: List[str] = []
    used_old_fallback = False

    try:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        if zip_path.exists():
            zip_path.unlink()
        part_path.replace(zip_path)
        _safe_extract_zip(zip_path, staging_dir)

        stage_integ = check_maa_integrity(staging_dir)
        if not stage_integ.get("valid"):
            return {
                "ok": False,
                "maa_path": str(dest),
                "old_version": old_ver,
                "error": f"更新包校验未通过，已中止覆盖: {stage_integ.get('reason', '')}",
            }

        if progress_callback:
            progress_callback(total_size, total_size, "正在安全覆盖更新内核与 resource/ 资源...")

        for src_file in sorted(staging_dir.rglob("*")):
            if cancel_event and cancel_event.is_set():
                return {"ok": False, "maa_path": str(dest), "old_version": old_ver, "error": "更新已取消"}
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(staging_dir)
            if _is_preserved_rel_path(rel):
                skipped_preserved += 1
                continue
            dst_file = dest / rel
            ok_rep, moved_old = _safe_replace_file(src_file, dst_file, dest, rel)
            if ok_rep:
                updated_files += 1
                if moved_old:
                    used_old_fallback = True
            else:
                locked_files.append(str(rel).replace("\\", "/"))
    except Exception as exc:
        return {"ok": False, "maa_path": str(dest), "old_version": old_ver, "error": f"增量更新失败: {exc}"}
    finally:
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(staging_dir, ignore_errors=True)
        try:
            if dl_dir.is_dir() and not any(dl_dir.iterdir()):
                dl_dir.rmdir()
        except OSError:
            pass

    integrity = check_maa_integrity(dest)
    new_ver = integrity.get("maa_version") or str(info.get("version") or "")
    if not integrity.get("valid"):
        return {
            "ok": False,
            "maa_path": str(dest),
            "old_version": old_ver,
            "new_version": new_ver,
            "updated_files": updated_files,
            "skipped_preserved": skipped_preserved,
            "locked_files": locked_files,
            "error": integrity.get("reason", "更新后内核完整性校验未通过"),
            "integrity": integrity,
        }

    if progress_callback:
        progress_callback(total_size, total_size, f"MAA 内核已更新至 {new_ver}")

    return {
        "ok": True,
        "maa_path": str(dest),
        "old_version": old_ver,
        "new_version": new_ver,
        "updated_files": updated_files,
        "skipped_preserved": skipped_preserved,
        "locked_files": locked_files,
        "restart_recommended": bool(locked_files or used_old_fallback),
        "integrity": integrity,
    }
