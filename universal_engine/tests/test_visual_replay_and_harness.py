"""Offline tests for visual_replay, FakeAdbReplayHarness closed loops and global Dry-Run interception.

Everything runs in ``tempfile.TemporaryDirectory()``: no real ADB, no real ``config/`` or ``reports/``.

Covers:
a. ``tools.visual_replay`` (4 modes, mocked OCR, 1280x720 annotated PNG, structured result)
b. ``tools.replay_harness.FakeAdbReplayHarness`` driving the REAL state machines in
   ``roguelike_ai_copilot`` / ``stage_pilot`` (perception mocked by frame id, logic untouched)
c. Global Dry-Run zero-side-effect interception (toolbox / MaaSession / scheduler / stage_pilot /
   webhook / post-action / game restart / LauncherController.set_dry_run)
d. Open-source repo ``.gitignore`` isolation of ``local_fixtures/`` and ``reports/replay_debug/``
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from universal_engine import maa_runner, roguelike_ai_copilot as rl, stage_pilot, toolbox_runner
from universal_engine.tests.test_toolbox_runner_and_gui import _create_mock_maa_structure
from universal_engine.tools import replay_harness, visual_replay
from universal_engine.utils import env_doctor, runtime_guard

ADB = "adb.exe"
ADDR = "127.0.0.1:16384"


# --------------------------------------------------------------------------- helpers
def _frame(fid: int, ui: bool = False, canvas: int = 0) -> Image.Image:
    """Synthetic 1280x720 frame. Pixel (0,0) red channel carries the frame id for mocked perception."""
    img = Image.new("RGB", (1280, 720), (12, 12, 12))
    d = ImageDraw.Draw(img)
    if ui:  # rendered UI chrome so rl._has_rendered_ui_chrome() is True
        for x in range(0, 1280, 8):
            d.rectangle([x, 0, x + 3, 90], fill=(210, 210, 210))
    if canvas:  # rendered tech-tree canvas interior
        for x in range(110, 1180, 10):
            d.rectangle([x, 130, x + 4, 570], fill=(canvas, canvas, canvas))
    img.putpixel((0, 0), (fid, 0, 0))
    return img


def _fid(img: Image.Image) -> int:
    return int(img.convert("RGB").getpixel((0, 0))[0])


def _box(text: str, cx: int, cy: int, w: int = 120, h: int = 40) -> Dict[str, Any]:
    return {
        "text": text,
        "compact": text.replace(" ", ""),
        "x": cx - w // 2,
        "y": cy - h // 2,
        "w": w,
        "h": h,
        "cx": cx,
        "cy": cy,
    }


def _ocr_by_frame(table: Dict[int, List[Dict[str, Any]]]):
    def _fake(img, *a, **kw):
        return [dict(b) for b in table.get(_fid(img), [])]

    return _fake


def _png_ok(testcase: unittest.TestCase, path: Any, size=(1280, 720)) -> None:
    p = Path(str(path))
    testcase.assertTrue(p.is_file(), f"missing png {p}")
    testcase.assertGreater(p.stat().st_size, 0)
    with Image.open(str(p)) as im:
        testcase.assertEqual(im.size, size)


class _GuardCleanMixin:
    def setUp(self) -> None:  # noqa: N802
        super().setUp()
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("MAA_DRY_RUN", None)
        runtime_guard.set_process_dry_run(False)

    def tearDown(self) -> None:  # noqa: N802
        runtime_guard.set_process_dry_run(False)
        self._env.stop()
        super().tearDown()


# --------------------------------------------------------------------------- a. visual_replay
class VisualReplayTests(_GuardCleanMixin, unittest.TestCase):
    def _run(self, mode: str, boxes: List[Dict[str, Any]], **kw) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            src = tmp_p / "shot.png"
            _frame(7).save(str(src))
            logs: List[str] = []
            with patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **k: [dict(b) for b in boxes]), \
                 patch.object(stage_pilot, "find_stage_boxes_via_ocr", side_effect=lambda *a, **k: [dict(b) for b in boxes]), \
                 patch.object(stage_pilot, "run_win_ocr_on_images", return_value={}), \
                 patch.object(stage_pilot, "detect_network_disconnect_popup", **kw.pop("popup_patch", {"return_value": {"detected": False}})):
                res = visual_replay.run_visual_replay(
                    src,
                    mode,
                    theme="BlackFlow",
                    target_stage=kw.get("target_stage", ""),
                    maa_dir=tmp_p / "maa",
                    out_dir=tmp_p / "out",
                    json_out=tmp_p / "out" / "res.json",
                    log_fn=logs.append,
                )
            _png_ok(self, res["overlay_path"])
            self.assertTrue((tmp_p / "out" / "res.json").is_file())
            on_disk = json.loads((tmp_p / "out" / "res.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["mode"], mode)
            self.assertTrue(any("[REPLAY]" in s for s in logs))
            # copy out what we need before tmp disappears
            res["_logs"] = logs
            return res

    def test_render_debug_overlay_normalizes_to_1280x720(self):
        small = Image.new("RGB", (640, 360), (30, 30, 30))
        out = visual_replay.render_debug_overlay(
            small,
            {
                "target_boxes": [{"bbox": [100, 100, 200, 160], "label": "hit"}],
                "filtered_boxes": [{"bbox": [300, 100, 400, 160], "label": "skip | reason"}],
                "forbidden_zones": [{"bbox": [0, 0, 100, 720], "label": "zone"}],
                "taps": [{"cx": 640, "cy": 360, "label": "TAP"}],
                "swipes": [{"x1": 900, "y1": 360, "x2": 300, "y2": 360, "label": "SWIPE"}],
                "banner_lines": ["banner"],
            },
            title="t",
        )
        self.assertEqual(out.size, (1280, 720))
        self.assertEqual(out.mode, "RGB")
        # also tolerates empty input
        self.assertEqual(visual_replay.render_debug_overlay(None, {}, title="").size, (1280, 720))

    def test_mode_stage_map(self):
        boxes = [_box("1-7", 330, 315, 60, 30), _box("1-8", 700, 400, 60, 30)]
        res = self._run("stage_map", boxes, target_stage="1-7")
        self.assertEqual(res["mode"], "stage_map")
        self.assertEqual(res["target_stage"], "1-7")
        self.assertEqual(len(res["ocr_boxes"]), 2)
        self.assertIn("nodes", res)
        self.assertIn("safe_blank_tap", res)
        self.assertIn("决策", res["decision"])
        self.assertIn("target_boxes", res["annotations"])
        self.assertIn("filtered_boxes", res["annotations"])
        self.assertTrue(res.get("target_match"), f"target 1-7 not matched: {res.get('target_match')} errors={res['errors']}")
        # non-target box is rendered as a filtered (gray) item with a reason
        self.assertTrue(any("非目标" in f["label"] for f in res["annotations"]["filtered_boxes"]))
        self.assertTrue(res["annotations"]["taps"])

    def test_mode_rogue_lobby(self):
        boxes = [_box("开始探险", 1100, 620), _box("科技树", 110, 330), _box("等级奖励", 110, 420)]
        res = self._run("rogue_lobby", boxes)
        self.assertEqual(res["mode"], "rogue_lobby")
        self.assertEqual(len(res["ocr_boxes"]), 3)
        self.assertIs(res["is_theme_lobby"], True)
        self.assertIs(res["is_theme_select_page"], False)
        self.assertIn("tech_entry", res)
        self.assertIn("level_reward_entry", res)
        self.assertIn("monthly_commission_entry", res)
        self.assertIn("决策", res["decision"])

    def test_mode_rogue_tech(self):
        boxes = [_box("64", 1170, 60, 60, 40), _box("72/78 已重构", 120, 610, 200, 40)]
        res = self._run("rogue_tech", boxes)
        self.assertEqual(res["mode"], "rogue_tech")
        for key in ("page_guard", "remaining_points", "progress_badge", "upgradeable_nodes", "confirm_button"):
            self.assertIn(key, res)
        zones = res["annotations"]["forbidden_zones"]
        self.assertGreaterEqual(len(zones), 2)
        self.assertTrue(any("弧形" in z["label"] for z in zones))  # arch zone
        self.assertIn("决策", res["decision"])

    def test_mode_offline_popup(self):
        popup = {
            "detected": True,
            "confirm_xy": (640, 560),
            "confirm_source": "mock",
            "matched_keywords": ["网络"],
        }
        res = self._run("offline_popup", [], popup_patch={"return_value": popup})
        self.assertEqual(res["mode"], "offline_popup")
        self.assertTrue(res["disconnect_popup"]["detected"])
        self.assertIn("home_or_online", res)
        self.assertIn("guide_popup", res)
        self.assertIn("断线弹窗=True", res["decision"])
        self.assertTrue(any(abs(t["cx"] - 640) < 1 and abs(t["cy"] - 560) < 1 for t in res["annotations"]["taps"]))

    def test_unknown_mode_rejected_and_cli_main(self):
        with self.assertRaises(ValueError):
            visual_replay.run_visual_replay("x.png", "nope")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "a.png"
            _frame(1).save(str(src))
            with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[]), \
                 patch.object(stage_pilot, "run_win_ocr_on_images", return_value={}), \
                 patch.object(stage_pilot, "detect_network_disconnect_popup", return_value={"detected": False}):
                code = visual_replay.main(
                    ["--image", str(src), "--mode", "offline_popup", "--maa-dir", tmp, "--out-dir", str(Path(tmp) / "o")]
                )
            self.assertEqual(code, 0)
            self.assertTrue(list((Path(tmp) / "o").glob("replay_offline_popup_*.png")))


# --------------------------------------------------------------------------- b. harness closed loops
class FakeAdbHarnessClosedLoopTests(_GuardCleanMixin, unittest.TestCase):
    maa_dir = Path(".")

    def test_reward_page_exit_does_not_double_tap_return(self):
        """_claim_all + _tap_return_to_lobby stop at the lobby and never overshoot into theme select."""
        harness = replay_harness.FakeAdbReplayHarness.from_frames(
            [_frame(0), _frame(1), _frame(2), _frame(3), _frame(4)], stay_on_last=True
        )
        table = {
            0: [_box("一键领取", 1100, 650)],
            1: [_box("获得物品", 640, 300)],
            2: [_box("已全部领取", 640, 650)],
            3: [_box("开始探险", 1100, 620)],
            4: [_box("进入主题", 640, 600), _box("添加到终端", 900, 600)],
        }
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=_ocr_by_frame(table)):
            with harness.attach(fast_sleep=True):
                claimed = rl._claim_all_on_current_reward_page(ADB, ADDR)
                self.assertEqual(claimed, 1)
                self.assertEqual(harness.current_index, 2)
                ok = rl._tap_return_to_lobby(ADB, ADDR, self.maa_dir, "BlackFlow")
        self.assertTrue(ok)
        back_taps = [a for a in harness.actions if a.kind == "tap" and (a.x, a.y) == (64.0, 40.0)]
        self.assertEqual(len(back_taps), 1, "'<' must be tapped exactly once")
        self.assertEqual(harness.current_index, 3, "must stop on the lobby, not overshoot to theme select")
        self.assertNotIn(4, [a.frame_index for a in harness.actions])
        harness.assert_action_count(min_count=3, max_count=3, kind="tap")
        harness.assert_no_forbidden_taps([[0, 0, 40, 20]])
        with self.assertRaises(AssertionError):
            harness.assert_no_forbidden_taps([[50, 30, 80, 50]])

    def test_return_to_lobby_refuses_to_tap_on_theme_select_page(self):
        harness = replay_harness.FakeAdbReplayHarness.from_frames([_frame(4), _frame(3)])
        table = {4: [_box("进入主题", 640, 600)], 3: [_box("开始探险", 1100, 620)]}
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=_ocr_by_frame(table)):
            with harness.attach(fast_sleep=True):
                ok = rl._tap_return_to_lobby(ADB, ADDR, self.maa_dir, "BlackFlow")
        self.assertFalse(ok)
        self.assertEqual(harness.actions, [])

    def _tech_patches(self, points: Dict[int, int], nodes: Dict[int, List[Dict[str, Any]]], valid_fids: List[int]):
        confirm = _box("确认升级", 1000, 660, 200, 50)
        hit = {
            "theme": "BlackFlow",
            "profile": rl.get_theme_ui_profile("BlackFlow"),
            "box": _box("科技树", 100, 300),
            "matched_via_fallback": False,
            "match_stage": "fullscreen_ocr",
            "requires_panel_verify": False,
        }

        def _guard(img, boxes, maa_dir=None, active_profile=None, cancel_event=None):
            return {"status": "tech_tree", "points": points.get(_fid(img))}

        def _points(img, boxes, allow_roi_ocr=True, cancel_event=None):
            return points.get(_fid(img))

        def _confirm(img, boxes, cancel_event=None, remaining_points=None):
            if _fid(img) in valid_fids and (points.get(_fid(img)) or 0) > 0:
                return {"valid": True, "button": dict(confirm), "button_status": "confirm_upgrade", "detail_text": "生命值+1"}
            return {"valid": False, "button_status": "none"}

        def _nodes(img, *a, **k):
            return [dict(n) for n in nodes.get(_fid(img), [])]

        return [
            patch.object(rl, "resolve_lobby_tech_tree_entry", return_value=hit),
            patch.object(rl, "classify_tech_tree_page_guard", side_effect=_guard),
            patch.object(rl, "extract_tech_tree_remaining_points", side_effect=_points),
            patch.object(rl, "evaluate_tech_tree_confirm_button", side_effect=_confirm),
            patch.object(rl, "detect_upgradeable_tech_nodes", side_effect=_nodes),
        ]

    def _run_tech(self, harness, patches, table):
        logs: List[str] = []
        from contextlib import ExitStack

        with ExitStack() as st:
            st.enter_context(patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=_ocr_by_frame(table)))
            for p in patches:
                st.enter_context(p)
            with harness.attach(fast_sleep=True):
                res = rl.auto_upgrade_roguelike_tech_tree(
                    ADB, ADDR, self.maa_dir, theme="BlackFlow", lobby_confirmed_theme="BlackFlow", log_callback=logs.append
                )
        return res, logs

    def test_tech_tree_multi_step_upgrade_points_to_zero(self):
        # lobby -> tree(64) -> tree(32) -> tree(0) ; badge 72/78 on the first tree frame
        frames = [_frame(0), _frame(1, ui=True, canvas=90), _frame(2, ui=True, canvas=90), _frame(3, ui=True, canvas=90)]
        harness = replay_harness.FakeAdbReplayHarness.from_frames(frames)
        table = {0: [_box("科技树", 100, 300)], 1: [_box("72/78 已重构", 120, 610, 200, 40)]}
        patches = self._tech_patches({1: 64, 2: 32, 3: 0}, {}, valid_fids=[1, 2, 3])
        res, _ = self._run_tech(harness, patches, table)
        self.assertTrue(res["ok"])
        self.assertTrue(res["entry_found"])
        self.assertEqual(res["upgraded_count"], 2)
        self.assertEqual([(n["points_before"], n["points_after"]) for n in res["upgraded_nodes"]], [(64, 32), (32, 0)])
        self.assertEqual(harness.current_index, 3)
        harness.assert_action_count(max_count=0, kind="swipe")
        # entry tap + 2 confirm taps, then only bounded '<' exit taps (cleanup, max 3) - no node/canvas taps
        self.assertEqual([(a.x, a.y) for a in harness.actions[:3]], [(100.0, 300.0), (1000.0, 660.0), (1000.0, 660.0)])
        tail = harness.actions[3:]
        self.assertLessEqual(len(tail), 3)
        self.assertTrue(all((a.x, a.y) == (64.0, 40.0) for a in tail))

    def test_tech_tree_left_swipe_discovers_page2_nodes(self):
        # lobby -> page1 (no lit nodes) --swipe--> page2 (node) --tap--> detail --confirm--> points 0
        frames = [
            _frame(0),
            _frame(1, ui=True, canvas=80),
            _frame(2, ui=True, canvas=170),
            _frame(3, ui=True, canvas=170),
            _frame(4, ui=True, canvas=170),
        ]
        harness = replay_harness.FakeAdbReplayHarness.from_frames(frames)
        table = {0: [_box("科技树", 100, 300)]}
        node = {"cx": 640, "cy": 360, "frontier_tier": 1, "has_halo": True}
        patches = self._tech_patches({1: 10, 2: 10, 3: 10, 4: 0}, {2: [node]}, valid_fids=[3])
        res, _ = self._run_tech(harness, patches, table)
        self.assertEqual(res["upgraded_count"], 1, res)
        swipes = [a for a in harness.actions if a.kind == "swipe"]
        self.assertEqual(len(swipes), 1)
        self.assertEqual((swipes[0].x, swipes[0].y, swipes[0].x2, swipes[0].y2), (960.0, 360.0, 320.0, 360.0))
        self.assertEqual(swipes[0].frame_index, 1)
        node_taps = [a for a in harness.actions if a.kind == "tap" and (a.x, a.y) == (640.0, 360.0)]
        self.assertEqual(len(node_taps), 1)
        self.assertEqual(node_taps[0].frame_index, 2, "page-2 node must be tapped after the swipe")
        self.assertEqual(res["upgraded_nodes"][0]["cx"], 640)

    def test_network_disconnect_popup_reconnect_and_recovery(self):
        frames = [_frame(0), _frame(1), _frame(2)]  # normal -> popup -> online
        harness = replay_harness.FakeAdbReplayHarness.from_frames(frames)

        def _detect(img, maa_dir=None, use_ocr=True, cancel_event=None):
            if _fid(img) == 1:
                return {"detected": True, "confirm_xy": (640, 560), "confirm_source": "mock", "matched_keywords": ["网络"]}
            return {"detected": False}

        with patch.object(stage_pilot, "detect_network_disconnect_popup", side_effect=_detect):
            with harness.attach(fast_sleep=True):
                before = stage_pilot.dismiss_network_disconnect_popup(ADB, ADDR, self.maa_dir, wait_after_tap_sec=0)
                self.assertFalse(before["detected"])
                self.assertEqual(before["taps"], 0)
                # mid-run the connection drops: any in-game action moves to the popup frame
                stage_pilot.adb_tap_norm(ADB, ADDR, 300, 300)
                self.assertEqual(harness.current_index, 1)
                res = stage_pilot.dismiss_network_disconnect_popup(ADB, ADDR, self.maa_dir, wait_after_tap_sec=0)
                self.assertEqual(harness.current_index, 2)
                online = stage_pilot.detect_network_disconnect_popup(harness.screencap(ADB, ADDR), self.maa_dir)
        self.assertTrue(res["detected"])
        self.assertTrue(res["dismissed"])
        self.assertEqual(res["taps"], 1)
        self.assertEqual(list(res["tapped_coords"]), [(640, 560)])
        self.assertFalse(online["detected"])
        self.assertEqual((harness.actions[-1].x, harness.actions[-1].y), (640.0, 560.0))

    def test_scenario_json_conditional_routes_forbidden_taps_and_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            for i in range(3):
                _frame(i).save(str(tmp_p / f"f{i}.png"))
            scenario = {
                "frames": [
                    {"image": "f0.png", "name": "start", "routes": [{"kind": "tap", "rect": [600, 300, 700, 400], "next": 2}]},
                    {"image": "f1.png", "name": "unused"},
                    {"image": "f2.png", "name": "end", "routes": [{"kind": "swipe", "rect": [900, 300, 1000, 400], "next": 1}]},
                ]
            }
            sc = tmp_p / "scenario.json"
            sc.write_text(json.dumps(scenario), encoding="utf-8")
            harness = replay_harness.FakeAdbReplayHarness.from_scenario_json(sc)
            self.assertEqual(len(harness.frames), 3)
            with harness.attach(fast_sleep=True, out_dir=tmp_p / "auto_tl"):
                stage_pilot.adb_tap_norm("", "", 100, 100)  # outside rect: stays
                self.assertEqual(harness.current_index, 0)
                stage_pilot.adb_swipe_norm("", "", 650, 350, 100, 350)  # wrong kind: stays
                self.assertEqual(harness.current_index, 0)
                stage_pilot.adb_tap_norm("", "", 650, 350)  # inside rect: -> 2
                self.assertEqual(harness.current_index, 2)
                stage_pilot.adb_tap_norm("", "", 950, 350)  # tap in swipe-only rect: stays
                self.assertEqual(harness.current_index, 2)
                stage_pilot.adb_swipe_norm("", "", 950, 350, 300, 350)  # swipe: -> 1
                self.assertEqual(harness.current_index, 1)
                img = stage_pilot.adb_screencap_image("", "")
                self.assertEqual(_fid(img), 1)
            self.assertEqual(len(harness.actions), 5)
            harness.assert_no_forbidden_taps([[0, 0, 50, 50]])
            with self.assertRaises(AssertionError):
                harness.assert_no_forbidden_taps([[640, 340, 660, 360]])
            paths = harness.dump_timeline_overlays(tmp_p / "timeline")
            self.assertEqual(len(paths), 5)
            for p in paths:
                _png_ok(self, p)
            # attach(out_dir=...) auto-dumps on exit
            self.assertEqual(len(list((tmp_p / "auto_tl").glob("timeline_*.png"))), 5)
            # hooks are released after attach
            self.assertIsNone(runtime_guard.get_adb_hooks())


# --------------------------------------------------------------------------- c. dry-run
class DryRunInterceptionTests(_GuardCleanMixin, unittest.TestCase):
    TOOL_CASES = [
        ("event_shop", {"blacklist_presets": ["数据增补仪", "龙门币"], "custom_blacklist": "碳"}),
        ("green_ticket_shop", {}),
        ("yellow_ticket_shop", {}),
        ("auto_raise_potential", {"use_normal_token": True}),
        ("stronghold_protocol", {"mode": "badge", "theme": "Regulation", "target": 3, "runs": 60}),
    ]

    def _no_side_effects(self):
        from contextlib import ExitStack

        st = ExitStack()
        mocks = {
            "maa_run_cmd": st.enter_context(patch.object(maa_runner, "_run_cmd")),
            "sp_run_cmd": st.enter_context(patch.object(stage_pilot, "_run_cmd")),
            "sub_run": st.enter_context(patch.object(subprocess, "run")),
            "sub_popen": st.enter_context(patch.object(subprocess, "Popen")),
        }
        return st, mocks

    def _assert_clean(self, mocks):
        for name, m in mocks.items():
            m.assert_not_called()

    @staticmethod
    def _summary_records(out_dir: Path) -> List[Dict[str, Any]]:
        files = sorted(out_dir.glob("dryrun_summary_*.json"))
        assert files, f"no dryrun_summary in {out_dir}"
        return json.loads(files[-1].read_text(encoding="utf-8"))["records"]

    def test_toolbox_runner_intercepts_append_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_p)
            cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG.copy()
            for tool_id, ov in self.TOOL_CASES:
                with self.subTest(tool=tool_id):
                    spec = toolbox_runner.build_toolbox_task_spec(tool_id, maa_dir, cfg, overrides=ov)
                    self.assertTrue(spec["available"], spec)
                    out = tmp_p / f"out_{tool_id}"
                    session = MagicMock()
                    st, mocks = self._no_side_effects()
                    with st, runtime_guard.dry_run_scope(True, max_actions=3, out_dir=out):
                        toolbox_runner.run_toolbox_task({"session": session}, tool_id, cfg, maa_dir=maa_dir, overrides=ov)
                    session.append_task.assert_not_called()
                    self._assert_clean(mocks)
                    tasks = [r for r in self._summary_records(out) if r["kind"] == "task"]
                    self.assertEqual(len(tasks), 1)
                    self.assertEqual(tasks[0]["task_type"], spec["task_type"])
                    self.assertEqual(tasks[0]["params"], spec["task_params"])
            # blacklist really reached the recorded params
            ev = [r for r in self._summary_records(tmp_p / "out_event_shop") if r["kind"] == "task"][0]
            self.assertEqual(ev["params"]["params"]["event_shop"]["blacklist"], ["数据增补仪", "龙门币", "碳"])

    def test_maa_session_run_single_task_never_reaches_asst(self):
        cases = [(tid, toolbox_runner.build_toolbox_task_spec(tid, _create_mock_maa_structure(Path(tempfile.mkdtemp())), env_doctor.DEFAULT_LAUNCHER_CONFIG.copy(), overrides=ov)) for tid, ov in self.TOOL_CASES]
        cases = [(tid, s["task_type"], s["task_params"]) for tid, s in cases]
        cases.append(("blackflow_money", "Roguelike", {"theme": "Roguelike@BlackFlow", "mode": 1, "investment_enabled": True}))
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG.copy()
            for tool_id, task_type, params in cases:
                with self.subTest(tool=tool_id):
                    out = tmp_p / f"o_{tool_id}"
                    sess = maa_runner.MaaSession(tmp_p / "MAA", cfg, base_dir=tmp_p)
                    sess._asst = MagicMock()
                    st, mocks = self._no_side_effects()
                    with st, runtime_guard.dry_run_scope(True, max_actions=3, out_dir=out):
                        res = sess.run_single_task(task_type, params)
                    sess._asst.append_task.assert_not_called()
                    self._assert_clean(mocks)
                    self.assertTrue(res.get("dry_run"))
                    self.assertEqual(res["task_type"], task_type)
                    self.assertEqual(res["params"], params)
                    tasks = [r for r in self._summary_records(out) if r["kind"] == "task"]
                    self.assertEqual([(t["task_type"], t["params"]) for t in tasks], [(task_type, params)])

    def test_master_scheduler_toolbox_under_dry_run(self):
        from universal_engine import master_scheduler

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_p)
            cfg_path = tmp_p / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "maa_dir": str(maa_dir)}, cfg_path)
            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_p)
            for tool_id, ov in self.TOOL_CASES[:2]:
                with self.subTest(tool=tool_id):
                    mock_session = MagicMock()
                    out = tmp_p / f"sched_{tool_id}"
                    st, mocks = self._no_side_effects()
                    with st, patch.object(engine, "_connect_session", return_value=(str(maa_dir), mock_session, None)), \
                         patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": ADB, "adb_addr": ADDR}), \
                         runtime_guard.dry_run_scope(True, max_actions=3, out_dir=out):
                        engine.run_toolbox_task(tool_id, overrides=ov, dry_run_post_action=True)
                    mock_session.append_task.assert_not_called()
                    self._assert_clean(mocks)
                    tasks = [r for r in self._summary_records(out) if r["kind"] == "task"]
                    self.assertEqual(len(tasks), 1)
                    self.assertTrue(tasks[0]["task_type"])
                    self.assertIsInstance(tasks[0]["params"], dict)

    def test_stage_pilot_tap_swipe_budget_and_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dry"
            ev = threading.Event()
            runtime_guard.set_last_captured_image(_frame(9, ui=True, canvas=100))
            with patch.object(stage_pilot, "_run_cmd") as run_cmd, patch.object(stage_pilot, "run_cmd_cancellable") as rcc:
                with runtime_guard.dry_run_scope(True, max_actions=3, out_dir=out) as ctx:
                    r1 = stage_pilot.adb_tap_norm(ADB, ADDR, 640, 360, cancel_event=ev)
                    r2 = stage_pilot.adb_swipe_norm(ADB, ADDR, 900, 360, 300, 360, 400, cancel_event=ev)
                    self.assertFalse(ev.is_set())
                    self.assertFalse(ctx.budget_exceeded)
                    r3 = stage_pilot.adb_tap_norm(ADB, ADDR, 100, 100, cancel_event=ev)
                run_cmd.assert_not_called()
                rcc.assert_not_called()
            self.assertTrue(r1)
            self.assertTrue(r2)
            self.assertFalse(r3, "3rd action hits the budget")
            self.assertTrue(ctx.budget_exceeded)
            self.assertTrue(ev.is_set(), "cancel_event must be set on budget exhaustion")
            recs = self._summary_records(out)
            self.assertEqual([r["kind"] for r in recs], ["tap", "swipe", "tap"])
            for r in recs:
                self.assertTrue(r.get("overlay_path"), r)
                _png_ok(self, r["overlay_path"])

    def test_webhook_post_action_and_game_restart_are_blocked(self):
        cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG.copy()
        cfg["notifications"] = {"webhook_enabled": True, "webhook_url": "https://example.invalid/hook"}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dry"
            with patch("urllib.request.urlopen") as urlopen, \
                 patch.object(maa_runner, "_run_cmd") as run_cmd, \
                 patch.object(maa_runner, "run_cmd_cancellable", create=True) as rcc, \
                 patch.object(maa_runner, "discover_adb_executable", return_value=ADB), \
                 patch.object(maa_runner, "_probe_adb_address", return_value=ADDR), \
                 patch.object(maa_runner, "_find_emulator_launcher", return_value=None), \
                 patch.object(maa_runner, "ensure_emulator_ready", side_effect=AssertionError("must not probe emulator")), \
                 patch.object(subprocess, "run") as sub_run, patch.object(subprocess, "Popen") as sub_popen:
                with runtime_guard.dry_run_scope(True, max_actions=3, out_dir=out):
                    wh = maa_runner.send_webhook_notification(cfg, "T", "C", event_type="info")
                    sd = maa_runner.execute_post_action("shutdown", cfg)
                    ce = maa_runner.execute_post_action("close_emulator", cfg)
                    fr = maa_runner._force_restart_game_client(cfg, ADB, ADDR, wait_after_stop=0, wait_after_start=0)
                    eg = maa_runner.ensure_game_running(cfg)
                urlopen.assert_not_called()
                run_cmd.assert_not_called()
                rcc.assert_not_called()
                sub_run.assert_not_called()
                sub_popen.assert_not_called()
            self.assertFalse(wh.get("sent"))
            self.assertTrue(wh.get("dry_run"))
            for r in (sd, ce):
                self.assertFalse(r["executed"])
                self.assertTrue(r["dry_run"])
            self.assertEqual(sd["action"], "shutdown")
            self.assertEqual(ce["action"], "close_emulator")
            self.assertTrue(fr)
            self.assertTrue(isinstance(eg, str) and eg)
            recs = self._summary_records(out)
            actions = [r.get("action") for r in recs if r["kind"] == "control"]
            self.assertEqual(actions, ["shutdown", "close_emulator", "force_restart_game_client", "ensure_game_running"])
            self.assertEqual([r["kind"] for r in recs].count("webhook"), 1)
            force = [r for r in recs if r.get("action") == "force_restart_game_client"][0]
            self.assertIn("force-stop", " ".join(force["command"]))  # recorded, never executed

    def test_controller_set_dry_run_is_memory_only(self):
        from universal_engine import master_scheduler
        from universal_engine.gui import controller as controller_mod

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            cfg_path = tmp_p / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            before = cfg_path.read_bytes()
            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_p)
            ctrl = controller_mod.LauncherController(engine=engine, auto_start_refresh=False)

            res_on = ctrl.set_dry_run(True)
            self.assertTrue(res_on["ok"])
            self.assertIs(res_on["dry_run"], True)
            self.assertIs(res_on["snapshot"]["dry_run"], True)
            self.assertTrue(runtime_guard.is_dry_run_enabled())
            self.assertEqual(cfg_path.read_bytes(), before, "launcher_config.json must not be modified")

            res_off = ctrl.set_dry_run(False)
            self.assertIs(res_off["dry_run"], False)
            self.assertIs(res_off["snapshot"]["dry_run"], False)
            self.assertFalse(runtime_guard.is_dry_run_enabled())
            self.assertEqual(cfg_path.read_bytes(), before)
            self.assertNotIn("dry_run", json.loads(cfg_path.read_text(encoding="utf-8")))

            ctrl.set_dry_run(True)
            runtime_guard.set_process_dry_run(False)  # simulate a restarted process
            fresh = controller_mod.LauncherController(engine=engine, auto_start_refresh=False)
            self.assertIs(bool(getattr(fresh, "_snapshot_formatted", {}).get("dry_run", False)), False)
            self.assertFalse(runtime_guard.is_dry_run_enabled())


# --------------------------------------------------------------------------- d. repo gitignore
class OpenSourceRepoGitignoreTests(unittest.TestCase):
    REPO = Path(__file__).resolve().parent.parent.parent

    def test_private_fixtures_and_debug_outputs_are_ignored(self):
        repo_dir = self.REPO
        if not repo_dir.is_dir():
            self.skipTest("open-source repo not present")
        paths = [
            "universal_engine/local_fixtures/sample.png",
            "universal_engine/local_fixtures/scenario.json",
            "reports/replay_debug/out.png",
        ]
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo_dir), "check-ignore", "-v", *paths],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self.skipTest("git unavailable")
        out = proc.stdout
        for p in paths:
            self.assertIn(p, out, f"{p} is NOT ignored\n{out}\n{proc.stderr}")
        self.assertEqual(proc.returncode, 0)
        text = (repo_dir / ".gitignore").read_text(encoding="utf-8", errors="replace")
        for rule in ("local_fixtures/", "reports/replay_debug/", "*.bmp", "*.webp"):
            self.assertIn(rule, text)


if __name__ == "__main__":
    unittest.main()
