import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

try:
    import master_scheduler as root_scheduler
except ImportError:
    root_scheduler = None
from universal_engine import maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_synthetic_map(
    nodes_spec: list[dict],
    *,
    bg: tuple[int, int, int] = (225, 228, 232),
) -> Image.Image:
    """Create a 1280x720 synthetic map image with white/dark stage cards and optional cyan 3-star badges."""
    img = Image.new("RGB", (1280, 720), bg)
    draw = ImageDraw.Draw(img)
    for spec in nodes_spec:
        x, y, w, h = spec["x"], spec["y"], spec.get("w", 112), spec.get("h", 34)
        is_dark = bool(spec.get("dark", False))
        card_fill = (34, 36, 40) if is_dark else (246, 247, 249)
        hdr_fill = (16, 18, 20)
        draw.rectangle([x, y - 10, x + w, y], fill=hdr_fill)
        draw.rectangle([x, y, x + w, y + h], fill=card_fill)
        if spec.get("cleared", False):
            # Cyan 3-star hexagon badge hugging the left edge of the card
            draw.rectangle([x - 30, y - 2, x + 2, y + h - 4], fill=(32, 178, 235))
        elif spec.get("partial_star", False):
            # 2-star badge with dark bottom-right notch
            draw.rectangle([x - 30, y - 2, x + 2, y + h - 4], fill=(32, 178, 235))
            draw.rectangle([x - 14, y + h // 2 - 2, x + 2, y + h - 4], fill=(12, 14, 18))
    return img


class UniversalDynamicPushTests(unittest.TestCase):
    def test_universal_stage_regex_normalization_validation_and_classification(self):
        # Valid full-format codes across mainline, side, hell, special, tutorial, story, and XX-XX-XX
        valid_codes = [
            "0-1", "1-7", "16-2", "16-12",
            "S4-1", "H12-1", "R8-1", "M8-6", "JT8-2", "END8-1",
            "TR-1", "TR-27", "ST-1", "EG-1",
            "OS-1", "OS-ST-2", "OS-TR-1", "OS-EX-1", "DH-MO-1",
            "PR-A-1", "AB-CD-12", "XX-XX-01",
        ]
        for code in valid_codes:
            self.assertTrue(stage_pilot.is_valid_stage_code(code), msg=f"Should be valid: {code}")

        # Noise / non-stage strings must be rejected
        invalid_codes = [
            "2026-11-22", "26-11-22", "11-05", "08-15", "135-135", "120-20",
            "16-0", "1-135", "V6-17-5", "VER-1-2", "SAN-10", "LV-80",
            "TUTOR-1", "D-16-2", "VEC-A", "OPERATION", "16",
        ]
        for bad in invalid_codes:
            self.assertFalse(stage_pilot.is_valid_stage_code(bad), msg=f"Should be invalid: {bad}")

        # OCR normalization across space-separated, CJK dash, and icon-prefixed strings
        self.assertEqual(stage_pilot.extract_universal_stage_code("16 一 2"), "16-2")
        self.assertEqual(stage_pilot.extract_universal_stage_code("D 16 一 2 《"), "16-2")
        self.assertEqual(stage_pilot.extract_universal_stage_code("DH MO 1"), "DH-MO-1")
        self.assertEqual(stage_pilot.extract_universal_stage_code("OS EX 1"), "OS-EX-1")
        self.assertEqual(stage_pilot.extract_universal_stage_code("JT8 2"), "JT8-2")
        self.assertEqual(stage_pilot.extract_universal_stage_code("AB-CD-12"), "AB-CD-12")
        self.assertEqual(stage_pilot.extract_universal_stage_code("0S-1"), "OS-1")
        self.assertEqual(stage_pilot.extract_universal_stage_code("0-1"), "0-1")

        # Node classification (including header_hint, EG, END8-1, XX-TR-X, XX-ST-X)
        self.assertEqual(stage_pilot.classify_stage_node("TR-27"), "tutorial")
        self.assertEqual(stage_pilot.classify_stage_node("OS-TR-1"), "tutorial")
        self.assertEqual(stage_pilot.classify_stage_node("XX-01", header_hint="TUTORIAL"), "tutorial")
        self.assertEqual(stage_pilot.classify_stage_node("ST-1"), "story")
        self.assertEqual(stage_pilot.classify_stage_node("OS-ST-2"), "story")
        self.assertEqual(stage_pilot.classify_stage_node("EG-1"), "story")
        self.assertEqual(stage_pilot.classify_stage_node("END8-1"), "story")
        self.assertEqual(stage_pilot.classify_stage_node("16-2"), "battle")
        self.assertEqual(stage_pilot.classify_stage_node("R8-1"), "battle")
        self.assertEqual(stage_pilot.classify_stage_node("DH-MO-1"), "battle")
        self.assertEqual(stage_pilot.classify_stage_node("AB-CD-12"), "battle")

    def test_chapter16_real_screenshot_detects_dark_tr27_and_auto_push_dynamic_loop(self):
        shot_16 = REPO_ROOT / "reports" / "push_16-2_victory_round_1.png"
        if not (shot_16.is_file() and (REPO_ROOT / "resource" / "template").is_dir() and sys.platform == "win32"):
            return

        with Image.open(shot_16) as im16:
            im16_rgb = im16.convert("RGB")
            dark_cards = stage_pilot.detect_dark_stage_cards(im16_rgb)
            self.assertEqual(len(dark_cards), 1)
            self.assertTrue(dark_cards[0]["is_dark_card"])
            self.assertFalse(dark_cards[0]["cleared"])
            self.assertTrue(560 <= dark_cards[0]["cx"] <= 710)

            nodes = stage_pilot.inspect_map_stage_nodes(im16_rgb, "")
            by_code = {n["code"]: n for n in nodes}
            self.assertIn("16-1", by_code)
            self.assertIn("16-2", by_code)
            self.assertIn("TR-27", by_code)
            self.assertTrue(by_code["16-1"]["cleared"])
            self.assertTrue(by_code["16-1"]["badge_full_3star"])
            self.assertTrue(by_code["16-2"]["cleared"])
            self.assertTrue(by_code["16-2"]["badge_full_3star"])
            self.assertFalse(by_code["TR-27"]["cleared"])
            self.assertEqual(by_code["TR-27"]["node_type"], "tutorial")
            self.assertTrue(by_code["TR-27"]["is_dark_card"])

            with patch.object(stage_pilot, "adb_screencap_image", return_value=im16_rgb):
                scan = stage_pilot.discover_next_uncleared_stage_on_map(
                    "adb.exe", "127.0.0.1:16384", REPO_ROOT, max_swipes=0
                )
            self.assertTrue(scan["found"])
            self.assertEqual(scan["next_stage"], "TR-27")
            self.assertEqual(scan["next_type"], "tutorial")

        # End-to-end MaaEngine.run_push(["auto"]) with EMPTY SideStory stages.json/StageActivityV2.json:
        # Step 1: real Ch.16 image (16-1(3★), 16-2(3★), TR-27(uncleared)) -> executes TR-27
        # Step 2: refreshed map shows TR-27(cleared) + 16-3(uncleared) -> executes 16-3
        # Step 3: refreshed map shows 16-3(cleared) -> swipes twice with identical frame -> all_cleared=True!
        map_after_tr27 = _make_synthetic_map([
            {"x": 120, "y": 480, "cleared": True},
            {"x": 400, "y": 400, "dark": True, "cleared": True},
            {"x": 680, "y": 440, "cleared": False},
        ])
        boxes_after_tr27 = [
            {"text": "16-2", "norm": "16-2", "x": 140, "y": 485, "w": 60, "h": 22, "pass": "norm"},
            {"text": "TR-27", "norm": "TR-27", "x": 420, "y": 405, "w": 72, "h": 22, "pass": "dark_card_roi"},
            {"text": "16-3", "norm": "16-3", "x": 700, "y": 445, "w": 60, "h": 22, "pass": "norm"},
        ]
        map_all_done = _make_synthetic_map([
            {"x": 120, "y": 480, "cleared": True},
            {"x": 400, "y": 400, "dark": True, "cleared": True},
            {"x": 680, "y": 440, "cleared": True},
        ])

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text("[]", encoding="utf-8")
            (base / "cache" / "gui").mkdir(parents=True)
            (base / "cache" / "gui" / "StageActivityV2.json").write_text("{}", encoding="utf-8")
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            state = {"tr27_done": False, "s16_3_done": False}
            executed: list[tuple[str, bool]] = []

            def _dyn_cap(*_a, **_kw):
                if not state["tr27_done"]:
                    return im16_rgb
                if not state["s16_3_done"]:
                    return map_after_tr27
                return map_all_done

            def _dyn_ocr(img_in):
                if img_in is im16_rgb:
                    return stage_pilot.__dict__["_orig_find_ocr"](img_in)
                return boxes_after_tr27

            def _fake_clear(st, **kwargs):
                executed.append((st, bool(kwargs.get("require_fresh_map_entry"))))
                if st == "TR-27":
                    state["tr27_done"] = True
                    return True, {"stage": st, "node_type": "tutorial", "victory": True}
                if st == "16-3":
                    state["s16_3_done"] = True
                    return True, {"stage": st, "victory": True, "map_clear_verified": True}
                return False, {"stage": st, "victory": False}

            orig_ocr = stage_pilot.find_stage_boxes_via_ocr
            stage_pilot.__dict__["_orig_find_ocr"] = orig_ocr
            try:
                with patch.object(env_doctor, "get_base_dir", return_value=base), \
                     patch.object(master_scheduler, "get_base_dir", return_value=base):
                    eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                    with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                         patch.object(eng, "refresh_cache"), \
                         patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                         patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                         patch.object(stage_pilot, "adb_screencap_image", side_effect=_dyn_cap), \
                         patch.object(stage_pilot, "adb_swipe_norm", return_value=True), \
                         patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
                         patch.object(stage_pilot, "find_stage_boxes_via_ocr", side_effect=_dyn_ocr), \
                         patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear), \
                         patch("time.sleep"):
                        res = eng.run_push(["auto"], refresh_cache_flag=False)
            finally:
                stage_pilot.__dict__.pop("_orig_find_ocr", None)

            self.assertTrue(res["ok"])
            self.assertTrue(res.get("dynamic_mode"))
            self.assertTrue(res.get("all_cleared"))
            self.assertEqual([s for s, _ in executed], ["TR-27", "16-3"])
            self.assertEqual(executed[0][1], False)
            self.assertEqual(executed[1][1], True)
            self.assertEqual(res["stages"], ["TR-27", "16-3"])

    def test_all_cleared_screen_swipes_left_to_discover_new_stage_and_stops_after_2_unchanged_swipes(self):
        # Page 1: R8-1(3★), JT8-2(3★) all cleared -> must swipe left (960->320) to reveal Page 2
        # Page 2: END8-1(story, uncleared) + AB-CD-12(battle, uncleared) -> picks leftmost END8-1!
        # Page 3 (after clearing both): all cleared + 2 identical frames -> stops cleanly with all_cleared=True
        page1 = _make_synthetic_map([
            {"x": 180, "y": 320, "cleared": True},
            {"x": 520, "y": 420, "cleared": True},
        ], bg=(210, 215, 222))
        page2 = _make_synthetic_map([
            {"x": 260, "y": 360, "cleared": False},
            {"x": 640, "y": 420, "cleared": False},
        ], bg=(135, 160, 195))
        page2_end = _make_synthetic_map([
            {"x": 260, "y": 360, "cleared": True},
            {"x": 640, "y": 420, "cleared": True},
        ], bg=(135, 160, 195))

        boxes_p1 = [
            {"text": "R8-1", "norm": "R8-1", "x": 200, "y": 325, "w": 65, "h": 22, "pass": "norm"},
            {"text": "JT8-2", "norm": "JT8-2", "x": 540, "y": 425, "w": 72, "h": 22, "pass": "norm"},
        ]
        boxes_p2 = [
            {"text": "END8-1", "norm": "END8-1", "x": 280, "y": 365, "w": 84, "h": 22, "pass": "norm"},
            {"text": "AB-CD-12", "norm": "AB-CD-12", "x": 660, "y": 425, "w": 96, "h": 22, "pass": "norm"},
        ]

        swipes: list[tuple[float, float, float, float]] = []

        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[page1, page2]), \
             patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda a, b, x1, y1, x2, y2, **kw: swipes.append((x1, y1, x2, y2)) or True), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", side_effect=[boxes_p1, boxes_p2]), \
             patch("time.sleep"):
            res1 = stage_pilot.discover_next_uncleared_stage_on_map("adb.exe", "127.0.0.1:16384", REPO_ROOT, max_swipes=5)

        self.assertTrue(res1["found"])
        self.assertEqual(res1["next_stage"], "END8-1")
        self.assertEqual(res1["next_type"], "story")
        self.assertEqual(res1["swipes_done"], 1)
        self.assertEqual(len(swipes), 1)
        self.assertGreater(swipes[0][0], swipes[0][2])  # swipes right-to-left to advance rightward

        # Now on page2_end, 2 consecutive identical frames after swiping -> stops at exactly 2 swipes
        swipes2: list[int] = []
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[page2_end, page2_end, page2_end]), \
             patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda *a, **kw: swipes2.append(1) or True), \
             patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=boxes_p2), \
             patch("time.sleep"):
            res2 = stage_pilot.discover_next_uncleared_stage_on_map(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, max_swipes=8, unchanged_swipe_limit=2
            )

        self.assertFalse(res2["found"])
        self.assertTrue(res2["all_cleared"])
        self.assertEqual(res2["swipes_done"], 2)
        self.assertEqual(len(swipes2), 2)

    def test_detail_panel_title_and_prts_lookup_support_all_stage_formats(self):
        # 1. Right detail panel title extraction across all formats
        for raw, expected in [
            ("OPERATION 16-2", "16-2"),
            ("OPERATION R8-1", "R8-1"),
            ("OPERATION S4-1", "S4-1"),
            ("OPERATION H12-1", "H12-1"),
            ("OPERATION JT8-2", "JT8-2"),
            ("TUTORIAL TR-27", "TR-27"),
            ("OPERATION OS-EX-1", "OS-EX-1"),
            ("OPERATION DH-MO-1", "DH-MO-1"),
            ("OPERATION AB-CD-12", "AB-CD-12"),
        ]:
            self.assertEqual(stage_pilot._extract_stage_code_from_title_text(raw, expected_stage=expected), expected)
            self.assertEqual(stage_pilot._extract_stage_code_from_title_text(raw, expected_stage=""), expected)

        # 2. PRTS online lookup strips '（标准）' and prefers standard stage_id over '#s' / '#f#' / 'easy_'
        fake_levels = [
            {"cat_three": "16-2（标准）", "stage_id": "main_16-01#s", "level_id": "obt/main/level_main_16-01#s", "name": "16-2s"},
            {"cat_three": "16-2（标准）", "stage_id": "main_16-01", "level_id": "obt/main/level_main_16-01", "name": "16-2"},
            {"cat_three": "H12-1（标准）", "stage_id": "hard_12-01", "level_id": "obt/hard/level_hard_12-01", "name": "H12-1"},
            {"cat_three": "R8-1", "stage_id": "main_08-01#f#", "level_id": "", "name": "R8-1突袭"},
            {"cat_three": "R8-1", "stage_id": "main_08-01", "level_id": "obt/main/level_main_08-01", "name": "R8-1"},
            {"cat_three": "AB-CD-12", "stage_id": "act99side_12", "level_id": "", "name": "AB-CD-12"},
        ]
        with patch.object(maa_runner, "load_prts_levels", return_value=fake_levels):
            r16 = maa_runner.lookup_prts_stage_online("16-2")
            self.assertEqual(r16["code"], "16-2")
            self.assertEqual(r16["stage_id"], "main_16-01")
            rh12 = maa_runner.lookup_prts_stage_online("H12-1")
            self.assertEqual(rh12["code"], "H12-1")
            self.assertEqual(rh12["stage_id"], "hard_12-01")
            rr8 = maa_runner.lookup_prts_stage_online("R8-1")
            self.assertEqual(rr8["stage_id"], "main_08-01")
            r3 = maa_runner.lookup_prts_stage_online("AB-CD-12")
            self.assertEqual(r3["stage_id"], "act99side_12")

        # 3. _guide_matches_stage works cleanly for 16-2, R8-1, XX-XX-XX and guards ranges
        self.assertTrue(maa_runner._guide_matches_stage({"stage_name": "", "doc": {"title": "16-2 低配挂机"}}, "16-2", "16-2", allow_doc_match=True))
        self.assertFalse(maa_runner._guide_matches_stage({"stage_name": "", "doc": {"title": "16-1至16-9全关卡摆完"}}, "16-2", "16-2", allow_doc_match=True))
        self.assertTrue(maa_runner._guide_matches_stage({"stage_name": "", "doc": {"title": "DH-MO-1 双人挂机"}}, "DH-MO-1", "DH-MO-1", allow_doc_match=True))

    @unittest.skipIf(root_scheduler is None, "Legacy root master_scheduler.py excluded from standalone repo")
    def test_root_scheduler_auto_push_uses_dynamic_loop(self):
        executed = []
        scans = [
            {"found": True, "on_stage_map": True, "all_cleared": False, "next_stage": "TR-27", "next_type": "tutorial"},
            {"found": True, "on_stage_map": True, "all_cleared": False, "next_stage": "16-3", "next_type": "battle"},
            {"found": False, "on_stage_map": True, "all_cleared": True, "next_stage": "", "next_type": ""},
        ]

        def _fake_clear(st, **kwargs):
            executed.append(st)
            return True, {"stage": st, "victory": True}

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(stages=["auto"], max_rounds=3, no_refresh_cache=True)
            with patch.object(root_scheduler.agent, "ensure_game_running"), \
                 patch.object(root_scheduler.agent, "init_maa_sdk", return_value=True), \
                 patch.object(root_scheduler.agent.stage_pilot, "discover_next_uncleared_stage_on_map", side_effect=scans), \
                 patch.object(root_scheduler.agent, "autonomous_clear_stage", side_effect=_fake_clear), \
                 patch.object(root_scheduler, "REPORTS_DIR", tmp):
                rc = root_scheduler.run_push_mode(args)
            self.assertEqual(rc, 0)
            self.assertEqual(executed, ["TR-27", "16-3"])


if __name__ == "__main__":
    unittest.main()
