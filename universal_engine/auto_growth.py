"""Depot + Operator Box shortage analyzer and autonomous stage planner."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .ai_strategist import PROF_TAG_TO_ROLE, load_recruitment_roles
from .utils.env_doctor import get_base_dir

_NON_FARMABLE_STAGE_RE = re.compile(
    r"(?:^|-)(?:ST|TR|EX|MO)-\d+|-S-\d+|#f#|^SSREOPEN",
    re.IGNORECASE,
)


def _is_non_farmable_stage_code(code: str) -> bool:
    s = str(code or "").strip()
    if not s:
        return True
    return bool(_NON_FARMABLE_STAGE_RE.search(s))


def _build_stage_index(stages_list: Any) -> Dict[str, Dict[str, Any]]:
    """Build normalized farmability metadata index from resource/stages.json."""
    index: Dict[str, Dict[str, Any]] = {}
    if not isinstance(stages_list, list):
        return index
    for st in stages_list:
        if not isinstance(st, dict):
            continue
        code = str(st.get("code", "") or "").strip()
        if not code:
            continue
        stage_id = str(st.get("stageId", "") or "").strip()
        if "#f#" in stage_id.lower():
            continue
        raw_ap = st.get("apCost")
        try:
            ap_cost = int(raw_ap) if raw_ap is not None else 1
        except (ValueError, TypeError):
            ap_cost = 1

        drop_infos = st.get("dropInfos")
        mat_drops: List[str] = []
        if isinstance(drop_infos, list):
            for d in drop_infos:
                if not isinstance(d, dict):
                    continue
                dtype = str(d.get("dropType", "") or "").upper()
                iid = str(d.get("itemId", "") or "").strip()
                if not iid or iid.lower() == "furni" or dtype == "FURNITURE":
                    continue
                if iid not in mat_drops:
                    mat_drops.append(iid)

        is_farmable = (not _is_non_farmable_stage_code(code)) and (ap_cost > 0)
        key = code.upper()
        if key not in index or (is_farmable and not index[key]["is_farmable"]):
            index[key] = {
                "code": code,
                "stage_id": stage_id,
                "ap_cost": ap_cost,
                "material_drop_ids": mat_drops,
                "has_material_drop": bool(mat_drops),
                "is_farmable": is_farmable,
            }
    return index


def build_dynamic_event_stage_queue(
    prefix: str,
    start_stage: str = "",
    max_stage: int = 10,
    known_stages: Optional[List[str]] = None,
) -> List[str]:
    """Build ordered SideStory battle stage queue (e.g. ['OS-1', ..., 'OS-10']) when local stages.json lags."""
    pref_clean = str(prefix or "").strip().upper().rstrip("-")
    if not pref_clean:
        return []
    known = [str(s).strip().upper() for s in (known_stages or []) if str(s).strip()]
    base_list = known if known else [f"{pref_clean}-{i}" for i in range(1, max(1, int(max_stage)) + 1)]
    start_up = str(start_stage or "").strip().upper()
    if not start_up:
        return base_list
    if start_up in base_list:
        return base_list[base_list.index(start_up) :]
    m = re.match(rf"^{re.escape(pref_clean)}-(\d+)$", start_up)
    if m:
        start_num = int(m.group(1))
        end_num = max(int(max_stage), start_num)
        return [f"{pref_clean}-{i}" for i in range(start_num, end_num + 1)]
    return [start_up] + base_list


def _load_navigable_event_stages(
    maa_path: Path,
    event_key: str,
    base_dir: Optional[Path] = None,
) -> Optional[Set[str]]:
    """Load navigable stage codes from resource/tasks/Stages/<event>.json and OTA cache tasks.json."""
    key_clean = re.sub(r"[^A-Za-z0-9_\-]", "", str(event_key or "").strip())
    if not key_clean:
        return None

    navigable: Set[str] = set()
    stage_task_file = maa_path / "resource" / "tasks" / "Stages" / f"{key_clean}.json"
    if stage_task_file.is_file():
        data = _read_json_safe(stage_task_file, {})
        if isinstance(data, dict):
            for k in data:
                ks = str(k).strip()
                if "@" not in ks and re.match(r"^[A-Za-z0-9]+-\d+$", ks):
                    navigable.add(ks.upper())

    if navigable:
        # Also include any OTA-added stages for the same event prefix
        ota_candidates = [maa_path / "cache" / "resource" / "tasks" / "tasks.json"]
        if base_dir is not None:
            ota_candidates.insert(0, Path(base_dir) / "cache" / "resource" / "tasks" / "tasks.json")
        pref_up = key_clean.upper() + "-"
        for ota_file in ota_candidates:
            if not ota_file.is_file():
                continue
            ota_data = _read_json_safe(ota_file, {})
            if isinstance(ota_data, dict):
                for k in ota_data:
                    ks = str(k).strip()
                    if "@" not in ks and ks.upper().startswith(pref_up) and re.match(r"^[A-Za-z0-9]+-\d+$", ks):
                        navigable.add(ks.upper())
        return navigable

    return None

ROLE_TO_CHIP_PREFIX: Dict[str, str] = {
    "vanguard_dp": "321",
    "guard_lane": "322",
    "guard_burst": "322",
    "defender": "323",
    "sniper": "324",
    "caster": "325",
    "medic": "326",
    "supporter": "327",
    "specialist": "328",
}

CHIP_GROUP_TO_STAGE: Dict[str, str] = {
    "321": "PR-C",
    "327": "PR-C",
    "322": "PR-D",
    "328": "PR-D",
    "323": "PR-A",
    "326": "PR-A",
    "324": "PR-B",
    "325": "PR-B",
}

CHIP_GROUP_NAMES: Dict[str, str] = {
    "321": "先锋",
    "322": "近卫",
    "323": "重装",
    "324": "狙击",
    "325": "术师",
    "326": "医疗",
    "327": "辅助",
    "328": "特种",
}

# Game-day weekday (0=Mon .. 6=Sun, 04:00 UTC+8 server reset)
WEEKLY_OPEN_SCHEDULE: Dict[str, set] = {
    "PR-A": {0, 3, 4, 6},
    "PR-B": {0, 1, 4, 5},
    "PR-C": {2, 3, 5, 6},
    "PR-D": {1, 2, 5, 6},
    "LS": {0, 1, 2, 3, 4, 5, 6},
    "CE": {1, 3, 5, 6},
    "AP": {0, 3, 5, 6},
    "SK": {0, 2, 4, 5},
    "CA": {1, 2, 4, 6},
}

CURATED_T3_STAGES: Dict[str, List[str]] = {
    "30013": ["1-7", "10-11", "4-6"],
    "30023": ["4-2", "S3-1", "5-2"],
    "30033": ["7-4", "2-6", "6-4"],
    "30043": ["10-11", "7-18", "5-5"],
    "30053": ["3-1", "JT8-3", "4-5"],
    "30063": ["7-15", "4-10", "6-16"],
    "30073": ["6-11", "4-4", "7-5"],
    "30083": ["4-7", "6-2", "5-6"],
    "30093": ["7-17", "4-8", "6-14"],
    "30103": ["7-10", "4-9", "5-9"],
    "31013": ["JT8-2", "10-3", "7-8"],
    "31023": ["10-14", "R8-7", "9-12"],
    "31033": ["R8-11", "12-3", "9-14"],
    "31043": ["9-4", "12-10", "13-14"],
    "31053": ["12-17", "10-17", "9-6"],
    "31063": ["11-15", "12-13", "11-3"],
    "31073": ["13-5", "15-18", "17-17"],
    "31083": ["14-19", "13-15", "15-20"],
    "31093": ["15-9", "17-4"],
    "31103": ["17-5"],
    "31113": ["17-11"],
}


@dataclass
class GrowthDecision:
    recommended_stages: List[str] = field(default_factory=list)
    reason: str = ""
    shortages: List[Dict[str, Any]] = field(default_factory=list)
    open_today: Dict[str, bool] = field(default_factory=dict)
    active_event: Optional[Dict[str, Any]] = None
    data_staleness: Dict[str, Any] = field(default_factory=dict)


def _pick_freshest_file(primary: Path, fallback: Path) -> Optional[Path]:
    candidates = [p for p in (primary, fallback) if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_json_safe(path: Optional[Path], default: Any) -> Any:
    if path is None or not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def sync_my_box(
    maa_dir: Path,
    base_dir: Optional[Path] = None,
    force: bool = False,
    persist: bool = True,
) -> Dict[str, Any]:
    """Synchronize OperBoxData.json into portable <base_dir>/data/my_box.json (no absolute personal paths)."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    box_dst = root / "data" / "my_box.json"
    box_src = _pick_freshest_file(root / "data" / "OperBoxData.json", Path(maa_dir) / "data" / "OperBoxData.json")

    need_rebuild = force or (not box_dst.is_file())
    if not need_rebuild and box_src is not None:
        need_rebuild = box_src.stat().st_mtime > box_dst.stat().st_mtime

    if need_rebuild and box_src is not None:
        raw = _read_json_safe(box_src, {})
        owned = [o for o in raw.get("own_opers", []) if o.get("own")]
        owned.sort(key=lambda x: (-x.get("elite", 0), -x.get("rarity", 0), -x.get("level", 0)))
        e2_list = [o for o in owned if o.get("elite", 0) == 2]
        my_box = {
            "source": "data/OperBoxData.json",
            "total_owned": len(owned),
            "e2_count": len(e2_list),
            "e2_operators": [
                {
                    "id": o.get("id", ""),
                    "name": o.get("name", ""),
                    "rarity": o.get("rarity", 0),
                    "elite": o.get("elite", 0),
                    "level": o.get("level", 1),
                    "potential": o.get("potential", 1),
                }
                for o in e2_list
            ],
            "all_owned": {o["name"]: o for o in owned if o.get("name")},
        }
        if persist:
            box_dst.parent.mkdir(parents=True, exist_ok=True)
            box_dst.write_text(json.dumps(my_box, ensure_ascii=False, indent=2), encoding="utf-8")
        return my_box

    return _read_json_safe(box_dst, {"source": "", "total_owned": 0, "e2_count": 0, "e2_operators": [], "all_owned": {}})


def load_depot_data(maa_dir: Path, base_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load freshest DepotData.json from isolated base_dir or external maa_dir."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    src = _pick_freshest_file(root / "data" / "DepotData.json", Path(maa_dir) / "data" / "DepotData.json")
    data = _read_json_safe(src, {})
    if not isinstance(data, dict):
        data = {}
    return {
        "done": bool(data.get("done", False)),
        "syncTime": str(data.get("syncTime", "")),
        "data": data.get("data", {}) if isinstance(data.get("data"), dict) else {},
    }


def _parse_iso_datetime(ts_str: str) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        return dt
    except Exception:
        return None


def is_data_stale(
    maa_dir: Path,
    base_dir: Optional[Path] = None,
    ttl_hours: float = 24.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Check whether DepotData.json or OperBoxData.json is missing or older than ttl_hours."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    ref_now = now or datetime.now(timezone(timedelta(hours=8)))
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone(timedelta(hours=8)))

    depot_file = _pick_freshest_file(root / "data" / "DepotData.json", Path(maa_dir) / "data" / "DepotData.json")
    depot_age_h: Optional[float] = None
    depot_stale = True
    if depot_file is not None:
        depot_json = _read_json_safe(depot_file, {})
        sync_dt = _parse_iso_datetime(str(depot_json.get("syncTime", "")))
        if sync_dt is None:
            sync_dt = datetime.fromtimestamp(depot_file.stat().st_mtime, tz=timezone.utc)
        depot_age_h = round(max(0.0, (ref_now.astimezone(timezone.utc) - sync_dt.astimezone(timezone.utc)).total_seconds() / 3600.0), 2)
        depot_stale = depot_age_h > ttl_hours

    box_file = _pick_freshest_file(root / "data" / "OperBoxData.json", Path(maa_dir) / "data" / "OperBoxData.json")
    box_age_h: Optional[float] = None
    box_stale = True
    if box_file is not None:
        box_dt = datetime.fromtimestamp(box_file.stat().st_mtime, tz=timezone.utc)
        box_age_h = round(max(0.0, (ref_now.astimezone(timezone.utc) - box_dt).total_seconds() / 3600.0), 2)
        box_stale = box_age_h > ttl_hours

    return {
        "depot_stale": depot_stale,
        "box_stale": box_stale,
        "depot_age_hours": depot_age_h,
        "box_age_hours": box_age_h,
    }


def _parse_activity_time(s: str, tz_hours: int) -> Optional[datetime]:
    try:
        dt = datetime.strptime(s.strip(), "%Y/%m/%d %H:%M:%S")
        return dt.replace(tzinfo=timezone(timedelta(hours=tz_hours)))
    except Exception:
        return None


def get_open_stages_today(
    now: Optional[datetime] = None,
    activity_data: Optional[Dict[str, Any]] = None,
    server: str = "Official",
) -> Dict[str, bool]:
    """Compute today's open chip/resource stages (04:00 UTC+8 reset, respecting resourceCollection all-open)."""
    tz_cn = timezone(timedelta(hours=8))
    cur = now or datetime.now(tz_cn)
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=tz_cn)
    cur_cn = cur.astimezone(tz_cn)
    game_weekday = (cur_cn - timedelta(hours=4)).weekday()

    open_map = {
        key: (game_weekday in days)
        for key, days in WEEKLY_OPEN_SCHEDULE.items()
    }

    if activity_data:
        region_key = "Official" if server in ("Official", "Bilibili", "CN") else server
        rc = activity_data.get(region_key, {}).get("resourceCollection", {})
        if isinstance(rc, dict) and rc.get("IsResourceCollection"):
            tz_h = int(rc.get("TimeZone", 8))
            t_start = _parse_activity_time(str(rc.get("UtcStartTime", "")), tz_h)
            t_end = _parse_activity_time(str(rc.get("UtcExpireTime", "")), tz_h)
            if t_start and t_end and t_start <= cur.astimezone(timezone.utc) <= t_end:
                for key in open_map:
                    open_map[key] = True

    return open_map


def fold_craftable_inventory(raw_depot: Dict[str, int], item_index: Dict[str, Any]) -> Dict[str, int]:
    """Fold single-ingredient T2 materials into T3 effective inventory using item_index formula."""
    effective = {str(k): int(v or 0) for k, v in raw_depot.items()}
    for item_id, meta in item_index.items():
        if not isinstance(meta, dict):
            continue
        formula = meta.get("formula")
        if not isinstance(formula, dict) or len(formula) != 1:
            continue
        if int(meta.get("rarity", 0)) != 2:
            continue
        src_id, req_cnt = next(iter(formula.items()))
        req_int = int(req_cnt or 0)
        if req_int <= 0:
            continue
        src_meta = item_index.get(str(src_id), {})
        if int(src_meta.get("rarity", 99)) >= 2:
            continue
        craftable = effective.get(str(src_id), 0) // req_int
        if craftable > 0:
            effective[str(item_id)] = effective.get(str(item_id), 0) + craftable
    return effective


def _extract_active_event(
    activity_data: Dict[str, Any],
    now_utc: datetime,
    server: str,
    stages_by_code: Optional[Dict[str, Dict[str, Any]]] = None,
    navigable_stages: Optional[Set[str]] = None,
) -> Optional[Dict[str, Any]]:
    region_key = "Official" if server in ("Official", "Bilibili", "CN") else server
    side_stories = activity_data.get(region_key, {}).get("sideStoryStage", {})
    active: List[Tuple[datetime, str, Dict[str, Any]]] = []
    for key, block in side_stories.items():
        act = block.get("Activity", {})
        tz_h = int(act.get("TimeZone", 8))
        t_start = _parse_activity_time(str(act.get("UtcStartTime", "")), tz_h)
        t_end = _parse_activity_time(str(act.get("UtcExpireTime", "")), tz_h)
        if t_start and t_end and t_start <= now_utc <= t_end:
            active.append((t_start, key, block))
    if not active:
        return None
    active.sort(key=lambda x: x[0], reverse=True)
    _, key, block = active[0]
    act = block.get("Activity", {})
    stages: List[Dict[str, str]] = []
    seen_codes: Set[str] = set()
    for st in block.get("Stages", []):
        if not isinstance(st, dict):
            continue
        val = str(st.get("Value", "")).strip()
        drop = str(st.get("Drop", "")).strip()
        if not val or any(tok in drop for tok in ("代理", "搓玉", "教学", "剧情", "不可代理")):
            continue
        if _is_non_farmable_stage_code(val):
            continue

        canonical_code = val
        drop_item_id = drop if drop.isdigit() else ""
        if stages_by_code:
            meta = stages_by_code.get(val.upper())
            if meta is None:
                continue
            if not meta.get("is_farmable") or not meta.get("has_material_drop"):
                continue
            canonical_code = str(meta["code"])
            if not drop_item_id and meta.get("material_drop_ids"):
                drop_item_id = str(meta["material_drop_ids"][0])

        if navigable_stages is not None and canonical_code.upper() not in navigable_stages:
            continue

        if canonical_code.upper() in seen_codes:
            continue
        seen_codes.add(canonical_code.upper())
        stages.append({"code": canonical_code, "drop_item_id": drop_item_id})
    return {
        "key": key,
        "title": str(act.get("StageName") or act.get("Tip") or key),
        "stages": stages,
    }


def _compute_chip_shortages(
    my_box: Dict[str, Any],
    raw_depot: Dict[str, int],
    role_map: Dict[str, str],
    growth_cfg: Dict[str, Any],
    open_today: Dict[str, bool],
) -> List[Dict[str, Any]]:
    safety = growth_cfg.get("material_safety_stock", {})
    chip_t1_floor = int(safety.get("CHIP_T1", 5))
    chip_t2_floor = int(safety.get("CHIP_T2", 8))
    cap = max(0, int(growth_cfg.get("chip_untargeted_cap", 2)))
    targets = [str(x).strip() for x in growth_cfg.get("target_operators", []) if str(x).strip()]

    all_owned = my_box.get("all_owned", {})
    selected_ops: List[Dict[str, Any]] = []
    if targets:
        for name in targets:
            op = all_owned.get(name)
            if op and int(op.get("elite", 0)) < 2:
                selected_ops.append(op)
    else:
        candidates = [
            op for op in all_owned.values()
            if int(op.get("elite", 0)) < 2 and int(op.get("rarity", 0)) >= 5
        ]
        candidates.sort(key=lambda o: (-int(o.get("rarity", 0)), -int(o.get("elite", 0)), -int(o.get("level", 0))))
        selected_ops = candidates[:cap]

    demand_t1: Dict[str, int] = {p: chip_t1_floor for p in CHIP_GROUP_TO_STAGE}
    demand_t2: Dict[str, int] = {p: chip_t2_floor for p in CHIP_GROUP_TO_STAGE}

    for op in selected_ops:
        role = role_map.get(op.get("name", ""), "")
        prefix = ROLE_TO_CHIP_PREFIX.get(role)
        if not prefix:
            continue
        rarity = int(op.get("rarity", 5))
        elite = int(op.get("elite", 0))
        if elite == 0:
            demand_t1[prefix] += 5 if rarity == 6 else 4
        demand_t2[prefix] += 4 if rarity == 6 else 3

    results: List[Dict[str, Any]] = []
    for prefix, stage_group in CHIP_GROUP_TO_STAGE.items():
        if not open_today.get(stage_group, False):
            continue
        t1_id = f"{prefix}1"
        t2_id = f"{prefix}2"
        t3_id = f"{prefix}3"
        have_t1 = int(raw_depot.get(t1_id, 0))
        have_t2 = int(raw_depot.get(t2_id, 0)) + int(raw_depot.get(t3_id, 0)) * 2

        gap_t2 = max(0, demand_t2[prefix] - have_t2)
        gap_t1 = max(0, demand_t1[prefix] - have_t1)
        if gap_t2 > 0:
            results.append({
                "category": "chip",
                "item_id": t2_id,
                "name": f"{CHIP_GROUP_NAMES[prefix]}芯片组",
                "have": have_t2,
                "target": demand_t2[prefix],
                "deficit": gap_t2,
                "stage": f"{stage_group}-2",
            })
        elif gap_t1 > 0:
            results.append({
                "category": "chip",
                "item_id": t1_id,
                "name": f"{CHIP_GROUP_NAMES[prefix]}芯片",
                "have": have_t1,
                "target": demand_t1[prefix],
                "deficit": gap_t1,
                "stage": f"{stage_group}-1",
            })

    results.sort(key=lambda x: -x["deficit"])
    return results


def decide_optimal_stages(
    maa_dir: Path,
    config: Dict[str, Any],
    my_box: Optional[Dict[str, Any]] = None,
    depot_data: Optional[Dict[str, Any]] = None,
    last_battle_is_annihilation: bool = False,
    base_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    persist: bool = True,
) -> GrowthDecision:
    """Compute ranked farming stage candidates from Depot + Box shortages, open schedule, and active events."""
    root = Path(base_dir) if base_dir is not None else get_base_dir()
    maa_path = Path(maa_dir)
    growth_cfg = config.get("growth", {}) if isinstance(config.get("growth"), dict) else {}
    server = str(config.get("server", "Official") or "Official")

    cur_now = now or datetime.now(timezone(timedelta(hours=8)))
    if cur_now.tzinfo is None:
        cur_now = cur_now.replace(tzinfo=timezone(timedelta(hours=8)))
    now_utc = cur_now.astimezone(timezone.utc)

    box = my_box if my_box is not None else sync_my_box(maa_path, base_dir=root, persist=persist)
    depot = depot_data if depot_data is not None else load_depot_data(maa_path, base_dir=root)
    raw_depot = {str(k): int(v or 0) for k, v in depot.get("data", {}).items()}

    item_index = _read_json_safe(maa_path / "resource" / "item_index.json", {})
    stages_list = _read_json_safe(maa_path / "resource" / "stages.json", [])
    stages_by_code = _build_stage_index(stages_list)
    valid_codes = {meta["code"] for meta in stages_by_code.values() if meta.get("is_farmable")}

    act_file = _pick_freshest_file(
        root / "cache" / "gui" / "StageActivityV2.json",
        maa_path / "cache" / "gui" / "StageActivityV2.json",
    )
    activity_data = _read_json_safe(act_file, {})

    open_today = get_open_stages_today(now=cur_now, activity_data=activity_data, server=server)
    staleness = is_data_stale(
        maa_path,
        base_dir=root,
        ttl_hours=float(growth_cfg.get("depot_sync_ttl_hours", 24)),
        now=cur_now,
    )

    if growth_cfg.get("fold_craftable_materials", True) and item_index:
        effective_depot = fold_craftable_inventory(raw_depot, item_index)
    else:
        effective_depot = dict(raw_depot)

    safety = growth_cfg.get("material_safety_stock", {})
    t3_floor = int(safety.get("T3_DEFAULT", 30))

    candidates: List[str] = []
    shortages: List[Dict[str, Any]] = []
    reasons: List[str] = []

    def _add_stage(code: str) -> None:
        if code and code not in candidates:
            candidates.append(code)

    # 1. 当期 SideStory 活动材料关（严格结合 stages.json 与 resource/tasks 校验可代理刷材料关）
    active_event: Optional[Dict[str, Any]] = None
    if activity_data:
        raw_ev = _extract_active_event(activity_data, now_utc, server)
        if raw_ev:
            nav_stages = _load_navigable_event_stages(maa_path, raw_ev["key"], base_dir=root)
            active_event = _extract_active_event(
                activity_data,
                now_utc,
                server,
                stages_by_code=stages_by_code or None,
                navigable_stages=nav_stages,
            )
    if growth_cfg.get("prefer_event", True) and active_event and active_event["stages"]:
        ev_scored: List[Tuple[int, str, str]] = []
        for idx, st in enumerate(active_event["stages"]):
            code = st["code"]
            drop_id = st["drop_item_id"]
            have = effective_depot.get(drop_id, 0) if drop_id else 0
            deficit = max(0, t3_floor - have) if drop_id else 1
            ev_scored.append((deficit * 100 - idx, code, drop_id))
        ev_scored.sort(reverse=True)
        for _, code, drop_id in ev_scored:
            _add_stage(code)
            if drop_id:
                have = effective_depot.get(drop_id, 0)
                if have < t3_floor:
                    shortages.append({
                        "category": "event_material",
                        "item_id": drop_id,
                        "name": item_index.get(drop_id, {}).get("name", drop_id),
                        "have": have,
                        "target": t3_floor,
                        "deficit": t3_floor - have,
                        "stage": code,
                    })
        reasons.append(f"优先当期活动 [{active_event['title']}] 推荐关 {ev_scored[0][1]}")

    # 2. 今日开放芯片本（目标干员 / 上限约束）
    if growth_cfg.get("allow_chip_stages", True):
        role_map = load_recruitment_roles(maa_path)
        chip_shortages = _compute_chip_shortages(box, raw_depot, role_map, growth_cfg, open_today)
        for cs in chip_shortages:
            if not valid_codes or cs["stage"] in valid_codes:
                _add_stage(cs["stage"])
                shortages.append(cs)
        if chip_shortages:
            reasons.append(f"芯片缺口 {chip_shortages[0]['name']} -> {chip_shortages[0]['stage']}")

    # 3. 常驻主线 T3 材料缺口（离线精选效率表 + formula 折算）
    mat_shortages: List[Dict[str, Any]] = []
    for item_id, stage_opts in CURATED_T3_STAGES.items():
        have = effective_depot.get(item_id, 0)
        if have >= t3_floor:
            continue
        chosen = next((s for s in stage_opts if not valid_codes or s in valid_codes), None)
        if not chosen:
            continue
        mat_shortages.append({
            "category": "material",
            "item_id": item_id,
            "name": item_index.get(item_id, {}).get("name", item_id),
            "have": have,
            "target": t3_floor,
            "deficit": t3_floor - have,
            "stage": chosen,
        })
    mat_shortages.sort(key=lambda x: -x["deficit"])
    for ms in mat_shortages[:4]:
        _add_stage(ms["stage"])
        shortages.append(ms)
    if mat_shortages:
        reasons.append(f"常规材料缺口 {mat_shortages[0]['name']}({mat_shortages[0]['have']}/{t3_floor}) -> {mat_shortages[0]['stage']}")

    # 4. 龙门币 / 经验资源本兜底
    if growth_cfg.get("allow_resource_stages", True):
        lmd = raw_depot.get("4001", 0)
        exp_val = (
            raw_depot.get("2001", 0) * 200
            + raw_depot.get("2002", 0) * 400
            + raw_depot.get("2003", 0) * 1000
            + raw_depot.get("2004", 0) * 2000
        )
        if lmd < 500000 and open_today.get("CE", False) and (not valid_codes or "CE-6" in valid_codes):
            _add_stage("CE-6")
            shortages.append({"category": "resource", "item_id": "4001", "name": "龙门币", "have": lmd, "target": 500000, "deficit": 500000 - lmd, "stage": "CE-6"})
            reasons.append("龙门币偏低 -> CE-6")
        if exp_val < 200000 and open_today.get("LS", False) and (not valid_codes or "LS-6" in valid_codes):
            _add_stage("LS-6")
            reasons.append("作战记录偏低 -> LS-6")

    # 5. 保底防卡死队列：若无任何推荐且上一次非剿灭，先尝试 <LastBattle>("")；尾部永远保证 1-7
    if not candidates and not last_battle_is_annihilation:
        candidates.append("")
        reasons.append("暂无显著仓库缺口，沿用 <LastBattle> 并以 1-7 保底")

    if "1-7" in candidates:
        candidates = [c for c in candidates if c != "1-7"]
    candidates.append("1-7")

    return GrowthDecision(
        recommended_stages=candidates,
        reason=" | ".join(reasons) if reasons else "保底主线 [1-7]",
        shortages=shortages,
        open_today=open_today,
        active_event=active_event,
        data_staleness=staleness,
    )
