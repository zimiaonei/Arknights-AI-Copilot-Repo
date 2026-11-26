import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
from PIL import Image

try:
    import autonomous_agent
except ImportError:
    autonomous_agent = None
from universal_engine import ai_strategist, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class OsEventFixesRegressionTests(unittest.TestCase):
    # ==================================================================
    # 1. Blue-background event map badge accuracy & dynamic mid-chapter -ST- insertion
    # ==================================================================
    def test_blue_bg_map_distinguishes_uncleared_os_st2_and_unplayed_nodes(self):
        shot_os5 = REPO_ROOT / "reports" / "push_abort_OS-ST-2_20261122_224849.png"
        shot_os3 = REPO_ROOT / "reports" / "push_OS-3_victory_round_1.png"
        shot_os1 = REPO_ROOT / "reports" / "push_OS-1_victory_round_1.png"
        if not (shot_os5.is_file() and (REPO_ROOT / "resource" / "template").is_dir() and sys.platform == "win32"):
            return

        with Image.open(shot_os5) as im5:
            nodes5 = stage_pilot.inspect_map_stage_nodes(im5, "OS")
            by5 = {n["code"]: n for n in nodes5}
            self.assertIn("OS-4", by5)
            self.assertIn("OS-ST-2", by5)
            self.assertTrue(by5["OS-2"]["cleared"])
            self.assertTrue(by5["OS-3"]["cleared"])
            self.assertTrue(by5["OS-4"]["cleared"])
            self.assertTrue(by5["OS-4"]["badge_full_3star"])
            self.assertFalse(by5["OS-ST-2"]["cleared"])
            self.assertFalse(by5["OS-ST-2"]["has_cleared_badge"])

            with patch.object(stage_pilot, "adb_screencap_image", return_value=im5):
                prereqs = stage_pilot.discover_unlocked_special_prereqs(
                    "OS-", "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_combat_stage="OS-5"
                )
                self.assertEqual(prereqs, ["OS-ST-2"])
                frontier = stage_pilot.detect_map_frontier_stage(
                    "OS-",
                    "adb.exe",
                    "127.0.0.1:16384",
                    REPO_ROOT,
                    target_stage="OS-10",
                    known_battle_stages=[f"OS-{i}" for i in range(1, 11)],
                    max_swipes=0,
                )
                self.assertEqual(frontier["frontier_stage"], "OS-ST-2")
                self.assertEqual(frontier["frontier_type"], "story")

        if shot_os3.is_file():
            with Image.open(shot_os3) as im3:
                nodes3 = stage_pilot.inspect_map_stage_nodes(im3, "OS")
                by3 = {n["code"]: n for n in nodes3}
                self.assertIn("OS-4", by3)
                # OS-4 sits on bright blue background and is NOT cleared yet!
                self.assertFalse(by3["OS-4"]["cleared"])
                self.assertFalse(by3["OS-4"]["has_cleared_badge"])
                # OS-1 was 2-star cleared (bottom-right notch), OS-2/3 were 3-star cleared
                self.assertTrue(by3["OS-1"]["partial_star"])
                self.assertFalse(by3["OS-1"]["badge_full_3star"])
                self.assertTrue(by3["OS-2"]["badge_full_3star"])
                self.assertTrue(by3["OS-3"]["badge_full_3star"])
                # Closed-loop check on 2-star OS-1 MUST reject even though OS-2 unlocked!
                with patch.object(stage_pilot, "adb_screencap_image", return_value=im3):
                    mv = stage_pilot.verify_stage_cleared_on_map(
                        "OS-1",
                        "adb.exe",
                        "127.0.0.1:16384",
                        REPO_ROOT,
                        next_stage="OS-2",
                        max_swipes=0,
                        settlement_stars=None,
                    )
                self.assertFalse(mv["verified"])
                self.assertTrue(mv["non_3_star"])

        if shot_os1.is_file():
            with Image.open(shot_os1) as im1:
                nodes1 = stage_pilot.inspect_map_stage_nodes(im1, "OS")
                by1 = {n["code"]: n for n in nodes1}
                self.assertIn("OS-1", by1)
                self.assertTrue(by1["OS-1"]["cleared"])
                if by1["OS-1"]["partial_star"]:
                    with patch.object(stage_pilot, "adb_screencap_image", return_value=im1):
                        mv1 = stage_pilot.verify_stage_cleared_on_map(
                            "OS-1",
                            "adb.exe",
                            "127.0.0.1:16384",
                            REPO_ROOT,
                            next_stage="OS-2",
                            max_swipes=0,
                            settlement_stars=None,
                        )
                    self.assertFalse(mv1["verified"])
                    self.assertTrue(mv1["non_3_star"])
                else:
                    self.assertTrue(by1["OS-1"]["badge_full_3star"])

    def test_auto_push_dynamically_inserts_mid_chapter_st_before_next_battle(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            executed: list[tuple[str, bool]] = []

            def _fake_prereqs(prefix, adb_p, adb_a, maa_p, target_combat_stage="", frontier_stage="", **kwargs):
                # Once OS-4 is cleared and OS-5 is next, OS-ST-2 newly appears on the right
                if target_combat_stage == "OS-5" and ("OS-ST-2", True) not in executed:
                    return ["OS-ST-2"]
                return []

            def _fake_clear(st, **kwargs):
                executed.append((st, bool(kwargs.get("skip_if_already_3star"))))
                return True, {"stage": st, "victory": True, "map_clear_verified": True}

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler._resolve_auto_push_stages", return_value=["OS-4", "OS-5"]), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch.object(stage_pilot, "discover_next_uncleared_stage_on_map", return_value={"found": False, "on_stage_map": False, "all_cleared": False, "reason": "not_on_stage_map"}), \
                     patch.object(stage_pilot, "detect_map_frontier_stage", return_value={"on_stage_map": True, "frontier_stage": "OS-4", "push_sequence": ["OS-4", "OS-5"]}), \
                     patch.object(stage_pilot, "discover_unlocked_special_prereqs", side_effect=_fake_prereqs), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res = eng.run_push(["auto"], refresh_cache_flag=False)

            self.assertTrue(res["ok"])
            self.assertEqual([s for s, _ in executed], ["OS-4", "OS-ST-2", "OS-5"])
            self.assertTrue(all(flag for _, flag in executed))

    # ==================================================================
    # 2. Hard 3-star standard: settlement 2/1 star -> battle_failed -> switch guide / repair & retry
    # ==================================================================
    def test_settlement_star_templates_distinguish_3_vs_2_vs_1_stars(self):
        tpl_dir = REPO_ROOT / "resource" / "template" / "Battle"
        t3_p = tpl_dir / "StageDrops" / "StageDrops-Stars-3.png"
        t2_p = tpl_dir / "StageDrops" / "StageDrops-Stars-2.png"
        eoa_p = tpl_dir / "BattleFlag" / "EndOfAction.png"
        if not (t3_p.is_file() and t2_p.is_file() and eoa_p.is_file()):
            return

        def _make_settle(star_img: Image.Image) -> Image.Image:
            canvas = Image.new("RGB", (1280, 720), (14, 16, 20))
            with Image.open(eoa_p) as eoa:
                canvas.paste(eoa.convert("RGB"), (18, 285))
            canvas.paste(star_img.convert("RGB"), (72, 275))
            return canvas

        with Image.open(t3_p) as s3:
            st3 = stage_pilot.analyze_screen_state(_make_settle(s3), REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st3["is_battle_victory"])
            self.assertEqual(st3["settlement_stars"], 3)

        with Image.open(t2_p) as s2:
            st2 = stage_pilot.analyze_screen_state(_make_settle(s2), REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st2["is_battle_victory"])
            self.assertEqual(st2["settlement_stars"], 2)

            # Construct 1-star image by dimming slot 2 of StageDrops-Stars-2.png
            arr1 = np.asarray(s2.convert("RGB")).copy()
            w3 = arr1.shape[1] // 3
            arr1[:, w3 : 2 * w3] = arr1[:, 2 * w3 : 2 * w3 + w3]
            st1 = stage_pilot.analyze_screen_state(_make_settle(Image.fromarray(arr1)), REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st1["is_battle_victory"])
            self.assertEqual(st1["settlement_stars"], 1)

    def test_non_3_star_is_battle_failed_and_triggers_guide_switch_until_3_star(self):
        # A. evaluate_copilot_victory rejects 1-star & 2-star
        base_m = {
            "completed_flag": True,
            "failed_flag": False,
            "battle_failed": False,
            "victory_settlement_confirmed": True,
            "auto_deploy_not_unlocked": False,
            "errors": [],
            "battle_started": True,
        }
        for bad_star in (1, 2):
            v = ai_strategist.evaluate_copilot_victory(
                {**base_m, "settlement_stars": bad_star},
                post_battle_state={"battle_victory_seen": True, "settlement_stars": bad_star},
                map_verify={"verified": True, "skipped": False},
                require_map_verify=True,
            )
            self.assertFalse(v["victory"])
            self.assertTrue(v["battle_failed"])
            self.assertTrue(v["non_3_star"])
            self.assertEqual(v["settlement_stars"], bad_star)

        # B. stars=? fallback: open OS-1 detail panel (real push_OS-1_fail_round_1.png with 2 stars) -> reject!
        shot_fail = REPO_ROOT / "reports" / "push_OS-1_fail_round_1.png"
        if shot_fail.is_file() and sys.platform == "win32":
            with Image.open(shot_fail) as im_panel:
                panel = stage_pilot.inspect_stage_detail_panel(im_panel, REPO_ROOT, expected_stage="OS-1", use_ocr=True)
                self.assertEqual(panel["displayed_stage"], "OS-1")
                self.assertEqual(panel["star_count"], 2)
                self.assertFalse(panel["has_three_stars"])

                with patch.object(stage_pilot, "adb_screencap_image", return_value=im_panel):
                    mv = stage_pilot.verify_stage_cleared_on_map(
                        "OS-1",
                        "adb.exe",
                        "127.0.0.1:16384",
                        REPO_ROOT,
                        next_stage="OS-2",
                        max_swipes=0,
                        settlement_stars=None,
                    )
                self.assertFalse(mv["verified"])
                self.assertTrue(mv["non_3_star"])
                self.assertEqual(mv["verified_stars"], 2)

        # C. autonomous_clear_stage switches guide & retries when Round 1 yields 2 stars
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "OS-1", "stageId": "act46side_01", "apCost": 9}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({"total_owned": 1, "e2_count": 1, "e2_operators": [{"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}], "all_owned": {"棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}}}),
                encoding="utf-8",
            )
            guides = [
                {"id": 73176, "like": 500, "views": 1000, "content": {"stage_name": "act46side_01", "opers": [{"name": "棘刺", "skill": 3}], "actions": []}},
                {"id": 73999, "like": 300, "views": 800, "content": {"stage_name": "act46side_01", "opers": [{"name": "棘刺", "skill": 3}], "actions": []}},
            ]
            rep_2star = {
                "round": 1, "victory": False, "battle_failed": True, "non_3_star": True, "map_clear_verified": False,
                "dual_victory_check": {"victory": False, "battle_failed": True, "non_3_star": True, "settlement_stars": 2, "reasons": ["battle_failed (non_3_star, stars=2)"]},
                "metrics": {"kills": 20, "costs": 15, "errors": [], "failed_flag": True, "battle_failed": True, "completed_flag": True, "settlement_stars": 2, "non_3_star": True, "entry_failed_tasks": []},
                "screenshot": "", "report_path": "",
            }
            rep_3star = {
                "round": 2, "victory": True, "battle_failed": False, "non_3_star": False, "map_clear_verified": True,
                "dual_victory_check": {"victory": True, "battle_failed": False, "non_3_star": False, "settlement_stars": 3, "reasons": []},
                "metrics": {"kills": 22, "costs": 15, "errors": [], "failed_flag": False, "battle_failed": False, "completed_flag": True, "settlement_stars": 3, "non_3_star": False, "entry_failed_tasks": []},
                "screenshot": "", "report_path": "",
            }
            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=[(1, rep_2star), (0, rep_3star)]) as mock_round:
                ok, summary = maa_runner.autonomous_clear_stage("OS-1", maa_dir=maa, config={}, max_rounds=3, base_dir=base)
            self.assertTrue(ok)
            self.assertEqual(mock_round.call_count, 2)
            self.assertEqual(summary["final_guide_id"], 73999)
            self.assertTrue(summary["history"][0]["non_3_star"])
            self.assertEqual(summary["history"][0]["settlement_stars"], 2)

    # ==================================================================
    # 3. Manual OS-1 retry: no skip, no secondary map search in formation, retry StartButton1, uncheck Auto-Deploy
    # ==================================================================
    def test_manual_os1_entry_no_skip_retries_start_and_unchecks_auto_deploy(self):
        # A. MaaEngine.run_push passes skip_if_already_3star=False for manual stages, True for auto
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            seen_flags: list[tuple[str, bool]] = []

            def _fake_clear(st, **kwargs):
                seen_flags.append((st, bool(kwargs.get("skip_if_already_3star"))))
                return True, {"stage": st, "victory": True, "map_clear_verified": True}

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res_manual = eng.run_push(["OS-1"], refresh_cache_flag=False)
            self.assertTrue(res_manual["ok"])
            self.assertEqual(seen_flags, [("OS-1", False)])

        # B. Already in formation (require_fresh_map_entry=False) -> immediate handoff without map search
        dummy = Image.new("RGB", (1280, 720), (30, 32, 40))
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"in_battle": False, "in_formation": True, "in_formation_select": False}), \
             patch.object(stage_pilot, "enter_stage_node") as mock_enter:
            prep_in = stage_pilot.prepare_battle_entry_if_on_map("OS-1", "adb.exe", "127.0.0.1:16384", REPO_ROOT, require_fresh_map_entry=False)
        self.assertTrue(prep_in["entered"])
        self.assertEqual(prep_in["outcome"], "formation")
        mock_enter.assert_not_called()

        # C. Real push_OS-1_fail_round_1.png: StartButton1 tap swallowed once -> run_story_skip_loop auto-re-taps Start & enters formation!
        shot_os1 = REPO_ROOT / "reports" / "push_OS-1_fail_round_1.png"
        if shot_os1.is_file() and (REPO_ROOT / "resource" / "template").is_dir() and sys.platform == "win32":
            with Image.open(shot_os1) as im_os1:
                im_panel = im_os1.convert("RGB")
            taps: list[tuple[int, int]] = []
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=[im_panel, im_panel, im_panel, dummy]), \
                 patch.object(
                     stage_pilot,
                     "analyze_screen_state",
                     side_effect=[
                         {"in_battle": False, "in_formation": False, "in_formation_select": False, "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False, "on_stage_map": True, "has_stage_start_btn": True, "stage_start_xy": (1142, 664)},
                         {"in_battle": False, "in_formation": False, "has_stage_start_btn": True, "stage_start_xy": (1142, 664)},
                         {"in_battle": False, "in_formation": False, "has_stage_start_btn": True, "stage_start_xy": (1142, 664), "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "has_skip_confirm": False, "has_skip_button": False, "has_end_of_plot": False, "has_story_dialog": False, "on_stage_map": True},
                         {"in_battle": False, "in_formation": True, "has_stage_start_btn": False, "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "has_skip_confirm": False, "has_skip_button": False, "has_end_of_plot": False, "has_story_dialog": False, "on_stage_map": False},
                     ],
                 ), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                prep = stage_pilot.prepare_battle_entry_if_on_map(
                    "OS-1", "adb.exe", "127.0.0.1:16384", REPO_ROOT, skip_if_already_3star=False, uncheck_auto_deploy=True
                )
            self.assertTrue(prep["entered"])
            self.assertEqual(prep["outcome"], "formation")
            self.assertEqual(taps, [(1142, 664), (1142, 664)])

        # D. Checked Auto-Deploy (UsePrtsSuccess + NotUsePrts) on 3-star manual retry -> unchecks before Start!
        up_ok = REPO_ROOT / "resource" / "template" / "Battle" / "UsePrts" / "UsePrtsSuccess.png"
        not_up = REPO_ROOT / "resource" / "template" / "Battle" / "UsePrts" / "NotUsePrts.png"
        if up_ok.is_file() and not_up.is_file():
            panel_checked = Image.new("RGB", (1280, 720), (30, 32, 38))
            for sx in (1040, 1095, 1150):
                for dx in range(-8, 9):
                    for dy in range(-8, 9):
                        if abs(dx) + abs(dy) <= 9:
                            panel_checked.putpixel((sx + dx, 110 + dy), (40, 185, 250))
            with Image.open(up_ok) as im_chk:
                panel_checked.paste(im_chk.convert("RGB"), (1010, 565))
            with Image.open(not_up) as im_bar:
                panel_checked.paste(im_bar.convert("RGB"), (1105, 573))

            with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[{"text": "OPERATION OS-1", "compact": "OPERATIONOS-1"}]):
                det = stage_pilot.inspect_stage_detail_panel(panel_checked, REPO_ROOT, expected_stage="OS-1", use_ocr=True)
            self.assertTrue(det["has_auto_deploy"])
            self.assertTrue(det["auto_deploy_checked"])
            self.assertTrue(det["already_cleared_3star"])

            taps_chk: list[tuple[int, int]] = []
            with patch.object(stage_pilot, "adb_screencap_image", return_value=panel_checked), \
                 patch.object(stage_pilot, "analyze_screen_state", return_value={"in_formation": False, "in_battle": False, "has_stage_start_btn": True, "stage_start_xy": (1142, 664)}), \
                 patch.object(stage_pilot, "inspect_stage_detail_panel", return_value=det), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps_chk.append((x, y)) or True), \
                 patch("time.sleep"):
                res_go = stage_pilot.enter_stage_node(
                    "OS-1", "adb.exe", "127.0.0.1:16384", REPO_ROOT, click_start=True, skip_if_already_3star=False, uncheck_auto_deploy=True
                )
            self.assertTrue(res_go["entered"])
            self.assertEqual(len(taps_chk), 2)
            self.assertNotEqual(taps_chk[0], (1142, 664))
            self.assertEqual(taps_chk[1], (1142, 664))

    # ==================================================================
    # 4. Centered STORY / 开始剧情 modal detection, click-to-skip & queue pruning
    # ==================================================================
    @staticmethod
    def _make_story_card_modal_image(with_play_tpl: bool = True) -> Image.Image:
        canvas = Image.new("RGB", (1280, 720), (38, 42, 54))
        # Left dark title block: x∈[0.18, 0.38] -> 230..486, y∈[0.36, 0.66] -> 259..475
        for px in range(230, 486):
            for py in range(259, 475):
                canvas.putpixel((px, py), (28, 30, 36))
        # Red REC accent at top of left block
        for px in range(248, 290):
            for py in range(274, 292):
                canvas.putpixel((px, py), (210, 45, 45))
        # Middle light-gray synopsis block: x∈[0.38, 0.71] -> 486..909, y∈[0.36, 0.66] -> 259..475
        for px in range(486, 909):
            for py in range(259, 475):
                canvas.putpixel((px, py), (212, 214, 218))
        # Right blue start block: x∈[0.71, 0.82] -> 909..1050, y∈[0.36, 0.66] -> 259..475
        for px in range(909, 1050):
            for py in range(259, 475):
                canvas.putpixel((px, py), (0, 114, 178))
        tpl_plot = REPO_ROOT / "resource" / "template" / "Battle" / "StartButton" / "BattleStartPlot.png"
        if with_play_tpl and tpl_plot.is_file():
            with Image.open(tpl_plot) as im_p:
                canvas.paste(im_p.convert("RGB"), (924, 296))
        return canvas

    def test_detect_story_card_modal_ocr_fallback_and_negative_guards(self):
        modal_img = self._make_story_card_modal_image(with_play_tpl=True)

        # 1. analyze_screen_state separates BattleStartPlot.png into has_story_start_btn (NOT has_stage_start_btn)
        if (REPO_ROOT / "resource" / "template" / "Battle" / "StartButton" / "BattleStartPlot.png").is_file():
            st_modal = stage_pilot.analyze_screen_state(modal_img, REPO_ROOT, use_ocr_fallback=False)
            self.assertTrue(st_modal["has_story_start_btn"])
            self.assertFalse(st_modal["has_stage_start_btn"])
            self.assertFalse(st_modal["on_stage_map"])

        # 2. OCR detects left OS-ST-2 + STORY + 圣巡与丧钟 and right 开始剧情 center
        left_boxes_ok = [
            {"text": "REC", "x": 248, "y": 274, "w": 38, "h": 14, "cx": 267, "cy": 281, "pass": "norm", "level": "word"},
            {"text": "OS-ST-2", "x": 255, "y": 332, "w": 124, "h": 32, "cx": 317, "cy": 348, "pass": "inv", "level": "word"},
            {"text": "STORY", "x": 255, "y": 374, "w": 68, "h": 16, "cx": 289, "cy": 382, "pass": "norm", "level": "word"},
            {"text": "圣巡与丧钟", "x": 255, "y": 398, "w": 115, "h": 22, "cx": 312, "cy": 409, "pass": "norm", "level": "word"},
        ]
        right_boxes_ok = [
            {"text": "开始剧情", "compact": "开始剧情", "x": 946, "y": 414, "w": 68, "h": 20, "cx": 980, "cy": 424, "pass": "inv", "level": "line"},
        ]
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=[left_boxes_ok, right_boxes_ok]):
            det = stage_pilot.detect_story_card_modal(modal_img, REPO_ROOT, expected_stage="OS-ST-2", use_ocr=True)
        self.assertTrue(det["is_story_card_modal"])
        self.assertEqual(det["displayed_stage"], "OS-ST-2")
        self.assertTrue(det["title_matches"])
        self.assertTrue(det["has_story_marker"])
        self.assertTrue(det["has_start_story_btn_ocr"])
        self.assertEqual(det["story_name"], "圣巡与丧钟")
        self.assertEqual(det["start_story_xy"], (980, 424))
        self.assertEqual(det["start_story_xy_source"], "ocr")

        # 3. Mismatched title (OS-ST-1 shown when expecting OS-ST-2) -> title_matches=False
        left_boxes_mm = [
            {"text": "OS-ST-1", "x": 255, "y": 332, "w": 124, "h": 32, "cx": 317, "cy": 348, "pass": "inv", "level": "word"},
            {"text": "STORY", "x": 255, "y": 374, "w": 68, "h": 16, "cx": 289, "cy": 382, "pass": "norm", "level": "word"},
        ]
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=[left_boxes_mm, right_boxes_ok]):
            det_mm = stage_pilot.detect_story_card_modal(modal_img, REPO_ROOT, expected_stage="OS-ST-2", use_ocr=True)
        self.assertTrue(det_mm["is_story_card_modal"])
        self.assertEqual(det_mm["displayed_stage"], "OS-ST-1")
        self.assertFalse(det_mm["title_matches"])

        # 4. OCR missing right button & no template -> falls back to normalized (0.765, 0.51) == (979, 367)
        modal_no_tpl = self._make_story_card_modal_image(with_play_tpl=False)
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=[left_boxes_ok, []]):
            det_fb = stage_pilot.detect_story_card_modal(modal_no_tpl, REPO_ROOT, expected_stage="OS-ST-2", use_ocr=True)
        self.assertTrue(det_fb["is_story_card_modal"])
        self.assertTrue(det_fb["title_matches"])
        self.assertEqual(det_fb["start_story_xy"], (979, 367))
        self.assertEqual(det_fb["start_story_xy_source"], "fallback")

        # 5. Negative guards on real map and battle detail screenshots (must NOT misclassify as story card modal)
        for rel in ("reports/push_abort_OS-ST-2_20261122_224849.png", "reports/push_OS-1_fail_round_1.png"):
            p = REPO_ROOT / rel
            if p.is_file():
                with Image.open(p) as im_real:
                    neg = stage_pilot.detect_story_card_modal(im_real, REPO_ROOT, expected_stage="OS-ST-2", use_ocr=False)
                self.assertFalse(neg["is_story_card_modal"], msg=f"False story modal on {rel}")

    def test_enter_and_handle_story_stage_never_calls_detail_panel_and_skips_to_next_unlock(self):
        map_img = Image.new("RGB", (1280, 720), (35, 38, 46))
        modal_img = self._make_story_card_modal_image(with_play_tpl=False)
        story_img = Image.new("RGB", (1280, 720), (20, 22, 26))

        # Case A: Click OS-ST-2 (640, 175) -> centered story card appears -> NEVER call inspect_stage_detail_panel ->
        #         click 开始剧情 -> SKIP + confirm -> map refreshes with OS-5 unlocked
        taps: list[tuple[int, int]] = []
        cap_seq = [map_img, map_img, modal_img, story_img, story_img, map_img, map_img, map_img]
        state_seq = [
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": True, "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False, "on_stage_map": False},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "has_skip_button": True, "skip_xy": (1200, 48), "has_skip_confirm": False, "has_end_of_plot": False, "has_story_dialog": True, "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "on_stage_map": False},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "has_skip_button": False, "has_skip_confirm": True, "confirm_xy": (795, 495), "has_end_of_plot": False, "has_story_dialog": True, "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "on_stage_map": False},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "has_skip_button": False, "has_skip_confirm": False, "has_end_of_plot": False, "has_story_dialog": False, "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "has_skip_button": False, "has_skip_confirm": False, "has_end_of_plot": False, "has_story_dialog": False, "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "has_skip_button": False, "has_skip_confirm": False, "has_end_of_plot": False, "has_story_dialog": False, "is_battle_failed": False, "is_battle_victory": False, "has_end_of_action": False, "on_stage_map": True},
        ]
        modal_det = {
            "is_story_card_modal": True,
            "displayed_stage": "OS-ST-2",
            "raw_title_text": "OS-ST-2 | STORY",
            "expected_stage": "OS-ST-2",
            "title_matches": True,
            "start_story_xy": (980, 424),
            "start_story_xy_source": "ocr",
        }
        nodes_after = [
            {"code": "OS-4", "node_type": "battle", "cleared": True, "badge_full_3star": True, "partial_star": False, "cx": 420},
            {"code": "OS-ST-2", "node_type": "story", "cleared": True, "badge_full_3star": True, "partial_star": False, "cx": 640},
            {"code": "OS-5", "node_type": "battle", "cleared": False, "badge_full_3star": False, "partial_star": False, "is_white_unlocked_card": True, "cx": 860},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=cap_seq), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=state_seq), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=(640, 175, {"x": 580, "y": 155, "w": 120, "h": 40})), \
             patch.object(stage_pilot, "detect_story_card_modal", return_value=modal_det), \
             patch.object(stage_pilot, "inspect_stage_detail_panel") as mock_panel, \
             patch.object(stage_pilot, "inspect_map_stage_nodes", return_value=nodes_after), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            ok, story_res = stage_pilot.handle_story_stage(
                "OS-ST-2", "adb.exe", "127.0.0.1:16384", REPO_ROOT, next_stage="OS-5"
            )
        self.assertTrue(ok)
        mock_panel.assert_not_called()
        self.assertTrue(story_res["story_card_modal"])
        self.assertTrue(story_res["map_refreshed"])
        self.assertIn("OS-5", story_res["unlocked_next_stages"])
        # Taps: node (640, 175) -> 开始剧情 (980, 424) -> SKIP (1200, 48) -> confirm (795, 495)
        self.assertEqual(taps[:4], [(640, 175), (980, 424), (1200, 48), (795, 495)])

        # Case B: Centered story card title mismatch twice (OS-ST-1 != OS-ST-2) -> close modal, NEVER click 开始剧情, abort!
        taps_mm: list[tuple[int, int]] = []
        state_mm = [
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": True, "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": False, "on_stage_map": True},
            {"in_formation": False, "in_battle": False, "has_stage_start_btn": False, "has_story_start_btn": True, "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False},
        ]
        modal_mm = {
            "is_story_card_modal": True,
            "displayed_stage": "OS-ST-1",
            "raw_title_text": "OS-ST-1 | STORY",
            "expected_stage": "OS-ST-2",
            "title_matches": False,
            "start_story_xy": (979, 367),
            "start_story_xy_source": "fallback",
        }
        with patch.object(stage_pilot, "adb_screencap_image", return_value=modal_img), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=state_mm), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=(640, 175, {"x": 580, "y": 155, "w": 120, "h": 40})), \
             patch.object(stage_pilot, "detect_story_card_modal", return_value=modal_mm), \
             patch.object(stage_pilot, "inspect_stage_detail_panel") as mock_panel_mm, \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps_mm.append((x, y)) or True), \
             patch("time.sleep"):
            ok_mm, res_mm = stage_pilot.handle_story_stage("OS-ST-2", "adb.exe", "127.0.0.1:16384", REPO_ROOT)
        self.assertFalse(ok_mm)
        mock_panel_mm.assert_not_called()
        self.assertTrue(res_mm["stage_mismatch"])
        self.assertNotIn((979, 367), taps_mm)
        self.assertEqual(taps_mm, [(640, 175), (110, 360), (640, 175), (110, 360)])

        # Case C: Both maa_runner.autonomous_clear_stage & autonomous_agent.autonomous_clear_stage forward next_stage
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            with patch.object(maa_runner, "ensure_game_running", return_value="com.hypergryph.arknights"), \
                 patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
                 patch.object(maa_runner, "_probe_adb_address", return_value="127.0.0.1:16384"), \
                 patch.object(stage_pilot, "handle_story_stage", return_value=(True, {"stage": "OS-ST-2", "node_type": "story", "victory": True})) as mock_hs1:
                ok_mr, _ = maa_runner.autonomous_clear_stage("OS-ST-2", maa_dir=maa, config={}, base_dir=base, next_stage="OS-5")
            self.assertTrue(ok_mr)
            self.assertEqual(mock_hs1.call_args.kwargs.get("next_stage"), "OS-5")

        if autonomous_agent is not None:
            with patch.object(autonomous_agent, "ensure_game_running"), \
                 patch.object(autonomous_agent, "resolve_stage_info", return_value={"code": "OS-ST-2", "stage_id": "OS-ST-2"}), \
                 patch.object(stage_pilot, "handle_story_stage", return_value=(True, {"stage": "OS-ST-2", "node_type": "story", "victory": True})) as mock_hs2:
                ok_aa, sum_aa = autonomous_agent.autonomous_clear_stage("OS-ST-2", next_stage="OS-5")
            self.assertTrue(ok_aa)
            self.assertEqual(sum_aa["stage_id"], "OS-ST-2")
            self.assertEqual(mock_hs2.call_args.kwargs.get("next_stage"), "OS-5")

    def test_mid_chapter_story_queue_prunes_3star_keeps_partial_star_and_advances_to_os5(self):
        # 1. Real OS-ST-2 map screenshot (timestamped immutable capture): 3-star OS-2~OS-4 must NOT be queued behind OS-ST-2
        for rel in ("reports/push_abort_OS-ST-2_20261122_224849.png",):
            p = REPO_ROOT / rel
            if p.is_file() and (REPO_ROOT / "resource" / "template").is_dir() and sys.platform == "win32":
                with Image.open(p) as im:
                    ev_info = stage_pilot.discover_event_from_map_image(
                        im, maa_dir=REPO_ROOT, known_battle_stages=[f"OS-{i}" for i in range(1, 11)]
                    )
                self.assertEqual(ev_info["frontier_stage"], "OS-ST-2")
                self.assertEqual(ev_info["push_sequence"], ["OS-ST-2", "OS-5", "OS-6", "OS-7", "OS-8", "OS-9", "OS-10"])

        # 2. Synthetic map where OS-1 is <3-star (partial_star=True) while OS-2~4 are 3-star -> OS-ST-2, OS-1, OS-5..10
        vis_partial = [
            {"code": "OS-1", "node_type": "battle", "cleared": True, "badge_full_3star": False, "partial_star": True},
            {"code": "OS-2", "node_type": "battle", "cleared": True, "badge_full_3star": True, "partial_star": False},
            {"code": "OS-3", "node_type": "battle", "cleared": True, "badge_full_3star": True, "partial_star": False},
            {"code": "OS-4", "node_type": "battle", "cleared": True, "badge_full_3star": True, "partial_star": False},
            {"code": "OS-ST-2", "node_type": "story", "cleared": False, "badge_full_3star": False, "partial_star": False},
        ]
        seq_gap = stage_pilot._build_sequential_push_chain(
            "OS-ST-2",
            "OS-10",
            known_battle_stages=[f"OS-{i}" for i in range(1, 11)],
            visible_nodes=vis_partial,
        )
        self.assertEqual(seq_gap, ["OS-ST-2", "OS-1", "OS-5", "OS-6", "OS-7", "OS-8", "OS-9", "OS-10"])

        # 3. MaaEngine.run_push(["auto"]) skips 3-star OS-2~OS-4 reported in OS-ST-2 visible_nodes_after,
        #    while still executing partial-star OS-1 and newly unlocked OS-5!
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            executed: list[str] = []

            def _fake_clear(st, **kwargs):
                executed.append(st)
                if st == "OS-ST-2":
                    return True, {
                        "stage": "OS-ST-2",
                        "node_type": "story",
                        "victory": True,
                        "skipped_story": True,
                        "visible_nodes_after": vis_partial,
                    }
                return True, {"stage": st, "victory": True, "map_clear_verified": True}

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler._resolve_auto_push_stages", return_value=["OS-1", "OS-2", "OS-3", "OS-4", "OS-5"]), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch.object(stage_pilot, "discover_next_uncleared_stage_on_map", return_value={"found": False, "on_stage_map": False, "all_cleared": False, "reason": "not_on_stage_map"}), \
                     patch.object(
                         stage_pilot,
                         "detect_map_frontier_stage",
                         return_value={
                             "on_stage_map": True,
                             "frontier_stage": "OS-ST-2",
                             "push_sequence": ["OS-ST-2", "OS-1", "OS-2", "OS-3", "OS-4", "OS-5"],
                         },
                     ), \
                     patch.object(stage_pilot, "discover_unlocked_special_prereqs", return_value=[]), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res = eng.run_push(["auto"], refresh_cache_flag=False)

            self.assertTrue(res["ok"])
            self.assertEqual(executed, ["OS-ST-2", "OS-1", "OS-5"])


if __name__ == "__main__":
    unittest.main()
