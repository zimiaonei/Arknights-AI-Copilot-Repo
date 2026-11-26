import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image, ImageDraw

from universal_engine import maa_runner, master_scheduler, roguelike_ai_copilot, stage_pilot
from universal_engine.gui import contract
from universal_engine.gui.bridge import WebViewJsApi
from universal_engine.gui.controller import LauncherController
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_disconnect_modal_image(with_white_circle: bool = True) -> Image.Image:
    """Create a synthetic 1280x720 Arknights network disconnect modal screenshot."""
    img = Image.new("RGB", (1280, 720), (35, 38, 46))
    draw = ImageDraw.Draw(img)
    # Center-top white message box & lower-middle dark bar (matches real capture layout)
    draw.rectangle([0, 210, 1280, 445], fill=(235, 236, 240))
    draw.rectangle([0, 446, 1280, 540], fill=(32, 34, 40))
    if with_white_circle:
        # White circular confirm button centered around (640, 491), radius 18px
        draw.ellipse([622, 473, 658, 509], fill=(252, 252, 252))
    return img


def _make_tech_tree_canvas_with_lit_node(cx: int = 620, cy: int = 340) -> Image.Image:
    """Create a dark 1280x720 tech-tree canvas with one bright glowing upgradeable node."""
    img = Image.new("RGB", (1280, 720), (22, 26, 36))
    draw = ImageDraw.Draw(img)
    draw.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(245, 210, 90), outline=(255, 255, 255), width=3)
    return img


class NetworkDisconnectSelfHealingTests(unittest.TestCase):
    def test_locate_confirm_button_visual_fallback_and_online_verification(self):
        modal_img = _make_disconnect_modal_image(with_white_circle=True)
        xy, src = stage_pilot.locate_disconnect_confirm_button(modal_img, maa_dir=None)
        self.assertEqual(src, "white_circle_visual")
        self.assertLessEqual(abs(xy[0] - 640), 4)
        self.assertLessEqual(abs(xy[1] - 491), 4)

        blank_img = Image.new("RGB", (1280, 720), (20, 20, 25))
        fb_xy, fb_src = stage_pilot.locate_disconnect_confirm_button(blank_img, maa_dir=None)
        self.assertEqual(fb_src, "fallback_norm_0.50_0.70")
        self.assertEqual(fb_xy, (640, 504))

        # OCR detection of "与神经网络连接丢失，请重新连接 (错误号:0)"
        disc_boxes = [
            {"text": "与神经网络连接丢失，请重新连接", "compact": "与神经网络连接丢失，请重新连接", "cx": 640, "cy": 310},
            {"text": "(错误号:0)", "compact": "(错误号:0)", "cx": 640, "cy": 355},
        ]
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=disc_boxes):
            det = stage_pilot.detect_network_disconnect_popup(modal_img, maa_dir=None, use_ocr=True)
        self.assertTrue(det["detected"])
        self.assertTrue(det["ocr_hit"])
        self.assertIn("与神经网络连接丢失", det["matched_keywords"])
        self.assertIn("请重新连接", det["matched_keywords"])
        self.assertIn("错误号", det["matched_keywords"])
        self.assertEqual(det["confirm_source"], "white_circle_visual")

        # Strict online verification rejects disconnect popup & login/loading, accepts Home
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=disc_boxes):
            v_disc = stage_pilot.is_game_home_or_online_screen(modal_img, maa_dir=None)
        self.assertFalse(v_disc["online"])
        self.assertTrue(v_disc["disconnect_popup"])
        self.assertEqual(v_disc["reason"], "still_on_disconnect_popup")

        loading_boxes = [{"text": "正在尝试与Rhodes Island取得神经连接 60%", "compact": "正在尝试与RhodesIsland取得神经连接60%"}]
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=loading_boxes):
            v_load = stage_pilot.is_game_home_or_online_screen(blank_img, maa_dir=None)
        self.assertFalse(v_load["online"])
        self.assertTrue(v_load["is_login_or_loading"])

        home_boxes = [
            {"text": "终端", "compact": "终端"},
            {"text": "编队", "compact": "编队"},
            {"text": "基建", "compact": "基建"},
            {"text": "理智", "compact": "理智"},
        ]
        with patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=home_boxes):
            v_home = stage_pilot.is_game_home_or_online_screen(blank_img, maa_dir=None)
        self.assertTrue(v_home["online"])
        self.assertTrue(v_home["is_home"])
        self.assertEqual(v_home["reason"], "home_verified")

    def test_session_live_intercepts_offline_confirm_and_consecutive_subtask_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Case A: Infrast@OfflineConfirm callback triggers OCR probe -> immediate early stop
            logs_a = []
            sess_a = maa_runner.MaaSession(maa_dir=base, config={}, base_dir=base, log_callback=logs_a.append)
            sess_a.adb_path = "adb.exe"
            sess_a.adb_addr = "127.0.0.1:16384"

            class _FakeAsstOffline:
                def __init__(self, session):
                    self.session = session
                    self.polls = 0
                    self.stopped = False

                def append_task(self, t, p):
                    return 11

                def start(self):
                    return True

                def running(self):
                    if self.stopped:
                        return False
                    self.polls += 1
                    if self.polls == 1:
                        self.session.events.append({
                            "ts": time.time(),
                            "message": "SubTaskStart",
                            "details": {"subtask": "ProcessTask", "details": {"task": "Infrast@OfflineConfirm"}},
                        })
                    return self.polls < 10

                def stop(self):
                    self.stopped = True

            fake_a = _FakeAsstOffline(sess_a)
            sess_a._asst = fake_a
            probe_hit = {
                "screenshot_ok": True,
                "detected": True,
                "ocr_hit": True,
                "matched_keywords": ["与神经网络连接丢失", "错误号"],
                "confirm_xy": (640, 491),
            }
            with patch.object(stage_pilot, "probe_network_disconnect_via_adb", return_value=probe_hit) as mock_probe_a, \
                 patch("time.sleep"):
                res_a = sess_a.run_single_task("Infrast", {"enable": True}, timeout_sec=60)

            self.assertTrue(fake_a.stopped)
            self.assertFalse(res_a["ok"])
            self.assertFalse(res_a["cancelled"])
            self.assertEqual(res_a["early_stop_reason"], "network_disconnect")
            self.assertTrue(res_a["network_disconnect_detected"])
            mock_probe_a.assert_called_once()
            self.assertTrue(any("[NET][掉线拦截]" in ln for ln in logs_a))

            # Case B: Consecutive InfrastMfgTask / InfrastTradeTask / InfrastReceptionTask SubTaskErrors -> OCR probe -> early stop
            logs_b = []
            sess_b = maa_runner.MaaSession(maa_dir=base, config={}, base_dir=base, log_callback=logs_b.append)
            sess_b.adb_path = "adb.exe"
            sess_b.adb_addr = "127.0.0.1:16384"

            class _FakeAsstSubErrs:
                def __init__(self, session):
                    self.session = session
                    self.polls = 0
                    self.stopped = False

                def append_task(self, t, p):
                    return 12

                def start(self):
                    return True

                def running(self):
                    if self.stopped:
                        return False
                    self.polls += 1
                    if self.polls == 1:
                        now = time.time()
                        for sub in ("InfrastMfgTask", "InfrastTradeTask", "InfrastReceptionTask"):
                            self.session.events.append({
                                "ts": now,
                                "message": "SubTaskError",
                                "details": {"subtask": sub, "pre_task": ""},
                            })
                    return self.polls < 10

                def stop(self):
                    self.stopped = True

            fake_b = _FakeAsstSubErrs(sess_b)
            sess_b._asst = fake_b
            with patch.object(stage_pilot, "probe_network_disconnect_via_adb", return_value=probe_hit) as mock_probe_b, \
                 patch("time.sleep"):
                res_b = sess_b.run_single_task("Infrast", {"enable": True}, timeout_sec=60)

            self.assertTrue(fake_b.stopped)
            self.assertFalse(res_b["ok"])
            self.assertEqual(res_b["early_stop_reason"], "network_disconnect")
            self.assertTrue(res_b["network_disconnect_detected"])
            mock_probe_b.assert_called_once()
            self.assertTrue(any("consecutive_subtask_errors" in ln for ln in logs_b))

    def test_run_task_with_recovery_rejects_false_startup_then_clicks_confirm_verifies_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            logs = []
            modal_img = _make_disconnect_modal_image(with_white_circle=True)
            home_img = Image.new("RGB", (1280, 720), (40, 50, 70))

            # Initial session returns ok=True on Infrast PackageTask, BUT events contain Infrast@OfflineConfirm!
            sess0 = MagicMock()
            sess0.maa_dir = base
            sess0.base_dir = base
            sess0.adb_path = "adb.exe"
            sess0.adb_addr = "127.0.0.1:16384"
            sess0.event_callback = None
            sess0.log_callback = logs.append
            sess0.cancel_event = None
            sess0.run_single_task.return_value = {
                "task_type": "Infrast",
                "ok": True,
                "timed_out": False,
                "cancelled": False,
                "events": [
                    {"message": "SubTaskStart", "details": {"details": {"task": "Infrast@OfflineConfirm"}}},
                    {"message": "SubTaskError", "details": {"subtask": "InfrastMfgTask"}},
                    {"message": "SubTaskError", "details": {"subtask": "InfrastTradeTask"}},
                ],
                "chain_errors": [],
                "sub_errors": [{"subtask": "InfrastMfgTask"}, {"subtask": "InfrastTradeTask"}],
                "log_lines": [],
            }

            # Reconnected sessions for Attempt 1 (StartUp false-positive!) and Attempt 2 (StartUp truly online + Infrast resume ok!)
            sess1 = MagicMock()
            sess1.maa_dir = base
            sess1.base_dir = base
            sess1.adb_path = "adb.exe"
            sess1.adb_addr = "127.0.0.1:16384"
            sess1.connect.return_value = True
            sess1.run_single_task.return_value = {"task_type": "StartUp", "ok": True, "events": [], "log_lines": []}

            sess2 = MagicMock()
            sess2.maa_dir = base
            sess2.base_dir = base
            sess2.adb_path = "adb.exe"
            sess2.adb_addr = "127.0.0.1:16384"
            sess2.connect.return_value = True

            def _sess2_run(task_type, params, **kwargs):
                if task_type == "StartUp":
                    return {"task_type": "StartUp", "ok": True, "events": [], "log_lines": []}
                return {
                    "task_type": task_type,
                    "ok": True,
                    "timed_out": False,
                    "cancelled": False,
                    "events": [{"message": "TaskChainCompleted", "details": {}}],
                    "chain_errors": [],
                    "sub_errors": [],
                    "log_lines": [],
                }

            sess2.run_single_task.side_effect = _sess2_run

            holder = {"session": sess0}
            taps = []
            verify_results = [
                # Attempt 1 secondary verification: StartUp lied! Still on disconnect popup!
                {
                    "online": False,
                    "is_home": False,
                    "is_login_or_loading": False,
                    "disconnect_popup": True,
                    "reason": "still_on_disconnect_popup",
                    "disconnect": {"matched_keywords": ["与神经网络连接丢失", "错误号"]},
                },
                # Attempt 2 secondary verification: truly back on Home!
                {
                    "online": True,
                    "is_home": True,
                    "is_login_or_loading": False,
                    "disconnect_popup": False,
                    "reason": "home_verified",
                    "disconnect": {"detected": False},
                },
            ]

            def _fake_dismiss(adb_path, adb_addr, **kwargs):
                taps.append((640, 491))
                return {"detected": True, "dismissed": True, "cancelled": False, "taps": 1, "tapped_coords": [(640, 491)], "still_popup": False}

            with patch.object(maa_runner, "MaaSession", side_effect=[sess1, sess2]), \
                 patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "wait_interruptible", return_value=False), \
                 patch.object(stage_pilot, "probe_network_disconnect_via_adb", return_value={"detected": True, "matched_keywords": ["与神经网络连接丢失"]}), \
                 patch.object(stage_pilot, "dismiss_network_disconnect_popup", side_effect=_fake_dismiss) as mock_dismiss, \
                 patch.object(stage_pilot, "adb_screencap_image", side_effect=[modal_img, home_img]), \
                 patch.object(stage_pilot, "is_game_home_or_online_screen", side_effect=verify_results):
                res = maa_runner.run_task_with_recovery(
                    holder,
                    "Infrast",
                    {"enable": True},
                    {"server": "Bilibili"},
                    max_retries=1,
                )

            self.assertTrue(res["ok"])
            self.assertTrue(res.get("network_recovered"))
            self.assertIs(holder["session"], sess2)
            # Both Attempt 1 and Attempt 2 dismissed popup -> clicked confirm button
            self.assertEqual(mock_dismiss.call_count, 2)
            self.assertEqual(len(taps), 2)
            # Verify Attempt 1 rejected false-positive StartUp and Attempt 2 verified online & resumed Infrast
            self.assertTrue(any("拒绝误判恢复成功" in ln for ln in logs))
            self.assertTrue(any("[NET][自愈成功]" in ln for ln in logs))
            called_tasks_sess2 = [c.args[0] for c in sess2.run_single_task.call_args_list]
            self.assertEqual(called_tasks_sess2, ["StartUp", "Infrast"])

    def test_network_recovery_exhausts_3_attempts_force_restarts_and_aborts_gracefully_without_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            maa = base / "maa"
            maa.mkdir(parents=True)
            logs = []
            modal_img = _make_disconnect_modal_image(with_white_circle=True)

            sess0 = MagicMock()
            sess0.maa_dir = maa
            sess0.base_dir = base
            sess0.adb_path = "adb.exe"
            sess0.adb_addr = "127.0.0.1:16384"
            sess0.event_callback = None
            sess0.log_callback = logs.append
            sess0.cancel_event = None
            sess0.run_single_task.return_value = {
                "task_type": "Infrast",
                "ok": False,
                "timed_out": False,
                "cancelled": False,
                "network_disconnect_detected": True,
                "early_stop_reason": "network_disconnect",
                "events": [{"message": "SubTaskStart", "details": {"details": {"task": "Infrast@OfflineConfirm"}}}],
                "chain_errors": [],
                "sub_errors": [],
                "log_lines": [],
            }

            reconn_sessions = []
            for _ in range(3):
                s = MagicMock()
                s.maa_dir = maa
                s.base_dir = base
                s.adb_path = "adb.exe"
                s.adb_addr = "127.0.0.1:16384"
                s.connect.return_value = True
                s.run_single_task.return_value = {"task_type": "StartUp", "ok": True, "events": [], "log_lines": []}
                reconn_sessions.append(s)

            holder = {"session": sess0}
            still_popup_verify = {
                "online": False,
                "is_home": False,
                "is_login_or_loading": False,
                "disconnect_popup": True,
                "reason": "still_on_disconnect_popup",
                "disconnect": {"matched_keywords": ["与神经网络连接丢失", "错误号"]},
            }
            dismiss_calls = []

            def _fake_dismiss(*args, **kwargs):
                dismiss_calls.append(1)
                return {"detected": True, "dismissed": False, "cancelled": False, "taps": 2, "tapped_coords": [(640, 491), (640, 491)], "still_popup": True}

            with patch.object(maa_runner, "MaaSession", side_effect=reconn_sessions), \
                 patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "_force_restart_game_client", return_value=True) as mock_force_restart, \
                 patch.object(maa_runner, "capture_adb_screenshot", return_value=False), \
                 patch.object(maa_runner, "wait_interruptible", return_value=False), \
                 patch.object(stage_pilot, "dismiss_network_disconnect_popup", side_effect=_fake_dismiss), \
                 patch.object(stage_pilot, "adb_screencap_image", return_value=modal_img), \
                 patch.object(stage_pilot, "is_game_home_or_online_screen", return_value=still_popup_verify):
                res = maa_runner.run_task_with_recovery(
                    holder,
                    "Infrast",
                    {"enable": True},
                    {"server": "Bilibili"},
                    max_retries=1,
                )

            self.assertFalse(res["ok"])
            self.assertTrue(res.get("network_recovery_failed"))
            self.assertTrue(holder.get("session_lost"))
            # Attempts 1 & 2 clicked confirm; Attempt 3 escalated to force-restart game client -> total exactly 3 attempts!
            self.assertEqual(len(dismiss_calls), 2)
            mock_force_restart.assert_called_once()
            self.assertEqual(res["network_recovery"]["attempts"], 3)
            self.assertTrue(any("[NET][自愈失败]" in ln for ln in logs))

            # Evidence screenshot must be persisted in isolated temp reports/
            ev_path = Path(res["network_recovery"].get("screenshot", ""))
            self.assertTrue(ev_path.is_file())
            self.assertTrue(str(ev_path).startswith(str(base)))
            self.assertEqual(res.get("recovery_screenshot"), str(ev_path))

            # Log tag classification contract
            self.assertEqual(contract.classify_log_tag("[NET][自愈失败] 连续 3 次恢复失败"), "error")
            self.assertEqual(contract.classify_log_tag("[NET][掉线拦截] 命中弹窗"), "alert")

            # Verify daily pipeline stops cleanly after network_recovery_failed without running Mall/Award
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config(
                {"first_run_done": True, "daily": {"skip_tasks": ["Recruit", "Fight", "Depot"]}},
                cfg_path,
            )
            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")

            daily_sess = MagicMock()
            daily_sess.connect.return_value = True
            daily_sess.recruit_alerts = []
            called_daily_tasks = []

            def _fake_run_with_rec(sh, task_type, params, cfg, **kwargs):
                called_daily_tasks.append(task_type)
                if task_type == "Infrast":
                    sh["session_lost"] = True
                    sh["network_recovery_failed"] = True
                    return {
                        "ok": False,
                        "network_recovery_failed": True,
                        "error": "[NET][自愈失败] 连续 3 次网络掉线恢复均失败，已中止任务",
                        "events": [],
                    }
                return {"ok": True, "events": []}

            with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                 patch.object(master_scheduler, "ensure_game_running", return_value="com.hypergryph.arknights.bilibili"), \
                 patch.object(master_scheduler, "init_maa_sdk"), \
                 patch.object(master_scheduler, "MaaSession", return_value=daily_sess), \
                 patch.object(master_scheduler, "run_task_with_recovery", side_effect=_fake_run_with_rec), \
                 patch.object(master_scheduler, "execute_post_action", return_value={"ok": True, "action": "none"}):
                daily_report = eng.run_daily()

            self.assertFalse(daily_report["ok"])
            self.assertTrue(daily_report.get("network_recovery_failed"))
            # Stopped right after Infrast; Mall & Award must NOT be invoked after 3x network recovery failure
            self.assertEqual(called_daily_tasks, ["StartUp", "Infrast"])


class RoguelikeSettlementAndUiMutexTests(unittest.TestCase):
    def test_sarkaz_post_settlement_navigates_lobby_claims_rewards_and_upgrades_tech_tree(self):
        prof = roguelike_ai_copilot.get_theme_ui_profile("Sarkaz")
        self.assertIn("假说辑录", prof["level_reward"]["keywords"])
        self.assertIn("历史重构", prof["tech_tree"]["keywords"])
        self.assertIn("萨卡兹的无终奇语", prof["theme_select_keywords"])

        dummy_img = Image.new("RGB", (1280, 720), (24, 28, 38))
        lit_tree_img = _make_tech_tree_canvas_with_lit_node(620, 340)
        empty_tree_img = Image.new("RGB", (1280, 720), (24, 28, 38))

        # Sequence of screens:
        # 1. Home -> click 终端
        # 2. Terminal -> click 长期探索
        # 3. Long-term Exploration -> click 集成战略
        # 4. Roguelike Hub -> click 萨卡兹的无终奇语
        # 5. Sarkaz Lobby (navigation succeeds!)
        # 6. claim_roguelike_rewards: Sarkaz Lobby -> click 假说辑录
        # 7. Level reward page -> click 一键领取
        # 8. Level reward page -> no more claim buttons
        # 9. _tap_return_to_lobby -> already sees 开始探索
        # 10. Sarkaz Lobby -> click 本月委托栏
        # 11. Commission page -> click 领取
        # 12. Commission page -> no more claim buttons
        # 13. _tap_return_to_lobby -> sees 开始探索
        # 14. auto_upgrade_roguelike_tech_tree: Sarkaz Lobby -> click 历史重构
        # 15. Tech tree canvas (1 lit node) -> tap node -> detail OCR shows 开局希望+2 + 重构
        # 16. Tech tree canvas (no more lit nodes) -> exit -> _tap_return_to_lobby
        screen_images = [
            dummy_img, dummy_img, dummy_img, dummy_img, dummy_img,  # navigation (5)
            dummy_img, dummy_img, dummy_img, dummy_img,             # level reward + return (4)
            dummy_img, dummy_img, dummy_img, dummy_img,             # commission + return (4)
            dummy_img, lit_tree_img, dummy_img, empty_tree_img, dummy_img,  # tech tree (5)
        ]
        ocr_sequence = [
            # Nav 1: Home
            [{"text": "终端", "compact": "终端", "cx": 960, "cy": 210}, {"text": "编队", "compact": "编队", "cx": 820, "cy": 410}, {"text": "基建", "compact": "基建", "cx": 990, "cy": 610}],
            # Nav 2: Terminal
            [{"text": "主线", "compact": "主线", "cx": 200, "cy": 640}, {"text": "长期探索", "compact": "长期探索", "cx": 640, "cy": 640}],
            # Nav 3: Long-term exploration
            [{"text": "集成战略", "compact": "集成战略", "cx": 420, "cy": 360}, {"text": "生息演算", "compact": "生息演算", "cx": 860, "cy": 360}],
            # Nav 4: Roguelike hub
            [{"text": "集成战略", "compact": "集成战略", "cx": 180, "cy": 50}, {"text": "萨卡兹的无终奇语", "compact": "萨卡兹的无终奇语", "cx": 760, "cy": 380}],
            # Nav 5: Sarkaz lobby reached!
            [{"text": "开始探索", "compact": "开始探索", "cx": 960, "cy": 620}, {"text": "假说辑录", "compact": "假说辑录", "cx": 1100, "cy": 72}, {"text": "历史重构", "compact": "历史重构", "cx": 180, "cy": 360}],
            # Claim 1: Sarkaz lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 960, "cy": 620}, {"text": "假说辑录", "compact": "假说辑录", "cx": 1100, "cy": 72}],
            # Claim 1 page: 一键领取
            [{"text": "一键领取", "compact": "一键领取", "cx": 1080, "cy": 640}],
            # Claim 1 page: already claimed
            [{"text": "已全部领取", "compact": "已全部领取", "cx": 1080, "cy": 640}],
            # Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 960, "cy": 620}],
            # Claim 2: Sarkaz lobby monthly commission
            [{"text": "本月委托栏", "compact": "本月委托栏", "cx": 1140, "cy": 265}],
            # Claim 2 page: 领取
            [{"text": "领取", "compact": "领取", "cx": 960, "cy": 320}],
            # Claim 2 page: done
            [{"text": "已领取", "compact": "已领取", "cx": 960, "cy": 320}],
            # Return to lobby
            [{"text": "开始探索", "compact": "开始探索", "cx": 960, "cy": 620}],
            # Tech tree entry on Sarkaz lobby
            [{"text": "历史重构", "compact": "历史重构", "cx": 180, "cy": 360}],
            # Tech tree node detail
            [{"text": "历史重构", "compact": "历史重构", "cx": 160, "cy": 40}, {"text": "开局希望+2 初始费用+4", "compact": "开局希望+2初始费用+4", "cx": 960, "cy": 260}, {"text": "重构", "compact": "重构", "cx": 1020, "cy": 580}],
            # Return to lobby after tech tree
            [{"text": "开始探索", "compact": "开始探索", "cx": 960, "cy": 620}],
        ]

        img_iter = iter(screen_images)
        ocr_iter = iter(ocr_sequence)
        taps = []
        logs = []

        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp)
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=lambda *a, **kw: next(img_iter, dummy_img)), \
                 patch.object(stage_pilot, "detect_network_disconnect_popup", return_value={"detected": False}), \
                 patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=lambda *a, **kw: next(ocr_iter, [])), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, *r, **kw: taps.append((x, y)) or True), \
                 patch("time.sleep"):
                res = roguelike_ai_copilot.run_roguelike_post_settlement(
                    adb_path="adb.exe",
                    adb_addr="127.0.0.1:16384",
                    maa_dir=maa,
                    config={"roguelike": {"theme": "Sarkaz", "auto_claim_rewards": True, "auto_upgrade_tech_tree": True}},
                    theme="Sarkaz",
                    navigate_to_lobby=True,
                    log_callback=logs.append,
                )

        self.assertTrue(res["ok"])
        self.assertTrue(res["navigation"]["ok"])
        self.assertEqual(res["rewards"]["level_rewards_claimed"], 1)
        self.assertEqual(res["rewards"]["commission_rewards_claimed"], 1)
        self.assertEqual(res["tech_tree"]["upgraded_count"], 1)
        # Verify navigation taps hit 终端 -> 长期探索 -> 集成战略 -> 萨卡兹的无终奇语
        self.assertIn((960, 210), taps)
        self.assertIn((640, 640), taps)
        self.assertIn((420, 360), taps)
        self.assertIn((760, 380), taps)
        # Verify reward & tech tree taps hit 假说辑录 & 历史重构
        self.assertIn((1100, 72), taps)
        self.assertIn((180, 360), taps)
        self.assertTrue(any("[RL-POST]" in ln for ln in logs))
        self.assertTrue(any("[RL-REWARD]" in ln for ln in logs))
        self.assertTrue(any("[RL-TECH]" in ln for ln in logs))

    def test_gui_bridge_roguelike_settlement_idle_e2e_and_running_mutex_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            maa_dir = root / "maa"
            (maa_dir / "resource").mkdir(parents=True)
            (maa_dir / "MaaCore.dll").write_bytes(b"fake-dll")

            cfg_path = root / "config" / "launcher_config.json"
            env_doctor.save_launcher_config(
                {
                    "first_run_done": True,
                    "maa_path": str(maa_dir),
                    "roguelike": {
                        "theme": "Sarkaz",
                        "auto_claim_rewards": True,
                        "auto_upgrade_tech_tree": True,
                    },
                },
                cfg_path,
            )

            with patch.object(env_doctor, "get_base_dir", return_value=root), \
                 patch.object(master_scheduler, "get_base_dir", return_value=root):
                engine = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="gui")

            snap_stub = {
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": str(maa_dir),
                "integrity": {"valid": True, "maa_version": "v6.17.5", "reason": "ok"},
                "my_box": {"total_owned": 50, "e2_count": 20},
                "depot": {"syncTime": "2026-11-25", "count": 40},
                "growth_preview": {"ok": True, "recommended_stages": ["1-7"], "reason": "test"},
                "roguelike_resolved": {
                    "theme": "Sarkaz",
                    "mode_label": "Exp",
                    "params": {"theme": "Sarkaz", "mode": 0},
                    "ai_copilot_enabled": False,
                    "auto_claim_rewards": True,
                    "auto_upgrade_tech_tree": True,
                },
                "reclamation_resolved": {
                    "theme": "RelaunchAnchor",
                    "mode_label": "RA1",
                    "params": {"mode": 16, "clear_store": False},
                    "loaded_from_gui_config": True,
                },
                "week_key": "2026-W48",
                "annihilation_process": [1800, 1800],
                "adb_info": {"connected": True, "adb_address": "127.0.0.1:16384", "width": 1280, "height": 720, "is_16_9": True},
            }

            with patch("universal_engine.gui.controller._dispatch_collect_snapshot", return_value=snap_stub):
                ctrl = LauncherController(engine=engine, auto_start_refresh=False)
                api = WebViewJsApi(ctrl)
                ctrl.drain_events()  # clear initial events

                # 1. Idle click when MAA is missing -> rejected with clear toast + terminal log (never silent)
                with patch.object(engine, "ensure_maa_ready", return_value={"ok": False, "reason": "未绑定 MAA 路径"}):
                    rej_maa = api.start_roguelike_post_settlement()
                self.assertFalse(rej_maa["ok"])
                self.assertTrue(rej_maa.get("rejected"))
                self.assertEqual(rej_maa.get("reason"), "maa_not_ready")
                evs_maa = ctrl.drain_events()
                self.assertTrue(any(ev["type"] == "toast" and ev["payload"].get("level") == "warn" for ev in evs_maa))
                self.assertTrue(any("[WARN][前置拦截]" in entry["text"] for entry in ctrl.logs))

                # 2. Idle click when MAA ready -> enters running state immediately, blocks concurrent starts with mutex feedback
                entered_gate = threading.Event()
                release_gate = threading.Event()
                captured_kwargs = {}

                def _fake_post_settlement(*args, **kwargs):
                    captured_kwargs.update(kwargs)
                    entered_gate.set()
                    release_gate.wait(timeout=5.0)
                    return {
                        "ok": True,
                        "theme": kwargs.get("theme", "Sarkaz"),
                        "navigation": {"ok": True, "last_screen": "theme_lobby"},
                        "rewards": {"ok": True, "level_rewards_claimed": 2, "commission_rewards_claimed": 1},
                        "tech_tree": {"ok": True, "upgraded_count": 3},
                        "cancelled": False,
                    }

                with patch.object(ctrl, "_ensure_adb_before_task", return_value={"connected": True, "adb_address": "127.0.0.1:16384", "width": 1280, "height": 720}), \
                     patch.object(ctrl, "_refresh_snapshot_async"), \
                     patch.object(engine, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa_dir)}), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.roguelike_ai_copilot.run_roguelike_post_settlement", side_effect=_fake_post_settlement):
                    try:
                        start_res = api.start_roguelike_post_settlement()
                        self.assertTrue(start_res["ok"])
                        self.assertTrue(start_res.get("started"))
                        self.assertTrue(start_res["task_state"]["running"])
                        self.assertEqual(start_res["task_state"]["task_kind"], "roguelike_post")
                        self.assertTrue(start_res["task_state"]["task_buttons_disabled"])
                        self.assertFalse(start_res["task_state"]["ra_button_enabled"])
                        self.assertTrue(start_res["task_state"]["stop_enabled"])

                        self.assertTrue(entered_gate.wait(timeout=5.0))
                        self.assertEqual(captured_kwargs.get("theme"), "Sarkaz")
                        self.assertTrue(captured_kwargs.get("navigate_to_lobby"))

                        # Concurrent clicks while running MUST be rejected cleanly with clear reason, toast & terminal log
                        for fn_name, call_fn in [
                            ("start_roguelike_post_settlement", api.start_roguelike_post_settlement),
                            ("start_roguelike", api.start_roguelike),
                            ("start_daily", api.start_daily),
                            ("toggle_reclamation", api.toggle_reclamation),
                        ]:
                            rej = call_fn()
                            self.assertFalse(rej["ok"], f"{fn_name} should reject while task running")
                            self.assertTrue(rej.get("rejected"), f"{fn_name} should mark rejected=True")
                            self.assertEqual(rej.get("reason"), "task_running")
                            self.assertIn("集成战略一键领奖与加点", str(rej.get("error", "")))

                        mid_events = ctrl.drain_events()
                        warn_toasts = [ev["payload"] for ev in mid_events if ev["type"] == "toast" and ev["payload"].get("level") == "warn"]
                        self.assertGreaterEqual(len(warn_toasts), 3)
                        self.assertTrue(any("[WARN][互斥拦截]" in entry["text"] for entry in ctrl.logs))
                    finally:
                        release_gate.set()
                        deadline = time.time() + 5.0
                        while ctrl.task_running and time.time() < deadline:
                            time.sleep(0.05)

                self.assertFalse(ctrl.task_running)
                final_state = ctrl.task_state
                self.assertFalse(final_state["running"])
                self.assertFalse(final_state["task_buttons_disabled"])
                self.assertTrue(final_state["ra_button_enabled"])
                self.assertFalse(final_state["stop_enabled"])

                end_events = ctrl.drain_events()
                self.assertTrue(
                    any(ev["type"] == "toast" and ev["payload"].get("level") == "success" for ev in end_events),
                    "Completion toast must be emitted after roguelike post-settlement succeeds",
                )
                self.assertTrue(any("等级奖励=2" in entry["text"] and "科技树加点=3项" in entry["text"] for entry in ctrl.logs))

    def test_frontend_rl_settlement_binding_and_unified_running_disable_audit(self):
        web_dir = REPO_ROOT / "universal_engine" / "gui" / "web"
        html = (web_dir / "index.html").read_text(encoding="utf-8")
        app_js = (web_dir / "js" / "app.js").read_text(encoding="utf-8")
        bridge_js = (web_dir / "js" / "bridge.js").read_text(encoding="utf-8")
        app_css = (web_dir / "styles" / "app.css").read_text(encoding="utf-8")

        expected_mutex_ids = (
            "btn-start-daily",
            "btn-start-annihilation",
            "btn-start-push",
            "btn-start-auto-growth",
            "btn-exec-growth-from-preview",
            "btn-sync-box",
            "btn-start-roguelike",
            "btn-rl-settlement",
        )
        for btn_id in expected_mutex_ids:
            self.assertIn(f'id="{btn_id}"', html, f"Missing button #{btn_id} in index.html")
            self.assertRegex(app_js, rf"""['"]{re.escape(btn_id)}['"]""", f"Missing #{btn_id} in MUTEX_TASK_BUTTON_IDS in app.js")

        # Verify unified disable helper & running-state UI lock
        self.assertIn("MUTEX_TASK_BUTTON_IDS", app_js)
        self.assertIn("function guardTaskNotRunning(", app_js)
        self.assertIn("function handleTaskStartResult(", app_js)
        self.assertIn("btn-rl-settlement", app_js)
        self.assertIn("startRoguelikePostSettlement", app_js)
        self.assertIn("startRoguelikePostSettlement", bridge_js)
        self.assertIn("task_buttons_disabled", app_js)
        self.assertIn(".pill-action.is-disabled", app_css)

        # Verify btn-rl-settlement click block goes through guardTaskNotRunning + handleTaskStartResult
        rl_click_match = re.search(
            r"""\$\(\s*['"]btn-rl-settlement['"]\s*\).*?addEventListener\(\s*['"]click['"].*?guardTaskNotRunning\(.*?startRoguelikePostSettlement\(\).*?handleTaskStartResult\(""",
            app_js,
            re.DOTALL,
        )
        self.assertIsNotNone(rl_click_match, "btn-rl-settlement click handler must invoke guard + startRoguelikePostSettlement + handleTaskStartResult")

        # Enforce zero Unicode emoji (including U+2713 checkmark) in all web assets
        forbidden_emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
        for name, content in (("index.html", html), ("app.js", app_js), ("bridge.js", bridge_js), ("app.css", app_css)):
            m = forbidden_emoji_re.search(content)
            self.assertIsNone(m, f"Forbidden Unicode symbol {repr(m.group(0)) if m else ''} found in {name}")


if __name__ == "__main__":
    unittest.main()
