import io
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from universal_engine import launcher_gui, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import engine_lock, env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _paste_tpl(canvas: Image.Image, template_rel: str, xy: tuple[int, int]) -> None:
    tpl_path = REPO_ROOT / "resource" / "template" / template_rel
    with Image.open(tpl_path) as tpl:
        canvas.paste(tpl.convert("RGB"), xy)


def _draw_three_blue_stars(canvas: Image.Image) -> None:
    # Draw 3 distinct bright blue stars in normalized x in [0.86, 0.96], y in [0.10, 0.16]
    for sx in (1105, 1150, 1195):
        for dx in range(-8, 9):
            for dy in range(-8, 9):
                if abs(dx) + abs(dy) <= 9:
                    canvas.putpixel((sx + dx, 100 + dy), (40, 175, 250))


class EngineLockRecoveryAndStageGuardTests(unittest.TestCase):
    # ==================================================================
    # 1. Cross-process single-instance mutex lock & dead-PID cleanup
    # ==================================================================
    def test_cross_process_lock_blocks_gui_cli_dual_launch_and_cleans_stale_deadlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa_dir = Path(tmp) / "MAA"
            cli_base = maa_dir / "universal_engine"
            (maa_dir / "config").mkdir(parents=True)
            (cli_base / "config").mkdir(parents=True)
            (maa_dir / "MaaCore.dll").write_bytes(b"fake-dll")
            cfg_path = cli_base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"maa_path": str(maa_dir), "first_run_done": True}, cfg_path)

            # Spawn a real live helper process to hold a genuine live PID
            sleeper = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(15)"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                # Simulate GUI (frozen at maa_dir) holding <maa_dir>/config/engine.lock
                lock_file = maa_dir / "config" / "engine.lock"
                lock_file.write_text(
                    json.dumps(
                        {
                            "pid": sleeper.pid,
                            "source": "gui",
                            "mode": "push",
                            "task": "push:TL-2",
                            "started_at": "2026-11-21T20:00:00",
                            "timestamp": 1700000000.0,
                            "token": "live-gui-token",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                self.assertTrue(engine_lock.is_pid_alive(sleeper.pid))

                # CLI running under cli_base MUST detect <maa_dir>/config/engine.lock and refuse immediately
                out_buf = io.StringIO()
                err_buf = io.StringIO()
                with patch.object(env_doctor, "get_base_dir", return_value=cli_base), \
                     patch.object(master_scheduler, "get_base_dir", return_value=cli_base), \
                     patch.object(master_scheduler, "resolve_maa_path", return_value=maa_dir), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True) as mock_sdk, \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready") as mock_emu, \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage") as mock_clear, \
                     redirect_stdout(out_buf), redirect_stderr(err_buf):
                    rc = master_scheduler.main(
                        ["--mode", "push", "--stages", "TL-2", "--config", str(cfg_path), "--no-refresh-cache"]
                    )
                self.assertEqual(rc, 3)
                self.assertIn("[LOCK]", err_buf.getvalue())
                self.assertIn(f"PID={sleeper.pid}", err_buf.getvalue())
                self.assertIn("mode=push", err_buf.getvalue())
                parsed = json.loads(out_buf.getvalue())
                self.assertTrue(parsed.get("locked"))
                self.assertFalse(parsed.get("ok"))
                mock_sdk.assert_not_called()
                mock_emu.assert_not_called()
                mock_clear.assert_not_called()

                # GUI launcher pre-start check must ALSO immediately reject without touching ADB
                orig_engine = master_scheduler.MaaEngine
                gui_eng = orig_engine(config_path=cfg_path, launch_source="gui")
                gui_eng.base_dir = maa_dir
                LauncherController = getattr(launcher_gui, "LauncherController", None)
                WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
                if LauncherController is None:
                    from universal_engine.gui.controller import LauncherController
                    from universal_engine.gui.bridge import WebViewJsApi

                with patch.object(launcher_gui, "collect_dashboard_snapshot", return_value={
                    "config": env_doctor.load_launcher_config(cfg_path),
                    "maa_dir": str(maa_dir),
                    "integrity": {"valid": True, "maa_version": "v6.22.0", "reason": "ok"},
                    "my_box": {"total_owned": 0, "e2_count": 0},
                    "depot": {"syncTime": "", "count": 0},
                    "growth_preview": {"ok": True, "recommended_stages": ["1-7"], "reason": "test"},
                    "week_key": "2026-W47",
                    "annihilation_process": None,
                    "adb_info": {"connected": True, "adb_address": "127.0.0.1:16384", "width": 1280, "height": 720, "is_16_9": True},
                }):
                    controller = LauncherController(engine=gui_eng, auto_start_refresh=False)
                    api = WebViewJsApi(controller)
                    called_fn = MagicMock(return_value={"ok": True})
                    res = controller._run_background_task("测试推图", called_fn)
                    called_fn.assert_not_called()
                    self.assertFalse(controller.task_running)

                    events = controller.drain_events()
                    toasts = [e["payload"] for e in events if e.get("type") == "toast"]
                    logs_in_events = [item["text"] for e in events if e.get("type") == "log_batch" for item in e.get("payload", [])]
                    all_text = " ".join([t.get("message", "") for t in toasts] + logs_in_events + [l.get("text", "") for l in controller.logs])
                    self.assertIn("[LOCK]", all_text)
                    self.assertIn(f"PID={sleeper.pid}", all_text)
            finally:
                sleeper.terminate()
                sleeper.wait(timeout=5)

            # After the owning process dies, stale engine.lock MUST be auto-cleaned and lock acquisition succeeds!
            self.assertFalse(engine_lock.is_pid_alive(sleeper.pid))
            logs: list[str] = []
            with engine_lock.EngineTaskLock(cli_base, mode="push", source="cli", maa_dir=maa_dir, log_callback=logs.append):
                self.assertTrue((maa_dir / "config" / "engine.lock").is_file())
                self.assertTrue((cli_base / "config" / "engine.lock").is_file())
                cur_data = json.loads((maa_dir / "config" / "engine.lock").read_text(encoding="utf-8"))
                self.assertEqual(cur_data["pid"], os.getpid())
                self.assertEqual(cur_data["source"], "cli")
                self.assertEqual(cur_data["mode"], "push")
                # Another thread in the same process must be blocked
                other_err: list[Exception] = []

                def _other_thread():
                    try:
                        with engine_lock.EngineTaskLock(maa_dir, mode="daily", source="gui"):
                            pass
                    except Exception as exc:
                        other_err.append(exc)

                t = threading.Thread(target=_other_thread)
                t.start()
                t.join(timeout=5)
                self.assertEqual(len(other_err), 1)
                self.assertIsInstance(other_err[0], engine_lock.EngineLockError)

                # Same thread re-entry succeeds cleanly
                with engine_lock.EngineTaskLock(cli_base, mode="sync", source="cli", maa_dir=maa_dir):
                    pass

            # Released cleanly on exit
            self.assertFalse((maa_dir / "config" / "engine.lock").exists())
            self.assertFalse((cli_base / "config" / "engine.lock").exists())
            self.assertTrue(any("[LOCK]" in l for l in logs))

    # ==================================================================
    # 2. Formation select stuck page detection + Home/Back reset + 1 retry
    # ==================================================================
    def test_formation_select_stuck_detection_and_home_recovery_retry(self):
        # A. Verify real stuck screenshot (if present) and synthetic formation-select screen
        real_stuck = REPO_ROOT / "universal_engine" / "reports" / "push_TL-1_fail_round_1.png"
        if real_stuck.is_file() and (REPO_ROOT / "resource" / "template").is_dir():
            with Image.open(real_stuck) as im:
                st = stage_pilot.analyze_screen_state(im, REPO_ROOT, use_ocr_fallback=False)
                self.assertTrue(st["in_formation_select"])
                self.assertFalse(st["on_stage_map"])
                abn = stage_pilot.detect_abnormal_subpage(im, REPO_ROOT, stage_code="TL-2", use_ocr=False)
                self.assertTrue(abn["is_abnormal"])
                self.assertEqual(abn["page_type"], "formation_select")

        if (REPO_ROOT / "resource" / "template" / "Battle" / "Formation" / "BattleQuickFormation" / "BattleQuickFormationClear.png").is_file():
            stuck_img = Image.new("RGB", (1280, 720), (30, 32, 38))
            _paste_tpl(stuck_img, "ReturnButton/Return.png", (45, 16))
            _paste_tpl(stuck_img, "QuickSwitch/Menu.png", (235, 16))
            _paste_tpl(stuck_img, "Battle/Formation/BattleQuickFormation/BattleQuickFormationClear.png", (385, 650))
            st_syn = stage_pilot.analyze_screen_state(stuck_img, REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st_syn["in_formation_select"])
            self.assertFalse(st_syn["on_stage_map"])
            self.assertTrue(st_syn["has_home_menu"])

        # B. Verify recover_from_abnormal_subpage clicks '<' up to 3x, then falls back to Home house button
        dummy = Image.new("RGB", (1280, 720), (20, 20, 20))
        taps: list[tuple[int, int]] = []
        detect_seq = [
            {"is_abnormal": True, "page_type": "formation_select", "reasons": ["清空选择"], "has_return": True, "return_xy": (89, 38), "has_home_menu": True, "home_menu_xy": (269, 37), "on_stage_map": False},
            {"is_abnormal": True, "page_type": "formation_overview", "reasons": ["StartButton2"], "has_return": True, "return_xy": (89, 38), "has_home_menu": True, "home_menu_xy": (269, 37), "on_stage_map": False},
            {"is_abnormal": True, "page_type": "other_subpage", "reasons": ["still_stuck"], "has_return": True, "return_xy": (89, 38), "has_home_menu": True, "home_menu_xy": (269, 37), "on_stage_map": False},
            {"is_abnormal": True, "page_type": "other_subpage", "reasons": ["still_stuck"], "has_return": True, "return_xy": (89, 38), "has_home_menu": True, "home_menu_xy": (269, 37), "on_stage_map": False},
            {"is_abnormal": False, "page_type": "home", "reasons": [], "has_return": False, "return_xy": (89, 38), "has_home_menu": False, "home_menu_xy": (269, 37), "on_stage_map": False},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(stage_pilot, "detect_abnormal_subpage", side_effect=detect_seq), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "return_to_home_screen", return_value={"ok": True, "steps": 1, "method": "quick_switch"}) as mock_home, \
             patch("time.sleep"):
            rec = stage_pilot.recover_from_abnormal_subpage("adb.exe", "127.0.0.1:16384", REPO_ROOT, stage_code="TL-3", max_back_clicks=3)
        self.assertTrue(rec["needed_recovery"])
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["initial_page"], "formation_select")
        self.assertEqual(rec["final_page"], "home")
        # 3 '<' clicks + 1 top-left house button tap
        self.assertEqual(taps, [(89, 38), (89, 38), (89, 38), (269, 37)])
        mock_home.assert_called_once()

        # C. End-to-end in autonomous_clear_stage: BattleStartPre & kills=None -> recovers from formation_select -> retries ONCE -> wins
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-3", "stageId": "act55side_03", "apCost": 15}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({"total_owned": 1, "e2_count": 1, "e2_operators": [{"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}], "all_owned": {"棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}}}),
                encoding="utf-8",
            )
            guides = [{
                "id": 9010, "like": 50, "views": 1000,
                "content": {"stage_name": "act55side_03", "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}], "groups": [], "actions": []},
            }]
            entry_fail = {
                "round": 1, "victory": False, "elapsed_seconds": 20.0,
                "adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384",
                "metrics": {"kills": None, "costs": None, "last_3_actions": [], "errors": ["SubTaskError:ProcessTask:BattleStartPre"], "failed_flag": False, "completed_flag": False, "stage_entry_failed": True, "entry_failed_tasks": ["BattleStartPre"]},
                "screenshot": "", "report_path": "",
            }
            win_after_recover = {
                "round": 1, "victory": True, "elapsed_seconds": 45.0,
                "adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384",
                "metrics": {"kills": 30, "costs": 20, "last_3_actions": [], "errors": [], "failed_flag": False, "completed_flag": True},
                "screenshot": "", "report_path": "",
            }
            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=[(1, entry_fail), (0, win_after_recover)]) as mock_round, \
                 patch.object(stage_pilot, "recover_from_abnormal_subpage", return_value={"needed_recovery": True, "recovered": True, "initial_page": "formation_select", "final_page": "stage_map", "actions": ["tap_return_1", "tap_return_2"]}) as mock_rec:
                ok, summary = maa_runner.autonomous_clear_stage("TL-3", maa_dir=maa, config={}, max_rounds=1, base_dir=base)
            self.assertTrue(ok)
            self.assertEqual(mock_round.call_count, 2)
            mock_rec.assert_called_once()
            self.assertEqual(summary.get("recovered_from"), "formation_select")

    # ==================================================================
    # 3. Sequential push aborts immediately on prerequisite failure
    # ==================================================================
    def test_sequential_push_aborts_chain_on_prereq_failure_and_writes_collab_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "ai": {"mode": "agent_collab", "enabled": False}}, cfg_path)

            fail_shot = base / "reports" / "push_TL-3_fail_round_1.png"
            fail_shot.parent.mkdir(parents=True)
            Image.new("RGB", (128, 72), (10, 20, 30)).save(fail_shot)

            attempted_stages: list[str] = []
            collab_payloads: list[dict] = []
            logs: list[str] = []

            def _fake_clear(st, **kwargs):
                attempted_stages.append(st)
                return False, {
                    "stage": st,
                    "stage_id": "act55side_03",
                    "victory": False,
                    "stage_entry_failed": True,
                    "screenshot": str(fail_shot),
                    "rounds_used": 2,
                    "history": [{"round": 1, "errors": ["SubTaskError:ProcessTask:BattleStartPre"], "screenshot": str(fail_shot)}],
                    "error": "stage_entry_failed",
                }

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res = eng.run_push(
                        ["TL-3", "TL-4", "TL-5", "TL-6", "TL-7", "TL-8"],
                        refresh_cache_flag=False,
                        log_callback=logs.append,
                        collab_handler=collab_payloads.append,
                    )

            self.assertFalse(res["ok"])
            self.assertTrue(res.get("aborted"))
            self.assertEqual(res.get("aborted_stage"), "TL-3")
            self.assertEqual(res.get("skipped_stages"), ["TL-4", "TL-5", "TL-6", "TL-7", "TL-8"])
            # MUST NOT blindly iterate TL-4 ~ TL-8!
            self.assertEqual(attempted_stages, ["TL-3"])
            self.assertEqual(res.get("abort_screenshot"), str(fail_shot))
            self.assertTrue(any("立即中止整条推图链" in l for l in logs))

            # Verify agent_task.json was written with trigger_reason='stage_entry_failed' and screenshot_path
            task_file = base / "agent_task.json"
            self.assertTrue(task_file.is_file())
            task_data = json.loads(task_file.read_text(encoding="utf-8"))
            self.assertEqual(task_data["trigger_reason"], "stage_entry_failed")
            self.assertEqual(task_data["stage_code"], "TL-3")
            self.assertEqual(task_data["screenshot_path"], str(fail_shot))
            self.assertEqual(task_data["battle_state"]["remaining_stages"], ["TL-4", "TL-5", "TL-6", "TL-7", "TL-8"])
            self.assertEqual(len(collab_payloads), 1)

    # ==================================================================
    # 4.1 Right detail panel title OCR guard (TL-1 shown when target=TL-2)
    # ==================================================================
    def test_stage_detail_title_guard_rejects_tl1_and_reselects_tl2(self):
        dummy = Image.new("RGB", (1280, 720), (25, 28, 35))
        taps: list[tuple[int, int]] = []
        logs: list[str] = []

        # Scenario A: Drawer opens showing TL-1 when target is TL-2 -> MUST refuse Start, close drawer, re-click TL-2 -> verify TL-2 -> click Start!
        screen_states = [
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},  # initial map
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},  # before click 1
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1110, 650), "on_stage_map": True},  # after click 1 -> wrong drawer (TL-1)!
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},  # before click 2
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1110, 650), "on_stage_map": True},  # after click 2 -> correct drawer (TL-2)!
        ]
        panels = [
            {"displayed_stage": "TL-1", "raw_title_text": "OPERATION TL-1", "expected_stage": "TL-2", "title_matches": False, "already_cleared_3star": True},
            {"displayed_stage": "TL-2", "raw_title_text": "OPERATION TL-2", "expected_stage": "TL-2", "title_matches": True, "already_cleared_3star": False},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=screen_states), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=(790, 448, {"x": 767, "y": 438, "w": 53, "h": 20})), \
             patch.object(stage_pilot, "inspect_stage_detail_panel", side_effect=panels), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "adb_swipe_norm") as mock_swipe, \
             patch("time.sleep"):
            res = stage_pilot.enter_stage_node("TL-2", "adb.exe", "127.0.0.1:16384", REPO_ROOT, click_start=True, log_callback=logs.append)

        self.assertTrue(res["entered"])
        self.assertTrue(res["title_verified"])
        self.assertEqual(res["displayed_stage"], "TL-2")
        # Taps: click TL-2 (790,448) -> close wrong TL-1 drawer at safe blank point -> re-click TL-2 (790,448) -> click Start (1110,650)
        self.assertEqual(len(taps), 4)
        self.assertEqual(taps[0], (790, 448))
        self.assertNotEqual(taps[1], (460, 150))
        self.assertTrue(45 <= taps[1][0] <= 720 and 82 <= taps[1][1] <= 665)
        self.assertFalse(767 <= taps[1][0] <= 820 and 438 <= taps[1][1] <= 458)
        self.assertEqual(taps[2], (790, 448))
        self.assertEqual(taps[3], (1110, 650))
        self.assertEqual(taps.count((1110, 650)), 1)
        mock_swipe.assert_called_once()
        self.assertTrue(any("TL-1" in l and "TL-2" in l for l in logs))

        # Scenario B: Consecutive 2 mismatches -> immediately abort and NEVER launch Copilot!
        taps.clear()
        states_2fail = [
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1110, 650), "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1110, 650), "on_stage_map": True},
        ]
        panels_2fail = [
            {"displayed_stage": "TL-1", "raw_title_text": "OPERATION TL-1", "expected_stage": "TL-2", "title_matches": False, "already_cleared_3star": True},
            {"displayed_stage": "TL-1", "raw_title_text": "OPERATION TL-1", "expected_stage": "TL-2", "title_matches": False, "already_cleared_3star": True},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=states_2fail), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=(790, 448, {"x": 767, "y": 438, "w": 53, "h": 20})), \
             patch.object(stage_pilot, "inspect_stage_detail_panel", side_effect=panels_2fail), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "adb_swipe_norm"), \
             patch("time.sleep"):
            res_mm = stage_pilot.enter_stage_node("TL-2", "adb.exe", "127.0.0.1:16384", REPO_ROOT, click_start=True)

        self.assertFalse(res_mm["entered"])
        self.assertTrue(res_mm["stage_mismatch"])
        self.assertEqual(res_mm["mismatch_count"], 2)
        self.assertNotIn((1110, 650), taps)

        # Verify _run_copilot_round refuses to invoke Copilot when prepare_battle_entry_if_on_map returns stage_mismatch
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            cop = base / "copilots" / "tl2.json"
            cop.parent.mkdir(parents=True)
            cop.write_text("{}", encoding="utf-8")
            fake_sess = MagicMock()
            fake_sess.connect.return_value = True
            fake_sess.adb_path = "adb.exe"
            fake_sess.adb_addr = "127.0.0.1:16384"
            with patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "init_maa_sdk", return_value=True), \
                 patch.object(maa_runner, "MaaSession", return_value=fake_sess), \
                 patch.object(maa_runner, "capture_adb_screenshot", return_value=False), \
                 patch.object(stage_pilot, "prepare_battle_entry_if_on_map", return_value={
                     "handled": True, "entered": False, "stage_mismatch": True, "displayed_stage": "TL-1", "reason": "连续 2 次对不上"
                 }):
                rc, rep = maa_runner._run_copilot_round(
                    1, cop, REPO_ROOT, {}, base, stage_code="TL-2", skip_if_already_3star=True
                )
            self.assertEqual(rc, 1)
            self.assertTrue(rep["stage_mismatch"])
            fake_sess.run_single_task.assert_not_called()

    # ==================================================================
    # 4.2 & 4.3 Frontier auto-skip cleared TL-1 -> TL-2 & 3-star + AutoDeploy skip
    # ==================================================================
    def test_auto_push_frontier_skips_cleared_tl1_and_3star_detail_panel_skips_copilot(self):
        # A. 3-star + AutoDeploy (代理指挥) detail drawer detection
        if (REPO_ROOT / "resource" / "template" / "Battle" / "UsePrts" / "UsePrts.png").is_file():
            cleared_panel_img = Image.new("RGB", (1280, 720), (32, 35, 42))
            _draw_three_blue_stars(cleared_panel_img)
            _paste_tpl(cleared_panel_img, "Battle/UsePrts/UsePrts.png", (1050, 575))
            with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[{"text": "OPERATION TL-1", "compact": "OPERATIONTL-1"}]):
                p_cleared = stage_pilot.inspect_stage_detail_panel(cleared_panel_img, REPO_ROOT, expected_stage="TL-1", use_ocr=True)
            self.assertTrue(p_cleared["title_matches"])
            self.assertTrue(p_cleared["has_three_stars"])
            self.assertTrue(p_cleared["has_auto_deploy"])
            self.assertTrue(p_cleared["already_cleared_3star"])

            # Negative check: uncleared stage with 0 stars & UnableToAgent2 padlock must NOT skip
            uncleared_panel_img = Image.new("RGB", (1280, 720), (32, 35, 42))
            _paste_tpl(uncleared_panel_img, "Battle/UsePrts/UnableToAgent2.png", (1010, 575))
            with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[{"text": "OPERATION TL-2", "compact": "OPERATIONTL-2"}]):
                p_new = stage_pilot.inspect_stage_detail_panel(uncleared_panel_img, REPO_ROOT, expected_stage="TL-2", use_ocr=True)
            self.assertTrue(p_new["title_matches"])
            self.assertFalse(p_new["has_three_stars"])
            self.assertTrue(p_new["auto_deploy_locked"])
            self.assertFalse(p_new["already_cleared_3star"])

        # B. _run_copilot_round skips Copilot cleanly when detail panel shows already_cleared_3star
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            cop = base / "copilots" / "tl1.json"
            cop.parent.mkdir(parents=True)
            cop.write_text("{}", encoding="utf-8")
            fake_sess = MagicMock()
            fake_sess.connect.return_value = True
            fake_sess.adb_path = "adb.exe"
            fake_sess.adb_addr = "127.0.0.1:16384"
            with patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "init_maa_sdk", return_value=True), \
                 patch.object(maa_runner, "MaaSession", return_value=fake_sess), \
                 patch.object(stage_pilot, "prepare_battle_entry_if_on_map", return_value={
                     "handled": True, "entered": False, "already_cleared": True, "skipped": True, "reason": "already_cleared_3star"
                 }):
                rc, rep = maa_runner._run_copilot_round(
                    1, cop, REPO_ROOT, {}, base, stage_code="TL-1", skip_if_already_3star=True
                )
            self.assertEqual(rc, 0)
            self.assertTrue(rep["already_cleared"])
            self.assertTrue(rep["victory"])
            fake_sess.run_single_task.assert_not_called()

        # C. --stages auto in MaaEngine.run_push detects TL-1(blue badge) + TL-2(white unlocked) and starts from TL-2!
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            # Synthetic map: TL-1 with left blue hexagon + white card; TL-2 with pure white card and NO blue badge
            def _make_tl_canvas(cleared_codes: set[str], visible_codes: tuple[str, ...]) -> Image.Image:
                img_c = Image.new("RGB", (1280, 720), (30, 34, 42))
                coords = {"TL-1": (400, 385), "TL-2": (600, 438), "TL-3": (820, 438)}
                for code in visible_codes:
                    bx, by = coords[code]
                    for px in range(bx - 16, bx + 69):
                        for py in range(by - 6, by + 26):
                            img_c.putpixel((px, py), (240, 240, 240))
                    if code in cleared_codes:
                        for px in range(bx - 48, bx - 14):
                            for py in range(by - 8, by + 22):
                                img_c.putpixel((px, py), (25, 145, 240))
                return img_c

            def _make_tl_boxes(visible_codes: tuple[str, ...]) -> list[dict]:
                coords = {"TL-1": (400, 385), "TL-2": (600, 438), "TL-3": (820, 438)}
                return [
                    {"text": code, "norm": code, "x": coords[code][0], "y": coords[code][1], "w": 53, "h": 20, "pass": "norm"}
                    for code in visible_codes
                ]

            cleared_set = {"TL-1"}
            pushed: list[str] = []

            def _fake_screencap(*_args, **_kwargs):
                return _make_tl_canvas(cleared_set, ("TL-1", "TL-2", "TL-3"))

            def _fake_ocr(_img):
                return _make_tl_boxes(("TL-1", "TL-2", "TL-3"))

            def _fake_clear(st, **kwargs):
                pushed.append(st)
                cleared_set.add(st)
                return True, {"stage": st, "victory": True, "map_clear_verified": True}

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch.object(stage_pilot, "adb_screencap_image", side_effect=_fake_screencap), \
                     patch.object(stage_pilot, "adb_swipe_norm", return_value=True), \
                     patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
                     patch.object(stage_pilot, "find_stage_boxes_via_ocr", side_effect=_fake_ocr), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear), \
                     patch("time.sleep"):
                    res = eng.run_push(["auto"], refresh_cache_flag=False)

            self.assertTrue(res["ok"])
            self.assertEqual(res["stages"], ["TL-2", "TL-3"])
            self.assertEqual(pushed, ["TL-2", "TL-3"])

    # ==================================================================
    # 4.4 OS-1 vs OS-TR-1 right detail title OCR & X-order normalization
    # ==================================================================
    def test_detail_panel_title_ocr_os1_vs_ostr1_and_x_order_normalization(self):
        # Prefix-aware normalization for OS-1 variants while keeping mainline chapter protection (11-1 != TL-1)
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("05-1", expected_prefix="OS"), "OS-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("0S-1", expected_prefix="OS"), "OS-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("O5-1", expected_prefix="OS"), "OS-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("OS 一 1", expected_prefix="OS"), "OS-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("OS-I", expected_prefix="OS"), "OS-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("O S 一 1", expected_prefix="OS"), "OS-1")
        self.assertNotEqual(stage_pilot._normalize_ocr_stage_token("11-1", expected_prefix="TL"), "TL-1")

        self.assertEqual(stage_pilot._extract_stage_code_from_title_text("OPERATION OS 一 1 ·", expected_stage="OS-1"), "OS-1")
        self.assertEqual(stage_pilot._extract_stage_code_from_title_text("OPERATION OS-TR-I'", expected_stage="OS-1"), "OS-TR-1")

        # Out-of-order WinOCR boxes ("1" at x=965 emitted before "05-" at x=925) must assemble left-to-right into OS-1, NOT 1-05
        ooo_boxes = [
            {"text": "1", "x": 965, "y": 89, "w": 8, "h": 14, "cx": 969, "cy": 96, "pass": "inv", "level": "word"},
            {"text": "05-", "x": 925, "y": 90, "w": 32, "h": 14, "cx": 941, "cy": 97, "pass": "inv", "level": "word"},
            {"text": "OPERATION", "x": 857, "y": 100, "w": 66, "h": 8, "cx": 890, "cy": 104, "pass": "inv", "level": "word"},
        ]
        code_ooo, raw_ooo = stage_pilot._extract_stage_code_from_title_boxes(ooo_boxes, expected_stage="OS-1")
        self.assertEqual(code_ooo, "OS-1")
        self.assertIn("05-", raw_ooo)

        # Real screenshot verification: reports/push_OS-1_fail_round_1.png is the OS-1 2-star detail panel;
        # verify OS-1 title matches expected='OS-1', rejects expected='OS-TR-1', and reports star_count=2 (< 3).
        real_fail = REPO_ROOT / "reports" / "push_OS-1_fail_round_1.png"
        if real_fail.is_file() and sys.platform == "win32":
            with Image.open(real_fail) as raw_im:
                im_os1_real = raw_im.convert("RGB")
            p_os1_ok = stage_pilot.inspect_stage_detail_panel(im_os1_real, REPO_ROOT, expected_stage="OS-1", use_ocr=True)
            self.assertEqual(p_os1_ok["displayed_stage"], "OS-1")
            self.assertTrue(p_os1_ok["title_matches"])
            self.assertEqual(p_os1_ok["star_count"], 2)
            self.assertFalse(p_os1_ok["has_three_stars"])
            self.assertTrue(p_os1_ok["auto_deploy_locked"])
            self.assertFalse(p_os1_ok["already_cleared_3star"])

            p_os1_not_tr = stage_pilot.inspect_stage_detail_panel(im_os1_real, REPO_ROOT, expected_stage="OS-TR-1", use_ocr=True)
            self.assertEqual(p_os1_not_tr["displayed_stage"], "OS-1")
            self.assertFalse(p_os1_not_tr["title_matches"])

    # ==================================================================
    # 4.5 Safe blank tap avoids OS-TR-1 & direct in-place switch to OS-1
    # ==================================================================
    def test_safe_blank_tap_avoids_ostr1_and_direct_inplace_switch_to_os1(self):
        # A. Synthetic map where OS-TR-1 covers (460, 150) + OS-1 & OS-ST-1 edge nodes exist
        synth_boxes = [
            {"text": "OS-ST-1", "norm": "OS-ST-1", "x": 0, "y": 145, "w": 45, "h": 24, "pass": "norm"},
            {"text": "OS-TR-1", "norm": "OS-TR-1", "x": 401, "y": 145, "w": 89, "h": 21, "pass": "tut_inv"},
            {"text": "TUTORIAL", "norm": "TUTORIAL", "x": 428, "y": 131, "w": 39, "h": 7, "pass": "norm"},
            {"text": "OS-1", "norm": "OS-1", "x": 528, "y": 415, "w": 58, "h": 24, "pass": "white_card_roi"},
        ]
        synth_cards = [{"x": 493, "y": 411, "w": 128, "h": 33, "cx": 557, "cy": 427}]
        bx, by = stage_pilot.find_safe_blank_tap_point(
            None, stage_boxes=synth_boxes, white_cards=synth_cards, min_margin=48,
        )
        self.assertNotEqual((bx, by), (460, 150))
        self.assertTrue(45 <= bx <= 720 and 82 <= by <= 665)
        for ox0, oy0, ox1, oy1 in ((0, 124, 65, 178), (325, 124, 515, 176), (493, 411, 621, 444)):
            dx = 0 if ox0 <= bx <= ox1 else (ox0 - bx if bx < ox0 else bx - ox1)
            dy = 0 if oy0 <= by <= oy1 else (oy0 - by if by < oy0 else by - oy1)
            self.assertGreaterEqual(math.hypot(dx, dy), 48.0)

        # Also check on real push_OS-1_fail_round_1.png if present
        real_fail = REPO_ROOT / "reports" / "push_OS-1_fail_round_1.png"
        if real_fail.is_file() and sys.platform == "win32":
            with Image.open(real_fail) as raw_im:
                im_fail = raw_im.convert("RGB")
            rbx, rby = stage_pilot.find_safe_blank_tap_point(im_fail)
            self.assertNotEqual((rbx, rby), (460, 150))
            self.assertFalse(325 <= rbx <= 515 and 124 <= rby <= 176)
            self.assertFalse(493 <= rbx <= 621 and 411 <= rby <= 444)

        # B. Direct in-place switch: OS-TR-1 drawer open, OS-1 unobstructed on left (557, 427) ->
        # enter_stage_node("OS-1") clicks (557, 427) directly WITHOUT tapping a blank close point first!
        dummy = Image.new("RGB", (1280, 720), (25, 28, 35))
        taps: list[tuple[int, int]] = []
        logs: list[str] = []
        states = [
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1142, 664), "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1142, 664), "on_stage_map": True},
        ]
        panels = [
            {"displayed_stage": "OS-TR-1", "raw_title_text": "OS-TR-I'", "expected_stage": "OS-1", "title_matches": False, "already_cleared_3star": True},
            {"displayed_stage": "OS-1", "raw_title_text": "OS 一 1", "expected_stage": "OS-1", "title_matches": True, "already_cleared_3star": False},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=states), \
             patch.object(stage_pilot, "inspect_stage_detail_panel", side_effect=panels), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=synth_boxes), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "adb_swipe_norm") as mock_swipe, \
             patch("time.sleep"):
            res = stage_pilot.enter_stage_node("OS-1", "adb.exe", "127.0.0.1:16384", REPO_ROOT, click_start=True, log_callback=logs.append)

        self.assertTrue(res["entered"])
        self.assertTrue(res["title_verified"])
        self.assertTrue(res.get("switched_in_place"))
        self.assertEqual(res["displayed_stage"], "OS-1")
        self.assertEqual(taps, [(557, 427), (1142, 664)])
        self.assertNotIn((460, 150), taps)
        mock_swipe.assert_not_called()
        self.assertTrue(any("原地切换面板" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
