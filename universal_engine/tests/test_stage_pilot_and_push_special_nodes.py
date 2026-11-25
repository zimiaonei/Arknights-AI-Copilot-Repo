import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from universal_engine import ai_strategist, launcher_gui, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _compose_screen_with_template(template_rel: str, dest_xy: tuple[int, int]) -> Image.Image:
    canvas = Image.new("RGB", (1280, 720), (35, 38, 46))
    tpl_path = REPO_ROOT / "resource" / "template" / template_rel
    with Image.open(tpl_path) as tpl:
        canvas.paste(tpl.convert("RGB"), dest_xy)
    return canvas


class StagePilotAndSpecialNodesTests(unittest.TestCase):
    def test_classify_stage_node_and_coord_scaling(self):
        self.assertEqual(stage_pilot.classify_stage_node("TL-ST-1"), "story")
        self.assertEqual(stage_pilot.classify_stage_node("st-2"), "story")
        self.assertEqual(stage_pilot.classify_stage_node("TL-TR-1"), "tutorial")
        self.assertEqual(stage_pilot.classify_stage_node("TR-15"), "tutorial")
        self.assertEqual(stage_pilot.classify_stage_node("TL-1"), "battle")
        self.assertEqual(stage_pilot.classify_stage_node("14-20"), "battle")

        self.assertEqual(stage_pilot.norm_to_device_xy(640, 360, 1920, 1080), (960, 540))
        self.assertEqual(stage_pilot.norm_to_device_xy(1200, 48, 1920, 1080), (1800, 72))

    def test_template_detection_on_real_map_and_story_screens(self):
        real_map = REPO_ROOT / "reports" / "push_TL-9_fail_round_2.png"
        if real_map.is_file() and (REPO_ROOT / "resource" / "template").is_dir():
            with Image.open(real_map) as im:
                st = stage_pilot.analyze_screen_state(im, REPO_ROOT, use_ocr_fallback=False)
                self.assertTrue(st["on_stage_map"])
                self.assertTrue(st["has_return"])
                self.assertFalse(st["has_skip_button"])
                self.assertFalse(st["in_formation"])
                self.assertFalse(st["in_formation_select"])

                with patch.object(stage_pilot, "adb_screencap_image", return_value=im):
                    prereqs = stage_pilot.discover_unlocked_special_prereqs(
                        "TL-", "adb", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-1"
                    )
                    self.assertEqual(prereqs, [])

        if (REPO_ROOT / "resource" / "template" / "Battle" / "Plot" / "SkipThePreBattlePlot.png").is_file():
            skip_img = _compose_screen_with_template("Battle/Plot/SkipThePreBattlePlot.png", (1150, 32))
            st_skip = stage_pilot.analyze_screen_state(skip_img, REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st_skip["has_skip_button"])
            self.assertGreater(st_skip["skip_xy"][0], 1100)
            self.assertLess(st_skip["skip_xy"][1], 100)

            conf_img = _compose_screen_with_template("Battle/Plot/SkipThePreBattlePlotConfirm.png", (774, 474))
            st_conf = stage_pilot.analyze_screen_state(conf_img, REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st_conf["has_skip_confirm"])
            self.assertGreater(st_conf["confirm_xy"][0], 720)

            form_img = _compose_screen_with_template("Battle/StartButton/StartButton2.png", (1030, 380))
            st_form = stage_pilot.analyze_screen_state(form_img, REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st_form["in_formation"])

    def test_story_skip_loop_skips_then_confirm_until_formation_or_map(self):
        fake_states = [
            {"has_skip_confirm": False, "has_skip_button": True, "skip_xy": (1190, 45), "has_end_of_plot": False, "in_formation": False, "in_battle": False, "has_story_dialog": False, "on_stage_map": False},
            {"has_skip_confirm": True, "confirm_xy": (795, 495), "has_skip_button": False, "has_end_of_plot": False, "in_formation": False, "in_battle": False, "has_story_dialog": False, "on_stage_map": False},
            {"has_skip_confirm": False, "has_skip_button": False, "has_end_of_plot": False, "in_formation": True, "in_battle": False, "has_story_dialog": False, "on_stage_map": False},
        ]
        taps = []
        dummy_img = Image.new("RGB", (1920, 1080), (0, 0, 0))
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=fake_states), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            res = stage_pilot.run_story_skip_loop("adb", "127.0.0.1:16384", REPO_ROOT, target_states=("formation", "map"), poll_interval=0.01)
            self.assertTrue(res["ok"])
            self.assertEqual(res["outcome"], "formation")
            self.assertEqual(taps, [(1190, 45), (795, 495)])

    def test_autonomous_clear_stage_story_skips_prts_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            with patch.object(maa_runner, "ensure_game_running", return_value="com.hypergryph.arknights"), \
                 patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
                 patch.object(maa_runner, "_probe_adb_address", return_value="127.0.0.1:16384"), \
                 patch.object(maa_runner, "fetch_prts_guides") as mock_fetch, \
                 patch.object(stage_pilot, "handle_story_stage", return_value=(True, {"stage": "TL-ST-1", "node_type": "story", "victory": True, "skipped_story": True})) as mock_story:
                ok, summary = maa_runner.autonomous_clear_stage("TL-ST-1", maa_dir=maa, config={}, base_dir=base)
                self.assertTrue(ok)
                self.assertEqual(summary["node_type"], "story")
                self.assertTrue(summary["skipped_story"])
                mock_fetch.assert_not_called()
                mock_story.assert_called_once()

    def test_autonomous_clear_stage_tutorial_handoff_auto_resume_and_manual_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            banner_events = []
            logs = []

            # Simulate: poll 1 in tutorial (not on map), poll 2 back on map with TL-1 unlocked
            dummy_img = Image.new("RGB", (1280, 720), (0, 0, 0))
            screen_seq = [
                {"on_stage_map": False},
                {"on_stage_map": True},
            ]
            with patch.object(maa_runner, "ensure_game_running", return_value="com.hypergryph.arknights"), \
                 patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
                 patch.object(maa_runner, "_probe_adb_address", return_value="127.0.0.1:16384"), \
                 patch.object(maa_runner, "_notify_windows_alert"), \
                 patch.object(maa_runner, "fetch_prts_guides") as mock_fetch, \
                 patch.object(stage_pilot, "enter_stage_node", return_value={"entered": True, "clicked_start": True}) as mock_enter, \
                 patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
                 patch.object(stage_pilot, "analyze_screen_state", side_effect=screen_seq), \
                 patch.object(stage_pilot, "find_stage_center_on_image", return_value=(600, 400, {"x": 570, "y": 390, "w": 60, "h": 20})), \
                 patch.object(stage_pilot, "_is_stage_node_unlocked_visual", return_value=True):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-TR-1",
                    maa_dir=maa,
                    config={},
                    base_dir=base,
                    next_stage="TL-1",
                    log_callback=logs.append,
                    tutorial_handler=banner_events.append,
                    tutorial_poll_interval=0.05,
                )
                self.assertTrue(ok)
                self.assertEqual(summary["node_type"], "tutorial")
                self.assertEqual(summary["handoff_status"], "auto_resumed")
                mock_fetch.assert_not_called()
                mock_enter.assert_called_once()
                self.assertEqual([e["action"] for e in banner_events], ["show", "hide"])
                self.assertIn("[教学关交接]", banner_events[0]["message"])
                self.assertTrue(any("[教学关交接]" in l for l in logs))

            # Manual resume trigger ("我已通关，立即继续")
            manual_ev = threading.Event()
            banner_events.clear()
            threading.Timer(0.08, manual_ev.set).start()
            with patch.object(maa_runner, "ensure_game_running", return_value="com.hypergryph.arknights"), \
                 patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
                 patch.object(maa_runner, "_probe_adb_address", return_value="127.0.0.1:16384"), \
                 patch.object(maa_runner, "_notify_windows_alert"), \
                 patch.object(stage_pilot, "enter_stage_node", return_value={"entered": True}), \
                 patch.object(stage_pilot, "adb_screencap_image", return_value=None):
                ok2, sum2 = maa_runner.autonomous_clear_stage(
                    "TL-TR-1",
                    maa_dir=maa,
                    config={},
                    base_dir=base,
                    next_stage="TL-1",
                    tutorial_handler=banner_events.append,
                    tutorial_resume_event=manual_ev,
                    tutorial_poll_interval=0.2,
                )
                self.assertTrue(ok2)
                self.assertEqual(sum2["handoff_status"], "manual_resumed")
                self.assertEqual([e["action"] for e in banner_events], ["show", "hide"])

    def test_parse_battle_metrics_ignores_benign_probes_on_completed_copilot(self):
        events = [
            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["SkipThePreBattlePlot"], "details": {}}},
            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartPre"], "details": {}}},
            {"message": "TaskChainCompleted", "details": {"taskchain": "Copilot"}},
        ]
        m = ai_strategist.parse_battle_metrics([], events)
        self.assertTrue(m["completed_flag"])
        self.assertFalse(m["failed_flag"])
        self.assertEqual(m["errors"], [])

        # Without TaskChainCompleted, SubTaskError still surfaces for round failure diagnosis
        m_fail = ai_strategist.parse_battle_metrics([], events[:2])
        self.assertFalse(m_fail["completed_flag"])
        self.assertEqual(len(m_fail["errors"]), 2)

    def test_run_push_passes_next_stage_and_gui_tutorial_banner_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path)
                seen_calls = []

                def _fake_clear(st, **kwargs):
                    seen_calls.append((st, kwargs.get("next_stage")))
                    return True, {"stage": st, "victory": True}

                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready"), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res = eng.run_push(["TL-ST-1", "TL-TR-1", "TL-1"], refresh_cache_flag=False)
                    self.assertTrue(res["ok"])
                    self.assertEqual(seen_calls, [("TL-ST-1", "TL-TR-1"), ("TL-TR-1", "TL-1"), ("TL-1", None)])

            # GUI tutorial banner show/manual-resume/hide & log highlighting
            orig_engine = master_scheduler.MaaEngine
            gui_eng = orig_engine(config_path=cfg_path)
            LauncherController = getattr(launcher_gui, "LauncherController", None)
            WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
            if LauncherController is None:
                from universal_engine.gui.controller import LauncherController
                from universal_engine.gui.bridge import WebViewJsApi

            with patch.object(launcher_gui, "collect_dashboard_snapshot", return_value={
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": "",
                "integrity": {"valid": False, "maa_version": "", "reason": "test"},
                "my_box": {"total_owned": 0, "e2_count": 0},
                "depot": {"syncTime": "", "count": 0},
                "growth_preview": {"ok": False, "recommended_stages": ["1-7"], "reason": "test"},
                "week_key": "2026-W47",
                "annihilation_process": None,
                "adb_info": {"connected": False, "adb_address": "", "width": None, "height": None, "is_16_9": False},
            }):
                controller = LauncherController(engine=gui_eng, auto_start_refresh=False)
                api = WebViewJsApi(controller)

                controller._show_tutorial_banner({
                    "action": "show",
                    "stage_code": "TL-TR-1",
                    "next_stage": "TL-1",
                    "message": "[教学关交接] 当前遇到无法跳过的教学关卡（TL-TR-1），请在模拟器中手动点完本关教学，通关返回地图后系统将自动恢复后续推图！",
                })
                self.assertTrue(controller.tutorial_banner_state.get("visible", False))
                self.assertEqual(controller.tutorial_banner_state.get("stage_code"), "TL-TR-1")
                evs = controller.drain_events()
                tut_events = [e["payload"] for e in evs if e.get("type") == "tutorial_banner"]
                self.assertTrue(len(tut_events) >= 1)
                self.assertTrue(tut_events[-1].get("visible", False))

                controller._append_log("[教学关交接] 当前遇到无法跳过的教学关卡（TL-TR-1）")
                evs_log = controller.drain_events()
                log_batches = [e["payload"] for e in evs_log if e.get("type") == "log_batch"]
                self.assertTrue(any(item.get("tag") == "tutorial" for b in log_batches for item in b) or any(l.get("tag") == "tutorial" for l in controller.logs))

                api.resume_from_tutorial()
                self.assertTrue(controller.tutorial_resume_event.is_set())
                self.assertFalse(controller.tutorial_banner_state.get("visible", False))
                evs_hide = controller.drain_events()
                hide_events = [e["payload"] for e in evs_hide if e.get("type") == "tutorial_banner"]
                self.assertTrue(len(hide_events) >= 1)
                self.assertFalse(hide_events[-1].get("visible", False))

    def test_detect_map_frontier_on_real_tl9_failure_screenshot(self):
        real_map = REPO_ROOT / "reports" / "push_TL-9_fail_round_2.png"
        if not (real_map.is_file() and (REPO_ROOT / "resource" / "template").is_dir()):
            return
        with Image.open(real_map) as im:
            with patch.object(stage_pilot, "adb_screencap_image", return_value=im):
                info = stage_pilot.detect_map_frontier_stage(
                    "TL-",
                    "adb.exe",
                    "127.0.0.1:16384",
                    REPO_ROOT,
                    target_stage="TL-9",
                    known_battle_stages=[f"TL-{i}" for i in range(1, 10)],
                    max_swipes=0,
                )
        self.assertTrue(info["on_stage_map"])
        self.assertFalse(info["target_visible"])
        self.assertEqual(info["frontier_stage"], "TL-2")
        self.assertEqual(info["frontier_type"], "battle")
        self.assertEqual(info["push_sequence"], [f"TL-{i}" for i in range(2, 10)])

    def test_discover_special_prereqs_filters_passed_and_cleared_nodes_on_tl2_screen(self):
        # 1. Real screenshot repro: TL-ST-1(blue), TL-TR-1(blue), TL-1(blue), TL-2(uncleared frontier) coexist on screen
        real_map = REPO_ROOT / "reports" / "push_TL-9_fail_round_2.png"
        if real_map.is_file() and (REPO_ROOT / "resource" / "template").is_dir():
            with Image.open(real_map) as im:
                with patch.object(stage_pilot, "adb_screencap_image", return_value=im):
                    self.assertEqual(
                        stage_pilot.discover_unlocked_special_prereqs(
                            "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-2"
                        ),
                        [],
                    )
                    self.assertEqual(
                        stage_pilot.discover_unlocked_special_prereqs(
                            "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-3"
                        ),
                        [],
                    )

        # Helper to synthesize a 1280x720 map canvas with optional blue cleared hexagons to the left of nodes
        def _make_map_canvas(blue_boxes: list[tuple[int, int]]) -> Image.Image:
            canvas = Image.new("RGB", (1280, 720), (35, 38, 46))
            for bx, by in blue_boxes:
                for px in range(max(0, bx - 40), max(0, bx - 10)):
                    for py in range(max(0, by - 8), min(720, by + 22)):
                        canvas.putpixel((px, py), (25, 145, 240))
            return canvas

        # 2. Rule 1: Even if TL-TR-1 lacks blue badge, unlocked TL-2 to its right proves TL-TR-1 is already passed
        canvas_no_badge = _make_map_canvas([(94, 346), (569, 385)])
        boxes_tl2_screen = [
            {"text": "TL-ST-1", "norm": "TL-ST-1", "x": 94, "y": 346, "w": 91, "h": 20, "pass": "norm"},
            {"text": "TL-TR-1", "norm": "TL-TR-1", "x": 325, "y": 347, "w": 90, "h": 19, "pass": "tut_inv"},
            {"text": "TL-1", "norm": "TL-1", "x": 569, "y": 385, "w": 53, "h": 20, "pass": "norm"},
            {"text": "TL-2", "norm": "TL-2", "x": 767, "y": 438, "w": 53, "h": 20, "pass": "norm"},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=canvas_no_badge), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=boxes_tl2_screen):
            self.assertEqual(
                stage_pilot.discover_unlocked_special_prereqs(
                    "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-2"
                ),
                [],
            )
            self.assertEqual(
                stage_pilot.discover_unlocked_special_prereqs(
                    "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-3"
                ),
                [],
            )
            frontier = stage_pilot.detect_map_frontier_stage(
                "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT,
                target_stage="TL-9", known_battle_stages=[f"TL-{i}" for i in range(1, 10)], max_swipes=0,
            )
            self.assertEqual(frontier["frontier_stage"], "TL-2")
            self.assertEqual(frontier["frontier_type"], "battle")

        # 3. Rule 2 (negative): Cleared TL-ST-1 & TL-TR-1 with blue badges alone on screen are NOT blocking
        canvas_both_cleared = _make_map_canvas([(94, 346), (325, 347)])
        boxes_only_special = boxes_tl2_screen[:2]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=canvas_both_cleared), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=boxes_only_special):
            self.assertEqual(
                stage_pilot.discover_unlocked_special_prereqs(
                    "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-1"
                ),
                [],
            )

        # 4. Rule 2 (positive): Uncleared TL-TR-1 at rightmost frontier (no TL-1 unlocked yet, no blue badge) IS blocking
        canvas_tr1_frontier = _make_map_canvas([(94, 346)])  # only TL-ST-1 is cleared; TL-TR-1 has no blue badge
        with patch.object(stage_pilot, "adb_screencap_image", return_value=canvas_tr1_frontier), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=boxes_only_special):
            self.assertEqual(
                stage_pilot.discover_unlocked_special_prereqs(
                    "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-1"
                ),
                ["TL-TR-1"],
            )

        # 5. Mid-chapter blocking tutorial: cleared TL-3 on left, uncleared TL-TR-2 at rightmost frontier -> blocking TL-4
        canvas_mid_tr2 = _make_map_canvas([(200, 360)])  # TL-3 cleared, TL-TR-2 uncleared
        boxes_mid_tr2 = [
            {"text": "TL-3", "norm": "TL-3", "x": 200, "y": 360, "w": 53, "h": 20, "pass": "norm"},
            {"text": "TL-TR-2", "norm": "TL-TR-2", "x": 440, "y": 360, "w": 90, "h": 19, "pass": "tut_inv"},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=canvas_mid_tr2), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=boxes_mid_tr2):
            self.assertEqual(
                stage_pilot.discover_unlocked_special_prereqs(
                    "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-4"
                ),
                ["TL-TR-2"],
            )

        # 6. Initial uncleared story node TL-ST-1 alone at frontier -> blocking TL-1
        canvas_st1_only = _make_map_canvas([])
        with patch.object(stage_pilot, "adb_screencap_image", return_value=canvas_st1_only), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=[boxes_tl2_screen[0]]):
            self.assertEqual(
                stage_pilot.discover_unlocked_special_prereqs(
                    "TL-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="TL-1"
                ),
                ["TL-ST-1"],
            )

    def test_reception_clue_quick_insert_and_unlock_fallback_flow(self):
        tpl_root = REPO_ROOT / "resource" / "template"
        qi_tpl = tpl_root / "InfrastPic" / "Clue" / "InfrastClueQuickInsert.png"
        un_tpl = tpl_root / "InfrastPic" / "Clue" / "UnlockClues.png"
        rf_tpl = tpl_root / "InfrastPic" / "Reception" / "ReceptionFlag.png"
        if not (qi_tpl.is_file() and un_tpl.is_file() and rf_tpl.is_file()):
            return

        # Screen 1: Clue board with ReceptionFlag, orange QuickInsert + red badge, and dark-grey (disabled) Unlock button
        s1 = Image.new("RGB", (1280, 720), (42, 46, 54))
        with Image.open(rf_tpl) as im_rf:
            s1.paste(im_rf.convert("RGB"), (385, 19))
        with Image.open(qi_tpl) as im_qi:
            s1.paste(im_qi.convert("RGB"), (1108, 631))
        # Red dot badge at upper-right of QuickInsert button
        for x in range(1245, 1265):
            for y in range(612, 632):
                s1.putpixel((x, y), (225, 45, 45))
        # Dark grey disabled Unlock button
        with Image.open(un_tpl) as im_un:
            dimmed = im_un.convert("RGB").point(lambda p: int(p * 0.45))
            s1.paste(dimmed, (554, 618))

        st1 = stage_pilot.analyze_reception_clue_screen(s1, REPO_ROOT, use_ocr=False)
        self.assertTrue(st1["on_clue_board"])
        self.assertTrue(st1["quick_insert_available"])
        self.assertTrue(st1["quick_insert_red_badge"])
        self.assertFalse(st1["unlock_clues_ready"])

        # Screen 2: After clicking QuickInsert -> UnlockClues lights up bright white (7/7 ready)
        s2 = Image.new("RGB", (1280, 720), (42, 46, 54))
        with Image.open(rf_tpl) as im_rf:
            s2.paste(im_rf.convert("RGB"), (385, 19))
        with Image.open(un_tpl) as im_un:
            s2.paste(im_un.convert("RGB"), (554, 618))

        st2 = stage_pilot.analyze_reception_clue_screen(s2, REPO_ROOT, use_ocr=False)
        self.assertTrue(st2["on_clue_board"])
        self.assertTrue(st2["unlock_clues_ready"])
        self.assertTrue(st2["unlock_highlighted"])

        # Screen 3: Clue exchange unlocked (no longer showing clickable UnlockClues)
        s3 = Image.new("RGB", (1280, 720), (42, 46, 54))
        with Image.open(rf_tpl) as im_rf:
            s3.paste(im_rf.convert("RGB"), (385, 19))

        taps = []
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[s1, s2, s3, s3, s3]), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[]), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            res = stage_pilot.ensure_reception_clues_processed("adb.exe", "127.0.0.1:16384", REPO_ROOT)
        self.assertTrue(res["ok"])
        self.assertTrue(res["entered_clue_board"])
        self.assertEqual(res["quick_insert_clicked"], 1)
        self.assertTrue(res["unlock_clues_clicked"])
        # Verify tap 1 hit QuickInsert (~1132, 655) and tap 2 hit UnlockClues (~682, 649)
        self.assertGreater(taps[0][0], 1080)
        self.assertGreater(taps[0][1], 600)
        self.assertTrue(540 <= taps[1][0] <= 820)
        self.assertTrue(600 <= taps[1][1] <= 690)

    def test_return_to_home_screen_via_quick_switch(self):
        menu_tpl = REPO_ROOT / "resource" / "template" / "QuickSwitch" / "Menu.png"
        home_tpl = REPO_ROOT / "resource" / "template" / "QuickSwitch" / "Home.png"
        if not (menu_tpl.is_file() and home_tpl.is_file()):
            return
        s_map = _compose_screen_with_template("QuickSwitch/Menu.png", (247, 19))
        s_open = _compose_screen_with_template("QuickSwitch/Home.png", (61, 256))
        s_home = Image.new("RGB", (1280, 720), (30, 34, 42))
        taps = []
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[s_map, s_open, s_home]), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[]), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            res = stage_pilot.return_to_home_screen("adb.exe", "127.0.0.1:16384", REPO_ROOT)
        self.assertTrue(res["ok"])
        self.assertEqual(res["method"], "quick_switch")
        self.assertEqual(len(taps), 2)

    def test_stage_ocr_fuzzy_normalization_and_prefix_correction(self):
        # Separator & digit confusions without prefix
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL 2"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL—2"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL–2"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL_2"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("） TL-2"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-Z"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-I"), "TL-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-l"), "TL-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-]"), "TL-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-O"), "TL-0")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-1O"), "TL-10")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-S"), "TL-5")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL-TR-I"), "TL-TR-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL TR l"), "TL-TR-1")

        # Known event prefix confusions (TI / IL -> TL)
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TI-2", expected_prefix="TL"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("IL-2", expected_prefix="TL"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TI Z", expected_prefix="TL"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("IL—2", expected_prefix="TL"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TL2", expected_prefix="TL"), "TL-2")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("TI-TR-l", expected_prefix="TL"), "TL-TR-1")

        # Negative guards: clipped edge 'T-1' and unrelated '11-2' must NOT falsely match 'TL'
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("T-1", expected_prefix="TL"), "T-1")
        self.assertEqual(stage_pilot._normalize_ocr_stage_token("11-2", expected_prefix="TL"), "11-2")
        self.assertFalse(stage_pilot._match_stage_token_fuzzy("T-1", "TL-1"))
        self.assertFalse(stage_pilot._match_stage_token_fuzzy("11-2", "TL-2"))
        self.assertTrue(stage_pilot._match_stage_token_fuzzy("TI 2", "TL-2"))
        self.assertTrue(stage_pilot._match_stage_token_fuzzy("IL—Z", "TL-2"))

        # Right detail panel title extraction with fuzzy forms
        self.assertEqual(stage_pilot._extract_stage_code_from_title_text("OPERATION TL 2", "TL-2"), "TL-2")
        self.assertEqual(stage_pilot._extract_stage_code_from_title_text("OPERATION TI-Z", "TL-2"), "TL-2")
        self.assertEqual(stage_pilot._extract_stage_code_from_title_text("OPERATION IL—2", "TL-2"), "TL-2")

    def test_real_tl2_failure_screenshot_detects_tl2_and_all_nodes(self):
        real_tl2 = REPO_ROOT / "reports" / "push_TL-2_fail_round_1.png"
        if not (REPO_ROOT / "resource" / "template").is_dir():
            return
        self.assertTrue(real_tl2.is_file(), f"Missing real regression screenshot: {real_tl2}")

        with Image.open(real_tl2) as im:
            # 1. Visual white-card detector finds exactly TL-1(cleared) and TL-2(uncleared)
            cards = stage_pilot.detect_white_stage_cards(im)
            self.assertEqual(len(cards), 2)
            self.assertTrue(cards[0]["cleared"])
            self.assertFalse(cards[1]["cleared"])
            self.assertTrue(580 <= cards[1]["cx"] <= 710)
            self.assertTrue(425 <= cards[1]["cy"] <= 470)

            # 2. Direct target lookup finds TL-2 reliably via white-card ROI upscaled OCR
            hit = stage_pilot.find_stage_center_on_image(im, "TL-2")
            self.assertIsNotNone(hit)
            cx, cy, box = hit
            self.assertTrue(585 <= cx <= 705, f"Unexpected TL-2 cx={cx}")
            self.assertTrue(428 <= cy <= 468, f"Unexpected TL-2 cy={cy}")
            self.assertEqual(box["norm"], "TL-2")
            self.assertEqual(box["pass"], "white_card_roi")

            # 3. Full frontier scan identifies TL-TR-1(已通蓝标), TL-1(已通蓝标), TL-2(未通白框)
            logs: list[str] = []
            with patch.object(stage_pilot, "adb_screencap_image", return_value=im):
                info = stage_pilot.detect_map_frontier_stage(
                    "TL-",
                    "adb.exe",
                    "127.0.0.1:16384",
                    REPO_ROOT,
                    target_stage="TL-9",
                    known_battle_stages=[f"TL-{i}" for i in range(1, 10)],
                    max_swipes=0,
                    log_callback=logs.append,
                )

        self.assertTrue(info["on_stage_map"])
        by_code = {n["code"]: n for n in info["visible_nodes"]}
        self.assertIn("TL-TR-1", by_code)
        self.assertIn("TL-1", by_code)
        self.assertIn("TL-2", by_code)
        self.assertTrue(by_code["TL-TR-1"]["cleared"])
        self.assertTrue(by_code["TL-1"]["cleared"])
        self.assertFalse(by_code["TL-2"]["cleared"])
        self.assertTrue(by_code["TL-2"]["is_white_unlocked_card"])
        self.assertEqual(info["frontier_stage"], "TL-2")
        self.assertEqual(info["push_sequence"], [f"TL-{i}" for i in range(2, 10)])
        joined_logs = "\n".join(logs)
        self.assertIn("TL-TR-1(已通蓝标)", joined_logs)
        self.assertIn("TL-1(已通蓝标)", joined_logs)
        self.assertIn("TL-2(未通白框)", joined_logs)

    def test_unknown_white_card_fallback_clicks_nearest_and_verifies_detail_title(self):
        # Case A: Real TL-2 screenshot where OCR is forced to miss TL-2 -> Layer 2 clicks white card right of TL-1
        real_tl2 = REPO_ROOT / "reports" / "push_TL-2_fail_round_1.png"
        if real_tl2.is_file():
            with Image.open(real_tl2) as im:
                im_copy = im.convert("RGB")
            taps: list[tuple[int, int]] = []
            logs: list[str] = []
            known_only_cleared = [
                {"code": "TL-TR-1", "node_type": "tutorial", "cleared": True, "cx": 216, "cy": 356, "x": 171},
                {"code": "TL-1", "node_type": "battle", "cleared": True, "cx": 441, "cy": 395, "x": 415},
            ]
            states = [
                {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
                {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
                {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1115, 650), "on_stage_map": True},
            ]
            with patch.object(stage_pilot, "adb_screencap_image", return_value=im_copy), \
                 patch.object(stage_pilot, "analyze_screen_state", side_effect=states), \
                 patch.object(stage_pilot, "find_stage_center_on_image", return_value=None), \
                 patch.object(stage_pilot, "inspect_map_stage_nodes", return_value=known_only_cleared), \
                 patch.object(stage_pilot, "inspect_stage_detail_panel", return_value={
                     "displayed_stage": "TL-2", "raw_title_text": "OPERATION TL-2", "expected_stage": "TL-2",
                     "title_matches": True, "already_cleared_3star": False,
                 }), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
                 patch.object(stage_pilot, "adb_swipe_norm") as mock_swipe, \
                 patch("time.sleep"):
                res = stage_pilot.enter_stage_node(
                    "TL-2", "adb.exe", "127.0.0.1:16384", REPO_ROOT,
                    click_start=True, max_swipes=2, log_callback=logs.append,
                )
            self.assertTrue(res["entered"])
            self.assertTrue(res["title_verified"])
            self.assertEqual(res["displayed_stage"], "TL-2")
            self.assertEqual(res["located_by"], "white_card_fallback")
            self.assertEqual(len(taps), 2)
            self.assertTrue(590 <= taps[0][0] <= 705 and 430 <= taps[0][1] <= 468)
            self.assertEqual(taps[1], (1115, 650))
            mock_swipe.assert_not_called()

        # Case B: Multiple uncleared white cards to right of TL-1 -> tries nearest first, closes wrong drawer, hits 2nd
        canvas = Image.new("RGB", (1280, 720), (35, 38, 46))
        # Cleared TL-1 card + blue badge on left
        for px in range(280, 394):
            for py in range(380, 412):
                canvas.putpixel((px, py), (242, 242, 242))
        for px in range(245, 275):
            for py in range(380, 412):
                canvas.putpixel((px, py), (25, 145, 240))
        # Candidate 1 (nearer, wrong branch TL-S-1) at cx=540, cy=396
        for px in range(483, 597):
            for py in range(380, 412):
                canvas.putpixel((px, py), (242, 242, 242))
        # Candidate 2 (farther, target TL-2) at cx=780, cy=446
        for px in range(723, 837):
            for py in range(430, 462):
                canvas.putpixel((px, py), (242, 242, 242))

        taps2: list[tuple[int, int]] = []
        states2 = [
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1115, 650), "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1115, 650), "on_stage_map": True},
        ]
        panels2 = [
            {"displayed_stage": "TL-S-1", "raw_title_text": "OPERATION TL-S-1", "expected_stage": "TL-2", "title_matches": False, "already_cleared_3star": False},
            {"displayed_stage": "TL-2", "raw_title_text": "OPERATION TL-2", "expected_stage": "TL-2", "title_matches": True, "already_cleared_3star": False},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=canvas), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=states2), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=None), \
             patch.object(stage_pilot, "inspect_map_stage_nodes", return_value=[{"code": "TL-1", "node_type": "battle", "cleared": True, "cx": 337, "cy": 396, "x": 280}]), \
             patch.object(stage_pilot, "inspect_stage_detail_panel", side_effect=panels2), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps2.append((x, y)) or True), \
             patch("time.sleep"):
            res2 = stage_pilot.enter_stage_node("TL-2", "adb.exe", "127.0.0.1:16384", REPO_ROOT, click_start=True, max_swipes=0)

        self.assertTrue(res2["entered"])
        self.assertEqual(res2["located_by"], "white_card_fallback")
        # Tap nearest candidate (~540,396) -> close wrong drawer at safe blank point -> tap 2nd candidate (~780,446) -> click Start (1115,650)
        self.assertEqual(len(taps2), 4)
        self.assertTrue(500 <= taps2[0][0] <= 580)
        self.assertNotEqual(taps2[1], (460, 150))
        self.assertTrue(45 <= taps2[1][0] <= 720 and 82 <= taps2[1][1] <= 665)
        for ox0, oy0, ox1, oy1 in ((280, 380, 394, 412), (483, 380, 597, 412), (723, 430, 837, 462)):
            self.assertFalse(ox0 <= taps2[1][0] <= ox1 and oy0 <= taps2[1][1] <= oy1)
        self.assertTrue(740 <= taps2[2][0] <= 820)
        self.assertEqual(taps2[3], (1115, 650))

    def test_pre_copilot_intercept_log_omits_misleading_battle_start_pre(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-2", "stageId": "act55side_02", "apCost": 9}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({"total_owned": 1, "e2_count": 1, "e2_operators": [{"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}], "all_owned": {"棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}}}),
                encoding="utf-8",
            )
            guides = [{
                "id": 108603, "like": 100, "views": 2000,
                "content": {"stage_name": "act55side_02", "opers": [{"name": "棘刺", "skill": 3}], "groups": [], "actions": []},
            }]
            intercept_report = {
                "round": 1,
                "elapsed_seconds": 0.0,
                "victory": False,
                "stage_entry_failed": True,
                "stage_mismatch": False,
                "error": "未能在当前地图识别到关卡节点 TL-2",
                "metrics": {
                    "kills": None,
                    "costs": None,
                    "last_3_actions": [],
                    "errors": ["未能在当前地图识别到关卡节点 TL-2"],
                    "completed_flag": False,
                    "failed_flag": False,
                    "stage_entry_failed": True,
                    "entry_failed_tasks": ["StagePilotEntryFailed"],
                },
                "prep_entry": {"handled": False, "entered": False, "reason": "未能在当前地图识别到关卡节点 TL-2"},
                "events_tail": [],
                "log_tail": [],
                "screenshot": "",
                "report_path": "",
            }
            logs: list[str] = []
            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", return_value=(1, intercept_report)):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-2", maa_dir=maa, config={}, max_rounds=1, base_dir=base, log_callback=logs.append
                )

            self.assertFalse(ok)
            self.assertTrue(summary["stage_entry_failed"])
            joined = "\n".join(logs)
            self.assertIn("未能在当前地图识别到关卡节点 TL-2", joined)
            self.assertIn("尚未启动 Copilot", joined)
            self.assertNotIn("BattleStartPre/BattleStartAll", joined)

            # ai_strategist._repair_deterministic must also report real intercept reason without BattleStartPre
            _, _, strat = ai_strategist._repair_deterministic(
                [{"guide_id": 108603, "copilot": guides[0]["content"]}],
                0,
                guides[0]["content"],
                intercept_report,
                {"all_owned": {}},
                1,
            )
            self.assertEqual(strat["mode"], "stage_entry_failed")
            self.assertIn("未能在当前地图识别到关卡节点 TL-2", strat["reason"])
            self.assertNotIn("BattleStartPre/BattleStartAll", strat["reason"])


if __name__ == "__main__":
    unittest.main()
