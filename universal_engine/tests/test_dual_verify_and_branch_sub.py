import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from universal_engine import ai_strategist, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class DualVerifyAndBranchSubTests(unittest.TestCase):
    # ==================================================================
    # 1. Dual hard victory verification: TaskChainCompleted on defeat MUST be battle_failed
    # ==================================================================
    def test_task_chain_completed_without_settlement_or_on_defeat_is_battle_failed(self):
        # Exact TL-2 Round 2 Copilot callback sequence: BattleProcessTask ends on defeat -> TaskChainCompleted, zero errors
        bare_completed_events = [
            {"message": "TaskChainStart", "details": {"taskchain": "Copilot"}},
            {"message": "SubTaskStart", "details": {"subtask": "BattleProcessTask", "details": {}}},
            {"message": "SubTaskCompleted", "details": {"subtask": "BattleProcessTask", "details": {}}},
            {"message": "TaskChainCompleted", "details": {"taskchain": "Copilot"}},
            {"message": "AllTasksCompleted", "details": {"taskchain": "Copilot"}},
        ]
        # Noise from PipelineAnalyzer to_be_recognized must NOT trigger false victory or defeat
        noisy_logs = [
            '[2026-11-21 21:14:40.000][INF] PipelineAnalyzer | {"to_be_recognized":["EndOfAction","StageDrops","MissionFailed"]}',
            '[2026-11-21 21:14:42.000][INF] BattleProcessTask::wait_until_end | pause_button_analyze count == 0',
        ]
        m = ai_strategist.parse_battle_metrics(noisy_logs, bare_completed_events, entered_formation=True)
        self.assertTrue(m["completed_flag"])
        self.assertFalse(m["failed_flag"])
        self.assertFalse(m["victory_settlement_confirmed"])

        # Check 1 fails even if map_verify is omitted: TaskChainCompleted alone can NEVER count as victory
        verdict_no_settle = ai_strategist.evaluate_copilot_victory(
            m,
            post_battle_state={"ok": True, "outcome": "map", "battle_failed": False, "battle_victory_seen": False},
            map_verify={"verified": False, "skipped": False, "current_stage_cleared": False, "next_stage_unlocked": False, "reason": "地图上 TL-2 未变蓝标且 TL-3 未解锁"},
            require_map_verify=True,
        )
        self.assertFalse(verdict_no_settle["victory"])
        self.assertTrue(verdict_no_settle["battle_failed"])
        self.assertFalse(verdict_no_settle["settlement_ok"])
        self.assertFalse(verdict_no_settle["map_ok"])

        # Explicit defeat / agent-locked tokens in logs or callbacks immediately mark battle_failed
        for tok in ("MissionFailed", "OperationFailed", "行动失败", "代理未解锁", "UnableToAgent"):
            m_fail = ai_strategist.parse_battle_metrics([f"[INF] Settlement result: {tok}"], bare_completed_events)
            v_fail = ai_strategist.evaluate_copilot_victory(
                m_fail,
                post_battle_state={"battle_victory_seen": True},
                map_verify={"verified": True, "skipped": False},
                require_map_verify=True,
            )
            self.assertFalse(v_fail["victory"], msg=f"Token {tok} should block victory")
            self.assertTrue(v_fail["battle_failed"], msg=f"Token {tok} should set battle_failed")
            self.assertFalse(v_fail["settlement_ok"], msg=f"Token {tok} should fail settlement check")

        # Settlement OK (victory screen seen) YET Map closed-loop says TL-2 still uncleared & TL-3 locked -> STILL battle_failed!
        v_map_fail = ai_strategist.evaluate_copilot_victory(
            m,
            post_battle_state={"ok": True, "outcome": "map", "battle_failed": False, "battle_victory_seen": True, "settlement_stars": 3},
            map_verify={"verified": False, "skipped": False, "current_stage_cleared": False, "next_stage_unlocked": False, "reason": "TL-2 未变蓝标且 TL-3 未解锁"},
            require_map_verify=True,
        )
        self.assertTrue(v_map_fail["settlement_ok"])
        self.assertFalse(v_map_fail["map_ok"])
        self.assertFalse(v_map_fail["victory"])
        self.assertTrue(v_map_fail["battle_failed"])

        # Positive case: BOTH Check 1 (victory settlement seen) AND Check 2 (map verified cleared/unlocked) pass -> victory!
        v_ok = ai_strategist.evaluate_copilot_victory(
            m,
            post_battle_state={"ok": True, "outcome": "map", "battle_failed": False, "battle_victory_seen": True, "settlement_stars": 3},
            map_verify={"verified": True, "skipped": False, "current_stage_cleared": True, "next_stage_unlocked": True},
            require_map_verify=True,
        )
        self.assertTrue(v_ok["victory"])
        self.assertFalse(v_ok["battle_failed"])
        self.assertTrue(v_ok["settlement_ok"])
        self.assertTrue(v_ok["map_ok"])

    def test_verify_stage_cleared_on_real_tl2_false_victory_screenshot(self):
        real_tl2_map = REPO_ROOT / "reports" / "push_TL-2_victory_round_2.png"
        if not (real_tl2_map.is_file() and (REPO_ROOT / "resource" / "template").is_dir()):
            return
        with Image.open(real_tl2_map) as im:
            with patch.object(stage_pilot, "adb_screencap_image", return_value=im):
                mv = stage_pilot.verify_stage_cleared_on_map(
                    "TL-2",
                    "adb.exe",
                    "127.0.0.1:16384",
                    REPO_ROOT,
                    next_stage="TL-3",
                    max_swipes=0,
                )
        self.assertTrue(mv["on_stage_map"])
        self.assertFalse(mv["current_stage_cleared"])
        self.assertFalse(mv["next_stage_unlocked"])
        self.assertFalse(mv["verified"])

    def test_run_copilot_round_intercepts_task_chain_completed_defeat_and_preserves_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            cop = base / "copilots" / "tl2.json"
            cop.parent.mkdir(parents=True)
            cop.write_text(
                json.dumps(
                    {
                        "stage_name": "act55side_02",
                        "opers": [{"name": "棘刺", "skill": 3}],
                        "groups": [],
                        "actions": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_sess = MagicMock()
            fake_sess.connect.return_value = True
            fake_sess.adb_path = "adb.exe"
            fake_sess.adb_addr = "127.0.0.1:16384"
            fake_sess.run_single_task.return_value = {
                "ok": True,
                "cancelled": False,
                "elapsed": 29.1,
                "events": [
                    {"message": "SubTaskStart", "details": {"subtask": "BattleProcessTask", "details": {}}},
                    {"message": "SubTaskCompleted", "details": {"subtask": "BattleProcessTask", "details": {}}},
                    {"message": "TaskChainCompleted", "details": {"taskchain": "Copilot"}},
                ],
                "log_lines": [],
                "story_probe_battle_failed": False,
                "story_probe_battle_victory": False,
            }

            call_order = []

            def _fake_shot(adb_p, adb_a, out_p):
                call_order.append(("screenshot", Path(out_p).name))
                Path(out_p).parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (64, 36), (10, 10, 10)).save(out_p)
                return True

            def _fake_dismiss(*args, **kwargs):
                call_order.append(("dismiss_battle_failed", ""))
                return True

            with patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "init_maa_sdk", return_value=True), \
                 patch.object(maa_runner, "MaaSession", return_value=fake_sess), \
                 patch.object(maa_runner, "capture_adb_screenshot", side_effect=_fake_shot), \
                 patch.object(stage_pilot, "prepare_battle_entry_if_on_map", return_value={"handled": True, "entered": True, "outcome": "formation"}), \
                 patch.object(stage_pilot, "run_story_skip_loop", return_value={"ok": False, "outcome": "battle_failed", "battle_failed": True, "battle_victory_seen": False}), \
                 patch.object(stage_pilot, "dismiss_battle_failed_to_map", side_effect=_fake_dismiss):
                rc, rep = maa_runner._run_copilot_round(
                    1, cop, REPO_ROOT, {}, base, stage_code="TL-2", next_stage="TL-3"
                )

            self.assertEqual(rc, 1)
            self.assertFalse(rep["victory"])
            self.assertTrue(rep["battle_failed"])
            self.assertTrue(rep["metrics"]["failed_flag"])
            self.assertFalse(rep["map_clear_verified"])
            # Failure screenshot MUST be captured BEFORE dismissing Mission Failed screen back to map!
            self.assertEqual([c[0] for c in call_order], ["screenshot", "dismiss_battle_failed"])
            self.assertEqual(call_order[0][1], "fail_round_1.png")

    # ==================================================================
    # 2. Tightened STORY skip coroutine: NEVER mistake Mission Failed for story dialog
    # ==================================================================
    def test_story_skip_never_clicks_mission_failed_or_dark_screen_without_dialog(self):
        dark_defeat_img = Image.new("RGB", (1280, 720), (14, 15, 18))

        # Without OCR / skip templates, dark bottom band alone must NOT trigger has_story_dialog
        st_plain_dark = stage_pilot.analyze_screen_state(dark_defeat_img, REPO_ROOT, use_ocr_fallback=False)
        self.assertFalse(st_plain_dark["has_story_dialog"])
        self.assertFalse(st_plain_dark["has_skip_button"])

        # With OCR reading "行动失败 / MISSION FAILED", must flag is_battle_failed=True & NEVER tap
        taps = []
        with patch.object(
            stage_pilot,
            "run_win_ocr_on_images",
            return_value={
                "tr": [],
                "mid": [{"text": "理智返还"}],
                "banner": [{"text": "行动失败 MISSION FAILED"}],
            },
        ), patch.object(stage_pilot, "adb_screencap_image", return_value=dark_defeat_img), \
           patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda *a, **kw: taps.append(a) or True):
            st_fail = stage_pilot.analyze_screen_state(dark_defeat_img, REPO_ROOT, use_ocr_fallback=True)
            self.assertTrue(st_fail["is_battle_failed"])
            self.assertFalse(st_fail["has_story_dialog"])
            self.assertFalse(st_fail["has_skip_button"])

            probe = stage_pilot.check_and_skip_story_once("adb.exe", "127.0.0.1:16384", REPO_ROOT)
            self.assertFalse(probe["acted"])
            self.assertTrue(probe["battle_failed"])

            loop_res = stage_pilot.run_story_skip_loop(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_states=("map",), max_duration_sec=2.0, poll_interval=0.05
            )
            self.assertFalse(loop_res["ok"])
            self.assertTrue(loop_res["battle_failed"])
            self.assertEqual(loop_res["outcome"], "battle_failed")
            self.assertEqual(taps, [])

        # With OCR reading "行动结束 / MISSION ACCOMPLISHED", must flag is_battle_victory=True
        with patch.object(
            stage_pilot,
            "run_win_ocr_on_images",
            return_value={"tr": [], "mid": [], "banner": [{"text": "行动结束 MISSION ACCOMPLISHED"}]},
        ):
            st_win = stage_pilot.analyze_screen_state(dark_defeat_img, REPO_ROOT, use_ocr_fallback=True)
            self.assertTrue(st_win["is_battle_victory"])
            self.assertTrue(st_win["has_end_of_action"])
            self.assertFalse(st_win["is_battle_failed"])
            self.assertFalse(st_win["has_story_dialog"])

    # ==================================================================
    # 3. Operator substitution: sync opers/groups/actions, forbid 0-block bearer for blocking vanguard,
    #    prefer full-match & 3~6 op guides
    # ==================================================================
    def test_substitution_syncs_opers_groups_actions_and_forbids_bearer_for_blocking_vanguard(self):
        box = {
            "e2_operators": [
                {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                {"name": "极境", "elite": 2, "level": 50, "rarity": 5},
                {"name": "琴柳", "elite": 2, "level": 60, "rarity": 6},
                {"name": "缪尔赛思", "elite": 2, "level": 60, "rarity": 6},
                {"name": "嵯峨", "elite": 2, "level": 60, "rarity": 6},
                {"name": "晓歌", "elite": 2, "level": 60, "rarity": 5},
                {"name": "星熊", "elite": 2, "level": 60, "rarity": 6},
            ],
            "all_owned": {
                "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                "德克萨斯": {"name": "德克萨斯", "elite": 1, "level": 50, "rarity": 5},
                "桃金娘": {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                "极境": {"name": "极境", "elite": 2, "level": 50, "rarity": 5},
                "琴柳": {"name": "琴柳", "elite": 2, "level": 60, "rarity": 6},
                "缪尔赛思": {"name": "缪尔赛思", "elite": 2, "level": 60, "rarity": 6},
                "嵯峨": {"name": "嵯峨", "elite": 2, "level": 60, "rarity": 6},
                "晓歌": {"name": "晓歌", "elite": 2, "level": 60, "rarity": 5},
                "星熊": {"name": "星熊", "elite": 2, "level": 60, "rarity": 6},
            },
        }
        copilot = {
            "stage_name": "act55side_02",
            "opers": [
                {"name": "棘刺", "skill": 3, "skill_usage": 1, "requirements": {"elite": 2, "skill_level": 7}},
                {"name": "德克萨斯", "skill": 2, "skill_usage": 2, "requirements": {"elite": 1, "skill_level": 7}},
            ],
            "groups": [
                {"name": "塞雷娅", "opers": [{"name": "塞雷娅", "skill": 1, "skill_usage": 0}]},
            ],
            "actions": [
                {"type": "Deploy", "name": "德克萨斯", "location": [8, 2], "direction": "Down"},
                {"type": "SkillUsage", "name": "德克萨斯", "skill_usage": 2},
                {"type": "Skill", "name": "德克萨斯", "location": [8, 2]},
                {"type": "Deploy", "name": "棘刺", "location": [7, 3], "direction": "Down"},
                {"type": "Deploy", "name": "塞雷娅", "location": [6, 3], "direction": "Left"},
                {"type": "Skill", "name": "塞雷娅"},
                {"type": "Retreat", "name": "德克萨斯"},
            ],
        }
        maa_dir = REPO_ROOT if (REPO_ROOT / "resource" / "battle_data.json").is_file() else None
        sub_res = ai_strategist.substitute_unavailable_operators_in_copilot(
            copilot, ["德克萨斯", "塞雷娅"], box, maa_dir=maa_dir
        )
        self.assertTrue(sub_res["ok"])
        sub_vg = sub_res["substitutions"]["德克萨斯"]
        # MUST choose same-branch blocking vanguard (嵯峨/晓歌), NEVER 0-block bearer (桃金娘/极境/琴柳) or ranged tactician (缪尔赛思)!
        self.assertIn(sub_vg, ("嵯峨", "晓歌"))
        self.assertNotIn(sub_vg, ("桃金娘", "极境", "琴柳", "缪尔赛思"))
        self.assertEqual(sub_res["substitutions"]["塞雷娅"], "星熊")

        patched = sub_res["copilot"]
        self.assertNotIn("德克萨斯", [o["name"] for o in patched["opers"]])
        self.assertIn(sub_vg, [o["name"] for o in patched["opers"]])
        self.assertEqual(patched["groups"][0]["name"], "星熊")
        self.assertEqual(patched["groups"][0]["opers"][0]["name"], "星熊")
        # All actions must rewrite old names & drop manual Skill action on passive skill_usage=0 substitute 星熊
        act_names = [a.get("name") for a in patched["actions"]]
        self.assertNotIn("德克萨斯", act_names)
        self.assertNotIn("塞雷娅", act_names)
        self.assertIn(sub_vg, act_names)
        self.assertIn("星熊", act_names)
        self.assertFalse(any(a.get("type") == "Skill" and a.get("name") == "星熊" for a in patched["actions"]))
        # SkillUsage action on substituted vanguard must sync to substitute's skill_usage
        sub_op_entry = next(o for o in patched["opers"] if o["name"] == sub_vg)
        su_acts = [a for a in patched["actions"] if a.get("type") == "SkillUsage" and a.get("name") == sub_vg]
        self.assertEqual(len(su_acts), 1)
        self.assertEqual(su_acts[0]["skill_usage"], sub_op_entry["skill_usage"])

        # Box with ONLY 0-block flagbearers cannot substitute blocking pioneer 德克萨斯
        bearer_only_box = {
            "e2_operators": [
                {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                {"name": "极境", "elite": 2, "level": 50, "rarity": 5},
                {"name": "琴柳", "elite": 2, "level": 60, "rarity": 6},
            ],
            "all_owned": {
                "桃金娘": {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                "极境": {"name": "极境", "elite": 2, "level": 50, "rarity": 5},
                "琴柳": {"name": "琴柳", "elite": 2, "level": 60, "rarity": 6},
            },
        }
        self.assertIsNone(ai_strategist.pick_best_substitute("德克萨斯", bearer_only_box, maa_dir=maa_dir))

    def test_prefers_full_match_3_to_6_op_guide_over_fragile_2_op_guide(self):
        box = {
            "e2_operators": [
                {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                {"name": "嵯峨", "elite": 2, "level": 60, "rarity": 6},
                {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
                {"name": "星熊", "elite": 2, "level": 60, "rarity": 6},
            ],
            "all_owned": {
                "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                "德克萨斯": {"name": "德克萨斯", "elite": 1, "level": 50, "rarity": 5},
                "嵯峨": {"name": "嵯峨", "elite": 2, "level": 60, "rarity": 6},
                "维什戴尔": {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
                "星熊": {"name": "星熊", "elite": 2, "level": 60, "rarity": 6},
            },
        }
        g_2op = {
            "id": 201, "like": 100, "views": 3000,
            "content": {
                "stage_name": "act55side_02",
                "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}, {"name": "德克萨斯", "skill": 1, "skill_usage": 1}],
                "groups": [], "actions": [],
            },
        }
        g_4op = {
            "id": 202, "like": 95, "views": 2500,
            "content": {
                "stage_name": "act55side_02",
                "opers": [
                    {"name": "棘刺", "skill": 3, "skill_usage": 1},
                    {"name": "嵯峨", "skill": 2, "skill_usage": 1},
                    {"name": "维什戴尔", "skill": 3, "skill_usage": 1},
                    {"name": "星熊", "skill": 2, "skill_usage": 0},
                ],
                "groups": [], "actions": [],
            },
        }
        a_2op = ai_strategist._adapt_guide_deterministic(g_2op, box, "act55side_02")
        a_4op = ai_strategist._adapt_guide_deterministic(g_4op, box, "act55side_02")
        self.assertGreater(a_4op["compat_score"], a_2op["compat_score"])

        # During formation repair when 德克萨斯 is Unavailable (not relaxable), must prioritize switching to full-match g_4op BEFORE substituting g_2op!
        fail_rep = {
            "metrics": {
                "kills": None,
                "errors": ['SubTaskError:BattleFormationTask:{"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}}'],
                "failed_flag": False,
                "completed_flag": False,
            },
            "events_tail": [],
            "log_tail": [],
        }
        idx_next, cop_next, strat_next = ai_strategist.repair_copilot(
            [a_2op, a_4op],
            0,
            a_2op["copilot"],
            fail_rep,
            box,
            round_idx=1,
            raw_guides_by_id={201: g_2op, 202: g_4op},
        )
        self.assertEqual(idx_next, 1)
        self.assertEqual(strat_next["repair_action"], "switch_guide")
        self.assertTrue(strat_next["full_native_match"])
        self.assertEqual(strat_next["switched_guide_id"], 202)
        self.assertEqual([o["name"] for o in cop_next["opers"]], ["棘刺", "嵯峨", "维什戴尔", "星熊"])

    # ==================================================================
    # 4. Stage selection consistency guard: forbid bare Copilot without stage_pilot entry
    # ==================================================================
    def test_stage_entry_consistency_forbids_bare_copilot_and_enforces_fresh_map_entry(self):
        # A. _run_copilot_round refuses to run Copilot when prepare_battle_entry_if_on_map returns entered=False
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            cop = base / "copilots" / "tl3.json"
            cop.parent.mkdir(parents=True)
            cop.write_text(
                json.dumps(
                    {
                        "stage_name": "act55side_02",
                        "opers": [{"name": "棘刺", "skill": 3}],
                        "groups": [],
                        "actions": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            fake_sess = MagicMock()
            fake_sess.connect.return_value = True
            fake_sess.adb_path = "adb.exe"
            fake_sess.adb_addr = "127.0.0.1:16384"

            with patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "init_maa_sdk", return_value=True), \
                 patch.object(maa_runner, "MaaSession", return_value=fake_sess), \
                 patch.object(maa_runner, "capture_adb_screenshot", return_value=False), \
                 patch.object(stage_pilot, "prepare_battle_entry_if_on_map", return_value={
                     "handled": False, "entered": False, "reason": "未能在当前地图识别到关卡节点 TL-3"
                 }):
                rc, rep = maa_runner._run_copilot_round(
                    1, cop, REPO_ROOT, {}, base, stage_code="TL-3", require_fresh_map_entry=True
                )
            self.assertEqual(rc, 1)
            self.assertFalse(rep["victory"])
            self.assertTrue(rep["stage_entry_failed"])
            self.assertTrue(ai_strategist.is_stage_entry_failure(rep))
            fake_sess.run_single_task.assert_not_called()

        # B. prepare_battle_entry_if_on_map(require_fresh_map_entry=True) exits stale formation to locate & verify TL-3
        dummy = Image.new("RGB", (1280, 720), (30, 30, 30))
        taps = []
        states = [
            {"in_battle": False, "in_formation": True, "in_formation_select": False, "return_xy": (89, 38), "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False, "on_stage_map": False, "has_stage_start_btn": False},
            {"in_battle": False, "in_formation": False, "in_formation_select": False, "return_xy": (89, 38), "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False, "on_stage_map": True, "has_stage_start_btn": False},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=states), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=(780, 440, {"x": 755, "y": 430, "w": 50, "h": 20})), \
             patch.object(stage_pilot, "enter_stage_node", return_value={"entered": True, "title_verified": True, "displayed_stage": "TL-3"}) as mock_enter, \
             patch.object(stage_pilot, "run_story_skip_loop", return_value={"ok": True, "outcome": "formation"}), \
             patch("time.sleep"):
            prep = stage_pilot.prepare_battle_entry_if_on_map(
                "TL-3", "adb.exe", "127.0.0.1:16384", REPO_ROOT, require_fresh_map_entry=True
            )
        self.assertTrue(prep["entered"])
        self.assertEqual(taps, [(89, 38)])
        mock_enter.assert_called_once()

        # C. MaaEngine.run_push passes require_fresh_map_entry=True on subsequent stages & stops if map_clear_verified=False
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            seen = []

            def _fake_clear(st, **kwargs):
                seen.append((st, kwargs.get("require_fresh_map_entry")))
                if st == "TL-2":
                    # Simulate stale/unverified map clear -> master_scheduler must refuse to advance to TL-3!
                    return True, {"stage": "TL-2", "victory": True, "map_clear_verified": False}
                return True, {"stage": st, "victory": True, "map_clear_verified": True}

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res = eng.run_push(["TL-2", "TL-3"], refresh_cache_flag=False)

            self.assertFalse(res["ok"])
            self.assertEqual(seen, [("TL-2", False)])
            self.assertEqual(res.get("skipped_stages"), ["TL-3"])


if __name__ == "__main__":
    unittest.main()
