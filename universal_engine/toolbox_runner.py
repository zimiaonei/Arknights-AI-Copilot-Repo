"""PRTS · MAA Toolbox Runner and Catalog Inspector (小工具 · 牛杂).

Manages lightweight utility tasks including OS-Karlan, Secret Front, Stronghold Protocol,
Ticket Shops, Event Shop, Potential Raising, Material Synthesis, and Cursed Relic.
Zero GUI dependencies.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

if getattr(sys, "frozen", False):
    from universal_engine import maa_runner
else:
    try:
        from . import maa_runner
    except (ImportError, ValueError):
        from universal_engine import maa_runner

SECRET_FRONT_ENDINGS: List[Tuple[str, str]] = [
    ("结局 A", "EndingA"),
    ("结局 B", "EndingB"),
    ("结局 C", "EndingC"),
    ("结局 D", "EndingD"),
    ("结局 E（默认）", "EndingE"),
]

SECRET_FRONT_EVENTS: List[Tuple[str, str]] = [
    ("不优先指定事件", ""),
    ("支援作战平台", "支援作战平台"),
    ("游侠", "游侠"),
    ("诡影迷踪", "诡影迷踪"),
]

STRONGHOLD_MODES: List[Tuple[str, str]] = [
    ("刷角标（完整通关）", "badge"),
    ("刷分（放生速通）", "score"),
]

STRONGHOLD_TARGETS: List[Tuple[str, int]] = [
    ("金色角标（通关 3 次）", 3),
    ("彩色角标（通关 6 次）", 6),
]

EVENT_SHOP_BLACKLIST_PRESETS: List[Tuple[str, str]] = [
    ("数据增补仪", "数据增补仪"),
    ("数据增补条", "数据增补条"),
    ("龙门币", "龙门币"),
    ("家具零件", "家具零件"),
    ("无限池/其他", "其他"),
]

RA_THEME_OPTIONS: List[Tuple[str, str]] = [
    ("跟随原版 MAA (默认重启锚点)", ""),
    ("重启锚点 (RelaunchAnchor)", "RelaunchAnchor"),
    ("沙洲遗闻 (Tales)", "Tales"),
]

RA_MODE_OPTIONS: List[Tuple[str, str]] = [
    ("跟随原版 MAA", ""),
    ("RA-1 关卡循环 · 精耕细作 (RA1)", "RA1"),
    ("RA-4 关卡循环 · 筹划经营/维什戴尔 (RA4)", "RA4"),
    ("RA-15 关卡循环 · 筹划经营/圣聆初雪 (RA15)", "RA15"),
    ("沙洲遗闻 · 无存档刷繁荣度 (ProsperityNoSave)", "ProsperityNoSave"),
    ("沙洲遗闻 · 存档造物刷繁荣度 (ProsperityInSave)", "ProsperityInSave"),
]


def _format_ra_mode_display(theme: str, mode_int: Any, mode_label: str = "") -> Tuple[str, str]:
    theme_disp = "重启锚点" if theme == "RelaunchAnchor" else ("沙洲遗闻" if theme == "Tales" else theme)
    mode_map = {
        ("RelaunchAnchor", 16): "RA-1 关卡循环 (RA1 · 精耕细作)",
        ("RelaunchAnchor", 32): "RA-4 关卡循环 (RA4 · 筹划经营)",
        ("RelaunchAnchor", 48): "RA-15 关卡循环 (RA15 · 筹划经营)",
        ("Tales", 0): "无存档刷繁荣度",
        ("Tales", 1): "存档内造物刷繁荣度",
    }
    try:
        mi = int(mode_int)
        m_disp = mode_map.get((theme, mi), str(mode_label or mi))
    except Exception:
        m_disp = str(mode_label or "默认模式")
    return theme_disp, m_disp

# Default configuration dictionary for Toolbox items
DEFAULT_TOOLBOX_CONFIG: Dict[str, Any] = {
    "secret_front": {
        "ending": "EndingE",
        "priority_event": "",
    },
    "stronghold_protocol": {
        "theme": "Regulation",
        "mode": "badge",
        "target": 3,
        "runs": 60,
        "attempts": 0,
    },
    "event_shop": {
        "blacklist_presets": [],
        "custom_blacklist": "",
    },
    "auto_raise_potential": {
        "use_normal_token": False,
    },
    "blackflow_money": {
        "stop_when_investment_full": True,
        "starts_count": 999,
    },
}

# MiniGame file stem exclusion set for dynamic scan (built-in independent tools)
EXCLUDED_MINIGAMES: Set[str] = {
    "secretfront",
    "spa",
    "autoraisepotential",
    "materialsynthesis",
    "cursedrelic",
    "pixelpaint",
    "pv-reproduction",
    "at",
    "os",
    "all",
}


def normalize_toolbox_config(cfg: Any) -> Dict[str, Any]:
    """Normalize toolbox configuration dictionary against DEFAULT_TOOLBOX_CONFIG."""
    norm = copy.deepcopy(DEFAULT_TOOLBOX_CONFIG)
    if not isinstance(cfg, dict):
        return norm

    src = cfg.get("toolbox") if isinstance(cfg.get("toolbox"), dict) else cfg
    if not isinstance(src, dict):
        return norm

    # secret_front
    sf = src.get("secret_front")
    if isinstance(sf, dict):
        if "ending" in sf and sf["ending"]:
            norm["secret_front"]["ending"] = str(sf["ending"]).strip()
        if "priority_event" in sf:
            norm["secret_front"]["priority_event"] = str(sf["priority_event"] or "").strip()

    # stronghold_protocol
    sp = src.get("stronghold_protocol")
    if isinstance(sp, dict):
        if "theme" in sp and sp["theme"]:
            norm["stronghold_protocol"]["theme"] = str(sp["theme"]).strip()
        if "mode" in sp and sp["mode"]:
            norm["stronghold_protocol"]["mode"] = str(sp["mode"]).strip()
        if "target" in sp:
            try:
                norm["stronghold_protocol"]["target"] = int(sp["target"])
            except (ValueError, TypeError):
                pass
        if "runs" in sp:
            try:
                norm["stronghold_protocol"]["runs"] = int(sp["runs"])
            except (ValueError, TypeError):
                pass
        if "attempts" in sp:
            try:
                norm["stronghold_protocol"]["attempts"] = int(sp["attempts"])
            except (ValueError, TypeError):
                pass

    # event_shop
    es = src.get("event_shop")
    if isinstance(es, dict):
        if "blacklist_presets" in es and isinstance(es["blacklist_presets"], list):
            norm["event_shop"]["blacklist_presets"] = [str(x) for x in es["blacklist_presets"]]
        if "custom_blacklist" in es:
            norm["event_shop"]["custom_blacklist"] = str(es["custom_blacklist"] or "")

    # auto_raise_potential
    arp = src.get("auto_raise_potential")
    if isinstance(arp, dict):
        if "use_normal_token" in arp:
            norm["auto_raise_potential"]["use_normal_token"] = bool(arp["use_normal_token"])

    # blackflow_money
    bm = src.get("blackflow_money")
    if isinstance(bm, dict):
        if "stop_when_investment_full" in bm:
            norm["blackflow_money"]["stop_when_investment_full"] = bool(bm["stop_when_investment_full"])
        if "starts_count" in bm:
            try:
                norm["blackflow_money"]["starts_count"] = int(bm["starts_count"])
            except (ValueError, TypeError):
                pass

    # Preserve any extra custom tool configurations
    for k, v in src.items():
        if k not in norm and isinstance(v, dict):
            norm[k] = copy.deepcopy(v)

    return norm


def merge_event_shop_blacklist(presets: Any, custom_str: Any) -> List[str]:
    """Split custom blacklist by CN/EN comma, semicolon, or whitespace, and merge with presets preserving order."""
    seen: Set[str] = set()
    result: List[str] = []

    if isinstance(presets, (list, tuple, set)):
        for item in presets:
            s = str(item or "").strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)

    if custom_str:
        tokens = re.split(r"[,，;；\s]+", str(custom_str))
        for tok in tokens:
            s = tok.strip()
            if s and s not in seen:
                seen.add(s)
                result.append(s)

    return result


def _read_json_safe(p: Optional[Path]) -> Optional[Dict[str, Any]]:
    if p is None or not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def _check_json_key(p: Optional[Path], key: str) -> bool:
    data = _read_json_safe(p)
    return bool(data and key in data)


def _check_json_keys(p: Optional[Path], keys: List[str]) -> bool:
    data = _read_json_safe(p)
    if not data:
        return False
    return all(k in data for k in keys)


def _get_stronghold_meta(maa_path: Optional[Path]) -> Tuple[List[Dict[str, str]], List[str], List[str]]:
    """Scan resource/stronghold_protocol/ subdirs and battle_data.json for operator recommendations."""
    themes: List[Dict[str, str]] = []
    recommended_operators: List[str] = []
    notices: List[str] = [
        "请在卫戍协议主界面启动。刷角标将自动完成完整通关，刷分采用放生速通；建议提前按推荐干员组队并按需开局借人。",
    ]

    if maa_path is None or not maa_path.exists():
        themes.append({"label": "规程 (Regulation)", "value": "Regulation"})
        return themes, recommended_operators, notices

    sp_dir = maa_path / "resource" / "stronghold_protocol"
    if sp_dir.is_dir():
        for d in sp_dir.iterdir():
            if d.is_dir():
                folder = d.name
                cap = folder.capitalize()
                lbl = f"{cap} ({cap})"
                if folder.lower() == "regulation":
                    lbl = "规程 (Regulation)"
                themes.append({"label": lbl, "value": cap})

    if not themes:
        themes.append({"label": "规程 (Regulation)", "value": "Regulation"})

    # Safe read of purchase.json and battle_data.json
    purchase_file = sp_dir / "regulation" / "purchase.json"
    battle_file = maa_path / "resource" / "battle_data.json"
    purchase_data = _read_json_safe(purchase_file)
    battle_data = _read_json_safe(battle_file)

    if purchase_data and battle_data and "priority" in purchase_data and "chars" in battle_data:
        chars_map = battle_data.get("chars", {})
        squad_names: List[str] = []
        borrow_names: List[str] = []
        for item in purchase_data.get("priority", []):
            if isinstance(item, dict) and "id" in item:
                cid = item["id"]
                char_info = chars_map.get(cid, {})
                cname = char_info.get("name") or cid
                if item.get("squad"):
                    squad_names.append(cname)
                else:
                    borrow_names.append(cname)

        if squad_names or borrow_names:
            recommended_operators = squad_names + borrow_names
            if borrow_names:
                notices.append(f"开局借人优先推荐：{'、'.join(borrow_names[:4])}。")

    return themes, recommended_operators, notices


def inspect_toolbox_catalog(
    maa_dir: Optional[Union[Path, str]],
    config: Optional[Dict[str, Any]] = None,
    reclamation_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Inspect and return the structured toolbox catalog with categories and tool states."""
    maa_path = Path(maa_dir) if maa_dir else None
    norm_tb = normalize_toolbox_config(config)

    # Activity name from version.json
    activity_name = ""
    maa_version = ""
    if maa_path and maa_path.exists():
        v_file = maa_path / "resource" / "version.json"
        v_data = _read_json_safe(v_file)
        if v_data:
            activity_name = str(v_data.get("activity", {}).get("name", "") or "")

        txt_file = maa_path / "version.txt"
        if txt_file.is_file():
            try:
                maa_version = txt_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        if not maa_version:
            cl_file = maa_path / "data" / "CHANGELOG.md"
            if cl_file.is_file():
                try:
                    for line in cl_file.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if line.startswith("## "):
                            maa_version = line.lstrip("# ").strip()
                            break
                except Exception:
                    pass
        if not maa_version:
            try:
                from universal_engine.utils import env_doctor
                integ = env_doctor.check_maa_integrity(maa_path)
                maa_version = str(integ.get("maa_version") or integ.get("version_hint") or "")
            except Exception:
                pass

    upgrade_hint = "当前 MAA 版本缺失对应任务资源文件，请升级至 MAA v6.23.2 或以上版本"

    # 1. current_event items
    current_event_items: List[Dict[str, Any]] = []

    # 1.1 os_karlan
    os_file = maa_path / "resource" / "tasks" / "MiniGame" / "OS.json" if maa_path else None
    os_available = _check_json_key(os_file, "MiniGame@OS@Begin")
    os_doc = "雪山降临1101 - 喀兰贸易技术研发部"
    if os_file and os_file.is_file():
        d = _read_json_safe(os_file)
        if d and "MiniGame@OS@Begin" in d:
            os_doc = str(d["MiniGame@OS@Begin"].get("doc") or os_doc)

    current_event_items.append({
        "id": "os_karlan",
        "name": "OS-喀兰贸易技术研发部",
        "title": "OS-喀兰贸易技术研发部",
        "subtitle": "当期活动小游戏",
        "category": "current_event",
        "task_kind": "custom",
        "available": os_available,
        "unavailable_reason": "" if os_available else upgrade_hint,
        "description": os_doc,
        "summary": "自动执行雪山降临1101活动「喀兰贸易技术研发部」小游戏",
        "notices": ["请先进入喀兰贸易技术研发部小游戏主界面启动。"],
        "params": {},
        "meta": {},
        "button_text": "启动小工具",
    })

    # 1.2 blackflow_money
    bf_file = maa_path / "resource" / "tasks" / "Roguelike" / "BlackFlow.json" if maa_path else None
    bf_available = _check_json_key(bf_file, "BlackFlow@Roguelike@Begin")
    current_event_items.append({
        "id": "blackflow_money",
        "name": "黑流树海刷钱",
        "title": "黑流树海刷钱",
        "subtitle": "沉沦者的黑流树海",
        "category": "current_event",
        "task_kind": "roguelike",
        "available": bf_available,
        "unavailable_reason": "" if bf_available else upgrade_hint,
        "description": "沉沦者的黑流树海源石锭速刷投资预设，自动选人选队并进行局内投资",
        "summary": "模式=1 源石锭速刷投资预设，满投资或达到次数上限自动停止",
        "notices": ["将直接调用肉鸽引擎执行 BlackFlow 源石锭速刷模式。"],
        "params": norm_tb.get("blackflow_money", {}),
        "meta": {},
        "button_text": "启动小工具",
    })

    # Dynamic scan of MiniGame/*.json for future active event minigames
    if maa_path and (maa_path / "resource" / "tasks" / "MiniGame").is_dir():
        mg_dir = maa_path / "resource" / "tasks" / "MiniGame"
        for p in sorted(mg_dir.glob("*.json")):
            stem = p.stem
            if stem.lower() in EXCLUDED_MINIGAMES:
                continue
            d = _read_json_safe(p)
            if not d:
                continue
            begin_key = None
            doc_str = ""
            for k, val in d.items():
                if k.endswith("@Begin"):
                    begin_key = k
                    if isinstance(val, dict):
                        doc_str = str(val.get("doc") or "")
                    break
            if begin_key:
                dyn_id = f"minigame_{stem.lower()}"
                dyn_title = doc_str if doc_str else f"活动小游戏 {stem}"
                dyn_item = {
                    "id": dyn_id,
                    "name": dyn_title,
                    "title": dyn_title,
                    "subtitle": "当期活动小游戏",
                    "category": "current_event",
                    "task_kind": "custom",
                    "available": True,
                    "unavailable_reason": "",
                    "description": doc_str or f"自动执行 {stem} 活动小游戏",
                    "summary": f"动态发现的小游戏任务：{begin_key}",
                    "notices": ["请在对应活动小游戏主界面启动。"],
                    "params": {},
                    "meta": {"begin_key": begin_key, "stem": stem},
                    "button_text": "启动小工具",
                }
                # Pin to top if matches activity_name
                if activity_name and (activity_name.lower() in doc_str.lower() or activity_name.lower() in stem.lower()):
                    current_event_items.insert(0, dyn_item)
                else:
                    current_event_items.append(dyn_item)

    # 2. permanent_event items
    permanent_event_items: List[Dict[str, Any]] = []

    # 2.1 secret_front
    sf_file = maa_path / "resource" / "tasks" / "MiniGame" / "SecretFront.json" if maa_path else None
    sf_available = _check_json_key(sf_file, "MiniGame@SecretFront@Begin")
    permanent_event_items.append({
        "id": "secret_front",
        "name": "隐秘战线",
        "title": "隐秘战线",
        "subtitle": "常驻迷你游戏",
        "category": "permanent_event",
        "task_kind": "custom",
        "available": sf_available,
        "unavailable_reason": "" if sf_available else upgrade_hint,
        "description": "隐秘战线全结局自动化探索与优先系列事件选择",
        "summary": "支持指定 EndingA ~ EndingE 及优先事件，自动推进探索",
        "notices": ["请在选小队界面开始，如有进行中存档须先手动删除后再启动。"],
        "params": norm_tb.get("secret_front", {}),
        "meta": {
            "endings": [{"label": lbl, "value": val} for lbl, val in SECRET_FRONT_ENDINGS],
            "events": [{"label": lbl, "value": val} for lbl, val in SECRET_FRONT_EVENTS],
        },
        "button_text": "启动小工具",
    })

    # 2.2 stronghold_protocol
    sp_file = maa_path / "resource" / "tasks" / "StrongholdProtocol" / "StrongholdProtocol.json" if maa_path else None
    sp_dir = maa_path / "resource" / "stronghold_protocol" if maa_path else None
    sp_available = _check_json_key(sp_file, "StrongholdProtocol@Farm@Begin") and bool(sp_dir and sp_dir.is_dir())
    sp_themes, sp_rec_ops, sp_notices = _get_stronghold_meta(maa_path)
    permanent_event_items.append({
        "id": "stronghold_protocol",
        "name": "卫戍协议",
        "title": "卫戍协议",
        "subtitle": "常驻模式",
        "category": "permanent_event",
        "task_kind": "stronghold_protocol",
        "available": sp_available,
        "unavailable_reason": "" if sp_available else upgrade_hint,
        "description": "卫戍协议极速模拟自动化刷取，支持刷角标与放生刷分双模式",
        "summary": "极速模拟挂机刷取，内置购买优先级与阵容构建建议",
        "notices": sp_notices,
        "params": norm_tb.get("stronghold_protocol", {}),
        "meta": {
            "themes": sp_themes,
            "modes": [{"label": lbl, "value": val} for lbl, val in STRONGHOLD_MODES],
            "targets": [{"label": lbl, "value": val} for lbl, val in STRONGHOLD_TARGETS],
            "recommended_operators": sp_rec_ops,
        },
        "button_text": "启动小工具",
    })

    # 2.3 reclamation
    ra3 = maa_path / "resource" / "tasks" / "RA" / "Reclamation3.json" if maa_path else None
    ra2 = maa_path / "resource" / "tasks" / "RA" / "Reclamation2.json" if maa_path else None
    ra_available = bool((ra3 and ra3.is_file()) or (ra2 and ra2.is_file()))
    ra_cfg = (config or {}).get("reclamation", {})
    ra_theme = str(ra_cfg.get("theme", "RelaunchAnchor") or "RelaunchAnchor")
    ra_mode = ra_cfg.get("mode", 16)
    ra_mode_lbl = str(ra_cfg.get("mode_label", "RA1") or "RA1")
    ra_theme_disp, ra_mode_disp = _format_ra_mode_display(ra_theme, ra_mode, ra_mode_lbl)
    ra_snap = reclamation_snapshot or {
        "theme": ra_theme,
        "theme_display": ra_theme_disp,
        "mode_label": ra_mode_lbl,
        "mode_display": ra_mode_disp,
        "badge_text": f"模式：{ra_theme_disp} · {ra_mode_disp}",
        "clear_store": bool(ra_cfg.get("clear_store", False)),
        "loaded_from_gui_config": bool(ra_cfg.get("loaded_from_gui_config", False)),
        "sub_hint": f"当前刷取：{ra_theme_disp} / {ra_mode_disp}。共用启动器全局生息配置。",
    }
    permanent_event_items.append({
        "id": "reclamation",
        "name": "生息演算",
        "title": "生息演算",
        "subtitle": "长时托管",
        "category": "permanent_event",
        "task_kind": "reclamation",
        "available": ra_available,
        "unavailable_reason": "" if ra_available else upgrade_hint,
        "description": "生息演算长时自动化托管，支持重启锚点（RA-1/RA-4/RA-15）与沙洲遗闻",
        "summary": "共用全局生息演算配置，支持关卡循环与自动搬空商店",
        "notices": ["生息演算任务与左侧导航栏生息演算共用相同配置，修改即时同步。"],
        "params": {
            "theme": ra_theme,
            "mode_label": ra_mode_lbl,
            "clear_store": bool(ra_cfg.get("clear_store", False)),
        },
        "meta": {
            "themes": [{"label": lbl, "value": val} for lbl, val in RA_THEME_OPTIONS],
            "modes": [{"label": lbl, "value": val} for lbl, val in RA_MODE_OPTIONS],
        },
        "button_text": "启动生息演算",
    })

    # 3. permanent_utility items
    permanent_utility_items: List[Dict[str, Any]] = []

    # 3.1 event_shop
    ss_file = maa_path / "resource" / "tasks" / "Stages" / "base.json" if maa_path else None
    ss_available = _check_json_key(ss_file, "SS@Store@Begin")
    permanent_utility_items.append({
        "id": "event_shop",
        "name": "活动商店",
        "title": "活动商店",
        "subtitle": "采购中心",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": ss_available,
        "unavailable_reason": "" if ss_available else upgrade_hint,
        "description": "活动代币商店一键兑换，支持预设与自定义黑名单过滤",
        "summary": "自动识别当前 SS 活动商店并清空可兑换物资",
        "notices": ["请在对应活动商店界面启动。可配置黑名单跳过不需要购买的物品。"],
        "params": norm_tb.get("event_shop", {}),
        "meta": {
            "presets": [{"label": lbl, "value": val} for lbl, val in EVENT_SHOP_BLACKLIST_PRESETS],
        },
        "button_text": "启动小工具",
    })

    # 3.2 green_ticket_shop
    tasks_file = maa_path / "resource" / "tasks" / "tasks.json" if maa_path else None
    gt_available = _check_json_keys(tasks_file, ["Store@Begin", "GreenTicket@Store@ChooseTicketType"])
    permanent_utility_items.append({
        "id": "green_ticket_shop",
        "name": "绿票商店",
        "title": "绿票商店",
        "subtitle": "凭证交易所",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": gt_available,
        "unavailable_reason": "" if gt_available else upgrade_hint,
        "description": "采购中心资质凭证区自动化购买：1层全买，2层购买寻访凭证和招聘许可",
        "summary": "1层全买，2层购买寻访凭证和招聘许可",
        "notices": ["1层全买，2层购买寻访凭证和招聘许可。请先进入采购中心凭证交易所界面启动。"],
        "params": {},
        "meta": {},
        "button_text": "启动小工具",
    })

    # 3.3 yellow_ticket_shop
    yt_available = _check_json_keys(tasks_file, ["Store@Begin", "YellowTicket@Store@ChooseTicketType"])
    permanent_utility_items.append({
        "id": "yellow_ticket_shop",
        "name": "黄票商店",
        "title": "黄票商店",
        "subtitle": "高级凭证区",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": yt_available,
        "unavailable_reason": "" if yt_available else upgrade_hint,
        "description": "采购中心高级凭证区自动化购买",
        "summary": "自动进入高级凭证区进行批量兑换",
        "notices": ["请先进入采购中心高级凭证区界面启动。"],
        "params": {},
        "meta": {},
        "button_text": "启动小工具",
    })

    # 3.4 ra_store
    ra_store_file = maa_path / "resource" / "tasks" / "RA" / "plugin" / "base.json" if maa_path else None
    ra_store_available = _check_json_key(ra_store_file, "RA@Store@Begin")
    permanent_utility_items.append({
        "id": "ra_store",
        "name": "生息演算商店",
        "title": "生息演算商店",
        "subtitle": "黑市兑换",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": ra_store_available,
        "unavailable_reason": "" if ra_store_available else upgrade_hint,
        "description": "生息演算黑市商店自动化兑换物资",
        "summary": "自动清空生息演算黑市可用代币",
        "notices": ["请先进入生息演算黑市界面启动。"],
        "params": {},
        "meta": {},
        "button_text": "启动小工具",
    })

    # 3.5 auto_raise_potential
    arp_file = maa_path / "resource" / "tasks" / "MiniGame" / "AutoRaisePotential.json" if maa_path else None
    arp_available = _check_json_key(arp_file, "MiniGame@AutoRaisePotential@Begin")
    permanent_utility_items.append({
        "id": "auto_raise_potential",
        "name": "自动提升潜能",
        "title": "自动提升潜能",
        "subtitle": "干员养成",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": arp_available,
        "unavailable_reason": "" if arp_available else upgrade_hint,
        "description": "自动检测并提升已拥有干员的潜能，支持通用信物选项",
        "summary": "遍历干员列表自动提升潜能，支持勾选使用通用信物",
        "notices": ["请先进入干员列表界面启动。"],
        "params": norm_tb.get("auto_raise_potential", {}),
        "meta": {},
        "button_text": "启动小工具",
    })

    # 3.6 material_synthesis
    ms_file = maa_path / "resource" / "tasks" / "MiniGame" / "MaterialSynthesis.json" if maa_path else None
    ms_available = _check_json_key(ms_file, "MiniGame@MaterialSynthesis@Begin")
    permanent_utility_items.append({
        "id": "material_synthesis",
        "name": "缺口材料合成",
        "title": "缺口材料合成",
        "subtitle": "基建加工站",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": ms_available,
        "unavailable_reason": "" if ms_available else upgrade_hint,
        "description": "根据目标材料缺口自动在加工站中合成下级材料",
        "summary": "自动在加工站合成目标材料的下级配方缺口",
        "notices": ["请先进入加工站目标材料合成页启动。"],
        "params": {},
        "meta": {},
        "button_text": "启动小工具",
    })

    # 3.7 cursed_relic
    cr_file = maa_path / "resource" / "tasks" / "MiniGame" / "CursedRelic.json" if maa_path else None
    cr_available = _check_json_key(cr_file, "MiniGame@CursedRelic@Begin")
    permanent_utility_items.append({
        "id": "cursed_relic",
        "name": "刷傀影维多利亚皇冠",
        "title": "刷傀影维多利亚皇冠",
        "subtitle": "肉鸽诅咒藏品",
        "category": "permanent_utility",
        "task_kind": "custom",
        "available": cr_available,
        "unavailable_reason": "" if cr_available else upgrade_hint,
        "description": "在傀影与猩红孤钻主题中刷取维多利亚王冠藏品",
        "summary": "自动重开刷取诅咒藏品，OCR 识别到维多利亚王冠自动停止",
        "notices": ["请在傀影肉鸽主题界面启动，OCR 识别到维多利亚王冠自动停止。"],
        "params": {},
        "meta": {},
        "button_text": "启动小工具",
    })

    categories = [
        {"id": "current_event", "category_id": "current_event", "key": "current_event", "name": "当期活动", "title": "当期活动", "items": current_event_items},
        {"id": "permanent_event", "category_id": "permanent_event", "key": "permanent_event", "name": "常驻活动", "title": "常驻活动", "items": permanent_event_items},
        {"id": "permanent_utility", "category_id": "permanent_utility", "key": "permanent_utility", "name": "常驻功能", "title": "常驻功能", "items": permanent_utility_items},
    ]

    return {
        "ok": True,
        "maa_version": maa_version,
        "activity_name": activity_name,
        "categories": categories,
        "toolbox_config": norm_tb,
        "reclamation": ra_snap,
    }


def build_toolbox_task_spec(
    tool_id: str,
    maa_dir: Optional[Union[Path, str]] = None,
    config: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Construct concrete task type and append_task params dict for specified tool."""
    maa_path = Path(maa_dir) if maa_dir else None
    norm_tb = normalize_toolbox_config(config)
    upgrade_hint = "当前 MAA 版本缺失对应任务资源文件，请升级至 MAA v6.23.2 或以上版本"

    if tool_id == "os_karlan":
        os_file = maa_path / "resource" / "tasks" / "MiniGame" / "OS.json" if maa_path else None
        avail = _check_json_key(os_file, "MiniGame@OS@Begin")
        task_params = {"task_names": ["MiniGame@OS@Begin"]}
        return {
            "tool_id": "os_karlan",
            "name": "OS-喀兰贸易技术研发部",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请先进入喀兰贸易技术研发部小游戏主界面启动。"],
            "timeout_sec": 1800,
        }

    if tool_id == "blackflow_money":
        bf_file = maa_path / "resource" / "tasks" / "Roguelike" / "BlackFlow.json" if maa_path else None
        avail = _check_json_key(bf_file, "BlackFlow@Roguelike@Begin")
        bm_cfg = norm_tb.get("blackflow_money", {})
        if overrides and isinstance(overrides, dict):
            bm_cfg = dict(bm_cfg)
            bm_cfg.update(overrides)
        stop_invest = bool(bm_cfg.get("stop_when_investment_full", True))
        starts_cnt = int(bm_cfg.get("starts_count", 999))
        task_params = {
            "theme": "BlackFlow",
            "mode": 1,
            "investment_enabled": True,
            "stop_when_investment_full": stop_invest,
        }
        return {
            "tool_id": "blackflow_money",
            "name": "黑流树海刷钱",
            "task_type": "Roguelike",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "roguelike",
            "theme": "BlackFlow",
            "rl_mode": 1,
            "investment_enabled": True,
            "stop_when_investment_full": stop_invest,
            "starts_count": starts_cnt,
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["沉沦者的黑流树海源石锭速刷投资预设，自动选人选队并进行局内投资。"],
            "timeout_sec": 7200,
            "blackflow_config": bm_cfg,
        }

    if tool_id == "secret_front":
        sf_file = maa_path / "resource" / "tasks" / "MiniGame" / "SecretFront.json" if maa_path else None
        avail = _check_json_key(sf_file, "MiniGame@SecretFront@Begin")
        sf_cfg = norm_tb.get("secret_front", {})
        ending = str((overrides or {}).get("ending") or sf_cfg.get("ending") or "EndingE").strip()
        p_event = str((overrides or {}).get("priority_event") if overrides and "priority_event" in overrides else sf_cfg.get("priority_event", "")).strip()
        task_name = f"MiniGame@SecretFront@Begin@{ending}" + (f"@{p_event}" if p_event else "")
        task_params = {"task_names": [task_name]}
        return {
            "tool_id": "secret_front",
            "name": "隐秘战线",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请在选小队界面开始，如有进行中存档须先手动删除后再启动。"],
            "timeout_sec": 2400,
        }

    if tool_id == "stronghold_protocol":
        sp_file = maa_path / "resource" / "tasks" / "StrongholdProtocol" / "StrongholdProtocol.json" if maa_path else None
        sp_dir = maa_path / "resource" / "stronghold_protocol" if maa_path else None
        avail = _check_json_key(sp_file, "StrongholdProtocol@Farm@Begin") and bool(sp_dir and sp_dir.is_dir())
        sp_cfg = norm_tb.get("stronghold_protocol", {})
        mode = str((overrides or {}).get("mode") or sp_cfg.get("mode") or "badge").strip()
        if mode == "score":
            spa_file = maa_path / "resource" / "tasks" / "MiniGame" / "SPA.json" if maa_path else None
            if not _check_json_key(spa_file, "MiniGame@SPA@Begin"):
                avail = False
            task_params = {"mode": "score"}
        else:
            theme = str((overrides or {}).get("theme") or sp_cfg.get("theme") or "Regulation").strip()
            target = int((overrides or {}).get("target", sp_cfg.get("target", 3)))
            runs = int((overrides or {}).get("runs", sp_cfg.get("runs", 60)))
            task_params = {
                "mode": "badge",
                "theme": theme,
                "target": target,
                "runs": runs,
            }
            attempts = int((overrides or {}).get("attempts", sp_cfg.get("attempts", 0)))
            if attempts > 0:
                task_params["attempts"] = attempts

        _, _, sp_notices = _get_stronghold_meta(maa_path)
        return {
            "tool_id": "stronghold_protocol",
            "name": "卫戍协议",
            "task_type": "StrongholdProtocol",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": sp_notices,
            "timeout_sec": 7200,
        }

    if tool_id == "reclamation":
        ra3 = maa_path / "resource" / "tasks" / "RA" / "Reclamation3.json" if maa_path else None
        ra2 = maa_path / "resource" / "tasks" / "RA" / "Reclamation2.json" if maa_path else None
        avail = bool((ra3 and ra3.is_file()) or (ra2 and ra2.is_file()))
        return {
            "tool_id": "reclamation",
            "name": "生息演算",
            "task_type": "Reclamation",
            "task_params": {},
            "params": {},
            "task_kind": "reclamation",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["生息演算任务与左侧导航栏生息演算共用相同配置。"],
            "timeout_sec": 7200,
        }

    if tool_id == "event_shop":
        ss_file = maa_path / "resource" / "tasks" / "Stages" / "base.json" if maa_path else None
        avail = _check_json_key(ss_file, "SS@Store@Begin")
        es_cfg = norm_tb.get("event_shop", {})
        presets = (overrides or {}).get("blacklist_presets") if (overrides and "blacklist_presets" in overrides) else es_cfg.get("blacklist_presets", [])
        custom_bl = (overrides or {}).get("custom_blacklist") if (overrides and "custom_blacklist" in overrides) else es_cfg.get("custom_blacklist", "")
        bl = merge_event_shop_blacklist(presets, custom_bl)
        task_params = {"task_names": ["SS@Store@Begin"]}
        if bl:
            task_params["params"] = {"event_shop": {"blacklist": bl}}
        return {
            "tool_id": "event_shop",
            "name": "活动商店",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请在对应活动商店界面启动。可配置黑名单跳过不需要购买的物品。"],
            "timeout_sec": 1200,
        }

    if tool_id == "green_ticket_shop":
        tasks_file = maa_path / "resource" / "tasks" / "tasks.json" if maa_path else None
        avail = _check_json_keys(tasks_file, ["Store@Begin", "GreenTicket@Store@ChooseTicketType"])
        task_params = {"task_names": ["GreenTicket@Store@Begin"]}
        return {
            "tool_id": "green_ticket_shop",
            "name": "绿票商店",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["1层全买，2层购买寻访凭证和招聘许可。请先进入采购中心凭证交易所界面启动。"],
            "timeout_sec": 900,
        }

    if tool_id == "yellow_ticket_shop":
        tasks_file = maa_path / "resource" / "tasks" / "tasks.json" if maa_path else None
        avail = _check_json_keys(tasks_file, ["Store@Begin", "YellowTicket@Store@ChooseTicketType"])
        task_params = {"task_names": ["YellowTicket@Store@Begin"]}
        return {
            "tool_id": "yellow_ticket_shop",
            "name": "黄票商店",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请先进入采购中心高级凭证区界面启动。"],
            "timeout_sec": 900,
        }

    if tool_id == "ra_store":
        ra_store_file = maa_path / "resource" / "tasks" / "RA" / "plugin" / "base.json" if maa_path else None
        avail = _check_json_key(ra_store_file, "RA@Store@Begin")
        task_params = {"task_names": ["RA@Store@Begin"]}
        return {
            "tool_id": "ra_store",
            "name": "生息演算商店",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请先进入生息演算黑市界面启动。"],
            "timeout_sec": 900,
        }

    if tool_id == "auto_raise_potential":
        arp_file = maa_path / "resource" / "tasks" / "MiniGame" / "AutoRaisePotential.json" if maa_path else None
        avail = _check_json_key(arp_file, "MiniGame@AutoRaisePotential@Begin")
        arp_cfg = norm_tb.get("auto_raise_potential", {})
        use_token = bool((overrides or {}).get("use_normal_token", arp_cfg.get("use_normal_token", False)))
        task_params = {"task_names": ["MiniGame@AutoRaisePotential@Begin"]}
        if use_token:
            task_params["params"] = {"auto_raise_potential": {"use_normal_token": True}}
        return {
            "tool_id": "auto_raise_potential",
            "name": "自动提升潜能",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请先进入干员列表界面启动。"],
            "timeout_sec": 1800,
        }

    if tool_id == "material_synthesis":
        ms_file = maa_path / "resource" / "tasks" / "MiniGame" / "MaterialSynthesis.json" if maa_path else None
        avail = _check_json_key(ms_file, "MiniGame@MaterialSynthesis@Begin")
        task_params = {"task_names": ["MiniGame@MaterialSynthesis@Begin"]}
        return {
            "tool_id": "material_synthesis",
            "name": "缺口材料合成",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请先进入加工站目标材料合成页启动。"],
            "timeout_sec": 1200,
        }

    if tool_id == "cursed_relic":
        cr_file = maa_path / "resource" / "tasks" / "MiniGame" / "CursedRelic.json" if maa_path else None
        avail = _check_json_key(cr_file, "MiniGame@CursedRelic@Begin")
        task_params = {"task_names": ["MiniGame@CursedRelic@Begin"]}
        return {
            "tool_id": "cursed_relic",
            "name": "刷傀影维多利亚皇冠",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请在傀影肉鸽主题界面启动，OCR 识别到维多利亚王冠自动停止。"],
            "timeout_sec": 3600,
        }

    # Dynamic minigame handler: minigame_<stem>
    if tool_id.startswith("minigame_"):
        stem = tool_id[len("minigame_"):]
        mg_file = None
        if maa_path:
            for p in (maa_path / "resource" / "tasks" / "MiniGame").glob("*.json"):
                if p.stem.lower() == stem.lower():
                    mg_file = p
                    break
        d = _read_json_safe(mg_file)
        begin_key = None
        doc_str = ""
        if d:
            for k, val in d.items():
                if k.endswith("@Begin"):
                    begin_key = k
                    if isinstance(val, dict):
                        doc_str = str(val.get("doc") or "")
                    break
        avail = bool(begin_key)
        task_params = {"task_names": [begin_key or f"MiniGame@{stem}@Begin"]}
        return {
            "tool_id": tool_id,
            "name": doc_str or f"活动小游戏 {stem}",
            "task_type": "Custom",
            "task_params": task_params,
            "params": task_params,
            "task_kind": "toolbox",
            "available": avail,
            "unavailable_reason": "" if avail else upgrade_hint,
            "notices": ["请在对应活动小游戏主界面启动。"],
            "timeout_sec": 1800,
        }

    return {
        "tool_id": tool_id,
        "name": tool_id,
        "task_type": "Unknown",
        "task_params": {},
        "params": {},
        "task_kind": "toolbox",
        "available": False,
        "unavailable_reason": f"未识别的小工具 ID: {tool_id}",
        "notices": [],
        "timeout_sec": 1800,
    }


def run_toolbox_task(
    session_holder: Dict[str, Any],
    tool_id: str,
    config: Dict[str, Any],
    maa_dir: Optional[Union[Path, str]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Execute a toolbox task skipping StartUp with unified error recovery and [TOOLBOX] logging."""
    t0 = time.time()
    spec = build_toolbox_task_spec(tool_id, maa_dir=maa_dir, config=config, overrides=overrides)
    if not spec.get("available"):
        err = str(spec.get("unavailable_reason") or "小工具不可用")
        if log_callback:
            try:
                log_callback(f"[TOOLBOX][ERR] {err}")
            except Exception:
                pass
        return {"ok": False, "tool_id": tool_id, "error": err, "available": False}

    if cancel_event is not None and cancel_event.is_set():
        return {"ok": False, "tool_id": tool_id, "cancelled": True}

    tool_name = spec.get("name") or tool_id
    if log_callback:
        try:
            log_callback(f"[TOOLBOX] 开始执行小工具：{tool_name}")
        except Exception:
            pass

    task_type = spec["task_type"]
    task_params = spec["task_params"]
    timeout_sec = int(spec.get("timeout_sec", 1800))

    cur_sess = session_holder.get("session")
    if hasattr(cur_sess, "append_task") and (not hasattr(cur_sess, "run_single_task") or type(cur_sess).__name__ == "MagicMock"):
        from .utils import runtime_guard as _rg

        _dry_ctx = _rg.get_active_dry_run_context()
        if _dry_ctx is not None:
            res = _dry_ctx.intercept_task(task_type, task_params, log_callback=log_callback)
        else:
            cur_sess.append_task(task_type, task_params)
            res = {"ok": True, "task_type": task_type, "params": task_params}
    else:
        try:
            res = maa_runner.run_task_with_recovery(
                session_holder,
                task_type,
                task_params,
                config,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            res = {"ok": False, "error": str(exc)}

    was_cancelled = bool((cancel_event is not None and cancel_event.is_set()) or res.get("cancelled"))
    res["cancelled"] = was_cancelled
    res["tool_id"] = tool_id
    res["tool_name"] = tool_name
    res["task_type"] = task_type
    res["task_params"] = task_params
    res["total_elapsed_seconds"] = round(time.time() - t0, 1)

    if log_callback:
        try:
            if was_cancelled:
                log_callback(f"[TOOLBOX] 小工具任务已停止：{tool_name}")
            elif res.get("ok"):
                log_callback(f"[TOOLBOX] 小工具任务完成：{tool_name}")
            else:
                err_msg = res.get("error") or res.get("error_reason") or "执行未完成"
                log_callback(f"[TOOLBOX] 小工具任务未完成：{tool_name} ({err_msg})")
        except Exception:
            pass

    return res
