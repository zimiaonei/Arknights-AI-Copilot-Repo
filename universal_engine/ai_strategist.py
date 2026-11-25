"""OpenAI-compatible AI strategist with seamless zero-key pure-MAA fallback."""

from __future__ import annotations

import base64
import copy
import json
import mimetypes
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union

ROLE_SUBSTITUTES: Dict[str, List[Tuple[str, int, int]]] = {
    # Blocking melee vanguards (2-block pioneer / 1-block agent & charger / counsellor) — NEVER 0-block flagbearers!
    "vanguard_block": [
        ("嵯峨", 2, 1),
        ("忍冬", 3, 1),
        ("推进之王", 2, 1),
        ("焰尾", 3, 1),
        ("伊内丝", 2, 1),
        ("晓歌", 2, 1),
        ("风笛", 3, 1),
        ("德克萨斯", 2, 1),
        ("凛冬", 2, 1),
        ("历阵锐枪芬", 2, 1),
        ("格拉尼", 2, 1),
        ("讯使", 2, 1),
        ("清道夫", 2, 1),
        ("芬", 1, 0),
    ],
    # Backward-compatible alias pointing strictly to blocking vanguards (never bearer)
    "vanguard_dp": [
        ("嵯峨", 2, 1),
        ("忍冬", 3, 1),
        ("推进之王", 2, 1),
        ("焰尾", 3, 1),
        ("伊内丝", 2, 1),
        ("晓歌", 2, 1),
        ("风笛", 3, 1),
        ("德克萨斯", 2, 1),
        ("凛冬", 2, 1),
        ("历阵锐枪芬", 2, 1),
        ("格拉尼", 2, 1),
        ("讯使", 2, 1),
        ("清道夫", 2, 1),
        ("芬", 1, 0),
    ],
    # 0-block Flagbearers (执旗手) — can only substitute other flagbearers, NEVER blocking vanguards
    "vanguard_bearer": [("桃金娘", 1, 1), ("极境", 1, 1), ("琴柳", 1, 1), ("万顷", 1, 1), ("嘉辛塔", 1, 1)],
    # Ranged Tacticians (战术家, high-ground only)
    "vanguard_tactician": [("缪尔赛思", 3, 1), ("伺夜", 3, 1), ("可露希尔", 3, 1), ("夜半", 2, 1), ("豆苗", 2, 1)],
    "guard_lane": [("棘刺", 3, 1), ("山", 2, 1), ("煌", 2, 1), ("赤刃明霄陈", 3, 1), ("锏", 3, 1), ("赫拉格", 2, 1), ("玛恩纳", 3, 1)],
    "guard_burst": [("玛恩纳", 3, 1), ("史尔特尔", 3, 1), ("银灰", 3, 1), ("赤刃明霄陈", 3, 1), ("锏", 3, 1), ("棘刺", 3, 1), ("山", 2, 1)],
    "defender": [("塞雷娅", 1, 1), ("斥罪", 3, 1), ("星熊", 2, 0), ("泥岩", 2, 1), ("号角", 3, 1), ("蛇屠箱", 2, 0)],
    "sniper": [("维什戴尔", 3, 1), ("蕾缪安", 3, 1), ("提丰", 3, 1), ("新约能天使", 3, 1), ("能天使", 3, 1), ("W", 2, 1), ("寒芒克洛丝", 2, 1)],
    "caster": [("艾雅法拉", 2, 1), ("澄闪", 3, 1), ("逻各斯", 3, 1), ("妮芙", 2, 1), ("夕", 3, 1), ("玛露西尔", 3, 1)],
    "medic": [("纯烬艾雅法拉", 1, 0), ("夜莺", 2, 0), ("闪灵", 2, 0), ("白面鸮", 2, 1), ("流明", 3, 1), ("凯尔希·思衡托", 2, 1), ("蜜莓", 1, 0)],
    "specialist": [("缄默德克萨斯", 3, 1), ("麒麟R夜刀", 2, 1), ("酒神", 3, 1), ("余", 3, 1), ("望", 3, 1), ("红", 2, 1), ("砾", 2, 0)],
    "supporter": [("浊心斯卡蒂", 2, 0), ("铃兰", 3, 1), ("魔王", 2, 1)],
}

OPERATOR_DEFAULT_ROLE: Dict[str, str] = {
    # Flagbearers (0-block during skill)
    "桃金娘": "vanguard_bearer", "极境": "vanguard_bearer", "琴柳": "vanguard_bearer",
    "万顷": "vanguard_bearer", "嘉辛塔": "vanguard_bearer",
    # Blocking melee vanguards (pioneer / charger / agent / counsellor)
    "德克萨斯": "vanguard_block", "推进之王": "vanguard_block", "嵯峨": "vanguard_block",
    "忍冬": "vanguard_block", "焰尾": "vanguard_block", "凛冬": "vanguard_block",
    "芬": "vanguard_block", "清道夫": "vanguard_block", "讯使": "vanguard_block",
    "香草": "vanguard_block", "贾维": "vanguard_block", "红隼": "vanguard_block",
    "伊内丝": "vanguard_block", "晓歌": "vanguard_block", "齐尔查克": "vanguard_block",
    "谜图": "vanguard_block", "寻澜": "vanguard_block", "冬时": "vanguard_block",
    "风笛": "vanguard_block", "历阵锐枪芬": "vanguard_block", "格拉尼": "vanguard_block",
    "翎羽": "vanguard_block", "苇草": "vanguard_block", "红豆": "vanguard_block", "野鬃": "vanguard_block",
    "凛御银灰": "vanguard_block", "松桐": "vanguard_block",
    # Ranged Tactician vanguards
    "缪尔赛思": "vanguard_tactician", "伺夜": "vanguard_tactician", "可露希尔": "vanguard_tactician",
    "夜半": "vanguard_tactician", "豆苗": "vanguard_tactician", "渡桥": "vanguard_tactician",
    # Guards
    "山": "guard_lane", "棘刺": "guard_lane", "煌": "guard_lane", "百炼嘉维尔": "guard_lane",
    "乌尔比安": "guard_lane", "圣约送葬人": "guard_lane", "玛恩纳": "guard_burst", "史尔特尔": "guard_burst",
    "银灰": "guard_burst", "赤刃明霄陈": "guard_burst", "锏": "guard_burst", "仇白": "guard_burst",
    "赫拉格": "guard_lane", "帕拉斯": "guard_lane", "拉普兰德": "guard_lane",
    # Defenders
    "塞雷娅": "defender", "黍": "defender", "泥岩": "defender", "星熊": "defender",
    "斥罪": "defender", "年": "defender", "号角": "defender", "古米": "defender", "临光": "defender", "蛇屠箱": "defender",
    # Snipers
    "维什戴尔": "sniper", "假日威龙陈": "sniper", "提丰": "sniper", "鸿雪": "sniper", "焰狐龙梓兰": "sniper",
    "蕾缪安": "sniper", "新约能天使": "sniper", "能天使": "sniper", "W": "sniper", "克洛丝": "sniper", "寒芒克洛丝": "sniper",
    # Casters
    "艾雅法拉": "caster", "澄闪": "caster", "逻各斯": "caster", "伊芙利特": "caster",
    "莫斯提马": "caster", "妮芙": "caster", "夕": "caster", "玛露西尔": "caster", "阿米娅": "caster",
    # Medics
    "纯烬艾雅法拉": "medic", "流明": "medic", "夜莺": "medic", "白面鸮": "medic",
    "凯尔希": "medic", "凯尔希·思衡托": "medic", "闪灵": "medic", "蜜莓": "medic", "华法琳": "medic",
    # Specialists
    "缄默德克萨斯": "specialist", "麒麟R夜刀": "specialist", "阿斯卡纶": "specialist", "伊桑": "specialist",
    "狮蝎": "specialist", "水月": "specialist", "歌蕾蒂娅": "specialist", "酒神": "specialist",
    "余": "specialist", "望": "specialist", "红": "specialist", "砾": "specialist",
    # Supporters
    "浊心斯卡蒂": "supporter", "铃兰": "supporter", "魔王": "supporter", "灵知": "supporter",
}

# Offline metadata fallback (profession, subProfessionId, position) mirroring <maa_dir>/resource/battle_data.json
BUILTIN_OPERATOR_META: Dict[str, Dict[str, str]] = {
    # Pioneer: 2-block pioneer (尖兵)
    **{
        n: {"profession": "PIONEER", "sub_profession": "pioneer", "position": "MELEE"}
        for n in ("德克萨斯", "推进之王", "嵯峨", "忍冬", "焰尾", "凛冬", "芬", "清道夫", "讯使", "香草", "贾维", "红隼")
    },
    # Pioneer: 1-block charger (冲锋手)
    **{
        n: {"profession": "PIONEER", "sub_profession": "charger", "position": "MELEE"}
        for n in ("风笛", "历阵锐枪芬", "格拉尼", "翎羽", "苇草", "红豆", "野鬃")
    },
    # Pioneer: 1-block agent (情报官)
    **{
        n: {"profession": "PIONEER", "sub_profession": "agent", "position": "MELEE"}
        for n in ("伊内丝", "晓歌", "齐尔查克", "谜图", "寻澜", "冬时")
    },
    # Pioneer: 2-block counsellor (策士)
    **{
        n: {"profession": "PIONEER", "sub_profession": "counsellor", "position": "MELEE"}
        for n in ("凛御银灰", "松桐")
    },
    # Pioneer: 0-block bearer (执旗手) — STRICTLY forbidden from replacing blocking vanguards!
    **{
        n: {"profession": "PIONEER", "sub_profession": "bearer", "position": "MELEE"}
        for n in ("桃金娘", "极境", "琴柳", "万顷", "嘉辛塔")
    },
    # Pioneer: RANGED tactician (战术家)
    **{
        n: {"profession": "PIONEER", "sub_profession": "tactician", "position": "RANGED"}
        for n in ("缪尔赛思", "伺夜", "可露希尔", "夜半", "豆苗", "渡桥")
    },
    # Core melee / ranged roles for offline position guards
    **{
        n: {"profession": "WARRIOR", "sub_profession": "lord", "position": "MELEE"}
        for n in ("棘刺", "银灰", "拉普兰德")
    },
    **{
        n: {"profession": "WARRIOR", "sub_profession": "fighter", "position": "MELEE"}
        for n in ("山",)
    },
    **{
        n: {"profession": "WARRIOR", "sub_profession": "centurion", "position": "MELEE"}
        for n in ("煌", "百炼嘉维尔")
    },
    **{
        n: {"profession": "WARRIOR", "sub_profession": "artsfghter", "position": "MELEE"}
        for n in ("史尔特尔", "赤刃明霄陈")
    },
    **{
        n: {"profession": "WARRIOR", "sub_profession": "librator", "position": "MELEE"}
        for n in ("玛恩纳",)
    },
    **{
        n: {"profession": "WARRIOR", "sub_profession": "sword", "position": "MELEE"}
        for n in ("锏",)
    },
    **{
        n: {"profession": "TANK", "sub_profession": "guardian", "position": "MELEE"}
        for n in ("塞雷娅", "黍", "古米", "临光")
    },
    **{
        n: {"profession": "TANK", "sub_profession": "protector", "position": "MELEE"}
        for n in ("星熊", "年", "蛇屠箱")
    },
    **{
        n: {"profession": "TANK", "sub_profession": "unyield", "position": "MELEE"}
        for n in ("泥岩", "斥罪")
    },
    **{
        n: {"profession": "TANK", "sub_profession": "fortress", "position": "MELEE"}
        for n in ("号角",)
    },
    **{
        n: {"profession": "SNIPER", "sub_profession": "bombarder", "position": "RANGED"}
        for n in ("维什戴尔",)
    },
    **{
        n: {"profession": "SNIPER", "sub_profession": "fastshot", "position": "RANGED"}
        for n in ("能天使", "克洛丝", "寒芒克洛丝")
    },
    **{
        n: {"profession": "CASTER", "sub_profession": "corecaster", "position": "RANGED"}
        for n in ("艾雅法拉", "阿米娅")
    },
    **{
        n: {"profession": "MEDIC", "sub_profession": "physician", "position": "RANGED"}
        for n in ("闪灵", "凯尔希", "华法琳")
    },
    **{
        n: {"profession": "MEDIC", "sub_profession": "ringhealer", "position": "RANGED"}
        for n in ("夜莺", "白面鸮")
    },
    **{
        n: {"profession": "MEDIC", "sub_profession": "wandermedic", "position": "RANGED"}
        for n in ("纯烬艾雅法拉", "蜜莓")
    },
    **{
        n: {"profession": "SPECIAL", "sub_profession": "executor", "position": "MELEE"}
        for n in ("缄默德克萨斯", "麒麟R夜刀", "红", "砾")
    },
}

_BLOCKING_VANGUARD_SUBS = frozenset({"pioneer", "charger", "agent", "counsellor"})
_BEARER_VANGUARD_SUBS = frozenset({"bearer"})
_TACTICIAN_VANGUARD_SUBS = frozenset({"tactician"})
_BATTLE_DATA_META_CACHE: Dict[str, Dict[str, Dict[str, str]]] = {}

PROF_TAG_TO_ROLE: Dict[str, str] = {
    "先锋干员": "vanguard_block",
    "近卫干员": "guard_lane",
    "重装干员": "defender",
    "狙击干员": "sniper",
    "术师干员": "caster",
    "医疗干员": "medic",
    "特种干员": "specialist",
    "辅助干员": "supporter",
}

DEFAULT_SKILL_MAP: Dict[str, Tuple[int, int]] = {
    op: (skill, usage)
    for ops in ROLE_SUBSTITUTES.values()
    for op, skill, usage in ops
}

_SUMMON_OWNER_MAP: Dict[str, str] = {
    "魂灵之影": "维什戴尔",
}
_SUMMON_TOKENS = {"魂灵之影", "触手", "机械水獭", "Mon3tr", "铁卫", "小车"}


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence:
        raw = fence.group(1).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _safe_int(val: Any) -> Optional[int]:
    if isinstance(val, bool) or val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


class OpenAICompatClient:
    """Zero-dependency OpenAI-compatible HTTP client (Gemini / DeepSeek / local Ollama)."""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model_name: str = "gpt-4o-mini",
        timeout_sec: int = 30,
    ):
        self.base_url = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model_name = (model_name or "gpt-4o-mini").strip()
        self.timeout_sec = max(5, int(timeout_sec or 30))

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "OpenAICompatClient":
        ai_cfg = (config or {}).get("ai", {})
        if not isinstance(ai_cfg, dict):
            ai_cfg = {}
        mode = str(ai_cfg.get("mode", "") or "").strip().lower()
        enabled = bool(ai_cfg.get("enabled", False)) and (mode != "agent_collab")
        raw_key = str(ai_cfg.get("api_key") or "") if enabled else ""
        timeout_val = _safe_int(ai_cfg.get("timeout_sec")) or 30
        return cls(
            base_url=str(ai_cfg.get("base_url") or "https://api.openai.com/v1"),
            api_key=raw_key,
            model_name=str(ai_cfg.get("model_name") or "gpt-4o-mini"),
            timeout_sec=timeout_val,
        )

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def endpoint_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if re.search(r"/v\d+(?:beta)?(?:/openai)?$", self.base_url, re.IGNORECASE):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> Optional[Dict[str, Any]]:
        if not self.is_configured():
            return None

        payload = {
            "model": self.model_name,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        req = urllib.request.Request(
            self.endpoint_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MAA-Universal-Engine/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if not isinstance(content, str):
                return None
            return _extract_json_object(content)
        except Exception:
            return None

    def chat_vision_json(
        self,
        system_prompt: str,
        user_prompt: str,
        image_path: Optional[Path] = None,
        image_bytes: Optional[bytes] = None,
        temperature: float = 0.2,
        fallback_text_only: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Send OpenAI-compatible multimodal image+text prompt returning parsed JSON, with text fallback."""
        if not self.is_configured():
            return None

        raw_bytes = image_bytes
        mime_type = "image/png"
        if raw_bytes is None and image_path is not None:
            p = Path(image_path)
            if p.is_file():
                try:
                    raw_bytes = p.read_bytes()
                    guessed, _ = mimetypes.guess_type(str(p))
                    if guessed and guessed.startswith("image/"):
                        mime_type = guessed
                except Exception:
                    raw_bytes = None

        if not raw_bytes:
            return self.chat_json(system_prompt, user_prompt, temperature=temperature)

        b64 = base64.b64encode(raw_bytes).decode("ascii")
        payload = {
            "model": self.model_name,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                        },
                    ],
                },
            ],
        }
        req = urllib.request.Request(
            self.endpoint_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MAA-Universal-Engine/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if isinstance(content, str):
                parsed = _extract_json_object(content)
                if parsed is not None:
                    return parsed
        except Exception:
            pass

        if fallback_text_only:
            return self.chat_json(system_prompt, user_prompt, temperature=temperature)
        return None

    def test_connection(self) -> Tuple[bool, str]:
        """Lightweight connectivity probe for GUI Settings / Onboarding validation."""
        if not self.api_key:
            return False, "请先填写 API Key（本地 Ollama 可填写 ollama）"
        payload = {
            "model": self.model_name,
            "temperature": 0.0,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with OK"}],
        }
        req = urllib.request.Request(
            self.endpoint_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "MAA-Universal-Engine/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=min(self.timeout_sec, 15)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
            return True, f"连接成功 ({self.model_name}): {content[:60] or 'OK'}"
        except Exception as exc:
            return False, f"连接失败: {exc}"


def load_operator_metadata(maa_dir: Optional[Path] = None) -> Dict[str, Dict[str, str]]:
    """Load operator profession/sub_profession/position metadata from battle_data.json + built-in fallback."""
    cache_key = str(Path(maa_dir).resolve()) if maa_dir is not None else "__builtin__"
    cached = _BATTLE_DATA_META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    meta: Dict[str, Dict[str, str]] = {k: dict(v) for k, v in BUILTIN_OPERATOR_META.items()}
    if maa_dir is not None:
        bd_path = Path(maa_dir) / "resource" / "battle_data.json"
        if bd_path.is_file():
            try:
                raw = json.loads(bd_path.read_text(encoding="utf-8"))
                chars = raw.get("chars", {}) if isinstance(raw, dict) else {}
                for cid, info in chars.items():
                    if not isinstance(info, dict) or not str(cid).startswith("char_"):
                        continue
                    name = str(info.get("name") or "").strip()
                    if not name:
                        continue
                    prof = str(info.get("profession") or "").strip().upper()
                    sub = str(info.get("subProfessionId") or "").strip().lower()
                    pos = str(info.get("position") or "").strip().upper()
                    if prof or sub or pos:
                        entry: Dict[str, Any] = {
                            "profession": prof,
                            "sub_profession": sub,
                            "position": pos,
                        }
                        if info.get("rarity") is not None:
                            entry["rarity"] = _safe_int(info.get("rarity"))
                        meta[name] = entry
            except Exception:
                pass

    _BATTLE_DATA_META_CACHE[cache_key] = meta
    return meta


def _role_from_meta(info: Dict[str, str]) -> Optional[str]:
    prof = str(info.get("profession") or "").upper()
    sub = str(info.get("sub_profession") or "").lower()
    if prof == "PIONEER":
        if sub in _BEARER_VANGUARD_SUBS:
            return "vanguard_bearer"
        if sub in _TACTICIAN_VANGUARD_SUBS:
            return "vanguard_tactician"
        return "vanguard_block"
    prof_map = {
        "WARRIOR": "guard_lane",
        "TANK": "defender",
        "SNIPER": "sniper",
        "CASTER": "caster",
        "MEDIC": "medic",
        "SPECIAL": "specialist",
        "SUPPORT": "supporter",
    }
    return prof_map.get(prof)


def load_recruitment_roles(maa_dir: Optional[Path] = None) -> Dict[str, str]:
    """Load operator role map supplemented from battle_data.json and recruitment.json."""
    roles = dict(OPERATOR_DEFAULT_ROLE)
    op_meta = load_operator_metadata(maa_dir)
    for name, info in op_meta.items():
        derived = _role_from_meta(info)
        if derived:
            # Always keep sub-profession accurate vanguard buckets (bearer vs block vs tactician)
            if name not in roles or derived in ("vanguard_block", "vanguard_bearer", "vanguard_tactician"):
                roles[name] = derived

    if maa_dir is None:
        return roles
    rec_path = Path(maa_dir) / "resource" / "recruitment.json"
    if not rec_path.is_file():
        return roles
    try:
        data = json.loads(rec_path.read_text(encoding="utf-8"))
        for op in data.get("operators", []):
            name = op.get("name", "")
            if not name or name in roles:
                continue
            for tag in op.get("tags", []):
                if tag in PROF_TAG_TO_ROLE:
                    roles[name] = PROF_TAG_TO_ROLE[tag]
                    break
    except Exception:
        pass
    return roles


def _is_compatible_substitute(
    orig_name: str,
    cand_name: str,
    op_meta: Dict[str, Dict[str, str]],
    role_map: Dict[str, str],
    fallback_role: str = "guard_lane",
) -> Tuple[bool, int]:
    """Return (allowed, affinity_tier 0..3) enforcing position & sub-profession/block compatibility.

    Hard rules:
    - Never swap MELEE <-> RANGED (invalid tile placement in MAA Copilot).
    - Never substitute a blocking vanguard (pioneer/charger/agent/counsellor, e.g. 德克萨斯) with a
      0-block flagbearer (bearer, e.g. 桃金娘/极境/琴柳) or ranged tactician (e.g. 缪尔赛思/伺夜).
    - Prefer exact sub_profession match (tier 3) > compatible same-branch/block role (tier 2) > same broad role (tier 1).
    """
    o_info = op_meta.get(orig_name, {})
    c_info = op_meta.get(cand_name, {})
    o_prof = str(o_info.get("profession") or "").upper()
    c_prof = str(c_info.get("profession") or "").upper()
    o_sub = str(o_info.get("sub_profession") or "").lower()
    c_sub = str(c_info.get("sub_profession") or "").lower()
    o_pos = str(o_info.get("position") or "").upper()
    c_pos = str(c_info.get("position") or "").upper()

    o_role = role_map.get(orig_name, fallback_role)
    c_role = role_map.get(cand_name, "")

    # Hard position check (MELEE vs RANGED)
    if o_pos and c_pos and o_pos != c_pos:
        return False, 0

    # Strict Vanguard branch/block guards
    is_orig_vanguard = o_prof == "PIONEER" or o_role in ("vanguard_block", "vanguard_dp", "vanguard_bearer", "vanguard_tactician")
    if is_orig_vanguard:
        is_cand_vanguard = c_prof == "PIONEER" or c_role in ("vanguard_block", "vanguard_dp", "vanguard_bearer", "vanguard_tactician")
        if not is_cand_vanguard:
            return False, 0

        orig_is_bearer = o_sub in _BEARER_VANGUARD_SUBS or o_role == "vanguard_bearer"
        orig_is_tactician = o_sub in _TACTICIAN_VANGUARD_SUBS or o_role == "vanguard_tactician"
        cand_is_bearer = c_sub in _BEARER_VANGUARD_SUBS or c_role == "vanguard_bearer"
        cand_is_tactician = c_sub in _TACTICIAN_VANGUARD_SUBS or c_role == "vanguard_tactician"

        if orig_is_tactician:
            return (True, 3) if cand_is_tactician else (False, 0)

        if orig_is_bearer:
            if cand_is_tactician:
                return False, 0
            if cand_is_bearer:
                return True, 3
            # A melee blocking vanguard can stand on a bearer tile if no other bearer exists
            return True, 2

        # Orig is a blocking melee vanguard (pioneer / charger / agent / counsellor):
        # STRICTLY FORBID 0-block flagbearers (桃金娘/极境/琴柳) and ranged tacticians!
        if cand_is_bearer or cand_is_tactician:
            return False, 0
        if o_sub and c_sub and o_sub == c_sub:
            return True, 3
        if o_sub == "pioneer" and c_sub == "counsellor":
            return True, 3
        if c_sub in _BLOCKING_VANGUARD_SUBS or c_role in ("vanguard_block", "vanguard_dp"):
            return True, 2
        return False, 0

    # Non-vanguard roles: match same profession/role with sub_profession affinity bonus
    if o_prof and c_prof and o_prof != c_prof:
        return False, 0
    if o_role != c_role:
        # Allow guard_lane <-> guard_burst only when both are MELEE WARRIOR
        if not ({o_role, c_role} == {"guard_lane", "guard_burst"}):
            return False, 0
    if o_sub and c_sub and o_sub == c_sub:
        return True, 3
    if o_role == c_role:
        return True, 2
    return True, 1


def _max_skill_level_for_elite(elite: int) -> int:
    if elite <= 0:
        return 4
    if elite == 1:
        return 7
    return 10


def _min_elite_for_skill(
    skill: Any,
    rarity: Optional[int] = None,
    skill_usage: Any = None,
) -> int:
    try:
        s = int(skill)
    except Exception:
        s = 1
    if s >= 3:
        return 2
    if s == 2:
        return 1
    if rarity == 3 and skill_usage is not None:
        usg = _safe_int(skill_usage)
        if usg is not None and usg != 0:
            return 1
    return 0


def _clamp_requirements(req: Optional[Dict[str, Any]], owned_op: Dict[str, Any], skill: Any = 1) -> Dict[str, Any]:
    out = dict(req or {})
    owned_elite = max(0, min(2, int(owned_op.get("elite", 0) or 0)))
    owned_level = max(1, int(owned_op.get("level", 1) or 1))
    min_elite = _min_elite_for_skill(skill)
    raw_req_elite = _safe_int(out.get("elite", owned_elite))
    req_elite = owned_elite if raw_req_elite is None else max(0, raw_req_elite)
    target_elite = max(min_elite, min(req_elite, owned_elite))
    out["elite"] = target_elite
    out["level"] = max(1, min(int(out.get("level", 1) or 1), owned_level)) if target_elite == owned_elite else 1
    max_sl = min(7, _max_skill_level_for_elite(target_elite))
    if target_elite == 0 and owned_level <= 10:
        out["skill_level"] = 1
    else:
        raw_sl = _safe_int(out.get("skill_level", max_sl))
        out["skill_level"] = max(1, min(raw_sl if raw_sl is not None else max_sl, max_sl))
    out["module"] = -1
    out["potentiality"] = 1
    return out


def _clean_actions_with_sub_map(
    actions: List[Dict[str, Any]],
    sub_map: Dict[str, str],
    active_opers: List[Dict[str, Any]],
    sub_op_specs: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Rewrite actions consistently after operator/group substitution (syncing name, skill, skill_usage)."""
    active_names = {str(o.get("name") or "").strip() for o in active_opers if isinstance(o, dict)}
    specs = sub_op_specs or {}
    cleaned: List[Dict[str, Any]] = []
    for act in actions:
        if not isinstance(act, dict):
            cleaned.append(act)
            continue
        act_copy = copy.deepcopy(act)
        orig_name = str(act_copy.get("name") or "").strip()
        if orig_name in sub_map:
            act_copy["name"] = sub_map[orig_name]
        orig_target = act_copy.get("target")
        if isinstance(orig_target, str) and orig_target.strip() in sub_map:
            act_copy["target"] = sub_map[orig_target.strip()]

        # Synchronize skill / skill_usage metadata on actions targeting a substituted operator/group
        spec = specs.get(orig_name) or specs.get(str(act_copy.get("name") or "").strip())
        if spec:
            act_type = str(act_copy.get("type") or "")
            new_skill = spec.get("skill")
            new_usage = spec.get("skill_usage")
            if "skill" in act_copy and new_skill is not None:
                act_copy["skill"] = int(new_skill)
            if act_type == "SkillUsage" and new_usage is not None:
                act_copy["skill_usage"] = int(new_usage)
                if int(new_usage) != 2 and "skill_times" in act_copy:
                    act_copy.pop("skill_times", None)
            elif "skill_usage" in act_copy and new_usage is not None:
                act_copy["skill_usage"] = int(new_usage)
            # If substitute uses passive/auto skill (skill_usage == 0), drop manual Skill activation
            # actions so MAA Copilot never stalls forever waiting for a manual skill-ready icon!
            if act_type == "Skill" and new_usage == 0:
                continue

        if orig_name in _SUMMON_TOKENS and act_copy.get("type") == "Retreat":
            owner = _SUMMON_OWNER_MAP.get(orig_name)
            if owner and owner not in active_names:
                continue
        cleaned.append(act_copy)
    return cleaned


def _safe_substitute_requirements(owned_op: Dict[str, Any], skill: Any = 1) -> Dict[str, Any]:
    """Conservative requirements for auto-substituted operators (avoids MAA rejecting unknown skill_level)."""
    owned_elite = int(owned_op.get("elite", 0))
    min_elite = _min_elite_for_skill(skill)
    target_elite = max(min_elite, min(owned_elite, 2))
    return {
        "elite": target_elite,
        "level": 1,
        "skill_level": 1,
        "module": -1,
        "potentiality": 1,
    }


def _iter_copilot_oper_entries(copilot: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if not isinstance(copilot, dict):
        return
    for op in copilot.get("opers") or []:
        if isinstance(op, dict):
            yield op
    for grp in copilot.get("groups") or []:
        if isinstance(grp, dict):
            for op in grp.get("opers") or []:
                if isinstance(op, dict):
                    yield op


def sanitize_copilot_requirements(
    copilot: Dict[str, Any],
    maa_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Ensure every operator's requirements satisfy MAA CopilotConfig::parse_oper_usage invariants."""
    clean = copy.deepcopy(copilot) if isinstance(copilot, dict) else {}
    op_meta = load_operator_metadata(maa_dir)

    for op in _iter_copilot_oper_entries(clean):
        name = str(op.get("name") or "").strip()
        rarity = _safe_int((op_meta.get(name) or {}).get("rarity"))
        skill = _safe_int(op.get("skill", 1))
        if skill is None or skill not in (1, 2, 3):
            skill = 1
        if rarity == 3 and skill > 1:
            op["skill"] = 1
            skill = 1
        if rarity is not None and rarity <= 2:
            if "skill" in op:
                op["skill"] = 1
            skill = 1
        req = op.get("requirements")
        if isinstance(req, dict):
            req_clean = dict(req)
            raw_elite = _safe_int(req_clean.get("elite", 0))
            elite = max(0, min(2, raw_elite if raw_elite is not None else 0))
            min_e = _min_elite_for_skill(skill, rarity=rarity, skill_usage=op.get("skill_usage"))
            if elite < min_e:
                elite = min(min_e, 1 if rarity == 3 else 2)
            req_clean["elite"] = elite

            if req_clean.get("module") is not None:
                mod = _safe_int(req_clean.get("module"))
                if mod is not None and mod >= 0 and elite < 2:
                    req_clean["module"] = -1

            if req_clean.get("skill_level") is not None:
                max_sl = _max_skill_level_for_elite(elite)
                sl = _safe_int(req_clean.get("skill_level"))
                req_clean["skill_level"] = max(1, min(sl if sl is not None else 1, max_sl))

            if req_clean.get("level") is not None:
                lv = _safe_int(req_clean.get("level"))
                max_lv = {0: 50, 1: 80, 2: 90}.get(elite, 50)
                req_clean["level"] = max(1, min(lv if lv is not None else 1, max_lv))

            if req_clean.get("potentiality") is not None:
                pot = _safe_int(req_clean.get("potentiality"))
                req_clean["potentiality"] = max(1, min(pot if pot is not None else 1, 6))

            op["requirements"] = req_clean
    return clean


def validate_copilot_payload(
    copilot: Any,
    maa_dir: Optional[Path] = None,
) -> Tuple[bool, List[str]]:
    """Validate that an in-memory copilot dict conforms to MAA Copilot JSON schema and oper invariants."""
    errors: List[str] = []
    if not isinstance(copilot, dict):
        return False, ["顶层结构必须为 JSON 对象"]

    stage_name = copilot.get("stage_name")
    if not isinstance(stage_name, str) or not stage_name.strip():
        errors.append("缺少有效的 stage_name")

    has_opers_key = "opers" in copilot
    has_groups_key = "groups" in copilot
    if not has_opers_key and not has_groups_key:
        errors.append("缺少 opers / groups 字段")
    if has_opers_key and not isinstance(copilot.get("opers"), list):
        errors.append("opers 必须为数组")
    if has_groups_key and not isinstance(copilot.get("groups"), list):
        errors.append("groups 必须为数组")

    actions = copilot.get("actions")
    if "actions" not in copilot or not isinstance(actions, list):
        errors.append("缺少 actions 数组")
    else:
        for idx, act in enumerate(actions):
            if not isinstance(act, dict) or not str(act.get("type") or "").strip():
                errors.append(f"actions[{idx}] 缺少有效的 type 字段")
                break

    op_meta = load_operator_metadata(maa_dir)
    oper_entries: List[Dict[str, Any]] = []
    if isinstance(copilot.get("opers"), list):
        for idx, op in enumerate(copilot["opers"]):
            if not isinstance(op, dict) or not str(op.get("name") or "").strip():
                errors.append(f"opers[{idx}] 缺少有效的 name 字段")
            else:
                oper_entries.append(op)
    if isinstance(copilot.get("groups"), list):
        for g_idx, grp in enumerate(copilot["groups"]):
            if not isinstance(grp, dict):
                errors.append(f"groups[{g_idx}] 必须为对象")
                continue
            g_ops = grp.get("opers")
            if g_ops is not None and not isinstance(g_ops, list):
                errors.append(f"groups[{g_idx}].opers 必须为数组")
                continue
            for o_idx, op in enumerate(g_ops or []):
                if not isinstance(op, dict) or not str(op.get("name") or "").strip():
                    errors.append(f"groups[{g_idx}].opers[{o_idx}] 缺少有效的 name 字段")
                else:
                    oper_entries.append(op)

    if not oper_entries:
        errors.append("opers 与 groups 中至少需包含 1 名干员")

    for op in oper_entries:
        name = str(op.get("name") or "").strip()
        rarity = _safe_int((op_meta.get(name) or {}).get("rarity"))
        skill = _safe_int(op.get("skill", 1))
        if skill is None:
            skill = 1
        req = op.get("requirements")
        if req is None:
            continue
        if not isinstance(req, dict):
            errors.append(f"干员 {name} 的 requirements 必须为对象")
            continue
        elite = _safe_int(req.get("elite", 0))
        if elite is None or elite < 0 or elite > 2:
            errors.append(f"干员 {name} 的 elite={req.get('elite')!r} 非法")
            continue
        min_e = _min_elite_for_skill(skill, rarity=rarity, skill_usage=op.get("skill_usage"))
        if elite < min_e:
            errors.append(f"干员 {name} 技能/星级要求 elite>={min_e}，但当前 elite={elite}")
        if req.get("skill_level") is not None:
            sl = _safe_int(req.get("skill_level"))
            max_sl = _max_skill_level_for_elite(elite)
            if sl is None or sl < 1 or sl > max_sl:
                errors.append(f"干员 {name} 在 elite={elite} 下 skill_level={req.get('skill_level')!r} 超出 1..{max_sl}")
        if req.get("module") is not None:
            mod = _safe_int(req.get("module"))
            if mod is not None and mod >= 0 and elite < 2:
                errors.append(f"干员 {name} 在 elite={elite} 下启用了模组 module={mod}")

    return (len(errors) == 0), errors


def validate_copilot_file(
    path: Union[str, Path],
    maa_dir: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Strictly verify a Copilot JSON file on disk (exists, non-empty, UTF-8 without BOM, valid MAA schema)."""
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception as exc:
        return False, f"作业路径非法 ({path}): {exc}", None

    if not resolved.is_file():
        return False, f"作业文件不存在: {resolved}", None
    try:
        raw = resolved.read_bytes()
    except Exception as exc:
        return False, f"作业文件读取失败 ({resolved}): {exc}", None
    if not raw or not raw.strip():
        return False, f"作业文件为空: {resolved}", None
    if raw.startswith(b"\xef\xbb\xbf"):
        return False, f"作业文件包含 UTF-8 BOM 头: {resolved}", None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, f"作业文件非合法 UTF-8 编码 ({resolved}): {exc}", None
    try:
        data = json.loads(text)
    except Exception as exc:
        return False, f"作业文件 JSON 语法错误 ({resolved}): {exc}", None

    ok, errs = validate_copilot_payload(data, maa_dir=maa_dir)
    if not ok:
        return False, f"作业结构不符合 MAA 规范: {'; '.join(errs)}", None
    return True, "", data


def write_validated_copilot_file(
    path: Union[str, Path],
    copilot: Dict[str, Any],
    maa_dir: Optional[Path] = None,
) -> Tuple[Path, Dict[str, Any]]:
    """Sanitize, validate, atomically write BOM-free UTF-8 JSON, and re-verify on disk."""
    resolved = Path(path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    clean = sanitize_copilot_requirements(copilot, maa_dir=maa_dir)
    ok_mem, errs_mem = validate_copilot_payload(clean, maa_dir=maa_dir)
    if not ok_mem:
        raise ValueError(f"作业内容不合法: {'; '.join(errs_mem)}")

    serialized = json.dumps(clean, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{resolved.stem}_", suffix=".tmp", dir=str(resolved.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(resolved))
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        raise

    ok_disk, err_disk, _ = validate_copilot_file(resolved, maa_dir=maa_dir)
    if not ok_disk:
        raise ValueError(err_disk or f"落盘校验失败: {resolved}")
    return resolved, clean


def _collect_copilot_oper_names(copilot: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()
    if not isinstance(copilot, dict):
        return names
    for op in copilot.get("opers") or []:
        if isinstance(op, dict):
            n = str(op.get("name") or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    for grp in copilot.get("groups") or []:
        if isinstance(grp, dict):
            for op in grp.get("opers") or []:
                if isinstance(op, dict):
                    n = str(op.get("name") or "").strip()
                    if n and n not in seen:
                        seen.add(n)
                        names.append(n)
    return names


def pick_best_substitute(
    orig_name: str,
    my_box: Dict[str, Any],
    used_names: Optional[Iterable[str]] = None,
    blacklist: Optional[Iterable[str]] = None,
    fallback_role: str = "guard_lane",
    maa_dir: Optional[Path] = None,
    role_map: Optional[Dict[str, str]] = None,
    op_meta: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[Tuple[str, int, int]]:
    """Pick the highest-investment owned same-branch/same-block substitute (forbids cross-branch/0-block swaps)."""
    owned = my_box.get("all_owned", {}) or {}
    e2_ops = my_box.get("e2_operators", []) or []
    meta = op_meta if op_meta is not None else load_operator_metadata(maa_dir)
    rmap = role_map if role_map is not None else load_recruitment_roles(maa_dir)
    used = {str(x).strip() for x in (used_names or []) if str(x).strip()}
    bl = {str(x).strip() for x in (blacklist or []) if str(x).strip()}
    role = rmap.get(orig_name, fallback_role)

    curated_order: Dict[str, Tuple[int, int, int]] = {}
    curated_lists: List[List[Tuple[str, int, int]]] = [ROLE_SUBSTITUTES.get(role, [])]
    if role == "vanguard_bearer":
        curated_lists.append(ROLE_SUBSTITUTES.get("vanguard_block", []))
    for list_idx, lst in enumerate(curated_lists):
        for idx, (sub_name, sk, usg) in enumerate(lst):
            if sub_name not in curated_order:
                curated_order[sub_name] = (list_idx * 100 + idx, sk, usg)

    all_candidates: List[Tuple[Tuple[int, int, int, int, int, int], Tuple[str, int, int]]] = []
    seen_cands: Set[str] = set()

    def _consider(cand_name: str, op_info: Dict[str, Any]) -> None:
        if not cand_name or cand_name in seen_cands or cand_name in used or cand_name in bl or cand_name == orig_name:
            return
        seen_cands.add(cand_name)
        allowed, affinity = _is_compatible_substitute(orig_name, cand_name, meta, rmap, fallback_role=fallback_role)
        if not allowed or affinity <= 0:
            return
        elite = int(op_info.get("elite", 0) or 0)
        level = int(op_info.get("level", 1) or 1)
        rarity = int(op_info.get("rarity", 0) or 0)
        if cand_name in curated_order:
            ord_idx, sk, usg = curated_order[cand_name]
            is_curated = 1
            neg_ord = -ord_idx
        else:
            sk, usg = DEFAULT_SKILL_MAP.get(cand_name, (2 if elite >= 1 else 1, 1))
            is_curated = 0
            neg_ord = -9999
        if elite < _min_elite_for_skill(sk):
            sk = 2 if elite >= 1 else 1
            usg = 1
        # Rank strictly: E2 first -> exact sub-profession/block affinity -> elite -> level -> rarity -> curated order
        rank_key = (1 if elite >= 2 else 0, affinity, elite, level, rarity, is_curated * 10000 + neg_ord)
        all_candidates.append((rank_key, (cand_name, sk, usg)))

    for cand_name in curated_order:
        if cand_name in owned and isinstance(owned[cand_name], dict):
            _consider(cand_name, owned[cand_name])

    for e2 in e2_ops:
        if isinstance(e2, dict):
            en = str(e2.get("name") or "").strip()
            if en:
                _consider(en, owned.get(en, e2))

    for en, op_info in owned.items():
        if isinstance(op_info, dict):
            _consider(en, op_info)

    if not all_candidates:
        return None
    all_candidates.sort(key=lambda x: x[0], reverse=True)
    return all_candidates[0][1]


def substitute_unavailable_operators_in_copilot(
    copilot: Dict[str, Any],
    unavailable_opers: Iterable[str],
    my_box: Dict[str, Any],
    blacklist: Optional[Iterable[str]] = None,
    maa_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Replace unavailable/blacklisted operators with strict same-branch substitutes and sync opers/groups/actions."""
    owned = my_box.get("all_owned", {}) or {}
    op_meta = load_operator_metadata(maa_dir)
    role_map = load_recruitment_roles(maa_dir)
    unavail_list = [str(x).strip() for x in (unavailable_opers or []) if str(x).strip()]
    bl_set: Set[str] = {str(x).strip() for x in (blacklist or []) if str(x).strip()} | set(unavail_list)

    patched = copy.deepcopy(copilot)
    used_names: Set[str] = set()
    for op in patched.get("opers") or []:
        if isinstance(op, dict):
            n = str(op.get("name") or "").strip()
            if n and n not in bl_set:
                used_names.add(n)
    for grp in patched.get("groups") or []:
        if isinstance(grp, dict):
            for op in grp.get("opers") or []:
                if isinstance(op, dict):
                    n = str(op.get("name") or "").strip()
                    if n and n not in bl_set:
                        used_names.add(n)

    sub_map: Dict[str, str] = {}
    sub_op_specs: Dict[str, Dict[str, Any]] = {}
    unsubstituted: List[str] = []

    new_opers: List[Dict[str, Any]] = []
    for op in patched.get("opers") or []:
        if not isinstance(op, dict):
            new_opers.append(op)
            continue
        name = str(op.get("name") or "").strip()
        if name in bl_set:
            rep = pick_best_substitute(
                name,
                my_box,
                used_names=used_names,
                blacklist=bl_set,
                maa_dir=maa_dir,
                role_map=role_map,
                op_meta=op_meta,
            )
            if rep:
                sub_name, sub_sk, sub_usg = rep
                used_names.add(sub_name)
                sub_map[name] = sub_name
                sub_op_specs[name] = {"name": sub_name, "skill": sub_sk, "skill_usage": sub_usg}
                new_opers.append({
                    "name": sub_name,
                    "skill": sub_sk,
                    "skill_usage": sub_usg,
                    "requirements": _safe_substitute_requirements(owned[sub_name], sub_sk),
                })
            else:
                if name not in unsubstituted:
                    unsubstituted.append(name)
                new_opers.append(op)
        else:
            new_opers.append(op)
    patched["opers"] = new_opers

    new_groups: List[Dict[str, Any]] = []
    for grp in patched.get("groups") or []:
        if not isinstance(grp, dict):
            new_groups.append(grp)
            continue
        grp_copy = copy.deepcopy(grp)
        grp_name = str(grp_copy.get("name") or "").strip()
        cands = [o for o in (grp_copy.get("opers") or []) if isinstance(o, dict)]
        cand_names = [str(o.get("name") or "").strip() for o in cands if str(o.get("name") or "").strip()]
        needs_sub = grp_name in bl_set or (bool(cands) and all(str(o.get("name") or "").strip() in bl_set for o in cands))
        if needs_sub:
            ref_name = next((n for n in cand_names if n), grp_name)
            rep = pick_best_substitute(
                ref_name,
                my_box,
                used_names=used_names,
                blacklist=bl_set,
                maa_dir=maa_dir,
                role_map=role_map,
                op_meta=op_meta,
            )
            if rep:
                sub_name, sub_sk, sub_usg = rep
                used_names.add(sub_name)
                grp_copy["opers"] = [{
                    "name": sub_name,
                    "skill": sub_sk,
                    "skill_usage": sub_usg,
                    "requirements": _safe_substitute_requirements(owned[sub_name], sub_sk),
                }]
                spec_entry = {"name": sub_name, "skill": sub_sk, "skill_usage": sub_usg}
                for cn in cand_names:
                    sub_map[cn] = sub_name
                    sub_op_specs[cn] = spec_entry
                if ref_name:
                    sub_map[ref_name] = sub_name
                    sub_op_specs[ref_name] = spec_entry
                if grp_name:
                    if grp_name in bl_set or grp_name == ref_name or grp_name in cand_names:
                        grp_copy["name"] = sub_name
                        sub_map[grp_name] = sub_name
                        sub_op_specs[grp_name] = spec_entry
                    else:
                        # Generic group label (e.g. "先锋"): preserve group name in actions but sync skill/skill_usage
                        sub_op_specs[grp_name] = {"name": grp_name, "skill": sub_sk, "skill_usage": sub_usg}
            else:
                if ref_name and ref_name not in unsubstituted:
                    unsubstituted.append(ref_name)
        elif cands:
            filtered = [o for o in cands if str(o.get("name") or "").strip() not in bl_set]
            if len(filtered) != len(cands) and filtered:
                grp_copy["opers"] = filtered
                removed_names = [str(o.get("name") or "").strip() for o in cands if str(o.get("name") or "").strip() in bl_set]
                kept_op = filtered[0]
                kept_name = str(kept_op.get("name") or "").strip()
                kept_spec = {
                    "name": kept_name,
                    "skill": _safe_int(kept_op.get("skill")) or 1,
                    "skill_usage": _safe_int(kept_op.get("skill_usage")),
                }
                for rn in removed_names:
                    if rn and kept_name:
                        sub_map[rn] = kept_name
                        sub_op_specs[rn] = kept_spec
        new_groups.append(grp_copy)
    patched["groups"] = new_groups

    active_opers: List[Dict[str, Any]] = list(new_opers)
    for grp in new_groups:
        if isinstance(grp, dict):
            active_opers.extend(o for o in (grp.get("opers") or []) if isinstance(o, dict))
    patched["actions"] = _clean_actions_with_sub_map(
        patched.get("actions", []),
        sub_map,
        active_opers,
        sub_op_specs=sub_op_specs,
    )

    return {
        "ok": bool(sub_map) and len(unsubstituted) == 0,
        "copilot": patched,
        "substitutions": sub_map,
        "unsubstituted": unsubstituted,
        "blacklist": sorted(bl_set),
    }


def _adapt_guide_deterministic(
    guide: Dict[str, Any],
    my_box: Dict[str, Any],
    stage_id: str,
    maa_dir: Optional[Path] = None,
    blacklist: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    content = copy.deepcopy(guide["content"])
    original_opers = _collect_copilot_oper_names(content)
    owned = my_box.get("all_owned", {})
    op_meta = load_operator_metadata(maa_dir)
    role_map = load_recruitment_roles(maa_dir)
    bl_set: Set[str] = {str(x).strip() for x in (blacklist or []) if str(x).strip()}
    blacklisted_hits = [n for n in original_opers if n in bl_set]

    used_names: Set[str] = set()
    new_opers: List[Dict[str, Any]] = []
    sub_map: Dict[str, str] = {}
    sub_op_specs: Dict[str, Dict[str, Any]] = {}
    missing_ops: List[str] = []
    e1_downgrades: List[str] = []

    def _pick_substitute(orig_name: str, fallback_role: str = "guard_lane") -> Optional[Tuple[str, int, int]]:
        return pick_best_substitute(
            orig_name,
            my_box,
            used_names=used_names,
            blacklist=bl_set,
            fallback_role=fallback_role,
            maa_dir=maa_dir,
            role_map=role_map,
            op_meta=op_meta,
        )

    new_groups: List[Dict[str, Any]] = []
    for grp in content.get("groups", []):
        grp_copy = copy.deepcopy(grp)
        grp_name = str(grp.get("name") or "").strip()
        cands = grp.get("opers", [])
        picked = None
        for op in cands:
            n = op.get("name", "")
            op_rarity = (owned.get(n) or {}).get("rarity")
            min_e = max(2, _min_elite_for_skill(op.get("skill", 1), rarity=op_rarity, skill_usage=op.get("skill_usage")))
            if n in owned and n not in bl_set and owned[n].get("elite", 0) >= min_e and n not in used_names:
                picked = op
                break
        if not picked:
            for op in cands:
                n = op.get("name", "")
                op_rarity = (owned.get(n) or {}).get("rarity")
                min_e = _min_elite_for_skill(op.get("skill", 1), rarity=op_rarity, skill_usage=op.get("skill_usage"))
                if n in owned and n not in bl_set and owned[n].get("elite", 0) >= min_e and n not in used_names:
                    picked = op
                    break
        if picked:
            pname = picked["name"]
            used_names.add(pname)
            picked_copy = copy.deepcopy(picked)
            picked_copy["requirements"] = _clamp_requirements(picked_copy.get("requirements"), owned[pname], picked_copy.get("skill", 1))
            grp_copy["opers"] = [picked_copy]
            if owned[pname].get("elite", 0) < 2:
                e1_downgrades.append(pname)
        else:
            first_name = cands[0].get("name", "") if cands else ""
            rep = _pick_substitute(first_name)
            if rep:
                sub_name, sk, usg = rep
                used_names.add(sub_name)
                req = _safe_substitute_requirements(owned[sub_name], sk) if bl_set else _clamp_requirements({}, owned[sub_name], sk)
                grp_copy["opers"] = [{
                    "name": sub_name,
                    "skill": sk,
                    "skill_usage": usg,
                    "requirements": req,
                }]
                spec_entry = {"name": sub_name, "skill": sk, "skill_usage": usg}
                for cand_op in cands:
                    cn = str(cand_op.get("name") or "").strip()
                    if cn:
                        sub_map[cn] = sub_name
                        sub_op_specs[cn] = spec_entry
                if grp_name and (grp_name == first_name or grp_name in bl_set):
                    grp_copy["name"] = sub_name
                    sub_map[grp_name] = sub_name
                    sub_op_specs[grp_name] = spec_entry
                elif grp_name:
                    sub_op_specs[grp_name] = {"name": grp_name, "skill": sk, "skill_usage": usg}
            else:
                missing_ops.append(f"Group:{grp.get('name', '?')}")
        new_groups.append(grp_copy)

    for op in content.get("opers", []):
        n = op.get("name", "")
        op_rarity = (owned.get(n) or {}).get("rarity")
        min_e = _min_elite_for_skill(op.get("skill", 1), rarity=op_rarity, skill_usage=op.get("skill_usage"))
        if n in owned and n not in bl_set and owned[n].get("elite", 0) >= min_e:
            used_names.add(n)

    for op in content.get("opers", []):
        name = op.get("name", "")
        sk = op.get("skill", 1)
        owned_op = owned.get(name)
        op_rarity = (owned_op or {}).get("rarity")
        min_e = _min_elite_for_skill(sk, rarity=op_rarity, skill_usage=op.get("skill_usage"))
        guide_req_e = int(op.get("requirements", {}).get("elite", 0))

        if owned_op and name not in bl_set and owned_op.get("elite", 0) >= min_e and not (guide_req_e >= 2 and owned_op.get("elite", 0) == 0):
            op_copy = copy.deepcopy(op)
            op_copy["requirements"] = _clamp_requirements(op_copy.get("requirements"), owned_op, sk)
            if owned_op.get("elite", 0) < 2 and guide_req_e >= 2:
                e1_downgrades.append(name)
            new_opers.append(op_copy)
            continue

        rep = _pick_substitute(name)
        if rep:
            sub_name, sub_sk, sub_usg = rep
            used_names.add(sub_name)
            sub_map[name] = sub_name
            sub_op_specs[name] = {"name": sub_name, "skill": sub_sk, "skill_usage": sub_usg}
            req = _safe_substitute_requirements(owned[sub_name], sub_sk) if (name in bl_set) else _clamp_requirements({}, owned[sub_name], sub_sk)
            new_opers.append({
                "name": sub_name,
                "skill": sub_sk,
                "skill_usage": sub_usg,
                "requirements": req,
            })
        else:
            missing_ops.append(name)
            new_opers.append(copy.deepcopy(op))

    content["version"] = content.get("version", 3)
    content["stage_name"] = stage_id or content.get("stage_name", "")
    content["minimum_required"] = content.get("minimum_required", "v6.0.0")
    content["opers"] = new_opers
    content["groups"] = new_groups
    active_opers: List[Dict[str, Any]] = list(new_opers)
    for grp in new_groups:
        if isinstance(grp, dict):
            active_opers.extend(o for o in (grp.get("opers") or []) if isinstance(o, dict))
    content["actions"] = _clean_actions_with_sub_map(
        content.get("actions", []),
        sub_map,
        active_opers,
        sub_op_specs=sub_op_specs,
    )

    like = guide.get("like", 0) or 0
    views = guide.get("views", 0) or 0
    squad_size = len(new_opers) + len(new_groups)
    full_native_match = bool(not missing_ops and not sub_map and not blacklisted_hits)

    # Prefer higher fault-tolerance 3~6 operator conventional guides over fragile 1~2 operator tight-axis guides
    if 3 <= squad_size <= 6:
        squad_bonus = 450
    elif squad_size >= 7:
        squad_bonus = 200
    elif 1 <= squad_size <= 2:
        squad_bonus = -400 - (600 if (sub_map or e1_downgrades) else 0)
    else:
        squad_bonus = 0

    full_match_bonus = (600 + (250 if not e1_downgrades else 0)) if full_native_match else 0

    compat_score = (
        like * 25
        + min(views // 100, 200)
        + squad_bonus
        + full_match_bonus
        - len(missing_ops) * 5000
        - len(blacklisted_hits) * 2000
        - len(sub_map) * 350
        - len(e1_downgrades) * 180
    )

    return {
        "guide_id": guide.get("id"),
        "like": like,
        "views": views,
        "squad_size": squad_size,
        "full_native_match": full_native_match,
        "compat_score": compat_score,
        "missing_ops": missing_ops,
        "substitutions": sub_map,
        "e1_downgrades": e1_downgrades,
        "original_opers": original_opers,
        "blacklisted_hits": blacklisted_hits,
        "copilot": content,
        "mode": "pure_maa",
    }


def _apply_ai_adaptation(
    guide: Dict[str, Any],
    baseline: Dict[str, Any],
    ai_reply: Dict[str, Any],
    my_box: Dict[str, Any],
    stage_id: str,
    blacklist: Optional[Iterable[str]] = None,
    maa_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    owned = my_box.get("all_owned", {})
    op_meta = load_operator_metadata(maa_dir)
    role_map = load_recruitment_roles(maa_dir)
    bl_set: Set[str] = {str(x).strip() for x in (blacklist or []) if str(x).strip()}
    subs = ai_reply.get("substitutions", {})
    skill_overrides = ai_reply.get("skill_overrides", {})
    if not isinstance(subs, dict) or not isinstance(skill_overrides, dict):
        return None

    group_first_op: Dict[str, str] = {}
    for grp in baseline.get("copilot", {}).get("groups", []):
        if isinstance(grp, dict) and grp.get("name"):
            g_ops = grp.get("opers") or []
            if g_ops and isinstance(g_ops[0], dict) and g_ops[0].get("name"):
                group_first_op[str(grp["name"])] = str(g_ops[0]["name"])

    for orig_name, sub_name in subs.items():
        if not isinstance(orig_name, str) or not isinstance(sub_name, str):
            return None
        if sub_name not in owned or sub_name in bl_set:
            return None
        ref_orig = group_first_op.get(orig_name, orig_name)
        if ref_orig != sub_name and (ref_orig in op_meta or ref_orig in role_map):
            allowed, _ = _is_compatible_substitute(ref_orig, sub_name, op_meta, role_map)
            if not allowed:
                return None

    content = copy.deepcopy(guide["content"])
    merged_subs = dict(baseline.get("substitutions", {}))
    merged_subs.update(subs)
    sub_op_specs: Dict[str, Dict[str, Any]] = {}

    used = set()
    new_groups: List[Dict[str, Any]] = []
    for grp in baseline["copilot"].get("groups", []):
        grp_copy = copy.deepcopy(grp)
        grp_name = grp_copy.get("name", "")
        if grp_name in subs:
            sub_name = subs[grp_name]
            owned_op = owned.get(sub_name)
            if not owned_op or sub_name in used:
                return None
            sk, usg = DEFAULT_SKILL_MAP.get(sub_name, (1, 1))
            ov = skill_overrides.get(sub_name, {})
            if ov:
                if not isinstance(ov, dict):
                    return None
                sk = _safe_int(ov.get("skill", sk))
                usg = _safe_int(ov.get("skill_usage", usg))
                if sk is None or usg is None:
                    return None
            if sk not in (1, 2, 3) or usg not in (0, 1, 2, 3) or owned_op.get("elite", 0) < _min_elite_for_skill(sk):
                return None
            used.add(sub_name)
            grp_copy["opers"] = [{
                "name": sub_name,
                "skill": sk,
                "skill_usage": usg,
                "requirements": _clamp_requirements({}, owned_op, sk),
            }]
            sub_op_specs[grp_name] = {"name": grp_name, "skill": sk, "skill_usage": usg}
        else:
            for op in grp_copy.get("opers", []):
                if op.get("name") in owned:
                    used.add(op["name"])
        new_groups.append(grp_copy)

    new_opers: List[Dict[str, Any]] = []
    missing_ops: List[str] = []

    for op in content.get("opers", []):
        orig = op.get("name", "")
        target_name = merged_subs.get(orig, orig)
        owned_op = owned.get(target_name)
        if target_name in used:
            return None
        if not owned_op:
            if orig in subs:
                return None
            missing_ops.append(orig)
            new_opers.append(copy.deepcopy(op))
            continue

        used.add(target_name)
        ov = skill_overrides.get(target_name, {})
        if not isinstance(ov, dict):
            return None
        sk = _safe_int(ov.get("skill", op.get("skill", DEFAULT_SKILL_MAP.get(target_name, (1, 1))[0])))
        usg = _safe_int(ov.get("skill_usage", op.get("skill_usage", DEFAULT_SKILL_MAP.get(target_name, (1, 1))[1])))
        if sk is None or usg is None or sk not in (1, 2, 3) or usg not in (0, 1, 2, 3):
            return None
        if owned_op.get("elite", 0) < _min_elite_for_skill(sk):
            return None

        op_copy = copy.deepcopy(op)
        op_copy["name"] = target_name
        op_copy["skill"] = sk
        op_copy["skill_usage"] = usg
        op_copy["requirements"] = _clamp_requirements(op_copy.get("requirements"), owned_op, sk)
        new_opers.append(op_copy)
        if target_name != orig:
            sub_op_specs[orig] = {"name": target_name, "skill": sk, "skill_usage": usg}

    content["version"] = content.get("version", 3)
    content["stage_name"] = stage_id or content.get("stage_name", "")
    content["minimum_required"] = content.get("minimum_required", "v6.0.0")
    content["opers"] = new_opers
    content["groups"] = new_groups
    active_opers: List[Dict[str, Any]] = list(new_opers)
    for grp in new_groups:
        if isinstance(grp, dict):
            active_opers.extend(o for o in (grp.get("opers") or []) if isinstance(o, dict))
    content["actions"] = _clean_actions_with_sub_map(
        content.get("actions", []),
        merged_subs,
        active_opers,
        sub_op_specs=sub_op_specs,
    )

    out = dict(baseline)
    out["copilot"] = content
    out["substitutions"] = merged_subs
    out["missing_ops"] = missing_ops
    out["mode"] = "ai_assisted"
    if isinstance(ai_reply.get("notes"), str):
        out["ai_notes"] = ai_reply["notes"]
    return out


def adapt_copilot(
    guide: Dict[str, Any],
    my_box: Dict[str, Any],
    stage_id: str,
    maa_dir: Optional[Path] = None,
    client: Optional[OpenAICompatClient] = None,
    blacklist: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Adapt PRTS copilot guide to user Box, using OpenAI strategist when configured and falling back seamlessly."""
    baseline = _adapt_guide_deterministic(guide, my_box, stage_id, maa_dir=maa_dir, blacklist=blacklist)
    if client is None or not client.is_configured():
        return baseline

    needs_help = bool(baseline["missing_ops"] or baseline["substitutions"] or baseline["e1_downgrades"])
    if not needs_help:
        return baseline

    e2_summary = [
        {"name": o["name"], "rarity": o.get("rarity"), "level": o.get("level")}
        for o in my_box.get("e2_operators", [])[:35]
    ]
    sys_prompt = (
        "你是明日方舟 MAA Copilot 作业适配助手。仅输出合法 JSON，格式为: "
        '{"substitutions": {"原干员名": "替补干员名"}, "skill_overrides": {"干员名": {"skill": 1-3, "skill_usage": 0-3}}, "notes": "..."}。'
        "只能选择用户已拥有干员，严禁跨高台/地面或用0阻挡执旗手(桃金娘/极境/琴柳)替补阻挡先锋。"
    )
    usr_prompt = json.dumps({
        "stage_id": stage_id,
        "guide_opers": guide.get("content", {}).get("opers", []),
        "baseline_missing": baseline["missing_ops"],
        "baseline_subs": baseline["substitutions"],
        "blacklist": sorted({str(x).strip() for x in (blacklist or []) if str(x).strip()}),
        "owned_e2": e2_summary,
    }, ensure_ascii=False)

    ai_reply = client.chat_json(sys_prompt, usr_prompt)
    if ai_reply is not None:
        applied = _apply_ai_adaptation(
            guide,
            baseline,
            ai_reply,
            my_box,
            stage_id,
            blacklist=blacklist,
            maa_dir=maa_dir,
        )
        if applied is not None:
            return applied

    fallback = dict(baseline)
    fallback["mode"] = "pure_maa_fallback"
    return fallback


_BENIGN_COPILOT_PROBES = frozenset({
    "SkipThePreBattlePlot",
    "SkipBattlePlot",
    "SkipThePreBattlePlotConfirm",
    "SkipBattlePlotConfirm",
    "EndOfPlot",
    "SkipForbiddenOperConfirm",
    "BattleStartPlot",
    "BattleStartPre",
    "StageNavigationBegin",
    "ClickPlotStage",
})

_STAGE_ENTRY_ERROR_TASKS = frozenset({
    "BattleStartPre",
    "BattleStartAll",
    "FightBegin",
})

_STAGE_ENTRY_LOG_RE = re.compile(
    r"SubTaskError[^\n]*?(?:BattleStartPre|BattleStartAll|FightBegin)",
    re.IGNORECASE,
)

_FORMATION_SELECT_TASKS = frozenset({
    "BattleQuickFormationClear",
    "BattleQuickFormationExpandRole",
    "BattleQuickFormationCollapseRole",
    "BattleQuickFormationSkillPage",
    "BattleQuickFormationConfirm",
})


def _record_unavailable_oper(
    out_ops: List[str],
    out_reasons: Dict[str, str],
    oper_name: Any,
    reason: Any = "Unavailable",
    prefer_reason: bool = False,
) -> None:
    name = str(oper_name or "").strip()
    if not name:
        return
    if name not in out_ops:
        out_ops.append(name)
    r_str = str(reason or "Unavailable").strip() or "Unavailable"
    if name not in out_reasons or prefer_reason or (out_reasons[name] == "Unavailable" and r_str != "Unavailable"):
        out_reasons[name] = r_str


def _extract_unavailable_from_opers_obj(
    opers_obj: Any,
    out_ops: List[str],
    out_reasons: Dict[str, str],
) -> None:
    if isinstance(opers_obj, dict):
        for group_key, val in opers_obj.items():
            if isinstance(val, list) and val:
                for item in val:
                    if isinstance(item, dict):
                        _record_unavailable_oper(
                            out_ops,
                            out_reasons,
                            item.get("name") or group_key,
                            item.get("reason") or "Unavailable",
                        )
                    elif isinstance(item, str):
                        _record_unavailable_oper(out_ops, out_reasons, item, "Unavailable")
            elif isinstance(val, dict):
                _record_unavailable_oper(
                    out_ops,
                    out_reasons,
                    val.get("name") or group_key,
                    val.get("reason") or "Unavailable",
                )
            elif isinstance(group_key, str) and group_key.strip():
                _record_unavailable_oper(out_ops, out_reasons, group_key, "Unavailable")
    elif isinstance(opers_obj, list):
        for item in opers_obj:
            if isinstance(item, dict):
                _record_unavailable_oper(
                    out_ops,
                    out_reasons,
                    item.get("name"),
                    item.get("reason") or "Unavailable",
                )
            elif isinstance(item, str):
                _record_unavailable_oper(out_ops, out_reasons, item, "Unavailable")


def _extract_json_objects_from_text(text: str) -> List[Dict[str, Any]]:
    objs: List[Dict[str, Any]] = []
    if not text or "{" not in text:
        return objs
    dec = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        pos = text.find("{", idx)
        if pos == -1:
            break
        try:
            obj, end = dec.raw_decode(text, pos)
            if isinstance(obj, dict):
                objs.append(obj)
            idx = max(end, pos + 1)
        except Exception:
            idx = pos + 1
    return objs


def _extract_formation_from_json_obj(
    obj: Dict[str, Any],
    out_ops: List[str],
    out_reasons: Dict[str, str],
) -> bool:
    """Inspect a parsed JSON payload from MAA event/log/error string; return True if it indicates BattleFormation error."""
    found_err = False
    subtask = str(obj.get("subtask") or "")
    cls_name = str(obj.get("class") or "")
    what = str(obj.get("what") or "")
    why = str(obj.get("why") or "")
    inner = obj.get("details") if isinstance(obj.get("details"), dict) else {}

    if what == "BattleFormationOperUnavailable":
        found_err = True
        op_name = inner.get("oper_name") or obj.get("oper_name")
        req_type = inner.get("requirement_type") or obj.get("requirement_type") or "Unavailable"
        _record_unavailable_oper(out_ops, out_reasons, op_name, req_type, prefer_reason=True)

    opers_obj = inner.get("opers") if isinstance(inner, dict) and "opers" in inner else obj.get("opers")
    if opers_obj is not None:
        found_err = True
        _extract_unavailable_from_opers_obj(opers_obj, out_ops, out_reasons)

    if why in ("OperatorMissing", "FormationFailed") and ("BattleFormation" in subtask or "BattleFormation" in cls_name):
        found_err = True

    return found_err


def _scan_text_for_formation_error(
    text: str,
    out_ops: List[str],
    out_reasons: Dict[str, str],
) -> Tuple[bool, bool, bool]:
    """Scan a log line or error string for (formation_entered, formation_select_entered, formation_error)."""
    entered = False
    select_entered = False
    has_err = False
    if not text:
        return entered, select_entered, has_err

    if (
        "BattleQuickFormation" in text
        or "BattleFormationOperUnavailable" in text
        or "BattleFormationSelected" in text
        or "BattleFormationTask::check_and_select_skill" in text
        or "BattleFormationTask::add_formation" in text
        or "BattleFormationTask::select_opers_in_cur_page" in text
    ):
        entered = True

    if (
        "BattleQuickFormationClear" in text
        or "BattleQuickFormationRole-" in text
        or "BattleQuickFormationSkillPage" in text
        or "BattleFormationOperUnavailable" in text
        or "BattleFormationSelected" in text
        or "select_opers_in_cur_page" in text
        or "formation_select" in text
    ):
        select_entered = True

    is_form_sub_err = (
        ("SubTaskError" in text and "BattleFormationTask" in text)
        or ("BattleFormationOperUnavailable" in text)
        or ("BattleFormationTask" in text and ("OperatorMissing" in text or '"Unavailable"' in text))
        or ("BattleFormationTask::check_and_select_skill" in text and "require:" in text)
    )
    if is_form_sub_err:
        has_err = True
        entered = True

    if is_form_sub_err or '"opers"' in text or '"oper_name"' in text:
        for obj in _extract_json_objects_from_text(text):
            if _extract_formation_from_json_obj(obj, out_ops, out_reasons):
                has_err = True
                entered = True

    return entered, select_entered, has_err


_LIST_NOISE_RE = re.compile(
    r'"(?:to_be_recognized|next|on_error_next|interrupt)"\s*:\s*\[[^\]]*\]'
)

_DEFEAT_TOKENS = (
    "FightMissionFailed",
    "Copilot@FightMissionFailed",
    "MissionFailed",
    "OperationFailed",
    "BattleFailed",
    "行动失败",
    "任务失败",
    "作战失败",
)

_AGENT_LOCKED_TOKENS = (
    "UnableToAgent",
    "代理未解锁",
    "无法代理",
    "未解锁代理",
)

_VICTORY_TASK_NAMES = frozenset({
    "StageDrops",
    "StageDrops-Stars-3",
    "StageDrops-Stars-Adverse",
    "EndOfAction",
    "EndOfActionAnnihilation",
    "MissionCompleted",
})

_VICTORY_TASK_LOG_RE = re.compile(
    r'"(?:task|what)"\s*:\s*"(?:StageDrops|StageDrops-Stars-3|StageDrops-Stars-Adverse|EndOfAction|EndOfActionAnnihilation|MissionCompleted)"'
)

_VICTORY_TEXT_TOKENS = (
    "行动结束",
    "行动完成",
    "MISSION ACCOMPLISHED",
)

_RELAXABLE_REQ_REASONS = frozenset({
    "skill_level",
    "level",
    "module",
    "potentiality",
})


def _strip_pipeline_list_noise(text: str) -> str:
    if not text:
        return ""
    return _LIST_NOISE_RE.sub("", text)


def parse_battle_metrics(
    log_lines: List[str],
    events: List[Dict[str, Any]],
    entered_formation: bool = False,
) -> Dict[str, Any]:
    """Extract kills, DP costs, settlement status, formation-unavailable ops, entry-failure flags, and errors."""
    kills = None
    costs = None
    actions: List[str] = []
    fatal_errors: List[str] = []
    probe_errors: List[str] = []
    entry_failed_tasks: List[str] = []
    unavailable_opers: List[str] = []
    unavailable_oper_reasons: Dict[str, str] = {}
    defeat_signals: List[str] = []
    battle_started = False
    formation_entered = bool(entered_formation)
    formation_select_entered = False
    formation_error = False
    copilot_file_error = False
    copilot_file_error_details: List[str] = []
    chain_error_seen = False
    failed = False
    completed = False
    victory_settlement_confirmed = False
    auto_deploy_not_unlocked = False
    settlement_stars: Optional[int] = None

    def _add_file_err_detail(msg: str) -> None:
        cleaned_msg = str(msg or "").strip()
        if cleaned_msg and cleaned_msg not in copilot_file_error_details:
            copilot_file_error_details.append(cleaned_msg)

    kill_re = re.compile(r'"kills"\s*:\s*(\d+)|kills[=:]\s*(\d+)', re.IGNORECASE)
    cost_re = re.compile(r'"costs?"\s*:\s*(\d+)|costs?[=:]\s*(\d+)', re.IGNORECASE)

    def _add_defeat_sig(sig: str) -> None:
        if sig and sig not in defeat_signals:
            defeat_signals.append(sig)

    for ev in events:
        msg_name = ev.get("message", "")
        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
        subtask = str(d.get("subtask", "") or "")
        cls_name = str(d.get("class", "") or "")
        what = str(d.get("what", "") or "")
        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
        task_name = str(det.get("task", "") or "")

        if msg_name == "SubTaskStart" and subtask == "BattleProcessTask":
            battle_started = True

        if msg_name in ("SubTaskStart", "SubTaskCompleted"):
            if task_name.startswith("BattleQuickFormation"):
                formation_entered = True
                if task_name in _FORMATION_SELECT_TASKS or task_name.startswith("BattleQuickFormationRole-"):
                    formation_select_entered = True
            if task_name in _VICTORY_TASK_NAMES:
                victory_settlement_confirmed = True
                battle_started = True
                if task_name in ("StageDrops-Stars-3", "StageDrops-Stars-Adverse"):
                    settlement_stars = 3
            elif task_name in ("StageDrops-Stars-2", "StageDrops-Stars-1"):
                settlement_stars = 1 if task_name.endswith("-1") else 2
                auto_deploy_not_unlocked = True
                battle_started = True
                _add_defeat_sig(task_name)
            elif task_name in ("FightMissionFailed", "Copilot@FightMissionFailed", "MissionFailed", "OperationFailed"):
                failed = True
                battle_started = True
                _add_defeat_sig(task_name)
            first_ev = d.get("first")
            if isinstance(first_ev, list):
                for ft in first_ev:
                    fts = str(ft)
                    if fts.startswith("BattleQuickFormation"):
                        formation_entered = True
                    if fts in _FORMATION_SELECT_TASKS or fts.startswith("BattleQuickFormationRole-"):
                        formation_select_entered = True

        if msg_name == "SubTaskExtraInfo":
            if what in ("BattleFormation", "BattleFormationSelected", "BattleFormationOperUnavailable"):
                formation_entered = True
            if what in ("BattleFormationSelected", "BattleFormationOperUnavailable"):
                formation_select_entered = True
            if what == "BattleFormationOperUnavailable":
                formation_error = True
                _extract_formation_from_json_obj(d, unavailable_opers, unavailable_oper_reasons)
            elif what in ("StageDrops", "EndOfAction", "MissionCompleted"):
                victory_settlement_confirmed = True
                battle_started = True
            elif what in ("FightMissionFailed", "MissionFailed", "OperationFailed", "BattleFailed"):
                failed = True
                battle_started = True
                _add_defeat_sig(what)
            elif what == "UnableToAgent":
                auto_deploy_not_unlocked = True
                _add_defeat_sig(what)

        if msg_name in ("TaskChainError", "InternalError", "InitFailed"):
            chain_error_seen = True
            fatal_errors.append(f"{msg_name}:{subtask or what}:{json.dumps(det, ensure_ascii=False)}")
        elif msg_name == "SubTaskError":
            d_json_txt = json.dumps(d, ensure_ascii=False)
            if what == "CopilotFileReadError" or "CopilotFileReadError" in d_json_txt:
                copilot_file_error = True
                _add_file_err_detail(what or "CopilotFileReadError")
            if subtask == "BattleFormationTask" or "BattleFormationTask" in cls_name:
                formation_error = True
                formation_entered = True
                _extract_formation_from_json_obj(d, unavailable_opers, unavailable_oper_reasons)
            first_list = [str(x) for x in d.get("first", [])] if isinstance(d.get("first"), list) else []
            for ft in first_list:
                if ft in _STAGE_ENTRY_ERROR_TASKS and ft not in entry_failed_tasks:
                    entry_failed_tasks.append(ft)
            if first_list:
                first_tag = ",".join(first_list)
            elif what and what != subtask:
                first_tag = f"{what}:{json.dumps(det, ensure_ascii=False)}" if det else what
            else:
                first_tag = json.dumps(det, ensure_ascii=False)
            err_entry = f"{msg_name}:{subtask or what}:{first_tag}"
            if first_list and all(x in _BENIGN_COPILOT_PROBES for x in first_list):
                probe_errors.append(err_entry)
            else:
                fatal_errors.append(err_entry)
        if msg_name == "TaskChainCompleted" and d.get("taskchain") == "Copilot":
            completed = True

        d_clean = _strip_pipeline_list_noise(json.dumps(d, ensure_ascii=False))
        for tok in _DEFEAT_TOKENS:
            if tok in d_clean:
                failed = True
                battle_started = True
                _add_defeat_sig(tok)
        for tok in _AGENT_LOCKED_TOKENS:
            if tok in d_clean:
                auto_deploy_not_unlocked = True
                _add_defeat_sig(tok)
        if any(tok in d_clean for tok in _VICTORY_TEXT_TOKENS):
            victory_settlement_confirmed = True
            battle_started = True

        if isinstance(det, dict):
            if "kills" in det:
                kills = det["kills"]
                if kills is not None:
                    battle_started = True
            if "cost" in det:
                costs = det["cost"]
                if costs is not None:
                    battle_started = True
            if "costs" in det:
                costs = det["costs"]
                if costs is not None:
                    battle_started = True
            if "action" in det or "name" in det or what:
                act_str = f"{subtask or what}:{det.get('action', '')}:{det.get('name', '')}@{det.get('location', '')}".strip(":")
                if act_str and (not actions or actions[-1] != act_str):
                    actions.append(act_str)

    for line in log_lines:
        is_trc = ("[TRC]" in line or "TRACE" in line)
        if not is_trc and any(
            tok in line
            for tok in (
                "CopilotFileReadError",
                "CopilotConfig parse failed",
                "asst::CopilotConfig::parse_oper_usage",
                "asst::CopilotConfig::parse_groups",
                "asst::Assistant::append_task | invalid params",
            )
        ):
            copilot_file_error = True
            _add_file_err_detail(re.sub(r"^\[[^\]]+\](?:\[[^\]]+\])*\s*", "", line.strip())[:180])
        if "BattleOfficiallyBegin" in line:
            battle_started = True
        elif "BattleProcessTask" in line and "set_stage_name" not in line and not is_trc:
            battle_started = True

        f_ent, f_sel, f_err = _scan_text_for_formation_error(line, unavailable_opers, unavailable_oper_reasons)
        if f_ent:
            formation_entered = True
        if f_sel:
            formation_select_entered = True
        if f_err:
            formation_error = True

        if _STAGE_ENTRY_LOG_RE.search(line):
            for tok in _STAGE_ENTRY_ERROR_TASKS:
                if tok in line and tok not in entry_failed_tasks:
                    entry_failed_tasks.append(tok)

        line_clean = _strip_pipeline_list_noise(line)
        if not is_trc:
            hit_defeat = next((tok for tok in _DEFEAT_TOKENS if tok in line_clean), None)
            if hit_defeat:
                failed = True
                battle_started = True
                _add_defeat_sig(hit_defeat)
                tail_snip = line.strip()[-180:]
                if tail_snip and tail_snip not in fatal_errors:
                    fatal_errors.append(tail_snip)

            hit_agent_locked = next((tok for tok in _AGENT_LOCKED_TOKENS if tok in line_clean), None)
            if hit_agent_locked:
                auto_deploy_not_unlocked = True
                _add_defeat_sig(hit_agent_locked)

            if '"StageDrops-Stars-1"' in line_clean:
                settlement_stars = 1
                auto_deploy_not_unlocked = True
                battle_started = True
                _add_defeat_sig("StageDrops-Stars-1")
            elif '"StageDrops-Stars-2"' in line_clean:
                settlement_stars = 2
                auto_deploy_not_unlocked = True
                battle_started = True
                _add_defeat_sig("StageDrops-Stars-2")
            elif _VICTORY_TASK_LOG_RE.search(line_clean) or any(tok in line_clean for tok in _VICTORY_TEXT_TOKENS):
                victory_settlement_confirmed = True
                battle_started = True
                if '"StageDrops-Stars-3"' in line_clean or '"StageDrops-Stars-Adverse"' in line_clean:
                    settlement_stars = 3

        for m in kill_re.finditer(line):
            val = m.group(1) or m.group(2)
            if val is not None:
                kills = int(val)
                battle_started = True
        for m in cost_re.finditer(line):
            val = m.group(1) or m.group(2)
            if val is not None:
                costs = int(val)
                battle_started = True
        if any(tag in line for tag in ("Copilot", "Deploy", "Retreat", "Skill", "BulletTime", "Action")):
            if not is_trc:
                txt = line.strip()
                if txt:
                    actions.append(txt[-160:])

    non_3_star = bool(settlement_stars is not None and settlement_stars < 3)
    if failed or auto_deploy_not_unlocked or non_3_star:
        victory_settlement_confirmed = False

    # Priority rule: once formation (or formation_select / BattleFormationTask error) is reached,
    # the leading BattleStartPre probe error is strictly harmless and must be filtered out.
    harmless_entry_probes: List[str] = []
    if formation_entered or formation_select_entered or formation_error or bool(unavailable_opers):
        if "BattleStartPre" in entry_failed_tasks:
            entry_failed_tasks = [t for t in entry_failed_tasks if t != "BattleStartPre"]
            harmless_entry_probes.append("BattleStartPre")
        if formation_error or bool(unavailable_opers):
            probe_errors = [e for e in probe_errors if "BattleStartPre" not in e]

    errors = fatal_errors if (completed and not failed and not chain_error_seen) else (fatal_errors + probe_errors)
    formation_unavailable = bool(
        (not copilot_file_error)
        and (not completed)
        and (not failed)
        and (kills is None)
        and (not battle_started)
        and (formation_error or bool(unavailable_opers) or (formation_select_entered and not entry_failed_tasks))
    )
    stage_entry_failed = bool(
        (not copilot_file_error)
        and (not completed)
        and (not failed)
        and (kills is None)
        and (not battle_started)
        and (not formation_unavailable)
        and (not formation_error)
        and (not unavailable_opers)
        and (not formation_select_entered)
        and bool(entry_failed_tasks)
    )
    return {
        "kills": kills,
        "costs": costs,
        "last_3_actions": actions[-3:],
        "errors": errors[-5:],
        "failed_flag": failed,
        "battle_failed": bool((failed or auto_deploy_not_unlocked) and not copilot_file_error),
        "completed_flag": completed,
        "copilot_file_error": copilot_file_error,
        "copilot_file_error_detail": "; ".join(copilot_file_error_details[:3]),
        "victory_settlement_confirmed": victory_settlement_confirmed,
        "auto_deploy_not_unlocked": auto_deploy_not_unlocked,
        "settlement_stars": settlement_stars,
        "non_3_star": non_3_star,
        "defeat_signals": defeat_signals,
        "battle_started": battle_started,
        "entry_failed_tasks": entry_failed_tasks,
        "harmless_entry_probes": harmless_entry_probes,
        "stage_entry_failed": stage_entry_failed,
        "formation_entered": formation_entered,
        "formation_select_entered": formation_select_entered,
        "formation_error": formation_error,
        "formation_unavailable": formation_unavailable,
        "unavailable_opers": unavailable_opers,
        "unavailable_oper_reasons": unavailable_oper_reasons,
    }


def evaluate_copilot_victory(
    metrics: Dict[str, Any],
    post_battle_state: Optional[Dict[str, Any]] = None,
    map_verify: Optional[Dict[str, Any]] = None,
    require_map_verify: bool = False,
    story_probe_battle_failed: bool = False,
    story_probe_battle_victory: bool = False,
) -> Dict[str, Any]:
    """Dual hard victory verification: BOTH settlement verification AND closed-loop map verification must pass.

    Never treat MAA Copilot's TaskChainCompleted alone as victory.
    """
    m = metrics if isinstance(metrics, dict) else {}
    pb = post_battle_state if isinstance(post_battle_state, dict) else {}
    mv = map_verify if isinstance(map_verify, dict) else None
    reasons: List[str] = []

    completed_flag = bool(m.get("completed_flag"))
    failed_flag = bool(m.get("failed_flag"))
    is_file_err = bool(m.get("copilot_file_error"))
    auto_locked = bool(m.get("auto_deploy_not_unlocked"))
    has_errors = bool(m.get("errors"))
    pb_failed = bool(pb.get("battle_failed")) or bool(story_probe_battle_failed)
    mv_failed_screen = bool(mv and mv.get("battle_failed_screen"))
    cand_stars: List[int] = []
    for raw_s in (
        pb.get("settlement_stars"),
        m.get("settlement_stars"),
        mv.get("verified_stars") if mv is not None else None,
        mv.get("detail_panel_stars") if mv is not None else None,
    ):
        if raw_s is not None:
            try:
                cand_stars.append(int(raw_s))
            except Exception:
                pass
    stars: Optional[int] = min(cand_stars) if cand_stars else None
    mv_non_3 = bool(mv is not None and not mv.get("skipped") and (mv.get("non_3_star") or mv.get("three_star_verified") is False))
    non_3_star = bool((stars is not None and stars < 3) or bool(m.get("non_3_star")) or mv_non_3)

    has_victory_marker = bool(
        m.get("victory_settlement_confirmed")
        or pb.get("battle_victory_seen")
        or story_probe_battle_victory
    )

    # Check 1: Settlement event / log / post-battle screen verification
    settlement_ok = True
    if not completed_flag:
        settlement_ok = False
        reasons.append("未收到 Copilot TaskChainCompleted")
    if failed_flag or m.get("battle_failed"):
        settlement_ok = False
        sigs = m.get("defeat_signals") or []
        reasons.append(f"日志/回调出现战败信号 ({','.join(sigs) if sigs else 'MissionFailed'})")
    if stars is not None and stars < 3:
        settlement_ok = False
        reasons.append(f"battle_failed (non_3_star, stars={stars}): 仅获得 {stars} 星（漏怪未满 3 星），禁止放行")
    elif auto_locked or non_3_star:
        settlement_ok = False
        reasons.append("battle_failed (non_3_star): 代理指挥未解锁或未满 3 星，未达 3 星代理标准")
    if pb_failed or mv_failed_screen:
        settlement_ok = False
        reasons.append("战后画面检测到「行动失败 / MISSION FAILED」结算界面")
    if has_errors:
        settlement_ok = False
        reasons.append(f"Copilot 执行存在错误 ({len(m.get('errors') or [])} 条)")
    if is_file_err:
        settlement_ok = False
        detail_txt = str(m.get("copilot_file_error_detail") or "").strip()
        reasons.append(
            f"CopilotFileReadError: 作业文件读取或解析失败（非战斗翻车）{': ' + detail_txt if detail_txt else ''}"
        )
    if completed_flag and settlement_ok and not has_victory_marker:
        settlement_ok = False
        reasons.append("仅收到 TaskChainCompleted 但未检测到通关结算标识（EndOfAction/StageDrops/行动结束），严禁判胜")

    # Check 2: Closed-loop stage map verification (current stage 3-star blue badge & next stage unlocked)
    map_ok = True
    if mv is not None:
        if mv.get("skipped"):
            map_ok = True
        else:
            map_ok = bool(mv.get("verified")) and not mv_non_3
            if not map_ok:
                reasons.append(str(mv.get("reason") or "地图闭环校验未通过：当前关未满 3 星或下一关未解锁"))
    elif require_map_verify:
        map_ok = False
        reasons.append("缺少战后地图闭环校验结果，无法确认当前关已 3 星通关")

    victory = bool(settlement_ok and map_ok and not non_3_star and not is_file_err)
    battle_failed = bool(
        (not victory)
        and (not is_file_err)
        and (
            failed_flag
            or auto_locked
            or non_3_star
            or pb_failed
            or mv_failed_screen
            or completed_flag
            or bool(m.get("battle_started"))
            or (mv is not None and not mv.get("skipped") and not mv.get("verified"))
        )
    )
    return {
        "victory": victory,
        "battle_failed": battle_failed,
        "copilot_file_error": is_file_err,
        "non_3_star": non_3_star,
        "settlement_ok": settlement_ok,
        "map_ok": map_ok,
        "has_victory_marker": has_victory_marker,
        "settlement_stars": stars,
        "reasons": reasons,
    }


_COPILOT_FILE_ERROR_LOG_TOKENS = (
    "CopilotFileReadError",
    "CopilotConfig parse failed",
    "asst::CopilotConfig::parse_oper_usage",
    "asst::CopilotConfig::parse_groups",
    "asst::Assistant::append_task | invalid params",
)


def extract_copilot_file_error_info(report_or_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Identify CopilotFileReadError / CopilotConfig parse failure separately from battle defeat."""
    empty = {"is_copilot_file_error": False, "copilot_file": "", "detail": ""}
    if not isinstance(report_or_metrics, dict):
        return empty
    metrics = report_or_metrics.get("metrics") if isinstance(report_or_metrics.get("metrics"), dict) else report_or_metrics
    if metrics.get("completed_flag") or metrics.get("battle_started") or metrics.get("kills") is not None:
        return empty

    copilot_file = str(report_or_metrics.get("copilot_file") or "").strip()
    details: List[str] = []
    detected = bool(report_or_metrics.get("copilot_file_error") or metrics.get("copilot_file_error"))

    for raw_det in (
        report_or_metrics.get("copilot_file_error_detail"),
        metrics.get("copilot_file_error_detail"),
    ):
        s = str(raw_det or "").strip()
        if s and s not in details:
            details.append(s)

    for err in metrics.get("errors") or []:
        es = str(err or "").strip()
        if "CopilotFileReadError" in es:
            detected = True
            if es not in details:
                details.append(es)

    for ev in report_or_metrics.get("events_tail") or []:
        if not isinstance(ev, dict) or ev.get("message") != "SubTaskError":
            continue
        d = ev.get("details") if isinstance(ev.get("details"), dict) else {}
        what = str(d.get("what") or "").strip()
        if what == "CopilotFileReadError" or "CopilotFileReadError" in json.dumps(d, ensure_ascii=False):
            detected = True
            if what and what not in details:
                details.append(what)

    for line in report_or_metrics.get("log_tail") or []:
        ls = str(line or "").strip()
        if any(tok in ls for tok in _COPILOT_FILE_ERROR_LOG_TOKENS):
            detected = True
            cleaned = re.sub(r"^\[[^\]]+\](?:\[[^\]]+\])*\s*", "", ls)[:180]
            if cleaned and cleaned not in details:
                details.append(cleaned)

    return {
        "is_copilot_file_error": detected,
        "copilot_file": copilot_file,
        "detail": "; ".join(details[:3]),
    }


def is_copilot_file_error(report_or_metrics: Dict[str, Any]) -> bool:
    """Return True when the Copilot task failed due to file reading/parsing rather than combat."""
    return bool(extract_copilot_file_error_info(report_or_metrics)["is_copilot_file_error"])


def extract_formation_unavailable_info(report_or_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Extract structured BattleFormationTask / formation_select diagnostics with higher priority than BattleStartPre."""
    empty: Dict[str, Any] = {
        "is_formation_unavailable": False,
        "unavailable_opers": [],
        "oper_reasons": {},
        "formation_entered": False,
        "in_formation_select": False,
        "has_formation_error": False,
    }
    if not isinstance(report_or_metrics, dict):
        return empty
    if is_copilot_file_error(report_or_metrics):
        return empty
    metrics = report_or_metrics.get("metrics") if isinstance(report_or_metrics.get("metrics"), dict) else report_or_metrics
    if metrics.get("completed_flag") or metrics.get("failed_flag") or metrics.get("kills") is not None:
        return empty

    out_ops: List[str] = []
    out_reasons: Dict[str, str] = {}
    raw_reasons = metrics.get("unavailable_oper_reasons") if isinstance(metrics.get("unavailable_oper_reasons"), dict) else {}
    for op in metrics.get("unavailable_opers") or []:
        _record_unavailable_oper(out_ops, out_reasons, op, raw_reasons.get(op, "Unavailable"))
    for k, v in raw_reasons.items():
        _record_unavailable_oper(out_ops, out_reasons, k, v, prefer_reason=True)

    formation_entered = bool(metrics.get("formation_entered"))
    in_formation_select = bool(metrics.get("formation_select_entered") or report_or_metrics.get("in_formation_select"))
    has_formation_error = bool(metrics.get("formation_error") or metrics.get("formation_unavailable") or out_ops)

    prep_entry = report_or_metrics.get("prep_entry") if isinstance(report_or_metrics.get("prep_entry"), dict) else {}
    if prep_entry.get("outcome") == "formation" or prep_entry.get("entered") or prep_entry.get("exited_formation_select"):
        formation_entered = True
    if prep_entry.get("in_formation_select"):
        in_formation_select = True

    for err in metrics.get("errors") or []:
        f_ent, f_sel, f_err = _scan_text_for_formation_error(str(err), out_ops, out_reasons)
        formation_entered = formation_entered or f_ent
        in_formation_select = in_formation_select or f_sel
        has_formation_error = has_formation_error or f_err

    for ev in report_or_metrics.get("events_tail") or []:
        if not isinstance(ev, dict):
            continue
        m = ev.get("message", "")
        d = ev.get("details", {}) if isinstance(ev.get("details"), dict) else {}
        sub = str(d.get("subtask", "") or "")
        cls_name = str(d.get("class", "") or "")
        what = str(d.get("what", "") or "")
        det = d.get("details", {}) if isinstance(d.get("details"), dict) else {}
        tname = str(det.get("task", "") or "")
        if m in ("SubTaskStart", "SubTaskCompleted") and tname.startswith("BattleQuickFormation"):
            formation_entered = True
            if tname in _FORMATION_SELECT_TASKS or tname.startswith("BattleQuickFormationRole-"):
                in_formation_select = True
        if m == "SubTaskExtraInfo":
            if what in ("BattleFormation", "BattleFormationSelected", "BattleFormationOperUnavailable"):
                formation_entered = True
            if what in ("BattleFormationSelected", "BattleFormationOperUnavailable"):
                in_formation_select = True
            if what == "BattleFormationOperUnavailable":
                has_formation_error = True
                _extract_formation_from_json_obj(d, out_ops, out_reasons)
        if m == "SubTaskError" and (sub == "BattleFormationTask" or "BattleFormationTask" in cls_name):
            has_formation_error = True
            formation_entered = True
            _extract_formation_from_json_obj(d, out_ops, out_reasons)

    for line in report_or_metrics.get("log_tail") or []:
        f_ent, f_sel, f_err = _scan_text_for_formation_error(str(line), out_ops, out_reasons)
        formation_entered = formation_entered or f_ent
        in_formation_select = in_formation_select or f_sel
        has_formation_error = has_formation_error or f_err

    is_unavail = bool(has_formation_error or out_ops or in_formation_select)
    return {
        "is_formation_unavailable": is_unavail,
        "unavailable_opers": out_ops,
        "oper_reasons": out_reasons,
        "formation_entered": formation_entered,
        "in_formation_select": in_formation_select,
        "has_formation_error": has_formation_error,
    }


def is_formation_unavailable(report_or_metrics: Dict[str, Any]) -> bool:
    """Return True when Copilot failed in BattleFormationTask / formation_select (stage IS unlocked & entered)."""
    return bool(extract_formation_unavailable_info(report_or_metrics)["is_formation_unavailable"])


def is_stage_entry_failure(report_or_metrics: Dict[str, Any]) -> bool:
    """Return True when Copilot failed before entering battle (NEVER True when BattleFormationTask/formation_select failed)."""
    if not isinstance(report_or_metrics, dict):
        return False
    if is_copilot_file_error(report_or_metrics):
        return False
    metrics = report_or_metrics.get("metrics") if isinstance(report_or_metrics.get("metrics"), dict) else report_or_metrics
    if metrics.get("completed_flag") or metrics.get("failed_flag"):
        return False
    if metrics.get("kills") is not None:
        return False

    # Priority guard: BattleFormationTask error or entering formation_select PROVES the stage is unlocked & entered;
    # harmless leading BattleStartPre must be ignored and NEVER misclassified as stage_entry_failed.
    form_info = extract_formation_unavailable_info(report_or_metrics)
    if (
        form_info["is_formation_unavailable"]
        or form_info["has_formation_error"]
        or form_info["unavailable_opers"]
        or form_info["in_formation_select"]
    ):
        return False

    # If stage_pilot already verified & entered formation, BattleStartPre alone is harmless;
    # only BattleStartAll / FightBegin / StageTitleMismatch / StagePilotEntryFailed still counts as stage entry failure.
    effective_entry_tokens = (
        {"BattleStartAll", "FightBegin", "StageTitleMismatch", "StagePilotEntryFailed"}
        if form_info["formation_entered"]
        else set(_STAGE_ENTRY_ERROR_TASKS) | {"StageTitleMismatch", "StagePilotEntryFailed"}
    )

    if metrics.get("stage_entry_failed"):
        entry_tasks_m = [str(t) for t in (metrics.get("entry_failed_tasks") or [])]
        if not entry_tasks_m or any(t in effective_entry_tokens for t in entry_tasks_m):
            return True

    entry_tasks = [str(t) for t in (metrics.get("entry_failed_tasks") or [])]
    if any(t in effective_entry_tokens for t in entry_tasks):
        return True

    errors_text = "\n".join(str(e) for e in (metrics.get("errors") or []))
    if any(tok in errors_text for tok in effective_entry_tokens):
        return True

    for ev in report_or_metrics.get("events_tail") or []:
        if isinstance(ev, dict) and ev.get("message") == "SubTaskError":
            first_list = ev.get("details", {}).get("first")
            if isinstance(first_list, list) and any(str(x) in effective_entry_tokens for x in first_list):
                return True

    log_tail = "\n".join(str(l) for l in (report_or_metrics.get("log_tail") or []))
    if _STAGE_ENTRY_LOG_RE.search(log_tail):
        if any(tok in log_tail for tok in effective_entry_tokens):
            return True

    return False


def _relax_copilot_requirements(
    copilot: Dict[str, Any],
    target_opers: Iterable[str],
    my_box: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Relax non-essential skill_level/level/module/potentiality constraints for owned operators."""
    owned = my_box.get("all_owned", {}) or {}
    targets = {str(x).strip() for x in (target_opers or []) if str(x).strip()}
    patched = copy.deepcopy(copilot)
    relaxed_targets: List[str] = []

    def _relax_entry(op: Dict[str, Any]) -> None:
        if not isinstance(op, dict):
            return
        name = str(op.get("name") or "").strip()
        if not name:
            return
        sk = _safe_int(op.get("skill", 1)) or 1
        min_e = _min_elite_for_skill(sk)
        req = op.get("requirements") if isinstance(op.get("requirements"), dict) else {}
        is_target = name in targets
        owned_op = owned.get(name)

        has_strict = bool(
            (req.get("skill_level") is not None and (_safe_int(req.get("skill_level")) or 1) > 1)
            or (req.get("level") is not None and (_safe_int(req.get("level")) or 1) > 1)
            or (req.get("module") is not None and (_safe_int(req.get("module")) or -1) != -1)
            or (req.get("potentiality") is not None and (_safe_int(req.get("potentiality")) or 1) > 1)
            or (req.get("elite") is not None and (_safe_int(req.get("elite")) or 0) > min_e)
        )
        if is_target:
            if not owned_op or int(owned_op.get("elite", 0) or 0) < min_e:
                return
            if has_strict and name not in relaxed_targets:
                relaxed_targets.append(name)
            op["requirements"] = {
                "elite": min_e,
                "level": 1,
                "skill_level": 1,
                "module": -1,
                "potentiality": 1,
            }
        elif req:
            # Also strip non-essential skill_level/module/potentiality across squad so another op won't trip next
            req_copy = dict(req)
            req_copy["skill_level"] = 1
            req_copy["module"] = -1
            req_copy["potentiality"] = 1
            if owned_op and int(owned_op.get("elite", 0) or 0) >= min_e:
                req_copy["elite"] = min(int(req_copy.get("elite", min_e) or min_e), int(owned_op.get("elite", 0) or 0))
            op["requirements"] = req_copy

    for op in patched.get("opers") or []:
        _relax_entry(op)
    for grp in patched.get("groups") or []:
        if isinstance(grp, dict):
            for op in grp.get("opers") or []:
                _relax_entry(op)

    return patched, relaxed_targets


def select_next_candidate_guide(
    candidates: List[Dict[str, Any]],
    tried_indices: Optional[Iterable[int]],
    blacklist: Optional[Iterable[str]],
    my_box: Dict[str, Any],
    maa_dir: Optional[Path] = None,
    stage_id: str = "",
    raw_guides_by_id: Optional[Dict[Any, Dict[str, Any]]] = None,
    require_native_full_match: bool = False,
) -> Optional[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
    """Select the best untried PRTS candidate guide, preferring full-Box-match 3~6 op guides free of blacklisted ops."""
    tried = set(tried_indices or [])
    bl_set: Set[str] = {str(x).strip() for x in (blacklist or []) if str(x).strip()}
    ranked: List[Tuple[Tuple[int, int, int, int], int, Dict[str, Any], Dict[str, Any]]] = []

    for idx, cand in enumerate(candidates):
        if idx in tried or not isinstance(cand, dict):
            continue
        gid = cand.get("guide_id")
        raw_guide = (raw_guides_by_id or {}).get(gid)
        if raw_guide is not None:
            adapted = _adapt_guide_deterministic(
                raw_guide,
                my_box,
                stage_id or str(cand.get("copilot", {}).get("stage_name", "")),
                maa_dir=maa_dir,
                blacklist=bl_set,
            )
            if adapted.get("missing_ops"):
                continue
            final_ops = set(_collect_copilot_oper_names(adapted["copilot"]))
            if final_ops & bl_set:
                continue
            avoids_natively = 1 if not adapted.get("blacklisted_hits") else 0
            is_native_full = 1 if adapted.get("full_native_match") else 0
            if require_native_full_match and not is_native_full:
                continue
            score = int(adapted.get("compat_score", 0) or 0)
            ranked.append((
                (is_native_full, avoids_natively, score, -idx),
                idx,
                adapted["copilot"],
                {
                    "guide_id": gid,
                    "full_native_match": bool(is_native_full),
                    "avoids_blacklist_natively": bool(avoids_natively),
                    "substitutions": dict(adapted.get("substitutions") or {}),
                    "compat_score": score,
                },
            ))
            continue

        cop = cand.get("copilot")
        if not isinstance(cop, dict):
            continue
        orig_ops = set(cand.get("original_opers") or _collect_copilot_oper_names(cop))
        cur_ops = set(_collect_copilot_oper_names(cop))
        bl_hits = sorted(cur_ops & bl_set)
        base_score = int(cand.get("compat_score", int(cand.get("like", 0) or 0) * 25) or 0)

        if not bl_hits:
            if cand.get("missing_ops"):
                continue
            avoids_natively = 1 if not (orig_ops & bl_set) else 0
            is_native_full = 1 if (avoids_natively and not cand.get("substitutions") and not cand.get("missing_ops")) else 0
            if require_native_full_match and not is_native_full:
                continue
            ranked.append((
                (is_native_full, avoids_natively, base_score, -idx),
                idx,
                copy.deepcopy(cop),
                {
                    "guide_id": gid,
                    "full_native_match": bool(is_native_full),
                    "avoids_blacklist_natively": bool(avoids_natively),
                    "substitutions": dict(cand.get("substitutions") or {}),
                    "compat_score": base_score,
                },
            ))
        else:
            if require_native_full_match:
                continue
            sub_res = substitute_unavailable_operators_in_copilot(
                cop,
                bl_hits,
                my_box,
                blacklist=bl_set,
                maa_dir=maa_dir,
            )
            if not sub_res.get("ok"):
                continue
            final_ops = set(_collect_copilot_oper_names(sub_res["copilot"]))
            if final_ops & bl_set:
                continue
            adj_score = base_score - len(bl_hits) * 2000 - len(sub_res.get("substitutions") or {}) * 350
            ranked.append((
                (0, 0, adj_score, -idx),
                idx,
                sub_res["copilot"],
                {
                    "guide_id": gid,
                    "full_native_match": False,
                    "avoids_blacklist_natively": False,
                    "substitutions": dict(sub_res.get("substitutions") or {}),
                    "compat_score": adj_score,
                },
            ))

    if not ranked:
        return None
    ranked.sort(key=lambda x: x[0], reverse=True)
    _, best_idx, best_cop, best_meta = ranked[0]
    return best_idx, best_cop, best_meta


def _repair_deterministic(
    candidates: List[Dict[str, Any]],
    cand_idx: int,
    cur_copilot: Dict[str, Any],
    report: Dict[str, Any],
    my_box: Dict[str, Any],
    round_idx: int,
    oper_blacklist: Optional[Iterable[str]] = None,
    tried_indices: Optional[Iterable[int]] = None,
    same_guide_sub_attempts: int = 0,
    same_guide_relax_attempts: int = 0,
    maa_dir: Optional[Path] = None,
    raw_guides_by_id: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    metrics = report.get("metrics", {})
    errors = metrics.get("errors", [])
    log_tail = "\n".join(report.get("log_tail", []))

    repaired = copy.deepcopy(cur_copilot)
    owned = my_box.get("all_owned", {})
    initial_bl_list: List[str] = []
    for op_b in (oper_blacklist or []):
        s = str(op_b or "").strip()
        if s and s not in initial_bl_list:
            initial_bl_list.append(s)
    bl_list: List[str] = list(initial_bl_list)
    tried_set: Set[int] = set(tried_indices or [])
    tried_set.add(cand_idx)

    strategy: Dict[str, Any] = {
        "deployment_with_pause": "1",
        "ignore_requirements": False,
        "reason": "",
        "mode": "pure_maa",
    }

    # Priority 0: CopilotFileReadError is a JSON/file read/parse failure, NEVER a battle defeat.
    # Immediately switch to the next valid PRTS guide and never retry the same broken file.
    cf_info = extract_copilot_file_error_info(report)
    if cf_info["is_copilot_file_error"]:
        detail_txt = cf_info["detail"] or "CopilotFileReadError"
        strategy["copilot_file_error"] = True
        strategy["copilot_file_error_detail"] = detail_txt
        strategy["battle_failed"] = False
        strategy["skip_agent_collab"] = True
        scan_tried: Set[int] = set(tried_set)
        stage_id_hint = str(repaired.get("stage_name") or "")
        while True:
            alt_cf = select_next_candidate_guide(
                candidates,
                tried_indices=scan_tried,
                blacklist=bl_list,
                my_box=my_box,
                maa_dir=maa_dir,
                stage_id=stage_id_hint,
                raw_guides_by_id=raw_guides_by_id,
                require_native_full_match=False,
            )
            if alt_cf is None:
                break
            new_idx, new_cop, alt_meta = alt_cf
            scan_tried.add(new_idx)
            sanitized_cop = sanitize_copilot_requirements(new_cop, maa_dir=maa_dir)
            ok_schema, _ = validate_copilot_payload(sanitized_cop, maa_dir=maa_dir)
            if not ok_schema:
                continue
            strategy["mode"] = "copilot_file_error_switch_guide"
            strategy["repair_action"] = "switch_guide"
            strategy["switched_guide_id"] = alt_meta.get("guide_id")
            strategy["reason"] = (
                f"作业文件解析失败 (CopilotFileReadError: {detail_txt})，"
                f"已自动切换至下一份合法 PRTS 备选作业 #{alt_meta.get('guide_id')} 重试"
            )
            return new_idx, sanitized_cop, strategy

        strategy["mode"] = "copilot_file_error"
        strategy["repair_action"] = "exhausted"
        strategy["abort_retry"] = True
        strategy["reason"] = f"CopilotFileReadError ({detail_txt})，所有合法 PRTS 备选作业均已耗尽，禁止拿同一坏文件反复重试"
        return cand_idx, repaired, strategy

    # Priority 1: BattleFormationTask / formation_select failure MUST be classified as formation_unavailable
    # (NEVER stage_entry_failed) and follow strict self-healing priority:
    #   (1) Relax non-essential skill_level/level/module requirements if purely requirement-limited
    #   (2) Switch to an untried full-Box-match PRTS guide (3~6 op conventional preferred)
    #   (3) Strictly same-branch/same-block substitute on current guide (never 0-block bearer for blocking vanguard)
    #   (4) Switch to untried PRTS guide with strict same-branch substitute
    #   (5) Exhausted -> external Agent collab
    form_info = extract_formation_unavailable_info(report)
    if form_info["is_formation_unavailable"]:
        unavail_ops = list(form_info["unavailable_opers"])
        op_reasons = dict(form_info["oper_reasons"])
        strategy["formation_unavailable"] = True
        strategy["unavailable_opers"] = unavail_ops
        strategy["unavailable_oper_reasons"] = op_reasons
        strategy["deployment_with_pause"] = "1"
        strategy["ignore_requirements"] = False

        # Step 1: Check whether failure is solely due to relaxable requirements (e.g. skill_level 6 < require 7)
        all_relaxable = bool(unavail_ops) and all(
            str(op_reasons.get(op_u) or "").strip().lower() in _RELAXABLE_REQ_REASONS
            and op_u in owned
            for op_u in unavail_ops
        )
        if same_guide_relax_attempts == 0 and all_relaxable:
            relaxed_cop, relaxed_ops = _relax_copilot_requirements(repaired, unavail_ops, my_box)
            if relaxed_ops:
                detail_str = ", ".join(f"{u}:{op_reasons.get(u, 'skill_level')}" for u in unavail_ops)
                strategy["mode"] = "formation_relax_requirements"
                strategy["repair_action"] = "relax_requirements"
                strategy["ignore_requirements"] = True
                strategy["deployment_with_pause"] = "1"
                strategy["blacklist_deferred"] = True
                strategy["relaxed_opers"] = relaxed_ops
                strategy["oper_blacklist"] = list(initial_bl_list)
                strategy["reason"] = (
                    f"编队干员仅因非必要练度/技能等级限制报错 ({detail_str})，"
                    f"先放宽练度校验并开启 ignore_requirements 与暂停部署重试原作业"
                )
                return cand_idx, relaxed_cop, strategy

        # Permanently blacklist unavailable ops for subsequent guide switching / substitution
        for op_u in unavail_ops:
            if op_u not in bl_list:
                bl_list.append(op_u)
        strategy["oper_blacklist"] = list(bl_list)
        stage_id_hint = str(repaired.get("stage_name") or "")

        # Step 2: Prefer switching to a PRTS guide natively 100% matched by user's Box (zero substitutions)
        alt_full = select_next_candidate_guide(
            candidates,
            tried_indices=tried_set,
            blacklist=bl_list,
            my_box=my_box,
            maa_dir=maa_dir,
            stage_id=stage_id_hint,
            raw_guides_by_id=raw_guides_by_id,
            require_native_full_match=True,
        )
        if alt_full is not None:
            new_idx, new_cop, alt_meta = alt_full
            strategy["mode"] = "formation_switch_guide"
            strategy["repair_action"] = "switch_guide"
            strategy["switched_guide_id"] = alt_meta.get("guide_id")
            strategy["full_native_match"] = True
            strategy["avoids_blacklist_natively"] = True
            strategy["substitutions"] = {}
            strategy["deployment_with_pause"] = "1"
            strategy["reason"] = (
                f"编队干员不可用 ({', '.join(unavail_ops)})，优先切换至 Box 全匹配的高赞 PRTS 作业 "
                f"#{alt_meta.get('guide_id')}（免替补，黑名单={bl_list}）"
            )
            return new_idx, new_cop, strategy

        # Step 3: Strictly same-branch/same-block substitute in-place on current guide (with pause deployment!)
        if same_guide_sub_attempts == 0 and unavail_ops:
            sub_res = substitute_unavailable_operators_in_copilot(
                repaired,
                unavail_ops,
                my_box,
                blacklist=bl_list,
                maa_dir=maa_dir,
            )
            if sub_res.get("ok"):
                strategy["mode"] = "formation_substitute"
                strategy["repair_action"] = "substitute_operator"
                strategy["substitutions"] = dict(sub_res["substitutions"])
                strategy["deployment_with_pause"] = "1"
                sub_desc = ", ".join(f"{k}->{v}" for k, v in sub_res["substitutions"].items())
                strategy["reason"] = (
                    f"无全匹配备选作业，已将不可用干员 ({', '.join(unavail_ops)}) 按同分支同阻挡定位"
                    f"替补为 [{sub_desc}]（同步改写编队与动作并开启暂停部署）"
                )
                return cand_idx, sub_res["copilot"], strategy

        # Step 4: Switch to next PRTS guide allowing strict same-branch substitution
        alt = select_next_candidate_guide(
            candidates,
            tried_indices=tried_set,
            blacklist=bl_list,
            my_box=my_box,
            maa_dir=maa_dir,
            stage_id=stage_id_hint,
            raw_guides_by_id=raw_guides_by_id,
            require_native_full_match=False,
        )
        if alt is not None:
            new_idx, new_cop, alt_meta = alt
            strategy["mode"] = "formation_switch_guide"
            strategy["repair_action"] = "switch_guide"
            strategy["switched_guide_id"] = alt_meta.get("guide_id")
            strategy["full_native_match"] = bool(alt_meta.get("full_native_match"))
            strategy["avoids_blacklist_natively"] = bool(alt_meta.get("avoids_blacklist_natively"))
            strategy["substitutions"] = dict(alt_meta.get("substitutions") or {})
            strategy["deployment_with_pause"] = "1"
            avoid_note = "原生不含黑名单干员" if alt_meta.get("avoids_blacklist_natively") else f"已按同分支替补 {alt_meta.get('substitutions')}"
            strategy["reason"] = (
                f"同份作业替补不可行，切换至备选 PRTS 作业 #{alt_meta.get('guide_id')}（{avoid_note}，黑名单={bl_list}）"
            )
            return new_idx, new_cop, strategy

        # Step 5: Fallback secondary same-branch substitute on current guide if no other guide exists
        if same_guide_sub_attempts > 0 and unavail_ops:
            sub_res2 = substitute_unavailable_operators_in_copilot(
                repaired,
                unavail_ops,
                my_box,
                blacklist=bl_list,
                maa_dir=maa_dir,
            )
            if sub_res2.get("ok"):
                strategy["mode"] = "formation_substitute"
                strategy["repair_action"] = "substitute_operator"
                strategy["substitutions"] = dict(sub_res2["substitutions"])
                strategy["deployment_with_pause"] = "1"
                sub_desc = ", ".join(f"{k}->{v}" for k, v in sub_res2["substitutions"].items())
                strategy["reason"] = f"无更多备选作业，继续按同分支替补不可用干员 [{sub_desc}] 重试"
                return cand_idx, sub_res2["copilot"], strategy

        # Step 6: All substitutes and alternative guides exhausted -> request external Agent
        strategy["mode"] = "formation_unavailable"
        strategy["repair_action"] = "exhausted"
        strategy["formation_exhausted"] = True
        strategy["abort_retry"] = True
        strategy["reason"] = f"所有同分支替补与 PRTS 备选作业均不可行（不可用干员黑名单: {bl_list}），需外部 Agent 介入"
        return cand_idx, repaired, strategy

    # Priority 2: Strictly distinguish stage-entry/unlock failure from battle failure before any rule retry.
    if is_stage_entry_failure(report):
        strategy["deployment_with_pause"] = "0"
        strategy["ignore_requirements"] = False
        strategy["mode"] = "stage_entry_failed"
        strategy["abort_retry"] = True
        entry_tasks_r = [str(t) for t in (metrics.get("entry_failed_tasks") or [])]
        if any(t in ("StagePilotEntryFailed", "StageTitleMismatch") for t in entry_tasks_r):
            real_reason = str(report.get("error") or (metrics.get("errors") or ["stage_pilot 进图前拦截"])[0])
            strategy["reason"] = f"进图前拦截（尚未启动 Copilot）：{real_reason}，严禁修轴或原地重试"
        else:
            strategy["reason"] = "关卡未解锁或进图失败（BattleStartPre/BattleStartAll 且 kills=None），严禁修轴或原地重试"
        return cand_idx, repaired, strategy

    # Priority 2.5: Non-3-star leak (stars < 3) -> prefer switching to next high-like PRTS guide, else pause-deploy & skill timing fix
    verdict_info = report.get("dual_victory_check") if isinstance(report.get("dual_victory_check"), dict) else {}
    stars_val = verdict_info.get("settlement_stars") if verdict_info.get("settlement_stars") is not None else metrics.get("settlement_stars")
    is_non_3 = bool(report.get("non_3_star") or verdict_info.get("non_3_star") or metrics.get("non_3_star") or (stars_val is not None and int(stars_val) < 3))
    if is_non_3:
        alt_3 = select_next_candidate_guide(
            candidates,
            tried_indices=tried_set,
            blacklist=bl_list,
            my_box=my_box,
            maa_dir=maa_dir,
            stage_id=str(repaired.get("stage_name") or ""),
            raw_guides_by_id=raw_guides_by_id,
        )
        if alt_3 is not None:
            cand_idx, repaired, alt_meta = alt_3
            strategy["mode"] = "non_3_star_switch_guide"
            strategy["repair_action"] = "switch_guide"
            strategy["deployment_with_pause"] = "1"
            strategy["reason"] = (
                f"上轮仅打出 {stars_val or '?'} 星（漏怪未满 3 星），自动切换至备选高赞 PRTS 作业 "
                f"#{alt_meta.get('guide_id')} 重打争取 3 星"
            )
            return cand_idx, repaired, strategy
        for op in repaired.get("opers", []):
            if op.get("skill_usage") == 0:
                def_sk, def_usg = DEFAULT_SKILL_MAP.get(str(op.get("name") or ""), (None, 1))
                if not (def_sk == op.get("skill") and def_usg == 0):
                    op["skill_usage"] = 1
        repaired["actions"] = [
            act for i, act in enumerate(repaired.get("actions", []))
            if not (act.get("type") == "SpeedUp" and i <= 3)
        ]
        for act in repaired.get("actions", []):
            if isinstance(act.get("post_delay"), int):
                act["post_delay"] = max(50, act["post_delay"] - 50)
        strategy["mode"] = "non_3_star_repair_timing"
        strategy["deployment_with_pause"] = "1"
        strategy["reason"] = f"上轮仅打出 {stars_val or '?'} 星且无更多备选作业，已启用暂停部署、取消开局倍速并微调技能/延迟重打"
        return cand_idx, repaired, strategy

    errors_only = " | ".join(errors)
    has_formation_err = (
        any(k in errors_only for k in ("BattleFormation", "NotEnoughOper", "Formation", "Requirement", "Unable to find"))
        or any(k in log_tail for k in ("NotEnoughOper", "Unable to find", "FormationError", "Requirement not met"))
    )
    if has_formation_err:
        for op in repaired.get("opers", []):
            n = op.get("name", "")
            if n in owned:
                op["requirements"] = _safe_substitute_requirements(owned[n], op.get("skill", 1))
        strategy["ignore_requirements"] = True
        strategy["reason"] = "编队或练度匹配异常，已下调需求并开启 ignore_requirements"
        if round_idx >= 2:
            alt = select_next_candidate_guide(
                candidates,
                tried_indices=tried_set,
                blacklist=bl_list,
                my_box=my_box,
                maa_dir=maa_dir,
                stage_id=str(repaired.get("stage_name") or ""),
                raw_guides_by_id=raw_guides_by_id,
            )
            if alt is not None:
                cand_idx, repaired, alt_meta = alt
                strategy["reason"] += f"，并切换至候选作业 #{alt_meta.get('guide_id')}"
        return cand_idx, repaired, strategy

    if metrics.get("kills") is None and metrics.get("costs") is None and not metrics.get("failed_flag"):
        alt = select_next_candidate_guide(
            candidates,
            tried_indices=tried_set,
            blacklist=bl_list,
            my_box=my_box,
            maa_dir=maa_dir,
            stage_id=str(repaired.get("stage_name") or ""),
            raw_guides_by_id=raw_guides_by_id,
        )
        if alt is not None:
            cand_idx, repaired, alt_meta = alt
            strategy["reason"] = f"未能正常进入战斗流程，切换至候选作业 #{alt_meta.get('guide_id')}"
        else:
            strategy["ignore_requirements"] = True
            strategy["reason"] = "未能进入战斗流程，尝试放宽要求重跑"
        return cand_idx, repaired, strategy

    last_acts = " | ".join(metrics.get("last_3_actions", []))
    if "BulletTime" in last_acts and not metrics.get("failed_flag"):
        for act in repaired.get("actions", []):
            if act.get("type") == "BulletTime" and isinstance(act.get("costs"), int) and act["costs"] > 5:
                act["costs"] = max(3, act["costs"] - 3)
        strategy["reason"] = "检测到 BulletTime 等费/条件卡顿，已下调触发费用阈值并启用暂停部署"
        return cand_idx, repaired, strategy

    if round_idx == 1:
        for op in repaired.get("opers", []):
            if op.get("skill_usage") == 0:
                def_sk, def_usg = DEFAULT_SKILL_MAP.get(str(op.get("name") or ""), (None, 1))
                if not (def_sk == op.get("skill") and def_usg == 0):
                    op["skill_usage"] = 1
        new_actions = []
        for act in repaired.get("actions", []):
            if act.get("type") == "SpeedUp" and len(new_actions) <= 3:
                continue
            new_actions.append(act)
        repaired["actions"] = new_actions
        strategy["deployment_with_pause"] = "1"
        strategy["reason"] = "战斗失败，已启用暂停下干员、移除早期二倍速并将非被动干员调整为自动开技能"
        return cand_idx, repaired, strategy

    alt = select_next_candidate_guide(
        candidates,
        tried_indices=tried_set,
        blacklist=bl_list,
        my_box=my_box,
        maa_dir=maa_dir,
        stage_id=str(repaired.get("stage_name") or ""),
        raw_guides_by_id=raw_guides_by_id,
    )
    if alt is not None:
        cand_idx, repaired, alt_meta = alt
        strategy["deployment_with_pause"] = "1"
        strategy["reason"] = f"第 {round_idx} 轮仍失败，自动切换至下一高赞兼容作业 #{alt_meta.get('guide_id')}"
    else:
        for act in repaired.get("actions", []):
            if isinstance(act.get("post_delay"), int):
                act["post_delay"] = max(50, act["post_delay"] - 50)
        strategy["ignore_requirements"] = True
        strategy["reason"] = "已尝试所有候选作业，微调延迟并放宽要求重试"

    return cand_idx, repaired, strategy


def _apply_ai_repair_patch(
    candidates: List[Dict[str, Any]],
    cand_idx: int,
    cur_copilot: Dict[str, Any],
    ai_reply: Dict[str, Any],
) -> Optional[Tuple[int, Dict[str, Any], Dict[str, Any]]]:
    reason = ai_reply.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None

    new_idx = cand_idx
    if "switch_candidate_index" in ai_reply and ai_reply["switch_candidate_index"] is not None:
        req_idx = _safe_int(ai_reply["switch_candidate_index"])
        if req_idx is None or not (0 <= req_idx < len(candidates)):
            return None
        new_idx = req_idx

    patched = copy.deepcopy(candidates[new_idx]["copilot"] if new_idx != cand_idx else cur_copilot)

    sk_overrides = ai_reply.get("skill_usage_overrides", {})
    if sk_overrides is not None:
        if not isinstance(sk_overrides, dict):
            return None
        for op in patched.get("opers", []):
            name = op.get("name", "")
            if name in sk_overrides:
                val = _safe_int(sk_overrides[name])
                if val is None or val not in (0, 1, 2, 3):
                    return None
                op["skill_usage"] = val

    act_patches = ai_reply.get("action_patches", [])
    if act_patches is not None:
        if not isinstance(act_patches, list):
            return None
        actions = patched.get("actions", [])
        for patch_item in act_patches:
            if not isinstance(patch_item, dict) or "index" not in patch_item:
                return None
            idx = _safe_int(patch_item.get("index"))
            if idx is None or not (0 <= idx < len(actions)):
                return None
            if "costs" in patch_item:
                c_val = _safe_int(patch_item.get("costs"))
                if c_val is None:
                    return None
                actions[idx]["costs"] = max(0, c_val)
            if "post_delay" in patch_item:
                d_val = _safe_int(patch_item.get("post_delay"))
                if d_val is None:
                    return None
                actions[idx]["post_delay"] = max(0, d_val)

    dwp = str(ai_reply.get("deployment_with_pause", "1"))
    if dwp not in ("0", "1"):
        return None
    ign = bool(ai_reply.get("ignore_requirements", False))

    return new_idx, patched, {
        "deployment_with_pause": dwp,
        "ignore_requirements": ign,
        "reason": reason.strip(),
        "mode": "ai_assisted",
    }


def repair_copilot(
    candidates: List[Dict[str, Any]],
    cand_idx: int,
    cur_copilot: Dict[str, Any],
    report: Dict[str, Any],
    my_box: Dict[str, Any],
    round_idx: int,
    client: Optional[OpenAICompatClient] = None,
    oper_blacklist: Optional[Iterable[str]] = None,
    tried_indices: Optional[Iterable[int]] = None,
    same_guide_sub_attempts: int = 0,
    same_guide_relax_attempts: int = 0,
    maa_dir: Optional[Path] = None,
    raw_guides_by_id: Optional[Dict[Any, Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any], Dict[str, Any]]:
    """Diagnose copilot failure and generate next-round patch; falls back cleanly to rule-based repair."""
    base_idx, base_copilot, base_strat = _repair_deterministic(
        candidates,
        cand_idx,
        cur_copilot,
        report,
        my_box,
        round_idx,
        oper_blacklist=oper_blacklist,
        tried_indices=tried_indices,
        same_guide_sub_attempts=same_guide_sub_attempts,
        same_guide_relax_attempts=same_guide_relax_attempts,
        maa_dir=maa_dir,
        raw_guides_by_id=raw_guides_by_id,
    )
    if (
        base_strat.get("abort_retry")
        or base_strat.get("copilot_file_error")
        or base_strat.get("formation_unavailable")
        or client is None
        or not client.is_configured()
    ):
        return base_idx, base_copilot, base_strat

    sys_prompt = (
        "你是明日方舟 MAA Copilot 修轴专家。根据战报日志与作业 JSON，输出合法 JSON 修复补丁："
        '{"deployment_with_pause":"1","ignore_requirements":false,"switch_candidate_index":null,'
        '"skill_usage_overrides":{"干员名":1},"action_patches":[{"index":0,"costs":8,"post_delay":200}],"reason":"..."}'
    )
    usr_prompt = json.dumps({
        "round_idx": round_idx,
        "cand_idx": cand_idx,
        "total_candidates": len(candidates),
        "non_3_star": bool(report.get("non_3_star") or (report.get("dual_victory_check") or {}).get("non_3_star")),
        "settlement_stars": (report.get("dual_victory_check") or {}).get("settlement_stars") or report.get("metrics", {}).get("settlement_stars"),
        "metrics": report.get("metrics", {}),
        "log_tail": report.get("log_tail", [])[-40:],
        "opers": cur_copilot.get("opers", []),
        "actions_head": cur_copilot.get("actions", [])[:30],
    }, ensure_ascii=False)

    ai_reply = client.chat_json(sys_prompt, usr_prompt)
    if ai_reply is not None:
        applied = _apply_ai_repair_patch(candidates, cand_idx, cur_copilot, ai_reply)
        if applied is not None:
            return applied

    base_strat["mode"] = "pure_maa_fallback"
    return base_idx, base_copilot, base_strat
