"""Unit tests for all 6 permanent Roguelike themes post-settlement reward & tech-tree automation.

All tests are 100% mocked (no real ADB, no local config mutation).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import numpy as np

from universal_engine import maa_runner, roguelike_ai_copilot, stage_pilot
from universal_engine.gui import contract


def _make_lit_node_image(cx: int = 580, cy: int = 340) -> Image.Image:
    img = Image.new("RGB", (1280, 720), (16, 20, 28))
    draw = ImageDraw.Draw(img)
    draw.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=(252, 222, 95))
    return img


def _make_synthetic_sarkaz_red_black_lobby() -> Image.Image:
    """Synthesize a 1280x720 Sarkaz lobby with vivid red/black stripes and white '历史重构 35' strokes."""
    img = Image.new("RGB", (1280, 720), (18, 16, 20))
    draw = ImageDraw.Draw(img)
    # High-contrast alternating red & black stripes across left function band
    for sx in range(0, 420, 24):
        draw.rectangle((sx, 120, sx + 12, 620), fill=(236, 24, 28))
    # Bright white strokes mimicking "历史重构 35" right at Sarkaz anchor (0.105, 0.425) -> (134, 306)
    for ox in range(92, 176, 12):
        draw.rectangle((ox, 298, ox + 4, 314), fill=(250, 250, 250))
        draw.rectangle((ox, 304, ox + 9, 307), fill=(250, 250, 250))
    # White strokes mimicking "前瞻性投资系统 999" lower down at (0.11, 0.56) -> (141, 403)
    for ox in range(95, 190, 14):
        draw.rectangle((ox, 396, ox + 4, 410), fill=(245, 245, 245))
    return img


class MultiThemeRoguelikeEntryDictionaryTests(unittest.TestCase):
    def test_all_six_permanent_themes_verified_official_entries_and_aliases(self):
        expected = {
            "Phantom": ("傀影与猩红孤钻", "古堡笔记", "幕后筹备"),
            "Mizuki": ("水月与深蓝之树", "生态谱系", "认知塑造"),
            "Sami": ("探索者的银凇止境", "科考纪录", "文化比较"),
            "Sarkaz": ("萨卡兹的无终奇语", "假说辑录", "历史重构"),
            "JieGarden": ("岁的界园志异", "游园纪念", "古今学识"),
            "BlackFlow": ("沉沦者的黑流树海", "源流堆栈", "生命游戏"),
        }
        for key, (disp, lvl_name, tech_name) in expected.items():
            prof = roguelike_ai_copilot.get_theme_ui_profile(key)
            self.assertEqual(prof["theme_key"], key)
            self.assertEqual(prof["display_name"], disp)
            self.assertEqual(prof["level_reward"]["name"], lvl_name)
            self.assertIn(lvl_name, prof["level_reward"]["keywords"])
            self.assertEqual(prof["tech_tree"]["name"], tech_name)
            self.assertIn(tech_name, prof["tech_tree"]["keywords"])
            self.assertEqual(prof["monthly_commission"]["keywords"], ["本月委托栏", "本月委托"])

        # Aliases Sui -> JieGarden and Drowning -> BlackFlow + Chinese titles
        sui_prof = roguelike_ai_copilot.get_theme_ui_profile("Sui")
        self.assertEqual(sui_prof["theme_key"], "JieGarden")
        self.assertEqual(sui_prof["level_reward"]["name"], "游园纪念")
        self.assertEqual(sui_prof["tech_tree"]["name"], "古今学识")
        self.assertEqual(sui_prof["tech_tree"]["keywords"], ["古今学识", "古今", "学识"])

        drown_prof = roguelike_ai_copilot.get_theme_ui_profile("Drowning")
        self.assertEqual(drown_prof["theme_key"], "BlackFlow")
        self.assertEqual(drown_prof["level_reward"]["name"], "源流堆栈")
        self.assertEqual(drown_prof["tech_tree"]["name"], "生命游戏")

        # Frontend display formatting & dropdown coverage
        opt_values = [v for _, v in contract.RL_THEME_OPTIONS]
        for key in expected:
            self.assertIn(key, opt_values)
        self.assertEqual(contract._format_rl_theme_display("JieGarden"), "岁的界园志异 (JieGarden)")
        self.assertEqual(contract._format_rl_theme_display("Sui"), "岁的界园志异 (Sui)")
        self.assertEqual(contract._format_rl_theme_display("Drowning"), "沉沦者的黑流树海 (Drowning)")
        self.assertEqual(contract._format_rl_theme_display("岁的界园志异"), "岁的界园志异 (JieGarden)")

        # MAA runner alias normalization preserves canonical theme keys
        res_sui = maa_runner.resolve_roguelike_settings(config={"roguelike": {"use_gui_config": False, "theme": "Sui"}})
        self.assertEqual(res_sui["theme"], "JieGarden")
        res_drown = maa_runner.resolve_roguelike_settings(config={"roguelike": {"use_gui_config": False, "theme": "Drowning"}})
        self.assertEqual(res_drown["theme"], "BlackFlow")

    def test_cross_theme_lobby_fallback_for_all_six_themes(self):
        """Even when configured theme is BlackFlow, manually opening any of the 6 lobbies auto-matches correctly."""
        scenarios = [
            ("Phantom", "古堡笔记", "幕后筹备"),
            ("Mizuki", "生态谱系", "认知塑造"),
            ("Sami", "科考纪录", "文化比较"),
            ("Sarkaz", "假说辑录", "历史重构"),
            ("JieGarden", "游园纪念", "古今学识"),
            ("BlackFlow", "源流堆栈", "生命游戏"),
        ]
        for actual_theme, lvl_text, tech_text in scenarios:
            boxes = [
                {"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610},
                {"text": lvl_text, "compact": lvl_text, "cx": 1090, "cy": 64},
                {"text": tech_text, "compact": tech_text, "cx": 180, "cy": 380},
            ]
            lvl_hit = roguelike_ai_copilot.resolve_lobby_level_reward_entry(boxes, configured_theme="BlackFlow")
            tech_hit = roguelike_ai_copilot.resolve_lobby_tech_tree_entry(boxes, configured_theme="BlackFlow")
            self.assertIsNotNone(lvl_hit, f"Failed level reward fallback for {actual_theme}")
            self.assertIsNotNone(tech_hit, f"Failed tech tree fallback for {actual_theme}")
            self.assertEqual(lvl_hit["theme"], actual_theme)
            self.assertEqual(tech_hit["theme"], actual_theme)
            self.assertEqual(lvl_hit["matched_via_fallback"], actual_theme != "BlackFlow")
            self.assertEqual(tech_hit["matched_via_fallback"], actual_theme != "BlackFlow")
            self.assertEqual(
                roguelike_ai_copilot.detect_lobby_theme_from_boxes(boxes, preferred_theme="BlackFlow"),
                actual_theme,
            )


class VerticalSplitAndBlacklistGuardTests(unittest.TestCase):
    def test_jiegarden_vertical_split_xueshi_gujin_merges_center(self):
        # Case 1: "学识" + "古今" split columns with bounding boxes -> exact merged bbox center
        boxes_col = [
            {"text": "图册", "compact": "图册", "x": 40, "y": 180, "w": 80, "h": 40, "cx": 80, "cy": 200},
            {"text": "见字", "compact": "见字", "x": 40, "y": 240, "w": 80, "h": 40, "cx": 80, "cy": 260},
            {"text": "学识", "compact": "学识", "x": 120, "y": 320, "w": 40, "h": 100, "cx": 140, "cy": 370},
            {"text": "古今", "compact": "古今", "x": 180, "y": 320, "w": 40, "h": 100, "cx": 200, "cy": 370},
            {"text": "蚀刻章套组", "compact": "蚀刻章套组", "x": 100, "y": 650, "w": 160, "h": 40, "cx": 180, "cy": 670},
        ]
        hit = roguelike_ai_copilot.resolve_lobby_tech_tree_entry(boxes_col, configured_theme="Sui")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["theme"], "JieGarden")
        self.assertEqual(hit["box"]["text"], "古今学识")
        # Merged x: [120, 220] -> cx=170, y: [320, 420] -> cy=370
        self.assertEqual(hit["box"]["cx"], 170)
        self.assertEqual(hit["box"]["cy"], 370)
        self.assertEqual(hit["box"]["merged_from"], ["学识", "古今"])

    def test_jiegarden_vertical_split_xuegu_shijin_merges_center(self):
        # Case 2: Horizontal-line WinOCR reading two vertical columns as "学古" (top) + "识今" (bottom)
        boxes_row = [
            {"text": "学古", "compact": "学古", "cx": 190, "cy": 340},
            {"text": "识今", "compact": "识今", "cx": 190, "cy": 400},
            {"text": "游园纪念", "compact": "游园纪念", "cx": 1100, "cy": 64},
        ]
        hit = roguelike_ai_copilot.resolve_lobby_tech_tree_entry(boxes_row, configured_theme="BlackFlow")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["theme"], "JieGarden")
        self.assertTrue(hit["matched_via_fallback"])
        self.assertEqual(hit["box"]["cx"], 190)
        self.assertEqual(hit["box"]["cy"], 370)
        self.assertEqual(hit["box"]["merged_from"], ["学古", "识今"])

    def test_sarkaz_history_reconstruct_near_blacklist_never_misclicks(self):
        # Real Sarkaz lobby layout: 前瞻性投资系统 / 巫仪档案库 / 印象重建 surrounding 历史重构, plus bottom bar
        sarkaz_lobby = [
            {"text": "前瞻性投资系统", "compact": "前瞻性投资系统", "cx": 170, "cy": 210},
            {"text": "巫仪档案库", "compact": "巫仪档案库", "cx": 170, "cy": 290},
            {"text": "历史重构", "compact": "历史重构", "cx": 170, "cy": 370},
            {"text": "印象重建", "compact": "印象重建", "cx": 170, "cy": 450},
            {"text": "蚀刻章套组", "compact": "蚀刻章套组", "cx": 160, "cy": 665},
            {"text": "深入探索", "compact": "深入探索", "cx": 380, "cy": 665},
            {"text": "常规行动", "compact": "常规行动", "cx": 560, "cy": 665},
        ]
        hit = roguelike_ai_copilot.resolve_lobby_tech_tree_entry(sarkaz_lobby, configured_theme="Sarkaz")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["theme"], "Sarkaz")
        self.assertEqual(hit["box"]["text"], "历史重构")
        self.assertEqual((hit["box"]["cx"], hit["box"]["cy"]), (170, 370))

    def test_blacklist_only_or_out_of_left_roi_entries_are_rejected(self):
        # 1) Blacklisted labels only -> must return None
        blacklist_only = [
            {"text": "前瞻性投资系统", "compact": "前瞻性投资系统", "cx": 170, "cy": 210},
            {"text": "巫仪档案库", "compact": "巫仪档案库", "cx": 170, "cy": 290},
            {"text": "印象重建", "compact": "印象重建", "cx": 170, "cy": 370},
            {"text": "图册", "compact": "图册", "cx": 170, "cy": 430},
            {"text": "见字", "compact": "见字", "cx": 170, "cy": 490},
            {"text": "黑流数据库", "compact": "黑流数据库", "cx": 170, "cy": 550},
        ]
        self.assertIsNone(roguelike_ai_copilot.resolve_lobby_tech_tree_entry(blacklist_only, configured_theme="Sarkaz"))

        # 2) Valid tech-tree text placed on right half (x > 620) or bottom bar (y > 640) or top bar (y < 80) -> rejected!
        out_of_roi_boxes = [
            {"text": "历史重构", "compact": "历史重构", "cx": 780, "cy": 360},  # right half
            {"text": "认知塑造", "compact": "认知塑造", "cx": 220, "cy": 675},  # bottom bar
            {"text": "生命游戏", "compact": "生命游戏", "cx": 220, "cy": 42},   # top bar
        ]
        self.assertIsNone(roguelike_ai_copilot.resolve_lobby_tech_tree_entry(out_of_roi_boxes, configured_theme="Sarkaz"))

        # End-to-end auto_upgrade_roguelike_tech_tree does zero taps and skips cleanly
        # (ROI-aware mock: full-screen gets blacklist + out-of-ROI; left crop only contains blacklist boxes)
        dummy_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        taps = []
        logs = []

        def _fake_ocr(im, *args, **kwargs):
            if im.size == (1280, 720):
                return blacklist_only + out_of_roi_boxes
            return [
                {"text": "前瞻性投资系统", "compact": "前瞻性投资系统", "cx": 580, "cy": 446},
                {"text": "巫仪档案库", "compact": "巫仪档案库", "cx": 580, "cy": 86},
            ]

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=_fake_ocr), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", log_callback=logs.append
                )
        self.assertTrue(res["ok"])
        self.assertFalse(res["entry_found"])
        self.assertEqual(res["upgraded_count"], 0)
        self.assertEqual(taps, [])
        self.assertTrue(any("[RL-TECH]" in ln for ln in logs))


class MonthlyCommissionAndEndToEndSettlementTests(unittest.TestCase):
    def test_monthly_commission_matches_both_variants_and_rejects_monthly_squad(self):
        b1 = [{"text": "本月委托栏", "compact": "本月委托栏", "cx": 1140, "cy": 265}]
        b2 = [{"text": "本月委托", "compact": "本月委托", "cx": 1120, "cy": 290}]
        b_squad = [{"text": "月度小队", "compact": "月度小队", "cx": 1120, "cy": 290}]

        hit1 = roguelike_ai_copilot.resolve_lobby_monthly_commission_entry(b1, configured_theme="BlackFlow")
        hit2 = roguelike_ai_copilot.resolve_lobby_monthly_commission_entry(b2, configured_theme="BlackFlow")
        hit_sq = roguelike_ai_copilot.resolve_lobby_monthly_commission_entry(b_squad, configured_theme="Sarkaz")
        self.assertIsNotNone(hit1)
        self.assertEqual(hit1["text"], "本月委托栏")
        self.assertIsNotNone(hit2)
        self.assertEqual(hit2["text"], "本月委托")
        self.assertIsNone(hit_sq)

    def test_ended_theme_silently_skips_missing_commission_without_error(self):
        dummy_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        ocr_seq = [
            # 1. Lobby -> sees 假说辑录 (Sarkaz)
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}, {"text": "假说辑录", "compact": "假说辑录", "cx": 1090, "cy": 64}],
            # 2. Level reward page -> 一键领取
            [{"text": "一键领取", "compact": "一键领取", "cx": 1080, "cy": 640}],
            # 3. Level reward page -> already claimed
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 1080, "cy": 640}],
            # 4. Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
            # 5. Lobby again -> NO monthly commission entry (ended theme!), only 月度小队
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}, {"text": "假说辑录", "compact": "假说辑录", "cx": 1090, "cy": 64}, {"text": "月度小队", "compact": "月度小队", "cx": 1140, "cy": 265}],
        ]
        taps = []
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.claim_roguelike_rewards(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", log_callback=logs.append
                )
        self.assertTrue(res["ok"])
        self.assertTrue(res["level_entry_found"])
        self.assertEqual(res["level_rewards_claimed"], 1)
        self.assertFalse(res["commission_entry_found"])
        self.assertEqual(res["commission_rewards_claimed"], 0)
        self.assertEqual(res.get("commission_skipped_reason"), "ended_theme_no_commission")
        self.assertNotIn((1140, 265), taps)
        self.assertFalse(any("[WARN]" in ln or "ERROR" in ln for ln in logs))
        self.assertTrue(any("已完结常驻主题" in ln for ln in logs))

    def test_manual_switched_jiegarden_lobby_full_post_settlement_and_multitheme_buttons(self):
        """Config says BlackFlow, user manually sits in JieGarden lobby with split '学识'+'古今' & '解锁' button."""
        dummy_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        lit_tree = _make_lit_node_image(540, 320)
        empty_tree = Image.new("RGB", (1280, 720), (20, 24, 32))

        jiegarden_lobby = [
            {"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610},
            {"text": "游园纪念", "compact": "游园纪念", "cx": 1095, "cy": 64},
            {"text": "图册", "compact": "图册", "cx": 80, "cy": 210},
            {"text": "学识", "compact": "学识", "cx": 140, "cy": 380},
            {"text": "古今", "compact": "古今", "cx": 200, "cy": 380},
        ]
        screen_images = [
            dummy_img,  # nav step 0 (already in JieGarden lobby!)
            dummy_img, dummy_img, dummy_img, dummy_img,  # level reward + return
            dummy_img,  # commission scan (none -> silent skip)
            dummy_img, lit_tree, dummy_img, empty_tree, dummy_img,  # tech tree
        ]
        ocr_seq = [
            jiegarden_lobby,  # nav step 0
            jiegarden_lobby,  # claim level reward scan
            [{"text": "一键领取", "compact": "一键领取", "cx": 1080, "cy": 640}],
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 1080, "cy": 640}],
            jiegarden_lobby,  # return to lobby
            jiegarden_lobby,  # commission scan (no commission on ended theme)
            jiegarden_lobby,  # tech tree lobby scan -> merged (170, 380)
            # Tech tree node detail with top-left header "古今学识" + "解锁" action button
            [
                {"text": "古今学识", "compact": "古今学识", "cx": 150, "cy": 42},
                {"text": "开局希望+2", "compact": "开局希望+2", "cx": 960, "cy": 260},
                {"text": "解锁", "compact": "解锁", "cx": 1040, "cy": 580},
            ],
            jiegarden_lobby,  # return to lobby
        ]
        img_iter = iter(screen_images)
        ocr_iter = iter(ocr_seq)
        taps = []
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, dummy_img)), \
                 patch.object(stage_pilot, "detect_network_disconnect_popup", return_value={"detected": False}), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.run_roguelike_post_settlement(
                    adb_path="adb.exe",
                    adb_addr="127.0.0.1:16384",
                    maa_dir=Path(tmp),
                    config={"roguelike": {"theme": "BlackFlow", "auto_claim_rewards": True, "auto_upgrade_tech_tree": True}},
                    theme="BlackFlow",
                    navigate_to_lobby=True,
                    log_callback=logs.append,
                )

        self.assertTrue(res["ok"])
        self.assertTrue(res["navigation"]["ok"])
        self.assertEqual(res["navigation"]["detected_theme"], "JieGarden")
        self.assertEqual(res["detected_theme"], "JieGarden")
        self.assertEqual(res["rewards"]["level_rewards_claimed"], 1)
        self.assertEqual(res["tech_tree"]["tech_tree_name"], "古今学识")
        self.assertEqual(res["tech_tree"]["upgraded_count"], 1)
        self.assertEqual(res["tech_tree"]["upgraded_nodes"][0]["upgrade_label"], "解锁")
        # Merged center of (140,380) and (200,380) = (170,380); never tapped 图册 (80,210) or header (150,42)
        self.assertIn((170, 380), taps)
        self.assertIn((1040, 580), taps)
        self.assertNotIn((80, 210), taps)
        self.assertNotIn((150, 42), taps)

    def test_secondary_tech_tree_button_variants_and_header_exclusion(self):
        """Verify 重构 / 解锁 / 升级 / 激活 / 确认 / 塑造 work while page titles & unavailable nodes are skipped."""
        dummy_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        lit_tree = _make_lit_node_image(540, 320)
        empty_tree = Image.new("RGB", (1280, 720), (20, 24, 32))

        for btn_label, header_title, entry_name, theme in (
            ("重构", "历史重构", "历史重构", "Sarkaz"),
            ("确认重构", "历史重构", "历史重构", "Sarkaz"),
            ("塑造", "认知塑造", "认知塑造", "Mizuki"),
            ("升级", "文化比较", "文化比较", "Sami"),
            ("确认", "幕后筹备", "幕后筹备", "Phantom"),
            ("激活", "生命游戏", "生命游戏", "BlackFlow"),
        ):
            taps = []
            screen_images = [dummy_img, lit_tree, dummy_img, empty_tree, dummy_img]
            ocr_seq = [
                [{"text": entry_name, "compact": entry_name, "cx": 180, "cy": 380}],
                [
                    {"text": header_title, "compact": header_title, "cx": 160, "cy": 40},
                    {"text": "开局希望+2", "compact": "开局希望+2", "cx": 960, "cy": 260},
                    {"text": btn_label, "compact": btn_label, "cx": 1030, "cy": 585},
                ],
                [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
            ]
            img_iter = iter(screen_images)
            ocr_iter = iter(ocr_seq)
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, dummy_img)), \
                     patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                     patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                     patch("time.sleep"):
                    res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                        "adb.exe", "127.0.0.1:16384", Path(tmp), theme=theme
                    )
            self.assertTrue(res["ok"], f"Failed for button {btn_label}")
            self.assertEqual(res["upgraded_count"], 1, f"Did not upgrade for button {btn_label}")
            self.assertIn((1030, 585), taps, f"Did not tap button {btn_label}")
            self.assertNotIn((160, 40), taps, f"Mis-clicked header {header_title} for button {btn_label}")


class SarkazThreeTierTechTreeRecoveryTests(unittest.TestCase):
    def test_synthetic_red_black_stripes_suppressed_while_white_text_preserved(self):
        canvas = _make_synthetic_sarkaz_red_black_lobby()
        prep = roguelike_ai_copilot.build_tech_tree_local_ocr_images(canvas)
        red_sup = prep["red_suppressed"]
        clahe_im = prep["clahe_gray"]

        # Expected crop: x in [13, 358] (w=345), y in [180, 540] (h=360), upscaled 2.5x -> ~862 x 900
        self.assertGreaterEqual(red_sup.width, 840)
        self.assertGreaterEqual(red_sup.height, 880)
        self.assertEqual(red_sup.size, clahe_im.size)

        arr_red = np.asarray(red_sup.convert("L"), dtype=np.uint8)
        # Pure red stripe background region (e.g. full-screen x=26..34, y=210..240 -> inside crop upper-left, no text)
        # Must collapse to pure white background (255) after min(G,B) + adaptive thresholding!
        bg_patch = arr_red[70:150, 30:55]
        self.assertGreater(float(np.mean(bg_patch == 255)), 0.98)

        # White text stroke region around full-screen (92..176, 298..314) -> crop (79..163, 118..134) -> 2.5x (198..408, 295..335)
        # Must survive as high-contrast dark foreground (0) for WinOCR!
        txt_patch = arr_red[295:335, 198:408]
        self.assertGreater(int(np.sum(txt_patch == 0)), 400)

        # CLAHE stream must produce non-degenerate enhanced contrast
        arr_clahe = np.asarray(clahe_im.convert("L"), dtype=np.uint8)
        self.assertGreater(float(np.std(arr_clahe)), 15.0)

    def test_stage2_red_suppressed_local_ocr_recovers_sarkaz_and_ignores_investment_999(self):
        sarkaz_img = _make_synthetic_sarkaz_red_black_lobby()
        lit_tree = _make_lit_node_image(540, 320)
        empty_tree = Image.new("RGB", (1280, 720), (16, 20, 28))

        ocr_calls = []

        def _fake_ocr(im, *args, **kwargs):
            idx = len(ocr_calls)
            ocr_calls.append(im.size)
            if idx == 0:
                # Tier 1 full-screen OCR: sees 假说辑录 + 开始探索, misses 历史重构 due to red stripes
                return [
                    {"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55},
                    {"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610},
                ]
            if idx == 1:
                # Tier 2 Stream A (red_suppressed 2.5x crop): detects 历史重构 35 AND lower 前瞻性投资系统 999
                # Local (450, 252) -> full-screen (134, 306); Local (475, 446) -> full-screen (141, 403)
                return [
                    {"text": "巫仪档案库", "compact": "巫仪档案库", "cx": 475, "cy": 86},
                    {"text": "历史重构 35", "compact": "历史重构35", "cx": 450, "cy": 252},
                    {"text": "前瞻性投资系统", "compact": "前瞻性投资系统", "cx": 475, "cy": 446},
                    {"text": "999", "compact": "999", "cx": 610, "cy": 446},
                ]
            if idx == 2:
                # Node detail inside 历史重构
                return [
                    {"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 40},
                    {"text": "开局希望+2", "compact": "开局希望+2", "cx": 960, "cy": 260},
                    {"text": "重构", "compact": "重构", "cx": 1020, "cy": 580},
                ]
            return [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}]

        screen_images = [sarkaz_img, lit_tree, empty_tree, empty_tree, sarkaz_img]
        img_iter = iter(screen_images)
        taps = []
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, sarkaz_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=_fake_ocr), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", log_callback=logs.append
                )

        self.assertTrue(res["ok"])
        self.assertTrue(res["entry_found"])
        self.assertEqual(res["entry_stage"], "local_ocr_red_suppressed")
        self.assertEqual(res["upgraded_count"], 1)
        # Verified mapped full-screen tap on 历史重构 35 (~134, 306), NEVER 前瞻性投资系统 (~141, 403)
        self.assertTrue(any(abs(x - 134) <= 3 and abs(y - 306) <= 3 for x, y in taps), f"taps={taps}")
        self.assertFalse(any(abs(y - 403) <= 15 and x < 300 for x, y in taps), f"Mis-clicked investment row: {taps}")
        # Second OCR call received the 2.5x upscaled cropped ROI (width > 800, not 1280x720)
        self.assertNotEqual(ocr_calls[1], (1280, 720))
        self.assertTrue(any("去红通道放大OCR" in ln for ln in logs))

    def test_stage2_pure_numeric_points_35_matches_and_excludes_999(self):
        # 1) Direct unit check on _find_tech_tree_numeric_badge_box
        mixed_boxes = [
            {"text": "巫仪档案库", "compact": "巫仪档案库", "cx": 141, "cy": 223},
            {"text": "12", "compact": "12", "cx": 210, "cy": 223},       # y=0.31 (< 0.35) & on archive row -> reject
            {"text": "35", "compact": "35", "cx": 156, "cy": 306},       # y=0.425 in [0.35, 0.50] -> MATCH!
            {"text": "前瞻性投资系统", "compact": "前瞻性投资系统", "cx": 141, "cy": 403},
            {"text": "999", "compact": "999", "cx": 210, "cy": 403},     # y=0.56 (> 0.50) & on investment row -> reject
        ]
        hit = roguelike_ai_copilot._find_tech_tree_numeric_badge_box(mixed_boxes, configured_theme="Sarkaz")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["text"], "35")
        self.assertEqual((hit["cx"], hit["cy"]), (156, 306))

        # Only 999 at investment bank row (or blacklisted row inside y band) -> must return None!
        only_bad = [
            {"text": "前瞻性投资系统", "compact": "前瞻性投资系统", "cx": 141, "cy": 403},
            {"text": "999", "compact": "999", "cx": 210, "cy": 403},
            {"text": "印象重建", "compact": "印象重建", "cx": 141, "cy": 310},
            {"text": "88", "compact": "88", "cx": 210, "cy": 310},  # same row as blacklisted 印象重建 -> reject
        ]
        self.assertIsNone(roguelike_ai_copilot._find_tech_tree_numeric_badge_box(only_bad, configured_theme="Sarkaz"))

        # 2) End-to-end Tier 2 pure numeric '35' recovery + secondary panel verification
        sarkaz_img = _make_synthetic_sarkaz_red_black_lobby()
        lit_tree = _make_lit_node_image(540, 320)
        empty_tree = Image.new("RGB", (1280, 720), (16, 20, 28))

        ocr_seq = [
            # Tier 1 full-screen OCR: misses 历史重构
            [{"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55}, {"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
            # Tier 2 Stream A (red_suppressed): only reads '35' (y_norm=0.425) and '999' (y_norm=0.56)
            [{"text": "35", "compact": "35", "cx": 520, "cy": 252}, {"text": "999", "compact": "999", "cx": 520, "cy": 446}],
            # Tier 2 Stream B (clahe_gray): no extra text
            [],
            # Fast secondary panel verification after clicking '35'
            [{"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 40}],
            # Node detail OCR
            [{"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 40}, {"text": "开局希望+2", "compact": "开局希望+2", "cx": 960, "cy": 260}, {"text": "重构", "compact": "重构", "cx": 1020, "cy": 580}],
            # Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        screen_images = [sarkaz_img, lit_tree, empty_tree, empty_tree, sarkaz_img]
        img_iter = iter(screen_images)
        ocr_iter = iter(ocr_seq)
        taps = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, sarkaz_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )

        self.assertTrue(res["ok"])
        self.assertTrue(res["entry_found"])
        self.assertEqual(res["entry_stage"], "local_ocr_points")
        self.assertEqual(res["upgraded_count"], 1)
        # Local (520, 252) -> full-screen (~153, 306), NEVER y~403
        self.assertTrue(any(abs(x - 153) <= 4 and abs(y - 306) <= 3 for x, y in taps), f"taps={taps}")
        self.assertFalse(any(abs(y - 403) <= 15 and x < 300 for x, y in taps), f"Mis-clicked 999: {taps}")

    def test_stage3_confirmed_sarkaz_anchor_fallback_and_wrong_panel_abort(self):
        sarkaz_img = _make_synthetic_sarkaz_red_black_lobby()
        lit_tree = _make_lit_node_image(540, 320)
        empty_tree = Image.new("RGB", (1280, 720), (16, 20, 28))

        # Case A: [RL-REWARD] confirms Sarkaz via 假说辑录 -> [RL-TECH] Tier 1 & 2 OCR miss completely ->
        # Tier 3 taps Sarkaz standard 16:9 anchor (0.105, 0.425) = (134, 306), verifies panel, upgrades node!
        screen_images_ok = [
            sarkaz_img, sarkaz_img, sarkaz_img, sarkaz_img,  # level reward + return
            sarkaz_img,                                      # commission scan (none -> silent skip)
            sarkaz_img,                                      # tech tree lobby scan
            lit_tree,                                        # Tier 3 verify_img (reused as round 0 tree_img!)
            empty_tree,                                      # node detail img
            empty_tree,                                      # round 1 tree_img (no more lit nodes)
            sarkaz_img,                                      # return to lobby
        ]
        ocr_seq_ok = [
            # RL-REWARD 1: Sarkaz lobby hits 127 假说辑录
            [{"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55}, {"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
            [{"text": "一键领取", "compact": "一键领取", "cx": 1080, "cy": 640}],
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 1080, "cy": 640}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
            # RL-REWARD 2: commission scan -> absent on ended Sarkaz
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}, {"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55}],
            # RL-TECH Tier 1 full-screen -> misses 历史重构
            [{"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55}, {"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
            # RL-TECH Tier 2 Stream A & B -> extreme noise, empty
            [],
            [],
            # RL-TECH Tier 3 post-click secondary panel verification -> confirms 历史重构 panel!
            [{"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 40}],
            # Node detail OCR -> 重构
            [{"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 40}, {"text": "开局希望+2", "compact": "开局希望+2", "cx": 960, "cy": 260}, {"text": "重构", "compact": "重构", "cx": 1020, "cy": 580}],
            # Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_iter = iter(screen_images_ok)
        ocr_iter = iter(ocr_seq_ok)
        taps = []
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, sarkaz_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.run_roguelike_post_settlement(
                    adb_path="adb.exe",
                    adb_addr="127.0.0.1:16384",
                    maa_dir=Path(tmp),
                    config={"roguelike": {"theme": "Sarkaz", "auto_claim_rewards": True, "auto_upgrade_tech_tree": True}},
                    theme="Sarkaz",
                    navigate_to_lobby=False,
                    log_callback=logs.append,
                )

        self.assertTrue(res["ok"])
        self.assertEqual(res["rewards"]["level_rewards_claimed"], 1)
        self.assertTrue(res["tech_tree"]["entry_found"])
        self.assertEqual(res["tech_tree"]["entry_stage"], "theme_anchor_fallback")
        self.assertEqual(res["tech_tree"]["upgraded_count"], 1)
        # Exact Sarkaz normalized anchor (0.105, 0.425) -> (134, 306)
        self.assertIn((134, 306), taps)
        self.assertTrue(any("主题锚点兜底" in ln for ln in logs))

        # Case B: Mis-entered 前瞻性投资系统 or 巫仪档案库 on Tier 3 anchor -> immediately tap '<' (64, 40) & abort!
        for wrong_title in ("前瞻性投资系统", "巫仪档案库"):
            taps_bad = []
            logs_bad = []
            ocr_bad = [
                [{"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55}],
                [],
                [],
                [{"text": wrong_title, "compact": wrong_title, "cx": 220, "cy": 60}],
            ]
            ocr_bad_iter = iter(ocr_bad)
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(stage_pilot, "adb_screencap_image", return_value=sarkaz_img), \
                     patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_bad_iter, [])), \
                     patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_bad.append((x, y)) or True), \
                     patch("time.sleep"):
                    res_bad = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                        "adb.exe",
                        "127.0.0.1:16384",
                        Path(tmp),
                        theme="Sarkaz",
                        lobby_confirmed_theme="Sarkaz",
                        log_callback=logs_bad.append,
                    )
            self.assertTrue(res_bad["ok"])
            self.assertFalse(res_bad["entry_found"])
            self.assertEqual(res_bad["upgraded_count"], 0)
            self.assertEqual(res_bad.get("wrong_panel_detected"), wrong_title)
            self.assertEqual(taps_bad, [(134, 306), (64, 40)])
            self.assertTrue(any("[WARN][RL-TECH]" in ln and wrong_title in ln for ln in logs_bad))


def _make_bottom_confirm_img(mode: str) -> Image.Image:
    """Create 1280x720 tech-tree detail screenshot with cyan-blue '确认升级' or grey '未解锁' bottom-right button."""
    img = Image.new("RGB", (1280, 720), (22, 26, 34))
    draw = ImageDraw.Draw(img)
    # Bottom detail bar backdrop
    draw.rectangle((0, 595, 1280, 720), fill=(28, 32, 42))
    if mode == "cyan_confirm":
        # Vivid cyan-blue clickable button inside x in [0.74, 0.98], y in [0.85, 0.98] -> (955..1245, 616..700)
        draw.rectangle((955, 616, 1245, 700), fill=(24, 178, 232))
    elif mode == "grey_locked":
        # Muted grey disabled button inside bottom-right ROI
        draw.rectangle((955, 616, 1245, 700), fill=(104, 106, 110))
    elif mode == "overview_overlay":
        # Full-screen dark attribute overview modal (no cyan button at bottom-right)
        draw.rectangle((40, 40, 1240, 690), fill=(30, 34, 44))
        for ry in range(160, 640, 48):
            draw.rectangle((120, ry, 1100, ry + 20), fill=(44, 48, 58))
    return img


class TechTreeSecondaryScreenStateMachineTests(unittest.TestCase):
    def test_forbidden_zones_filter_overview_63_78_and_prioritize_glowing_halo_nodes(self):
        # Verify forbidden zone boundaries directly
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(128, 618))   # 63/78 已重构 bottom-left
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(160, 45))    # top nav bar y < 0.14
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(1160, 60))   # top-right points
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(640, 640))   # bottom bar y > 0.82
        self.assertFalse(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(620, 340))  # valid canvas center

        canvas = Image.new("RGB", (1280, 720), (18, 20, 26))
        draw = ImageDraw.Draw(canvas)
        # Forbidden bright blobs: bottom-left '63/78 已重构' (128, 618), top bar (160, 45), top-right points (1160, 60)
        draw.rectangle((70, 585, 220, 650), fill=(245, 245, 245))
        draw.rectangle((100, 20, 240, 70), fill=(245, 245, 245))
        draw.rectangle((1100, 30, 1240, 95), fill=(245, 245, 245))
        # Unlocked solid bright white node on left (340, 320)
        draw.ellipse((318, 298, 362, 342), fill=(248, 248, 248))
        # Frontier upgradeable grey stone tablet with bright white glowing outer halo at (620, 340)
        draw.ellipse((590, 310, 650, 370), fill=(250, 250, 250))  # bright outer halo
        draw.ellipse((602, 322, 638, 358), fill=(95, 96, 102))    # grey stone tablet core
        # Dark locked tablet on right (900, 340) without halo
        draw.ellipse((876, 316, 924, 364), fill=(72, 74, 78))

        nodes = roguelike_ai_copilot.detect_upgradeable_tech_nodes(canvas)
        self.assertEqual(len(nodes), 1)
        # None of the forbidden zones, solid pure-white upgraded node (340, 320), or dark locked tablet (900, 340) may appear
        for n in nodes:
            self.assertFalse(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(n["cx"], n["cy"]))
            self.assertGreater(abs(n["cx"] - 128) + abs(n["cy"] - 618), 100)
            self.assertGreater(abs(n["cx"] - 340) + abs(n["cy"] - 320), 60)
            self.assertGreater(abs(n["cx"] - 900) + abs(n["cy"] - 340), 60)
        # Glowing-halo frontier grey tablet (620, 340) must be detected cleanly!
        self.assertTrue(nodes[0]["has_halo"])
        self.assertLess(abs(nodes[0]["cx"] - 620), 25)
        self.assertLess(abs(nodes[0]["cy"] - 340), 25)

    def test_overview_overlay_auto_exits_and_never_misclicks_broken_header_as_button(self):
        lobby_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        tree_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        overview_img = _make_bottom_confirm_img("overview_overlay")
        cyan_detail_img = _make_bottom_confirm_img("cyan_confirm")

        # Mock detect_upgradeable_tech_nodes returning:
        # 1) forbidden (128, 618) -> must be filtered out and NEVER tapped!
        # 2) bad candidate (420, 320) -> triggers full-screen overview overlay with broken title '历 重 构' + '63/78'
        # 3) real frontier node (640, 340) -> opens cyan-blue '确认升级 x3' even while corner still shows '63/78 已重构'
        lit_seq = [
            [
                {"cx": 128, "cy": 618, "lit_pixels": 400, "has_halo": False},
                {"cx": 420, "cy": 320, "lit_pixels": 320, "has_halo": True},
                {"cx": 640, "cy": 340, "lit_pixels": 320, "has_halo": True},
            ],
            [],
        ]
        screen_images = [
            lobby_img,        # lobby entry scan
            tree_img,         # round 0 tree canvas
            overview_img,     # after tapping (420, 320) -> overview overlay
            cyan_detail_img,  # after tapping (640, 340) -> cyan confirm bar
            tree_img,         # round 1 tree canvas (empty lit_seq -> done)
            lobby_img,        # return to lobby
        ]
        ocr_seq = [
            # Lobby entry
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            # Overview overlay (reproducing exact real-device OCR: '历 重 构' + '63/78' + stat list!)
            [
                {"text": "历 重 构", "compact": "历重构", "cx": 160, "cy": 42},
                {"text": "63/78 已重构", "compact": "63/78已重构", "cx": 140, "cy": 100},
                {"text": "我方单位攻击力 +15%", "compact": "我方单位攻击力+15%", "cx": 480, "cy": 220},
                {"text": "我方单位生命 +15%", "compact": "我方单位生命+15%", "cx": 480, "cy": 280},
                {"text": "初始希望 +2", "compact": "初始希望+2", "cx": 480, "cy": 340},
            ],
            # Valid frontier node detail bar with cyan '确认升级 x3' at bottom-right AND '63/78 已重构' still in bottom-left
            [
                {"text": "63/78 已重构", "compact": "63/78已重构", "cx": 128, "cy": 618},
                {"text": "开局希望+2 初始费用+4", "compact": "开局希望+2初始费用+4", "cx": 560, "cy": 650},
                {"text": "确认升级 x3", "compact": "确认升级x3", "cx": 1100, "cy": 660},
            ],
            # Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_iter = iter(screen_images)
        ocr_iter = iter(ocr_seq)
        taps = []
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, lobby_img)), \
                 patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", side_effect=lit_seq), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", log_callback=logs.append
                )

        self.assertTrue(res["ok"])
        self.assertEqual(res["upgraded_count"], 1)
        self.assertEqual(res["upgraded_nodes"][0]["upgrade_label"], "确认升级 x3")
        # Forbidden (128, 618) and broken header (160, 42) were NEVER tapped!
        self.assertNotIn((128, 618), taps)
        self.assertNotIn((160, 42), taps)
        # Auto-exited overview overlay via '<' (64, 40), then clicked cyan-blue confirm button (1100, 660)
        self.assertIn((64, 40), taps)
        self.assertIn((1100, 660), taps)
        self.assertTrue(any("误入属性总览弹窗" in ln for ln in logs))

    def test_cyan_confirm_upgrade_clicks_and_grey_locked_skips_without_infinite_loop(self):
        cyan_img = _make_bottom_confirm_img("cyan_confirm")
        grey_img = _make_bottom_confirm_img("grey_locked")

        # 1) Direct color & truth verification checks
        c_ok = roguelike_ai_copilot.evaluate_tech_tree_confirm_button(
            cyan_img,
            [{"text": "确认升级 x3", "compact": "确认升级x3", "cx": 1100, "cy": 660}],
        )
        self.assertTrue(c_ok["valid"])
        self.assertEqual(c_ok["color_state"], "active")
        self.assertEqual(c_ok["button"]["text"], "确认升级 x3")

        c_grey_text = roguelike_ai_copilot.evaluate_tech_tree_confirm_button(
            grey_img,
            [{"text": "未解锁", "compact": "未解锁", "cx": 1100, "cy": 660}],
        )
        self.assertFalse(c_grey_text["valid"])
        self.assertEqual(c_grey_text["color_state"], "disabled_grey")

        # Even if OCR misread grey disabled button text as '解锁', grey color check STILL rejects it!
        c_grey_misread = roguelike_ai_copilot.evaluate_tech_tree_confirm_button(
            grey_img,
            [{"text": "解锁", "compact": "解锁", "cx": 1100, "cy": 660}],
        )
        self.assertFalse(c_grey_misread["valid"])
        self.assertEqual(c_grey_misread["color_state"], "disabled_grey")

        # 2) End-to-end: Node 1 is grey '未解锁' (skipped), Node 2 is cyan '确认升级 x3' (clicked & counted=1)
        lobby_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        tree_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        lit_seq_mix = [
            [
                {"cx": 520, "cy": 320, "lit_pixels": 300, "has_halo": True},
                {"cx": 700, "cy": 320, "lit_pixels": 300, "has_halo": True},
            ],
            [],
        ]
        imgs_mix = [lobby_img, tree_img, grey_img, cyan_img, tree_img, lobby_img]
        ocr_mix = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            [{"text": "前置节点效果", "compact": "前置节点效果", "cx": 520, "cy": 650}, {"text": "未解锁", "compact": "未解锁", "cx": 1100, "cy": 660}],
            [{"text": "干员攻击力+8%", "compact": "干员攻击力+8%", "cx": 520, "cy": 650}, {"text": "确认升级 x3", "compact": "确认升级x3", "cx": 1100, "cy": 660}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_it = iter(imgs_mix)
        ocr_it = iter(ocr_mix)
        taps_mix = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_it, lobby_img)), \
                 patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", side_effect=lit_seq_mix), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_it, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_mix.append((x, y)) or True), \
                 patch("time.sleep"):
                res_mix = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )
        self.assertTrue(res_mix["ok"])
        self.assertEqual(res_mix["upgraded_count"], 1)
        self.assertEqual(res_mix["upgraded_nodes"][0]["cx"], 700)
        # Confirm button (1100, 660) tapped ONLY once (for Node 2, NOT Node 1)
        self.assertEqual(taps_mix.count((1100, 660)), 1)

        # 3) End-to-end locked-only screen: skips without infinite loop and exits cleanly after 1 probe swipe
        taps_locked = []
        swipes_locked = []
        imgs_locked = [lobby_img, tree_img, grey_img, tree_img, lobby_img]
        ocr_locked = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            [{"text": "前置节点", "compact": "前置节点", "cx": 520, "cy": 650}, {"text": "未解锁", "compact": "未解锁", "cx": 1100, "cy": 660}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_lit = iter(imgs_locked)
        ocr_lit = iter(ocr_locked)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_lit, lobby_img)), \
                 patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", return_value=[{"cx": 520, "cy": 320, "lit_pixels": 300, "has_halo": True}]), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_lit, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_locked.append((x, y)) or True), \
                 patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda *a, **kw: swipes_locked.append(a) or True), \
                 patch("time.sleep"):
                res_locked = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", max_upgrades=6
                )
        self.assertTrue(res_locked["ok"])
        self.assertEqual(res_locked["upgraded_count"], 0)
        # Node (520, 320) tapped exactly once, confirm button never tapped, at most 1 swipe probe
        self.assertEqual(taps_locked.count((520, 320)), 1)
        self.assertNotIn((1100, 660), taps_locked)
        self.assertEqual(len(swipes_locked), 1)


def _make_realistic_tech_tree_screen(
    with_home_icon: bool = True,
    with_return_icon: bool = True,
    bottom_btn_mode: str = "cyan_confirm",
) -> Image.Image:
    """Create 1280x720 tech-tree screen with rendered top bar (< + house icon + points pill) & bottom bar."""
    img = Image.new("RGB", (1280, 720), (20, 24, 32))
    draw = ImageDraw.Draw(img)
    # Top navigation bar backdrop
    draw.rectangle((0, 0, 1280, 82), fill=(16, 18, 24))
    if with_return_icon:
        # Crisp white '<' return arrow in top-left (x < 0.14, y < 0.12)
        draw.polygon([(52, 42), (76, 24), (82, 30), (64, 42), (82, 54), (76, 60)], fill=(242, 242, 242))
    if with_home_icon:
        # Crisp white house icon inside x in [0.15, 0.28], y < 0.12 -> (234..280, 22..58)
        draw.polygon([(257, 20), (232, 40), (282, 40)], fill=(244, 244, 244))
        draw.rectangle((240, 40, 274, 60), fill=(244, 244, 244))
    # Top-right points indicator badge (x > 0.78, y < 0.16)
    draw.rectangle((1080, 22, 1220, 58), fill=(36, 40, 50))
    # Bottom detail bar backdrop
    draw.rectangle((0, 595, 1280, 720), fill=(26, 30, 40))
    if bottom_btn_mode == "cyan_confirm":
        # Vivid cyan-blue '确认升级 xN' button inside x in [0.72, 0.99], y in [0.84, 0.99]
        draw.rectangle((955, 622, 1250, 704), fill=(24, 178, 232))
    elif bottom_btn_mode == "grey_locked":
        draw.rectangle((955, 622, 1250, 704), fill=(104, 106, 110))
    return img


class TechTreePageGuardAndPointsVerificationTests(unittest.TestCase):
    def test_home_icon_present_with_63_78_badge_never_triggers_overview_return(self):
        tech_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")
        no_home_img = _make_realistic_tech_tree_screen(with_home_icon=False, with_return_icon=True, bottom_btn_mode="none")

        self.assertTrue(roguelike_ai_copilot.detect_tech_tree_home_icon(tech_img))
        self.assertFalse(roguelike_ai_copilot.detect_tech_tree_home_icon(no_home_img))

        # Main tech tree canvas has bottom-left '63/78 已重构' AND node stats, WITH top house icon -> NEVER overview!
        canvas_boxes = [
            {"text": "63/78 已重构", "compact": "63/78已重构", "cx": 128, "cy": 618},
            {"text": "我方单位攻击力 +5%", "compact": "我方单位攻击力+5%", "cx": 520, "cy": 650},
            {"text": "我方单位防御力 +5%", "compact": "我方单位防御力+5%", "cx": 520, "cy": 680},
        ]
        self.assertFalse(
            roguelike_ai_copilot._is_tech_tree_overview_overlay(
                canvas_boxes, has_bottom_confirm=False, detail_img=tech_img
            )
        )

        # Even without house icon, '63/78 已重构' + single stat line must NOT trigger overview!
        single_stat_boxes = [
            {"text": "63/78 已重构", "compact": "63/78已重构", "cx": 128, "cy": 618},
            {"text": "我方单位攻击力 +5%", "compact": "我方单位攻击力+5%", "cx": 520, "cy": 650},
        ]
        self.assertFalse(
            roguelike_ai_copilot._is_tech_tree_overview_overlay(
                single_stat_boxes, has_bottom_confirm=False, detail_img=no_home_img
            )
        )

        # Only when top house icon is ABSENT AND >=2 distinct attribute summary groups appear -> True!
        full_overview_boxes = [
            {"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 42},
            {"text": "我方单位攻击力 +15%", "compact": "我方单位攻击力+15%", "cx": 420, "cy": 220},
            {"text": "我方单位生命值 +15%", "compact": "我方单位生命值+15%", "cx": 420, "cy": 280},
            {"text": "初始源石锭 +10", "compact": "初始源石锭+10", "cx": 420, "cy": 340},
        ]
        self.assertTrue(
            roguelike_ai_copilot._is_tech_tree_overview_overlay(
                full_overview_boxes, has_bottom_confirm=False, detail_img=no_home_img
            )
        )

    def test_page_guard_intercepts_archive_and_fictional_exhibition_without_node_tap_or_swipe(self):
        lobby_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        archive_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        exhibition_img = _make_realistic_tech_tree_screen(with_home_icon=False, with_return_icon=True, bottom_btn_mode="none")
        # Draw bright white blobs on archive/exhibition so node detector WOULD find candidates if Page Guard didn't intercept!
        ImageDraw.Draw(archive_img).ellipse((480, 290, 560, 370), fill=(250, 250, 250))
        ImageDraw.Draw(exhibition_img).ellipse((580, 290, 660, 370), fill=(250, 250, 250))

        screen_images = [
            lobby_img,       # lobby entry
            archive_img,     # round 0: accidentally in 巫仪档案库
            exhibition_img,  # round 1: accidentally in 虚构展览 / 设为活动主界面背景
            archive_img,     # round 2: returned to Sarkaz lobby (开始探索)
            lobby_img,       # finally _tap_return_to_lobby
        ]
        ocr_seq = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            [{"text": "巫仪档案库", "compact": "巫仪档案库", "cx": 180, "cy": 50}, {"text": "想象实体图鉴", "compact": "想象实体图鉴", "cx": 420, "cy": 320}],
            [{"text": "虚构展览", "compact": "虚构展览", "cx": 180, "cy": 50}, {"text": "设为活动主界面背景", "compact": "设为活动主界面背景", "cx": 960, "cy": 640}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}, {"text": "假说辑录", "compact": "假说辑录", "cx": 1099, "cy": 55}],
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_iter = iter(screen_images)
        ocr_iter = iter(ocr_seq)
        taps = []
        swipes = []
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, lobby_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda *a, **kw: swipes.append(a) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", log_callback=logs.append
                )

        self.assertTrue(res["ok"])
        self.assertEqual(res["upgraded_count"], 0)
        self.assertEqual(res.get("page_guard_interceptions"), 2)
        # Only taps allowed: lobby entry (134, 306) + two '<' recovery taps (64, 40) -> ZERO node clicks, ZERO swipes!
        self.assertEqual(taps, [(134, 306), (64, 40), (64, 40)])
        self.assertEqual(swipes, [])
        self.assertTrue(any("Page Guard 拦截" in ln for ln in logs))

    def test_default_selected_confirm_upgrade_clicks_ocr_bbox_center_and_verifies_points_35_to_32(self):
        lobby_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        tree_before_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="cyan_confirm")
        tree_after_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")

        # Case A: Default-selected '确认升级 x3' with OCR bbox (1040, 648, 140, 40) -> exact center (1110, 668)
        # Points drop 35 -> 32 on attempt 1 -> counted as +1; next round sees points=0 -> clean exit!
        screen_images_a = [
            lobby_img,        # lobby entry
            tree_before_img,  # round 0 tech tree screen (default-selected cyan 确认升级 x3)
            tree_after_img,   # post-click verification frame (points 35 -> 32)
            lobby_img,        # return to lobby
        ]
        ocr_seq_a = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            # Round 0 tech tree: top-right points=35, bottom-left 63/78 已重构, bottom-right 确认升级 x3 bbox center (1110, 668)
            [
                {"text": "35", "compact": "35", "cx": 1150, "cy": 40},
                {"text": "63/78 已重构", "compact": "63/78已重构", "cx": 128, "cy": 618},
                {"text": "开局希望+2", "compact": "开局希望+2", "cx": 520, "cy": 650},
                {"text": "确认升级 x3", "compact": "确认升级x3", "x": 1040, "y": 648, "w": 140, "h": 40, "cx": 1110, "cy": 668},
            ],
            # Post-click frame: points dropped 35 -> 32, bottom-right now grey 未解锁 (and points -> 0 on next round)
            [
                {"text": "32", "compact": "32", "cx": 1150, "cy": 40},
                {"text": "64/78 已重构", "compact": "64/78已重构", "cx": 128, "cy": 618},
                {"text": "未解锁", "compact": "未解锁", "cx": 1110, "cy": 668},
            ],
            # Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_it_a = iter(screen_images_a)
        ocr_it_a = iter(ocr_seq_a)
        taps_a = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_it_a, lobby_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_it_a, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_a.append((x, y)) or True), \
                 patch("time.sleep"):
                res_a = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )

        self.assertTrue(res_a["ok"])
        self.assertEqual(res_a["upgraded_count"], 1)
        self.assertEqual(res_a["upgraded_nodes"][0]["points_before"], 35)
        self.assertEqual(res_a["upgraded_nodes"][0]["points_after"], 32)
        self.assertEqual(res_a["upgraded_nodes"][0]["cx"], 1110)
        self.assertEqual(res_a["upgraded_nodes"][0]["cy"], 668)
        # Clicked lobby entry (134, 306) then DIRECTLY clicked OCR bbox center (1110, 668) without touching any node or '<'!
        self.assertEqual(taps_a, [(134, 306), (1110, 668)])

        # Case B: Points do NOT decrease on attempt 1 (35 -> 35) -> auto-retries once -> STILL 35 -> aborts with upgraded_count=0!
        screen_images_b = [
            lobby_img,        # lobby entry
            tree_before_img,  # round 0 tech tree screen
            tree_before_img,  # after attempt 1 (still identical, points=35)
            tree_before_img,  # after attempt 2 retry (still identical, points=35)
            lobby_img,        # return to lobby
        ]
        same_35_boxes = [
            {"text": "35", "compact": "35", "cx": 1150, "cy": 40},
            {"text": "63/78 已重构", "compact": "63/78已重构", "cx": 128, "cy": 618},
            {"text": "确认升级 x3", "compact": "确认升级x3", "x": 1040, "y": 648, "w": 140, "h": 40, "cx": 1110, "cy": 668},
        ]
        ocr_seq_b = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            same_35_boxes,  # round 0
            same_35_boxes,  # attempt 1 after -> still 35!
            same_35_boxes,  # attempt 2 retry after -> still 35!
            [{"text": "开始探索", "compact": "开始探索", "cx": 1060, "cy": 610}],
        ]
        img_it_b = iter(screen_images_b)
        ocr_it_b = iter(ocr_seq_b)
        taps_b = []

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_it_b, lobby_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_it_b, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_b.append((x, y)) or True), \
                 patch("time.sleep"):
                res_b = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )

        self.assertTrue(res_b["ok"])
        self.assertEqual(res_b["upgraded_count"], 0)
        # Exactly 1 entry tap + 2 taps on (1110, 668) (first + 1 focus retry), then exited cleanly!
        self.assertEqual(taps_b, [(134, 306), (1110, 668), (1110, 668)])


def _draw_glowing_frontier_tablet(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    half_w: int = 26,
    half_h: int = 32,
    with_left_white_line: bool = True,
) -> None:
    """Draw a Sarkaz-style frontier grey stone tablet with closed bright white glowing outer outline."""
    if with_left_white_line:
        # Bright glowing white connecting wire entering from the left
        draw.line([(cx - half_w - 48, cy), (cx - half_w - 1, cy)], fill=(238, 240, 244), width=4)
    # Closed bright white outer glowing border
    draw.rounded_rectangle(
        (cx - half_w - 5, cy - half_h - 5, cx + half_w + 5, cy + half_h + 5),
        radius=8,
        fill=(246, 246, 248),
    )
    # Medium-grey stone tablet interior with subtle carved stone motif
    draw.rounded_rectangle(
        (cx - half_w, cy - half_h, cx + half_w, cy + half_h),
        radius=5,
        fill=(94, 96, 102),
    )
    draw.rectangle((cx - 8, cy - 10, cx + 8, cy + 10), outline=(136, 138, 144), width=2)


def _draw_dark_locked_tablet(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    half_w: int = 26,
    half_h: int = 32,
) -> None:
    """Draw a dark locked stone tablet connected only by a black dark wire on the left."""
    draw.line([(cx - half_w - 48, cy), (cx - half_w - 1, cy)], fill=(10, 11, 14), width=4)
    draw.rounded_rectangle(
        (cx - half_w, cy - half_h, cx + half_w, cy + half_h),
        radius=5,
        fill=(68, 70, 74),
        outline=(84, 86, 92),
        width=2,
    )


def _draw_warm_white_background_flare(img: Image.Image, cx: int, cy: int, radius: int = 105) -> None:
    """Composite a realistic smooth warm-white atmospheric light flare onto a canvas."""
    arr = np.asarray(img, dtype=np.float32).copy()
    yy, xx = np.ogrid[: arr.shape[0], : arr.shape[1]]
    dist2 = (xx - float(cx)) ** 2 + (yy - float(cy)) ** 2
    sigma2 = float(radius * radius) * 0.42
    weight = np.exp(-dist2 / max(1.0, 2.0 * sigma2)).astype(np.float32)
    flare = np.stack([weight * 238.0, weight * 224.0, weight * 196.0], axis=-1)
    blended = np.clip(arr * (1.0 - weight[:, :, None] * 0.85) + flare, 0.0, 255.0).astype(np.uint8)
    img.paste(Image.fromarray(blended, mode="RGB"))


class RealDeviceRoguelikeRegressionTests(unittest.TestCase):
    """Regression unit tests covering all 4 real-device state-machine & visual-algorithm fixes."""

    def test_exit_level_reward_stops_at_lobby_and_theme_select_blocks_anchor_and_fake_panel(self):
        # 1) Exiting 假说辑录: single '<' tap -> sleep(1.2) -> recognizes Sarkaz lobby ('开始探险'/'蚀刻章套组'/'常规行动')
        #    and IMMEDIATELY stops tapping '<' (never double-taps back to Theme Selection page!).
        dummy_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        sarkaz_lobby_boxes = [
            {"text": "127 假说辑录", "compact": "127假说辑录", "cx": 1099, "cy": 55},
            {"text": "开始探险", "compact": "开始探险", "cx": 1060, "cy": 610},
            {"text": "蚀刻章套组", "compact": "蚀刻章套组", "cx": 160, "cy": 665},
            {"text": "常规行动", "compact": "常规行动", "cx": 560, "cy": 665},
        ]
        ocr_seq_reward = [
            sarkaz_lobby_boxes,                                               # Lobby -> locate 假说辑录
            [{"text": "一键领取", "compact": "一键领取", "cx": 1080, "cy": 640}],  # Level reward claim
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 1080, "cy": 640}],  # Claim done
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 1080, "cy": 640}],  # Return check 0 (still on reward page -> tap '<' ONCE)
            sarkaz_lobby_boxes,                                               # Return check 1 (after 1.2s -> Sarkaz lobby -> STOP '<'!)
            sarkaz_lobby_boxes,                                               # Commission scan (ended theme -> silent skip)
        ]
        taps_rew = []
        sleeps_rew = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq_reward), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_rew.append((x, y)) or True), \
                 patch("time.sleep", side_effect=lambda s: sleeps_rew.append(s)):
                rew_res = roguelike_ai_copilot.claim_roguelike_rewards(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )
        self.assertTrue(rew_res["ok"])
        self.assertEqual(rew_res["level_rewards_claimed"], 1)
        # Strictly ONE '<' tap (64, 40) and waited 1.2s before lobby verification!
        self.assertEqual(taps_rew.count((64, 40)), 1, f"Expected single '<' tap, got taps={taps_rew}")
        self.assertIn(1.2, sleeps_rew)

        # 2) Theme Selection page hard detection across all 4 signature markers
        for marker in ("进入主题", "添加到终端", "查看收藏", "该主题已全额收录"):
            self.assertTrue(
                roguelike_ai_copilot.is_roguelike_theme_select_page(
                    [{"text": marker, "compact": marker, "cx": 640, "cy": 540}]
                ),
                f"Marker {marker} must trigger theme-selection page detection",
            )
            self.assertFalse(
                roguelike_ai_copilot.is_roguelike_theme_lobby(
                    [{"text": marker, "compact": marker, "cx": 640, "cy": 540}, {"text": "假说辑录", "compact": "假说辑录", "cx": 1099, "cy": 55}],
                    preferred_theme="Sarkaz",
                )
            )

        # 3) On Theme Selection page WITHOUT lobby recovery: MUST block Stage-3 anchor fallback (134, 306) & node scanning!
        sarkaz_img = _make_synthetic_sarkaz_red_black_lobby()
        theme_select_unrecoverable = [
            {"text": "添加到终端", "compact": "添加到终端", "cx": 1020, "cy": 610},
            {"text": "查看收藏", "compact": "查看收藏", "cx": 860, "cy": 610},
            {"text": "该主题已全额收录", "compact": "该主题已全额收录", "cx": 640, "cy": 520},
        ]
        self.assertIsNone(
            roguelike_ai_copilot._resolve_theme_anchor_fallback(
                sarkaz_img, confirmed_theme="Sarkaz", boxes=theme_select_unrecoverable
            )
        )
        taps_ts = []
        swipes_ts = []
        logs_ts = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", return_value=sarkaz_img), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=theme_select_unrecoverable), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_ts.append((x, y)) or True), \
                 patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda *a, **kw: swipes_ts.append(a) or True), \
                 patch("time.sleep"):
                res_ts = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe",
                    "127.0.0.1:16384",
                    Path(tmp),
                    theme="Sarkaz",
                    lobby_confirmed_theme="Sarkaz",
                    log_callback=logs_ts.append,
                )
        self.assertFalse(res_ts["entry_found"])
        self.assertEqual(res_ts.get("wrong_panel_detected"), "theme_select_page")
        self.assertEqual(res_ts["upgraded_count"], 0)
        self.assertEqual(taps_ts, [])
        self.assertEqual(swipes_ts, [])
        self.assertTrue(any("主题选择页" in ln for ln in logs_ts))

        # 4) On Theme Selection page WITH target card + '进入主题': taps '进入主题' first to return to lobby (NEVER taps 添加到终端 or 134,306 on theme select!)
        theme_select_with_enter = [
            {"text": "萨卡兹的无终奇语", "compact": "萨卡兹的无终奇语", "cx": 640, "cy": 260},
            {"text": "进入主题", "compact": "进入主题", "cx": 1060, "cy": 620},
            {"text": "添加到终端", "compact": "添加到终端", "cx": 860, "cy": 620},
            {"text": "查看收藏", "compact": "查看收藏", "cx": 700, "cy": 620},
        ]
        tree_empty_realistic = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        ocr_recover_seq = [
            theme_select_with_enter,  # Initial screen: Theme Selection page -> tap '进入主题' (1060, 620)
            [{"text": "开始探险", "compact": "开始探险", "cx": 1060, "cy": 610}, {"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],  # Recovered Sarkaz lobby!
            [{"text": "32", "compact": "32", "cx": 1150, "cy": 48}, {"text": "64/78 已重构", "compact": "64/78已重构", "cx": 128, "cy": 618}],  # Tech tree canvas (0 glowing nodes -> clean exit)
            sarkaz_lobby_boxes,       # Return to lobby
        ]
        imgs_recover = [sarkaz_img, sarkaz_img, tree_empty_realistic, sarkaz_img]
        img_it_rec = iter(imgs_recover)
        ocr_it_rec = iter(ocr_recover_seq)
        taps_rec = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_it_rec, sarkaz_img)), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_it_rec, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_rec.append((x, y)) or True), \
                 patch("time.sleep"):
                res_rec = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", lobby_confirmed_theme="Sarkaz"
                )
        self.assertTrue(res_rec["entry_found"])
        # First tap MUST be '进入主题' (1060, 620), second tap '历史重构' (134, 306), NEVER '添加到终端' (860, 620)!
        self.assertEqual(taps_rec[:2], [(1060, 620), (134, 306)])
        self.assertNotIn((860, 620), taps_rec)

        # 5) Strict tech-tree panel admission check: 11 bright blobs ALONE must NEVER pass!
        fake_many_blobs_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        draw_fake = ImageDraw.Draw(fake_many_blobs_img)
        for bx in (260, 420, 580, 740, 900):
            _draw_glowing_frontier_tablet(draw_fake, bx, 260, with_left_white_line=False)
            _draw_glowing_frontier_tablet(draw_fake, bx, 430, with_left_white_line=False)
        _draw_glowing_frontier_tablet(draw_fake, 1060, 340, with_left_white_line=False)
        self.assertGreaterEqual(len(roguelike_ai_copilot.detect_upgradeable_tech_nodes(fake_many_blobs_img)), 10)

        sarkaz_prof = roguelike_ai_copilot.get_theme_ui_profile("Sarkaz")
        # 5a) Even with 11 glowing blobs + '<' + house icon, MISSING top-right points indicator -> REJECTED!
        with patch.object(stage_pilot, "adb_screencap_image", return_value=fake_many_blobs_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[]), \
             patch.object(stage_pilot, "adb_tap_norm", return_value=True), \
             patch("time.sleep"):
            v_no_pts = roguelike_ai_copilot._verify_entered_tech_tree_panel(
                "adb.exe", "127.0.0.1:16384", sarkaz_prof
            )
        self.assertFalse(v_no_pts["ok"])
        self.assertEqual(v_no_pts["wrong_panel"], "unconfirmed_panel")

        # 5b) 11 glowing blobs + theme selection keywords -> REJECTED without tapping '<'!
        taps_v_ts = []
        with patch.object(stage_pilot, "adb_screencap_image", return_value=fake_many_blobs_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=theme_select_with_enter), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_v_ts.append((x, y)) or True), \
             patch("time.sleep"):
            v_ts = roguelike_ai_copilot._verify_entered_tech_tree_panel(
                "adb.exe", "127.0.0.1:16384", sarkaz_prof
            )
        self.assertFalse(v_ts["ok"])
        self.assertEqual(v_ts["wrong_panel"], "theme_select_page")
        self.assertEqual(taps_v_ts, [])

        # 5c) Missing house icon even with points -> REJECTED!
        no_home_with_pts = _make_realistic_tech_tree_screen(with_home_icon=False, with_return_icon=True, bottom_btn_mode="none")
        pts_box = [{"text": "32", "compact": "32", "cx": 1150, "cy": 48}]
        guard_no_home = roguelike_ai_copilot.classify_tech_tree_page_guard(
            no_home_with_pts, pts_box, maa_dir=None, active_profile=sarkaz_prof
        )
        self.assertEqual(guard_no_home["status"], "unknown_not_tech_tree")

        # 5d) '<' + house icon + top-right points in x in [0.78,0.99], y in [0.02,0.18] -> ACCEPTED!
        guard_ok = roguelike_ai_copilot.classify_tech_tree_page_guard(
            fake_many_blobs_img, pts_box, maa_dir=None, active_profile=sarkaz_prof
        )
        self.assertEqual(guard_ok["status"], "tech_tree")
        self.assertEqual(guard_ok["points"], 32)

    def test_edge_page_arrows_and_64_78_arch_completely_filtered_and_arch_edge_nudges_right(self):
        # 1) Direct forbidden-zone assertions for left/right page arrows and expanded bottom-left 64/78 arch
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(51, 310))    # '←' arrow x=0.04, y=0.43
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(100, 360))   # x=0.078 < 0.08
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(1229, 310))  # '→' arrow x=0.96, y=0.43
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(1195, 360))  # x=0.934 > 0.93
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(154, 468))   # 64/78 arch top x=0.12, y=0.65
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(220, 460))   # arch rim x=0.172<0.18, y=0.639>0.62
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(640, 108))   # y=0.15 < 0.16
        self.assertTrue(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(640, 584))   # y=0.811 > 0.80
        self.assertFalse(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(520, 360))  # safe center
        self.assertTrue(roguelike_ai_copilot.is_point_near_tech_tree_arch_edge(250, 470))      # arch-adjacent buffer

        # 2) Visual scan on canvas with bright '←' arrow, '→' arrow, bright 64/78 arch, and 1 safe frontier tablet
        canvas = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        draw = ImageDraw.Draw(canvas)
        # Left page-turn arrow '←' at (51, 310) (x=0.04, y=0.43) — drawn as bright glowing halo-like ring to stress-test!
        _draw_glowing_frontier_tablet(draw, 51, 310, half_w=18, half_h=24, with_left_white_line=False)
        # Right page-turn arrow '→' at (1229, 310) (x=0.96, y=0.43)
        _draw_glowing_frontier_tablet(draw, 1229, 310, half_w=18, half_h=24, with_left_white_line=False)
        # Bottom-left '64/78 已重构' arch spanning x in [30..225], y in [455..650] (x < 0.18, y > 0.62)
        _draw_glowing_frontier_tablet(draw, 140, 510, half_w=42, half_h=50, with_left_white_line=False)
        # Single valid frontier tablet inside safe zone at (520, 360)
        _draw_glowing_frontier_tablet(draw, 520, 360, with_left_white_line=True)

        nodes = roguelike_ai_copilot.detect_upgradeable_tech_nodes(canvas)
        self.assertEqual(len(nodes), 1, f"Expected only safe frontier node, got {nodes}")
        self.assertLess(abs(nodes[0]["cx"] - 520), 20)
        self.assertLess(abs(nodes[0]["cy"] - 360), 20)
        self.assertFalse(roguelike_ai_copilot.is_point_in_tech_tree_forbidden_zone(nodes[0]["cx"], nodes[0]["cy"]))

        # 3) Arch-edge candidate nudges canvas gently to the right before tapping, then upgrades safely in center!
        lobby_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        tree_before_nudge = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        tree_after_nudge = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        detail_cyan = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="cyan_confirm")
        tree_after_upgrade = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")

        lit_seq = [
            [{"cx": 250, "cy": 470, "lit_pixels": 320, "has_halo": True}],  # Round 0: hugs arch edge -> gentle drag right!
            [{"cx": 550, "cy": 470, "lit_pixels": 320, "has_halo": True}],  # Round 1: shifted safely to screen center!
            [],
        ]
        imgs_nudge = [lobby_img, tree_before_nudge, tree_after_nudge, detail_cyan, tree_after_upgrade, lobby_img]
        pts32 = [{"text": "32", "compact": "32", "cx": 1150, "cy": 48}, {"text": "64/78 已重构", "compact": "64/78已重构", "cx": 128, "cy": 618}]
        ocr_nudge = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            pts32,  # Round 0 guard
            pts32,  # Round 1 guard after gentle right-drag
            pts32 + [{"text": "确认升级 x3", "compact": "确认升级x3", "x": 1040, "y": 648, "w": 140, "h": 40, "cx": 1110, "cy": 668}],
            [{"text": "29", "compact": "29", "cx": 1150, "cy": 48}, {"text": "未解锁", "compact": "未解锁", "cx": 1110, "cy": 668}],
            [{"text": "开始探险", "compact": "开始探险", "cx": 1060, "cy": 610}],
        ]
        img_it = iter(imgs_nudge)
        ocr_it = iter(ocr_nudge)
        taps = []
        swipes = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_it, lobby_img)), \
                 patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", side_effect=lit_seq), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_it, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda *a, **kw: swipes.append(a) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )
        self.assertTrue(res["ok"])
        self.assertEqual(res["upgraded_count"], 1)
        # Arch-edge coordinate (250, 470) was NEVER poked; canvas dragged right (x1 < x2) first, then (550, 470) upgraded!
        self.assertNotIn((250, 470), taps)
        self.assertIn((550, 470), taps)
        self.assertEqual(len(swipes), 1)
        self.assertLess(swipes[0][2], swipes[0][4], f"Expected rightward drag (x1 < x2), got {swipes[0]}")

    def test_visual_detector_prioritizes_white_halo_grey_tablets_over_upgraded_locked_and_warm_glow(self):
        canvas = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        # 1) Large smooth warm-white background glow flare in upper-middle canvas (must NOT be detected!)
        _draw_warm_white_background_flare(canvas, cx=560, cy=215, radius=110)

        draw = ImageDraw.Draw(canvas)
        # 2) Already-upgraded solid bright pure-white nodes on the left (must be ELIMINATED!)
        draw.rounded_rectangle((210, 232, 266, 288), radius=8, fill=(248, 248, 248))
        draw.rounded_rectangle((210, 402, 266, 458), radius=8, fill=(246, 246, 248))
        # Even an upgraded white node with a tiny dark glyph in center still has >55% pure-white central core
        draw.rectangle((234, 426, 242, 434), fill=(90, 90, 95))

        # 3) Three genuine upgradeable frontier grey stone tablets with closed white glowing halo & left glowing lines
        #    (drawn deliberately out-of-order to verify strict X-ascending sorting!)
        _draw_glowing_frontier_tablet(draw, 650, 360, with_left_white_line=True)
        _draw_glowing_frontier_tablet(draw, 430, 440, with_left_white_line=True)
        _draw_glowing_frontier_tablet(draw, 430, 280, with_left_white_line=True)

        # 4) Dark locked stone tablets on the right with black lines and NO glowing halo (must be ELIMINATED!)
        _draw_dark_locked_tablet(draw, 890, 280)
        _draw_dark_locked_tablet(draw, 890, 440)

        nodes = roguelike_ai_copilot.detect_upgradeable_tech_nodes(canvas)
        self.assertEqual(len(nodes), 3, f"Expected exactly 3 white-halo frontier tablets, got {nodes}")

        # Verify every hit is a glowing-halo frontier tablet with low central white ratio & left glowing wire
        for n in nodes:
            self.assertTrue(n["has_halo"])
            self.assertEqual(n["node_kind"], "glowing_frontier")
            self.assertTrue(n.get("has_left_glow_line"))
            self.assertLess(n.get("core_white_ratio", 1.0), 0.35)
            # Must NOT hit solid white upgraded nodes, dark locked tablets, or warm-white glow center
            self.assertGreater(abs(n["cx"] - 238) + abs(n["cy"] - 260), 70)
            self.assertGreater(abs(n["cx"] - 238) + abs(n["cy"] - 430), 70)
            self.assertGreater(abs(n["cx"] - 890) + abs(n["cy"] - 280), 70)
            self.assertGreater(abs(n["cx"] - 890) + abs(n["cy"] - 440), 70)
            self.assertGreater(abs(n["cx"] - 560) + abs(n["cy"] - 215), 70)

        # Verify strict left-to-right (X ascending, then Y ascending) ordering!
        expected_order = [(430, 280), (430, 440), (650, 360)]
        for (ex, ey), got in zip(expected_order, nodes):
            self.assertLess(abs(got["cx"] - ex), 18, f"Expected ({ex},{ey}), got {got}")
            self.assertLess(abs(got["cy"] - ey), 18, f"Expected ({ex},{ey}), got {got}")
        self.assertLessEqual(nodes[0]["cx"], nodes[1]["cx"])
        self.assertLessEqual(nodes[1]["cx"], nodes[2]["cx"])

    def test_two_consecutive_already_upgraded_nodes_early_stop_swipe_left_and_chain_upgrade(self):
        lobby_img = Image.new("RGB", (1280, 720), (20, 24, 32))
        # Page 1 (all-upgraded left region) & Page 2 (frontier page — visually distinct so swipe-movement check passes!)
        page1_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        page1_upgraded_detail = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")

        page2_img = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="none")
        ImageDraw.Draw(page2_img).rectangle((220, 170, 1080, 520), fill=(48, 56, 72))  # Visually shift viewport
        page2_cyan_1 = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="cyan_confirm")
        page2_after_1 = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")
        page2_cyan_2 = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="cyan_confirm")
        page2_after_2 = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")

        # Page 1 has 6 candidates; after 2 consecutive '已升级' hits, MUST immediately abort Page 1 and swipe left!
        page1_candidates = [
            {"cx": 220, "cy": 260, "lit_pixels": 300, "has_halo": True},
            {"cx": 220, "cy": 420, "lit_pixels": 300, "has_halo": True},
            {"cx": 360, "cy": 260, "lit_pixels": 300, "has_halo": True},
            {"cx": 360, "cy": 420, "lit_pixels": 300, "has_halo": True},
            {"cx": 500, "cy": 260, "lit_pixels": 300, "has_halo": True},
            {"cx": 500, "cy": 420, "lit_pixels": 300, "has_halo": True},
        ]
        lit_seq = [
            page1_candidates,                                           # Round 0 (Page 1): 6 nodes, stops after #2!
            [{"cx": 540, "cy": 340, "lit_pixels": 300, "has_halo": True}],  # Round 1 (Page 2): frontier node 1
            [{"cx": 700, "cy": 340, "lit_pixels": 300, "has_halo": True}],  # Round 2 (Page 2 in-place rescan): newly lit downstream node 2!
            [],                                                         # Round 3: done!
        ]
        screen_images = [
            lobby_img,               # Lobby entry scan
            page1_img,               # Round 0: Page 1 canvas
            page1_upgraded_detail,   # Tap node #1 (220, 260) -> 已升级 (1/2)
            page1_upgraded_detail,   # Tap node #2 (220, 420) -> 已升级 (2/2 -> EARLY STOP + SWIPE LEFT!)
            page2_img,               # Round 1: Page 2 canvas (viewport moved!)
            page2_cyan_1,            # Tap frontier node (540, 340) -> cyan 确认升级 x3
            page2_after_1,           # After confirm #1 -> points 32 -> 29 (prefetched into Round 2!)
            page2_cyan_2,            # Tap newly lit downstream node (700, 340) -> cyan 确认升级 x3
            page2_after_2,           # After confirm #2 -> points 29 -> 26 (prefetched into Round 3!)
            lobby_img,               # Return to lobby
        ]
        pts32 = [{"text": "32", "compact": "32", "cx": 1150, "cy": 48}, {"text": "64/78 已重构", "compact": "64/78已重构", "cx": 128, "cy": 618}]
        pts29 = [{"text": "29", "compact": "29", "cx": 1150, "cy": 48}, {"text": "65/78 已重构", "compact": "65/78已重构", "cx": 128, "cy": 618}]
        pts26 = [{"text": "26", "compact": "26", "cx": 1150, "cy": 48}, {"text": "66/78 已重构", "compact": "66/78已重构", "cx": 128, "cy": 618}]
        ocr_seq = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            pts32,                                                                           # Round 0 Page Guard
            pts32 + [{"text": "已升级", "compact": "已升级", "cx": 1110, "cy": 668}],        # Node #1 -> 已升级
            pts32 + [{"text": "已升级", "compact": "已升级", "cx": 1110, "cy": 668}],        # Node #2 -> 已升级 -> EARLY STOP!
            pts32,                                                                           # Round 1 Page Guard (Page 2)
            pts32 + [{"text": "开局希望+2", "compact": "开局希望+2", "cx": 520, "cy": 650}, {"text": "确认升级 x3", "compact": "确认升级x3", "x": 1040, "y": 648, "w": 140, "h": 40, "cx": 1110, "cy": 668}],
            pts29 + [{"text": "已升级", "compact": "已升级", "cx": 1110, "cy": 668}],        # Post-upgrade #1: 32 -> 29!
            pts29 + [{"text": "干员攻击力+8%", "compact": "干员攻击力+8%", "cx": 520, "cy": 650}, {"text": "确认升级 x3", "compact": "确认升级x3", "x": 1040, "y": 648, "w": 140, "h": 40, "cx": 1110, "cy": 668}],
            pts26 + [{"text": "已升级", "compact": "已升级", "cx": 1110, "cy": 668}],        # Post-upgrade #2: 29 -> 26!
            [{"text": "开始探险", "compact": "开始探险", "cx": 1060, "cy": 610}],            # Return to lobby
        ]

        img_it = iter(screen_images)
        ocr_it = iter(ocr_seq)
        taps = []
        swipes = []
        logs = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_it, lobby_img)), \
                 patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", side_effect=lit_seq), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_it, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda *a, **kw: swipes.append(a) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz", log_callback=logs.append
                )

        self.assertTrue(res["ok"])
        self.assertEqual(res.get("early_stop_already_upgraded_swipes"), 1)
        self.assertEqual(res["upgraded_count"], 2)
        self.assertEqual(res["upgraded_nodes"][0]["points_before"], 32)
        self.assertEqual(res["upgraded_nodes"][0]["points_after"], 29)
        self.assertEqual(res["upgraded_nodes"][1]["points_before"], 29)
        self.assertEqual(res["upgraded_nodes"][1]["points_after"], 26)
        # Page 1 nodes #3..#6 were NEVER tapped (early stop after 2 consecutive '已升级'!)
        for skipped_xy in ((360, 260), (360, 420), (500, 260), (500, 420)):
            self.assertNotIn(skipped_xy, taps, f"Early stop failed: tapped Page 1 node {skipped_xy}")
        # Exactly 1 leftward swipe (x1=960 -> x2=320) to explore frontier
        self.assertEqual(len(swipes), 1)
        self.assertGreater(swipes[0][2], swipes[0][4], f"Expected leftward swipe (x1 > x2), got {swipes[0]}")
        self.assertTrue(any("连续命中 2 个「已升级」节点" in ln for ln in logs))

        # 2) Also verify '未解锁' skips and prioritizes untried candidates strictly to the LEFT of the locked node
        locked_detail = _make_realistic_tech_tree_screen(with_home_icon=True, with_return_icon=True, bottom_btn_mode="grey_locked")
        lit_locked_order = [
            [
                {"cx": 720, "cy": 340, "lit_pixels": 300, "has_halo": True},
                {"cx": 880, "cy": 340, "lit_pixels": 300, "has_halo": False},
                {"cx": 460, "cy": 340, "lit_pixels": 300, "has_halo": False},
            ],
            [],
        ]
        imgs_lo = [lobby_img, page2_img, locked_detail, page2_cyan_1, page2_after_1, lobby_img]
        ocr_lo = [
            [{"text": "历史重构", "compact": "历史重构", "cx": 134, "cy": 306}],
            pts32,
            pts32 + [{"text": "未解锁", "compact": "未解锁", "cx": 1110, "cy": 668}],
            pts32 + [{"text": "开局希望+2", "compact": "开局希望+2", "cx": 520, "cy": 650}, {"text": "确认升级 x3", "compact": "确认升级x3", "x": 1040, "y": 648, "w": 140, "h": 40, "cx": 1110, "cy": 668}],
            pts29 + [{"text": "已升级", "compact": "已升级", "cx": 1110, "cy": 668}],
            [{"text": "开始探险", "compact": "开始探险", "cx": 1060, "cy": 610}],
        ]
        img_lo_it = iter(imgs_lo)
        ocr_lo_it = iter(ocr_lo)
        taps_lo = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_lo_it, lobby_img)), \
                 patch.object(roguelike_ai_copilot, "detect_upgradeable_tech_nodes", side_effect=lit_locked_order), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_lo_it, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps_lo.append((x, y)) or True), \
                 patch("time.sleep"):
                res_lo = roguelike_ai_copilot.auto_upgrade_roguelike_tech_tree(
                    "adb.exe", "127.0.0.1:16384", Path(tmp), theme="Sarkaz"
                )
        self.assertTrue(res_lo["ok"])
        self.assertEqual(res_lo["upgraded_count"], 1)
        # After (720, 340) returned '未解锁', MUST immediately jump LEFT to (460, 340) rather than right (880, 340)!
        self.assertIn((460, 340), taps_lo)
        self.assertNotIn((880, 340), taps_lo)


if __name__ == "__main__":
    unittest.main()
