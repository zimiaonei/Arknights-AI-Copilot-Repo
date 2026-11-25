import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from universal_engine import master_scheduler
from universal_engine.auto_growth import GrowthDecision
from universal_engine.master_scheduler import (
    MaaEngine,
    get_arknights_week_key,
    run_annihilation_pipeline,
    run_sanity_fight_pipeline,
)
from universal_engine.utils import env_doctor


class MasterSchedulerTests(unittest.TestCase):
    def test_sanity_fight_anti_deadlock_skips_locked_event_stages_fast_to_1_7(self):
        fake_sess = MagicMock()
        fake_sess.cancel_event = None
        fake_sess.adb_path = ""
        fake_sess.adb_addr = ""
        fail_res = {
            "ok": False,
            "timed_out": False,
            "cancelled": False,
            "elapsed": 2.0,
            "events": [{"message": "SubTaskError", "details": {"first": ["FightBegin"]}}],
            "chain_errors": [],
            "sub_errors": [{}],
            "log_lines": [],
        }
        ok_res = {
            "ok": True,
            "timed_out": False,
            "cancelled": False,
            "elapsed": 10.0,
            "events": [{"message": "SubTaskExtraInfo", "details": {"what": "StageDrops", "details": {"stage": {"stageCode": "1-7"}, "stats": []}}}],
            "chain_errors": [],
            "sub_errors": [],
            "log_lines": [],
        }
        fake_sess.run_single_task.side_effect = [fail_res, ok_res]
        holder = {"session": fake_sess}
        cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG
        state = {"last_battle_is_annihilation": False}

        with patch("universal_engine.master_scheduler.save_state"):
            # auto_push=False: TL-9 fails FightBegin -> NEVER retries TL-9, skips locked TL-8/TL-7, runs 1-7 directly
            out = run_sanity_fight_pipeline(
                holder, cfg, state=state, candidate_stages=["TL-9", "TL-8", "TL-7", "1-7"], reason="test", daily_mode=True
            )
            self.assertEqual(out["selected_stage"], "1-7")
            self.assertEqual(out["recommended_stage"], "TL-9")
            self.assertEqual([a["stage"] for a in out["attempted"]], ["TL-9", "1-7"])
            self.assertEqual(fake_sess.run_single_task.call_count, 2)

    def test_daily_auto_push_uses_frontier_and_falls_back_to_1_7_on_tutorial_or_entry_fail(self):
        import threading

        def _make_sess(maa_dir: Path):
            s = MagicMock()
            s.maa_dir = maa_dir
            s.adb_path = "adb.exe"
            s.adb_addr = "127.0.0.1:16384"
            s.cancel_event = threading.Event()
            return s

        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(json.dumps([
                {"code": f"TL-{i}", "stageId": f"act55side_0{i}", "apCost": 18} for i in range(1, 10)
            ]), encoding="utf-8")

            fail_fight = {
                "ok": False, "timed_out": False, "cancelled": False, "elapsed": 15.0,
                "events": [{"message": "SubTaskError", "details": {"first": ["FightBegin"]}}],
                "chain_errors": [{}], "sub_errors": [{}], "log_lines": [],
            }
            ok_1_7 = {
                "ok": True, "timed_out": False, "cancelled": False, "elapsed": 8.0,
                "events": [{"message": "SubTaskExtraInfo", "details": {"what": "StageDrops", "details": {"stage": {"stageCode": "1-7"}, "stats": []}}}],
                "chain_errors": [], "sub_errors": [], "log_lines": [],
            }

            # Case A: frontier is TL-TR-1 tutorial -> MUST abort push without running Copilot and fall back to 1-7
            s1 = _make_sess(maa)
            s1.run_single_task.return_value = fail_fight
            s2 = _make_sess(maa)
            s2.connect.return_value = True
            s2.run_single_task.side_effect = [{"ok": True, "events": [], "log_lines": []}, ok_1_7]
            holder = {"session": s1}
            with patch("universal_engine.master_scheduler.stage_pilot.detect_map_frontier_stage", return_value={
                "on_stage_map": True, "visible_nodes": [], "target_visible": False, "target_cleared": False,
                "frontier_stage": "TL-TR-1", "frontier_type": "tutorial", "push_sequence": ["TL-TR-1", "TL-1"],
            }), \
                 patch("universal_engine.master_scheduler.autonomous_clear_stage") as mock_push, \
                 patch("universal_engine.master_scheduler.MaaSession", return_value=s2), \
                 patch("universal_engine.master_scheduler.save_state"):
                out = run_sanity_fight_pipeline(
                    holder, env_doctor.DEFAULT_LAUNCHER_CONFIG, state={},
                    candidate_stages=["TL-9", "TL-8", "PR-C-2", "1-7"], auto_push=True, daily_mode=True,
                )
            mock_push.assert_not_called()
            self.assertEqual(out["selected_stage"], "1-7")

            # Case B: frontier is TL-2 -> pushes TL-2 first (NOT TL-9); if TL-2 fails entry, immediately falls back to 1-7
            s3 = _make_sess(maa)
            s3.run_single_task.return_value = fail_fight
            s4 = _make_sess(maa)
            s4.connect.return_value = True
            s4.run_single_task.side_effect = [{"ok": True, "events": [], "log_lines": []}, ok_1_7]
            holder2 = {"session": s3}
            pushed_stages = []

            def _fail_tl2(st, **kwargs):
                pushed_stages.append((st, kwargs.get("daily_mode")))
                return False, {"stage": st, "victory": False, "stage_entry_failed": True}

            with patch("universal_engine.master_scheduler.stage_pilot.detect_map_frontier_stage", return_value={
                "on_stage_map": True, "visible_nodes": [], "target_visible": False, "target_cleared": False,
                "frontier_stage": "TL-2", "frontier_type": "battle", "push_sequence": ["TL-2", "TL-3", "TL-9"],
            }), \
                 patch("universal_engine.master_scheduler.stage_pilot.discover_unlocked_special_prereqs", return_value=[]), \
                 patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fail_tl2), \
                 patch("universal_engine.master_scheduler.MaaSession", return_value=s4), \
                 patch("universal_engine.master_scheduler.save_state"):
                out2 = run_sanity_fight_pipeline(
                    holder2, env_doctor.DEFAULT_LAUNCHER_CONFIG, state={},
                    candidate_stages=["TL-9", "TL-8", "1-7"], auto_push=True, daily_mode=True,
                )
            self.assertEqual(pushed_stages, [("TL-2", True)])
            self.assertEqual(out2["selected_stage"], "1-7")

            # Case C (live log repro): cleared TL-TR-1 and uncleared TL-2 coexist on first screen ->
            # MUST NOT misjudge TL-TR-1 as blocking; proceeds directly to push TL-2!
            from PIL import Image
            canvas = Image.new("RGB", (1280, 720), (35, 38, 46))
            for bx, by in ((94, 346), (325, 347), (569, 385)):
                for px in range(bx - 40, bx - 10):
                    for py in range(by - 8, by + 22):
                        canvas.putpixel((px, py), (25, 145, 240))
            ocr_boxes = [
                {"text": "TL-ST-1", "norm": "TL-ST-1", "x": 94, "y": 346, "w": 91, "h": 20, "pass": "norm"},
                {"text": "TL-TR-1", "norm": "TL-TR-1", "x": 325, "y": 347, "w": 90, "h": 19, "pass": "tut_inv"},
                {"text": "TL-1", "norm": "TL-1", "x": 569, "y": 385, "w": 53, "h": 20, "pass": "norm"},
                {"text": "TL-2", "norm": "TL-2", "x": 767, "y": 438, "w": 53, "h": 20, "pass": "norm"},
            ]
            s5 = _make_sess(maa)
            s5.run_single_task.return_value = fail_fight
            s6 = _make_sess(maa)
            s6.connect.return_value = True
            s6.run_single_task.side_effect = [{"ok": True, "events": [], "log_lines": []}, ok_1_7]
            holder3 = {"session": s5}
            pushed_c = []
            logs_c = []

            def _record_push(st, **kwargs):
                pushed_c.append(st)
                return False, {"stage": st, "victory": False, "stage_entry_failed": True}

            with patch("universal_engine.stage_pilot.adb_screencap_image", return_value=canvas), \
                 patch("universal_engine.stage_pilot.analyze_screen_state", return_value={"on_stage_map": True}), \
                 patch("universal_engine.stage_pilot.find_stage_boxes_via_ocr", return_value=ocr_boxes), \
                 patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_record_push), \
                 patch("universal_engine.master_scheduler.MaaSession", return_value=s6), \
                 patch("universal_engine.master_scheduler.save_state"):
                run_sanity_fight_pipeline(
                    holder3, env_doctor.DEFAULT_LAUNCHER_CONFIG, state={},
                    candidate_stages=["TL-9", "1-7"], auto_push=True, daily_mode=True,
                    log_callback=logs_c.append,
                )
            self.assertEqual(pushed_c, ["TL-2"])
            self.assertFalse(any("TL-TR-1 阻塞" in line for line in logs_c))

    def test_cancelled_fight_never_reports_unrun_stage_as_selected(self):
        import threading
        cancel = threading.Event()
        fake_sess = MagicMock()
        fake_sess.cancel_event = cancel
        fake_sess.adb_path = ""
        fake_sess.adb_addr = ""

        def _fail_then_cancel(task_type, params, timeout_sec=1800, tick_callback=None):
            cancel.set()
            return {"ok": False, "cancelled": True, "elapsed": 0.0, "events": [], "log_lines": [], "chain_errors": [], "sub_errors": []}

        fake_sess.run_single_task.side_effect = _fail_then_cancel
        out = run_sanity_fight_pipeline(
            {"session": fake_sess}, env_doctor.DEFAULT_LAUNCHER_CONFIG, state={},
            candidate_stages=["TL-9", "TL-8", "1-7"], daily_mode=True,
        )
        self.assertTrue(out.get("cancelled"))
        self.assertIsNone(out["selected_stage"])
        self.assertEqual(out["recommended_stage"], "TL-9")
        self.assertEqual(fake_sess.run_single_task.call_count, 1)

    def test_daily_pipeline_isolates_fight_failure_returns_home_and_always_runs_award_and_reception_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"daily": {"skip_tasks": ["startup", "recruit", "mall", "annihilation"]}}, cfg_path)

            sess = MagicMock()
            sess.cancel_event = None
            sess.recruit_alerts = []
            sess.adb_path = "adb.exe"
            sess.adb_addr = "127.0.0.1:16384"
            sess.maa_dir = maa

            def _fake_task(task_type, params, timeout_sec=1800, tick_callback=None):
                if task_type == "Infrast":
                    return {
                        "ok": True, "cancelled": False, "elapsed": 5.0,
                        "events": [{"message": "SubTaskError", "details": {"first": ["UnlockClues"]}}],
                        "log_lines": ["vacancy_cnt: 7 , available: 6"],
                    }
                if task_type == "StartUp":
                    return {"ok": True, "cancelled": False, "elapsed": 1.0, "events": [], "log_lines": []}
                if task_type == "Award":
                    return {"ok": True, "cancelled": False, "elapsed": 2.0, "events": [], "log_lines": []}
                return {"ok": True, "cancelled": False, "elapsed": 1.0, "events": [], "log_lines": []}

            sess.run_single_task.side_effect = _fake_task

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base), \
                 patch.object(MaaEngine, "_connect_session", return_value=(maa, sess, "")), \
                 patch.object(MaaEngine, "_prepare_session_and_auto_targets", side_effect=RuntimeError("fight crash")), \
                 patch("universal_engine.master_scheduler.stage_pilot.ensure_reception_clues_processed", return_value={"ok": True, "quick_insert_clicked": 1, "unlock_clues_clicked": True}) as mock_rec, \
                 patch("universal_engine.master_scheduler.stage_pilot.return_to_home_screen", return_value={"ok": True, "method": "quick_switch"}) as mock_home:
                engine = MaaEngine(config_path=cfg_path)
                report = engine.run_daily(dry_run_post_action=True)

            # Bug 4: Reception fallback ran after Infrast skipped QuickInsert
            mock_rec.assert_called_once()
            self.assertEqual(report["tasks"]["Infrast"]["reception_fallback"]["quick_insert_clicked"], 1)
            self.assertTrue(report["tasks"]["Infrast"]["reception_fallback"]["unlock_clues_clicked"])
            # Bug 3: Fight exception was isolated, returned home, and Award STILL ran and claimed rewards!
            self.assertIn("fight crash", report["tasks"]["Fight"].get("error", ""))
            mock_home.assert_called_once()
            self.assertTrue(report["tasks"]["Award"]["claimed"])

    def test_engine_check_and_auto_growth_ttl_sync_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                engine = MaaEngine(config_path=cfg_path)
                report = engine.check_environment(check_adb=False)
                self.assertIn("base_dir", report)
                self.assertIn("write_permission", report)

                with patch.object(engine, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch("universal_engine.master_scheduler.is_data_stale", return_value={"depot_stale": True, "box_stale": True}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch(
                         "universal_engine.master_scheduler.ensure_emulator_ready",
                         return_value={"ready": True, "adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384", "connection_config": "MuMuEmulator12"},
                     ), \
                     patch("universal_engine.master_scheduler.ensure_game_running"), \
                     patch("universal_engine.master_scheduler.sync_my_box", return_value={"e2_count": 0}), \
                     patch("universal_engine.master_scheduler.load_depot_data", return_value={"data": {}}), \
                     patch("universal_engine.master_scheduler.MaaSession") as mock_sess_cls, \
                     patch("universal_engine.master_scheduler.decide_optimal_stages") as mock_decide:
                    sess = mock_sess_cls.return_value
                    sess.connect.return_value = True
                    sess.run_single_task.return_value = {
                        "ok": True,
                        "events": [{"message": "SubTaskExtraInfo", "details": {"what": "StageDrops", "details": {"stage": {"stageCode": "1-7"}, "stats": []}}}],
                        "log_lines": [],
                        "elapsed": 1.0,
                        "chain_errors": [],
                        "sub_errors": [],
                    }
                    mock_decide.return_value = GrowthDecision(recommended_stages=["1-7"], reason="fallback")
                    res = engine.run_fight(fight_mode="AUTO", dry_run_post_action=True)
                    submitted = [c.args[0] for c in sess.run_single_task.call_args_list]
                    self.assertIn("Depot", submitted)
                    self.assertIn("OperBox", submitted)
                    self.assertIn("Fight", submitted)
                    self.assertEqual(res["selected_stage"], "1-7")

    def test_annihilation_cached_skip_and_cli_check_mode(self):
        wk = get_arknights_week_key()
        state = {"annihilation": {"week_key": wk, "weekly_process": [1800, 1800]}}
        holder = {"session": MagicMock()}
        res = run_annihilation_pipeline(holder, env_doctor.DEFAULT_LAUNCHER_CONFIG, state)
        self.assertEqual(res["status"], "already_1800_cached")
        holder["session"].run_single_task.assert_not_called()

        buf = io.StringIO()
        with patch.object(MaaEngine, "check_environment", return_value={"healthy": True, "base_dir": ".", "maa_integrity": {"valid": True}}), \
             patch.object(MaaEngine, "plan_auto_growth", return_value={"recommended_stages": ["1-7"], "reason": "ok"}), \
             redirect_stdout(buf):
            rc = master_scheduler.main(["--mode", "check", "--no-refresh-cache", "--no-adb-check"])
            self.assertEqual(rc, 0)
            out_json = json.loads(buf.getvalue())
            self.assertTrue(out_json["environment"]["healthy"])

    def test_cancel_event_halts_run_daily_and_skips_post_action(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"post_action": "shutdown"}, cfg_path)

            cancel = threading.Event()
            sess = MagicMock()
            def _cancel_after_startup(task_type, params, timeout_sec=1800):
                cancel.set()
                return {"ok": True, "cancelled": True, "elapsed": 0.1, "events": [], "log_lines": []}
            sess.run_single_task.side_effect = _cancel_after_startup
            sess.recruit_alerts = []
            sess.adb_path = ""

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base), \
                 patch.object(MaaEngine, "_connect_session", return_value=(maa, sess, "")), \
                 patch("universal_engine.master_scheduler.execute_post_action") as mock_post:
                engine = MaaEngine(config_path=cfg_path)
                res = engine.run_daily(cancel_event=cancel, dry_run_post_action=False)
                self.assertTrue(res["cancelled"])
                self.assertFalse(res["ok"])
                self.assertEqual(sess.run_single_task.call_count, 1)
                mock_post.assert_not_called()

    def test_resolve_auto_push_stages_skips_ssreopen_proxy_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (base / "cache" / "gui").mkdir(parents=True)
            (maa / "resource").mkdir(parents=True)
            (base / "cache" / "gui" / "StageActivityV2.json").write_text(json.dumps({
                "Official": {"sideStoryStage": {"SSReopen": {
                    "Activity": {"UtcStartTime": "2026/01/01 00:00:00", "UtcExpireTime": "2099/01/01 00:00:00", "TimeZone": 8},
                    "Stages": [
                        {"Display": "SSReopen-AT", "Value": "SSReopen-AT", "Drop": "代理1~8关"},
                        {"Display": "AT-8", "Value": "AT-8", "Drop": "31093"},
                    ],
                }}}
            }), encoding="utf-8")
            (maa / "resource" / "stages.json").write_text(json.dumps([
                {"code": "AT-1", "stageId": "act_at_1"},
                {"code": "AT-8", "stageId": "act_at_8"},
            ]), encoding="utf-8")
            stages = master_scheduler._resolve_auto_push_stages(maa, base, "Official")
            self.assertEqual(stages, ["AT-1", "AT-8"])

    def test_reconnect_failure_after_push_does_not_leave_closed_session_and_cli_preserves_config(self):
        cur_sess = MagicMock()
        cur_sess.maa_dir = Path(".")
        fail_res = {
            "ok": False, "events": [], "chain_errors": [], "sub_errors": [],
            "log_lines": ["PipelineAnalyzer::analyze UnableToAgent"],
        }
        cur_sess.run_single_task.return_value = fail_res
        bad_new_sess = MagicMock()
        bad_new_sess.connect.return_value = False
        holder = {"session": cur_sess}

        with patch("universal_engine.master_scheduler.autonomous_clear_stage", return_value=(True, {})), \
             patch("universal_engine.master_scheduler.MaaSession", return_value=bad_new_sess):
            out = run_sanity_fight_pipeline(holder, env_doctor.DEFAULT_LAUNCHER_CONFIG, candidate_stages=["TL-9", "1-7"], auto_push=True)
            self.assertIsNone(out["selected_stage"])
            self.assertTrue(holder.get("session_lost"))
            # Should NOT attempt "1-7" on a closed/disconnected session
            self.assertEqual(len(out["attempted"]), 0)

        # CLI --mode fight without --stage/--medicine/--stone preserves config values and exits 2 on failure
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "launcher_config.json"
            env_doctor.save_launcher_config({"daily": {"override_stage": "4-7", "medicine": 2, "stone": 1}}, cfg_path)
            with patch.object(MaaEngine, "run_fight", return_value={"selected_stage": None}) as mock_fight:
                rc = master_scheduler.main(["--config", str(cfg_path), "--mode", "fight", "--no-refresh-cache"])
                self.assertEqual(rc, 2)
                self.assertIsNone(mock_fight.call_args.kwargs["stage"])
                self.assertIsNone(mock_fight.call_args.kwargs["medicine"])
                self.assertIsNone(mock_fight.call_args.kwargs["stone"])


if __name__ == "__main__":
    unittest.main()
