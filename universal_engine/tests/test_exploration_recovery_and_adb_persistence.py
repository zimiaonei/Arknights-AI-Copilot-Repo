import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from universal_engine import launcher_gui, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_synthetic_guide_popup_image() -> Image.Image:
    """Create a 1280x720 synthetic image matching Arknights full-screen guide carousel layout."""
    img = Image.new("RGB", (1280, 720), (14, 14, 16))
    draw = ImageDraw.Draw(img)
    # Main dark card backdrop
    draw.rectangle((128, 76, 1148, 650), fill=(48, 50, 56), outline=(120, 122, 128), width=2)
    # Top pagination indicator bars (6 segments)
    for i in range(6):
        x0 = 465 + i * 60
        color = (235, 235, 235) if i == 0 else (105, 105, 110)
        draw.rectangle((x0, 30, x0 + 44, 38), fill=color)
    # Top-left "× 关闭" pill
    draw.rectangle((102, 88, 210, 130), fill=(52, 54, 58))
    draw.line((115, 100, 133, 118), fill=(245, 245, 245), width=3)
    draw.line((133, 100, 115, 118), fill=(245, 245, 245), width=3)
    draw.rectangle((146, 100, 195, 118), fill=(235, 235, 235))
    # Right-edge '>' chevron arrow
    draw.line((1175, 330, 1205, 360), fill=(245, 245, 245), width=4)
    draw.line((1205, 360, 1175, 390), fill=(245, 245, 245), width=4)
    return img


class ReclamationResidualCleanupAndRetryTests(unittest.TestCase):
    def test_detect_reclamation_state_on_real_unsettled_drawer_screenshot(self):
        shot = REPO_ROOT / "reports" / "reclamation_end_screen.png"
        if not shot.is_file():
            self.skipTest("real reclamation_end_screen.png not present")
        with Image.open(shot) as im:
            st = stage_pilot.detect_reclamation_screen_state(im, REPO_ROOT, target_stage="RA-1", use_ocr=True)
        self.assertEqual(st["screen"], "relaunch_anchor_residual_settlement")
        self.assertTrue(st["needs_cleanup"])
        self.assertTrue(st["has_residual_task"])
        self.assertTrue(st["drawer_expanded"])
        self.assertFalse(st["ready_for_maa"])
        self.assertIsNotNone(st["settle_xy"])

    def test_cleanup_exits_active_battle_settles_and_collapses_drawer_to_clean_map(self):
        dummy_img = Image.new("RGB", (1280, 720), (40, 44, 52))
        taps = []
        ocr_seq = [
            # Step 1: Active battle inside RA-1
            [{"compact": "请部署罗德岛采集基站", "cx": 640, "cy": 70}],
            # Step 2: Pause menu with 放弃本次行动
            [{"compact": "放弃本次行动", "cx": 420, "cy": 380}],
            # Step 3: Confirm abandon modal
            [{"compact": "是否放弃本次行动", "cx": 640, "cy": 320}, {"compact": "确认", "cx": 860, "cy": 495}],
            # Step 4: Settlement result splash
            [{"compact": "点击任意处继续", "cx": 640, "cy": 640}],
            # Step 5: Big map with right detail drawer still open (blocking map)
            [
                {"compact": "建设元件", "cx": 250, "cy": 24},
                {"compact": "关卡地图", "cx": 930, "cy": 260},
                {"compact": "敌人详情", "cx": 1150, "cy": 260},
                {"compact": "返回", "cx": 75, "cy": 42},
            ],
            # Step 6: Clean unobstructed big map
            [
                {"compact": "建设元件", "cx": 250, "cy": 24},
                {"compact": "建设装置", "cx": 420, "cy": 24},
                {"compact": "统筹系统", "cx": 420, "cy": 678},
            ],
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            res = stage_pilot.cleanup_reclamation_residual_state(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_stage="RA-1", reset_drawer=True
            )
        self.assertTrue(res["ok"])
        self.assertTrue(res["cleaned"])
        self.assertEqual(res["final_screen"], "relaunch_anchor_map")
        self.assertEqual(
            res["actions"],
            ["open_battle_menu", "tap_abandon", "confirm_modal", "tap_settlement_continue", "collapse_drawer_blank"],
        )
        self.assertEqual(taps, [(68, 48), (420, 380), (860, 495), (640, 640), (240, 360)])

    def test_run_reclamation_task_self_heals_mapra1_entry_error_up_to_2_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            (maa / "resource" / "tasks" / "RA").mkdir(parents=True)
            (maa / "resource" / "tasks" / "RA" / "Reclamation3.json").write_text("{}", encoding="utf-8")

            sess = MagicMock()
            sess.maa_dir = maa
            sess.base_dir = Path(tmp)
            sess.adb_path = "adb.exe"
            sess.adb_addr = "127.0.0.1:16384"
            sess.cancel_event = None

            entry_fail = {
                "ok": False,
                "cancelled": False,
                "timed_out": False,
                "elapsed": 12.0,
                "events": [{"message": "SubTaskError", "details": {"first": ["RelaunchAnchor@MapRA1-Entry"]}}],
                "sub_errors": [{"first": ["RelaunchAnchor@MapRA1-Entry"]}],
                "chain_errors": [{"taskchain": "Reclamation"}],
                "log_lines": [],
            }
            entry_ok = {
                "ok": True,
                "cancelled": False,
                "timed_out": False,
                "elapsed": 60.0,
                "events": [{"message": "SubTaskCompleted", "details": {"details": {"task": "RelaunchAnchor@RA@RA1-FinishConstruction"}}}],
                "sub_errors": [],
                "chain_errors": [],
                "log_lines": [],
            }

            # Case 1: Fails twice with MapRA1-Entry, succeeds on 2nd self-healing retry
            sess.run_single_task.side_effect = [entry_fail, entry_fail, entry_ok]
            cleanup_calls = []

            def _fake_cleanup(*args, **kwargs):
                cleanup_calls.append(kwargs.get("reset_drawer"))
                return {"ok": True, "cleaned": True, "cancelled": False, "actions": ["tap_settle"], "final_screen": "relaunch_anchor_map"}

            with patch.object(stage_pilot, "dismiss_guide_popups", return_value={"ok": True, "dismissed_count": 0, "cancelled": False}), \
                 patch.object(stage_pilot, "cleanup_reclamation_residual_state", side_effect=_fake_cleanup), \
                 patch.object(stage_pilot, "navigate_to_reclamation_relaunch_anchor", return_value={"ok": True, "already_in_ra": True, "steps": 0, "reason": "ok"}):
                out = maa_runner.run_reclamation_task({"session": sess}, {}, maa_dir=maa)

            self.assertTrue(out["ok"])
            self.assertEqual(out["entry_retries"], 2)
            self.assertEqual(sess.run_single_task.call_count, 3)
            # Pre-handoff (reset_drawer=False) + 2 retries (reset_drawer=True)
            self.assertEqual(cleanup_calls, [False, True, True])

            # Case 2: Persistent failure stops strictly after 2 retries (3 attempts total)
            sess.run_single_task.reset_mock()
            sess.run_single_task.side_effect = [entry_fail, entry_fail, entry_fail, entry_ok]
            with patch.object(stage_pilot, "dismiss_guide_popups", return_value={"ok": True, "dismissed_count": 0, "cancelled": False}), \
                 patch.object(stage_pilot, "cleanup_reclamation_residual_state", side_effect=_fake_cleanup), \
                 patch.object(stage_pilot, "navigate_to_reclamation_relaunch_anchor", return_value={"ok": True, "already_in_ra": True, "steps": 0, "reason": "ok"}):
                out2 = maa_runner.run_reclamation_task({"session": sess}, {}, maa_dir=maa)

            self.assertFalse(out2["ok"])
            self.assertEqual(out2["entry_retries"], 2)
            self.assertEqual(sess.run_single_task.call_count, 3)


class GuidePopupDismissAndRoguelikeRetryTests(unittest.TestCase):
    def test_detect_guide_popup_visual_and_real_screenshots(self):
        syn = _make_synthetic_guide_popup_image()
        det_vis = stage_pilot.detect_guide_popup(syn, use_ocr=False)
        self.assertTrue(det_vis["detected"])
        self.assertEqual(det_vis["method"], "visual")
        self.assertIsNotNone(det_vis["close_xy"])

        # Real full-screen mechanism guide popups (both OCR & tightened pure-visual MUST detect)
        for pos_rel in (
            Path("debug") / "roguelike" / "2026.11.20-21.43.12.491_raw.png",
            Path("debug") / "roguelike" / "2026.11.20-21.00.36.146_raw.png",
        ):
            pos_path = REPO_ROOT / pos_rel
            if pos_path.is_file():
                with Image.open(pos_path) as im:
                    det_ocr = stage_pilot.detect_guide_popup(im, use_ocr=True)
                    det_only_vis = stage_pilot.detect_guide_popup(im, use_ocr=False)
                self.assertTrue(det_ocr["detected"], f"Missed real guide popup (OCR): {pos_rel}")
                self.assertTrue(det_only_vis["detected"], f"Missed real guide popup (visual): {pos_rel}")
                cx, cy = det_ocr["close_xy"]
                self.assertTrue(30 <= cx <= 260 and 60 <= cy <= 210)

        # Real Sarkaz '思绪-构想 / 思维负荷' dark UI & other non-popup screens MUST NOT trigger false positives
        for neg_rel in (
            Path("debug") / "roguelike" / "2026.11.20-21.44.24.515_raw.png",
            Path("reports") / "reclamation_end_screen.png",
            Path("reports") / "daily_end_screen.png",
        ):
            neg_path = REPO_ROOT / neg_rel
            if neg_path.is_file():
                with Image.open(neg_path) as im:
                    det_neg_ocr = stage_pilot.detect_guide_popup(im, use_ocr=True)
                    det_neg_vis = stage_pilot.detect_guide_popup(im, use_ocr=False)
                self.assertFalse(det_neg_ocr["detected"], f"False positive (OCR) on {neg_rel}: {det_neg_ocr}")
                self.assertFalse(det_neg_vis["detected"], f"False positive (visual) on {neg_rel}: {det_neg_vis}")

    def test_dismiss_guide_popups_closes_stacked_overlays_until_clear(self):
        popup_img = _make_synthetic_guide_popup_image()
        clean_img = Image.new("RGB", (1280, 720), (160, 170, 180))
        taps = []
        ocr_seq = [
            [{"compact": "×关闭", "cx": 153, "cy": 109, "level": "line"}],
            [{"compact": "关闭", "cx": 150, "cy": 112, "level": "line"}],
            [],
        ]
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[popup_img, popup_img, clean_img]), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            res = stage_pilot.dismiss_guide_popups("adb.exe", "127.0.0.1:16384")
        self.assertTrue(res["ok"])
        self.assertEqual(res["dismissed_count"], 2)
        self.assertFalse(res["aborted_no_change"])
        self.assertEqual(taps, [(153, 109), (150, 112)])

    def test_dismiss_guide_popups_aborts_after_2_no_change_clicks(self):
        # Even if detector repeatedly fires on an unchanging screen, MSE check MUST stop after 2 no-change clicks
        static_dark = Image.new("RGB", (1280, 720), (22, 24, 28))
        taps = []
        logs = []
        with patch.object(stage_pilot, "adb_screencap_image", return_value=static_dark), \
             patch.object(
                 stage_pilot,
                 "detect_guide_popup",
                 return_value={"detected": True, "close_xy": (176, 178), "method": "visual", "ocr_text": ""},
             ), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            res = stage_pilot.dismiss_guide_popups("adb.exe", "127.0.0.1:16384", max_popups=8, log_callback=logs.append)
        self.assertTrue(res["ok"])
        self.assertTrue(res["aborted_no_change"])
        self.assertEqual(res["dismissed_count"], 0)
        self.assertEqual(len(taps), 2)
        self.assertTrue(any("无明显变化" in m for m in logs))

        # Case 2: 1 real popup closes (screen changes), then 2 stagnant false-positive taps abort cleanly with dismissed_count=1
        popup_img = _make_synthetic_guide_popup_image()
        taps2 = []
        with patch.object(
            stage_pilot,
            "adb_screencap_image",
            side_effect=[popup_img, static_dark, static_dark, static_dark, static_dark],
        ), patch.object(
            stage_pilot,
            "detect_guide_popup",
            return_value={"detected": True, "close_xy": (153, 109), "method": "ocr+visual", "ocr_text": "×关闭"},
        ), patch.object(
            stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps2.append((x, y)) or True
        ), patch.object(stage_pilot, "wait_interruptible", return_value=False):
            res2 = stage_pilot.dismiss_guide_popups("adb.exe", "127.0.0.1:16384", max_popups=8)
        self.assertTrue(res2["aborted_no_change"])
        self.assertEqual(res2["dismissed_count"], 1)
        self.assertEqual(len(taps2), 3)

    def test_sarkaz_thought_subpanel_detection_and_escape_via_collapse_and_return_fallback(self):
        # 1. Verify real Sarkaz '思绪-构想 / 思维负荷' screenshot is detected with bottom '↓ 收起'
        subpanel_shot = REPO_ROOT / "debug" / "roguelike" / "2026.11.20-21.44.24.515_raw.png"
        if subpanel_shot.is_file():
            with Image.open(subpanel_shot) as im:
                st = stage_pilot.detect_roguelike_subpanel_state(im, maa_dir=REPO_ROOT, use_ocr=True)
            self.assertTrue(st["in_subpanel"])
            self.assertEqual(st["subpanel_type"], "sarkaz_thought_panel")
            self.assertIsNotNone(st["collapse_xy"])
            cx, cy = st["collapse_xy"]
            self.assertTrue(560 <= cx <= 690 and 640 <= cy <= 710)

        # 2. Escape clicks bottom '↓ 收起' -> screen transitions to map -> post-escape guide popup scan runs
        panel_img = Image.new("RGB", (1280, 720), (24, 24, 28))
        map_img = Image.new("RGB", (1280, 720), (140, 120, 110))
        taps = []
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[panel_img, map_img]), \
             patch.object(
                 stage_pilot,
                 "detect_roguelike_subpanel_state",
                 side_effect=[
                     {"in_subpanel": True, "subpanel_type": "sarkaz_thought_panel", "collapse_xy": (623, 694), "return_xy": (45, 42), "markers": ["收起", "思维负荷"]},
                     {"in_subpanel": False, "subpanel_type": "", "collapse_xy": None, "return_xy": None, "markers": []},
                 ],
             ), \
             patch.object(stage_pilot, "dismiss_guide_popups", return_value={"ok": True, "dismissed_count": 1, "clicks": [(153, 109)], "cancelled": False}) as mock_post_guide, \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            esc = stage_pilot.escape_roguelike_subpanel("adb.exe", "127.0.0.1:16384", maa_dir=REPO_ROOT)
        self.assertTrue(esc["ok"])
        self.assertTrue(esc["escaped"])
        self.assertEqual(esc["actions"], ["tap_collapse_thought_panel"])
        self.assertEqual(taps, [(623, 694)])
        self.assertEqual(esc["guide_popups_dismissed"], 1)
        mock_post_guide.assert_called_once()

        # 3. If '↓ 收起' produces no screen change, falls back to top-left '<' return button
        taps_fb = []
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[panel_img, panel_img, panel_img, map_img]), \
             patch.object(
                 stage_pilot,
                 "detect_roguelike_subpanel_state",
                 side_effect=[
                     {"in_subpanel": True, "subpanel_type": "sarkaz_thought_panel", "collapse_xy": (623, 694), "return_xy": (45, 42), "markers": ["收起"]},
                     {"in_subpanel": True, "subpanel_type": "sarkaz_thought_panel", "collapse_xy": (623, 694), "return_xy": (45, 42), "markers": ["收起"]},
                     {"in_subpanel": False, "subpanel_type": "", "collapse_xy": None, "return_xy": None, "markers": []},
                 ],
             ), \
             patch.object(stage_pilot, "dismiss_guide_popups", return_value={"ok": True, "dismissed_count": 0, "clicks": [], "cancelled": False}), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps_fb.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            esc_fb = stage_pilot.escape_roguelike_subpanel("adb.exe", "127.0.0.1:16384", maa_dir=REPO_ROOT)
        self.assertTrue(esc_fb["escaped"])
        self.assertEqual(esc_fb["actions"], ["tap_collapse_thought_panel", "tap_subpanel_return"])
        self.assertEqual(taps_fb, [(623, 694), (45, 42)])

    def test_run_roguelike_task_escapes_sarkaz_strategy_change_error_up_to_3_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            (maa / "resource" / "tasks" / "Roguelike").mkdir(parents=True)
            (maa / "resource" / "tasks" / "Roguelike" / "Sarkaz.json").write_text("{}", encoding="utf-8")

            sess = MagicMock()
            sess.maa_dir = maa
            sess.base_dir = Path(tmp)
            sess.adb_path = "adb.exe"
            sess.adb_addr = "127.0.0.1:16384"
            sess.cancel_event = None

            strat_err = {
                "ok": False,
                "cancelled": False,
                "timed_out": False,
                "elapsed": 55.0,
                "events": [{"message": "SubTaskError", "details": {"first": ["Sarkaz@RoguelikeBegin"], "pre_task": "Sarkaz@Roguelike2StrategyChange"}}],
                "sub_errors": [{"first": ["Sarkaz@RoguelikeBegin"], "pre_task": "Sarkaz@Roguelike2StrategyChange"}],
                "chain_errors": [{"taskchain": "Roguelike"}],
                "log_lines": [],
            }
            rl_ok = {
                "ok": True,
                "cancelled": False,
                "timed_out": False,
                "elapsed": 120.0,
                "events": [{"message": "SubTaskCompleted", "details": {"details": {"task": "Sarkaz@Roguelike@StartExplore"}}}],
                "sub_errors": [],
                "chain_errors": [],
                "log_lines": [],
            }

            # Case 1: Fails 3 times (StrategyChange/Begin), recovers via subpanel escape & succeeds on 3rd retry (4th call)
            sess.run_single_task.side_effect = [strat_err, strat_err, strat_err, rl_ok]
            with patch.object(stage_pilot, "dismiss_guide_popups", return_value={"ok": True, "dismissed_count": 0, "clicks": [], "cancelled": False}), \
                 patch.object(stage_pilot, "escape_roguelike_subpanel", return_value={"ok": True, "escaped": True, "actions": ["tap_collapse_thought_panel"], "guide_popups_dismissed": 0, "cancelled": False}) as mock_esc, \
                 patch("universal_engine.roguelike_ai_copilot.run_roguelike_post_settlement", return_value={"skipped": True}):
                out = maa_runner.run_roguelike_task(
                    {"session": sess},
                    {"roguelike": {"theme": "Sarkaz", "auto_claim_rewards": False, "auto_upgrade_tech_tree": False}},
                    maa_dir=maa,
                    base_dir=Path(tmp),
                )
            self.assertTrue(out["ok"])
            self.assertEqual(out["recovery_retries"], 3)
            self.assertEqual(out["subpanel_escapes"], 3)
            self.assertEqual(mock_esc.call_count, 3)
            self.assertEqual(sess.run_single_task.call_count, 4)

            # Case 2: Persistent failure caps strictly at 3 retries (4 attempts total)
            sess.run_single_task.reset_mock()
            sess.run_single_task.side_effect = [strat_err, strat_err, strat_err, strat_err, rl_ok]
            with patch.object(stage_pilot, "dismiss_guide_popups", return_value={"ok": True, "dismissed_count": 0, "clicks": [], "cancelled": False}), \
                 patch.object(stage_pilot, "escape_roguelike_subpanel", return_value={"ok": True, "escaped": True, "actions": ["tap_collapse_thought_panel"], "guide_popups_dismissed": 0, "cancelled": False}), \
                 patch("universal_engine.roguelike_ai_copilot.run_roguelike_post_settlement", return_value={"skipped": True}):
                out2 = maa_runner.run_roguelike_task(
                    {"session": sess},
                    {"roguelike": {"theme": "Sarkaz", "auto_claim_rewards": False, "auto_upgrade_tech_tree": False}},
                    maa_dir=maa,
                    base_dir=Path(tmp),
                )
            self.assertFalse(out2["ok"])
            self.assertEqual(out2["recovery_retries"], 3)
            self.assertEqual(sess.run_single_task.call_count, 4)

            # Case 3: Cross-theme gui.new.json guard (Mizuki squad/difficulty must NOT leak into Sarkaz)
            cfg_dir = maa / "config"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "gui.new.json").write_text(
                '{"Current":"Default","Configurations":{"Default":{"TaskQueue":[{"TaskType":"Roguelike","Theme":"Mizuki","Mode":"Investment","Difficulty":12,"Squad":"心胜于物分队","Roles":"稳扎稳打"}]}}}',
                encoding="utf-8",
            )
            sarkaz_params = maa_runner.build_roguelike_task_params({"roguelike": {"theme": "Sarkaz"}}, maa_dir=maa)
            self.assertEqual(sarkaz_params["theme"], "Sarkaz")
            self.assertEqual(sarkaz_params["mode"], 0)
            self.assertEqual(sarkaz_params["difficulty"], 2147483647)
            self.assertEqual(sarkaz_params["squad"], "指挥分队")
            self.assertEqual(sarkaz_params["roles"], "取长补短")

    def test_run_roguelike_task_scans_before_handoff_and_retries_on_roguelike_begin_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            (maa / "resource" / "tasks" / "Roguelike").mkdir(parents=True)
            (maa / "resource" / "tasks" / "Roguelike" / "Sarkaz.json").write_text("{}", encoding="utf-8")

            sess = MagicMock()
            sess.maa_dir = maa
            sess.base_dir = Path(tmp)
            sess.adb_path = "adb.exe"
            sess.adb_addr = "127.0.0.1:16384"
            sess.cancel_event = None

            begin_err = {
                "ok": False,
                "cancelled": False,
                "timed_out": False,
                "elapsed": 15.0,
                "events": [{"message": "SubTaskError", "details": {"first": ["Sarkaz@Roguelike@Begin"], "pre_task": "Sarkaz@Roguelike@NextLevel"}}],
                "sub_errors": [{"first": ["Sarkaz@Roguelike@Begin"]}],
                "chain_errors": [{"taskchain": "Roguelike"}],
                "log_lines": [],
            }
            rl_ok = {
                "ok": True,
                "cancelled": False,
                "timed_out": False,
                "elapsed": 90.0,
                "events": [{"message": "SubTaskCompleted", "details": {"details": {"task": "Sarkaz@Roguelike@StartExplore"}}}],
                "sub_errors": [],
                "chain_errors": [],
                "log_lines": [],
            }
            sess.run_single_task.side_effect = [begin_err, rl_ok]

            # 1st dismiss call (pre-handoff): 0 popups; 2nd dismiss call (after Sarkaz@Roguelike@Begin): 1 popup closed -> auto retry!
            dismiss_side = [
                {"ok": True, "dismissed_count": 0, "clicks": [], "cancelled": False},
                {"ok": True, "dismissed_count": 1, "clicks": [(153, 109)], "cancelled": False},
            ]
            with patch.object(stage_pilot, "dismiss_guide_popups", side_effect=dismiss_side) as mock_dismiss, \
                 patch.object(stage_pilot, "escape_roguelike_subpanel", return_value={"ok": True, "escaped": False, "actions": [], "guide_popups_dismissed": 0, "cancelled": False}), \
                 patch("universal_engine.roguelike_ai_copilot.run_roguelike_post_settlement", return_value={"skipped": True}):
                out = maa_runner.run_roguelike_task(
                    {"session": sess},
                    {"roguelike": {"theme": "Sarkaz", "auto_claim_rewards": False, "auto_upgrade_tech_tree": False}},
                    maa_dir=maa,
                    base_dir=Path(tmp),
                )

            self.assertTrue(out["ok"])
            self.assertTrue(out["guide_retry_triggered"])
            self.assertEqual(out["guide_popups_dismissed"], 1)
            self.assertEqual(mock_dismiss.call_count, 2)
            self.assertEqual(sess.run_single_task.call_count, 2)

        # Also verify non-@ variant Sarkaz@RoguelikeBegin and StrategyChange are recognized
        self.assertTrue(
            maa_runner._is_roguelike_begin_error(
                {"ok": False, "cancelled": False, "timed_out": False, "sub_errors": [{"first": ["Sarkaz@RoguelikeBegin"]}], "events": [], "log_lines": []}
            )
        )
        self.assertTrue(
            maa_runner._is_roguelike_begin_error(
                {"ok": False, "cancelled": False, "timed_out": False, "sub_errors": [{"pre_task": "Sarkaz@Roguelike2StrategyChange"}], "events": [], "log_lines": []}
            )
        )


class StopPreservesAdbAndAutoReconnectTests(unittest.TestCase):
    def test_session_close_gracefully_stops_asst_without_killing_adb_server(self):
        sess = maa_runner.MaaSession(maa_dir=REPO_ROOT, config={})
        fake_asst = MagicMock()
        running_states = [True, True, False]
        fake_asst.running.side_effect = lambda: running_states.pop(0) if running_states else False
        sess._asst = fake_asst
        sess._cb_ref = object()

        with patch.object(maa_runner, "_run_cmd") as mock_cmd:
            sess.close()

        fake_asst.stop.assert_called_once()
        mock_cmd.assert_not_called()
        self.assertIsNone(sess._asst)

    def test_check_adb_resolution_always_restarts_server_if_kill_server_ran_and_never_kills_when_cancelled(self):
        # 1. Pre-cancelled event aborts before any kill-server
        cev = threading.Event()
        cev.set()
        with patch.object(env_doctor, "_run_adb") as mock_adb:
            with self.assertRaises(env_doctor.OperationCancelledError):
                env_doctor.check_adb_resolution("adb.exe", adb_address="127.0.0.1:16384", cancel_event=cev)
        mock_adb.assert_not_called()

        # 2. If allow_kill_server=False (used by lightweight probes & post-task refresh), kill-server is NEVER called
        cmds = []

        def _fail_adb(cmd, timeout_sec=5.0, cancel_event=None, text=True):
            cmds.append(cmd)
            return SimpleNamespace(returncode=1, stdout="", stderr="device offline")

        with patch.object(env_doctor, "_run_adb", side_effect=_fail_adb), \
             patch.object(env_doctor, "discover_mumu_manager", return_value=None), \
             patch.object(env_doctor, "_tcp_port_open", return_value=False):
            env_doctor.check_adb_resolution("adb.exe", adb_address="127.0.0.1:16384", allow_kill_server=False)
        self.assertFalse(any("kill-server" in c for c in cmds))

    def test_gui_stop_refreshes_adb_badge_silently_and_next_task_auto_reconnects_before_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            orig_engine = master_scheduler.MaaEngine
            eng = orig_engine(config_path=cfg_path)

            snap_disconnected = {
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": str(tmp),
                "integrity": {"valid": True, "maa_version": "v6.20.1", "reason": "ok"},
                "my_box": {"total_owned": 0, "e2_count": 0},
                "depot": {"syncTime": "", "count": 0},
                "growth_preview": {"ok": False, "recommended_stages": ["1-7"], "reason": "test"},
                "week_key": "2026-W47",
                "annihilation_process": None,
                "adb_info": {"connected": False, "adb_address": "", "width": None, "height": None, "is_16_9": False},
            }
            snap_connected = dict(snap_disconnected)
            snap_connected["adb_info"] = {
                "connected": True,
                "adb_address": "127.0.0.1:16384",
                "width": 1920,
                "height": 1080,
                "is_16_9": True,
            }

            snap_calls = []

            def _fake_snapshot(_eng, check_adb=False):
                snap_calls.append(check_adb)
                return snap_connected if len(snap_calls) >= 2 else snap_disconnected

            probe_calls = []

            def _fake_probe(**kwargs):
                probe_calls.append(kwargs)
                return {
                    "connected": True,
                    "ready": True,
                    "adb_path": "adb.exe",
                    "adb_address": "127.0.0.1:16384",
                    "adb_addr": "127.0.0.1:16384",
                    "width": 1920,
                    "height": 1080,
                    "is_16_9": True,
                    "mumu_adbd_hung": False,
                }

            task_started = threading.Event()

            def _blocking_reclamation(**kwargs):
                task_started.set()
                env_doctor.wait_interruptible(30.0, cancel_event=kwargs.get("cancel_event"), step=0.05)
                return {"ok": False, "cancelled": True}

            daily_called = threading.Event()

            def _fast_daily(**kwargs):
                daily_called.set()
                return {"ok": True, "cancelled": False, "tasks": {}, "server": "Official", "total_elapsed_seconds": 0.1}

            with patch("universal_engine.gui.controller.collect_dashboard_snapshot", side_effect=_fake_snapshot), \
                 patch.object(launcher_gui, "collect_dashboard_snapshot", side_effect=_fake_snapshot), \
                 patch("universal_engine.utils.maa_installer.resolve_maa_path", return_value=Path(tmp)), \
                 patch("universal_engine.utils.env_doctor.discover_adb_executable", return_value=None), \
                 patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(tmp)}), \
                 patch.object(eng, "probe_or_launch_emulator", side_effect=_fake_probe), \
                 patch.object(eng, "run_reclamation", side_effect=_blocking_reclamation), \
                 patch.object(eng, "run_daily", side_effect=_fast_daily):
                LauncherController = getattr(launcher_gui, "LauncherController", None)
                WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
                if LauncherController is None:
                    from universal_engine.gui.controller import LauncherController
                    from universal_engine.gui.bridge import WebViewJsApi

                controller = LauncherController(engine=eng, auto_start_refresh=False)
                api = WebViewJsApi(controller)
                controller._apply_snapshot(snap_disconnected)

                # 1. Start Reclamation while cached ADB is disconnected -> auto-probes & connects BEFORE running!
                api.toggle_reclamation()
                deadline = time.monotonic() + 2.0
                while not task_started.is_set() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(task_started.is_set())
                self.assertEqual(len(probe_calls), 1)

                events_start = controller.drain_events()
                adb_evs = [e["payload"] for e in events_start if e.get("type") in ("adb_status", "snapshot")]
                found_adb = any(
                    "127.0.0.1:16384" in str(payload.get("adb_address", ""))
                    or "127.0.0.1:16384" in str(payload.get("adb", {}).get("adb_address", ""))
                    for payload in adb_evs
                )
                self.assertTrue(found_adb or "127.0.0.1:16384" in str(controller.snapshot_payload))

                # 2. Manually stop Reclamation -> task stops cleanly AND triggers silent check_adb=True refresh
                snap_calls.clear()
                api.toggle_reclamation()
                deadline = time.monotonic() + 2.0
                while (controller.task_running or True not in snap_calls) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(controller.task_running)
                self.assertIn(True, snap_calls)

                # 3. Simulate ADB cache becoming disconnected -> starting Daily automatically calls probe_or_launch_emulator
                controller._apply_adb_status({"connected": False, "adb_address": "", "width": None, "height": None, "is_16_9": False})
                api.start_daily()
                deadline = time.monotonic() + 2.0
                while (controller.task_running or not daily_called.is_set()) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(daily_called.is_set())
                self.assertEqual(len(probe_calls), 2)

                events_daily = controller.drain_events()
                adb_daily = [e["payload"] for e in events_daily if e.get("type") in ("adb_status", "snapshot")]
                found_daily = any(
                    "127.0.0.1:16384" in str(payload.get("adb_address", ""))
                    or "127.0.0.1:16384" in str(payload.get("adb", {}).get("adb_address", ""))
                    for payload in adb_daily
                )
                self.assertTrue(found_daily or "127.0.0.1:16384" in str(controller.snapshot_payload))


if __name__ == "__main__":
    unittest.main()
