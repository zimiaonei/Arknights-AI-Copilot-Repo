"""Frozen UI <-> Python Bridge Contract, Shared Options & Pure Helpers.

This module defines the immutable interface contract shared across:
- `universal_engine/gui/web/` (Frontend M3 HTML/CSS/JS)
- `universal_engine/gui/controller.py` & `bridge.py` & `app.py` (Backend Bridge)
- `universal_engine/tests/` (Headless GUI & Collab test suite)

Frozen Bridge API Methods (`FROZEN_BRIDGE_METHODS`):
  1.  get_initial_state() -> dict
  2.  refresh_snapshot(check_adb: bool = False) -> dict
  3.  start_daily() -> dict
  4.  start_auto_growth() -> dict
  5.  preview_growth_plan() -> dict
  6.  start_annihilation() -> dict
  7.  start_push(stages_raw: str = "") -> dict
  8.  start_sync_box() -> dict
  9.  set_roguelike_mode(mode_label: str) -> dict
  10. start_roguelike(mode_label: str = "") -> dict
  11. start_roguelike_post_settlement() -> dict
  12. toggle_reclamation() -> dict
  13. stop_current_task() -> dict
  14. set_server(server: str) -> dict
  15. set_post_action(post_action: str) -> dict
  16. probe_or_launch_emulator() -> dict
  17. check_maa_update() -> dict
  18. confirm_maa_update(release_info: dict, local_ver: str, remote_ver: str) -> dict
  19. cancel_maa_update() -> dict
  20. get_agent_prompts() -> dict
  21. copy_to_clipboard(text: str) -> dict
  22. open_path(target: str) -> dict
  23. resume_with_rules() -> dict
  24. resume_from_tutorial() -> dict
  25. get_settings() -> dict
  26. save_settings(payload: dict) -> dict
  27. sync_exploration_from_maa_gui(maa_path: str = "") -> dict
  28. test_webhook(webhook_url: str) -> dict
  29. test_ai_api(base_url: str, api_key: str, model_name: str) -> dict
  30. browse_maa_folder() -> dict
  31. wizard_scan_maa() -> dict
  32. wizard_bind_maa(folder: str) -> dict
  33. wizard_start_download_maa() -> dict
  34. wizard_cancel_download_maa() -> dict
  35. wizard_run_diagnostics(emu_type: str, adb_address: str) -> dict
  36. wizard_launch_emulator(emu_type: str, adb_address: str) -> dict
  37. wizard_finish(payload: dict) -> dict
  38. get_logs() -> dict
  39. clear_logs() -> dict
  40. report_smoke_metrics(metrics: dict) -> dict

Frozen Push Event Types (`window.__prtsPushBatch([{type, payload}, ...])`):
  - `snapshot`: {
      config, maa_dir,
      maa: {valid, version, reason},
      adb: {connected, adb_address, width, height, is_16_9, mumu_adbd_hung, warning},
      box: {total_owned, e2_count},
      depot: {syncTime, count},
      ai: {mode, model_name, is_builtin_api, label},
      growth: {ok, recommended_stages, first_stage, reason},
      annihilation: {week_key, process, done, label},
      server, post_action,
      quick_summary: {fight_mode, auto_push, text},
      roguelike: {
        theme, theme_display, mode_label, target_display,
        ai_copilot_enabled, card_mode, auto_claim_rewards,
        auto_upgrade_tech_tree, sub_hint
      },
      reclamation: {
        theme, theme_display, mode_label, mode_display,
        badge_text, clear_store, loaded_from_gui_config, sub_hint
      }
    }
  - `adb_status`: {connected, adb_address, width, height, is_16_9, mumu_adbd_hung, warning}
  - `task_state`: {
      running, task_kind, status_text, progress_mode, progress_value,
      progress_cur, progress_total, level, ra_button_text, ra_button_enabled, stop_enabled
    }
  - `log_batch`: [{id, ts, text, tag}, ...]
  - `agent_banner`: {visible, task_type, title, subtitle, task_info}
  - `tutorial_banner`: {visible, stage_code, next_stage, title, subtitle}
  - `toast`: {level, title, message}
  - `maa_update_check_done`: {ok, has_update, local_version, remote_version, total_size_mb, release_info, error}
  - `maa_update_progress`: {done, total, msg, ratio}
  - `maa_update_apply_done`: {ok, new_version, updated_files, skipped_preserved, restart_recommended, error}
  - `wizard_maa_scan`: {valid, maa_dir, maa_version, reason}
  - `wizard_dl_progress`: {done, total, msg, ratio}
  - `wizard_dl_finished`: {ok, version, integrity, error}
  - `wizard_diag_done`: {write_permission, maa_integrity, adb_resolution, base_dir}
  - `wizard_launch_done`: {ok, error, diag}
  - `open_wizard`: {}
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

if getattr(sys, "frozen", False):
    from universal_engine import roguelike_ai_copilot
else:
    from .. import roguelike_ai_copilot

BRIDGE_API_VERSION = "1.0"

FROZEN_BRIDGE_METHODS: Tuple[str, ...] = (
    "get_initial_state",
    "refresh_snapshot",
    "start_daily",
    "start_auto_growth",
    "preview_growth_plan",
    "start_annihilation",
    "start_push",
    "start_sync_box",
    "set_roguelike_mode",
    "start_roguelike",
    "start_roguelike_post_settlement",
    "toggle_reclamation",
    "stop_current_task",
    "set_server",
    "set_post_action",
    "probe_or_launch_emulator",
    "check_maa_update",
    "confirm_maa_update",
    "cancel_maa_update",
    "get_agent_prompts",
    "copy_to_clipboard",
    "open_path",
    "resume_with_rules",
    "resume_from_tutorial",
    "get_settings",
    "save_settings",
    "sync_exploration_from_maa_gui",
    "test_webhook",
    "test_ai_api",
    "browse_maa_folder",
    "wizard_scan_maa",
    "wizard_bind_maa",
    "wizard_start_download_maa",
    "wizard_cancel_download_maa",
    "wizard_run_diagnostics",
    "wizard_launch_emulator",
    "wizard_finish",
    "get_logs",
    "clear_logs",
    "report_smoke_metrics",
)

FROZEN_PUSH_EVENTS: Tuple[str, ...] = (
    "snapshot",
    "adb_status",
    "task_state",
    "log_batch",
    "agent_banner",
    "tutorial_banner",
    "toast",
    "maa_update_check_done",
    "maa_update_progress",
    "maa_update_apply_done",
    "wizard_maa_scan",
    "wizard_dl_progress",
    "wizard_dl_finished",
    "wizard_diag_done",
    "wizard_launch_done",
    "open_wizard",
)

RL_CARD_MODE_PURE = "纯 MAA 自动刷取"
RL_CARD_MODE_AI = "AI 辅助策略通关"
RL_CARD_MODES: Tuple[str, str] = (RL_CARD_MODE_PURE, RL_CARD_MODE_AI)

RL_THEME_OPTIONS: List[Tuple[str, str]] = [
    ("跟随原版 MAA (默认黑流树海)", ""),
    ("沉沦者的黑流树海 (BlackFlow)", "BlackFlow"),
    ("岁的界园志异 (JieGarden)", "JieGarden"),
    ("萨卡兹的无终奇语 (Sarkaz)", "Sarkaz"),
    ("探索者的银凇止境 (Sami)", "Sami"),
    ("水月与深蓝之树 (Mizuki)", "Mizuki"),
    ("傀影与猩红孤钻 (Phantom)", "Phantom"),
]

RL_TARGET_OPTIONS: List[Tuple[str, str]] = [
    ("跟随原版 MAA (默认刷等级)", ""),
    ("刷等级 / 经验 (Exp)", "Exp"),
    ("刷源石锭投资 (Investment)", "Investment"),
    ("刷藏品 / 烧水 (Collectible)", "Collectible"),
    ("黑流生态养成 (BlackFlowCultivation)", "BlackFlowCultivation"),
    ("月度小队 (MonthlySquad)", "MonthlySquad"),
    ("深入调查 (DeepExploration)", "DeepExploration"),
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

EMULATOR_TYPE_OPTIONS: List[str] = ["auto", "mumu12", "ldplayer", "bluestacks", "nox"]

SKIP_TASK_OPTIONS: List[Tuple[str, str]] = [
    ("startup", "唤醒"),
    ("recruit", "公招"),
    ("infrast", "基建"),
    ("mall", "信用商店"),
    ("annihilation", "剿灭"),
    ("fight", "刷理智"),
    ("award", "领奖"),
]

API_PRESETS: List[Dict[str, str]] = [
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model_name": "deepseek-chat"},
    {"name": "Gemini (OpenAI兼容)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model_name": "gemini-2.5-flash"},
    {"name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1", "model_name": "qwen2.5:14b"},
    {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model_name": "gpt-4o-mini"},
]

LOG_CATEGORIES: List[Tuple[str, str]] = [
    ("all", "全部"),
    ("growth", "养成决策"),
    ("agent", "Agent协同"),
    ("tutorial", "教学交接"),
    ("roguelike", "肉鸽AI"),
    ("reclamation", "生息演算"),
    ("report", "每日战报"),
    ("alert", "告警/错误"),
]


def _opt_val_to_label(options: List[Tuple[str, str]], val: Any) -> str:
    raw = str(val or "").strip()
    for lbl, v in options:
        if v.lower() == raw.lower():
            return lbl
    return options[0][0]


def _opt_label_to_val(options: List[Tuple[str, str]], label: str) -> str:
    for lbl, v in options:
        if lbl == label:
            return v
    return ""


def _format_rl_theme_display(theme: str) -> str:
    raw = str(theme or "").strip()
    if not raw:
        prof = roguelike_ai_copilot.get_theme_ui_profile("BlackFlow")
        return str(prof.get("display_name") or "沉沦者的黑流树海")
    prof = roguelike_ai_copilot.get_theme_ui_profile(raw)
    disp = str(prof.get("display_name") or raw)
    canonical = str(prof.get("theme_key") or raw)
    if raw == disp:
        return f"{disp} ({canonical})"
    return f"{disp} ({raw})"


def _format_rl_mode_display(mode_int: Any, mode_label: str = "") -> str:
    mapping = {
        0: "刷等级/经验",
        1: "刷源石锭投资",
        4: "刷藏品/烧水",
        5: "刷坍缩范式",
        6: "月度小队",
        7: "深入调查",
        10001: "萨卡兹速刷投资",
        30001: "黑流生态养成",
    }
    try:
        mi = int(mode_int)
        if mi in mapping:
            return mapping[mi]
    except Exception:
        pass
    return str(mode_label or "默认刷取")


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


def classify_log_tag(line: str) -> str:
    """Classify a single log line into semantic highlight tag."""
    if "[AI养成决策]" in line:
        return "growth"
    if "[Agent协同]" in line:
        return "agent"
    if "[教学关交接]" in line:
        return "tutorial"
    if any(k in line for k in ("[肉鸽AI]", "[RL-AI", "[RL-REWARD]", "[RL-TECH]", "[RL-POST]", "[ROGUELIKE]")):
        return "roguelike"
    if any(k in line for k in ("[生息演算]", "[RECLAMATION]")):
        return "reclamation"
    if "[STORY]" in line or "[剧情跳过]" in line:
        return "story"
    if any(k in line for k in ("[每日战报]", "[战报]", "[DROP]", "[收获]")):
        return "report"
    if any(k in line for k in ("[LOCK]", "[防错关]", "[NET][自愈失败]")):
        return "error"
    if any(k in line for k in ("[RECOVER]", "[NET]")):
        return "alert"
    if any(k in line for k in ("[ALERT]", "[RECRUIT]", "[WARN]")):
        return "alert"
    if any(k in line for k in ("[ERR]", "[FAIL]", "Error", "失败")):
        return "error"
    return "normal"


def build_init_agent_prompt(base_dir: Path, maa_dir: Optional[Path] = None) -> str:
    """Build prompt #1 for an external desktop AI Agent to onboard and configure workflows."""
    root_str = str(Path(base_dir).resolve())
    maa_str = str(Path(maa_dir).resolve()) if maa_dir else "<尚未定位，请协助配置>"
    cfg_path = str((Path(base_dir) / "config" / "launcher_config.json").resolve())
    readme_path = str((Path(base_dir) / "README.md").resolve())
    return (
        f"你现在是我的明日方舟 MAA 智能自动化副驾驶。请先阅读工作区目录 `{root_str}` 下的核心文件：\n"
        f"1. 说明文档：`{readme_path}`\n"
        f"2. 启动器配置：`{cfg_path}`\n"
        f"3. 调度与推图/长时探索模块：`master_scheduler.py`、`maa_runner.py`、`auto_growth.py`、`ai_strategist.py`、`roguelike_ai_copilot.py`\n"
        f"4. 当前绑定的 MAA 内核目录：`{maa_str}`\n\n"
        f"请你完成以下初始化接管工作：\n"
        f"- 核对 `launcher_config.json` 中的区服（Official/Bilibili）、模拟器 ADB 地址、刷理智策略（AUTO/MANUAL）及肉鸽/生息演算主题与目标；\n"
        f"- 如需我配合配置定向养成干员（`growth.target_operators`）或日常跳过任务，请向我确认后更新配置；\n"
        f"- 确认无误后，告知我你已就绪，可通过 `python -m universal_engine.master_scheduler --mode check` 进行体检，或直接响应后续推图修轴与肉鸽 AI 局外抉择任务。"
    )


def build_repair_agent_prompt(base_dir: Path, task_info: Optional[Dict[str, Any]] = None) -> str:
    """Build prompt #2 for external AI Agent to read agent_task.json (copilot repair or roguelike decision)."""
    root = Path(base_dir).resolve()
    task_file = str((root / "agent_task.json").resolve())
    box_file = str((root / "data" / "my_box.json").resolve())
    cfg_file = str((root / "config" / "launcher_config.json").resolve())

    if task_info and str(task_info.get("task_type", "")).lower() == "roguelike_decision":
        theme = task_info.get("theme", "BlackFlow")
        dec_type = task_info.get("decision_type", "unknown")
        resp_file = task_info.get("response_file", str((root / "agent_decision.json").resolve()))
        shot = task_info.get("screenshot_path", "")
        return (
            f"我在使用 PRTS · MAA 自主控制台进行集成战略（肉鸽 · {theme}）AI 辅助通关，触发了局外抉择协同任务。\n"
            f"当前抉择点：`{dec_type}`\n"
            f"- 协同任务文件：`{task_file}`\n"
            f"- 响应写入文件：`{resp_file}`\n"
            f"- 我的干员 Box：`{box_file}`\n"
            + (f"- 现场截图：`{shot}`\n" if shot else "")
            + "\n请你按以下步骤直接处理：\n"
            "1. 读取 `agent_task.json` 中的 `options` 候选项、`context`（当前层数/血量/希望/编队）与 `rule_baseline` 规则建议；\n"
            "2. 对照 `my_box.json` 与现场截图判断最优选择，将 `{\"selected_index\": 0, \"reason\": \"...\"}` 写入 `response_file`（或直接更新 `agent_task.json` 的 `selected_index` 并将 `status` 改为 `\"resolved\"`）；\n"
            "3. 保存后桌面启动器会自动读取响应并继续探索。"
        )

    if task_info:
        stage = task_info.get("stage_code", "未知关卡")
        reason = task_info.get("trigger_reason", "battle_failed")
        bs = task_info.get("battle_state") or {}
        kills = bs.get("kills")
        unavail_ops = task_info.get("unavailable_opers") or bs.get("unavailable_opers") or []
        target_json = task_info.get("target_copilot_path", str(root / "copilots" / "<target>.json"))
        shot = task_info.get("screenshot_path", "")
        report = task_info.get("report_path", "")
        unavail_line = f"- 不可用干员列表：`{unavail_ops}`\n" if unavail_ops else ""
        context_block = (
            f"当前现场摘要：关卡={stage} | 触发原因={reason} | 击杀数={kills}\n"
            f"- 协同任务文件：`{task_file}`\n"
            f"- 待修改目标作业 JSON：`{target_json}`\n"
            f"{unavail_line}"
            f"- 我的干员 Box：`{box_file}`\n"
            + (f"- 失败战报 JSON：`{report}`\n" if report else "")
            + (f"- 现场截图：`{shot}`\n" if shot else "")
        )
    else:
        context_block = (
            f"请读取并分析以下协同文件：\n"
            f"- 协同任务文件：`{task_file}`\n"
            f"- 我的干员 Box：`{box_file}`\n"
            f"- 启动器配置（含定向养成目标与肉鸽/生息设置）：`{cfg_file}`\n"
        )

    return (
        f"我在使用 MAA 自主推图/养成时触发了 AI 协同修轴，工作目录为 `{root}`。\n"
        f"{context_block}\n"
        f"请你按以下步骤直接处理：\n"
        f"1. 读取 `agent_task.json` 中的 `battle_state`（翻车击杀数 `kills`、费用 `costs`、最后动作与错误日志）、`screenshot_path` 及 `tile_pos_file`；\n"
        f"2. 对照 `my_box.json` 中我实际拥有的高练度精二干员与技能等级，直接修改并保存 `target_copilot_path` 指向的作业 JSON（修正干员替补、技能选择、费用触发点或部署/撤退时机，确保合法 JSON 且不引用未拥有干员）；\n"
        f"3. 保存后桌面启动器会自动监听到文件变化并立即恢复下一轮战斗；如果同时需要调整定向养成干员列表，请同步更新 `config/launcher_config.json` 中的 `growth.target_operators`。"
    )


def get_options_bundle() -> Dict[str, Any]:
    """Return JSON-serializable option lists for frontend dropdowns/chips."""
    return {
        "bridge_version": BRIDGE_API_VERSION,
        "rl_card_modes": list(RL_CARD_MODES),
        "rl_themes": [{"label": lbl, "value": val} for lbl, val in RL_THEME_OPTIONS],
        "rl_targets": [{"label": lbl, "value": val} for lbl, val in RL_TARGET_OPTIONS],
        "ra_themes": [{"label": lbl, "value": val} for lbl, val in RA_THEME_OPTIONS],
        "ra_modes": [{"label": lbl, "value": val} for lbl, val in RA_MODE_OPTIONS],
        "emulator_types": list(EMULATOR_TYPE_OPTIONS),
        "skip_tasks": [{"key": k, "label": lbl} for k, lbl in SKIP_TASK_OPTIONS],
        "api_presets": list(API_PRESETS),
        "log_categories": [{"key": k, "label": lbl} for k, lbl in LOG_CATEGORIES],
    }


FALLBACK_README_CONTENT = """# PRTS · MAA 通用智能自主桌面控制台（Universal Engine）

基于 MAA Core SDK 与现代 Light-Mode pywebview 架构构建的明日方舟全自动日常、自主养成规划与 AI 协同推图系统。
支持免 API Key 的「桌面 AI Agent 协同」与「内置 OpenAI 兼容 API」双模式。

---

## 1. 新手引导与快速启动（3 步引导向导）

首次运行或未检测到 MAA 时，启动器将自动弹出 3 步新手引导向导：
1. **定位与绑定 MAA 内核**：自动检索当前目录、父级目录及系统常见路径；支持手动浏览选择或一键国内加速下载解压。
2. **连接模拟器与环境体检**：自动扫描 MuMu / 雷电等常见 ADB 端口（默认 `127.0.0.1:16384`），检测 16:9 分辨率及读写权限。
3. **选择 AI 协同方式**：推荐免 API Key 的「桌面 AI Agent 直连协同」，亦可选用「内置 OpenAI 兼容 API」模式。

---

## 2. 双模式 AI 协同架构

### 模式 A：直连桌面 AI Agent（默认 · 免 API Key）
兼容 Antigravity、Claude Code、Cursor、OpenClaw 等桌面/终端 AI Agent：
- **日常全速零打扰**：本地全速执行日常收菜与规则决策，不产生任何 API 费用。
- **现场遇阻监听与自动接续**：推图翻车或缺失干员时，自动在工作区生成 `agent_task.json` 协同文件，包含战场切片、截图及干员 Box 信息。
- **自动恢复**：Agent 修改目标作业 JSON 并保存后，面板自动监听并立即恢复下一轮战斗。

### 模式 B：内置 OpenAI 兼容 API（静默调用）
在配置中填写 `base_url`、`api_key` 与 `model_name`，引擎将在推图遇阻时静默请求模型生成修轴补丁，网络异常或格式不符时自动回退本地规则。

---

## 3. 七大核心任务模式

1. **一键日常收菜**：自动化串联唤醒、公招、基建、信用商店、剿灭、刷理智与领奖。
2. **智能自主养成**：结合仓库与干员练度，智能决策刷取活动材料、芯片或主线关卡。
3. **当期剿灭托管**：每周一 04:00 自动检查并打满合成玉上限；支持自动下载高赞作业解锁新剿灭。
4. **新活动 / 主线推图**：输入关卡代码（如 `14-20`），自动调度作业抄录、Box 替补、剧情跳过及教学关交接。
5. **刷新干员与仓库档案**：通过 MAA 识别并同步干员练度库（`data/my_box.json`）与仓库物资。
6. **集成战略（肉鸽）**：支持「纯 MAA 自动刷取」与「AI 辅助策略通关」，包含自动领奖与科技树加点。
7. **生息演算**：支持重启锚点（`RA-1`、`RA-4`、`RA-15`）及沙洲遗闻刷取，支持自动搬空商店。

---

## 4. 核心配置文件与协同文件

- 启动器配置：`config/launcher_config.json`
- 协同任务文件：`agent_task.json`
- 干员练度库：`data/my_box.json`

---

## 5. 无头 CLI 命令行用法

```bash
# 环境与决策体检
python -m universal_engine.master_scheduler --mode check

# 全自动日常（B服，完成后关闭模拟器）
python -m universal_engine.master_scheduler --mode daily --server Bilibili --post-action close_emulator

# 推图（指定关卡）
python -m universal_engine.master_scheduler --mode push --stages 14-20

# 肉鸽与生息演算
python -m universal_engine.master_scheduler --mode roguelike --theme BlackFlow
python -m universal_engine.master_scheduler --mode reclamation --theme RelaunchAnchor --ra-mode RA1
```
"""


def load_readme_content(base_dir: Optional[Union[Path, str]] = None) -> Tuple[str, str]:
    """Resolve and load README.md content across source, bundled, and fallback environments.

    Search hierarchy:
      1. Path(base_dir) / "README.md"
      2. Path(base_dir) / "universal_engine" / "README.md"
      3. Path(sys.executable).resolve().parent / "README.md" (when sys.frozen)
      4. Path(sys._MEIPASS) / "universal_engine" / "README.md" (when sys._MEIPASS)
      5. Path(sys._MEIPASS) / "README.md" (when sys._MEIPASS)
      6. Path(__file__).resolve().parent.parent / "README.md" (universal_engine/README.md)
      7. Path(__file__).resolve().parent.parent.parent / "README.md"
      8. Fallback to (FALLBACK_README_CONTENT, "embedded-fallback")
    """
    candidates: List[Path] = []
    if base_dir:
        b = Path(base_dir)
        candidates.append(b / "README.md")
        candidates.append(b / "universal_engine" / "README.md")

    if getattr(sys, "frozen", False):
        try:
            candidates.append(Path(sys.executable).resolve().parent / "README.md")
        except Exception:
            pass
        if hasattr(sys, "_MEIPASS"):
            meipass = Path(getattr(sys, "_MEIPASS"))
            candidates.append(meipass / "universal_engine" / "README.md")
            candidates.append(meipass / "README.md")

    try:
        candidates.append(Path(__file__).resolve().parent.parent / "README.md")
    except Exception:
        pass
    try:
        candidates.append(Path(__file__).resolve().parent.parent.parent / "README.md")
    except Exception:
        pass

    for p in candidates:
        try:
            if p.is_file():
                txt = p.read_text(encoding="utf-8")
                if txt.strip():
                    return txt, str(p.resolve())
        except Exception:
            continue

    return FALLBACK_README_CONTENT, "embedded-fallback"

