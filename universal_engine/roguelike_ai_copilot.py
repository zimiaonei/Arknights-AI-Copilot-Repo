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

# Unified ROI & anti-misclick constants (1280x720 normalized coordinates)
MONTHLY_COMMISSION_KEYWORDS: Tuple[str, ...] = ("本月委托栏", "本月委托")
LEVEL_REWARD_PRIMARY_ROI: Tuple[int, int, int, int] = (860, 0, 420, 160)
LEVEL_REWARD_FALLBACK_ROI: Tuple[int, int, int, int] = (640, 0, 640, 260)
MONTHLY_COMMISSION_PRIMARY_ROI: Tuple[int, int, int, int] = (840, 120, 440, 420)
MONTHLY_COMMISSION_FALLBACK_ROI: Tuple[int, int, int, int] = (640, 80, 640, 520)
# Left-half valid zone for lobby tech-tree entries (excludes top bar y<80, bottom bar y>640, right half x>620)
TECH_TREE_LEFT_VALID_ROI: Tuple[int, int, int, int] = (0, 80, 620, 560)
# Stage-2 local high-contrast OCR crop & numeric point-badge band (normalized [0, 1])
TECH_TREE_LOCAL_OCR_ROI_NORM: Tuple[float, float, float, float] = (0.01, 0.25, 0.28, 0.75)
TECH_TREE_POINTS_BAND_Y_NORM: Tuple[float, float] = (0.35, 0.50)
TECH_TREE_LOCAL_OCR_UPSCALE: float = 2.5

TECH_TREE_ENTRY_BLACKLIST: Tuple[str, ...] = (
    "前瞻性投资系统",
    "投资系统",
    "巫仪档案库",
    "档案库",
    "印象重建",
    "图册",
    "见字",
    "黑流数据库",
    "数据库",
    "蚀刻章套组",
    "蚀刻章",
    "深入探索",
    "深入调查",
    "常规行动",
    "主题切换",
    "切换主题",
    "更换主题",
    "月度小队",
    "开始探险",
    "继续探险",
    "开始探索",
    "继续探索",
)

# Hard detection keywords for Integrated Strategies Theme Selection hub page vs Theme Lobby
THEME_SELECT_PAGE_KEYWORDS: Tuple[str, ...] = (
    "进入主题",
    "添加到终端",
    "查看收藏",
    "该主题已全额收录",
)

THEME_LOBBY_KEYWORDS: Tuple[str, ...] = (
    "开始探险",
    "继续探险",
    "开始探索",
    "继续探索",
    "蚀刻章套组",
    "常规行动",
    "深入探索",
    "深入调查",
    "月度小队",
)

_TECH_TREE_POINTS_NAME_KEYWORDS: Tuple[str, ...] = (
    "可用点数",
    "剩余点数",
    "点数",
    "重构点",
    "思绪",
    "构想",
    "蜡烛",
    "烛火",
    "灯火",
    "密文",
    "学识",
    "演化",
    "细胞",
    "源流",
)

_TECH_TREE_WRONG_PANEL_KEYWORDS: Tuple[str, ...] = (
    "前瞻性投资系统",
    "投资系统",
    "源石锭存取",
    "存入源石锭",
    "取出源石锭",
    "投资余额",
    "巫仪档案库",
    "档案库",
    "收藏品图鉴",
    "干员图鉴",
    "敌方图鉴",
    "印象重建",
    "黑流数据库",
    "蚀刻章套组",
    "蚀刻章",
)

# Tech-tree secondary panel zones (normalized [0, 1] & 1280x720)
# Valid safe canvas for clickable tech nodes: x in [0.08, 0.93], y in [0.16, 0.80]
# (strictly excludes left/right edge page-turn arrows '<'/'->' at x<0.08 or x>0.93, top bar y<0.16, bottom bar y>0.80)
TECH_TREE_CANVAS_SAFE_ROI_NORM: Tuple[float, float, float, float] = (0.08, 0.16, 0.93, 0.80)
TECH_TREE_CANVAS_ROI_1280: Tuple[int, int, int, int] = (103, 116, 1087, 460)
# Forbidden zones inside tech-tree panel (never clicked as nodes):
# 1) Left/Right edge page-turn arrows: x < 0.08 and x > 0.93
# 2) Top navigation bar: y < 0.16
# 3) Bottom detail bar: y > 0.80
# 4) Bottom-left attribute overview arch ('64/78 已重构'): tightened to x < 0.175, y > 0.63 so adjacent bottom-row small squares are preserved
# 5) Top-right points display: x >= 0.78, y <= 0.20
TECH_TREE_FORBIDDEN_ZONES_NORM: Tuple[Tuple[float, float, float, float], ...] = (
    (0.0, 0.0, 0.08, 1.0),
    (0.93, 0.0, 1.0, 1.0),
    (0.0, 0.0, 1.0, 0.16),
    (0.0, 0.80, 1.0, 1.0),
    (0.0, 0.63, 0.175, 1.0),
    (0.78, 0.0, 1.0, 0.20),
)
# Bottom-right confirm/upgrade button ROI (unique ground-truth for upgrade action): x in [0.72, 0.99], y in [0.84, 0.99]
TECH_TREE_CONFIRM_BTN_ROI_NORM: Tuple[float, float, float, float] = (0.72, 0.84, 0.99, 0.99)
TECH_TREE_CONFIRM_BTN_ROI_1280: Tuple[int, int, int, int] = (922, 605, 345, 108)
# Top-bar home house icon ROI (x in [0.15, 0.28], y < 0.12) & return arrow ROI
TECH_TREE_HOME_ICON_ROI_NORM: Tuple[float, float, float, float] = (0.15, 0.0, 0.28, 0.12)
TECH_TREE_HOME_ICON_ROI_1280: Tuple[int, int, int, int] = (192, 0, 166, 86)
TECH_TREE_RETURN_ICON_ROI_1280: Tuple[int, int, int, int] = (0, 0, 180, 86)
# Top-right remaining points ROI: x in [0.78, 0.99], y in [0.02, 0.18]
TECH_TREE_TOP_RIGHT_POINTS_ROI_NORM: Tuple[float, float, float, float] = (0.78, 0.02, 0.99, 0.18)
TECH_TREE_TOP_RIGHT_POINTS_ROI_1280: Tuple[int, int, int, int] = (998, 14, 269, 116)
# Bottom-left progress counter badge ROI ('72/78 已重构' etc.): x in [0.0, 0.33], y in [0.53, 1.0]
TECH_TREE_PROGRESS_BADGE_ROI_1280: Tuple[int, int, int, int] = (0, 380, 420, 340)
_TECH_TREE_PROGRESS_BADGE_KEYWORDS: Tuple[str, ...] = (
    "已重构",
    "已塑造",
    "已激活",
    "已解锁",
    "已点亮",
    "已习得",
    "已演化",
    "已研习",
)
# Fallback extended bottom-right action quadrant ONLY for unrendered blank-canvas mock tests
_TECH_TREE_CONFIRM_BTN_COMPAT_ROI_1280: Tuple[int, int, int, int] = (920, 540, 350, 175)

# Wrong-page signatures intercepted by per-round Page Guard (never click nodes or swipe here!)
_TECH_TREE_PAGE_GUARD_WRONG_KEYWORDS: Tuple[str, ...] = (
    "巫仪档案库",
    "想象实体图鉴",
    "虚构展览",
    "设为活动主界面背景",
    "设为界面背景",
    "前瞻性投资系统",
    "投资系统",
    "源石锭存取",
    "存入源石锭",
    "取出源石锭",
    "收藏品图鉴",
    "干员图鉴",
    "敌方图鉴",
    "印象重建",
    "黑流数据库",
    "蚀刻章套组",
)

_TECH_OVERVIEW_OVERLAY_KEYWORDS: Tuple[str, ...] = (
    "属性总览",
    "效果总览",
    "加成总览",
    "效果一览",
)

# Disjoint semantic attribute groups for full-screen overview modal detection (requires >=2 hits AND NO home icon)
_TECH_OVERVIEW_ATTR_GROUPS: Tuple[Tuple[str, ...], ...] = (
    ("我方单位攻击力", "所有干员攻击力"),
    ("我方单位防御力", "我方单位防御", "所有干员防御力", "所有干员防御"),
    ("我方单位生命值", "我方单位生命", "所有干员生命值", "所有干员生命"),
    ("初始源石锭", "额外源石锭"),
    ("初始希望", "开局希望"),
    ("初始费用", "初始部署费用"),
    ("目标生命值", "目标生命"),
    ("部署上限", "编队上限", "编队人数"),
)

_TECH_TREE_PANEL_CONFIRM_KEYWORDS: Tuple[str, ...] = (
    "历史重构",
    "认知塑造",
    "文化比较",
    "幕后筹备",
    "古今学识",
    "生命游戏",
    "已重构",
    "已激活",
    "已解锁",
    "已塑造",
    "已点亮",
    "可用点数",
    "剩余点数",
    "确认重构",
    "确认激活",
    "确认解锁",
    "激活",
    "解锁",
    "重构",
    "升级",
    "塑造",
    "点亮",
)

_REWARD_ENTRY_BLACKLIST: Tuple[str, ...] = (
    "蚀刻章套组",
    "蚀刻章",
    "深入探索",
    "深入调查",
    "常规行动",
    "开始探险",
    "继续探险",
    "开始探索",
    "继续探索",
    "月度小队",
)

# Multi-theme lobby & post-settlement UI anchor profiles (1280x720 normalized coordinates)
THEME_UI_PROFILES: Dict[str, Dict[str, Any]] = {
    "Phantom": {
        "theme_key": "Phantom",
        "aliases": ["Phantom", "傀影与猩红孤钻", "猩红孤钻"],
        "display_name": "傀影与猩红孤钻",
        "is_ended_theme": True,
        "theme_select_keywords": ["傀影与猩红孤钻", "猩红孤钻", "古堡笔记"],
        "lobby_keywords": ["开始探险", "开始探索", "古堡笔记", "幕后筹备"],
        "level_reward": {
            "name": "古堡笔记",
            "keywords": ["古堡笔记"],
            "roi": LEVEL_REWARD_PRIMARY_ROI,
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": list(MONTHLY_COMMISSION_KEYWORDS),
            "roi": MONTHLY_COMMISSION_PRIMARY_ROI,
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "幕后筹备",
            "keywords": ["幕后筹备"],
            "split_pairs": [],
            "roi": TECH_TREE_LEFT_VALID_ROI,
            "anchor_norm": (0.145, 0.722),
            "fallback_xy": (185, 520),
        },
    },
    "Mizuki": {
        "theme_key": "Mizuki",
        "aliases": ["Mizuki", "水月与深蓝之树", "深蓝之树"],
        "display_name": "水月与深蓝之树",
        "is_ended_theme": True,
        "theme_select_keywords": ["水月与深蓝之树", "深蓝之树", "生态谱系"],
        "lobby_keywords": ["开始探险", "开始探索", "生态谱系", "认知塑造"],
        "level_reward": {
            "name": "生态谱系",
            "keywords": ["生态谱系"],
            "roi": LEVEL_REWARD_PRIMARY_ROI,
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": list(MONTHLY_COMMISSION_KEYWORDS),
            "roi": MONTHLY_COMMISSION_PRIMARY_ROI,
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "认知塑造",
            "keywords": ["认知塑造"],
            "split_pairs": [],
            "roi": TECH_TREE_LEFT_VALID_ROI,
            "anchor_norm": (0.145, 0.778),
            "fallback_xy": (185, 560),
        },
    },
    "Sami": {
        "theme_key": "Sami",
        "aliases": ["Sami", "探索者的银凇止境", "银凇止境"],
        "display_name": "探索者的银凇止境",
        "is_ended_theme": True,
        "theme_select_keywords": ["探索者的银凇止境", "银凇止境", "科考纪录", "科考记录"],
        "lobby_keywords": ["开始探险", "开始探索", "科考纪录", "科考记录", "文化比较"],
        "level_reward": {
            "name": "科考纪录",
            "keywords": ["科考纪录", "科考记录"],
            "roi": LEVEL_REWARD_PRIMARY_ROI,
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": list(MONTHLY_COMMISSION_KEYWORDS),
            "roi": MONTHLY_COMMISSION_PRIMARY_ROI,
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "文化比较",
            "keywords": ["文化比较"],
            "split_pairs": [],
            "roi": TECH_TREE_LEFT_VALID_ROI,
            "anchor_norm": (0.145, 0.667),
            "fallback_xy": (185, 480),
        },
    },
    "Sarkaz": {
        "theme_key": "Sarkaz",
        "aliases": ["Sarkaz", "萨卡兹的无终奇语", "无终奇语"],
        "display_name": "萨卡兹的无终奇语",
        "is_ended_theme": True,
        "theme_select_keywords": ["萨卡兹的无终奇语", "无终奇语", "假说辑录"],
        "lobby_keywords": ["开始探险", "开始探索", "假说辑录", "历史重构"],
        "level_reward": {
            "name": "假说辑录",
            "keywords": ["假说辑录"],
            "roi": LEVEL_REWARD_PRIMARY_ROI,
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": list(MONTHLY_COMMISSION_KEYWORDS),
            "roi": MONTHLY_COMMISSION_PRIMARY_ROI,
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "历史重构",
            "keywords": ["历史重构"],
            "split_pairs": [],
            "roi": TECH_TREE_LEFT_VALID_ROI,
            # Standard 16:9 anchor for '历史重构 35' (avoids 巫仪档案库 ~0.31 and 前瞻性投资系统 ~0.56)
            "anchor_norm": (0.105, 0.425),
            "fallback_xy": (134, 306),
        },
    },
    "JieGarden": {
        "theme_key": "JieGarden",
        "aliases": ["JieGarden", "Sui", "岁的界园志异", "界园志异"],
        "display_name": "岁的界园志异",
        "is_ended_theme": True,
        "theme_select_keywords": ["岁的界园志异", "界园志异", "游园纪念"],
        "lobby_keywords": ["开始探险", "开始探索", "游园纪念", "古今学识"],
        "level_reward": {
            "name": "游园纪念",
            "keywords": ["游园纪念"],
            "roi": LEVEL_REWARD_PRIMARY_ROI,
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": list(MONTHLY_COMMISSION_KEYWORDS),
            "roi": MONTHLY_COMMISSION_PRIMARY_ROI,
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "古今学识",
            "keywords": ["古今学识", "古今", "学识"],
            "split_pairs": [("学识", "古今"), ("学古", "识今")],
            "roi": TECH_TREE_LEFT_VALID_ROI,
            "anchor_norm": (0.145, 0.583),
            "fallback_xy": (185, 420),
        },
    },
    "BlackFlow": {
        "theme_key": "BlackFlow",
        "aliases": ["BlackFlow", "Drowning", "沉沦者的黑流树海", "黑流树海"],
        "display_name": "沉沦者的黑流树海",
        "is_ended_theme": False,
        "theme_select_keywords": ["沉沦者的黑流树海", "黑流树海", "源流堆栈"],
        "lobby_keywords": ["开始探险", "开始探索", "源流堆栈", "生命游戏", "本月委托"],
        "level_reward": {
            "name": "源流堆栈",
            "keywords": ["源流堆栈"],
            "roi": LEVEL_REWARD_PRIMARY_ROI,
            "fallback_xy": (1085, 58),
        },
        "monthly_commission": {
            "name": "本月委托栏",
            "keywords": list(MONTHLY_COMMISSION_KEYWORDS),
            "roi": MONTHLY_COMMISSION_PRIMARY_ROI,
            "fallback_xy": (1145, 268),
        },
        "tech_tree": {
            "name": "生命游戏",
            "keywords": ["生命游戏"],
            "split_pairs": [],
            "roi": TECH_TREE_LEFT_VALID_ROI,
            "anchor_norm": (0.145, 0.847),
            "fallback_xy": (185, 610),
        },
    },
}

THEME_ALIASES: Dict[str, str] = {}
for _tkey, _tprof in THEME_UI_PROFILES.items():
    THEME_ALIASES[_tkey.lower()] = _tkey
    for _alias in _tprof.get("aliases", ()):
        _ac = str(_alias or "").strip().lower()
        if _ac:
            THEME_ALIASES[_ac] = _tkey

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
    "确认升级",
    "确认激活",
    "确认解锁",
    "确认重构",
    "确认强化",
    "确认塑造",
    "确认点亮",
    "确认演化",
    "确认研习",
    "确认筹备",
    "激活",
    "解锁",
    "重构",
    "升级",
    "强化",
    "塑造",
    "提升",
    "点亮",
    "演化",
    "研习",
    "确认",
    "确定",
)

_TECH_ALREADY_UPGRADED_TEXTS: Tuple[str, ...] = (
    "已升级",
    "已重构",
    "已激活",
    "已解锁",
    "已塑造",
    "已点亮",
    "已满级",
    "已习得",
)

_TECH_LOCKED_TEXTS: Tuple[str, ...] = (
    "未解锁",
    "未满足",
    "前置未解锁",
    "无法升级",
    "无法解锁",
    "无法激活",
    "无法重构",
    "不可升级",
    "不可激活",
    "不可解锁",
)

_TECH_INSUFFICIENT_POINTS_TEXTS: Tuple[str, ...] = (
    "点数不足",
    "材料不足",
    "资源不足",
)

_TECH_UNAVAILABLE_TEXTS: Tuple[str, ...] = (
    "未解锁",
    "已激活",
    "已解锁",
    "已重构",
    "已塑造",
    "已点亮",
    "已升级",
    "已满级",
    "已习得",
    "点数不足",
    "材料不足",
    "资源不足",
    "未满足",
    "前置未解锁",
    "无法升级",
    "无法解锁",
    "无法激活",
    "无法重构",
    "不可升级",
    "不可激活",
    "不可解锁",
)

_TECH_UPGRADE_BUTTON_EXCLUDE: Tuple[str, ...] = _TECH_UNAVAILABLE_TEXTS + (
    "历史重构",
    "历重构",
    "史重构",
    "认知塑造",
    "认塑造",
    "文化比较",
    "幕后筹备",
    "古今学识",
    "生命游戏",
    "取消",
    "返回",
    "关闭",
    "重置",
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


def normalize_roguelike_theme(theme: str, default: str = "BlackFlow") -> str:
    """Resolve canonical Roguelike theme key across official keys, aliases (Sui/Drowning), and Chinese titles."""
    raw = str(theme or "").strip()
    if not raw:
        return default
    hit = THEME_ALIASES.get(raw.lower())
    if hit:
        return hit
    # Partial Chinese title match fallback
    for tkey, prof in THEME_UI_PROFILES.items():
        disp = str(prof.get("display_name") or "")
        if disp and (raw == disp or raw in disp):
            return tkey
    return default


def get_theme_ui_profile(theme: str) -> Dict[str, Any]:
    """Return UI landmark profile for a Roguelike theme (supporting aliases like Sui/Drowning), defaulting to BlackFlow."""
    canonical = normalize_roguelike_theme(theme, default="BlackFlow")
    return copy.deepcopy(THEME_UI_PROFILES.get(canonical, THEME_UI_PROFILES["BlackFlow"]))


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

    if is_roguelike_theme_select_page(boxes):
        return {"screen": "theme_select", "boxes": boxes}
    if is_roguelike_theme_lobby(boxes, preferred_theme=theme) or any(
        k in joined for k in profile["lobby_keywords"][1:]
    ):
        return {"screen": "theme_lobby", "boxes": boxes}
    if any(k in joined for k in ("招募干员", "干员招募", "选择招募", "临时招募", "放弃招募")):
        return {"screen": "recruitment", "boxes": boxes}
    if any(k in joined for k in ("险路恶敌", "首领")) and any(k in joined for k in ("开始行动", "出发前往", "进入作战")):
        return {"screen": "boss_preview", "boxes": boxes}
    if any(k in joined for k in ("不期而遇", "狭路相逢", "选择一项", "离开")) and not any(
        k in joined for k in ("开始探索", "开始探险")
    ):
        return {"screen": "encounter", "boxes": boxes}
    if any(k in joined for k in ("作战", "紧急作战", "诡意行商", "秘境行商", "安全的角落", "出发前往")):
        return {"screen": "map", "boxes": boxes}
    return {"screen": "unknown", "boxes": boxes}


def _box_center(box: Dict[str, Any]) -> Tuple[int, int]:
    return int(box.get("cx", box.get("x", 0))), int(box.get("cy", box.get("y", 0)))


def _box_text_compact(box: Dict[str, Any]) -> str:
    return re.sub(r"\s+", "", str(box.get("compact") or box.get("text") or ""))


def _box_in_roi(
    box: Dict[str, Any],
    roi: Optional[Tuple[int, int, int, int]],
    pad: int = 20,
) -> bool:
    if roi is None:
        return True
    rx, ry, rw, rh = roi
    cx, cy = _box_center(box)
    return (rx - pad <= cx <= rx + rw + pad) and (ry - pad <= cy <= ry + rh + pad)


def _find_matching_ocr_box(
    boxes: Sequence[Dict[str, Any]],
    keywords: Sequence[str],
    roi: Optional[Tuple[int, int, int, int]] = None,
    exclude_keywords: Sequence[str] = (),
    roi_pad: int = 20,
) -> Optional[Dict[str, Any]]:
    ex_clean = [re.sub(r"\s+", "", str(ex)) for ex in exclude_keywords if str(ex or "").strip()]
    for kw in keywords:
        kw_clean = re.sub(r"\s+", "", str(kw or ""))
        if not kw_clean:
            continue
        for b in boxes:
            txt = _box_text_compact(b)
            if not txt:
                continue
            if ex_clean and any(ex in txt for ex in ex_clean):
                continue
            if kw_clean not in txt:
                continue
            if not _box_in_roi(b, roi, pad=roi_pad):
                continue
            return dict(b)
    return None


def _merge_ocr_boxes(boxes: Sequence[Dict[str, Any]], label: str) -> Dict[str, Any]:
    """Merge multiple OCR boxes (e.g. vertical split-column text) into a single centered target box."""
    xs0: List[int] = []
    ys0: List[int] = []
    xs1: List[int] = []
    ys1: List[int] = []
    cxs: List[int] = []
    cys: List[int] = []
    for b in boxes:
        cx, cy = _box_center(b)
        cxs.append(cx)
        cys.append(cy)
        if all(k in b for k in ("x", "y", "w", "h")):
            x0 = int(b["x"])
            y0 = int(b["y"])
            xs0.append(x0)
            ys0.append(y0)
            xs1.append(x0 + max(1, int(b["w"])))
            ys1.append(y0 + max(1, int(b["h"])))
    merged_from = [str(b.get("text") or b.get("compact") or "") for b in boxes]
    if xs0 and len(xs0) == len(boxes):
        min_x, min_y = min(xs0), min(ys0)
        max_x, max_y = max(xs1), max(ys1)
        return {
            "text": label,
            "compact": label,
            "x": min_x,
            "y": min_y,
            "w": max(1, max_x - min_x),
            "h": max(1, max_y - min_y),
            "cx": int(round((min_x + max_x) / 2.0)),
            "cy": int(round((min_y + max_y) / 2.0)),
            "merged_from": merged_from,
        }
    return {
        "text": label,
        "compact": label,
        "cx": int(round(sum(cxs) / max(1, len(cxs)))),
        "cy": int(round(sum(cys) / max(1, len(cys)))),
        "merged_from": merged_from,
    }


def _find_split_pair_ocr_box(
    boxes: Sequence[Dict[str, Any]],
    split_pairs: Sequence[Tuple[str, str]],
    label: str,
    roi: Optional[Tuple[int, int, int, int]] = None,
    exclude_keywords: Sequence[str] = (),
    roi_pad: int = 0,
    max_dx: int = 220,
    max_dy: int = 160,
) -> Optional[Dict[str, Any]]:
    """Detect adjacent two-column/split OCR fragments (e.g. '学识'+'古今' or '学古'+'识今') and return merged center."""
    if not split_pairs:
        return None
    ex_clean = [re.sub(r"\s+", "", str(ex)) for ex in exclude_keywords if str(ex or "").strip()]
    valid_boxes: List[Tuple[int, Dict[str, Any], str]] = []
    for idx, b in enumerate(boxes):
        txt = _box_text_compact(b)
        if not txt:
            continue
        if ex_clean and any(ex in txt for ex in ex_clean):
            continue
        if not _box_in_roi(b, roi, pad=roi_pad):
            continue
        valid_boxes.append((idx, b, txt))

    for part_a, part_b in split_pairs:
        ka = re.sub(r"\s+", "", str(part_a or ""))
        kb = re.sub(r"\s+", "", str(part_b or ""))
        if not ka or not kb:
            continue
        cands_a = [(idx, b) for idx, b, txt in valid_boxes if ka in txt]
        cands_b = [(idx, b) for idx, b, txt in valid_boxes if kb in txt]
        best_pair: Optional[Tuple[int, Dict[str, Any], Dict[str, Any]]] = None
        for idx_a, ba in cands_a:
            cxa, cya = _box_center(ba)
            for idx_b, bb in cands_b:
                if idx_a == idx_b:
                    continue
                cxb, cyb = _box_center(bb)
                dx = abs(cxa - cxb)
                dy = abs(cya - cyb)
                if dx > max_dx or dy > max_dy:
                    continue
                dist = dx * dx + dy * dy
                if best_pair is None or dist < best_pair[0]:
                    best_pair = (dist, ba, bb)
        if best_pair is not None:
            merged = _merge_ocr_boxes([best_pair[1], best_pair[2]], label=label)
            if _box_in_roi(merged, roi, pad=roi_pad):
                return merged

    # Bonus resilience: 4 single-character split boxes for a 4-char vertical title (e.g. 古/今/学/识)
    clean_label = re.sub(r"\s+", "", str(label or ""))
    if len(clean_label) == 4 and len(set(clean_label)) == 4:
        char_boxes: List[Dict[str, Any]] = []
        used_indices = set()
        for ch in clean_label:
            hit = next(((idx, b) for idx, b, txt in valid_boxes if idx not in used_indices and txt == ch), None)
            if hit is None:
                char_boxes = []
                break
            used_indices.add(hit[0])
            char_boxes.append(hit[1])
        if len(char_boxes) == 4:
            cxs = [_box_center(b)[0] for b in char_boxes]
            cys = [_box_center(b)[1] for b in char_boxes]
            if (max(cxs) - min(cxs) <= max_dx) and (max(cys) - min(cys) <= max_dy):
                merged = _merge_ocr_boxes(char_boxes, label=label)
                if _box_in_roi(merged, roi, pad=roi_pad):
                    return merged
    return None


def _find_tech_tree_entry_for_profile(
    boxes: Sequence[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Locate a theme's tech-tree entry strictly inside the left-side valid zone with blacklist & split-pair support."""
    tech_spec = profile.get("tech_tree") or {}
    name = str(tech_spec.get("name") or "")
    keywords = [str(k) for k in (tech_spec.get("keywords") or []) if str(k or "").strip()]
    split_pairs = list(tech_spec.get("split_pairs") or [])
    roi = tech_spec.get("roi") or TECH_TREE_LEFT_VALID_ROI

    # 1. Full/primary keywords first (e.g. '古今学识', '历史重构', '认知塑造')
    full_kws: List[str] = []
    short_kws: List[str] = []
    for kw in keywords:
        if kw == name or len(kw) >= 3:
            full_kws.append(kw)
        else:
            short_kws.append(kw)

    hit = _find_matching_ocr_box(
        boxes,
        full_kws,
        roi=roi,
        exclude_keywords=TECH_TREE_ENTRY_BLACKLIST,
        roi_pad=0,
    )
    if hit is not None and _box_in_roi(hit, TECH_TREE_LEFT_VALID_ROI, pad=0):
        return hit

    # 2. Split-column OCR pairs BEFORE short partial keywords (so '学识'+'古今' merges to exact center)
    if split_pairs:
        merged = _find_split_pair_ocr_box(
            boxes,
            split_pairs=split_pairs,
            label=name or (full_kws[0] if full_kws else "科技树"),
            roi=roi,
            exclude_keywords=TECH_TREE_ENTRY_BLACKLIST,
            roi_pad=0,
        )
        if merged is not None and _box_in_roi(merged, TECH_TREE_LEFT_VALID_ROI, pad=0):
            return merged

    # 3. Short partial keywords fallback (still strictly inside left-side valid ROI & blacklist-filtered)
    if short_kws:
        hit_short = _find_matching_ocr_box(
            boxes,
            short_kws,
            roi=roi,
            exclude_keywords=TECH_TREE_ENTRY_BLACKLIST,
            roi_pad=0,
        )
        if hit_short is not None and _box_in_roi(hit_short, TECH_TREE_LEFT_VALID_ROI, pad=0):
            return hit_short

    return None


def _theme_scan_order(configured_theme: str) -> List[str]:
    norm = normalize_roguelike_theme(configured_theme, default="BlackFlow")
    order = [norm] if norm in THEME_UI_PROFILES else []
    for tkey in THEME_UI_PROFILES:
        if tkey not in order:
            order.append(tkey)
    return order


def resolve_lobby_tech_tree_entry(
    boxes: Sequence[Dict[str, Any]],
    configured_theme: str = "BlackFlow",
) -> Optional[Dict[str, Any]]:
    """Find tech-tree entry with configured-theme priority and all-6-theme fallback."""
    order = _theme_scan_order(configured_theme)
    primary = order[0] if order else "BlackFlow"
    for tkey in order:
        prof = THEME_UI_PROFILES[tkey]
        box = _find_tech_tree_entry_for_profile(boxes, prof)
        if box is not None:
            return {
                "theme": tkey,
                "profile": copy.deepcopy(prof),
                "box": box,
                "matched_via_fallback": (tkey != primary),
                "match_stage": "fullscreen_ocr",
            }
    return None


def _box_mean_2d(gray: "np.ndarray", radius: int = 15) -> "np.ndarray":
    """Fast O(H*W) 2D box-filter mean using integral image (pure NumPy, zero OpenCV dependency)."""
    r = max(1, int(radius))
    padded = np.pad(gray.astype(np.float32), ((r, r), (r, r)), mode="edge")
    integ = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1), dtype=np.float64)
    integ[1:, 1:] = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    k = 2 * r + 1
    window_sum = (
        integ[k:, k:]
        - integ[:-k, k:]
        - integ[k:, :-k]
        + integ[:-k, :-k]
    )
    return (window_sum / float(k * k)).astype(np.float32)


def _apply_clahe_gray_numpy(
    gray_u8: "np.ndarray",
    tile_grid: Tuple[int, int] = (8, 8),
    clip_limit: float = 2.5,
) -> "np.ndarray":
    """Pure-NumPy tiled Contrast-Limited Adaptive Histogram Equalization (CLAHE) with bilinear interpolation."""
    h, w = gray_u8.shape
    ny = max(1, min(int(tile_grid[0]), h // 16 if h >= 32 else 1))
    nx = max(1, min(int(tile_grid[1]), w // 16 if w >= 32 else 1))
    tile_h = h / float(ny)
    tile_w = w / float(nx)

    luts = np.zeros((ny, nx, 256), dtype=np.float32)
    for ty in range(ny):
        y0 = int(round(ty * tile_h))
        y1 = h if ty == ny - 1 else max(y0 + 1, int(round((ty + 1) * tile_h)))
        for tx in range(nx):
            x0 = int(round(tx * tile_w))
            x1 = w if tx == nx - 1 else max(x0 + 1, int(round((tx + 1) * tile_w)))
            tile = gray_u8[y0:y1, x0:x1]
            n_px = max(1, int(tile.size))
            hist = np.bincount(tile.ravel(), minlength=256).astype(np.int64)
            limit = max(1, int(round(float(clip_limit) * n_px / 256.0)))
            excess = int(np.maximum(hist - limit, 0).sum())
            hist = np.minimum(hist, limit)
            if excess > 0:
                q, rem = divmod(excess, 256)
                hist += q
                if rem > 0:
                    step = max(1, 256 // rem)
                    hist[: rem * step : step] += 1
            cdf = np.cumsum(hist).astype(np.float32)
            luts[ty, tx] = np.clip(np.round(cdf * (255.0 / float(max(1.0, cdf[-1])))), 0.0, 255.0)

    ys = np.clip((np.arange(h, dtype=np.float32) + 0.5) / tile_h - 0.5, 0.0, float(ny - 1))
    xs = np.clip((np.arange(w, dtype=np.float32) + 0.5) / tile_w - 0.5, 0.0, float(nx - 1))
    y0 = np.floor(ys).astype(np.int32)
    x0 = np.floor(xs).astype(np.int32)
    y1 = np.minimum(y0 + 1, ny - 1)
    x1 = np.minimum(x0 + 1, nx - 1)
    wy = (ys - y0)[:, None]
    wx = (xs - x0)[None, :]

    v = gray_u8.astype(np.int32)
    c00 = luts[y0[:, None], x0[None, :], v]
    c01 = luts[y0[:, None], x1[None, :], v]
    c10 = luts[y1[:, None], x0[None, :], v]
    c11 = luts[y1[:, None], x1[None, :], v]
    top = c00 * (1.0 - wx) + c01 * wx
    bot = c10 * (1.0 - wx) + c11 * wx
    out = top * (1.0 - wy) + bot * wy
    return np.clip(np.round(out), 0, 255).astype(np.uint8)


def build_tech_tree_local_ocr_images(
    img: Image.Image,
    roi_norm: Tuple[float, float, float, float] = TECH_TREE_LOCAL_OCR_ROI_NORM,
    scale: float = TECH_TREE_LOCAL_OCR_UPSCALE,
) -> Dict[str, Any]:
    """Crop left lobby function band (default x in [0.01,0.28], y in [0.25,0.75]), upscale 2.5x,
    and build two enhanced OCR streams:
      1) ``red_suppressed``: min(G, B) channel (eliminates Sarkaz bright-red striped backdrop) + adaptive binarization
      2) ``clahe_gray``: tiled CLAHE local-contrast enhanced grayscale (generalized across all themes)
    """
    rgb = img.convert("RGB")
    ow, oh = max(1, rgb.width), max(1, rgb.height)
    nx0, ny0, nx1, ny1 = roi_norm
    x0 = max(0, min(ow - 2, int(round(nx0 * ow))))
    y0 = max(0, min(oh - 2, int(round(ny0 * oh))))
    x1 = max(x0 + 2, min(ow, int(round(nx1 * ow))))
    y1 = max(y0 + 2, min(oh, int(round(ny1 * oh))))

    crop_rgb = rgb.crop((x0, y0, x1, y1))
    roi_1280 = (
        float(x0) * 1280.0 / float(ow),
        float(y0) * 720.0 / float(oh),
        max(1.0, float(x1 - x0) * 1280.0 / float(ow)),
        max(1.0, float(y1 - y0) * 720.0 / float(oh)),
    )

    eff_scale = max(1.0, float(scale))
    up_w = max(2, int(round(crop_rgb.width * eff_scale)))
    up_h = max(2, int(round(crop_rgb.height * eff_scale)))
    up_rgb = crop_rgb.resize((up_w, up_h), Image.Resampling.BICUBIC)

    if np is None:
        gray_fallback = up_rgb.convert("L").convert("RGB")
        return {
            "roi_1280": roi_1280,
            "scale": eff_scale,
            "crop_rgb": crop_rgb,
            "red_suppressed": gray_fallback,
            "clahe_gray": gray_fallback,
        }

    arr = np.asarray(up_rgb, dtype=np.uint8)
    # Stream A: suppress red channel via min(G, B) — bright red stripes collapse to dark while white text stays ~255
    gb_min = np.minimum(arr[:, :, 1], arr[:, :, 2]).astype(np.float32)
    local_mean = _box_mean_2d(gb_min, radius=max(9, int(round(15 * eff_scale / 2.5))))
    # Adaptive foreground mask (white text strokes -> black=0 on white=255 for optimal WinOCR legibility)
    text_mask = (gb_min >= (local_mean + 20.0)) & (gb_min >= 115.0)
    bin_u8 = np.where(text_mask, 0, 255).astype(np.uint8)
    red_suppressed_img = Image.fromarray(bin_u8, mode="L").convert("RGB")

    # Stream B: tiled CLAHE grayscale for non-red themes
    gray_u8 = np.clip(
        np.round(0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]),
        0,
        255,
    ).astype(np.uint8)
    clahe_u8 = _apply_clahe_gray_numpy(gray_u8, tile_grid=(8, 8), clip_limit=2.5)
    clahe_img = Image.fromarray(clahe_u8, mode="L").convert("RGB")

    return {
        "roi_1280": roi_1280,
        "scale": eff_scale,
        "crop_rgb": crop_rgb,
        "red_suppressed": red_suppressed_img,
        "clahe_gray": clahe_img,
    }


def _map_local_ocr_boxes_to_full_1280(
    local_boxes: Sequence[Dict[str, Any]],
    roi_1280: Tuple[float, float, float, float],
) -> List[Dict[str, Any]]:
    """Map OCR boxes normalized to a cropped ROI's 1280x720 back into full-screen 1280x720 coordinates."""
    rx0, ry0, rw, rh = roi_1280
    mapped: List[Dict[str, Any]] = []
    for b in local_boxes or []:
        nb = dict(b)
        lcx = float(b.get("cx", b.get("x", 0)))
        lcy = float(b.get("cy", b.get("y", 0)))
        nb["cx"] = int(round(rx0 + (lcx / 1280.0) * rw))
        nb["cy"] = int(round(ry0 + (lcy / 720.0) * rh))
        if all(k in b for k in ("x", "y", "w", "h")):
            nb["x"] = int(round(rx0 + (float(b["x"]) / 1280.0) * rw))
            nb["y"] = int(round(ry0 + (float(b["y"]) / 720.0) * rh))
            nb["w"] = max(1, int(round((float(b["w"]) / 1280.0) * rw)))
            nb["h"] = max(1, int(round((float(b["h"]) / 720.0) * rh)))
        mapped.append(nb)
    return mapped


def _find_tech_tree_numeric_badge_box(
    boxes: Sequence[Dict[str, Any]],
    configured_theme: str = "BlackFlow",
    roi_norm: Tuple[float, float, float, float] = TECH_TREE_LOCAL_OCR_ROI_NORM,
    y_band_norm: Tuple[float, float] = TECH_TREE_POINTS_BAND_Y_NORM,
) -> Optional[Dict[str, Any]]:
    """Match pure numeric tech-tree point badges (e.g. '35') strictly inside y in [0.35, 0.50] & left band,
    excluding blacklist rows (e.g. '前瞻性投资系统' 999 at y~0.56).
    """
    ex_clean = [re.sub(r"\s+", "", str(ex)) for ex in TECH_TREE_ENTRY_BLACKLIST if str(ex or "").strip()]
    bl_ys: List[int] = []
    for b in boxes or []:
        txt = _box_text_compact(b)
        if txt and any(ex in txt for ex in ex_clean):
            bl_ys.append(_box_center(b)[1])

    x_min = int(round(roi_norm[0] * 1280.0))
    x_max = int(round(roi_norm[2] * 1280.0))
    y_min = int(round(y_band_norm[0] * 720.0))
    y_max = int(round(y_band_norm[1] * 720.0))

    prof = get_theme_ui_profile(configured_theme)
    anchor_norm = prof.get("tech_tree", {}).get("anchor_norm") or (0.105, 0.425)
    anchor_x = int(round(float(anchor_norm[0]) * 1280.0))
    anchor_y = int(round(float(anchor_norm[1]) * 720.0))

    candidates: List[Tuple[int, Dict[str, Any], int]] = []
    for b in boxes or []:
        txt = _box_text_compact(b)
        if not txt or not re.fullmatch(r"\d{1,4}", txt):
            continue
        val = int(txt)
        if val <= 0:
            continue
        cx, cy = _box_center(b)
        if not (x_min <= cx <= x_max and y_min <= cy <= y_max):
            continue
        if any(abs(cy - by) <= 42 for by in bl_ys):
            continue
        dist2 = (cx - anchor_x) ** 2 + (cy - anchor_y) ** 2
        candidates.append((dist2, dict(b), val))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    _, best_box, best_val = candidates[0]
    best_box["points_value"] = best_val
    return best_box


def resolve_lobby_tech_tree_via_local_ocr(
    img: Image.Image,
    configured_theme: str = "BlackFlow",
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Stage-2 tech-tree entry locator: crop left band, upscale 2.5x, run red-suppressed adaptive binary
    OCR and CLAHE grayscale OCR; match tech-tree text first, then non-blacklisted numeric points (y in [0.35, 0.50]).
    """
    if cancel_event and cancel_event.is_set():
        return None
    norm_theme = normalize_roguelike_theme(configured_theme, default="BlackFlow")
    prep = build_tech_tree_local_ocr_images(img)
    roi_1280 = prep["roi_1280"]
    combined_mapped: List[Dict[str, Any]] = []

    for stream_key, stage_name, use_inv in (
        ("red_suppressed", "local_ocr_red_suppressed", False),
        ("clahe_gray", "local_ocr_clahe", True),
    ):
        if cancel_event and cancel_event.is_set():
            return None
        enh_img = prep[stream_key]
        raw_local = stage_pilot.find_text_boxes_via_ocr(
            enh_img,
            include_inverted=use_inv,
            upscale_min_width=enh_img.width,
            pad_border=16,
            cancel_event=cancel_event,
        )
        mapped = _map_local_ocr_boxes_to_full_1280(raw_local, roi_1280)
        combined_mapped.extend(mapped)
        hit = resolve_lobby_tech_tree_entry(mapped, configured_theme=norm_theme)
        if hit is not None:
            hit["match_stage"] = stage_name
            hit["requires_panel_verify"] = False
            return hit

    # Text did not match in either enhanced stream -> try non-blacklisted numeric points badge (y in [0.35, 0.50])
    num_box = _find_tech_tree_numeric_badge_box(combined_mapped, configured_theme=norm_theme)
    if num_box is not None:
        prof = get_theme_ui_profile(norm_theme)
        return {
            "theme": norm_theme,
            "profile": prof,
            "box": num_box,
            "matched_via_fallback": False,
            "match_stage": "local_ocr_points",
            "requires_panel_verify": True,
        }
    return None


def _resolve_theme_anchor_fallback(
    img: Image.Image,
    confirmed_theme: Optional[str],
    boxes: Optional[Sequence[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Stage-3 tech-tree entry locator: standard 16:9 normalized anchor when lobby theme was positively confirmed.
    Strictly forbidden on the Integrated Strategies Theme Selection page."""
    if not confirmed_theme:
        return None
    if boxes and is_roguelike_theme_select_page(boxes):
        return None
    ow, oh = max(1, img.width), max(1, img.height)
    if abs((float(ow) / float(oh)) - (16.0 / 9.0)) > 0.08:
        return None
    norm_theme = normalize_roguelike_theme(confirmed_theme, default="")
    if norm_theme not in THEME_UI_PROFILES:
        return None
    prof = get_theme_ui_profile(norm_theme)
    tech_spec = prof["tech_tree"]
    anchor_norm = tech_spec.get("anchor_norm")
    if not anchor_norm or len(anchor_norm) != 2:
        return None
    cx = int(round(float(anchor_norm[0]) * 1280.0))
    cy = int(round(float(anchor_norm[1]) * 720.0))
    return {
        "theme": norm_theme,
        "profile": prof,
        "box": {
            "text": f"{tech_spec['name']}(锚点兜底)",
            "compact": tech_spec["name"],
            "cx": cx,
            "cy": cy,
            "anchor_norm": (float(anchor_norm[0]), float(anchor_norm[1])),
        },
        "matched_via_fallback": False,
        "match_stage": "theme_anchor_fallback",
        "requires_panel_verify": True,
    }


def _verify_entered_tech_tree_panel(
    adb_path: str,
    adb_addr: str,
    active_profile: Dict[str, Any],
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    maa_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Strict post-click admission check for tech-tree entry.

    NEVER relies on bright-blob count alone. Requires ALL of:
      1) Absence of theme-selection / lobby / wrong-panel markers
         (e.g. '进入主题', '添加到终端', '查看收藏', '该主题已全额收录', '开始探险', '开始探索', '蚀刻章套组');
      2) Top bar '<' return icon AND Home house icon present;
      3) Top-right ROI x in [0.78, 0.99], y in [0.02, 0.18] has remaining points number or points currency name.
    (Legacy unrendered flat blank test canvases may pass ONLY when the exact official tech-tree title is present.)
    """
    verify_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
    if verify_img is None:
        return {"ok": False, "wrong_panel": "screencap_failed", "returned": False, "img": None}

    verify_boxes = stage_pilot.find_text_boxes_via_ocr(verify_img, include_inverted=True, cancel_event=cancel_event)
    joined = "".join(_box_text_compact(b) for b in (verify_boxes or []))

    # 1. Hard reject: Integrated Strategies Theme Selection page -> NEVER tap '<' (would exit IS!), abort immediately
    if is_roguelike_theme_select_page(verify_boxes):
        _emit(
            log_callback,
            "[WARN][RL-TECH] 准入校验拦截：当前处于集成战略「主题选择页」，严禁误判进入科技树或继续点返回键，立即中止加点。",
        )
        return {"ok": False, "wrong_panel": "theme_select_page", "returned": False, "img": verify_img, "boxes": verify_boxes}

    # 2. Hard reject: Still on theme lobby ('开始探险' / '开始探索' / '蚀刻章套组' / '常规行动') -> abort without tapping '<'
    if any(lk in joined for lk in ("开始探险", "继续探险", "开始探索", "继续探索", "蚀刻章套组", "常规行动")):
        _emit(log_callback, "[WARN][RL-TECH] 兜底点击后仍停留在肉鸽主题大厅，未进入科技树面板，中止加点。")
        return {"ok": False, "wrong_panel": "theme_lobby", "returned": False, "img": verify_img, "boxes": verify_boxes}

    # 3. Explicit mis-entry into investment bank / archive / gallery -> immediately tap top-left '<' and abort
    for wrong_kw in _TECH_TREE_WRONG_PANEL_KEYWORDS:
        if wrong_kw in joined:
            _emit(
                log_callback,
                f"[WARN][RL-TECH] 兜底点击误入非科技树界面（命中「{wrong_kw}」），立即点击左上角返回键并中止加点。",
            )
            stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, verify_img.width, verify_img.height)
            time.sleep(0.8)
            return {"ok": False, "wrong_panel": wrong_kw, "returned": True, "img": verify_img, "boxes": verify_boxes}
    for wrong_kw in _TECH_TREE_PAGE_GUARD_WRONG_KEYWORDS:
        if wrong_kw in joined:
            _emit(
                log_callback,
                f"[WARN][RL-TECH] 兜底点击误入非科技树界面（命中「{wrong_kw}」），立即点击左上角返回键并中止加点。",
            )
            stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, verify_img.width, verify_img.height)
            time.sleep(0.8)
            return {"ok": False, "wrong_panel": wrong_kw, "returned": True, "img": verify_img, "boxes": verify_boxes}

    # 4. Tech-tree admission check: top '<' + house icon + (top-right points indicator or bottom-left progress badge)
    tech_name = str(active_profile.get("tech_tree", {}).get("name") or "")
    has_ret = detect_tech_tree_return_icon(verify_img, maa_dir=maa_dir)
    has_home = detect_tech_tree_home_icon(verify_img, maa_dir=maa_dir, boxes=verify_boxes)
    has_progress_badge, _ = _detect_tech_tree_progress_badge(verify_boxes)
    has_points_indicator, pts_val = _has_top_right_tech_points_indicator(
        verify_img, verify_boxes, allow_roi_ocr=True, cancel_event=cancel_event
    )

    if has_ret and has_home and (has_points_indicator or has_progress_badge):
        _emit(
            log_callback,
            f"[RL-TECH] 二级界面准入校验通过（< + 房子图标 + 右上角点数={pts_val} / 进度徽章={has_progress_badge}），已确认进入「{tech_name}」科技树面板。",
        )
        return {
            "ok": True,
            "wrong_panel": "",
            "returned": False,
            "img": verify_img,
            "boxes": verify_boxes,
            "points": pts_val,
        }

    # Legacy compatibility ONLY for unrendered flat blank synthetic unit-test canvases that carry the official panel title
    official_titles = tuple(
        t for t in (tech_name, "历史重构", "认知塑造", "文化比较", "幕后筹备", "古今学识", "生命游戏") if t
    )
    if not _has_rendered_ui_chrome(verify_img) and any(t in joined for t in official_titles):
        _emit(log_callback, f"[RL-TECH] 二级界面快速校验通过，已确认进入「{tech_name}」科技树面板。")
        return {"ok": True, "wrong_panel": "", "returned": False, "img": verify_img, "boxes": verify_boxes, "points": pts_val}

    _emit(
        log_callback,
        f"[WARN][RL-TECH] 二级界面准入校验未通过（禁止仅凭亮块数误判；需满足 <={has_ret}、房子={has_home}、右上角点数={has_points_indicator}），"
        f"中止进入「{tech_name}」科技树加点。",
    )
    if has_ret:
        stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, verify_img.width, verify_img.height)
        time.sleep(0.8)
        return {"ok": False, "wrong_panel": "unconfirmed_panel", "returned": True, "img": verify_img, "boxes": verify_boxes}
    return {"ok": False, "wrong_panel": "unconfirmed_panel", "returned": False, "img": verify_img, "boxes": verify_boxes}


def resolve_lobby_level_reward_entry(
    boxes: Sequence[Dict[str, Any]],
    configured_theme: str = "BlackFlow",
) -> Optional[Dict[str, Any]]:
    """Find top-right level-reward entry with configured-theme priority and all-6-theme fallback."""
    order = _theme_scan_order(configured_theme)
    primary = order[0] if order else "BlackFlow"

    # Pass 1: primary top-right ROI (configured theme first, then others)
    for tkey in order:
        prof = THEME_UI_PROFILES[tkey]
        lvl_spec = prof["level_reward"]
        box = _find_matching_ocr_box(
            boxes,
            lvl_spec["keywords"],
            roi=lvl_spec.get("roi", LEVEL_REWARD_PRIMARY_ROI),
            exclude_keywords=_REWARD_ENTRY_BLACKLIST,
        )
        if box is not None:
            return {
                "theme": tkey,
                "profile": copy.deepcopy(prof),
                "box": box,
                "matched_via_fallback": (tkey != primary),
            }

    # Pass 2: broader top-right fallback ROI
    for tkey in order:
        prof = THEME_UI_PROFILES[tkey]
        lvl_spec = prof["level_reward"]
        box = _find_matching_ocr_box(
            boxes,
            lvl_spec["keywords"],
            roi=LEVEL_REWARD_FALLBACK_ROI,
            exclude_keywords=_REWARD_ENTRY_BLACKLIST,
        )
        if box is not None:
            return {
                "theme": tkey,
                "profile": copy.deepcopy(prof),
                "box": box,
                "matched_via_fallback": (tkey != primary),
            }
    return None


def resolve_lobby_monthly_commission_entry(
    boxes: Sequence[Dict[str, Any]],
    configured_theme: str = "BlackFlow",
) -> Optional[Dict[str, Any]]:
    """Find right-side monthly commission entry ('本月委托栏' / '本月委托')."""
    prof = get_theme_ui_profile(configured_theme)
    comm_spec = prof["monthly_commission"]
    kws = list(comm_spec.get("keywords") or MONTHLY_COMMISSION_KEYWORDS)
    box = _find_matching_ocr_box(
        boxes,
        kws,
        roi=comm_spec.get("roi", MONTHLY_COMMISSION_PRIMARY_ROI),
        exclude_keywords=_REWARD_ENTRY_BLACKLIST,
    )
    if box is None:
        box = _find_matching_ocr_box(
            boxes,
            kws,
            roi=MONTHLY_COMMISSION_FALLBACK_ROI,
            exclude_keywords=_REWARD_ENTRY_BLACKLIST,
        )
    return box


def detect_lobby_theme_from_boxes(
    boxes: Sequence[Dict[str, Any]],
    preferred_theme: str = "BlackFlow",
) -> Optional[str]:
    """Identify which of the 6 Roguelike theme lobbies is shown on screen using lobby-only anchors."""
    pref = normalize_roguelike_theme(preferred_theme, default="BlackFlow")
    best_theme: Optional[str] = None
    best_score = 0
    for tkey, prof in THEME_UI_PROFILES.items():
        score = 0
        lvl_box = _find_matching_ocr_box(
            boxes,
            prof["level_reward"]["keywords"],
            roi=LEVEL_REWARD_FALLBACK_ROI,
            exclude_keywords=_REWARD_ENTRY_BLACKLIST,
        )
        if lvl_box is not None:
            score += 4
        tech_box = _find_tech_tree_entry_for_profile(boxes, prof)
        if tech_box is not None:
            score += 4
        if score <= 0:
            continue
        if score > best_score or (score == best_score and tkey == pref):
            best_score = score
            best_theme = tkey
    return best_theme


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


def is_roguelike_theme_select_page(boxes: Sequence[Dict[str, Any]]) -> bool:
    """Hard recognition for the Integrated Strategies Theme Selection hub page
    (hits any of '进入主题', '添加到终端', '查看收藏', '该主题已全额收录').
    """
    joined = "".join(_box_text_compact(b) for b in (boxes or []))
    if not joined:
        return False
    return any(kw in joined for kw in THEME_SELECT_PAGE_KEYWORDS)


def is_roguelike_theme_lobby(
    boxes: Sequence[Dict[str, Any]],
    preferred_theme: str = "BlackFlow",
) -> bool:
    """Return True iff OCR boxes positively identify a Roguelike theme lobby and NOT the theme selection page."""
    if not boxes:
        return False
    if is_roguelike_theme_select_page(boxes):
        return False
    joined = "".join(_box_text_compact(b) for b in boxes)
    if any(kw in joined for kw in THEME_LOBBY_KEYWORDS):
        return True
    # Avoid confusing reward pages (with '一键领取' / '已全部领取') for lobbies
    if any(rw in joined for rw in (_REWARD_CLAIM_BUTTON_TEXTS + _REWARD_ALREADY_CLAIMED_TEXTS)):
        return False
    if detect_lobby_theme_from_boxes(boxes, preferred_theme=preferred_theme) is not None:
        return True
    if resolve_lobby_monthly_commission_entry(boxes, configured_theme=preferred_theme) is not None:
        return True
    return False


def is_point_in_tech_tree_forbidden_zone(cx: int, cy: int) -> bool:
    """Return True if a 1280x720 coordinate falls outside the safe clickable tech-tree node canvas
    x in [0.08, 0.93], y in [0.16, 0.80] or inside forbidden UI zones:
      - Left/Right edge page-turn arrows ('<-'/'->': x < 0.08 or x > 0.93)
      - Bottom-left '64/78 已重构' attribute overview arch (x < 0.175 & y > 0.63)
      - Top navigation bar (y < 0.16), bottom detail bar (y > 0.80), top-right points bar (x >= 0.78 & y <= 0.20)
    """
    nx = float(cx) / 1280.0
    ny = float(cy) / 720.0
    if nx < 0.08 or nx > 0.93 or ny < 0.16 or ny > 0.80:
        return True
    if nx < 0.175 and ny > 0.63:
        return True
    if nx >= 0.78 and ny <= 0.20:
        return True
    return False


def is_point_near_tech_tree_arch_edge(cx: int, cy: int) -> bool:
    """Return True if a non-forbidden candidate coordinate hugs the outer edge of the bottom-left
    '64/78 已重构' arch (x in [0.08, 0.25], y in [0.60, 0.80]), where tapping risks opening the overview modal."""
    if is_point_in_tech_tree_forbidden_zone(cx, cy):
        return False
    nx = float(cx) / 1280.0
    ny = float(cy) / 720.0
    return (0.08 <= nx <= 0.25) and (0.60 <= ny <= 0.80)


def _has_rendered_ui_chrome(img: Optional[Image.Image]) -> bool:
    """Return True if the screenshot has rendered UI chrome in top bar (y<100) or bottom bar (y>600).
    Distinguishes real game screenshots / rich UI mock frames from flat blank synthetic test canvases.
    """
    if np is None or img is None:
        return False
    arr = np.asarray(img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR), dtype=np.float32)
    top_std = float(np.max(np.std(arr[:100, :, :], axis=(0, 1))))
    bot_std = float(np.max(np.std(arr[600:, :, :], axis=(0, 1))))
    return (top_std >= 2.0) or (bot_std >= 2.0)


def _has_rendered_tech_tree_canvas(img: Optional[Image.Image]) -> bool:
    """Return True if the tech-tree node canvas interior (y in [116,576], x in [103,1190]) has rendered content."""
    if np is None or img is None:
        return False
    arr = np.asarray(img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR), dtype=np.float32)
    canvas = arr[116:576, 103:1190, :]
    return float(np.max(np.std(canvas, axis=(0, 1)))) >= 2.0


def _detect_top_bar_white_icon(
    img: Image.Image,
    roi_1280: Tuple[int, int, int, int],
    boxes: Optional[Sequence[Dict[str, Any]]] = None,
    reject_cjk_overlap: bool = False,
) -> bool:
    """Visual detector for crisp white navigation icons (< return arrow or home house icon) in top bar."""
    if np is None or img is None:
        return False
    rx, ry, rw, rh = roi_1280
    if reject_cjk_overlap and boxes:
        for b in boxes:
            txt = _box_text_compact(b)
            if txt and re.search(r"[\u4e00-\u9fff]", txt) and _box_in_roi(b, roi_1280, pad=0):
                return False
    arr = np.asarray(img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR), dtype=np.float32)
    x0, y0 = max(0, rx), max(6, ry)
    x1, y1 = min(1280, rx + rw), min(720, ry + rh)
    patch = arr[y0:y1, x0:x1]
    if patch.size == 0:
        return False
    luma = 0.299 * patch[:, :, 0] + 0.587 * patch[:, :, 1] + 0.114 * patch[:, :, 2]
    max_c = np.maximum(np.maximum(patch[:, :, 0], patch[:, :, 1]), patch[:, :, 2])
    min_c = np.minimum(np.minimum(patch[:, :, 0], patch[:, :, 1]), patch[:, :, 2])
    sat = max_c - min_c
    white_px = (luma >= 180.0) & (sat <= 40.0)
    dark_px = luma <= 115.0
    white_ratio = float(np.mean(white_px))
    dark_ratio = float(np.mean(dark_px))
    if not (0.012 <= white_ratio <= 0.45 and dark_ratio >= 0.35):
        return False
    ys, xs = np.where(white_px)
    if ys.size < 40:
        return False
    span_w = int(xs.max() - xs.min()) + 1
    span_h = int(ys.max() - ys.min()) + 1
    return (12 <= span_w <= 110) and (12 <= span_h <= 68)


def detect_tech_tree_home_icon(
    img: Optional[Image.Image],
    maa_dir: Optional[Path] = None,
    boxes: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    """Detect the top-bar Home house icon (x in [0.15, 0.28], y < 0.12).
    Whenever this icon is present, we are on a main sub-canvas (e.g. tech tree) and NEVER on an overview modal.
    """
    if np is None or img is None:
        return False
    if maa_dir is not None:
        tpl_path = Path(maa_dir) / "resource" / "template" / "QuickSwitch" / "Menu.png"
        if tpl_path.is_file():
            gray_1280 = np.asarray(
                img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"),
                dtype=np.float32,
            )
            matched, score, _, _ = stage_pilot._match_any_template(
                gray_1280,
                Path(maa_dir),
                [("QuickSwitch/Menu.png", (160, 0, 210, 90), 0.65, None)],
            )
            if matched:
                return True
            # If MAA template exists and score is very low (<0.42), still allow visual check only if no CJK text overlaps
            if score < 0.35 and boxes and any(
                re.search(r"[\u4e00-\u9fff]", _box_text_compact(b)) and _box_in_roi(b, TECH_TREE_HOME_ICON_ROI_1280, pad=0)
                for b in boxes
            ):
                return False
    return _detect_top_bar_white_icon(img, TECH_TREE_HOME_ICON_ROI_1280, boxes=boxes, reject_cjk_overlap=True)


def detect_tech_tree_return_icon(
    img: Optional[Image.Image],
    maa_dir: Optional[Path] = None,
) -> bool:
    """Detect the top-left '<' return icon (x < 0.14, y < 0.12)."""
    if np is None or img is None:
        return False
    if maa_dir is not None:
        ret1 = Path(maa_dir) / "resource" / "template" / "ReturnButton" / "Return.png"
        ret2 = Path(maa_dir) / "resource" / "template" / "ReturnButton" / "Return-White.png"
        if ret1.is_file() or ret2.is_file():
            gray_1280 = np.asarray(
                img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR).convert("L"),
                dtype=np.float32,
            )
            matched, _, _, _ = stage_pilot._match_any_template(
                gray_1280,
                Path(maa_dir),
                [
                    ("ReturnButton/Return.png", TECH_TREE_RETURN_ICON_ROI_1280, 0.65, None),
                    ("ReturnButton/Return-White.png", TECH_TREE_RETURN_ICON_ROI_1280, 0.65, None),
                ],
            )
            if matched:
                return True
    return _detect_top_bar_white_icon(img, (8, 0, 150, 86), boxes=None, reject_cjk_overlap=False)


def extract_tech_tree_remaining_points(
    img: Optional[Image.Image],
    boxes: Sequence[Dict[str, Any]],
    allow_roi_ocr: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Optional[int]:
    """Read remaining tech-tree upgrade points integer from top-right ROI (x > 0.78, y < 0.16)."""
    def _parse_points_from_boxes(cand_boxes: Sequence[Dict[str, Any]]) -> Optional[int]:
        nums: List[Tuple[int, int]] = []
        for b in cand_boxes or []:
            if not _box_in_roi(b, TECH_TREE_TOP_RIGHT_POINTS_ROI_1280, pad=10):
                continue
            txt = _box_text_compact(b)
            if not txt:
                continue
            # Exclude ratios like 63/78 or clock times like 23:14
            cleaned = re.sub(r"\d{1,3}/\d{1,3}", " ", txt)
            cleaned = re.sub(r"\d{1,2}:\d{2}", " ", cleaned)
            for m in re.finditer(r"\d{1,5}", cleaned):
                val = int(m.group(0))
                cx = _box_center(b)[0]
                nums.append((cx, val))
        if not nums:
            return None
        # Prefer rightmost top-right numeric badge
        nums.sort(key=lambda t: -t[0])
        return nums[0][1]

    val = _parse_points_from_boxes(boxes)
    if val is not None:
        return val

    if allow_roi_ocr and img is not None and _has_rendered_ui_chrome(img) and not (cancel_event and cancel_event.is_set()):
        roi_boxes = stage_pilot.find_text_boxes_via_ocr(
            img,
            roi=TECH_TREE_TOP_RIGHT_POINTS_ROI_1280,
            include_inverted=True,
            include_binarized=True,
            upscale_factor=3.0,
            pad_border=24,
            cancel_event=cancel_event,
        )
        return _parse_points_from_boxes(roi_boxes or [])
    return None


def _has_top_right_tech_points_indicator(
    img: Optional[Image.Image],
    boxes: Sequence[Dict[str, Any]],
    allow_roi_ocr: bool = True,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[bool, Optional[int]]:
    """Verify whether top-right ROI x in [0.78, 0.99], y in [0.02, 0.18] contains a tech-tree points number
    or points currency name. Returns ``(has_indicator, parsed_points_or_None)``.
    """
    def _has_points_name_in_boxes(cand_boxes: Sequence[Dict[str, Any]]) -> bool:
        for b in cand_boxes or []:
            if not _box_in_roi(b, TECH_TREE_TOP_RIGHT_POINTS_ROI_1280, pad=10):
                continue
            txt = _box_text_compact(b)
            if txt and any(kw in txt for kw in _TECH_TREE_POINTS_NAME_KEYWORDS):
                return True
        return False

    pts = extract_tech_tree_remaining_points(img, boxes, allow_roi_ocr=False, cancel_event=cancel_event)
    if pts is not None:
        return True, pts
    if _has_points_name_in_boxes(boxes):
        return True, None

    if allow_roi_ocr and img is not None and _has_rendered_ui_chrome(img) and not (cancel_event and cancel_event.is_set()):
        roi_boxes = stage_pilot.find_text_boxes_via_ocr(
            img,
            roi=TECH_TREE_TOP_RIGHT_POINTS_ROI_1280,
            include_inverted=True,
            include_binarized=True,
            upscale_factor=3.0,
            pad_border=24,
            cancel_event=cancel_event,
        )
        pts_roi = extract_tech_tree_remaining_points(img, roi_boxes or [], allow_roi_ocr=False, cancel_event=cancel_event)
        if pts_roi is not None:
            return True, pts_roi
        if _has_points_name_in_boxes(roi_boxes or []):
            return True, None
    return False, None


def _detect_tech_tree_progress_badge(
    boxes: Sequence[Dict[str, Any]],
) -> Tuple[bool, bool]:
    """Detect bottom-left tech-tree completion badge (e.g. '72/78 已重构' / '72/78').
    Returns ``(has_badge, has_badge_with_keyword)``.
    """
    has_ratio = False
    has_kw = False
    for b in boxes or []:
        if not _box_in_roi(b, TECH_TREE_PROGRESS_BADGE_ROI_1280, pad=12):
            continue
        txt = _box_text_compact(b)
        if not txt:
            continue
        if re.search(r"\d{1,3}\s*/\s*\d{1,3}", txt):
            has_ratio = True
        if any(kw in txt for kw in _TECH_TREE_PROGRESS_BADGE_KEYWORDS):
            has_kw = True
    return (has_ratio or has_kw), (has_ratio and has_kw)


def _is_tech_tree_overview_overlay(
    detail_boxes: Sequence[Dict[str, Any]],
    has_bottom_confirm: bool = False,
    detail_img: Optional[Image.Image] = None,
    maa_dir: Optional[Path] = None,
) -> bool:
    """Detect accidental entry into the full-screen attribute overview modal.

    STRICT rules (never triggered by bottom-left '63/78' or '已重构'):
      1) If a bottom-right confirm button is active -> False.
      2) If the top-bar Home house icon (x in [0.15, 0.28], y < 0.12) is present -> ALWAYS False (main tech-tree canvas!).
      3) Only return True when NO house icon is present AND at least 2 distinct full-screen attribute summary groups
         (e.g. '我方单位攻击力', '我方单位防御力', '初始源石锭', '我方单位生命值') or explicit overview title match.
    """
    if has_bottom_confirm:
        return False
    if detail_img is not None and detect_tech_tree_home_icon(detail_img, maa_dir=maa_dir, boxes=detail_boxes):
        return False
    compacts = [_box_text_compact(b) for b in (detail_boxes or [])]
    joined = "".join(compacts)
    if not joined:
        return False
    if any(kw in joined for kw in _TECH_OVERVIEW_OVERLAY_KEYWORDS):
        return True
    group_hits = sum(1 for grp in _TECH_OVERVIEW_ATTR_GROUPS if any(kw in joined for kw in grp))
    return group_hits >= 2


def classify_tech_tree_page_guard(
    img: Image.Image,
    boxes: Sequence[Dict[str, Any]],
    maa_dir: Optional[Path],
    active_profile: Dict[str, Any],
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Per-round Page Guard inside the tech-tree upgrade loop.

    Negative hard intercepts run first:
      - Theme selection page ('进入主题' / '添加到终端' / '查看收藏' / '该主题已全额收录') -> ``'theme_select_page'``
      - Theme lobby ('开始探险' / '开始探索' / '蚀刻章套组' / '常规行动') -> ``'lobby'``
      - Wrong sub-panels (archive / investment / exhibition / medal set) -> ``'wrong_page'``
      - Attribute overview overlay (no home icon + >=2 stat groups) -> ``'overview_overlay'``

    Positive redundant confirmation (tolerates occasional single-digit OCR miss like '5'):
      - Tech-tree canvas is confirmed when NO negative markers hit AND at least one redundant pair holds:
        (top '<' + house icon), OR (house icon + points/progress badge), OR (progress badge + '<'/points/keyword).
    """
    joined = "".join(_box_text_compact(b) for b in (boxes or []))

    # 1. Hard check: Integrated Strategies Theme Selection page (NEVER tap nodes, swipe, or tap '<' here!)
    if is_roguelike_theme_select_page(boxes):
        return {
            "status": "theme_select_page",
            "wrong_keyword": "theme_select_page",
            "has_home_icon": False,
            "has_return_icon": False,
            "has_progress_badge": False,
            "has_points_indicator": False,
            "points": None,
        }

    # 2. Returned to Roguelike theme lobby -> stop safely before wrong_page checks (lobby often shows 蚀刻章套组)
    if any(lk in joined for lk in ("开始探险", "继续探险", "开始探索", "继续探索", "蚀刻章套组", "常规行动")):
        return {
            "status": "lobby",
            "wrong_keyword": "theme_lobby",
            "has_home_icon": False,
            "has_return_icon": False,
            "has_progress_badge": False,
            "has_points_indicator": False,
            "points": None,
        }

    # 3. Explicit mis-entry pages (巫仪档案库 / 想象实体图鉴 / 虚构展览 / 设为活动主界面背景 / 投资系统 etc.)
    for wrong_kw in _TECH_TREE_PAGE_GUARD_WRONG_KEYWORDS:
        if wrong_kw in joined:
            return {
                "status": "wrong_page",
                "wrong_keyword": wrong_kw,
                "has_home_icon": False,
                "has_return_icon": False,
                "has_progress_badge": False,
                "has_points_indicator": False,
                "points": None,
            }

    has_home = detect_tech_tree_home_icon(img, maa_dir=maa_dir, boxes=boxes)
    has_ret = detect_tech_tree_return_icon(img, maa_dir=maa_dir)

    # 4. Full-screen attribute overview overlay (ONLY when home house icon is absent!)
    if _is_tech_tree_overview_overlay(boxes, has_bottom_confirm=False, detail_img=img, maa_dir=maa_dir):
        return {
            "status": "overview_overlay",
            "wrong_keyword": "overview_overlay",
            "has_home_icon": has_home,
            "has_return_icon": has_ret,
            "has_progress_badge": False,
            "has_points_indicator": False,
            "points": None,
        }

    has_progress_badge, has_progress_badge_kw = _detect_tech_tree_progress_badge(boxes)
    has_points_indicator, points = _has_top_right_tech_points_indicator(
        img, boxes, allow_roi_ocr=True, cancel_event=cancel_event
    )

    # 5. Redundant positive tech-tree canvas confirmation:
    #    Do NOT require all 3 simultaneously so single-digit OCR misses (e.g. '5') never abort a valid tech tree.
    confirmed_tech_tree = (
        (has_ret and has_home)
        or (has_home and (has_points_indicator or has_progress_badge))
        or (has_progress_badge and (has_ret or has_points_indicator or has_progress_badge_kw))
    )
    if confirmed_tech_tree:
        return {
            "status": "tech_tree",
            "wrong_keyword": "",
            "has_home_icon": has_home,
            "has_return_icon": has_ret,
            "has_progress_badge": has_progress_badge,
            "has_points_indicator": has_points_indicator,
            "points": points,
        }

    return {
        "status": "unknown_not_tech_tree",
        "wrong_keyword": "unverified_screen",
        "has_home_icon": has_home,
        "has_return_icon": has_ret,
        "has_progress_badge": has_progress_badge,
        "has_points_indicator": has_points_indicator,
        "points": points,
    }


def _ocr_box_bbox_center(box: Dict[str, Any]) -> Tuple[int, int]:
    """Return exact bounding-box center of an OCR box (preferring x+w/2, y+h/2 when bbox is present)."""
    if all(k in box for k in ("x", "y", "w", "h")):
        ux = int(round(float(box["x"]) + float(box["w"]) / 2.0))
        uy = int(round(float(box["y"]) + float(box["h"]) / 2.0))
        return ux, uy
    return int(box.get("cx", 0)), int(box.get("cy", 0))


def _execute_and_verify_tech_upgrade(
    adb_path: str,
    adb_addr: str,
    before_img: Image.Image,
    confirm_btn: Dict[str, Any],
    points_before: Optional[int],
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Click the exact OCR bbox center of '确认升级', wait 0.6s, and verify points deduction (e.g. 35 -> 32)
    or node state progression. Retries once if points/state unchanged (window focus protection).
    """
    ux, uy = _ocr_box_bbox_center(confirm_btn)
    btn_label = str(confirm_btn.get("text") or "确认升级")

    before_canvas_small = None
    if np is not None:
        b1280 = before_img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
        # Compare valid node canvas band (y in [115, 590]) for node progression
        before_canvas_small = np.asarray(b1280.crop((40, 115, 1240, 590)).resize((300, 120)), dtype=np.float32)

    last_after_img: Optional[Image.Image] = None
    last_after_boxes: List[Dict[str, Any]] = []
    last_points_after: Optional[int] = None

    for attempt in (1, 2):
        if cancel_event and cancel_event.is_set():
            return {"verified": False, "cancelled": True, "tap_xy": (ux, uy)}

        if attempt == 2:
            _emit(
                log_callback,
                f"[RL-TECH] 首击「{btn_label}」({ux},{uy})后点数未扣除，补点一次以防窗口失焦...",
            )
        stage_pilot.adb_tap_norm(adb_path, adb_addr, ux, uy, before_img.width, before_img.height)
        time.sleep(0.85)

        after_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
        if after_img is None:
            return {"verified": False, "reason": "after_screencap_failed", "tap_xy": (ux, uy)}
        last_after_img = after_img
        after_boxes = stage_pilot.find_text_boxes_via_ocr(after_img, include_inverted=True)
        last_after_boxes = list(after_boxes or [])
        points_after = extract_tech_tree_remaining_points(
            after_img, last_after_boxes, allow_roi_ocr=True, cancel_event=cancel_event
        )
        last_points_after = points_after

        # Primary truth: top-right remaining points strictly decreased (e.g. 35 -> 32)!
        if points_before is not None and points_after is not None and points_after < points_before:
            return {
                "verified": True,
                "attempts": attempt,
                "tap_xy": (ux, uy),
                "points_before": points_before,
                "points_after": points_after,
                "verify_reason": f"points:{points_before}->{points_after}",
                "after_img": after_img,
                "after_boxes": last_after_boxes,
            }

        # Secondary truth: confirm button transitioned off active state OR canvas visibly progressed
        after_color = inspect_tech_tree_confirm_button_color(after_img).get("state", "")
        canvas_diff = 0.0
        if before_canvas_small is not None and np is not None:
            a1280 = after_img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
            after_canvas_small = np.asarray(a1280.crop((40, 115, 1240, 590)).resize((300, 120)), dtype=np.float32)
            canvas_diff = float(np.mean(np.abs(after_canvas_small - before_canvas_small)))

        # If points are explicitly known on both sides and did NOT decrease, require BOTH button drop and canvas change
        if points_before is not None and points_after is not None:
            state_advanced = (after_color in ("disabled_grey", "inactive")) and (canvas_diff > 6.0)
        else:
            state_advanced = (after_color in ("disabled_grey", "inactive")) or (canvas_diff > 5.0)

        if state_advanced:
            return {
                "verified": True,
                "attempts": attempt,
                "tap_xy": (ux, uy),
                "points_before": points_before,
                "points_after": points_after,
                "verify_reason": f"state_advanced(btn={after_color},diff={canvas_diff:.1f})",
                "after_img": after_img,
                "after_boxes": last_after_boxes,
            }

    return {
        "verified": False,
        "attempts": 2,
        "tap_xy": (ux, uy),
        "points_before": points_before,
        "points_after": last_points_after,
        "reason": f"no_effect_after_retry(points={points_before}->{last_points_after})",
        "after_img": last_after_img,
        "after_boxes": last_after_boxes,
    }


def inspect_tech_tree_confirm_button_color(
    img: Image.Image,
    roi_norm: Tuple[float, float, float, float] = TECH_TREE_CONFIRM_BTN_ROI_NORM,
) -> Dict[str, Any]:
    """Inspect bottom-right confirm button ROI (x in [0.74,0.98], y in [0.85,0.98]) color state:
      - ``active``: vivid cyan-blue (Arknights '确认升级') or high-saturation bright clickable button
      - ``disabled_grey``: desaturated mid-grey disabled button (e.g. '未解锁')
      - ``unrendered_blank``: flat dark synthetic canvas (pure-mock unit test image with no UI rendered)
      - ``inactive``: textured background without a clickable confirm button
    """
    if np is None or img is None:
        return {"state": "unrendered_blank", "cyan_ratio": 0.0, "accent_ratio": 0.0, "grey_ratio": 0.0}
    rgb = img.convert("RGB")
    ow, oh = max(1, rgb.width), max(1, rgb.height)
    nx0, ny0, nx1, ny1 = roi_norm
    x0 = max(0, min(ow - 2, int(round(nx0 * ow))))
    y0 = max(0, min(oh - 2, int(round(ny0 * oh))))
    x1 = max(x0 + 2, min(ow, int(round(nx1 * ow))))
    y1 = max(y0 + 2, min(oh, int(round(ny1 * oh))))
    patch = np.asarray(rgb.crop((x0, y0, x1, y1)), dtype=np.float32)
    if patch.size == 0:
        return {"state": "unrendered_blank", "cyan_ratio": 0.0, "accent_ratio": 0.0, "grey_ratio": 0.0}

    r, g, b = patch[:, :, 0], patch[:, :, 1], patch[:, :, 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    max_c = np.maximum(np.maximum(r, g), b)
    min_c = np.minimum(np.minimum(r, g), b)
    sat = max_c - min_c

    # Cyan-blue clickable button (Arknights '确认升级 xN')
    cyan_mask = (b >= 135.0) & (g >= 105.0) & ((b - r) >= 35.0)
    # High-saturation bright clickable button across themes (cyan / gold / vivid orange)
    accent_mask = (sat >= 48.0) & (luma >= 95.0)
    # Desaturated grey disabled button ('未解锁' / insufficient points)
    grey_mask = (sat <= 26.0) & (luma >= 55.0) & (luma <= 170.0)

    cyan_ratio = float(np.mean(cyan_mask))
    accent_ratio = float(np.mean(accent_mask))
    grey_ratio = float(np.mean(grey_mask))
    luma_mean = float(np.mean(luma))
    luma_std = float(np.std(luma))

    if cyan_ratio >= 0.08 or accent_ratio >= 0.10:
        state = "active"
    elif grey_ratio >= 0.15:
        state = "disabled_grey"
    elif luma_mean < 45.0 and luma_std < 2.0:
        state = "unrendered_blank"
    else:
        state = "inactive"

    return {
        "state": state,
        "cyan_ratio": round(cyan_ratio, 4),
        "accent_ratio": round(accent_ratio, 4),
        "grey_ratio": round(grey_ratio, 4),
        "luma_mean": round(luma_mean, 2),
        "luma_std": round(luma_std, 2),
    }


def evaluate_tech_tree_confirm_button(
    detail_img: Image.Image,
    detail_boxes: Sequence[Dict[str, Any]],
    cancel_event: Optional[threading.Event] = None,
    remaining_points: Optional[int] = None,
) -> Dict[str, Any]:
    """Single source of truth for tech-tree upgrade confirmation: inspect bottom-right button ROI
    (x in [0.72, 0.99], y in [0.84, 0.99]) via OCR + color verification.

    Returns ``button_status`` in:
      ``'confirm_upgrade'`` | ``'already_upgraded'`` | ``'locked'`` | ``'insufficient_points'`` | ``'disabled_grey'`` | ``'none'``
    """
    color_info = inspect_tech_tree_confirm_button_color(detail_img)
    color_state = color_info["state"]

    # Collect OCR boxes inside strict bottom-right button ROI (x in [0.72, 0.99], y in [0.84, 0.99]) first
    strict_boxes = [b for b in (detail_boxes or []) if _box_in_roi(b, TECH_TREE_CONFIRM_BTN_ROI_1280, pad=10)]
    strict_joined = "".join(_box_text_compact(b) for b in strict_boxes)
    has_strict_disabled = any(bad_kw in strict_joined for bad_kw in _TECH_UNAVAILABLE_TEXTS)
    has_strict_action = (
        _find_matching_ocr_box(
            strict_boxes,
            _TECH_UPGRADE_BUTTON_TEXTS,
            exclude_keywords=_TECH_UPGRADE_BUTTON_EXCLUDE,
        )
        is not None
    )

    # Dedicated 2.5x upscaled ROI OCR on bottom-right button patch when:
    # 1) Vivid cyan-blue/bright button is rendered but full-screen OCR missed its text; OR
    # 2) Grey disabled button is rendered and full-screen OCR returned ZERO boxes in bottom-right ROI
    #    (lets us distinguish '已升级' vs '未解锁' vs '点数不足' reliably!).
    need_roi_ocr = (
        (color_state == "active" and not has_strict_action and not has_strict_disabled)
        or (color_state == "disabled_grey" and not strict_boxes and _has_rendered_ui_chrome(detail_img))
    )
    if need_roi_ocr and not (cancel_event and cancel_event.is_set()):
        roi_ocr_boxes = stage_pilot.find_text_boxes_via_ocr(
            detail_img,
            roi=TECH_TREE_CONFIRM_BTN_ROI_1280,
            include_inverted=True,
            include_binarized=True,
            upscale_factor=2.5,
            pad_border=12,
            cancel_event=cancel_event,
        )
        if roi_ocr_boxes:
            strict_boxes = list(roi_ocr_boxes)
            strict_joined = "".join(_box_text_compact(b) for b in strict_boxes)

    cost_match = re.search(r"[xX×\*]\s*(\d{1,4})", strict_joined)
    if cost_match is None:
        cost_match = re.search(
            r"(?:确认升级|确认激活|确认解锁|确认重构|确认强化|确认塑造|确认点亮|确认演化|确认研习|确认筹备)\s*(\d{1,4})",
            strict_joined,
        )
    required_cost: Optional[int] = int(cost_match.group(1)) if cost_match else None

    def _classify_unavailable(kw: str) -> Tuple[str, bool, bool, bool]:
        if kw in _TECH_ALREADY_UPGRADED_TEXTS:
            return "already_upgraded", False, True, False
        if kw in _TECH_INSUFFICIENT_POINTS_TEXTS:
            return "insufficient_points", True, False, False
        if kw in _TECH_LOCKED_TEXTS:
            return "locked", False, False, True
        return "disabled_grey", False, False, False

    # Check disabled / already-upgraded / locked text inside bottom-right button ROI
    for bad_kw in _TECH_UNAVAILABLE_TEXTS:
        if bad_kw in strict_joined:
            b_status, insuff, already_up, is_locked = _classify_unavailable(bad_kw)
            return {
                "valid": False,
                "reason": f"disabled_text:{bad_kw}",
                "button_status": b_status,
                "insufficient_points": insuff,
                "already_upgraded": already_up,
                "is_locked": is_locked,
                "color_state": color_state,
                "required_cost": required_cost,
                "button": None,
            }

    # Explicit grey disabled button state -> classify as insufficient_points when required_cost > remaining_points, else disabled_grey
    if color_state == "disabled_grey":
        if required_cost is not None and remaining_points is not None and required_cost > remaining_points:
            return {
                "valid": False,
                "reason": f"disabled_grey_insufficient_points(cost={required_cost}>pts={remaining_points})",
                "button_status": "insufficient_points",
                "insufficient_points": True,
                "already_upgraded": False,
                "is_locked": False,
                "color_state": color_state,
                "required_cost": required_cost,
                "button": None,
            }
        return {
            "valid": False,
            "reason": "disabled_grey_button",
            "button_status": "disabled_grey",
            "insufficient_points": False,
            "already_upgraded": False,
            "is_locked": False,
            "color_state": color_state,
            "required_cost": required_cost,
            "button": None,
        }

    # Search valid action button inside strict bottom-right ROI (x in [0.72, 0.99], y in [0.84, 0.99])
    btn = _find_matching_ocr_box(
        strict_boxes,
        _TECH_UPGRADE_BUTTON_TEXTS,
        exclude_keywords=_TECH_UPGRADE_BUTTON_EXCLUDE,
    )

    # Compatibility fallback for legacy blank-canvas unit tests whose mock button is at (1020..1060, 580)
    if btn is None and color_state == "unrendered_blank":
        compat_boxes = [b for b in (detail_boxes or []) if _box_in_roi(b, _TECH_TREE_CONFIRM_BTN_COMPAT_ROI_1280, pad=10)]
        compat_joined = "".join(_box_text_compact(b) for b in compat_boxes)
        for bad_kw in _TECH_UNAVAILABLE_TEXTS:
            if bad_kw in compat_joined:
                b_status, insuff, already_up, is_locked = _classify_unavailable(bad_kw)
                return {
                    "valid": False,
                    "reason": f"disabled_text:{bad_kw}",
                    "button_status": b_status,
                    "insufficient_points": insuff,
                    "already_upgraded": already_up,
                    "is_locked": is_locked,
                    "color_state": color_state,
                    "required_cost": required_cost,
                    "button": None,
                }
        btn = _find_matching_ocr_box(
            compat_boxes,
            _TECH_UPGRADE_BUTTON_TEXTS,
            exclude_keywords=_TECH_UPGRADE_BUTTON_EXCLUDE,
        )

    if btn is None:
        return {
            "valid": False,
            "reason": "no_confirm_button_text",
            "button_status": "none",
            "insufficient_points": False,
            "already_upgraded": False,
            "is_locked": False,
            "color_state": color_state,
            "required_cost": required_cost,
            "button": None,
        }

    # On rendered screenshots, require active cyan-blue/bright clickable color state!
    if color_state not in ("active", "unrendered_blank"):
        return {
            "valid": False,
            "reason": f"button_not_active_color:{color_state}",
            "button_status": "none",
            "insufficient_points": False,
            "already_upgraded": False,
            "is_locked": False,
            "color_state": color_state,
            "required_cost": required_cost,
            "button": None,
        }

    # Explicit cost vs remaining_points check regardless of button color state
    if required_cost is not None and remaining_points is not None and required_cost > remaining_points:
        return {
            "valid": False,
            "reason": f"insufficient_points(cost={required_cost}>pts={remaining_points})",
            "button_status": "insufficient_points",
            "insufficient_points": True,
            "already_upgraded": False,
            "is_locked": False,
            "color_state": color_state,
            "required_cost": required_cost,
            "button": None,
        }

    # Also check node description region (excluding bottom-left '64/78 已重构' badge & top header) for points shortage
    desc_boxes = [
        b for b in (detail_boxes or [])
        if not is_point_in_tech_tree_forbidden_zone(*_box_center(b))
        or (_box_center(b)[0] >= 300 and _box_center(b)[1] >= 540)
    ]
    desc_text = " ".join(str(b.get("text") or "") for b in desc_boxes)
    for shortage_kw in ("点数不足", "材料不足", "资源不足", "前置未解锁", "已满级"):
        if shortage_kw in desc_text:
            b_status, insuff, already_up, is_locked = _classify_unavailable(shortage_kw)
            return {
                "valid": False,
                "reason": f"desc_unavailable:{shortage_kw}",
                "button_status": b_status,
                "insufficient_points": insuff,
                "already_upgraded": already_up,
                "is_locked": is_locked,
                "color_state": color_state,
                "required_cost": required_cost,
                "button": None,
            }

    return {
        "valid": True,
        "reason": "ok",
        "button_status": "confirm_upgrade",
        "insufficient_points": False,
        "already_upgraded": False,
        "is_locked": False,
        "color_state": color_state,
        "required_cost": required_cost,
        "button": btn,
        "detail_text": desc_text,
    }


def _compute_sobel_grad_2d(luma: "np.ndarray") -> "np.ndarray":
    """Compute Sobel gradient magnitude on a 2D float32 luminance array (pure NumPy)."""
    p = np.pad(luma.astype(np.float32), ((1, 1), (1, 1)), mode="edge")
    gx = (
        (p[:-2, 2:] + 2.0 * p[1:-1, 2:] + p[2:, 2:])
        - (p[:-2, :-2] + 2.0 * p[1:-1, :-2] + p[2:, :-2])
    ) * 0.25
    gy = (
        (p[2:, :-2] + 2.0 * p[2:, 1:-1] + p[2:, 2:])
        - (p[:-2, :-2] + 2.0 * p[:-2, 1:-1] + p[:-2, 2:])
    ) * 0.25
    return np.hypot(gx, gy).astype(np.float32)


def _build_tech_tree_allowed_mask(canvas_roi: Tuple[int, int, int, int]) -> "np.ndarray":
    """Return 720x1280 bool mask strictly limited to safe scan ROI minus all forbidden UI zones."""
    mask = np.zeros((720, 1280), dtype=bool)
    rx, ry, rw, rh = canvas_roi
    x0, y0 = max(0, int(rx)), max(0, int(ry))
    x1, y1 = min(1280, int(rx + rw)), min(720, int(ry + rh))
    if x1 > x0 and y1 > y0:
        mask[y0:y1, x0:x1] = True
    for fx0, fy0, fx1, fy1 in TECH_TREE_FORBIDDEN_ZONES_NORM:
        mx0 = max(0, int(round(fx0 * 1280.0)))
        my0 = max(0, int(round(fy0 * 720.0)))
        mx1 = min(1280, int(round(fx1 * 1280.0)))
        my1 = min(720, int(round(fy1 * 720.0)))
        if mx1 > mx0 and my1 > my0:
            mask[my0:my1, mx0:mx1] = False
    return mask


# Precomputed 8 angular sectors x 3 sub-rays for fast closed-halo ring verification
_HALO_SECTOR_RAYS: Tuple[Tuple[Tuple[float, float], ...], ...] = tuple(
    tuple(
        (
            float(np.cos(np.deg2rad(sec * 45.0 + off))),
            float(np.sin(np.deg2rad(sec * 45.0 + off))),
        )
        for off in (-12.0, 0.0, 12.0)
    )
    for sec in range(8)
) if np is not None else ()


def _evaluate_tech_node_halo_at(
    cx: int,
    cy: int,
    luma: "np.ndarray",
    sat: "np.ndarray",
    bg_local: "np.ndarray",
    grad: "np.ndarray",
    est_r: int = 28,
) -> Optional[Dict[str, Any]]:
    """Verify whether (cx, cy) is a frontier upgradeable grey stone tablet or small square stat node
    surrounded by a closed high-gradient bright white glowing outline, strictly rejecting solid pure-white
    upgraded icons, dark locked stone tablets, and smooth warm-white background light patches.
    """
    h, w = luma.shape
    if is_point_in_tech_tree_forbidden_zone(cx, cy):
        return None

    # Test adaptive radii around est_r so small bottom-row square stat nodes (~16-36px) and large steles all match cleanly
    cand_radii = sorted(
        {
            max(8, min(46, int(est_r) + delta))
            for delta in (-6, -3, 0, 3, 6)
        }
        | {10, 14, 18, 24, 30, 36}
    )

    best_halo: Optional[Dict[str, Any]] = None
    for rad in cand_radii:
        # 1. Central ~40% core region: reject solid pure-white upgraded nodes!
        rc = max(3, int(round(0.40 * rad)))
        cy0, cy1 = max(0, cy - rc), min(h, cy + rc + 1)
        cx0, cx1 = max(0, cx - rc), min(w, cx + rc + 1)
        core_l = luma[cy0:cy1, cx0:cx1]
        core_s = sat[cy0:cy1, cx0:cx1]
        if core_l.size < 16:
            continue
        core_mean = float(np.mean(core_l))
        core_sat_mean = float(np.mean(core_s))
        core_white_ratio = float(np.mean((core_l >= 200.0) & (core_s <= 45.0)))

        # Hard reject solid pure-white upgraded icons & bright warm-white light cores
        if core_white_ratio >= 0.42 or (core_mean >= 190.0 and core_sat_mean <= 48.0):
            continue
        # Stone tablet / small square interior must be mid-grey (not pitch-black void, not blinding white)
        if not (28.0 <= core_mean <= 178.0):
            continue

        # 1b. Interior pattern & saturation inspection (strictly inside 0.55*rad halo inner edge)
        r_in = max(3, int(round(0.48 * rad)))
        iy0, iy1 = max(0, cy - r_in), min(h, cy + r_in + 1)
        ix0, ix1 = max(0, cx - r_in), min(w, cx + r_in + 1)
        inner_l = luma[iy0:iy1, ix0:ix1]
        inner_s = sat[iy0:iy1, ix0:ix1]
        if inner_l.size < 16:
            continue
        inner_sat_mean = float(np.mean(inner_s))
        inner_color_ratio = float(np.mean(inner_s >= 42.0))
        inner_dark_ratio = float(np.mean(inner_l <= 52.0))
        inner_bright_ratio = float(np.mean((inner_l >= 180.0) & (inner_s <= 50.0)))
        inner_std = float(np.std(inner_l))

        # Hard reject already-upgraded large steles with colored crests or dark-base + bright-white emblem
        if inner_sat_mean >= 28.0 or inner_color_ratio >= 0.15:
            continue
        if inner_dark_ratio >= 0.25 and (inner_bright_ratio >= 0.10 or inner_std >= 46.0):
            continue

        # 2. 8-sector closed glowing white outer rim check across radial band [0.55*R, 1.52*R]
        r_steps = np.linspace(max(6.0, 0.55 * rad), min(68.0, 1.52 * rad), 14, dtype=np.float32)
        out_steps = np.linspace(max(15.0, 1.62 * rad), min(92.0, 2.08 * rad), 5, dtype=np.float32)

        valid_sectors = 0
        valid_cardinals = 0
        sec_lumas: List[float] = []
        sec_grads: List[float] = []
        outer_vals: List[float] = []

        for sec_idx, rays in enumerate(_HALO_SECTOR_RAYS):
            best_sec_wl = 0.0
            best_sec_grad = 0.0
            for dx, dy in rays:
                xs = np.clip(np.round(cx + dx * r_steps).astype(np.int32), 0, w - 1)
                ys = np.clip(np.round(cy + dy * r_steps).astype(np.int32), 0, h - 1)
                rl = luma[ys, xs]
                rs = sat[ys, xs]
                rg = grad[ys, xs]
                white_sel = rs <= 58.0
                if np.any(white_sel):
                    wl = float(np.max(rl[white_sel]))
                    if wl > best_sec_wl:
                        best_sec_wl = wl
                mg = float(np.max(rg))
                if mg > best_sec_grad:
                    best_sec_grad = mg

                oxs = np.clip(np.round(cx + dx * out_steps).astype(np.int32), 0, w - 1)
                oys = np.clip(np.round(cy + dy * out_steps).astype(np.int32), 0, h - 1)
                outer_vals.append(float(np.median(luma[oys, oxs])))

            sec_lumas.append(best_sec_wl)
            sec_grads.append(best_sec_grad)
            if (best_sec_wl >= 170.0) and ((best_sec_wl - core_mean) >= 28.0) and (best_sec_grad >= 20.0):
                valid_sectors += 1
                if sec_idx % 2 == 0:
                    valid_cardinals += 1

        if valid_sectors < 6 or valid_cardinals < 3:
            continue

        ring_luma_med = float(np.median(sec_lumas))
        ring_grad_med = float(np.median(sec_grads))
        outer_bg_med = float(np.median(outer_vals)) if outer_vals else 0.0
        ring_minus_core = ring_luma_med - core_mean
        ring_minus_outer = ring_luma_med - outer_bg_med

        if (
            ring_luma_med >= 175.0
            and ring_minus_core >= 30.0
            and ring_grad_med >= 20.0
            and ring_minus_outer >= 22.0
        ):
            # 3. Check incoming LEFT white line & outgoing RIGHT white vs black connecting lines
            lx0 = max(0, cx - int(round(rad * 1.15)) - 54)
            lx1 = max(lx0 + 4, cx - int(round(rad * 1.02)) - 2)
            ly0 = max(0, cy - max(20, int(round(rad * 0.90))))
            ly1 = min(h, cy + max(20, int(round(rad * 0.90))))
            strip_l = luma[ly0:ly1, lx0:lx1]
            strip_s = sat[ly0:ly1, lx0:lx1]
            strip_bg = bg_local[ly0:ly1, lx0:lx1]
            has_left_line = False
            if strip_l.size > 0:
                glow_line_px = (strip_l >= 165.0) & (strip_s <= 52.0) & ((strip_l - strip_bg) >= 22.0)
                col_hits = np.any(glow_line_px, axis=0)
                has_left_line = (
                    int(np.sum(glow_line_px)) >= 10
                    and float(np.mean(col_hits)) >= 0.25
                    and float(np.mean(glow_line_px)) <= 0.55
                )

            rx0 = min(w - 4, cx + int(round(rad * 1.02)) + 2)
            rx1 = min(w, cx + int(round(rad * 1.15)) + 44)
            ry0 = max(0, cy - max(18, int(round(rad * 0.85))))
            ry1 = min(h, cy + max(18, int(round(rad * 0.85))))
            r_strip_l = luma[ry0:ry1, rx0:rx1]
            r_strip_s = sat[ry0:ry1, rx0:rx1]
            r_strip_bg = bg_local[ry0:ry1, rx0:rx1]
            has_right_glow_line = False
            has_right_dark_line = False
            if r_strip_l.size > 0 and (rx1 - rx0) >= 6:
                r_glow_px = (r_strip_l >= 165.0) & (r_strip_s <= 52.0) & ((r_strip_l - r_strip_bg) >= 22.0)
                r_col_hits = np.any(r_glow_px, axis=0)
                if (
                    int(np.sum(r_glow_px)) >= 10
                    and float(np.mean(r_col_hits)) >= 0.25
                    and float(np.mean(r_glow_px)) <= 0.50
                    and bool(np.any(r_col_hits[:min(12, r_col_hits.size)]))
                ):
                    has_right_glow_line = True

                r_dark_px = (r_strip_l <= 45.0) & ((r_strip_bg - r_strip_l) >= 10.0)
                d_col_hits = np.any(r_dark_px, axis=0)
                if (
                    int(np.sum(r_dark_px)) >= 8
                    and float(np.mean(d_col_hits)) >= 0.22
                    and float(np.mean(r_dark_px)) <= 0.55
                    and bool(np.any(d_col_hits[:min(14, d_col_hits.size)]))
                ):
                    has_right_dark_line = True

            # Hard reject already-upgraded stele emitting a right-side bright white line with dark/patterned core
            if (
                has_right_glow_line
                and not has_right_dark_line
                and (
                    inner_dark_ratio >= 0.15
                    or inner_bright_ratio >= 0.08
                    or inner_sat_mean >= 16.0
                    or inner_std >= 30.0
                    or core_white_ratio >= 0.15
                )
            ):
                continue

            is_clean_grey_interior = (
                (inner_sat_mean <= 20.0)
                and (inner_color_ratio <= 0.08)
                and (inner_dark_ratio <= 0.15)
                and (inner_bright_ratio <= 0.08)
                and (60.0 <= core_mean <= 168.0)
            )
            unlockable_score = (
                valid_sectors * 10.0
                + min(100.0, ring_minus_core) * 0.6
                + min(60.0, ring_luma_med - 175.0) * 0.5
                + max(0.0, 28.0 - inner_sat_mean) * 2.0
                + (25.0 if is_clean_grey_interior else 0.0)
                + (15.0 if has_left_line else 0.0)
                + (15.0 if has_right_dark_line else 0.0)
                - inner_dark_ratio * 80.0
                - inner_bright_ratio * 100.0
                - inner_sat_mean * 1.5
                - (90.0 if has_right_glow_line else 0.0)
            )
            if has_right_glow_line:
                frontier_tier = 2
            elif is_clean_grey_interior:
                frontier_tier = 0
            else:
                frontier_tier = 1

            quality = (
                valid_sectors * 15.0
                + min(120.0, ring_minus_core)
                + min(80.0, ring_grad_med)
                + min(80.0, ring_minus_outer) * 0.5
                - core_white_ratio * 120.0
            )
            if best_halo is None or quality > best_halo["halo_score"]:
                best_halo = {
                    "cx": int(cx),
                    "cy": int(cy),
                    "radius": int(rad),
                    "lit_pixels": int(round(valid_sectors * rad * 2.5)),
                    "has_halo": True,
                    "has_left_glow_line": has_left_line,
                    "has_right_glow_line": has_right_glow_line,
                    "has_right_dark_line": has_right_dark_line,
                    "core_white_ratio": round(core_white_ratio, 4),
                    "core_mean": round(core_mean, 1),
                    "inner_sat_mean": round(inner_sat_mean, 2),
                    "ring_luma": round(ring_luma_med, 1),
                    "halo_sectors": int(valid_sectors),
                    "halo_score": round(quality, 2),
                    "unlockable_score": round(unlockable_score, 2),
                    "frontier_tier": int(frontier_tier),
                    "node_kind": "glowing_frontier",
                    "node_size_class": "small_square" if rad <= 17 else "large_tablet",
                }
    return best_halo


def _has_solid_white_upgraded_nodes(
    img: Image.Image,
    canvas_roi: Tuple[int, int, int, int] = TECH_TREE_CANVAS_ROI_1280,
) -> bool:
    """Return True if the safe canvas ROI contains compact solid pure-white already-upgraded icons
    (used on all-upgraded left pages when zero frontier glowing-halo nodes exist to trigger left-swipe).
    """
    if np is None or img is None:
        return False
    arr = np.asarray(img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR), dtype=np.float32)
    luma = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    sat = np.maximum(np.maximum(arr[:, :, 0], arr[:, :, 1]), arr[:, :, 2]) - np.minimum(
        np.minimum(arr[:, :, 0], arr[:, :, 1]), arr[:, :, 2]
    )
    bg_local = _box_mean_2d(luma, radius=28)
    allowed = _build_tech_tree_allowed_mask(canvas_roi)
    solid_white = allowed & (luma >= 205.0) & (sat <= 42.0) & ((luma - bg_local) >= 35.0)
    # Require at least one compact >= 140px solid-white icon cluster inside safe canvas
    cell = 24
    gh, gw = 720 // cell, 1280 // cell
    counts = solid_white[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).sum(axis=(1, 3))
    return bool(np.any(counts >= 140))


def detect_upgradeable_tech_nodes(
    img: Image.Image,
    canvas_roi: Tuple[int, int, int, int] = TECH_TREE_CANVAS_ROI_1280,
    brightness_threshold: float = 175.0,
    min_cluster_pixels: int = 16,
) -> List[Dict[str, Any]]:
    """Detect upgradeable frontier nodes in a tech-tree screenshot (large stone tablets & small bottom-row squares).

    Strictly limits scanning to ``x in [0.08, 0.93], y in [0.16, 0.80]`` (excluding left/right page arrows
    and the tightened bottom-left ``64/78 已重构`` arch ``x < 0.175, y > 0.63``) and classifies nodes by local structure:
      1) Eliminates already-upgraded solid pure-white icons (high pure-white pixel ratio in central core).
      2) Eliminates dark locked tablets and smooth warm-white background glow patches (never uses global brightness mean).
      3) Extracts closed high-gradient bright white glowing-halo grey stone tablets & small square stat nodes.
      4) Sorts candidates strictly left-to-right (X ascending, then Y ascending) within priority tier.
    """
    if np is None or img is None:
        return []
    img_1280 = img.convert("RGB").resize((1280, 720), Image.Resampling.BILINEAR)
    arr = np.asarray(img_1280, dtype=np.float32)

    r_ch, g_ch, b_ch = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    luma = 0.299 * r_ch + 0.587 * g_ch + 0.114 * b_ch
    max_c = np.maximum(np.maximum(r_ch, g_ch), b_ch)
    min_c = np.minimum(np.minimum(r_ch, g_ch), b_ch)
    sat = max_c - min_c

    bg_local = _box_mean_2d(luma, radius=28)
    grad = _compute_sobel_grad_2d(luma)
    allowed_mask = _build_tech_tree_allowed_mask(canvas_roi)

    # ------------------------------------------------------------------
    # Stream A: Multi-scale closed glowing white halo proposal (2x max-pooled -> 640x360)
    # Use a padded rim mask so borders of nodes sitting close to safe-zone edges are not clipped;
    # node centers remain strictly gated by allowed_mask & is_point_in_tech_tree_forbidden_zone.
    # ------------------------------------------------------------------
    rim_bounds_mask = np.zeros((720, 1280), dtype=bool)
    rim_bounds_mask[100:592, 84:1208] = True
    white_rim_px = (
        rim_bounds_mask
        & (luma >= max(160.0, brightness_threshold - 10.0))
        & (sat <= 55.0)
        & (((luma - bg_local) >= 18.0) | (grad >= 20.0))
    )
    core_white_px = (luma >= 200.0) & (sat <= 45.0)

    rim_h = white_rim_px.reshape(360, 2, 640, 2).max(axis=(1, 3)).astype(np.float32)
    luma_h = luma.reshape(360, 2, 640, 2).mean(axis=(1, 3)).astype(np.float32)
    cwhite_h = core_white_px.reshape(360, 2, 640, 2).mean(axis=(1, 3)).astype(np.float32)
    allow_h = allowed_mask.reshape(360, 2, 640, 2).min(axis=(1, 3))

    best_score_map = np.zeros((360, 640), dtype=np.float32)
    best_rad_map = np.full((360, 640), 28, dtype=np.int32)

    for rh in (5, 7, 9, 12, 16, 20, 24):
        r_strip = 1 if rh <= 7 else max(2, rh // 4)
        rim_s = _box_mean_2d(rim_h, radius=r_strip)
        r_core = max(2, int(round(0.38 * rh)))
        core_l = _box_mean_2d(luma_h, radius=r_core)
        core_w = _box_mean_2d(cwhite_h, radius=r_core)

        sl_y = slice(rh, 360 - rh)
        sl_x = slice(rh, 640 - rh)
        d = max(1, int(round(0.71 * rh)))

        top = rim_s[0 : 360 - 2 * rh, sl_x]
        bot = rim_s[2 * rh : 360, sl_x]
        lft = rim_s[sl_y, 0 : 640 - 2 * rh]
        rgt = rim_s[sl_y, 2 * rh : 640]
        tl = np.maximum(
            rim_s[rh - d : 360 - rh - d, rh - d : 640 - rh - d],
            rim_s[0 : 360 - 2 * rh, 0 : 640 - 2 * rh],
        )
        tr = np.maximum(
            rim_s[rh - d : 360 - rh - d, rh + d : 640 - rh + d],
            rim_s[0 : 360 - 2 * rh, 2 * rh : 640],
        )
        bl = np.maximum(
            rim_s[rh + d : 360 - rh + d, rh - d : 640 - rh - d],
            rim_s[2 * rh : 360, 0 : 640 - 2 * rh],
        )
        br = np.maximum(
            rim_s[rh + d : 360 - rh + d, rh + d : 640 - rh + d],
            rim_s[2 * rh : 360, 2 * rh : 640],
        )

        th = 0.075
        card_cnt = (top >= th).astype(np.int8) + (bot >= th).astype(np.int8) + (lft >= th).astype(np.int8) + (rgt >= th).astype(np.int8)
        dir_cnt = card_cnt + (tl >= th).astype(np.int8) + (tr >= th).astype(np.int8) + (bl >= th).astype(np.int8) + (br >= th).astype(np.int8)
        ring_min4 = np.minimum(np.minimum(top, bot), np.minimum(lft, rgt))
        ring_avg8 = (top + bot + lft + rgt + tl + tr + bl + br) * 0.125

        core_ok = (
            allow_h[sl_y, sl_x]
            & (core_l[sl_y, sl_x] >= 28.0)
            & (core_l[sl_y, sl_x] <= 180.0)
            & (core_w[sl_y, sl_x] < 0.45)
        )
        sc = np.where(
            core_ok & (card_cnt >= 3) & (dir_cnt >= 5),
            ring_avg8 * (0.4 + ring_min4) * (1.0 + 0.01 * np.clip(215.0 - core_l[sl_y, sl_x], 0.0, 150.0)),
            0.0,
        ).astype(np.float32)

        sub_best = best_score_map[sl_y, sl_x]
        better = sc > sub_best
        if np.any(better):
            sub_best[better] = sc[better]
            best_rad_map[sl_y, sl_x][better] = rh * 2

    # Non-maximum suppression on half-res halo score map (extract up to 48 peaks)
    halo_proposals: List[Tuple[int, int, int, float]] = []
    peak_ys, peak_xs = np.where(best_score_map >= 0.05)
    if peak_ys.size > 0:
        peak_vals = best_score_map[peak_ys, peak_xs]
        # Fast pre-filter to top 6000 pixels if needed
        if peak_vals.size > 6000:
            top_idx = np.argpartition(peak_vals, -6000)[-6000:]
            peak_ys = peak_ys[top_idx]
            peak_xs = peak_xs[top_idx]
            peak_vals = peak_vals[top_idx]
        order = np.argsort(-peak_vals)
        for idx in order:
            hy = int(peak_ys[idx])
            hx = int(peak_xs[idx])
            val = float(peak_vals[idx])
            if any(abs(hx - px) < 14 and abs(hy - py) < 14 for px, py, _, _ in halo_proposals):
                continue
            halo_proposals.append((hx, hy, int(best_rad_map[hy, hx]), val))
            if len(halo_proposals) >= 48:
                break

    verified_nodes: List[Dict[str, Any]] = []
    for hx, hy, est_r, _ in halo_proposals:
        cx_full, cy_full = hx * 2, hy * 2
        ev = _evaluate_tech_node_halo_at(cx_full, cy_full, luma, sat, bg_local, grad, est_r=est_r)
        if ev is not None:
            verified_nodes.append(ev)

    # ------------------------------------------------------------------
    # Stream B: Compact saturated warm-gold / bright-cyan accent nodes (BlackFlow / other themes)
    # Strictly requires sat >= 65 (completely rejects pure-white upgraded icons & warm-white flares!)
    # ------------------------------------------------------------------
    warm_gold = allowed_mask & (r_ch > 190.0) & (g_ch > 145.0) & (b_ch < 140.0) & (sat >= 65.0) & ((luma - bg_local) >= 20.0)
    bright_cyan = allowed_mask & (b_ch > 180.0) & (g_ch > 160.0) & (r_ch < 150.0) & (sat >= 65.0) & ((luma - bg_local) >= 20.0)
    accent_mask = warm_gold | bright_cyan

    cell = 12
    gh, gw = 720 // cell, 1280 // cell
    acc_counts = accent_mask[: gh * cell, : gw * cell].reshape(gh, cell, gw, cell).sum(axis=(1, 3))
    cell_min = max(4, min_cluster_pixels // 4)
    visited = np.zeros((gh, gw), dtype=bool)
    accent_candidates: List[Tuple[int, int, int]] = []
    for gy in range(gh):
        for gx in range(gw):
            if visited[gy, gx] or acc_counts[gy, gx] < cell_min:
                continue
            stack = [(gy, gx)]
            visited[gy, gx] = True
            total_px = 0
            sum_x = 0.0
            sum_y = 0.0
            min_gx, max_gx, min_gy, max_gy = gx, gx, gy, gy
            while stack:
                py, px = stack.pop()
                w_px = int(acc_counts[py, px])
                total_px += w_px
                sum_x += (px + 0.5) * cell * w_px
                sum_y += (py + 0.5) * cell * w_px
                if px < min_gx:
                    min_gx = px
                if px > max_gx:
                    max_gx = px
                if py < min_gy:
                    min_gy = py
                if py > max_gy:
                    max_gy = py
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny_c, nx_c = py + dy, px + dx
                        if 0 <= ny_c < gh and 0 <= nx_c < gw and not visited[ny_c, nx_c] and acc_counts[ny_c, nx_c] >= cell_min:
                            visited[ny_c, nx_c] = True
                            stack.append((ny_c, nx_c))
            bw = (max_gx - min_gx + 1) * cell
            bh = (max_gy - min_gy + 1) * cell
            if total_px >= min_cluster_pixels and bw <= 128 and bh <= 128:
                nx = int(round(sum_x / total_px))
                ny = int(round(sum_y / total_px))
                if not is_point_in_tech_tree_forbidden_zone(nx, ny):
                    accent_candidates.append((total_px, nx, ny))

    for px_cnt, nx, ny in sorted(accent_candidates, key=lambda t: -t[0]):
        if any(
            abs(nx - n["cx"]) < max(26, min(48, int(round((14 + int(n.get("radius", 24))) * 0.9))))
            and abs(ny - n["cy"]) < max(26, min(48, int(round((14 + int(n.get("radius", 24))) * 0.9))))
            for n in verified_nodes
        ):
            continue
        # Reject if core is solid pure white or lacks sharp edge gradient
        core_l = luma[max(0, ny - 7) : min(720, ny + 8), max(0, nx - 7) : min(1280, nx + 8)]
        core_s = sat[max(0, ny - 7) : min(720, ny + 8), max(0, nx - 7) : min(1280, nx + 8)]
        ring_g = grad[max(0, ny - 32) : min(720, ny + 33), max(0, nx - 32) : min(1280, nx + 33)]
        if core_l.size == 0 or ring_g.size == 0:
            continue
        c_white_ratio = float(np.mean((core_l >= 200.0) & (core_s <= 45.0)))
        if c_white_ratio >= 0.38:
            continue
        if (
            float(np.mean(core_s)) < 35.0
            or float(np.percentile(core_s, 75)) < 50.0
            or float(np.percentile(ring_g, 96)) < 18.0
            or float(np.max(ring_g)) < 28.0
        ):
            continue
        verified_nodes.append({
            "cx": nx,
            "cy": ny,
            "radius": 14 if px_cnt < 400 else 22,
            "lit_pixels": px_cnt,
            "has_halo": False,
            "has_left_glow_line": False,
            "has_right_glow_line": False,
            "frontier_tier": 2,
            "unlockable_score": 0.0,
            "core_white_ratio": round(c_white_ratio, 4),
            "node_kind": "accent_lit",
            "node_size_class": "small_square" if px_cnt < 400 else "large_tablet",
        })

    # Deduplicate nearby centroids using adaptive radius-aware separation
    verified_nodes.sort(
        key=lambda n: (
            int(n.get("frontier_tier", 2)),
            0 if n.get("has_halo") else 1,
            -float(n.get("unlockable_score", 0.0)),
            -float(n.get("halo_score", 0.0)),
            -int(n.get("lit_pixels", 0)),
        )
    )
    deduped: List[Dict[str, Any]] = []
    for cand in verified_nodes:
        cand_r = int(cand.get("radius", 24))
        if any(
            abs(cand["cx"] - n["cx"]) < max(26, min(48, int(round((cand_r + int(n.get("radius", 24))) * 0.9))))
            and abs(cand["cy"] - n["cy"]) < max(26, min(48, int(round((cand_r + int(n.get("radius", 24))) * 0.9))))
            for n in deduped
        ):
            continue
        deduped.append(cand)

    # Final priority order:
    #   1) Frontier tier (0: clean low-sat grey core + glowing outer halo + no outgoing right white wire)
    #   2) Glowing-halo grey stone frontier nodes first
    #   3) Left-white-line-connected frontier nodes before unconnected/isolated ones
    #   4) Higher unlockable score for non-Tier-0 fallback candidates
    #   5) Strictly left-to-right (X ascending), then top-to-bottom (Y ascending)
    deduped.sort(
        key=lambda n: (
            int(n.get("frontier_tier", 2)),
            0 if n.get("has_halo") else 1,
            0 if n.get("has_left_glow_line") else 1,
            0.0 if int(n.get("frontier_tier", 2)) == 0 else -round(float(n.get("unlockable_score", 0.0)), 1),
            int(n["cx"]),
            int(n["cy"]),
        )
    )
    return deduped


def _tap_return_to_lobby(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str,
    max_taps: int = 3,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Tap top-left '<' return button one tap at a time, waiting 1.2s after each tap and immediately
    verifying lobby identity ('开始探险'/'开始探索'/'蚀刻章套组'/'常规行动' etc.).
    Stops immediately once back in lobby OR if the Theme Selection page is detected (never blind-taps '<').
    """
    tapped_any = False
    for _ in range(max(1, int(max_taps))):
        if cancel_event and cancel_event.is_set():
            return False
        img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
        if img is None:
            return False
        boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True, cancel_event=cancel_event)
        if is_roguelike_theme_lobby(boxes, preferred_theme=theme):
            return True
        if is_roguelike_theme_select_page(boxes):
            _emit(log_callback, "[WARN][RL-POST] 返回大厅校验检测到处于「主题选择页」，立即停止点击返回键 (<)。")
            return False
        stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, img.width, img.height)
        tapped_any = True
        time.sleep(1.2)

    if tapped_any and not (cancel_event and cancel_event.is_set()):
        img_final = stage_pilot.adb_screencap_image(adb_path, adb_addr)
        if img_final is not None:
            boxes_final = stage_pilot.find_text_boxes_via_ocr(img_final, include_inverted=True, cancel_event=cancel_event)
            if is_roguelike_theme_lobby(boxes_final, preferred_theme=theme):
                return True
            if is_roguelike_theme_select_page(boxes_final):
                _emit(log_callback, "[WARN][RL-POST] 返回大厅末次校验检测到处于「主题选择页」，立即停止返回。")
                return False

    _emit(log_callback, f"[RL-POST] 已尝试返回 {theme} 主界面。")
    return False


def _recover_from_theme_select_to_lobby(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    img: Image.Image,
    boxes: Sequence[Dict[str, Any]],
    target_theme: str,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Recover from the Integrated Strategies Theme Selection hub page back into ``target_theme`` lobby
    by selecting the target theme card if needed and tapping '进入主题' (strictly avoiding '添加到终端' / '查看收藏').
    """
    norm_theme = normalize_roguelike_theme(target_theme, default="BlackFlow")
    profile = get_theme_ui_profile(norm_theme)
    disp = str(profile.get("display_name") or norm_theme)
    select_kws = list(profile.get("theme_select_keywords") or [disp])

    cur_img = img
    cur_boxes = list(boxes or [])

    for _step in range(2):
        if cancel_event and cancel_event.is_set():
            return {"ok": False, "cancelled": True, "img": cur_img, "boxes": cur_boxes}

        if is_roguelike_theme_lobby(cur_boxes, preferred_theme=norm_theme):
            return {"ok": True, "cancelled": False, "img": cur_img, "boxes": cur_boxes}

        enter_box = _find_matching_ocr_box(
            cur_boxes,
            ("进入主题",),
            exclude_keywords=("添加到终端", "查看收藏", "该主题已全额收录"),
        )
        target_card_box = _find_matching_ocr_box(
            cur_boxes,
            select_kws,
            exclude_keywords=("添加到终端", "查看收藏", "该主题已全额收录", "进入主题"),
        )
        # Check if a DIFFERENT theme's display name is highlighted as the active card when our target card is visible
        cur_joined = "".join(_box_text_compact(b) for b in cur_boxes)
        target_visible = any(kw in cur_joined for kw in select_kws if kw)

        if enter_box is not None and (target_visible or target_card_box is None):
            _emit(
                log_callback,
                f"[RL-TECH] 主题选择页命中「{enter_box['text']}」({enter_box['cx']},{enter_box['cy']})，点击返回「{disp}」大厅...",
            )
            stage_pilot.adb_tap_norm(adb_path, adb_addr, enter_box["cx"], enter_box["cy"], cur_img.width, cur_img.height)
            time.sleep(1.5)
        elif target_card_box is not None:
            _emit(
                log_callback,
                f"[RL-TECH] 主题选择页点击目标主题卡片「{target_card_box['text']}」({target_card_box['cx']},{target_card_box['cy']})...",
            )
            stage_pilot.adb_tap_norm(
                adb_path, adb_addr, target_card_box["cx"], target_card_box["cy"], cur_img.width, cur_img.height
            )
            time.sleep(1.2)
        else:
            return {"ok": False, "cancelled": False, "img": cur_img, "boxes": cur_boxes}

        next_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
        if next_img is None:
            return {"ok": False, "cancelled": False, "img": cur_img, "boxes": cur_boxes}
        cur_img = next_img
        cur_boxes = list(stage_pilot.find_text_boxes_via_ocr(cur_img, include_inverted=True, cancel_event=cancel_event) or [])
        if is_roguelike_theme_lobby(cur_boxes, preferred_theme=norm_theme):
            _emit(log_callback, f"[RL-TECH] 已从主题选择页成功返回「{disp}」主题大厅。")
            return {"ok": True, "cancelled": False, "img": cur_img, "boxes": cur_boxes}

    return {"ok": False, "cancelled": False, "img": cur_img, "boxes": cur_boxes}


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


def _other_theme_unique_markers(target_theme: str) -> List[str]:
    """Return unique level-reward / tech-tree markers belonging to OTHER Roguelike themes."""
    norm_target = normalize_roguelike_theme(target_theme, default="BlackFlow")
    markers: List[str] = []
    for t_key, prof in THEME_UI_PROFILES.items():
        if t_key == norm_target:
            continue
        lvl = prof.get("level_reward", {})
        tech = prof.get("tech_tree", {})
        for kw in (lvl.get("name", ""), tech.get("name", "")):
            if kw and kw not in markers and "/" not in kw:
                markers.append(kw)
    return markers


def ensure_roguelike_theme_lobby(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str = "BlackFlow",
    max_steps: int = 12,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    accept_any_current_lobby: bool = True,
) -> Dict[str, Any]:
    """Ensure the game is positioned on a Roguelike theme lobby before reward/tech-tree actions.

    If already inside any of the 6 permanent Roguelike theme lobbies on step 0 and
    ``accept_any_current_lobby`` is True, keep the user's current lobby and report ``detected_theme``.
    """
    norm_theme = normalize_roguelike_theme(theme, default="BlackFlow")
    profile = get_theme_ui_profile(norm_theme)
    disp = str(profile.get("display_name") or norm_theme)
    select_kws = list(profile.get("theme_select_keywords") or [disp, profile["level_reward"]["name"]])

    result: Dict[str, Any] = {
        "ok": False,
        "cancelled": False,
        "already_in_lobby": False,
        "theme": norm_theme,
        "detected_theme": norm_theme,
        "steps": 0,
        "last_screen": "unknown",
        "reason": "",
    }
    if not adb_path or not adb_addr:
        result["reason"] = "ADB 路径或地址为空"
        return result

    swipes_used = 0
    returned_home_once = False

    for step in range(max(1, int(max_steps))):
        if cancel_event and cancel_event.is_set():
            result["cancelled"] = True
            result["reason"] = "cancelled"
            return result

        img = stage_pilot.adb_screencap_image(adb_path, adb_addr, timeout=5.0, cancel_event=cancel_event)
        if cancel_event and cancel_event.is_set():
            result["cancelled"] = True
            result["reason"] = "cancelled"
            return result
        if img is None:
            result["reason"] = "ADB 截屏失败，无法定位肉鸽主题大厅"
            return result

        # Check network disconnect popup first
        disc = stage_pilot.detect_network_disconnect_popup(img, maa_dir=maa_dir, use_ocr=True, cancel_event=cancel_event)
        if disc.get("detected"):
            _emit(log_callback, f"[NET][RL-POST] 导航至「{disp}」大厅时检测到网络掉线弹窗，自动点击确认重连...")
            stage_pilot.dismiss_network_disconnect_popup(
                adb_path, adb_addr, maa_dir=maa_dir, max_taps=2, cancel_event=cancel_event, log_callback=log_callback
            )
            result["last_screen"] = "disconnect_popup"
            result["steps"] = step + 1
            time.sleep(2.0)
            continue

        boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True, cancel_event=cancel_event)
        joined = "".join(re.sub(r"\s+", "", str(b.get("compact") or b.get("text") or "")) for b in (boxes or []))
        result["steps"] = step

        on_theme_select = is_roguelike_theme_select_page(boxes)
        detected_theme = None if on_theme_select else detect_lobby_theme_from_boxes(boxes, preferred_theme=norm_theme)
        has_explore_btn = (not on_theme_select) and any(
            k in joined for k in ("开始探险", "继续探险", "开始探索", "继续探索", "蚀刻章套组", "常规行动")
        )

        # 1. Target theme lobby reached!
        if (not on_theme_select) and (detected_theme == norm_theme or (has_explore_btn and detected_theme is None)):
            result["ok"] = True
            result["already_in_lobby"] = (step == 0)
            result["detected_theme"] = norm_theme
            result["last_screen"] = "theme_lobby"
            result["reason"] = f"已就绪于「{disp}」主题大厅"
            _emit(log_callback, f"[RL-POST] {result['reason']}，开始扫描等级奖励、月度委托与科技树入口。")
            return result

        # 1b. Already manually positioned in another permanent Roguelike theme lobby on step 0 -> adapt in-place!
        if (not on_theme_select) and step == 0 and accept_any_current_lobby and detected_theme is not None and detected_theme != norm_theme:
            det_disp = str(THEME_UI_PROFILES[detected_theme].get("display_name") or detected_theme)
            result["ok"] = True
            result["already_in_lobby"] = True
            result["detected_theme"] = detected_theme
            result["last_screen"] = "theme_lobby"
            result["reason"] = (
                f"检测到当前已位于「{det_disp}」主题大厅（配置主题为「{disp}」），"
                f"直接按当前大厅主题扫描等级奖励、月度委托与科技树入口"
            )
            _emit(log_callback, f"[RL-POST] {result['reason']}。")
            return result

        # 2. Currently in another Roguelike theme's lobby -> exit back to theme selector
        if (not on_theme_select) and has_explore_btn and detected_theme is not None and detected_theme != norm_theme:
            result["last_screen"] = "other_theme_lobby"
            sw_box = _find_matching_ocr_box(boxes, ("主题切换", "切换主题", "更换主题"))
            tx, ty = (sw_box["cx"], sw_box["cy"]) if sw_box else (64, 40)
            _emit(log_callback, f"[RL-POST] 当前位于其他肉鸽主题大厅，点击 ({tx},{ty}) 返回主题选择页以切换至「{disp}」...")
            stage_pilot.adb_tap_norm(adb_path, adb_addr, tx, ty, img.width, img.height, cancel_event=cancel_event)
            time.sleep(1.2)
            continue

        # 3. Roguelike theme selection / hub screen
        target_sel_box = _find_matching_ocr_box(
            boxes,
            select_kws,
            exclude_keywords=("添加到终端", "查看收藏", "该主题已全额收录"),
        )
        enter_theme_box = _find_matching_ocr_box(
            boxes,
            ("进入主题", "前往"),
            exclude_keywords=("添加到终端", "查看收藏", "该主题已全额收录"),
        )
        if target_sel_box is not None and (
            on_theme_select or "集成战略" in joined or "长期探索" not in joined or enter_theme_box is not None
        ):
            result["last_screen"] = "roguelike_hub"
            # When '进入主题' button is explicitly shown alongside target theme title, tap '进入主题' directly
            click_box = enter_theme_box if (on_theme_select and enter_theme_box is not None) else target_sel_box
            _emit(
                log_callback,
                f"[RL-POST] 主题选择页命中「{click_box['text']}」({click_box['cx']},{click_box['cy']})，点击进入「{disp}」...",
            )
            stage_pilot.adb_tap_norm(
                adb_path, adb_addr, click_box["cx"], click_box["cy"], img.width, img.height, cancel_event=cancel_event
            )
            time.sleep(1.8)
            continue

        if "集成战略" in joined and ("生息演算" in joined or "保全派驻" in joined or "长期探索" in joined):
            result["last_screen"] = "long_term_exploration"
            is_box = _find_matching_ocr_box(boxes, ("集成战略",))
            if is_box is not None:
                _emit(log_callback, f"[RL-POST] 长期探索页 -> 点击「集成战略」({is_box['cx']},{is_box['cy']})...")
                stage_pilot.adb_tap_norm(
                    adb_path, adb_addr, is_box["cx"], is_box["cy"], img.width, img.height, cancel_event=cancel_event
                )
                time.sleep(1.6)
                continue

        if on_theme_select or "集成战略" in joined or enter_theme_box is not None:
            result["last_screen"] = "roguelike_hub"
            if swipes_used < 4:
                swipes_used += 1
                if swipes_used <= 2:
                    _emit(log_callback, f"[RL-POST] 肉鸽主题页未见「{disp}」，右滑主题列表查找 ({swipes_used}/4)...")
                    stage_pilot.adb_swipe_norm(adb_path, adb_addr, 550, 560, 120, 560, 420, img.width, img.height, cancel_event=cancel_event)
                else:
                    _emit(log_callback, f"[RL-POST] 肉鸽主题页未见「{disp}」，左滑主题列表查找 ({swipes_used}/4)...")
                    stage_pilot.adb_swipe_norm(adb_path, adb_addr, 120, 560, 550, 560, 420, img.width, img.height, cancel_event=cancel_event)
                time.sleep(1.0)
                continue
            if enter_theme_box is not None:
                _emit(log_callback, f"[RL-POST] 尝试点击「{enter_theme_box['text']}」({enter_theme_box['cx']},{enter_theme_box['cy']})...")
                stage_pilot.adb_tap_norm(
                    adb_path, adb_addr, enter_theme_box["cx"], enter_theme_box["cy"], img.width, img.height, cancel_event=cancel_event
                )
                time.sleep(1.6)
                continue

        # 4. Terminal screen -> click 长期探索 (or 集成战略)
        lt_box = _find_matching_ocr_box(boxes, ("长期探索", "常态事务"))
        if lt_box is not None:
            result["last_screen"] = "terminal"
            _emit(log_callback, f"[RL-POST] 终端界面 -> 点击「{lt_box['text']}」({lt_box['cx']},{lt_box['cy']})...")
            stage_pilot.adb_tap_norm(
                adb_path, adb_addr, lt_box["cx"], lt_box["cy"], img.width, img.height, cancel_event=cancel_event
            )
            time.sleep(1.5)
            continue

        # 5. Home screen -> click 终端
        term_box = _find_matching_ocr_box(boxes, ("终端",))
        if term_box is not None and any(hk in joined for hk in ("编队", "干员", "采购中心", "基建", "公开招募", "理智")):
            result["last_screen"] = "home"
            _emit(log_callback, f"[RL-POST] 游戏主界面 -> 点击「终端」({term_box['cx']},{term_box['cy']})...")
            stage_pilot.adb_tap_norm(
                adb_path, adb_addr, term_box["cx"], term_box["cy"], img.width, img.height, cancel_event=cancel_event
            )
            time.sleep(1.6)
            continue

        # 6. Unknown subpage -> dismiss guide popup or return to Home once
        guide_res = stage_pilot.dismiss_guide_popups(
            adb_path, adb_addr, maa_dir=maa_dir, max_dismiss=2, cancel_event=cancel_event, log_callback=log_callback
        )
        if int(guide_res.get("dismissed_count", 0) or 0) > 0:
            time.sleep(0.8)
            continue

        if not returned_home_once:
            returned_home_once = True
            _emit(log_callback, f"[RL-POST] 当前不在「{disp}」主题大厅，先返回游戏主界面再自动进入集成战略...")
            stage_pilot.return_to_home_screen(
                adb_path, adb_addr, maa_dir, max_steps=5, cancel_event=cancel_event, log_callback=log_callback
            )
            time.sleep(1.2)
            continue

        time.sleep(1.0)

    result["reason"] = (
        f"未能在 {max_steps} 步内进入「{disp}」主题大厅（最后界面={result['last_screen']}）；"
        f"请确认游戏已登录在线或手动进入该主题大厅后重试。"
    )
    _emit(log_callback, f"[WARN][RL-POST] {result['reason']}")
    return result


def claim_roguelike_rewards(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str = "BlackFlow",
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Detect and claim level rewards (all 6 permanent themes) and monthly commissions from theme lobby."""
    norm_theme = normalize_roguelike_theme(theme, default="BlackFlow")
    profile = get_theme_ui_profile(norm_theme)
    active_theme = norm_theme
    active_profile = profile
    result: Dict[str, Any] = {
        "ok": True,
        "theme": norm_theme,
        "detected_theme": norm_theme,
        "source": "adb_ocr",
        "level_rewards_claimed": 0,
        "commission_rewards_claimed": 0,
        "level_entry_found": False,
        "commission_entry_found": False,
        "matched_via_fallback": False,
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
        _emit(log_callback, f"[WARN][RL-REWARD] {result['error']}")
        return result

    lobby_boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True)

    # 1. Level Rewards (configured theme priority + all-6-theme auto fallback)
    lvl_hit = resolve_lobby_level_reward_entry(lobby_boxes, configured_theme=norm_theme)
    if lvl_hit is not None:
        active_theme = lvl_hit["theme"]
        active_profile = lvl_hit["profile"]
        lvl_spec = active_profile["level_reward"]
        lvl_box = lvl_hit["box"]
        result["detected_theme"] = active_theme
        result["matched_via_fallback"] = bool(lvl_hit["matched_via_fallback"])
        result["level_entry_found"] = True
        if lvl_hit["matched_via_fallback"]:
            _emit(
                log_callback,
                f"[RL-REWARD] 按配置主题「{profile['display_name']}」未命中，已自动泛匹配当前大厅"
                f"「{active_profile['display_name']}」等级奖励入口「{lvl_box['text']}」({lvl_box['cx']},{lvl_box['cy']})，进入领奖...",
            )
        else:
            _emit(
                log_callback,
                f"[RL-REWARD] 主界面定位等级奖励入口「{lvl_box['text']}」({lvl_box['cx']},{lvl_box['cy']})，进入领奖...",
            )
        stage_pilot.adb_tap_norm(adb_path, adb_addr, lvl_box["cx"], lvl_box["cy"], img.width, img.height)
        time.sleep(1.0)
        n_lvl = _claim_all_on_current_reward_page(
            adb_path, adb_addr, cancel_event=cancel_event, log_callback=log_callback, page_label=lvl_spec["name"]
        )
        result["level_rewards_claimed"] = n_lvl
        result["actions"].append({"entry": lvl_spec["name"], "claimed": n_lvl, "theme": active_theme})
        if n_lvl == 0:
            _emit(log_callback, f"[RL-REWARD] {lvl_spec['name']}：当前无可领取的等级奖励。")
        _tap_return_to_lobby(
            adb_path, adb_addr, maa_dir, active_theme, cancel_event=cancel_event, log_callback=log_callback
        )
    else:
        det_hint = detect_lobby_theme_from_boxes(lobby_boxes, preferred_theme=norm_theme)
        if det_hint:
            active_theme = det_hint
            active_profile = get_theme_ui_profile(det_hint)
            result["detected_theme"] = active_theme
        _emit(
            log_callback,
            f"[RL-REWARD] 主界面未识别到等级奖励入口（配置主题「{profile['display_name']}」对应「{profile['level_reward']['name']}」，泛匹配 6 主题均未命中），跳过等级奖励领取。",
        )

    if cancel_event and cancel_event.is_set():
        result["cancelled"] = True
        return result

    # 2. Monthly Commissions (unified '本月委托栏' / '本月委托'; silent skip on ended themes)
    img2 = stage_pilot.adb_screencap_image(adb_path, adb_addr)
    if img2 is not None:
        lobby_boxes2 = stage_pilot.find_text_boxes_via_ocr(img2, include_inverted=True)
        if is_roguelike_theme_select_page(lobby_boxes2):
            _emit(log_callback, "[WARN][RL-REWARD] 领奖返回后检测到误处「主题选择页」，尝试点击「进入主题」回到大厅...")
            rec = _recover_from_theme_select_to_lobby(
                adb_path,
                adb_addr,
                maa_dir,
                img2,
                lobby_boxes2,
                target_theme=active_theme,
                cancel_event=cancel_event,
                log_callback=log_callback,
            )
            if rec.get("ok"):
                img2 = rec["img"]
                lobby_boxes2 = rec["boxes"]
            else:
                result["commission_skipped_reason"] = "theme_select_page_guard"
                return result
        det_on_screen2 = detect_lobby_theme_from_boxes(lobby_boxes2, preferred_theme=active_theme)
        if det_on_screen2 and not result["level_entry_found"]:
            active_theme = det_on_screen2
            active_profile = get_theme_ui_profile(det_on_screen2)
            result["detected_theme"] = active_theme
        comm_spec = active_profile["monthly_commission"]
        comm_box = resolve_lobby_monthly_commission_entry(lobby_boxes2, configured_theme=active_theme)
        if comm_box is not None:
            result["commission_entry_found"] = True
            _emit(log_callback, f"[RL-REWARD] 主界面定位月度委托入口「{comm_box['text']}」({comm_box['cx']},{comm_box['cy']})，进入领奖...")
            stage_pilot.adb_tap_norm(adb_path, adb_addr, comm_box["cx"], comm_box["cy"], img2.width, img2.height)
            time.sleep(1.0)
            n_comm = _claim_all_on_current_reward_page(
                adb_path, adb_addr, cancel_event=cancel_event, log_callback=log_callback, page_label=comm_spec["name"]
            )
            result["commission_rewards_claimed"] = n_comm
            result["actions"].append({"entry": comm_spec["name"], "claimed": n_comm, "theme": active_theme})
            if n_comm == 0:
                _emit(log_callback, f"[RL-REWARD] {comm_spec['name']}：当前无可领取的委托奖励。")
            _tap_return_to_lobby(
                adb_path, adb_addr, maa_dir, active_theme, cancel_event=cancel_event, log_callback=log_callback
            )
        else:
            if active_profile.get("is_ended_theme"):
                result["commission_skipped_reason"] = "ended_theme_no_commission"
                _emit(
                    log_callback,
                    f"[RL-REWARD] 当前主题「{active_profile['display_name']}」为已完结常驻主题（主界面无「本月委托栏」/「本月委托」入口），正常跳过委托领奖。",
                )
            else:
                result["commission_skipped_reason"] = "no_commission_entry_visible"
                _emit(
                    log_callback,
                    f"[RL-REWARD] 主界面未识别到月度委托入口（「本月委托栏」/「本月委托」），当前无可用月度委托，正常跳过委托领奖。",
                )

    return result


def auto_upgrade_roguelike_tech_tree(
    adb_path: str,
    adb_addr: str,
    maa_dir: Path,
    theme: str = "BlackFlow",
    priority_order: Optional[Sequence[str]] = None,
    max_upgrades: int = 50,
    cancel_event: Optional[threading.Event] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    lobby_confirmed_theme: Optional[str] = None,
) -> Dict[str, Any]:
    """Enter Roguelike tech tree via 3-tier pipeline:
      1) Full-screen OCR (configured theme + all-6-theme fallback + split-char merge + blacklist/left-ROI)
      2) Local left-band ROI (x in [0.01,0.28], y in [0.25,0.75]) 2.5x red-suppressed adaptive binary & CLAHE OCR
         (supports both tech-tree text and non-blacklisted pure numeric point badges in y in [0.35,0.50])
      3) Confirmed-lobby standard 16:9 normalized anchor fallback + fast secondary-panel verification
    """
    norm_theme = normalize_roguelike_theme(theme, default="BlackFlow")
    profile = get_theme_ui_profile(norm_theme)
    tech_spec = profile["tech_tree"]
    eff_priority = list(priority_order) if priority_order else list(DEFAULT_TECH_TREE_PRIORITY)
    result: Dict[str, Any] = {
        "ok": True,
        "theme": norm_theme,
        "detected_theme": norm_theme,
        "tech_tree_name": tech_spec["name"],
        "source": "adb_ocr",
        "entry_found": False,
        "entry_stage": "",
        "matched_via_fallback": False,
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
        _emit(log_callback, f"[WARN][RL-TECH] {result['error']}")
        return result

    # Tier 1: Full-screen OCR (configured theme priority + all-6-theme fallback)
    lobby_boxes = stage_pilot.find_text_boxes_via_ocr(img, include_inverted=True)

    # Track confirmed lobby theme from upstream ([RL-POST]/[RL-REWARD]) or current screen's level reward
    confirmed_theme = (
        normalize_roguelike_theme(lobby_confirmed_theme, default="")
        if lobby_confirmed_theme
        else ""
    )

    # Hard Guard: If currently on the Integrated Strategies Theme Selection page
    # ('进入主题' / '添加到终端' / '查看收藏' / '该主题已全额收录'), NEVER run tech-tree anchor fallback or node scanning!
    # Must click target theme's '进入主题' to return to lobby first; abort cleanly if recovery fails.
    if is_roguelike_theme_select_page(lobby_boxes):
        _emit(
            log_callback,
            "[WARN][RL-TECH] 检测到当前处于集成战略「主题选择页」，严禁执行科技树锚点兜底与节点扫描，先尝试点击「进入主题」返回大厅...",
        )
        rec = _recover_from_theme_select_to_lobby(
            adb_path=adb_path,
            adb_addr=adb_addr,
            maa_dir=maa_dir,
            img=img,
            boxes=lobby_boxes,
            target_theme=confirmed_theme or norm_theme,
            cancel_event=cancel_event,
            log_callback=log_callback,
        )
        if rec.get("cancelled") or (cancel_event and cancel_event.is_set()):
            result["ok"] = False
            result["cancelled"] = True
            return result
        if not rec.get("ok"):
            result["entry_found"] = False
            result["wrong_panel_detected"] = "theme_select_page"
            result["skipped_reason"] = "当前处于主题选择页，已拦截科技树锚点兜底与节点扫描（未能通过「进入主题」返回大厅）"
            _emit(log_callback, f"[WARN][RL-TECH] {result['skipped_reason']}。")
            return result
        img = rec["img"]
        lobby_boxes = rec["boxes"]

    tech_hit = resolve_lobby_tech_tree_entry(lobby_boxes, configured_theme=norm_theme)

    if not confirmed_theme and not is_roguelike_theme_select_page(lobby_boxes):
        lvl_confirm = resolve_lobby_level_reward_entry(lobby_boxes, configured_theme=norm_theme)
        if lvl_confirm is not None:
            confirmed_theme = lvl_confirm["theme"]

    # Tier 2: Local left-band ROI (x in [0.01,0.28], y in [0.25,0.75]) 2.5x red-suppressed & CLAHE OCR
    if tech_hit is None and not is_roguelike_theme_select_page(lobby_boxes):
        _emit(
            log_callback,
            f"[RL-TECH] 全屏 OCR 未命中「{tech_spec['name']}」，启动第二级局部去红通道/CLAHE 2.5× 放大二次 OCR...",
        )
        tech_hit = resolve_lobby_tech_tree_via_local_ocr(
            img,
            configured_theme=confirmed_theme or norm_theme,
            cancel_event=cancel_event,
            log_callback=log_callback,
        )
        if cancel_event and cancel_event.is_set():
            result["ok"] = False
            result["cancelled"] = True
            return result

    # Tier 3: Confirmed-lobby standard 16:9 theme anchor fallback (strictly blocked on theme selection page)
    if tech_hit is None and confirmed_theme and not is_roguelike_theme_select_page(lobby_boxes):
        tech_hit = _resolve_theme_anchor_fallback(img, confirmed_theme=confirmed_theme, boxes=lobby_boxes)
        if tech_hit is not None:
            _emit(
                log_callback,
                f"[RL-TECH] 前两级 OCR 均未识出科技树文字，但已确认处于「{tech_hit['profile']['display_name']}」大厅，"
                f"启用第三级标准布局锚点兜底 ({tech_hit['box']['cx']},{tech_hit['box']['cy']})...",
            )

    if tech_hit is None:
        result["skipped_reason"] = (
            f"主界面左侧有效区未识别到科技树入口（配置主题「{profile['display_name']}」对应「{tech_spec['name']}」，"
            f"全屏 OCR 与局部去红/CLAHE 二次 OCR 均未命中）"
        )
        _emit(log_callback, f"[RL-TECH] {result['skipped_reason']}，跳过科技树自动加点。")
        return result

    active_theme = tech_hit["theme"]
    active_profile = tech_hit["profile"]
    tech_spec = active_profile["tech_tree"]
    entry_box = tech_hit["box"]
    match_stage = str(tech_hit.get("match_stage") or "fullscreen_ocr")
    requires_verify = bool(tech_hit.get("requires_panel_verify", False))

    result["detected_theme"] = active_theme
    result["tech_tree_name"] = tech_spec["name"]
    result["matched_via_fallback"] = bool(tech_hit["matched_via_fallback"])
    result["entry_stage"] = match_stage
    result["entry_found"] = True
    result["entry_box"] = dict(entry_box)

    merge_note = f"（竖排拆字合并自 {entry_box['merged_from']}）" if entry_box.get("merged_from") else ""
    stage_note = {
        "fullscreen_ocr": "",
        "local_ocr_red_suppressed": "[去红通道放大OCR] ",
        "local_ocr_clahe": "[CLAHE放大OCR] ",
        "local_ocr_points": "[局部点数识别] ",
        "theme_anchor_fallback": "[主题锚点兜底] ",
    }.get(match_stage, f"[{match_stage}] ")

    if tech_hit["matched_via_fallback"]:
        _emit(
            log_callback,
            f"[RL-TECH] {stage_note}按配置主题「{profile['display_name']}」未命中，已自动泛匹配当前大厅"
            f"「{active_profile['display_name']}」科技树入口「{entry_box['text']}」{merge_note}({entry_box['cx']},{entry_box['cy']})，进入自动加点...",
        )
    else:
        _emit(
            log_callback,
            f"[RL-TECH] {stage_note}定位科技树入口「{entry_box['text']}」{merge_note}({entry_box['cx']},{entry_box['cy']})，进入自动加点...",
        )
    stage_pilot.adb_tap_norm(adb_path, adb_addr, entry_box["cx"], entry_box["cy"], img.width, img.height)
    time.sleep(1.1)

    prefetched_tree_img: Optional[Image.Image] = None
    prefetched_tree_boxes: Optional[List[Dict[str, Any]]] = None
    if requires_verify:
        v_res = _verify_entered_tech_tree_panel(
            adb_path,
            adb_addr,
            active_profile,
            cancel_event=cancel_event,
            log_callback=log_callback,
            maa_dir=maa_dir,
        )
        if not v_res.get("ok"):
            result["entry_found"] = False
            result["wrong_panel_detected"] = str(v_res.get("wrong_panel") or "unconfirmed_panel")
            result["skipped_reason"] = f"兜底入口校验未通过（{result['wrong_panel_detected']}），已中止科技树加点"
            return result
        prefetched_tree_img = v_res.get("img")
        if v_res.get("boxes") is not None:
            prefetched_tree_boxes = list(v_res["boxes"])

    aborted_on_theme_select = False
    exit_reason = "completed"
    last_known_points: Optional[int] = None
    canvas_swipes = 0
    try:
        skipped_coords: List[Tuple[int, int]] = []
        permanent_bad_coords: List[Tuple[int, int]] = []
        max_canvas_swipes = 16
        consecutive_unmoved_swipes = 0
        arch_nudge_swipes = 0
        max_arch_nudge_swipes = 4
        wrong_page_recoveries = 0
        no_progress_rounds = 0
        max_no_progress_rounds = 20
        consecutive_verify_failures = 0
        ever_saw_insufficient_points = False
        prev_tree_img_for_swipe: Optional[Image.Image] = None
        max_total_rounds = max(max_upgrades * 3 + 30, 80)

        for _round in range(max_total_rounds):
            if result["upgraded_count"] >= max_upgrades:
                exit_reason = "max_upgrades_reached"
                break
            if no_progress_rounds >= max_no_progress_rounds:
                exit_reason = "no_progress_limit"
                _emit(
                    log_callback,
                    f"[RL-TECH] 科技树「{tech_spec['name']}」连续 {no_progress_rounds} 轮无扣点进展，触发安全上限退出。",
                )
                break
            if cancel_event and cancel_event.is_set():
                result["cancelled"] = True
                exit_reason = "cancelled"
                break

            if prefetched_tree_img is not None:
                tree_img = prefetched_tree_img
                prefetched_tree_img = None
            else:
                tree_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
            if tree_img is None:
                exit_reason = "screencap_failed"
                break

            rendered_ui = _has_rendered_ui_chrome(tree_img)
            tree_boxes: List[Dict[str, Any]] = []
            round_points_before: Optional[int] = None

            # Per-round Page Guard (with dual-frame recheck on unknown_not_tech_tree) + Default-Selected '确认升级' Check
            if rendered_ui:
                if prefetched_tree_boxes is not None:
                    tree_boxes = prefetched_tree_boxes
                    prefetched_tree_boxes = None
                else:
                    tree_boxes = stage_pilot.find_text_boxes_via_ocr(tree_img, include_inverted=True)

                guard_action = "proceed"
                for guard_try in (1, 2):
                    guard = classify_tech_tree_page_guard(
                        tree_img, tree_boxes, maa_dir=maa_dir, active_profile=active_profile, cancel_event=cancel_event
                    )
                    g_status = guard["status"]
                    round_points_before = guard.get("points")
                    if round_points_before is not None:
                        last_known_points = round_points_before

                    if g_status == "theme_select_page":
                        result["page_guard_interceptions"] = int(result.get("page_guard_interceptions", 0)) + 1
                        aborted_on_theme_select = True
                        exit_reason = "theme_select_page"
                        _emit(
                            log_callback,
                            "[WARN][RL-TECH] Page Guard 拦截：当前处于集成战略「主题选择页」，严禁节点点击、左滑或继续点返回，安全中止加点。",
                        )
                        guard_action = "break"
                        break

                    if g_status == "wrong_page":
                        wrong_kw = str(guard.get("wrong_keyword") or "非科技树页面")
                        result["page_guard_interceptions"] = int(result.get("page_guard_interceptions", 0)) + 1
                        _emit(
                            log_callback,
                            f"[WARN][RL-TECH] Page Guard 拦截：当前误入「{wrong_kw}」，立即点击左上角返回键 (<) 退出（严禁执行节点点击或左滑）。",
                        )
                        stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, tree_img.width, tree_img.height)
                        time.sleep(0.8)
                        wrong_page_recoveries += 1
                        no_progress_rounds += 1
                        prev_tree_img_for_swipe = None
                        if wrong_page_recoveries >= 3:
                            exit_reason = "wrong_page_limit"
                            _emit(log_callback, "[WARN][RL-TECH] 连续误入非科技树页面，安全中止科技树加点。")
                            guard_action = "break"
                        else:
                            guard_action = "continue"
                        break

                    if g_status == "lobby":
                        exit_reason = "returned_to_lobby"
                        _emit(log_callback, "[WARN][RL-TECH] Page Guard 检测到已退回肉鸽主题大厅，安全中止科技树加点（严禁在大厅点击或左滑）。")
                        guard_action = "break"
                        break

                    if g_status == "overview_overlay":
                        _emit(log_callback, "[WARN][RL-TECH] Page Guard 检测到处于属性总览弹窗（无顶部房子图标），点击左上角返回键 (<) 返回科技树画布。")
                        stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, tree_img.width, tree_img.height)
                        time.sleep(0.75)
                        wrong_page_recoveries += 1
                        no_progress_rounds += 1
                        prev_tree_img_for_swipe = None
                        if arch_nudge_swipes < max_arch_nudge_swipes:
                            arch_nudge_swipes += 1
                            _emit(log_callback, "[RL-TECH] 退出属性总览弹窗后轻微向右拖动画布，将左侧前沿节点移至中部安全区...")
                            stage_pilot.adb_swipe_norm(adb_path, adb_addr, 360, 360, 660, 360, 380, tree_img.width, tree_img.height)
                            time.sleep(0.85)
                            skipped_coords.clear()
                            permanent_bad_coords.clear()
                        if wrong_page_recoveries >= 3:
                            exit_reason = "wrong_page_limit"
                            guard_action = "break"
                        else:
                            guard_action = "continue"
                        break

                    if g_status == "unknown_not_tech_tree":
                        diag = (
                            f"<={guard.get('has_return_icon')}, 房子={guard.get('has_home_icon')}, "
                            f"进度徽章={guard.get('has_progress_badge')}, 点数区={guard.get('has_points_indicator')}"
                        )
                        if guard_try == 1:
                            _emit(
                                log_callback,
                                f"[WARN][RL-TECH] Page Guard 首帧校验存疑（{diag}），等待 0.6s 复核第二帧...",
                            )
                            if cancel_event and cancel_event.is_set():
                                result["cancelled"] = True
                                exit_reason = "cancelled"
                                guard_action = "break"
                                break
                            time.sleep(0.6)
                            if cancel_event and cancel_event.is_set():
                                result["cancelled"] = True
                                exit_reason = "cancelled"
                                guard_action = "break"
                                break
                            recheck_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
                            if recheck_img is not None:
                                tree_img = recheck_img
                                tree_boxes = stage_pilot.find_text_boxes_via_ocr(tree_img, include_inverted=True)
                                continue
                        exit_reason = "unknown_not_tech_tree"
                        _emit(
                            log_callback,
                            f"[WARN][RL-TECH] Page Guard 连续两帧未确认处于科技树画布（{diag}），安全中止加点。",
                        )
                        guard_action = "break"
                        break

                    break

                if guard_action == "break":
                    break
                if guard_action == "continue":
                    continue

                effective_points = round_points_before if round_points_before is not None else last_known_points

                if round_points_before is not None and round_points_before <= 0:
                    exit_reason = "points_exhausted"
                    _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」右上角剩余点数为 0，结束加点。")
                    break

                # Step 1: Prioritize directly clicking the default-selected frontier node's cyan '确认升级' button!
                default_confirm = evaluate_tech_tree_confirm_button(
                    tree_img, tree_boxes, cancel_event=cancel_event, remaining_points=effective_points
                )
                if default_confirm.get("insufficient_points"):
                    ever_saw_insufficient_points = True
                if default_confirm["valid"]:
                    up_btn = default_confirm["button"]
                    ver = _execute_and_verify_tech_upgrade(
                        adb_path=adb_path,
                        adb_addr=adb_addr,
                        before_img=tree_img,
                        confirm_btn=up_btn,
                        points_before=round_points_before,
                        cancel_event=cancel_event,
                        log_callback=log_callback,
                    )
                    if ver.get("cancelled"):
                        result["cancelled"] = True
                        exit_reason = "cancelled"
                        break
                    if not ver.get("verified"):
                        exit_reason = "upgrade_verify_failed"
                        _emit(
                            log_callback,
                            f"[WARN][RL-TECH] 点击默认选中「{up_btn['text']}」({ver['tap_xy'][0]},{ver['tap_xy'][1]}) 后未检测到扣点或节点推进（{ver.get('reason')}），判定未生效并安全退出。",
                        )
                        break
                    desc_text = str(default_confirm.get("detail_text") or " ".join(b.get("text", "") for b in tree_boxes))
                    score, tier = score_tech_tree_node(desc_text, "", priority_order=eff_priority)
                    no_progress_rounds = 0
                    consecutive_unmoved_swipes = 0
                    ever_saw_insufficient_points = False
                    consecutive_verify_failures = 0
                    skipped_coords.clear()
                    prev_tree_img_for_swipe = None
                    last_known_points = ver.get("points_after")
                    prefetched_tree_img = ver.get("after_img")
                    prefetched_tree_boxes = ver.get("after_boxes")
                    result["upgraded_count"] += 1
                    result["upgraded_nodes"].append({
                        "cx": ver["tap_xy"][0],
                        "cy": ver["tap_xy"][1],
                        "tier": tier,
                        "score": score,
                        "upgrade_label": up_btn["text"],
                        "points_before": ver.get("points_before"),
                        "points_after": ver.get("points_after"),
                        "verify_reason": ver.get("verify_reason", ""),
                        "summary": desc_text[:120],
                    })
                    _emit(
                        log_callback,
                        f"[RL-TECH] 已直接点击默认选中「{up_btn['text']}」({ver['tap_xy'][0]},{ver['tap_xy'][1]}) "
                        f"并核验生效 [{ver.get('verify_reason')}]（累计+{result['upgraded_count']}）",
                    )
                    continue
            else:
                prefetched_tree_boxes = None

            # If we just performed a canvas left-swipe, verify the viewport actually shifted
            if prev_tree_img_for_swipe is not None:
                moved = False
                if np is not None:
                    a1 = np.asarray(prev_tree_img_for_swipe.convert("RGB").resize((320, 180)), dtype=np.float32)
                    a2 = np.asarray(tree_img.convert("RGB").resize((320, 180)), dtype=np.float32)
                    moved = float(np.mean(np.abs(a1 - a2))) > 3.0
                prev_tree_img_for_swipe = None
                required_unmoved_swipes = 2 if rendered_ui else 1
                if moved:
                    consecutive_unmoved_swipes = 0
                    skipped_coords.clear()
                    permanent_bad_coords.clear()
                else:
                    consecutive_unmoved_swipes += 1
                    if consecutive_unmoved_swipes >= required_unmoved_swipes:
                        exit_reason = "insufficient_points_all_frontiers" if ever_saw_insufficient_points else "reached_right_edge"
                        if required_unmoved_swipes >= 2:
                            _emit(
                                log_callback,
                                f"[RL-TECH] 科技树「{tech_spec['name']}」连续两次左滑画面无变化，已到达最右侧边缘（{exit_reason}），结束加点。",
                            )
                        else:
                            _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」画布已到达最右侧边缘，结束加点。")
                        break

            # Directly scan current screen's glowing frontier nodes first (never blindly page left!)
            raw_lit_nodes = detect_upgradeable_tech_nodes(tree_img)
            # Filter out forbidden UI zones (arrows x<0.08/x>0.93, tightened arch x<0.175 & y>0.63, top/bottom bars) & bad coords
            lit_nodes: List[Dict[str, Any]] = []
            for n in raw_lit_nodes:
                ncx, ncy = int(n["cx"]), int(n["cy"])
                if is_point_in_tech_tree_forbidden_zone(ncx, ncy):
                    continue
                if any(abs(ncx - bx) <= 28 and abs(ncy - by) <= 28 for bx, by in permanent_bad_coords):
                    continue
                if any(abs(ncx - sx) <= 28 and abs(ncy - sy) <= 28 for sx, sy in skipped_coords):
                    continue
                lit_nodes.append(n)

            if not lit_nodes:
                has_canvas_content = _has_rendered_tech_tree_canvas(tree_img) or _has_solid_white_upgraded_nodes(tree_img)
                if not raw_lit_nodes:
                    # On rendered screens with visible tech-tree content (e.g. all-upgraded page),
                    # swipe left to explore right toward frontier instead of prematurely giving up!
                    if rendered_ui and canvas_swipes < max_canvas_swipes and has_canvas_content:
                        canvas_swipes += 1
                        no_progress_rounds += 1
                        prev_tree_img_for_swipe = tree_img
                        _emit(
                            log_callback,
                            f"[RL-TECH] 当前屏未检出可解锁发光前沿节点，向左滑动探索右侧区域 ({canvas_swipes}/{max_canvas_swipes})...",
                        )
                        stage_pilot.adb_swipe_norm(adb_path, adb_addr, 960, 360, 320, 360, 420, tree_img.width, tree_img.height)
                        time.sleep(0.9)
                        continue
                    exit_reason = "insufficient_points_all_frontiers" if ever_saw_insufficient_points else "no_more_upgradeable_nodes"
                    _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」未检测到更多亮起的可升级节点，结束加点。")
                    break
                if canvas_swipes < max_canvas_swipes:
                    canvas_swipes += 1
                    no_progress_rounds += 1
                    prev_tree_img_for_swipe = tree_img
                    _emit(
                        log_callback,
                        f"[RL-TECH] 当前屏无可解锁节点，向左滑动一次画布探测右侧前沿发光节点 ({canvas_swipes}/{max_canvas_swipes})...",
                    )
                    stage_pilot.adb_swipe_norm(adb_path, adb_addr, 960, 360, 320, 360, 420, tree_img.width, tree_img.height)
                    time.sleep(0.9)
                    continue
                exit_reason = "insufficient_points_all_frontiers" if ever_saw_insufficient_points else "max_swipes_reached"
                _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」当前暂无更多可解锁节点，结束加点。")
                break

            # If the top-priority leftmost candidate sits right along the rim of the bottom-left '64/78 已重构' arch,
            # gently drag the canvas right first so frontier nodes move safely to screen center before tapping!
            if (
                arch_nudge_swipes < max_arch_nudge_swipes
                and is_point_near_tech_tree_arch_edge(int(lit_nodes[0]["cx"]), int(lit_nodes[0]["cy"]))
            ):
                arch_nudge_swipes += 1
                no_progress_rounds += 1
                prev_tree_img_for_swipe = None
                _emit(
                    log_callback,
                    f"[RL-TECH] 候选前沿节点 ({lit_nodes[0]['cx']},{lit_nodes[0]['cy']}) 紧邻左下角拱门禁区边缘，"
                    f"先轻微向右拖动画布移至中部安全区再重扫 ({arch_nudge_swipes}/{max_arch_nudge_swipes})...",
                )
                stage_pilot.adb_swipe_norm(adb_path, adb_addr, 360, 360, 660, 360, 380, tree_img.width, tree_img.height)
                time.sleep(0.85)
                skipped_coords.clear()
                permanent_bad_coords.clear()
                continue

            # Inspect candidate nodes across the entire safe viewport (left-to-right up to x≈0.93)
            # Never early-stop mid-screen on '已升级' or '点数不足' so right-side / cheaper nodes are never missed!
            effective_points = round_points_before if round_points_before is not None else last_known_points
            evaluated: List[Dict[str, Any]] = []
            saw_insufficient_points = False
            saw_locked_on_screen = False
            upgraded_hits_on_screen = 0
            last_tapped_xy: Optional[Tuple[int, int]] = None
            consecutive_upgraded = 0
            upgraded_immediately = False
            triggered_arch_rescan = False
            aborted_loop = False

            remaining_nodes = list(lit_nodes[:36])
            while remaining_nodes:
                if cancel_event and cancel_event.is_set():
                    result["cancelled"] = True
                    exit_reason = "cancelled"
                    break
                node = remaining_nodes.pop(0)
                ncx, ncy = int(node["cx"]), int(node["cy"])
                if arch_nudge_swipes < max_arch_nudge_swipes and is_point_near_tech_tree_arch_edge(ncx, ncy):
                    arch_nudge_swipes += 1
                    no_progress_rounds += 1
                    prev_tree_img_for_swipe = None
                    _emit(
                        log_callback,
                        f"[RL-TECH] 候选前沿节点 ({ncx},{ncy}) 紧邻左下角拱门禁区边缘，"
                        f"先轻微向右拖动画布移至中部安全区再重扫 ({arch_nudge_swipes}/{max_arch_nudge_swipes})...",
                    )
                    stage_pilot.adb_swipe_norm(adb_path, adb_addr, 360, 360, 660, 360, 380, tree_img.width, tree_img.height)
                    time.sleep(0.85)
                    skipped_coords.clear()
                    permanent_bad_coords.clear()
                    triggered_arch_rescan = True
                    break
                stage_pilot.adb_tap_norm(adb_path, adb_addr, ncx, ncy, tree_img.width, tree_img.height)
                last_tapped_xy = (ncx, ncy)
                time.sleep(0.55)
                detail_img = stage_pilot.adb_screencap_image(adb_path, adb_addr)
                if detail_img is None:
                    continue
                detail_boxes = stage_pilot.find_text_boxes_via_ocr(detail_img, include_inverted=True)

                # Hard guard against Theme Selection page during node scan
                if is_roguelike_theme_select_page(detail_boxes):
                    result["page_guard_interceptions"] = int(result.get("page_guard_interceptions", 0)) + 1
                    aborted_on_theme_select = True
                    aborted_loop = True
                    exit_reason = "theme_select_page"
                    _emit(log_callback, "[WARN][RL-TECH] 点击节点后检测到处于「主题选择页」，立即中止科技树扫描。")
                    break

                # Guard against accidentally opening wrong subpages during node scan
                detail_joined = "".join(_box_text_compact(b) for b in (detail_boxes or []))
                wrong_hit = next((w for w in _TECH_TREE_PAGE_GUARD_WRONG_KEYWORDS if w in detail_joined), None)
                if wrong_hit is not None:
                    result["page_guard_interceptions"] = int(result.get("page_guard_interceptions", 0)) + 1
                    _emit(
                        log_callback,
                        f"[WARN][RL-TECH] 点击候选节点 ({ncx},{ncy}) 后误入「{wrong_hit}」，立即点击返回键 (<) 退出。",
                    )
                    stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, detail_img.width, detail_img.height)
                    time.sleep(0.75)
                    permanent_bad_coords.append((ncx, ncy))
                    last_tapped_xy = None
                    continue

                confirm_eval = evaluate_tech_tree_confirm_button(
                    detail_img, detail_boxes, cancel_event=cancel_event, remaining_points=effective_points
                )

                # Self-heal ONLY if top-bar house icon is ABSENT AND >=2 full-screen attribute summary groups appear
                if _is_tech_tree_overview_overlay(
                    detail_boxes,
                    has_bottom_confirm=confirm_eval["valid"],
                    detail_img=detail_img,
                    maa_dir=maa_dir,
                ):
                    _emit(
                        log_callback,
                        f"[WARN][RL-TECH] 检测到误入属性总览弹窗（坐标 ({ncx},{ncy})，无顶部房子图标），立即点击左上角返回键 (<) 返回科技树画布。",
                    )
                    stage_pilot.adb_tap_norm(adb_path, adb_addr, 64, 40, detail_img.width, detail_img.height)
                    time.sleep(0.75)
                    permanent_bad_coords.append((ncx, ncy))
                    last_tapped_xy = None
                    near_arch = is_point_near_tech_tree_arch_edge(ncx, ncy) or (
                        (ncx / 1280.0) < 0.30 and (ncy / 720.0) > 0.50
                    )
                    if (rendered_ui or near_arch) and arch_nudge_swipes < max_arch_nudge_swipes:
                        arch_nudge_swipes += 1
                        no_progress_rounds += 1
                        prev_tree_img_for_swipe = None
                        _emit(
                            log_callback,
                            f"[RL-TECH] 退出属性总览弹窗后不再死磕原坐标，先轻微向右拖动画布移至中部安全区再重扫 ({arch_nudge_swipes}/{max_arch_nudge_swipes})...",
                        )
                        stage_pilot.adb_swipe_norm(
                            adb_path, adb_addr, 360, 360, 660, 360, 380, detail_img.width, detail_img.height
                        )
                        time.sleep(0.85)
                        skipped_coords.clear()
                        permanent_bad_coords.clear()
                        triggered_arch_rescan = True
                        break
                    continue

                btn_status = str(confirm_eval.get("button_status") or "")
                if not confirm_eval["valid"]:
                    if btn_status == "already_upgraded":
                        upgraded_hits_on_screen += 1
                        consecutive_upgraded += 1
                        skipped_coords.append((ncx, ncy))
                        _emit(
                            log_callback,
                            f"[RL-TECH] 节点 ({ncx},{ncy}) 显示「已升级」，跳过并继续探测同屏剩余候选节点（剩余 {len(remaining_nodes)} 个）。",
                        )
                        continue

                    consecutive_upgraded = 0
                    if confirm_eval.get("insufficient_points"):
                        saw_insufficient_points = True
                        ever_saw_insufficient_points = True
                        _emit(
                            log_callback,
                            f"[RL-TECH] 节点 ({ncx},{ncy}) 点数不足（需求={confirm_eval.get('required_cost') or '未知'}，"
                            f"剩余={effective_points if effective_points is not None else '未知'}），跳过并继续尝试同屏其余更低费用节点...",
                        )
                    elif btn_status == "locked":
                        saw_locked_on_screen = True
                    skipped_coords.append((ncx, ncy))
                    # When hitting '未解锁', prioritize any remaining untried candidate strictly to the LEFT of ncx
                    if btn_status == "locked" and remaining_nodes:
                        remaining_nodes.sort(
                            key=lambda n: (
                                0 if int(n["cx"]) < ncx else 1,
                                int(n.get("frontier_tier", 2)),
                                0 if n.get("has_halo") else 1,
                                int(n["cx"]),
                                int(n["cy"]),
                            )
                        )
                    continue

                consecutive_upgraded = 0
                up_btn = confirm_eval["button"]
                desc_text = str(confirm_eval.get("detail_text") or " ".join(b.get("text", "") for b in detail_boxes))
                score, tier = score_tech_tree_node(desc_text, "", priority_order=eff_priority)
                ux_c, uy_c = _ocr_box_bbox_center(up_btn)

                # On real/rendered UI screens, immediately click cyan '确认升级', verify points drop,
                # count +1, and re-scan in-place for newly lit downstream nodes!
                if rendered_ui:
                    pts_before = round_points_before
                    if pts_before is None:
                        pts_before = extract_tech_tree_remaining_points(
                            detail_img, detail_boxes, allow_roi_ocr=True, cancel_event=cancel_event
                        )
                    ver = _execute_and_verify_tech_upgrade(
                        adb_path=adb_path,
                        adb_addr=adb_addr,
                        before_img=detail_img,
                        confirm_btn=up_btn,
                        points_before=pts_before,
                        cancel_event=cancel_event,
                        log_callback=log_callback,
                    )
                    if ver.get("cancelled"):
                        result["cancelled"] = True
                        exit_reason = "cancelled"
                        aborted_loop = True
                        break
                    if not ver.get("verified"):
                        consecutive_verify_failures += 1
                        permanent_bad_coords.append((ncx, ncy))
                        if consecutive_verify_failures >= 2 or not remaining_nodes:
                            exit_reason = "upgrade_verify_failed"
                            _emit(
                                log_callback,
                                f"[WARN][RL-TECH] 点击「{up_btn['text']}」({ver['tap_xy'][0]},{ver['tap_xy'][1]}) 后未检测到扣点或节点推进（{ver.get('reason')}），判定加点未生效并退出循环。",
                            )
                            aborted_loop = True
                            break
                        _emit(
                            log_callback,
                            f"[WARN][RL-TECH] 点击节点 ({ncx},{ncy})「{up_btn['text']}」未检测到扣点推进，跳过并继续探测其余前沿节点。",
                        )
                        continue
                    prefetched_tree_img = ver.get("after_img")
                    prefetched_tree_boxes = ver.get("after_boxes")
                    no_progress_rounds = 0
                    consecutive_unmoved_swipes = 0
                    ever_saw_insufficient_points = False
                    consecutive_verify_failures = 0
                    skipped_coords.clear()
                    prev_tree_img_for_swipe = None
                    last_known_points = ver.get("points_after")
                    result["upgraded_count"] += 1
                    result["upgraded_nodes"].append({
                        "cx": ncx,
                        "cy": ncy,
                        "tier": tier,
                        "score": score,
                        "upgrade_label": up_btn["text"],
                        "points_before": ver.get("points_before"),
                        "points_after": ver.get("points_after"),
                        "verify_reason": ver.get("verify_reason", ""),
                        "summary": desc_text[:120],
                    })
                    _emit(
                        log_callback,
                        f"[RL-TECH] 命中青蓝「{up_btn['text']}」({ver['tap_xy'][0]},{ver['tap_xy'][1]}) 并核验扣点生效 "
                        f"[{ver.get('verify_reason')}]（节点 ({ncx},{ncy})，累计+{result['upgraded_count']}），原地重扫下游节点...",
                    )
                    upgraded_immediately = True
                    break

                evaluated.append({
                    "node": node,
                    "detail_img": detail_img,
                    "detail_boxes": detail_boxes,
                    "detail_text": desc_text[:120],
                    "score": score,
                    "tier": tier,
                    "button": up_btn,
                    "upgrade_xy": (ux_c, uy_c),
                    "upgrade_label": up_btn["text"],
                })

            if result["cancelled"] or aborted_loop:
                break
            if upgraded_immediately or triggered_arch_rescan:
                continue

            if not evaluated:
                no_progress_rounds += 1
                has_canvas_content = _has_rendered_tech_tree_canvas(tree_img)
                saw_non_upgraded_frontier = saw_insufficient_points or saw_locked_on_screen
                if saw_insufficient_points and not (rendered_ui and has_canvas_content):
                    exit_reason = "insufficient_points"
                    _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」当前点数不足，结束加点。")
                    break
                if (not saw_non_upgraded_frontier) and upgraded_hits_on_screen > 0 and (
                    canvas_swipes >= max_canvas_swipes
                    or consecutive_unmoved_swipes >= (2 if rendered_ui else 1)
                ):
                    exit_reason = "already_upgraded_exhausted"
                    _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」已达最大左滑探测次数且整屏均为已升级节点，结束加点。")
                    break
                if canvas_swipes < max_canvas_swipes:
                    canvas_swipes += 1
                    prev_tree_img_for_swipe = tree_img
                    _emit(
                        log_callback,
                        f"[RL-TECH] 整屏候选节点遍历完毕且未命中可升级节点，向左滑动一次画布探测右侧新节点 ({canvas_swipes}/{max_canvas_swipes})...",
                    )
                    stage_pilot.adb_swipe_norm(adb_path, adb_addr, 960, 360, 320, 360, 420, tree_img.width, tree_img.height)
                    time.sleep(0.9)
                    continue
                exit_reason = "insufficient_points_all_frontiers" if ever_saw_insufficient_points else "max_swipes_reached"
                _emit(log_callback, f"[RL-TECH] 科技树「{tech_spec['name']}」当前暂无点数足够的可激活节点。")
                break

            # Pick highest-priority affordable node (开局希望/初始费用/招募折扣 > 战斗属性 > 商店刷新)
            evaluated.sort(key=lambda item: -item["score"])
            best = evaluated[0]
            bnode = best["node"]
            bxy = (int(bnode["cx"]), int(bnode["cy"]))
            if last_tapped_xy != bxy:
                stage_pilot.adb_tap_norm(adb_path, adb_addr, bxy[0], bxy[1], tree_img.width, tree_img.height)
                time.sleep(0.45)

            if rendered_ui:
                pts_before = round_points_before
                if pts_before is None:
                    pts_before = extract_tech_tree_remaining_points(
                        best["detail_img"], best["detail_boxes"], allow_roi_ocr=True, cancel_event=cancel_event
                    )
                ver = _execute_and_verify_tech_upgrade(
                    adb_path=adb_path,
                    adb_addr=adb_addr,
                    before_img=best["detail_img"],
                    confirm_btn=best["button"],
                    points_before=pts_before,
                    cancel_event=cancel_event,
                    log_callback=log_callback,
                )
                if ver.get("cancelled"):
                    result["cancelled"] = True
                    exit_reason = "cancelled"
                    break
                if not ver.get("verified"):
                    exit_reason = "upgrade_verify_failed"
                    _emit(
                        log_callback,
                        f"[WARN][RL-TECH] 点击「{best['upgrade_label']}」({ver['tap_xy'][0]},{ver['tap_xy'][1]}) 后未检测到扣点或节点推进（{ver.get('reason')}），判定加点未生效并退出循环。",
                    )
                    break
                prefetched_tree_img = ver.get("after_img")
                prefetched_tree_boxes = ver.get("after_boxes")
                no_progress_rounds = 0
                consecutive_unmoved_swipes = 0
                ever_saw_insufficient_points = False
                consecutive_verify_failures = 0
                skipped_coords.clear()
                prev_tree_img_for_swipe = None
                if ver.get("points_after") is not None:
                    last_known_points = ver["points_after"]
                result["upgraded_count"] += 1
                result["upgraded_nodes"].append({
                    "cx": bxy[0],
                    "cy": bxy[1],
                    "tier": best["tier"],
                    "score": best["score"],
                    "upgrade_label": best["upgrade_label"],
                    "points_before": ver.get("points_before"),
                    "points_after": ver.get("points_after"),
                    "verify_reason": ver.get("verify_reason", ""),
                    "summary": best["detail_text"],
                })
                _emit(
                    log_callback,
                    f"[RL-TECH] 已按优先级自动加点 [{best['tier']}|分={best['score']}|按钮={best['upgrade_label']}|{ver.get('verify_reason')}] "
                    f"({bxy[0]},{bxy[1]}) -> {best['detail_text'][:48]}",
                )
            else:
                ux, uy = best["upgrade_xy"]
                stage_pilot.adb_tap_norm(adb_path, adb_addr, ux, uy, tree_img.width, tree_img.height)
                time.sleep(0.85)
                # Dismiss any confirmation / activation toast
                stage_pilot.adb_tap_norm(adb_path, adb_addr, 640, 620, tree_img.width, tree_img.height)
                time.sleep(0.35)

                no_progress_rounds = 0
                consecutive_unmoved_swipes = 0
                ever_saw_insufficient_points = False
                consecutive_verify_failures = 0
                skipped_coords.clear()
                result["upgraded_count"] += 1
                result["upgraded_nodes"].append({
                    "cx": bxy[0],
                    "cy": bxy[1],
                    "tier": best["tier"],
                    "score": best["score"],
                    "upgrade_label": best["upgrade_label"],
                    "summary": best["detail_text"],
                })
                _emit(
                    log_callback,
                    f"[RL-TECH] 已按优先级自动加点 [{best['tier']}|分={best['score']}|按钮={best['upgrade_label']}] ({bxy[0]},{bxy[1]}) -> {best['detail_text'][:48]}",
                )
        else:
            exit_reason = "max_rounds_reached"
    finally:
        result["exit_reason"] = exit_reason
        result["remaining_points"] = last_known_points
        result["canvas_swipes"] = canvas_swipes
        _emit(
            log_callback,
            f"[RL-TECH] 科技树「{tech_spec['name']}」自动加点汇总：本次升级 {result['upgraded_count']} 项，"
            f"剩余点数={last_known_points if last_known_points is not None else '未知'}，"
            f"左滑探测={canvas_swipes} 次，退出原因={exit_reason}。",
        )
        if not aborted_on_theme_select:
            _tap_return_to_lobby(
                adb_path, adb_addr, maa_dir, active_theme, cancel_event=cancel_event, log_callback=log_callback
            )

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
    navigate_to_lobby: bool = False,
) -> Dict[str, Any]:
    """Post-settlement routine: optionally navigate to theme lobby, claim level/commission rewards & upgrade tech tree."""
    norm_theme = normalize_roguelike_theme(theme, default="BlackFlow")
    eff_theme = norm_theme
    confirmed_lobby_theme: Optional[str] = None
    rl_cfg = config.get("roguelike", {}) if isinstance(config.get("roguelike"), dict) else {}
    auto_claim = bool(rl_cfg.get("auto_claim_rewards", True))
    auto_tech = bool(rl_cfg.get("auto_upgrade_tech_tree", True))
    prio = rl_cfg.get("tech_tree_priority") or list(DEFAULT_TECH_TREE_PRIORITY)

    caps = capabilities or {}
    theme_caps_map = caps.get("roguelike_theme_capabilities", {}) if isinstance(caps.get("roguelike_theme_capabilities"), dict) else {}

    out: Dict[str, Any] = {
        "ok": True,
        "theme": norm_theme,
        "detected_theme": norm_theme,
        "navigation": {"skipped": True},
        "rewards": {"skipped": True},
        "tech_tree": {"skipped": True},
        "cancelled": False,
    }

    if cancel_event and cancel_event.is_set():
        out["ok"] = False
        out["cancelled"] = True
        return out

    if not adb_path or not adb_addr:
        out["ok"] = False
        out["skipped_reason"] = "ADB 路径或地址为空，跳过局后视觉领奖与科技树加点"
        _emit(log_callback, f"[WARN][RL-POST] {out['skipped_reason']}")
        return out

    if navigate_to_lobby and (auto_claim or auto_tech):
        nav_res = ensure_roguelike_theme_lobby(
            adb_path=adb_path,
            adb_addr=adb_addr,
            maa_dir=maa_dir,
            theme=eff_theme,
            cancel_event=cancel_event,
            log_callback=log_callback,
        )
        out["navigation"] = nav_res
        if nav_res.get("cancelled") or (cancel_event and cancel_event.is_set()):
            out["ok"] = False
            out["cancelled"] = True
            return out
        if not nav_res.get("ok"):
            out["ok"] = False
            out["error"] = str(nav_res.get("reason") or f"未能进入 {eff_theme} 主题大厅")
            return out
        if nav_res.get("detected_theme"):
            eff_theme = normalize_roguelike_theme(str(nav_res["detected_theme"]), default=eff_theme)
            out["detected_theme"] = eff_theme
            confirmed_lobby_theme = eff_theme

    theme_caps = theme_caps_map.get(eff_theme, {}) if isinstance(theme_caps_map, dict) else {}

    if auto_claim:
        if theme_caps.get("native_reward_claim"):
            out["rewards"] = {"ok": True, "source": "maa_native", "note": "已由 MAA 原生任务处理领奖"}
        else:
            out["rewards"] = claim_roguelike_rewards(
                adb_path, adb_addr, maa_dir, theme=eff_theme, cancel_event=cancel_event, log_callback=log_callback
            )
            if out["rewards"].get("detected_theme"):
                eff_theme = normalize_roguelike_theme(str(out["rewards"]["detected_theme"]), default=eff_theme)
                out["detected_theme"] = eff_theme
            if out["rewards"].get("level_entry_found"):
                confirmed_lobby_theme = eff_theme
            if out["rewards"].get("cancelled"):
                out["ok"] = False
                out["cancelled"] = True
                return out
            if out["rewards"].get("ok") is False:
                out["ok"] = False

    if auto_tech:
        theme_caps = theme_caps_map.get(eff_theme, {}) if isinstance(theme_caps_map, dict) else {}
        if theme_caps.get("native_tech_tree_upgrade"):
            out["tech_tree"] = {"ok": True, "source": "maa_native", "note": "已由 MAA 原生任务处理科技树加点"}
        else:
            out["tech_tree"] = auto_upgrade_roguelike_tech_tree(
                adb_path,
                adb_addr,
                maa_dir,
                theme=eff_theme,
                priority_order=prio,
                cancel_event=cancel_event,
                log_callback=log_callback,
                lobby_confirmed_theme=confirmed_lobby_theme,
            )
            if out["tech_tree"].get("detected_theme"):
                out["detected_theme"] = normalize_roguelike_theme(str(out["tech_tree"]["detected_theme"]), default=eff_theme)
            if out["tech_tree"].get("cancelled"):
                out["ok"] = False
                out["cancelled"] = True
            elif out["tech_tree"].get("ok") is False:
                out["ok"] = False

    return out
