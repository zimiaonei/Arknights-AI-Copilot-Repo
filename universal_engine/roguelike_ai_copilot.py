"""Hybrid Roguelike AI Strategic Copilot (out-of-battle decisions) & Post-Settlement Reward/Tech-Tree Automation.

Architecture:
- In-battle combat is handled 100% by MAA Core (avoiding LLM latency / leaked enemies).
- Out-of-battle critical decisions are handled by ``RoguelikeAICopilot`` across 4 decision points:
  1. Operator recruitment draft (``decide_recruitment``)
  2. Map branch & node selection (``decide_map_node``)
  3. Encounter events & collectibles/ideas selection (``decide_encounter_or_relic``)
  4. Pre-boss strategy: fetch & adapt PRTS copilot first, or hand back to MAA native battle (``decide_boss_strategy``)
- Supports both ``builtin_api`` (OpenAI-compatible multimodal/text) and ``agent_collab``
  (zero-key desktop Agent via ``agent_task.json`` with configurable timeout auto-fallback to rules).
- Multi-theme adaptive post-settlement reward claiming (e.g. BlackFlow "源流堆栈" & "本月委托栏")
  and tech-tree auto-upgrade (e.g. BlackFlow "生命游戏") via ``stage_pilot`` ADB vision/OCR.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PIL import Image

try:
    import numpy as np  # type: ignore
except ImportError:  # pragma: no cover
    np = None  # type: ignore

from . import ai_strategist, auto_growth, stage_pilot
from .utils.env_doctor import get_base_dir

# Multi-theme lobby & post-settlement UI anchor profiles (1280x720 normalized coordinates)
THEME_UI_PROFILES: Dict[str, Dict[str, Any]] = {
    "BlackFlow": {
        "display_name": "沉沦者的黑流树海",
        "lobby_keywords": ["开始探索", "源流堆栈", "生命游戏", "本月委托"],
        "level_reward": {
            "name": "源流堆栈",
            "keywords": ["源流堆栈", "源流", "堆栈"],
            "roi": (880, 0, 400, 140),
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": ["本月委托栏", "本月委托", "委托栏", "月度委托"],
            "roi": (900, 130, 380, 360),
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "生命游戏",
            "keywords": ["生命游戏", "生命"],
            "roi": (0, 450, 440, 270),
            "fallback_xy": (185, 610),
        },
    },
    "JieGarden": {
        "display_name": "岁的界园志异",
        "lobby_keywords": ["开始探索", "岁兽残识", "本月委托"],
        "level_reward": {
            "name": "奇闻奖励",
            "keywords": ["岁兽残识", "奇闻", "等级奖励", "等级"],
            "roi": (880, 0, 400, 140),
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托",
            "keywords": ["本月委托栏", "本月委托", "月度委托", "委托"],
            "roi": (900, 130, 380, 360),
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "演武/铸塑",
            "keywords": ["铸塑", "演武", "认知", "强化"],
            "roi": (0, 450, 440, 270),
            "fallback_xy": (185, 610),
        },
    },
    "Sarkaz": {
        "display_name": "萨卡兹的无终奇语",
        "lobby_keywords": ["开始探索", "认知塑造", "古今学识"],
        "level_reward": {
            "name": "古今学识",
            "keywords": ["古今学识", "异闻", "奇谭", "等级"],
            "roi": (880, 0, 400, 140),
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托",
            "keywords": ["本月委托栏", "本月委托", "月度委托", "委托"],
            "roi": (900, 130, 380, 360),
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "认知塑造",
            "keywords": ["认知塑造", "认知"],
            "roi": (0, 450, 440, 270),
            "fallback_xy": (185, 610),
        },
    },
    "Sami": {
        "display_name": "探索者的银凇止境",
        "lobby_keywords": ["开始探索", "认识塑造", "生态标本"],
        "level_reward": {
            "name": "探索者记录",
            "keywords": ["探索者记录", "生态标本", "等级"],
            "roi": (880, 0, 400, 140),
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托",
            "keywords": ["本月委托栏", "本月委托", "月度委托", "委托"],
            "roi": (900, 130, 380, 360),
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "认识塑造",
            "keywords": ["认识塑造", "认知塑造"],
            "roi": (0, 450, 440, 270),
            "fallback_xy": (185, 610),
        },
    },
    "Mizuki": {
        "display_name": "水月与深蓝之树",
        "lobby_keywords": ["开始探索", "心胜于物", "生态图鉴"],
        "level_reward": {
            "name": "生态图鉴",
            "keywords": ["生态图鉴", "奖励", "等级"],
            "roi": (880, 0, 400, 140),
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托",
            "keywords": ["本月委托栏", "本月委托", "月度委托", "委托"],
            "roi": (900, 130, 380, 360),
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "心胜于物",
            "keywords": ["心胜于物"],
            "roi": (0, 450, 440, 270),
            "fallback_xy": (185, 610),
        },
    },
    "Phantom": {
        "display_name": "傀影与猩红孤钻",
        "lobby_keywords": ["开始探索", "幕后筹备", "猩红剧团"],
        "level_reward": {
            "name": "剧团奖励",
            "keywords": ["剧团奖励", "剧目", "等级", "奖励"],
            "roi": (880, 0, 400, 140),
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托",
            "keywords": ["本月委托栏", "本月委托", "月度委托", "委托"],
            "roi": (900, 130, 380, 360),
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "幕后筹备",
            "keywords": ["幕后筹备"],
            "roi": (0, 450, 440, 270),
            "fallback_xy": (185, 610),
        },
    },
}

DEFAULT_TECH_TREE_PRIORITY: Tuple[str, ...] = (
    "hope_dp_recruit",
    "combat_stats",
    "shop_refresh",
)

TECH_TREE_CATEGORY_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "hope_dp_recruit": (
        "开局希望",
        "初始希望",
        "希望上限",
        "额外希望",
        "希望",
        "初始费用",
        "初始部署费用",
        "部署费用",
        "费用",
        "招募折扣",
        "招募消耗",
        "招募所需",
        "进阶消耗",
        "精二消耗",
        "临时招募",
        "希望消耗",
        "分队",
    ),
    "combat_stats": (
        "攻击力",
        "生命上限",
        "防御力",
        "攻击速度",
        "攻速",
        "技力",
        "目标生命",
        "生命值",
        "护盾",
        "同时部署",
        "编队上限",
        "部署上限",
        "战斗属性",
        "法术抗性",
        "伤害",
        "再部署",
        "物理",
        "法术",
    ),
    "shop_refresh": (
        "商店刷新",
        "刷新商店",
        "刷新次数",
        "刷新",
        "投资",
        "源石锭",
        "折扣",
        "商品",
        "藏品",
        "收藏品",
        "零件箱",
        "行动力",
        "背包",
        "售卖",
    ),
}

# Tactical role buckets for Roguelike draft balance
_BLOCKER_ROLES = frozenset({"guard_lane", "defender"})
_ANTIAIR_ROLES = frozenset({"sniper", "caster"})
_MEDIC_ROLES = frozenset({"medic"})

# High-value Roguelike carry operators (supplemented by theme recruitment.json when available)
_META_ROGUELIKE_CARRIES: Dict[str, int] = {
    "维什戴尔": 260,
    "新约能天使": 240,
    "机械师": 230,
    "电弧": 210,
    "酒神": 210,
    "逻各斯": 200,
    "纯烬艾雅法拉": 195,
    "伊内丝": 190,
    "玛恩纳": 185,
    "缄默德克萨斯": 180,
    "麒麟R夜刀": 175,
    "提丰": 170,
    "棘刺": 165,
    "山": 160,
    "塞雷娅": 160,
    "黍": 160,
    "凯尔希": 155,
    "凯尔希·思衡托": 160,
    "澄闪": 155,
    "艾雅法拉": 150,
    "流明": 145,
    "夜莺": 140,
    "桃金娘": 135,
    "古米": 110,
    "克洛丝": 95,
    "芬": 90,
    "斑点": 90,
    "安赛尔": 85,
    "芙蓉": 80,
}

_DANGEROUS_EMERGENCY_KEYWORDS: Tuple[str, ...] = (
    "紧急",
    "险路",
    "失控",
    "死斗",
    "领主",
    "狂暴",
    "高危",
    "灾祸",
)

_REWARD_CLAIM_BUTTON_TEXTS: Tuple[str, ...] = (
    "一键领取",
    "全部领取",
    "领取全部",
    "可领取",
    "领取奖励",
    "领取",
)

_REWARD_ALREADY_CLAIMED_TEXTS: Tuple[str, ...] = (
    "已领取",
    "已全部领取",
    "全部已领",
    "已领完",
    "不可领取",
    "无法领取",
)

_REWARD_DISMISS_TEXTS: Tuple[str, ...] = (
    "获得物品",
    "获得奖励",
    "点击任意处继续",
    "点击空白处继续",
    "点击继续",
)

_TECH_UPGRADE_BUTTON_TEXTS: Tuple[str, ...] = (
    "激活",
    "解锁",
    "升级",
    "强化",
    "提升",
    "点亮",
    "确认激活",
    "确认解锁",
)

_TECH_UNAVAILABLE_TEXTS: Tuple[str, ...] = (
    "已激活",
    "已解锁",
    "已满级",
    "点数不足",
    "材料不足",
    "未满足",
    "前置未解锁",
    "无法升级",
)


def _emit(log_callback: Optional[Callable[[str], None]], msg: str) -> None:
    if log_callback:
        try:
            log_callback(msg)
            return
        except Exception:
            pass
    print(msg)


def _file_signature(path: Path) -> Tuple[int, str]:
    if not path.is_file():
        return (0, "")
    st = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return (st.st_mtime_ns, digest)


def _safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    if isinstance(val, bool) or val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def get_theme_ui_profile(theme: str) -> Dict[str, Any]:
    """Return UI landmark profile for a Roguelike theme, defaulting cleanly to BlackFlow."""
    key = str(theme or "BlackFlow").strip()
    for k, v in THEME_UI_PROFILES.items():
        if k.lower() == key.lower():
            return copy.deepcopy(v)
    return copy.deepcopy(THEME_UI_PROFILES["BlackFlow"])


@dataclass
class RoguelikeRunContext:
    """Mutable out-of-battle context tracked across a Roguelike run."""

    theme: str = "BlackFlow"
    floor: int = 1
    hp: int = 10
    max_hp: int = 10
    ingots: int = 0
    hope: int = 6
    action_points: int = 10
    current_team: List[str] = field(default_factory=list)
    relics: List[str] = field(default_factory=list)
    ideas: List[str] = field(default_factory=list)
    decisions_log: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "theme": self.theme,
            "floor": self.floor,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "ingots": self.ingots,
            "hope": self.hope,
            "action_points": self.action_points,
            "current_team": list(self.current_team),
            "relics": list(self.relics),
            "ideas": list(self.ideas),
        }


@dataclass
class RoguelikeDecision:
    """Structured output returned by any of the 4 AI strategic decision points."""

    decision_type: str
    selected_index: Optional[int]
    selected_value: Any
    action: str  # "select" | "give_up" | "prts_copilot" | "maa_native_battle" | "cancelled"
    source: str  # "builtin_api_vision" | "builtin_api_text" | "agent_collab" | "rule_engine" | "rule_timeout_fallback" | "cancelled"
    reason: str
    click_xy: Optional[Tuple[int, int]] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_type": self.decision_type,
            "selected_index": self.selected_index,
            "selected_value": self.selected_value,
            "action": self.action,
            "source": self.source,
            "reason": self.reason,
            "click_xy": list(self.click_xy) if self.click_xy else None,
            "extra": copy.deepcopy(self.extra),
        }


def load_theme_recruitment_priorities(maa_dir: Optional[Path], theme: str) -> Dict[str, int]:
    """Read MAA's theme-specific recruitment.json into {operator_name: recruit_priority}."""
    scores: Dict[str, int] = {}
    if maa_dir is None:
        return scores
    rec_file = Path(maa_dir) / "resource" / "roguelike" / theme / "recruitment.json"
    if not rec_file.is_file():
        return scores
    try:
        data = json.loads(rec_file.read_text(encoding="utf-8"))
        for grp in data.get("priority", []):
            for op in grp.get("opers", []):
                name = str(op.get("name", "") or "").strip()
                prio = _safe_int(op.get("recruit_priority"), 0) or 0
                if name and prio > scores.get(name, -10**9):
                    scores[name] = prio
    except Exception:
        pass
    return scores


def _summarize_team_roles(
    current_team: Sequence[str],
    role_map: Dict[str, str],
    owned_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    e2_count = 0
    for name in current_team:
        role = role_map.get(name, "guard_lane")
        counts[role] = counts.get(role, 0) + 1
        op_info = owned_map.get(name)
        if op_info and int(op_info.get("elite", 0)) >= 2:
            e2_count += 1
    blocker_count = sum(counts.get(r, 0) for r in _BLOCKER_ROLES)
    antiair_count = sum(counts.get(r, 0) for r in _ANTIAIR_ROLES)
    medic_count = sum(counts.get(r, 0) for r in _MEDIC_ROLES)
    return {
        "role_counts": counts,
        "blocker_count": blocker_count,
        "antiair_count": antiair_count,
        "medic_count": medic_count,
        "e2_count": e2_count,
        "team_size": len(current_team),
    }


def _normalize_candidate_list(candidates: Sequence[Any]) -> List[Dict[str, Any]]:
    norm: List[Dict[str, Any]] = []
    for idx, item in enumerate(candidates):
        if isinstance(item, str):
            norm.append({"index": idx, "name": item.strip(), "hope_cost": 0, "is_temp": False, "is_promote": False})
        elif isinstance(item, dict):
            d = dict(item)
            d.setdefault("index", idx)
            d["name"] = str(d.get("name", d.get("title", "")) or "").strip()
            d["hope_cost"] = int(d.get("hope_cost", d.get("cost", 0)) or 0)
            d["is_temp"] = bool(d.get("is_temp", False))
            d["is_promote"] = bool(d.get("is_promote", False))
            norm.append(d)
    return norm


def write_roguelike_agent_task(
    base_dir: Path,
    decision_type: str,
    theme: str,
    options: List[Dict[str, Any]],
    context: Dict[str, Any],
    rule_baseline: Dict[str, Any],
    screenshot_path: str = "",
    timeout_sec: float = 45.0,
    auto_fallback_on_timeout: bool = True,
) -> Tuple[Path, Path, Dict[str, Any]]:
    """Write <base_dir>/agent_task.json for desktop Agent collaboration on a Roguelike decision."""
    root = Path(base_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    task_file = root / "agent_task.json"
    response_file = root / "agent_decision.json"
    if response_file.is_file():
        try:
            response_file.unlink()
        except Exception:
            pass

    payload: Dict[str, Any] = {
        "task_id": uuid.uuid4().hex[:12],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "status": "waiting_for_agent",
        "task_type": "roguelike_decision",
        "decision_type": decision_type,
        "theme": theme,
        "screenshot_path": str(screenshot_path or ""),
        "my_box_path": str(root / "data" / "my_box.json"),
        "response_file": str(response_file),
        "timeout_sec": float(timeout_sec),
        "auto_fallback_on_timeout": bool(auto_fallback_on_timeout),
        "options": options,
        "context": context,
        "rule_baseline": rule_baseline,
        "selected_index": None,
        "selected_option": None,
        "agent_reason": "",
        "instructions_for_agent": (
            f"当前为《明日方舟》集成战略（{theme}）局外关键抉择点 [{decision_type}]。"
            "请结合 screenshot_path 截图、options 候选项与 context（当前编队/血量/希望/my_box练度），"
            "在 response_file 中写入 JSON {\"selected_index\": 0, \"reason\": \"...\"}，"
            "或直接修改本文件的 selected_index 并将 status 改为 \"resolved\"。"
            "若超时未响应且启用了超时兜底，系统将自动按 rule_baseline 规则选择，防止无人值守卡死。"
        ),
    }
    task_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return task_file, response_file, payload


def _update_task_status(task_file: Path, status: str, extra: Optional[Dict[str, Any]] = None) -> None:
    try:
        if task_file.is_file():
            data = json.loads(task_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["status"] = status
                data["updated_at"] = datetime.now().isoformat(timespec="seconds")
                if extra:
                    data.update(extra)
                task_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def wait_for_agent_decision(
    task_file: Path,
    response_file: Path,
    cancel_event: Optional[threading.Event] = None,
    manual_resume_event: Optional[threading.Event] = None,
    timeout_sec: float = 45.0,
    auto_fallback_on_timeout: bool = True,
    poll_interval: float = 0.4,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Poll agent_task.json & agent_decision.json until Agent responds, cancelled, or timed out."""
    initial_sig = _file_signature(task_file)
    t0 = time.time()

    while True:
        if cancel_event and cancel_event.is_set():
            _update_task_status(task_file, "cancelled")
            return "cancelled", None

        if manual_resume_event and manual_resume_event.is_set():
            manual_resume_event.clear()
            _update_task_status(task_file, "fallback_rule")
            return "fallback_rule", None

        # Check dedicated response_file first
        if response_file.is_file():
            try:
                resp_data = json.loads(response_file.read_text(encoding="utf-8"))
                if isinstance(resp_data, dict) and (
                    resp_data.get("selected_index") is not None
                    or resp_data.get("selected_option") is not None
                    or resp_data.get("action") is not None
                ):
                    _update_task_status(task_file, "applied", {"agent_response": resp_data})
                    return "agent_resolved", resp_data
            except Exception:
                pass

        # Check direct modification of agent_task.json
        cur_sig = _file_signature(task_file)
        if cur_sig != initial_sig and cur_sig[0] != 0:
            try:
                task_data = json.loads(task_file.read_text(encoding="utf-8"))
                if isinstance(task_data, dict):
                    st = str(task_data.get("status", "") or "").strip().lower()
                    if st in ("skip", "fallback", "fallback_rule"):
                        return "fallback_rule", None
                    if st == "resolved" or task_data.get("selected_index") is not None or task_data.get("selected_option") is not None:
                        _update_task_status(task_file, "applied")
                        return "agent_resolved", task_data
            except Exception:
                pass

        if timeout_sec > 0 and (time.time() - t0) >= timeout_sec:
            if auto_fallback_on_timeout:
                _update_task_status(task_file, "timeout_fallback")
                return "timeout_fallback", None
            # If auto_fallback is explicitly disabled, keep waiting unless cancelled

        time.sleep(max(0.02, poll_interval))


class RoguelikeAICopilot:
    """Strategic AI Copilot for Roguelike out-of-battle decisions (dual-mode: builtin_api + agent_collab)."""

    def __init__(
        self,
        config: Dict[str, Any],
        maa_dir: Optional[Path] = None,
        base_dir: Optional[Path] = None,
        my_box: Optional[Dict[str, Any]] = None,
        client: Optional[ai_strategist.OpenAICompatClient] = None,
        collab_handler: Optional[Callable[[Dict[str, Any]], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        manual_resume_event: Optional[threading.Event] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        poll_interval: float = 0.4,
    ):
        self.config = config or {}
        self.maa_dir = Path(maa_dir).resolve() if maa_dir is not None else None
        self.base_dir = Path(base_dir).resolve() if base_dir is not None else get_base_dir()
        self.collab_handler = collab_handler
        self.cancel_event = cancel_event
        self.manual_resume_event = manual_resume_event
        self.log_callback = log_callback
        self.poll_interval = poll_interval

        self.rl_cfg = self.config.get("roguelike", {}) if isinstance(self.config.get("roguelike"), dict) else {}
        self.ai_cfg = self.config.get("ai", {}) if isinstance(self.config.get("ai"), dict) else {}
        self.ai_mode = str(self.ai_cfg.get("mode", "agent_collab") or "agent_collab").strip().lower()
        self.vision_enabled = bool(self.rl_cfg.get("ai_vision_enabled", True))
        self.decision_timeout_sec = float(self.rl_cfg.get("ai_decision_timeout_sec", 45.0))
        self.auto_fallback_on_timeout = bool(self.rl_cfg.get("ai_auto_fallback_on_timeout", True))
        self.boss_prts_assist = bool(self.rl_cfg.get("boss_prts_assist", True))

        self.client = client if client is not None else ai_strategist.OpenAICompatClient.from_config(self.config)
        self.role_map = ai_strategist.load_recruitment_roles(self.maa_dir)
        self._my_box_cache = my_box
        self._theme_recruit_cache: Dict[str, Dict[str, int]] = {}
        self.decision_history: List[Dict[str, Any]] = []

    def get_my_box(self) -> Dict[str, Any]:
        if self._my_box_cache is not None:
            return self._my_box_cache
        if self.maa_dir is not None:
            self._my_box_cache = auto_growth.sync_my_box(self.maa_dir, base_dir=self.base_dir, persist=False)
        else:
            box_file = self.base_dir / "data" / "my_box.json"
            if box_file.is_file():
                try:
                    self._my_box_cache = json.loads(box_file.read_text(encoding="utf-8"))
                except Exception:
                    self._my_box_cache = {"all_owned": {}, "e2_operators": [], "e2_count": 0}
            else:
                self._my_box_cache = {"all_owned": {}, "e2_operators": [], "e2_count": 0}
        return self._my_box_cache

    def _get_theme_recruit_scores(self, theme: str) -> Dict[str, int]:
        if theme not in self._theme_recruit_cache:
            self._theme_recruit_cache[theme] = load_theme_recruitment_priorities(self.maa_dir, theme)
        return self._theme_recruit_cache[theme]

    def _use_agent_collab(self) -> bool:
        if self.client.is_configured():
            return False
        return self.ai_mode == "agent_collab" and (
            self.collab_handler is not None or bool(self.rl_cfg.get("ai_copilot_enabled", False))
        )

    def _record_decision(self, decision: RoguelikeDecision, run_context: Optional[RoguelikeRunContext] = None) -> RoguelikeDecision:
        entry = decision.to_dict()
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        self.decision_history.append(entry)
        if run_context is not None:
            run_context.decisions_log.append(entry)
        _emit(
            self.log_callback,
            f"[RL-AI:{decision.decision_type}] ({decision.source}) action={decision.action} "
            f"choice={decision.selected_value} | {decision.reason}",
        )
        return decision

    def _resolve_index_from_ai_reply(
        self,
        reply: Dict[str, Any],
        options: List[Dict[str, Any]],
        valid_indices: Sequence[int],
        name_field: str = "name",
    ) -> Optional[int]:
        valid_set = set(valid_indices)
        req_idx = _safe_int(reply.get("selected_index"))
        if req_idx is not None and req_idx in valid_set:
            return req_idx

        req_opt = reply.get("selected_option")
        if req_opt is None:
            req_opt = reply.get("choice") or reply.get("operator") or reply.get("node")
        if isinstance(req_opt, int) and req_opt in valid_set:
            return req_opt
        if isinstance(req_opt, str) and req_opt.strip():
            needle = req_opt.strip().lower()
            for idx in valid_indices:
                opt = options[idx]
                cand_name = str(opt.get(name_field, opt.get("title", opt.get("id", ""))) or "").strip().lower()
                if cand_name and (cand_name == needle or needle in cand_name):
                    return idx
        return None

    def _dispatch_choice(
        self,
        decision_type: str,
        theme: str,
        options: List[Dict[str, Any]],
        valid_indices: List[int],
        context: Dict[str, Any],
        baseline: RoguelikeDecision,
        system_prompt: str,
        screenshot_path: Optional[Path] = None,
        name_field: str = "name",
    ) -> RoguelikeDecision:
        if not valid_indices:
            return baseline

        # Mode 1: Built-in OpenAI-compatible API (multimodal vision preferred, text fallback)
        if self.client.is_configured():
            user_prompt = json.dumps(
                {
                    "decision_type": decision_type,
                    "theme": theme,
                    "options": options,
                    "valid_indices": valid_indices,
                    "context": context,
                    "rule_baseline": baseline.to_dict(),
                },
                ensure_ascii=False,
            )
            shot = Path(screenshot_path) if screenshot_path else None
            use_vision = bool(self.vision_enabled and shot is not None and shot.is_file())
            if use_vision:
                reply = self.client.chat_vision_json(system_prompt, user_prompt, image_path=shot)
                src_tag = "builtin_api_vision"
            else:
                reply = self.client.chat_json(system_prompt, user_prompt)
                src_tag = "builtin_api_text"

            if isinstance(reply, dict):
                if str(reply.get("action", "")).strip().lower() == "give_up":
                    return RoguelikeDecision(
                        decision_type=decision_type,
                        selected_index=None,
                        selected_value=None,
                        action="give_up",
                        source=src_tag,
                        reason=str(reply.get("reason") or "AI 决策放弃本次选择"),
                    )
                chosen = self._resolve_index_from_ai_reply(reply, options, valid_indices, name_field=name_field)
                if chosen is not None:
                    opt = options[chosen]
                    xy = tuple(opt["click_xy"]) if isinstance(opt.get("click_xy"), (list, tuple)) and len(opt["click_xy"]) == 2 else None
                    return RoguelikeDecision(
                        decision_type=decision_type,
                        selected_index=chosen,
                        selected_value=opt.get(name_field) or opt.get("title") or opt.get("id") or chosen,
                        action="select",
                        source=src_tag,
                        reason=str(reply.get("reason") or "AI 策略模型选择"),
                        click_xy=xy,
                        extra={"option": opt, "ai_raw": reply},
                    )
            return baseline

        # Mode 2: Desktop Agent zero-key collaboration via agent_task.json
        if self._use_agent_collab():
            task_file, response_file, payload = write_roguelike_agent_task(
                base_dir=self.base_dir,
                decision_type=decision_type,
                theme=theme,
                options=options,
                context=context,
                rule_baseline=baseline.to_dict(),
                screenshot_path=str(screenshot_path or ""),
                timeout_sec=self.decision_timeout_sec,
                auto_fallback_on_timeout=self.auto_fallback_on_timeout,
            )
            payload["agent_task_path"] = str(task_file)
            _emit(
                self.log_callback,
                f"[RL-Agent协同] 已写入 {task_file.name} ({decision_type})，等待外部 Agent 响应"
                f"（超时 {self.decision_timeout_sec:.0f}s，自动兜底={self.auto_fallback_on_timeout}）...",
            )
            if self.collab_handler is not None:
                try:
                    self.collab_handler(payload)
                except Exception:
                    pass

            status_res, agent_reply = wait_for_agent_decision(
                task_file=task_file,
                response_file=response_file,
                cancel_event=self.cancel_event,
                manual_resume_event=self.manual_resume_event,
                timeout_sec=self.decision_timeout_sec,
                auto_fallback_on_timeout=self.auto_fallback_on_timeout,
                poll_interval=self.poll_interval,
            )
            if status_res == "cancelled":
                return RoguelikeDecision(
                    decision_type=decision_type,
                    selected_index=None,
                    selected_value=None,
                    action="cancelled",
                    source="cancelled",
                    reason="收到取消信号，终止肉鸽 AI 决策等待",
                )
            if status_res == "agent_resolved" and isinstance(agent_reply, dict):
                if str(agent_reply.get("action", "")).strip().lower() == "give_up":
                    return RoguelikeDecision(
                        decision_type=decision_type,
                        selected_index=None,
                        selected_value=None,
                        action="give_up",
                        source="agent_collab",
                        reason=str(agent_reply.get("reason") or agent_reply.get("agent_reason") or "外部 Agent 建议放弃"),
                    )
                chosen = self._resolve_index_from_ai_reply(agent_reply, options, valid_indices, name_field=name_field)
                if chosen is not None:
                    opt = options[chosen]
                    xy = tuple(opt["click_xy"]) if isinstance(opt.get("click_xy"), (list, tuple)) and len(opt["click_xy"]) == 2 else None
                    return RoguelikeDecision(
                        decision_type=decision_type,
                        selected_index=chosen,
                        selected_value=opt.get(name_field) or opt.get("title") or opt.get("id") or chosen,
                        action="select",
                        source="agent_collab",
                        reason=str(agent_reply.get("reason") or agent_reply.get("agent_reason") or "外部桌面 Agent 协同决策"),
                        click_xy=xy,
                        extra={"option": opt, "agent_raw": agent_reply},
                    )
            if status_res == "timeout_fallback":
                fb = copy.deepcopy(baseline)
                fb.source = "rule_timeout_fallback"
                fb.reason = f"外部 Agent 等待超时 ({self.decision_timeout_sec:.0f}s)，自动按内置规则兜底: {baseline.reason}"
                return fb
            return baseline

        return baseline

    # ------------------------------------------------------------------
    # Decision Point 1: Operator Recruitment Draft (干员招募抓位)
    # ------------------------------------------------------------------
    def _rule_decide_recruitment(
        self,
        candidates: List[Dict[str, Any]],
        current_team: Sequence[str],
        hope: int,
        theme: str,
    ) -> Tuple[RoguelikeDecision, List[int], Dict[str, Any]]:
        my_box = self.get_my_box()
        owned = my_box.get("all_owned", {}) if isinstance(my_box.get("all_owned"), dict) else {}
        team_set = set(current_team)
        team_info = _summarize_team_roles(current_team, self.role_map, owned)
        theme_scores = self._get_theme_recruit_scores(theme)

        affordable: List[int] = []
        scored: List[Tuple[float, int, Dict[str, Any], List[str]]] = []

        for idx, cand in enumerate(candidates):
            name = cand["name"]
            cost = 0 if cand["is_temp"] else int(cand.get("hope_cost", 0))
            if cost > hope:
                continue
            if name in team_set and not cand["is_promote"]:
                continue

            affordable.append(idx)
            op_info = owned.get(name)
            role = self.role_map.get(name, "guard_lane")
            score = 0.0
            reasons: List[str] = []

            if cand["is_temp"]:
                score += 260.0
                reasons.append("免费临时招募")

            if op_info:
                elite = int(op_info.get("elite", 0))
                level = int(op_info.get("level", 1))
                rarity = int(op_info.get("rarity", 3))
                if elite >= 2:
                    score += 620.0 + min(level, 90) * 0.8 + rarity * 18.0
                    reasons.append(f"已精二 Lv.{level}")
                elif elite == 1:
                    score += 220.0 + min(level, 80) * 0.5 + rarity * 10.0
                    reasons.append(f"精一 Lv.{level}")
                else:
                    score += 60.0 + rarity * 4.0
                    reasons.append("精零")
            elif not cand["is_temp"]:
                score -= 250.0
                reasons.append("未在 my_box 持有")

            # Role gap compensation: 阻挡 / 对空 / 医疗
            if role in _BLOCKER_ROLES:
                if team_info["blocker_count"] == 0:
                    score += 310.0
                    reasons.append("补齐地面阻挡位")
                elif team_info["blocker_count"] == 1:
                    score += 120.0
                    reasons.append("增强前排抗线")
            elif role in _ANTIAIR_ROLES:
                if team_info["antiair_count"] == 0:
                    score += 290.0
                    reasons.append("补齐对空高台输出")
                elif team_info["antiair_count"] == 1:
                    score += 110.0
                    reasons.append("补充远程输出")
            elif role in _MEDIC_ROLES:
                if team_info["medic_count"] == 0:
                    score += 280.0
                    reasons.append("补齐医疗续航")
                elif team_info["medic_count"] == 1 and team_info["team_size"] >= 5:
                    score += 70.0
            elif role == "vanguard_dp" and team_info["role_counts"].get("vanguard_dp", 0) == 0:
                score += 140.0
                reasons.append("补齐回费先锋")

            if cand["is_promote"] and name in team_set:
                score += 220.0
                reasons.append("核心干员进阶精二")

            # Theme recruitment table & meta carry bonus
            if name in theme_scores:
                score += min(theme_scores[name], 1000) * 0.28
            if name in _META_ROGUELIKE_CARRIES:
                score += _META_ROGUELIKE_CARRIES[name]
                reasons.append("肉鸽核心强卡")

            # Hope efficiency tie-breaker
            score -= cost * 12.0
            scored.append((score, idx, cand, reasons))

        if not scored:
            dec = RoguelikeDecision(
                decision_type="recruitment",
                selected_index=None,
                selected_value=None,
                action="give_up",
                source="rule_engine",
                reason=f"当前希望 ({hope}) 不足或无合适可招募干员，放弃本次招募",
            )
            return dec, [], team_info

        scored.sort(key=lambda x: (-x[0], x[1]))
        best_score, best_idx, best_cand, best_reasons = scored[0]
        xy = tuple(best_cand["click_xy"]) if isinstance(best_cand.get("click_xy"), (list, tuple)) and len(best_cand["click_xy"]) == 2 else None
        dec = RoguelikeDecision(
            decision_type="recruitment",
            selected_index=best_idx,
            selected_value=best_cand["name"],
            action="select",
            source="rule_engine",
            reason=f"推荐抓位 {best_cand['name']}（评分 {best_score:.0f}：{'、'.join(best_reasons[:3])}）",
            click_xy=xy,
            extra={
                "score": round(best_score, 1),
                "team_summary": team_info,
                "ranking": [
                    {"index": i, "name": c["name"], "score": round(s, 1)}
                    for s, i, c, _ in scored[:5]
                ],
            },
        )
        return dec, affordable, team_info

    def decide_recruitment(
        self,
        candidates: Sequence[Any],
        run_context: Optional[RoguelikeRunContext] = None,
        current_team: Optional[Sequence[str]] = None,
        hope: Optional[int] = None,
        theme: Optional[str] = None,
        screenshot_path: Optional[Path] = None,
    ) -> RoguelikeDecision:
        """Decision Point 1: Choose best operator to recruit/promote based on my_box & squad balance."""
        ctx = run_context or RoguelikeRunContext()
        eff_theme = theme or ctx.theme
        eff_team = list(current_team) if current_team is not None else list(ctx.current_team)
        eff_hope = int(hope) if hope is not None else int(ctx.hope)
        norm_cands = _normalize_candidate_list(candidates)

        baseline, affordable, team_info = self._rule_decide_recruitment(norm_cands, eff_team, eff_hope, eff_theme)
        sys_prompt = (
            "你是《明日方舟》集成战略（肉鸽）抓位策略专家。"
            "结合当前编队职能缺口（阻挡/对空/医疗）、my_box 干员精二练度与剩余希望，从 valid_indices 中选出最优干员。"
            '仅输出合法 JSON：{"selected_index": 0, "action": "select", "reason": "..."}。'
        )
        ai_ctx = {
            "run": ctx.to_dict(),
            "hope": eff_hope,
            "current_team": eff_team,
            "team_summary": team_info,
            "owned_e2_top": [o.get("name") for o in self.get_my_box().get("e2_operators", [])[:25]],
        }
        decision = self._dispatch_choice(
            decision_type="recruitment",
            theme=eff_theme,
            options=norm_cands,
            valid_indices=affordable,
            context=ai_ctx,
            baseline=baseline,
            system_prompt=sys_prompt,
            screenshot_path=screenshot_path,
            name_field="name",
        )
        if decision.action == "select" and decision.selected_value and run_context is not None:
            if decision.selected_value not in run_context.current_team:
                run_context.current_team.append(str(decision.selected_value))
        return self._record_decision(decision, run_context)

    # ------------------------------------------------------------------
    # Decision Point 2: Map Branch & Stage Node Selection (地图分叉与关卡节点选择)
    # ------------------------------------------------------------------
    def _rule_decide_map_node(
        self,
        nodes: List[Dict[str, Any]],
        ctx: RoguelikeRunContext,
    ) -> Tuple[RoguelikeDecision, List[int], Dict[str, Any]]:
        owned = self.get_my_box().get("all_owned", {})
        team_info = _summarize_team_roles(ctx.current_team, self.role_map, owned)

        weak_squad = (
            team_info["e2_count"] < 2
            or team_info["blocker_count"] == 0
            or (ctx.floor >= 2 and team_info["antiair_count"] == 0)
            or (ctx.floor >= 2 and team_info["medic_count"] == 0)
        )
        low_hp = ctx.hp <= 3 or (ctx.max_hp > 0 and (ctx.hp / ctx.max_hp) <= 0.35)

        valid_indices: List[int] = []
        scored: List[Tuple[float, int, Dict[str, Any], List[str]]] = []

        for idx, node in enumerate(nodes):
            ap_cost = int(node.get("ap_cost", node.get("cost", 0)) or 0)
            if ap_cost > ctx.action_points and ctx.theme.lower() == "blackflow":
                continue
            valid_indices.append(idx)

            ntype = str(node.get("node_type", node.get("type", "combat")) or "combat").strip().lower()
            nname = str(node.get("name", node.get("stage_code", ntype)) or ntype).strip()
            score = 0.0
            reasons: List[str] = []

            if ntype in ("boss", "battle_boss", "dreadful_foe"):
                score += 180.0 if ctx.floor >= 3 else 120.0
                reasons.append("楼层首领节点")
            elif ntype in ("emergency", "battle_elite", "battle_savage", "emergency_ops"):
                # Core safety rule: avoid difficult emergency ops when HP low or squad incomplete
                base_emg = 240.0
                is_high_danger = any(k in nname for k in _DANGEROUS_EMERGENCY_KEYWORDS) or bool(node.get("high_risk"))
                if low_hp:
                    base_emg -= 520.0
                    reasons.append(f"血量偏低({ctx.hp})，规避紧急作战")
                if weak_squad:
                    base_emg -= 460.0
                    reasons.append("当前阵容核心职能/精二不足，规避高难紧急作战")
                if is_high_danger and weak_squad:
                    base_emg -= 180.0
                if not low_hp and not weak_squad:
                    base_emg += 120.0
                    reasons.append("阵容成型且血量健康，可打紧急作战赚发育")
                score += base_emg
            elif ntype in ("safehouse", "rest"):
                score += 340.0 if low_hp else 190.0
                reasons.append("回血休整/精二发育" if low_hp else "安全发育节点")
            elif ntype in ("boons", "wish", "downtime"):
                score += 290.0
                reasons.append("稳定藏品/无战损收益")
            elif ntype in ("encounter", "incident"):
                score += 275.0
                if node.get("has_informant"):
                    score += 80.0
                    reasons.append("带线人角标的不期而遇")
                else:
                    reasons.append("低风险不期而遇事件")
            elif ntype in ("trader", "shop", "scrap_shop"):
                if ctx.ingots >= 16:
                    score += 310.0
                    reasons.append(f"源石锭充裕({ctx.ingots})，优先进商店转化战力")
                elif ctx.ingots >= 8:
                    score += 220.0
                    reasons.append("进商店补强或投资")
                else:
                    score += 110.0
                    reasons.append("源石锭较少，商店收益一般")
            elif ntype in ("combat", "battle_normal", "combat_ops"):
                score += 245.0 if not low_hp else 160.0
                reasons.append("常规作战稳步发育")
            elif ntype in ("portal", "door", "bosky_passage"):
                score += 230.0
                reasons.append("特殊通道拓展路线")
            else:
                score += 150.0
                reasons.append(f"常规节点({ntype})")

            score -= ap_cost * 8.0
            scored.append((score, idx, node, reasons))

        if not scored:
            return (
                RoguelikeDecision(
                    decision_type="map_node",
                    selected_index=None,
                    selected_value=None,
                    action="give_up",
                    source="rule_engine",
                    reason="当前无可到达的地图节点",
                ),
                [],
                team_info,
            )

        scored.sort(key=lambda x: (-x[0], x[1]))
        best_score, best_idx, best_node, best_reasons = scored[0]
        label = str(best_node.get("name") or best_node.get("node_type") or best_idx)
        xy = tuple(best_node["click_xy"]) if isinstance(best_node.get("click_xy"), (list, tuple)) and len(best_node["click_xy"]) == 2 else None
        dec = RoguelikeDecision(
            decision_type="map_node",
            selected_index=best_idx,
            selected_value=label,
            action="select",
            source="rule_engine",
            reason=f"选择节点 {label}（评分 {best_score:.0f}：{'、'.join(best_reasons[:2])}）",
            click_xy=xy,
            extra={
                "node_type": best_node.get("node_type", best_node.get("type", "")),
                "weak_squad": weak_squad,
                "low_hp": low_hp,
                "ranking": [
                    {"index": i, "name": n.get("name", n.get("node_type")), "score": round(s, 1)}
                    for s, i, n, _ in scored[:5]
                ],
            },
        )
        return dec, valid_indices, team_info

    def decide_map_node(
        self,
        nodes: Sequence[Dict[str, Any]],
        run_context: Optional[RoguelikeRunContext] = None,
        screenshot_path: Optional[Path] = None,
    ) -> RoguelikeDecision:
        """Decision Point 2: Pick optimal reachable map branch/node weighing HP, team strength & risk."""
        ctx = run_context or RoguelikeRunContext()
        norm_nodes = [dict(n, index=i) for i, n in enumerate(nodes)]
        baseline, valid_indices, team_info = self._rule_decide_map_node(norm_nodes, ctx)

        sys_prompt = (
            "你是《明日方舟》集成战略（肉鸽）地图路线规划专家。"
            "权衡当前血量、源石锭、行动力、干员练度与职能完整度，规避当前阵容难处理的紧急作战，从 valid_indices 中选择最优下一个节点。"
            '仅输出合法 JSON：{"selected_index": 0, "action": "select", "reason": "..."}。'
        )
        ai_ctx = {"run": ctx.to_dict(), "team_summary": team_info}
        decision = self._dispatch_choice(
            decision_type="map_node",
            theme=ctx.theme,
            options=norm_nodes,
            valid_indices=valid_indices,
            context=ai_ctx,
            baseline=baseline,
            system_prompt=sys_prompt,
            screenshot_path=screenshot_path,
            name_field="name",
        )
        return self._record_decision(decision, run_context)

    # ------------------------------------------------------------------
    # Decision Point 3: Encounter Event & Relic/Idea Choice (不期而遇事件与藏品/构想抉择)
    # ------------------------------------------------------------------
    def _rule_decide_encounter_or_relic(
        self,
        event_title: str,
        options: List[Dict[str, Any]],
        category: str,
        ctx: RoguelikeRunContext,
    ) -> Tuple[RoguelikeDecision, List[int]]:
        valid_indices = [i for i, opt in enumerate(options) if not opt.get("disabled", False)]
        if not valid_indices:
            valid_indices = list(range(len(options)))

        low_hp = ctx.hp <= 3
        scored: List[Tuple[float, int, Dict[str, Any], List[str]]] = []

        for idx in valid_indices:
            opt = options[idx]
            text = " ".join(
                str(opt.get(k, "") or "")
                for k in ("title", "name", "description", "reward_text", "cost_text")
            )
            score = 100.0
            reasons: List[str] = []

            # Positive value keywords
            positive_rules = [
                (("热水壶", "国王", "空间租赁", "林间代步", "调查预付款"), 190.0, "高收益开局/核心道具"),
                (("攻速", "攻击速度", "技力", "同时部署", "部署上限"), 170.0, "顶级战斗藏品/构想"),
                (("精二", "进阶", "招募券", "希望", "免费招募"), 150.0, "提升干员招募/进阶发育"),
                (("收藏品", "稀有藏品", "藏品", "构想", "加工品"), 125.0, "获取藏品/构想收益"),
                (("源石锭", "恢复", "目标生命+", "护盾", "展开"), 95.0, "资源或续航增益"),
                (("攻击力", "生命上限", "防御", "费用"), 85.0, "属性加成"),
            ]
            for kws, pts, label in positive_rules:
                if any(k in text for k in kws):
                    score += pts
                    reasons.append(label)

            # Negative risk / cost checks
            if any(k in text for k in ("损失目标生命", "消耗目标生命", "失去目标生命", "生命值上限-")):
                if low_hp or ctx.hp <= 2:
                    score -= 480.0
                    reasons.append(f"血量危险({ctx.hp})，规避扣血代价")
                else:
                    score -= 60.0
            if any(k in text for k in ("失去所有源石锭", "消耗全部源石锭")) and ctx.ingots >= 15:
                score -= 220.0
                reasons.append("避免清空高额源石锭")
            if any(k in text for k in ("进入战斗", "迎战", "强敌")) and (low_hp or len(ctx.current_team) < 3):
                score -= 200.0
                reasons.append("战力不足时避免高风险事件战斗")
            if any(k in text for k in ("离开", "什么都不做", "放弃", "无视")):
                # Safe fallback: below real gains, above lethal traps
                score -= 45.0
                reasons.append("安全离开保底")

            scored.append((score, idx, opt, reasons))

        if not scored:
            return (
                RoguelikeDecision(
                    decision_type=category,
                    selected_index=None,
                    selected_value=None,
                    action="give_up",
                    source="rule_engine",
                    reason="无可用事件/藏品选项",
                ),
                [],
            )

        scored.sort(key=lambda x: (-x[0], x[1]))
        best_score, best_idx, best_opt, best_reasons = scored[0]
        label = str(best_opt.get("title") or best_opt.get("name") or f"选项{best_idx + 1}")
        xy = tuple(best_opt["click_xy"]) if isinstance(best_opt.get("click_xy"), (list, tuple)) and len(best_opt["click_xy"]) == 2 else None
        dec = RoguelikeDecision(
            decision_type=category,
            selected_index=best_idx,
            selected_value=label,
            action="select",
            source="rule_engine",
            reason=f"[{event_title or category}] 选择「{label}」（评分 {best_score:.0f}：{'、'.join(best_reasons[:2]) or '综合最优'}）",
            click_xy=xy,
            extra={
                "event_title": event_title,
                "category": category,
                "ranking": [
                    {"index": i, "title": o.get("title", o.get("name")), "score": round(s, 1)}
                    for s, i, o, _ in scored[:5]
                ],
            },
        )
        return dec, valid_indices

    def decide_encounter_or_relic(
        self,
        event_title: str,
        options: Sequence[Any],
        category: str = "encounter",
        run_context: Optional[RoguelikeRunContext] = None,
        screenshot_path: Optional[Path] = None,
    ) -> RoguelikeDecision:
        """Decision Point 3: Choose optimal option in an encounter event, relic pick, or idea selection."""
        ctx = run_context or RoguelikeRunContext()
        norm_opts: List[Dict[str, Any]] = []
        for i, item in enumerate(options):
            if isinstance(item, str):
                norm_opts.append({"index": i, "title": item.strip(), "description": ""})
            elif isinstance(item, dict):
                d = dict(item)
                d.setdefault("index", i)
                d.setdefault("title", str(d.get("name", f"选项{i + 1}")))
                norm_opts.append(d)

        baseline, valid_indices = self._rule_decide_encounter_or_relic(event_title, norm_opts, category, ctx)
        sys_prompt = (
            "你是《明日方舟》集成战略（肉鸽）不期而遇事件与藏品/构想抉择专家。"
            "结合当前血量、源石锭、编队与藏品，在避免致死代价前提下最大化长期发育收益，从 valid_indices 中选择最优选项。"
            '仅输出合法 JSON：{"selected_index": 0, "action": "select", "reason": "..."}。'
        )
        ai_ctx = {"run": ctx.to_dict(), "event_title": event_title, "category": category}
        decision = self._dispatch_choice(
            decision_type=category,
            theme=ctx.theme,
            options=norm_opts,
            valid_indices=valid_indices,
            context=ai_ctx,
            baseline=baseline,
            system_prompt=sys_prompt,
            screenshot_path=screenshot_path,
            name_field="title",
        )
        if decision.action == "select" and category in ("relic", "collectible") and decision.selected_value and run_context is not None:
            run_context.relics.append(str(decision.selected_value))
        elif decision.action == "select" and category == "idea" and decision.selected_value and run_context is not None:
            run_context.ideas.append(str(decision.selected_value))
        return self._record_decision(decision, run_context)

    # ------------------------------------------------------------------
    # Decision Point 4: Pre-Boss PRTS Copilot vs MAA Native Combat Handoff
    # ------------------------------------------------------------------
    def decide_boss_strategy(
        self,
        boss_stage_name: str,
        run_context: Optional[RoguelikeRunContext] = None,
        fetch_guides_fn: Optional[Callable[[str, str, int], List[Dict[str, Any]]]] = None,
        resolve_stage_fn: Optional[Callable[[str, Path], Dict[str, Any]]] = None,
        screenshot_path: Optional[Path] = None,
    ) -> RoguelikeDecision:
        """Decision Point 4: Before Boss battle, prefer compatible PRTS copilot or fall back to MAA battle engine."""
        from . import maa_runner

        ctx = run_context or RoguelikeRunContext()
        stage_query = str(boss_stage_name or "").strip()
        if not self.boss_prts_assist or not stage_query:
            return self._record_decision(
                RoguelikeDecision(
                    decision_type="boss_strategy",
                    selected_index=0,
                    selected_value="maa_native_battle",
                    action="maa_native_battle",
                    source="rule_engine",
                    reason="未启用 Boss PRTS 作业优先拉取或关卡名为空，交由 MAA 肉鸽原生战斗引擎接管",
                ),
                run_context,
            )

        res_fn = resolve_stage_fn or maa_runner.resolve_stage_info
        guides_fn = fetch_guides_fn or maa_runner.fetch_prts_guides

        stage_info = res_fn(stage_query, self.maa_dir) if self.maa_dir is not None else {"stage_id": stage_query, "code": stage_query}
        stage_id = str(stage_info.get("stage_id") or stage_query)
        stage_code = str(stage_info.get("code") or stage_query)

        try:
            guides = guides_fn(stage_id, stage_code, 5)
        except Exception:
            guides = []

        if not guides:
            return self._record_decision(
                RoguelikeDecision(
                    decision_type="boss_strategy",
                    selected_index=0,
                    selected_value="maa_native_battle",
                    action="maa_native_battle",
                    source="rule_engine",
                    reason=f"未在 prts.maa.plus 检索到 Boss 关「{stage_code}」作业，自动交由 MAA 原生战斗引擎接管",
                    extra={"stage_id": stage_id, "stage_code": stage_code},
                ),
                run_context,
            )

        my_box = self.get_my_box()
        # Filter box to current Roguelike recruited squad if squad is known and non-empty
        effective_box = copy.deepcopy(my_box)
        if ctx.current_team:
            owned_all = effective_box.get("all_owned", {})
            squad_owned = {k: v for k, v in owned_all.items() if k in set(ctx.current_team)}
            if squad_owned:
                effective_box["all_owned"] = squad_owned
                effective_box["e2_operators"] = [o for o in effective_box.get("e2_operators", []) if o.get("name") in squad_owned]

        adapted: List[Dict[str, Any]] = []
        for g in guides:
            try:
                ad = ai_strategist.adapt_copilot(g, effective_box, stage_id, maa_dir=self.maa_dir, client=self.client)
                adapted.append(ad)
            except Exception:
                continue
        adapted.sort(key=lambda x: -x.get("compat_score", -10**9))

        best = adapted[0] if adapted else None
        if best and not best.get("missing_ops") and best.get("compat_score", -1) > 0:
            out_dir = self.base_dir / "copilots"
            out_dir.mkdir(parents=True, exist_ok=True)
            safe_stage = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", stage_code)
            copilot_path = out_dir / f"rl_boss_{ctx.theme}_{safe_stage}.json"
            copilot_path.write_text(json.dumps(best["copilot"], ensure_ascii=False, indent=2), encoding="utf-8")

            options = [
                {
                    "index": 0,
                    "title": "prts_copilot",
                    "description": f"使用适配 PRTS 作业 #{best.get('guide_id')} (兼容分 {best.get('compat_score')})",
                    "copilot_path": str(copilot_path),
                },
                {
                    "index": 1,
                    "title": "maa_native_battle",
                    "description": "交由 MAA 肉鸽内置自动战斗引擎接管",
                },
            ]
            baseline = RoguelikeDecision(
                decision_type="boss_strategy",
                selected_index=0,
                selected_value="prts_copilot",
                action="prts_copilot",
                source="rule_engine",
                reason=f"命中高兼容 PRTS Boss 作业 #{best.get('guide_id')}（零缺失干员，兼容分 {best.get('compat_score')}），优先采用 Copilot 过关",
                extra={
                    "stage_id": stage_id,
                    "stage_code": stage_code,
                    "guide_id": best.get("guide_id"),
                    "compat_score": best.get("compat_score"),
                    "copilot_path": str(copilot_path),
                    "copilot": best["copilot"],
                },
            )
            sys_prompt = (
                "你是《明日方舟》肉鸽 Boss 战前决策助手。决定使用适配好的 PRTS 作业(0)还是交由 MAA 肉鸽原生战斗引擎(1)。"
                '仅输出合法 JSON：{"selected_index": 0, "reason": "..."}。'
            )
            dec = self._dispatch_choice(
                decision_type="boss_strategy",
                theme=ctx.theme,
                options=options,
                valid_indices=[0, 1],
                context={"run": ctx.to_dict(), "boss_stage": stage_code, "best_guide_id": best.get("guide_id")},
                baseline=baseline,
                system_prompt=sys_prompt,
                screenshot_path=screenshot_path,
                name_field="title",
            )
            if dec.selected_index == 0:
                dec.action = "prts_copilot"
                dec.selected_value = "prts_copilot"
                dec.extra.update(baseline.extra)
            elif dec.action != "cancelled":
                dec.action = "maa_native_battle"
                dec.selected_value = "maa_native_battle"
            return self._record_decision(dec, run_context)

        return self._record_decision(
            RoguelikeDecision(
                decision_type="boss_strategy",
                selected_index=1,
                selected_value="maa_native_battle",
                action="maa_native_battle",
                source="rule_engine",
                reason=f"检索到 {len(guides)} 份 PRTS 作业但当前肉鸽阵容缺失关键干员，无缝交由 MAA 战斗引擎接管",
                extra={
                    "stage_id": stage_id,
                    "stage_code": stage_code,
                    "missing_ops": best.get("missing_ops", []) if best else [],
                },
            ),
            run_context,
        )


# ======================================================================
# Screen Classification & Visual Tech-Tree / Reward Claiming Helpers
# ======================================================================


def classify_roguelike_screen(
    img: Image.Image,
    maa_dir: Path,
    theme: str = "BlackFlow",
) -> Dict[str, Any]:
    """Classify current Roguelike screen into battle / lobby / recruitment / encounter / map / unknown."""
    st = stage_pilot.analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
    if st.get("in_battle"):
        return {"screen": "in_battle", "boxes": []}

    profile = get_theme_ui_profile(theme)
    boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True)
    joined = " ".join(b.get("compact", "") for b in boxes)

    if any(k in joined for k in ("开始探索", "继续探索")) or any(k in joined for k in profile["lobby_keywords"][1:]):
        return {"screen": "theme_lobby", "boxes": boxes}
    if any(k in joined for k in ("招募干员", "干员招募", "选择招募", "临时招募", "放弃招募")):
        return {"screen": "recruitment", "boxes": boxes}
    if any(k in joined for k in ("险路恶敌", "首领")) and any(k in joined for k in ("开始行动", "出发前往", "进入作战")):
        return {"screen": "boss_preview", "boxes": boxes}
    if any(k in joined for k in ("不期而遇", "狭路相逢", "选择一项", "离开")) and not any(k in joined for k in ("开始探索",)):
        return {"screen": "encounter", "boxes": boxes}
    if any(k in joined for k in ("作战", "紧急作战", "诡意行商", "秘境行商", "安全的角落", "出发前往")):
        return {"screen": "map", "boxes": boxes}
    return {"screen": "unknown", "boxes": boxes}


def _find_matching_ocr_box(
    boxes: Sequence[Dict[str, Any]],
    keywords: Sequence[str],
    roi: Optional[Tuple[int, int, int, int]] = None,
    exclude_keywords: Sequence[str] = (),
) -> Optional[Dict[str, Any]]:
    for kw in keywords:
        kw_clean = re.sub(r"\s+", "", kw)
        for b in boxes:
            txt = str(b.get("compact") or b.get("text") or "")
            if exclude_keywords and any(ex in txt for ex in exclude_keywords):
                continue
            if kw_clean not in txt:
                continue
            if roi is not None:
                rx, ry, rw, rh = roi
                cx, cy = int(b.get("cx", b.get("x", 0))), int(b.get("cy", b.get("y", 0)))
                if not (rx - 20 <= cx <= rx + rw + 20 and ry - 20 <= cy <= ry + rh + 20):
                    continue
            return dict(b)
    return None


def score_tech_tree_node(
    name: str,
    description: str = "",
    priority_order: Optional[Sequence[str]] = None,
) -> Tuple[int, str]:
    """Score a Roguelike tech-tree node according to priority tiers.

    Default priority (highest to lowest):
      1. ``hope_dp_recruit``: 开局希望 / 初始费用 / 招募折扣
      2. ``combat_stats``: 战斗属性（攻击/生命/防御/攻速/目标生命等）
      3. ``shop_refresh``: 商店刷新 / 投资 / 源石锭
    """
    order = list(priority_order) if priority_order else list(DEFAULT_TECH_TREE_PRIORITY)
    text = f"{name} {description}".strip()
    if not text:
        return 10, "unknown"

    best_score = 30
    best_cat = "other"
    tier_count = len(order)
    for tier_idx, cat in enumerate(order):
        kws = TECH_TREE_CATEGORY_KEYWORDS.get(cat, ())
        hits = [kw for kw in kws if kw in text]
        if hits:
            # Higher tier dominates strictly (e.g. 300+, 200+, 100+)
            base_tier = (tier_count - tier_idx) * 100
            bonus = min(45, len(hits) * 12)
            # Extra tie-break preference for hope/DP/recruit specifics inside tier 1
            if cat == "hope_dp_recruit" and any(k in text for k in ("开局希望", "初始希望", "初始费用", "招募折扣")):
                bonus += 25
            cand_score = base_tier + bonus
            if cand_score > best_score:
                best_score = cand_score
                best_cat = cat
    return best_score, best_cat


def detect_upgradeable_tech_nodes(
    img: Image.Image,
    canvas_roi: Tuple[int, int, int, int] = (80, 90, 1120, 560),
    brightness_threshold: float = 175.0,
    min_cluster_pixels: int = 24,
) -> List[Dict[str, Any]]:
    """Detect lit / glowing upgradeable nodes in a tech-tree screenshot via brightness & highlight clustering."""
    if np is None:
        return []
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    arr = np.asarray(img_1280, dtype=np.float32)

    rx, ry, rw, rh = canvas_roi
    x0, y0 = max(0, rx), max(0, ry)
    x1, y1 = min(1280, rx + rw), min(720, ry + rh)
    patch = arr[y0:y1, x0:x1]
    if patch.size == 0:
        return []

    # Lit upgradeable nodes typically exhibit bright cyan/gold/white borders or high-luminance glow
    luma = 0.299 * patch[:, :, 0] + 0.587 * patch[:, :, 1] + 0.114 * patch[:, :, 2]
    warm_gold = (patch[:, :, 0] > 190) & (patch[:, :, 1] > 145) & (patch[:, :, 2] < 130)
    bright_cyan = (patch[:, :, 2] > 185) & (patch[:, :, 1] > 165) & (patch[:, :, 0] < 150)
    bright_white = luma >= brightness_threshold
    lit_mask = bright_white | warm_gold | bright_cyan

    # Downsample onto a 32x32 grid to cluster distinct node locations quickly without scipy
    cell = 32
    gh = max(1, patch.shape[0] // cell)
    gw = max(1, patch.shape[1] // cell)
    trimmed = lit_mask[: gh * cell, : gw * cell]
    counts = trimmed.reshape(gh, cell, gw, cell).sum(axis=(1, 3))

    raw_centers: List[Tuple[int, int, int]] = []
    visited = np.zeros((gh, gw), dtype=bool)
    for cy in range(gh):
        for cx in range(gw):
            if visited[cy, cx] or counts[cy, cx] < min_cluster_pixels:
                continue
            # Flood-fill 8-connected bright cells
            stack = [(cy, cx)]
            visited[cy, cx] = True
            total_px = 0
            sum_x = 0.0
            sum_y = 0.0
            while stack:
                py, px = stack.pop()
                w_px = int(counts[py, px])
                total_px += w_px
                sum_x += (px + 0.5) * cell * w_px
                sum_y += (py + 0.5) * cell * w_px
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = py + dy, px + dx
                        if 0 <= ny < gh and 0 <= nx < gw and not visited[ny, nx] and counts[ny, nx] >= min_cluster_pixels:
                            visited[ny, nx] = True
                            stack.append((ny, nx))
            if total_px >= min_cluster_pixels:
                node_x = int(round(x0 + sum_x / total_px))
                node_y = int(round(y0 + sum_y / total_px))
                raw_centers.append((total_px, node_x, node_y))

    # Deduplicate nearby centroids (within 56px)
    raw_centers.sort(key=lambda t: -t[0])
    nodes: List[Dict[str, Any]] = []
    for px_cnt, nx, ny in raw_centers:
        if any(abs(nx - n["cx"]) < 56 and abs(ny - n["cy"]) < 56 for n in nodes):
            continue
        nodes.append({"cx": nx, "cy": ny, "lit_pixels": px_cnt})

    # Sort left-to-right, top-to-bottom for deterministic inspection
    nodes.sort(key=lambda n: (n["cx"], n["cy"]))
    return nodes


def _tap_return_to_lobby(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str,
    max_taps: int = 3,
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Tap top-left return button until back on the Roguelike theme lobby screen."""
    profile = get_theme_ui_profile(theme)
    for _ in range(max_taps):
        img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
        if img is None:
            return False
        boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=False)
        joined = " ".join(b.get("compact", "") for b in boxes)
        if "开始探索" in joined or any(k in joined for k in profile["lobby_keywords"][1:]):
            return True
        st = stage_pilot.analyze_screen_state(img, maa_dir, use_ocr_fallback=False)
        if st.get("has_return"):
            stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, img.width, img.height)
        else:
            stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, img.width, img.height)
        time.sleep(0.8)
    _emit(log_callback, f"[RL-POST] 已尝试返回 {theme} 主界面。")
    return True


def _claim_all_on_current_reward_page(
    adb_path: str,
    adb_addr: str,
    max_clicks: int = 8,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    page_label: str = "奖励页",
) -> int:
    """Repeatedly OCR-detect and click claim buttons ('一键领取' / '领取') and dismiss popups."""
    claimed = 0
    for _ in range(max_clicks):
        if cancel_event and cancel_event.is_set():
            break
        img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
        if img is None:
            break
        boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True)

        # First dismiss any reward-obtained popup
        popup_hit = _find_matching_ocr_box(boxes, _REWARD_DISMISS_TEXTS)
        if popup_hit is not None:
            stage_pilot.adb_tap_norm(adb_path, adb_addr, 640, 620, img.width, img.height)
            time.sleep(0.6)
            continue

        btn = _find_matching_ocr_box(
            boxes,
            _REWARD_CLAIM_BUTTON_TEXTS,
            exclude_keywords=_REWARD_ALREADY_CLAIMED_TEXTS,
        )
        if btn is None:
            break
        stage_pilot.adb_tap_norm(adb_path, adb_addr, btn["cx"], btn["cy"], img.width, img.height)
        claimed += 1
        _emit(log_callback, f"[RL-REWARD] {page_label}：识别到「{btn['text']}」({btn['cx']},{btn['cy']})，已自动点击领取。")
        time.sleep(0.9)
        # Dismiss potential reward obtained popup
        stage_pilot.adb_tap_norm(adb_path, adb_addr, 640, 620, img.width, img.height)
        time.sleep(0.5)
    return claimed


def claim_roguelike_rewards(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str = "BlackFlow",
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Detect and claim level rewards (e.g. '源流堆栈') and monthly commissions ('本月委托栏') from theme lobby."""
    profile = get_theme_ui_profile(theme)
    result: Dict[str, Any] = {
        "ok": True,
        "theme": theme,
        "source": "adb_ocr",
        "level_rewards_claimed": 0,
        "commission_rewards_claimed": 0,
        "actions": [],
        "cancelled": False,
    }
    if cancel_event and cancel_event.is_set():
        result["ok"] = False
        result["cancelled"] = True
        return result

    img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
    if img is None:
        result["ok"] = False
        result["error"] = "ADB 截图失败，跳过领奖"
        return result

    lobby_boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True)

    # 1. Level Rewards (e.g. BlackFlow top-right "源流堆栈")
    lvl_spec = profile["level_reward"]
    lvl_box = _find_matching_ocr_box(lobby_boxes, lvl_spec["keywords"], roi=lvl_spec["roi"])
    if lvl_box is not None:
        _emit(log_callback, f"[RL-REWARD] 主界面定位等级奖励入口「{lvl_box['text']}」({lvl_box['cx']},{lvl_box['cy']})，进入领奖...")
        stage_pilot.adb_tap_norm(adb_path, adb_addr, lvl_box["cx"], lvl_box["cy"], img.width, img.height)
        time.sleep(1.0)
        n_lvl = _claim_all_on_current_reward_page(
            adb_path, adb_addr, cancel_event=cancel_event, log_callback=log_callback, page_label=lvl_spec["name"]
        )
        result["level_rewards_claimed"] = n_lvl
        result["actions"].append({"entry": lvl_spec["name"], "claimed": n_lvl})
        _tap_return_to_lobby(adb_path, adb_addr, maa_dir, theme, log_callback=log_callback)

    if cancel_event and cancel_event.is_set():
        result["cancelled"] = True
        return result

    # 2. Monthly Commissions (e.g. BlackFlow right-side "本月委托栏")
    img2 = stage_pilot.adb_screencap_image(adb_path, adb_addr)
    if img2 is not None:
        lobby_boxes2 = stage_pilot.find_text_boxes_via_ocr(img2, include_inverted=True)
        comm_spec = profile["monthly_commission"]
        comm_box = _find_matching_ocr_box(lobby_boxes2, comm_spec["keywords"], roi=comm_spec["roi"])
        if comm_box is not None:
            _emit(log_callback, f"[RL-REWARD] 主界面定位月度委托入口「{comm_box['text']}」({comm_box['cx']},{comm_box['cy']})，进入领奖...")
            stage_pilot.adb_tap_norm(adb_path, adb_addr, comm_box["cx"], comm_box["cy"], img2.width, img2.height)
            time.sleep(1.0)
            n_comm = _claim_all_on_current_reward_page(
                adb_path, adb_addr, cancel_event=cancel_event, log_callback=log_callback, page_label=comm_spec["name"]
            )
            result["commission_rewards_claimed"] = n_comm
            result["actions"].append({"entry": comm_spec["name"], "claimed": n_comm})
            _tap_return_to_lobby(adb_path, adb_addr, maa_dir, theme, log_callback=log_callback)

    return result


def auto_upgrade_roguelike_tech_tree(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str = "BlackFlow",
    priority_order: Optional[Sequence[str]] = None,
    max_upgrades: int = 12,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Enter Roguelike tech tree (e.g. BlackFlow '生命游戏'), detect lit upgradeable nodes, and upgrade by priority."""
    profile = get_theme_ui_profile(theme)
    tech_spec = profile["tech_tree"]
    eff_priority = list(priority_order) if priority_order else list(DEFAULT_TECH_TREE_PRIORITY)
    result: Dict[str, Any] = {
        "ok": True,
        "theme": theme,
        "tech_tree_name": tech_spec["name"],
        "source": "adb_ocr",
        "upgraded_count": 0,
        "upgraded_nodes": [],
        "cancelled": False,
    }
    if cancel_event and cancel_event.is_set():
        result["ok"] = False
        result["cancelled"] = True
        return result

    img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
    if img is None:
        result["ok"] = False
        result["error"] = "ADB 截图失败，跳过科技树加点"
        return result

    lobby_boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True)
    entry_box = _find_matching_ocr_box(lobby_boxes, tech_spec["keywords"], roi=tech_spec["roi"])
    if entry_box is None:
        result["skipped_reason"] = f"主界面未识别到科技树入口「{tech_spec['name']}」"
        return result

    _emit(log_callback, f"[RL-TECH] 定位科技树入口「{entry_box['text']}」({entry_box['cx']},{entry_box['cy']})，进入自动加点...")
    stage_pilot.adb_tap_norm(adb_path, adb_addr, entry_box["cx"], entry_box["cy"], img.width, img.height)
    time.sleep(1.1)

    try:
        for _round in range(max_upgrades):
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                break

            tree_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
            if tree_img is None:
                break

            lit_nodes = detect_upgradeable_tech_nodes(tree_img)
            if not lit_nodes:
                _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」未检测到更多亮起的可升级节点，结束加点。")
                break

            # Inspect each lit candidate node to read its title/description & upgrade button via OCR
            evaluated: List[Dict[str, Any]] = []
            for node in lit_nodes[:10]:
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    break
                stage_pilot.adb_tap_norm(adb_path, adb_addr, node["cx"], node["cy"], tree_img.width, tree_img.height)
                time.sleep(0.55)
                detail_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
                if detail_img is None:
                    continue
                detail_boxes = stage_pilot.find_text_boxes_via_ocr(detail_img, include_inverted=True)
                detail_text = " ".join(b.get("text", "") for b in detail_boxes)
                if any(u in detail_text for u in _TECH_UNAVAILABLE_TEXTS):
                    continue
                up_btn = _find_matching_ocr_box(detail_boxes, _TECH_UPGRADE_BUTTON_TEXTS)
                if up_btn is None:
                    continue
                score, tier = score_tech_tree_node(detail_text, "", priority_order=eff_priority)
                evaluated.append({
                    "node": node,
                    "detail_text": detail_text[:120],
                    "score": score,
                    "tier": tier,
                    "upgrade_xy": (up_btn["cx"], up_btn["cy"]),
                    "upgrade_label": up_btn["text"],
                })

            if result["cancelled"] or not evaluated:
                break

            # Pick highest-priority affordable node (开局希望/初始费用/招募折扣 > 战斗属性 > 商店刷新)
            evaluated.sort(key=lambda item: -item["score"])
            best = evaluated[0]
            bnode = best["node"]
            stage_pilot.adb_tap_norm(adb_path, adb_addr, bnode["cx"], bnode["cy"], tree_img.width, tree_img.height)
            time.sleep(0.45)
            ux, uy = best["upgrade_xy"]
            stage_pilot.adb_tap_norm(adb_path, adb_addr, ux, uy, tree_img.width, tree_img.height)
            time.sleep(0.85)
            # Dismiss any confirmation / activation toast
            stage_pilot.adb_tap_norm(adb_path, adb_addr, 640, 620, tree_img.width, tree_img.height)
            time.sleep(0.35)

            result["upgraded_count"] += 1
            result["upgraded_nodes"].append({
                "cx": bnode["cx"],
                "cy": bnode["cy"],
                "tier": best["tier"],
                "score": best["score"],
                "summary": best["detail_text"],
            })
            _emit(
                log_callback,
                f"[RL-TECH] 已按优先级自动加点 [{best['tier']}|分={best['score']}] ({bnode['cx']},{bnode['cy']}) -> {best['detail_text'][:48]}",
            )
    finally:
        _tap_return_to_lobby(adb_path, adb_addr, maa_dir, theme, log_callback=log_callback)

    return result


def run_roguelike_post_settlement(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    config: Dict[str, Any],
    theme: str = "BlackFlow",
    capabilities: Optional[Dict[str, Any]] = None,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Post-settlement routine: claim level/commission rewards & auto-upgrade tech tree when MAA lacks native coverage."""
    rl_cfg = config.get("roguelike", {}) if isinstance(config.get("roguelike"), dict) else {}
    auto_claim = bool(rl_cfg.get("auto_claim_rewards", True))
    auto_tech = bool(rl_cfg.get("auto_upgrade_tech_tree", True))
    prio = rl_cfg.get("tech_tree_priority") or list(DEFAULT_TECH_TREE_PRIORITY)

    caps = capabilities or {}
    theme_caps = caps.get("roguelike_theme_capabilities", {}).get(theme, {}) if isinstance(caps.get("roguelike_theme_capabilities"), dict) else {}

    out: Dict[str, Any] = {
        "theme": theme,
        "rewards": {"skipped": True},
        "tech_tree": {"skipped": True},
        "cancelled": False,
    }

    if cancel_event and cancel_event.is_set():
        out["cancelled"] = True
        return out

    if not adb_path or not adb_addr:
        out["skipped_reason"] = "ADB 路径或地址为空，跳过局后视觉领奖与科技树加点"
        return out

    if auto_claim:
        if theme_caps.get("native_reward_claim"):
            out["rewards"] = {"ok": True, "source": "maa_native", "note": "已由 MAA 原生任务处理领奖"}
        else:
            out["rewards"] = claim_roguelike_rewards(
                adb_path, adb_addr, maa_dir, theme=theme, cancel_event=cancel_event, log_callback=log_callback
            )
            if out["rewards"].get("cancelled"):
                out["cancelled"] = True
                return out

    if auto_tech:
        if theme_caps.get("native_tech_tree_upgrade"):
            out["tech_tree"] = {"ok": True, "source": "maa_native", "note": "已由 MAA 原生任务处理科技树加点"}
        else:
            out["tech_tree"] = auto_upgrade_roguelike_tech_tree(
                adb_path,
                adb_addr,
                maa_dir,
                theme=theme,
                priority_order=prio,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )
            if out["tech_tree"].get("cancelled"):
                out["cancelled"] = True

    return out
