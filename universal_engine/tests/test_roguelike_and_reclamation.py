import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from universal_engine import ai_strategist, maa_runner, master_scheduler, roguelike_ai_copilot, stage_pilot
from universal_engine.roguelike_ai_copilot import (
    RoguelikeAICopilot,
    RoguelikeRunContext,
    auto_upgrade_roguelike_tech_tree,
    claim_roguelike_rewards,
    detect_upgradeable_tech_nodes,
    get_theme_ui_profile,
    run_roguelike_post_settlement,
    score_tech_tree_node,
)
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _sample_my_box() -> dict:
    ops = [
        {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
        {"name": "塞雷娅", "elite": 2, "level": 60, "rarity": 6},
        {"name": "纯烬艾雅法拉", "elite": 2, "level": 60, "rarity": 6},
        {"name": "棘刺", "elite": 2, "level": 70, "rarity": 6},
        {"name": "能天使", "elite": 2, "level": 60, "rarity": 6},
        {"name": "克洛丝", "elite": 1, "level": 55, "rarity": 3},
        {"name": "芬", "elite": 1, "level": 55, "rarity": 3},
    ]
    return {
        "total_owned": len(ops),
        "e2_count": sum(1 for o in ops if o["elite"] >= 2),
        "e2_operators": [o for o in ops if o["elite"] >= 2],
        "all_owned": {o["name"]: o for o in ops},
    }


def _make_fake_gui_config(maa_dir: Path, rl_task: dict, ra_task: dict) -> None:
    cfg_dir = maa_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "Current": "Default",
        "Configurations": {
            "Default": {
                "TaskQueue": [
                    {"TaskType": "Roguelike", **rl_task},
                    {"TaskType": "Reclamation", **ra_task},
                ]
            }
        },
    }
    (cfg_dir / "gui.new.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class ExplorationConfigAndNativeTasksTests(unittest.TestCase):
    def test_inspect_real_maa_exploration_capabilities(self):
        if not (REPO_ROOT / "resource" / "tasks" / "Roguelike" / "BlackFlow.json").is_file():
            self.skipTest("MAA resource/tasks not present")
        caps = maa_runner.inspect_maa_exploration_capabilities(REPO_ROOT)
        self.assertEqual(caps["roguelike_task_type"], "Roguelike")
        self.assertEqual(caps["reclamation_task_type"], "Reclamation")
        self.assertIn("BlackFlow", caps["roguelike_themes"])
        self.assertIn("RelaunchAnchor", caps["reclamation_themes"])
        self.assertIn("Tales", caps["reclamation_themes"])

        bf = caps["roguelike_theme_capabilities"]["BlackFlow"]
        self.assertTrue(bf["native_level_check"])
        # Verified MAA v6.22.0 does not natively claim BlackFlow rewards or upgrade 生命游戏 tech tree
        self.assertFalse(bf["native_reward_claim"])
        self.assertFalse(bf["native_tech_tree_upgrade"])
        self.assertTrue(bf["requires_adb_post_settlement"])

    def test_roguelike_and_reclamation_config_precedence_and_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            _make_fake_gui_config(
                maa,
                rl_task={
                    "Theme": "BlackFlow",
                    "Mode": "Investment",
                    "Difficulty": 12,
                    "Squad": "",  # empty -> fallback to built-in default
                    "Roles": "稳扎稳打",
                    "CoreChar": "维什戴尔",
                    "StartCount": 20,
                    "Investment": True,
                    "InvestCount": 300,
                    "StopWhenLevelMax": True,
                },
                ra_task={
                    "Theme": "RelaunchAnchor",
                    "Mode": "RA4",
                    "ToolToCraft": "",
                    "IncrementMode": 0,
                    "MaxCraftCountPerRound": 16,
                    "ClearStore": True,
                },
            )

            # 1. Inherits gui.new.json when launcher_config fields are blank/None
            rl_from_gui = maa_runner.build_roguelike_task_params(env_doctor.DEFAULT_LAUNCHER_CONFIG, maa_dir=maa)
            self.assertEqual(rl_from_gui["theme"], "BlackFlow")
            self.assertEqual(rl_from_gui["mode"], 1)  # Investment -> 1
            self.assertEqual(rl_from_gui["difficulty"], 12)
            self.assertEqual(rl_from_gui["squad"], "指挥分队")  # fallback since gui.new.json had ""
            self.assertEqual(rl_from_gui["roles"], "稳扎稳打")
            self.assertEqual(rl_from_gui["core_char"], "维什戴尔")
            self.assertEqual(rl_from_gui["starts_count"], 20)
            self.assertTrue(rl_from_gui["stop_at_max_level"])

            ra_from_gui = maa_runner.build_reclamation_task_params(env_doctor.DEFAULT_LAUNCHER_CONFIG, maa_dir=maa)
            self.assertEqual(ra_from_gui["theme"], "RelaunchAnchor")
            self.assertEqual(ra_from_gui["mode"], 32)  # Verified MaaCore.dll v6.22.0: RA4 == 32
            self.assertTrue(ra_from_gui["clear_store"])

            # 2. launcher_config overrides gui.new.json, runtime overrides override launcher_config
            custom_cfg = env_doctor._deep_merge(
                env_doctor.DEFAULT_LAUNCHER_CONFIG,
                {
                    "roguelike": {"theme": "Sarkaz", "mode": "Exp", "squad": "蓝图测绘分队", "core_char": "新约能天使"},
                    "reclamation": {"theme": "Tales", "mode": "ProsperityInSave", "tools_to_craft": ["荧光棒", "便携式种植槽"]},
                },
            )
            rl_over = maa_runner.build_roguelike_task_params(custom_cfg, maa_dir=maa, overrides={"core_char": "酒神"})
            self.assertEqual(rl_over["theme"], "Sarkaz")
            self.assertEqual(rl_over["mode"], 0)
            self.assertEqual(rl_over["squad"], "蓝图测绘分队")
            self.assertEqual(rl_over["core_char"], "酒神")

            ra_over = maa_runner.build_reclamation_task_params(custom_cfg, maa_dir=maa)
            self.assertEqual(ra_over["theme"], "Tales")
            self.assertEqual(ra_over["mode"], 1)
            self.assertEqual(ra_over["tools_to_craft"], ["荧光棒", "便携式种植槽"])
            self.assertEqual(ra_over["num_craft_batches"], 16)

            # 3. RelaunchAnchor RA1 / RA15 mappings & BlackFlow mode 4 -> 30001 safety mapping
            self.assertEqual(maa_runner.build_reclamation_task_params({}, overrides={"theme": "RelaunchAnchor", "mode": "RA1"})["mode"], 16)
            self.assertEqual(maa_runner.build_reclamation_task_params({}, overrides={"theme": "RelaunchAnchor", "mode": "RA15"})["mode"], 48)
            self.assertEqual(maa_runner.build_roguelike_task_params({}, overrides={"theme": "BlackFlow", "mode": "Collectible"})["mode"], 30001)

            # 4. Missing gui.new.json cleanly falls back to built-in defaults
            empty_maa = Path(tmp) / "no_gui_maa"
            empty_maa.mkdir()
            rl_def = maa_runner.build_roguelike_task_params({}, maa_dir=empty_maa)
            self.assertEqual(rl_def["theme"], "BlackFlow")
            self.assertEqual(rl_def["squad"], "指挥分队")
            self.assertEqual(rl_def["roles"], "取长补短")
            ra_def = maa_runner.build_reclamation_task_params({}, maa_dir=empty_maa)
            self.assertEqual(ra_def["theme"], "RelaunchAnchor")
            self.assertEqual(ra_def["mode"], 16)

    def test_engine_roguelike_reclamation_pipelines_cli_and_cancel_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource" / "tasks" / "Roguelike").mkdir(parents=True)
            (maa / "resource" / "tasks" / "Roguelike" / "BlackFlow.json").write_text(
                json.dumps({"BlackFlow@Roguelike@CheckLevel": {"text": ["源流堆栈"]}}, ensure_ascii=False),
                encoding="utf-8",
            )
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"post_action": "shutdown"}, cfg_path)

            sess = MagicMock()
            sess.maa_dir = maa
            sess.base_dir = base
            sess.adb_path = "adb.exe"
            sess.adb_addr = "127.0.0.1:16384"
            sess.cancel_event = None
            sess.run_single_task.side_effect = [
                # run_roguelike: StartUp + Roguelike
                {"ok": True, "cancelled": False, "elapsed": 1.0, "events": [], "log_lines": []},
                {
                    "ok": True,
                    "cancelled": False,
                    "elapsed": 120.5,
                    "events": [
                        {"message": "SubTaskCompleted", "details": {"details": {"task": "BlackFlow@Roguelike@StartExplore"}}},
                        {"message": "SubTaskExtraInfo", "details": {"what": "BlackFlowStrategyStarted", "details": {"profile": "burn_with_investment"}}},
                        {"message": "SubTaskExtraInfo", "details": {"what": "RoguelikeInvestment", "details": {"count": 12}}},
                        {"message": "SubTaskCompleted", "details": {"details": {"task": "BlackFlow@Roguelike@GamePass"}}},
                    ],
                    "log_lines": [],
                    "chain_errors": [],
                },
                # run_reclamation: StartUp + Reclamation
                {"ok": True, "cancelled": False, "elapsed": 1.0, "events": [], "log_lines": []},
                {
                    "ok": True,
                    "cancelled": False,
                    "elapsed": 90.0,
                    "events": [
                        {"message": "SubTaskCompleted", "details": {"details": {"task": "RelaunchAnchor@RA@RA1-FinishConstruction"}}},
                        {"message": "SubTaskCompleted", "details": {"details": {"task": "RA@Store@Underfunded"}}},
                    ],
                    "log_lines": [],
                    "chain_errors": [],
                },
            ]

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler.MaaEngine, "_connect_session", return_value=(maa, sess, "")), \
                 patch("universal_engine.master_scheduler.capture_adb_screenshot", return_value=False), \
                 patch("universal_engine.roguelike_ai_copilot.run_roguelike_post_settlement", return_value={"theme": "BlackFlow", "rewards": {"level_rewards_claimed": 2}, "tech_tree": {"upgraded_count": 1}}) as mock_post_rl:
                engine = master_scheduler.MaaEngine(config_path=cfg_path)
                rl_report = engine.run_roguelike(theme="BlackFlow", rl_mode="Exp", dry_run_post_action=True)
                self.assertTrue(rl_report["ok"])
                self.assertEqual(rl_report["theme"], "BlackFlow")
                self.assertEqual(rl_report["metrics"]["runs_started"], 1)
                self.assertEqual(rl_report["metrics"]["settlements"], 1)
                self.assertEqual(rl_report["metrics"]["investments"], 12)
                self.assertEqual(rl_report["metrics"]["blackflow_profile"], "burn_with_investment")
                self.assertTrue((base / "data" / "latest_roguelike_report.json").is_file())
                mock_post_rl.assert_called_once()

                ra_report = engine.run_reclamation(theme="RelaunchAnchor", ra_mode="RA1", clear_store=True, dry_run_post_action=True)
                self.assertTrue(ra_report["ok"])
                self.assertEqual(ra_report["params"]["mode"], 16)
                self.assertEqual(ra_report["metrics"]["cycles_completed"], 1)
                self.assertTrue(ra_report["metrics"]["store_cleared"])
                self.assertTrue((base / "data" / "latest_reclamation_report.json").is_file())

                # Verify cancel_event stops immediately without running post_action
                cancel_ev = threading.Event()
                cancel_ev.set()
                with patch("universal_engine.master_scheduler.execute_post_action") as mock_post_act:
                    c_rl = engine.run_roguelike(cancel_event=cancel_ev)
                    c_ra = engine.run_reclamation(cancel_event=cancel_ev)
                    self.assertTrue(c_rl["cancelled"])
                    self.assertFalse(c_rl["ok"])
                    self.assertTrue(c_ra["cancelled"])
                    self.assertFalse(c_ra["ok"])
                    mock_post_act.assert_not_called()

        # Verify CLI --mode roguelike & --mode reclamation in both master_scheduler & maa_runner
        buf = io.StringIO()
        with patch.object(master_scheduler.MaaEngine, "run_roguelike", return_value={"ok": True, "theme": "BlackFlow"}) as mock_cli_rl, \
             patch.object(master_scheduler.MaaEngine, "run_reclamation", return_value={"ok": True, "theme": "RelaunchAnchor"}) as mock_cli_ra, \
             redirect_stdout(buf):
            rc1 = master_scheduler.main(["--mode", "roguelike", "--theme", "BlackFlow", "--rl-mode", "Exp", "--ai-copilot", "--no-refresh-cache"])
            self.assertEqual(rc1, 0)
            self.assertEqual(mock_cli_rl.call_args.kwargs["theme"], "BlackFlow")
            self.assertTrue(mock_cli_rl.call_args.kwargs["ai_copilot_enabled"])

            rc2 = maa_runner.main(["--mode", "reclamation", "--theme", "RelaunchAnchor", "--ra-mode", "RA4", "--clear-store", "--no-refresh-cache"])
            self.assertEqual(rc2, 0)
            self.assertEqual(mock_cli_ra.call_args.kwargs["ra_mode"], "RA4")
            self.assertTrue(mock_cli_ra.call_args.kwargs["clear_store"])


class RoguelikeAICopilotTests(unittest.TestCase):
    def test_decision_1_recruitment_prefers_e2_and_role_balance_and_hope_budget(self):
        copilot = RoguelikeAICopilot(
            config={"ai": {"mode": "agent_collab", "enabled": False}, "roguelike": {"ai_copilot_enabled": False}},
            my_box=_sample_my_box(),
        )
        # Missing medic & blocker: team only has sniper 维什戴尔
        ctx = RoguelikeRunContext(theme="BlackFlow", floor=2, hope=6, current_team=["维什戴尔"])
        # Candidate list includes unowned 6*, E1 3*, and owned E2 medic 纯烬艾雅法拉
        dec = copilot.decide_recruitment(
            candidates=[
                {"name": "未持有干员X", "hope_cost": 6},
                {"name": "克洛丝", "hope_cost": 0},
                {"name": "纯烬艾雅法拉", "hope_cost": 6},
            ],
            run_context=ctx,
        )
        self.assertEqual(dec.action, "select")
        self.assertEqual(dec.selected_value, "纯烬艾雅法拉")
        self.assertIn("纯烬艾雅法拉", ctx.current_team)

        # Missing blocker when medic & anti-air already covered -> picks E2 defender 塞雷娅 over E1 sniper 克洛丝
        ctx2 = RoguelikeRunContext(theme="BlackFlow", floor=2, hope=6, current_team=["维什戴尔", "纯烬艾雅法拉"])
        dec2 = copilot.decide_recruitment(
            candidates=[
                {"name": "克洛丝", "hope_cost": 0},
                {"name": "塞雷娅", "hope_cost": 6},
            ],
            run_context=ctx2,
        )
        self.assertEqual(dec2.selected_value, "塞雷娅")

        # Insufficient hope -> returns give_up cleanly
        dec3 = copilot.decide_recruitment(
            candidates=[{"name": "塞雷娅", "hope_cost": 6}],
            run_context=RoguelikeRunContext(hope=2, current_team=[]),
        )
        self.assertEqual(dec3.action, "give_up")

    def test_decision_2_map_node_avoids_risky_emergency_when_weak_or_low_hp(self):
        copilot = RoguelikeAICopilot(
            config={"ai": {"mode": "agent_collab", "enabled": False}, "roguelike": {"ai_copilot_enabled": False}},
            my_box=_sample_my_box(),
        )
        nodes = [
            {"name": "紧急作战：失控阵线", "node_type": "emergency", "click_xy": [500, 220]},
            {"name": "安全的角落", "node_type": "safehouse", "click_xy": [500, 420]},
            {"name": "不期而遇", "node_type": "encounter", "click_xy": [500, 560]},
        ]
        # Low HP & weak team -> MUST avoid emergency and choose safehouse
        weak_ctx = RoguelikeRunContext(theme="BlackFlow", floor=3, hp=2, max_hp=10, current_team=["克洛丝"])
        dec_safe = copilot.decide_map_node(nodes, run_context=weak_ctx)
        self.assertEqual(dec_safe.selected_value, "安全的角落")

        # Strong full E2 squad & healthy HP -> can pick high-reward emergency over plain combat
        strong_ctx = RoguelikeRunContext(
            theme="BlackFlow",
            floor=3,
            hp=10,
            max_hp=10,
            current_team=["维什戴尔", "塞雷娅", "纯烬艾雅法拉", "棘刺"],
        )
        dec_strong = copilot.decide_map_node(
            [
                {"name": "普通作战", "node_type": "combat"},
                {"name": "紧急作战：精英遭遇", "node_type": "emergency"},
            ],
            run_context=strong_ctx,
        )
        self.assertEqual(dec_strong.selected_value, "紧急作战：精英遭遇")

    def test_decision_3_encounter_avoids_lethal_hp_loss_and_prefers_high_value_relic(self):
        copilot = RoguelikeAICopilot(
            config={"ai": {"mode": "agent_collab", "enabled": False}, "roguelike": {"ai_copilot_enabled": False}},
            my_box=_sample_my_box(),
        )
        low_hp_ctx = RoguelikeRunContext(theme="BlackFlow", floor=4, hp=1, max_hp=10, ingots=10)
        dec = copilot.decide_encounter_or_relic(
            event_title="沉重的契约",
            options=[
                {"title": "以血换宝", "description": "损失目标生命2点，获得1件收藏品"},
                {"title": "离开", "description": "什么都不做，安全离开"},
            ],
            category="encounter",
            run_context=low_hp_ctx,
        )
        self.assertEqual(dec.selected_value, "离开")

        # Relic selection prefers attack-speed / hope over plain minor bonus
        dec_relic = copilot.decide_encounter_or_relic(
            event_title="藏品三选一",
            options=[
                {"title": "普通徽章", "description": "防御+5%"},
                {"title": "金酒之杯", "description": "每5源石锭提升攻击速度+7"},
                {"title": "离开", "description": "放弃"},
            ],
            category="relic",
            run_context=RoguelikeRunContext(hp=8, ingots=25),
        )
        self.assertEqual(dec_relic.selected_value, "金酒之杯")

    def test_decision_4_boss_strategy_prts_hit_vs_maa_native_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text("[]", encoding="utf-8")
            (maa / "resource" / "recruitment.json").write_text("{}", encoding="utf-8")

            copilot = RoguelikeAICopilot(
                config={"ai": {"mode": "agent_collab", "enabled": False}, "roguelike": {"ai_copilot_enabled": False, "boss_prts_assist": True}},
                maa_dir=maa,
                base_dir=base,
                my_box=_sample_my_box(),
            )
            ctx = RoguelikeRunContext(theme="BlackFlow", floor=5, current_team=["维什戴尔", "塞雷娅", "纯烬艾雅法拉"])

            compatible_guide = [{
                "id": 77701,
                "like": 150,
                "views": 4000,
                "content": {
                    "stage_name": "ro6_boss_1",
                    "opers": [
                        {"name": "维什戴尔", "skill": 3, "skill_usage": 1},
                        {"name": "塞雷娅", "skill": 1, "skill_usage": 1},
                    ],
                    "groups": [],
                    "actions": [{"type": "Deploy", "name": "维什戴尔", "location": [4, 4]}],
                },
            }]
            dec_prts = copilot.decide_boss_strategy(
                "ro6_boss_1",
                run_context=ctx,
                fetch_guides_fn=lambda sid, code, limit: compatible_guide,
            )
            self.assertEqual(dec_prts.action, "prts_copilot")
            self.assertTrue(Path(dec_prts.extra["copilot_path"]).is_file())

            # When no PRTS guide is found -> gracefully falls back to MAA native battle engine
            dec_native = copilot.decide_boss_strategy(
                "ro6_unknown_boss",
                run_context=ctx,
                fetch_guides_fn=lambda sid, code, limit: [],
            )
            self.assertEqual(dec_native.action, "maa_native_battle")

            # End-to-end run_roguelike_task hybrid boss handoff (pauses at 5F boss -> resumes native if no PRTS guide)
            fake_sess = MagicMock()
            fake_sess.maa_dir = maa
            fake_sess.base_dir = base
            fake_sess.adb_path = ""
            fake_sess.adb_addr = ""
            fake_sess.cancel_event = None
            fake_sess.run_single_task.side_effect = [
                {
                    "ok": True,
                    "cancelled": False,
                    "elapsed": 50.0,
                    "events": [
                        {"message": "SubTaskCompleted", "details": {"details": {"task": "BlackFlow@Roguelike@StageDreadfulFoe-5"}}},
                    ],
                    "log_lines": [],
                    "chain_errors": [],
                },
                {
                    "ok": True,
                    "cancelled": False,
                    "elapsed": 30.0,
                    "events": [
                        {"message": "SubTaskCompleted", "details": {"details": {"task": "BlackFlow@Roguelike@GamePass"}}},
                    ],
                    "log_lines": [],
                    "chain_errors": [],
                },
            ]
            cfg_ai = env_doctor._deep_merge(
                env_doctor.DEFAULT_LAUNCHER_CONFIG,
                {
                    "ai": {"mode": "agent_collab", "enabled": False},
                    "roguelike": {
                        "theme": "BlackFlow",
                        "ai_copilot_enabled": True,
                        "boss_prts_assist": True,
                        "auto_claim_rewards": False,
                        "auto_upgrade_tech_tree": False,
                    },
                },
            )
            with patch.object(maa_runner, "fetch_prts_guides", return_value=[]):
                res_hybrid = maa_runner.run_roguelike_task({"session": fake_sess}, cfg_ai, maa_dir=maa, base_dir=base)
            self.assertTrue(res_hybrid["ok"])
            self.assertEqual(res_hybrid["boss_decision"]["action"], "maa_native_battle")
            self.assertEqual(fake_sess.run_single_task.call_count, 2)
            # First run injected stop_at_final_boss=True; second run resumed with stop_at_final_boss=False
            self.assertTrue(fake_sess.run_single_task.call_args_list[0].args[1]["stop_at_final_boss"])
            self.assertFalse(fake_sess.run_single_task.call_args_list[1].args[1]["stop_at_final_boss"])

    def test_dual_ai_mode_builtin_multimodal_and_agent_collab_with_timeout_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            shot = base / "screen_choice.png"
            base.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (64, 36), (12, 34, 56)).save(shot, format="PNG")

            # Mode 1: builtin_api multimodal sends image_url base64 payload and applies choice
            captured = {}

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({
                        "choices": [{"message": {"content": '{"selected_index": 1, "reason": "视觉识别到高优路线"}'}}]
                    }).encode("utf-8")

            def _fake_urlopen(req, timeout=30):
                captured["payload"] = json.loads(req.data.decode("utf-8"))
                return _FakeResp()

            api_cfg = {
                "ai": {"mode": "builtin_api", "enabled": True, "api_key": "sk-test", "model_name": "gpt-4o"},
                "roguelike": {"ai_copilot_enabled": True, "ai_vision_enabled": True},
            }
            copilot_api = RoguelikeAICopilot(config=api_cfg, base_dir=base, my_box=_sample_my_box())
            with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
                dec_api = copilot_api.decide_map_node(
                    [{"name": "普通作战", "node_type": "combat"}, {"name": "不期而遇", "node_type": "encounter"}],
                    run_context=RoguelikeRunContext(theme="BlackFlow", hp=8),
                    screenshot_path=shot,
                )
            self.assertEqual(dec_api.source, "builtin_api_vision")
            self.assertEqual(dec_api.selected_index, 1)
            user_content = captured["payload"]["messages"][1]["content"]
            self.assertIsInstance(user_content, list)
            self.assertTrue(any(part.get("type") == "image_url" and "data:image/png;base64," in part["image_url"]["url"] for part in user_content))

            # Mode 2A: agent_collab resolves when external desktop Agent writes agent_decision.json
            collab_cfg = {
                "ai": {"mode": "agent_collab", "enabled": False},
                "roguelike": {"ai_copilot_enabled": True, "ai_decision_timeout_sec": 3.0, "ai_auto_fallback_on_timeout": True},
            }
            seen_tasks = []

            def _external_agent_responder(payload: dict):
                seen_tasks.append(payload)
                resp_path = Path(payload["response_file"])

                def _write_reply():
                    time.sleep(0.08)
                    resp_path.write_text(
                        json.dumps({"selected_index": 1, "reason": "桌面 Agent 优先抓医疗"}, ensure_ascii=False),
                        encoding="utf-8",
                    )

                threading.Thread(target=_write_reply, daemon=True).start()

            copilot_collab = RoguelikeAICopilot(
                config=collab_cfg,
                base_dir=base,
                my_box=_sample_my_box(),
                collab_handler=_external_agent_responder,
                poll_interval=0.03,
            )
            dec_agent = copilot_collab.decide_recruitment(
                candidates=[{"name": "棘刺", "hope_cost": 6}, {"name": "纯烬艾雅法拉", "hope_cost": 6}],
                run_context=RoguelikeRunContext(theme="BlackFlow", hope=6, current_team=["维什戴尔"]),
                screenshot_path=shot,
            )
            self.assertEqual(dec_agent.source, "agent_collab")
            self.assertEqual(dec_agent.selected_value, "纯烬艾雅法拉")
            self.assertEqual(seen_tasks[0]["task_type"], "roguelike_decision")
            self.assertEqual(seen_tasks[0]["screenshot_path"], str(shot))

            # Mode 2B: agent_collab timeout auto-fallback prevents unattended deadlock
            timeout_cfg = {
                "ai": {"mode": "agent_collab", "enabled": False},
                "roguelike": {"ai_copilot_enabled": True, "ai_decision_timeout_sec": 0.12, "ai_auto_fallback_on_timeout": True},
            }
            copilot_timeout = RoguelikeAICopilot(
                config=timeout_cfg,
                base_dir=base,
                my_box=_sample_my_box(),
                poll_interval=0.03,
            )
            dec_fb = copilot_timeout.decide_encounter_or_relic(
                event_title="洞中宝",
                options=[
                    {"title": "离开", "description": "无事发生"},
                    {"title": "拿取源石锭", "description": "获得8源石锭与收藏品"},
                ],
                run_context=RoguelikeRunContext(theme="BlackFlow", hp=8),
                screenshot_path=shot,
            )
            self.assertEqual(dec_fb.source, "rule_timeout_fallback")
            self.assertEqual(dec_fb.selected_value, "拿取源石锭")
            task_saved = json.loads((base / "agent_task.json").read_text(encoding="utf-8"))
            self.assertEqual(task_saved["status"], "timeout_fallback")


class RoguelikePostSettlementRewardsAndTechTreeTests(unittest.TestCase):
    def test_multi_theme_profiles_and_tech_tree_priority_scoring(self):
        for theme in ("BlackFlow", "JieGarden", "Sarkaz", "Sami", "Mizuki", "Phantom"):
            prof = get_theme_ui_profile(theme)
            self.assertIn("level_reward", prof)
            self.assertIn("monthly_commission", prof)
            self.assertIn("tech_tree", prof)

        bf = get_theme_ui_profile("BlackFlow")
        self.assertEqual(bf["level_reward"]["name"], "源流堆栈")
        self.assertEqual(bf["monthly_commission"]["name"], "本月委托栏")
        self.assertEqual(bf["tech_tree"]["name"], "生命游戏")

        s_hope, cat_hope = score_tech_tree_node("源流启迪", "开局希望+2，招募所需希望-1")
        s_combat, cat_combat = score_tech_tree_node("体能强化", "所有干员攻击力+8%，生命上限+8%")
        s_shop, cat_shop = score_tech_tree_node("商路拓展", "诡意行商商店刷新次数+1")
        s_other, cat_other = score_tech_tree_node("奇闻收藏", "解锁背景档案")

        self.assertEqual(cat_hope, "hope_dp_recruit")
        self.assertEqual(cat_combat, "combat_stats")
        self.assertEqual(cat_shop, "shop_refresh")
        self.assertEqual(cat_other, "other")
        self.assertGreater(s_hope, s_combat)
        self.assertGreater(s_combat, s_shop)
        self.assertGreater(s_shop, s_other)

    def test_detect_upgradeable_tech_nodes_on_synthetic_canvas(self):
        canvas = Image.new("RGB", (1280, 720), (18, 22, 30))
        draw = ImageDraw.Draw(canvas)
        expected = [(260, 240), (540, 380), (840, 290)]
        # Warm gold accent, bright cyan accent, and white-halo grey stone frontier node
        draw.ellipse((260 - 18, 240 - 18, 260 + 18, 240 + 18), fill=(250, 210, 80))
        draw.ellipse((540 - 18, 380 - 18, 540 + 18, 380 + 18), fill=(90, 230, 245))
        draw.ellipse((840 - 28, 290 - 28, 840 + 28, 290 + 28), fill=(248, 248, 248))
        draw.ellipse((840 - 18, 290 - 18, 840 + 18, 290 + 18), fill=(95, 98, 104))

        nodes = detect_upgradeable_tech_nodes(canvas)
        self.assertEqual(len(nodes), 3)
        # Glowing-halo frontier node (840, 290) is prioritized first, followed by accent nodes left-to-right
        self.assertTrue(nodes[0]["has_halo"])
        self.assertLess(abs(nodes[0]["cx"] - 840), 25)
        self.assertLess(abs(nodes[0]["cy"] - 290), 25)
        accent_sorted = sorted(nodes[1:], key=lambda n: n["cx"])
        for exp_xy, got in zip(expected[:2], accent_sorted):
            self.assertLess(abs(got["cx"] - exp_xy[0]), 25)
            self.assertLess(abs(got["cy"] - exp_xy[1]), 25)

    def test_claim_blackflow_rewards_and_auto_upgrade_tech_tree_by_priority(self):
        dummy_img = Image.new("RGB", (1280, 720), (20, 20, 25))
        taps = []

        # Sequence of OCR boxes for claim_roguelike_rewards:
        # 1) lobby -> sees 源流堆栈 (top-right)
        # 2) inside 源流堆栈 -> sees 一键领取
        # 3) inside 源流堆栈 -> nothing left to claim
        # 4) _tap_return_to_lobby -> sees 开始探索 (back on lobby)
        # 5) lobby -> sees 本月委托栏 (right)
        # 6) inside 本月委托栏 -> sees 领取
        # 7) inside 本月委托栏 -> nothing left
        # 8) _tap_return_to_lobby -> back on lobby
        ocr_seq_rewards = [
            [{"text": "源流堆栈", "compact": "源流堆栈", "cx": 1090, "cy": 58}],
            [{"text": "一键领取", "compact": "一键领取", "cx": 1080, "cy": 640}],
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 640, "cy": 360}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1120, "cy": 620}],
            [{"text": "本月委托栏", "compact": "本月委托栏", "cx": 1140, "cy": 265}],
            [{"text": "领取", "compact": "领取", "cx": 960, "cy": 320}],
            [{"text": "已完成", "compact": "已完成", "cx": 960, "cy": 320}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1120, "cy": 620}],
        ]

        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq_rewards), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            rew = claim_roguelike_rewards("adb.exe", "127.0.0.1:16384", REPO_ROOT, theme="BlackFlow")
        self.assertTrue(rew["ok"])
        self.assertEqual(rew["level_rewards_claimed"], 1)
        self.assertEqual(rew["commission_rewards_claimed"], 1)
        self.assertIn((1090, 58), taps)
        self.assertIn((1140, 265), taps)

        # Now test auto_upgrade_roguelike_tech_tree priority ordering:
        # 3 lit nodes on canvas: (250,250)=商店刷新, (500,300)=干员攻击力, (750,350)=开局希望+2
        # Expect round 1 to pick (750,350) hope_dp_recruit FIRST, then round 2 empty -> finish!
        taps.clear()
        lit_seq = [
            [{"cx": 250, "cy": 250, "lit_pixels": 300}, {"cx": 500, "cy": 300, "lit_pixels": 300}, {"cx": 750, "cy": 350, "lit_pixels": 300}],
            [],
        ]
        ocr_seq_tech = [
            [{"text": "生命游戏", "compact": "生命游戏", "cx": 185, "cy": 610}],  # lobby entry
            [{"text": "商店刷新次数+1", "compact": "商店刷新次数+1", "cx": 980, "cy": 260}, {"text": "激活", "compact": "激活", "cx": 1060, "cy": 580}],
            [{"text": "干员攻击力+10%", "compact": "干员攻击力+10%", "cx": 980, "cy": 260}, {"text": "激活", "compact": "激活", "cx": 1060, "cy": 580}],
            [{"text": "开局希望+2 初始费用+4", "compact": "开局希望+2初始费用+4", "cx": 980, "cy": 260}, {"text": "激活", "compact": "激活", "cx": 1060, "cy": 580}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1120, "cy": 620}],  # return to lobby
        ]

        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", side_effect=lit_seq), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq_tech), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            tech_res = auto_upgrade_roguelike_tech_tree("adb.exe", "127.0.0.1:16384", REPO_ROOT, theme="BlackFlow")

        self.assertTrue(tech_res["ok"])
        self.assertEqual(tech_res["upgraded_count"], 1)
        self.assertEqual(tech_res["upgraded_nodes"][0]["tier"], "hope_dp_recruit")
        self.assertEqual(tech_res["upgraded_nodes"][0]["cx"], 750)

        # Verify run_roguelike_post_settlement skips ADB when MAA declares native support
        native_caps = {
            "roguelike_theme_capabilities": {
                "BlackFlow": {"native_reward_claim": True, "native_tech_tree_upgrade": True}
            }
        }
        post_native = run_roguelike_post_settlement(
            "adb.exe", "127.0.0.1:16384", REPO_ROOT, env_doctor.DEFAULT_LAUNCHER_CONFIG, theme="BlackFlow", capabilities=native_caps
        )
        self.assertEqual(post_native["rewards"]["source"], "maa_native")
        self.assertEqual(post_native["tech_tree"]["source"], "maa_native")


if __name__ == "__main__":
    unittest.main()
