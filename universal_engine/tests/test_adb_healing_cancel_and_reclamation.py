import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PIL import Image

from universal_engine import launcher_gui, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class AdbHealingAndCancelTests(unittest.TestCase):
    def test_discover_adb_prefers_emulator_over_system_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sys_adb = root / "platform-tools" / "adb.exe"
            mumu_adb = root / "MuMuPlayer" / "nx_main" / "adb.exe"
            sys_adb.parent.mkdir(parents=True)
            mumu_adb.parent.mkdir(parents=True)
            sys_adb.write_bytes(b"old-adb")
            mumu_adb.write_bytes(b"mumu-adb")

            with patch("shutil.which", return_value=str(sys_adb)), \
                 patch.object(env_doctor, "_discover_running_emulator_adb", return_value=None), \
                 patch.object(env_doctor, "_candidate_adb_paths", return_value=[mumu_adb]):
                found = env_doctor.discover_adb_executable(config={}, maa_dir=None)
            self.assertEqual(Path(found).resolve(), mumu_adb.resolve())

    def test_parse_mumu_manager_info_single_and_multi_instance(self):
        # MuMu NX real-world payload: is_android_started=False yet player_state=start_finished & is_process_started=True
        single_json = json.dumps({
            "index": "0",
            "name": "MuMu安卓设备",
            "is_android_started": False,
            "is_process_started": True,
            "adb_port": 16384,
            "adb_host_ip": "127.0.0.1",
            "pid": 15200,
            "player_state": "start_finished",
        })
        insts = env_doctor.parse_mumu_manager_info(single_json)
        self.assertEqual(len(insts), 1)
        self.assertTrue(insts[0]["process_running"])
        self.assertEqual(insts[0]["adb_addr"], "127.0.0.1:16384")

        multi_json = json.dumps({
            "0": {"index": "0", "is_process_started": False, "adb_port": 16384, "adb_host_ip": "127.0.0.1"},
            "1": {"index": "1", "is_process_started": True, "adb_port": 16416, "adb_host_ip": "127.0.0.1", "player_state": "start_finished"},
        })
        insts2 = env_doctor.parse_mumu_manager_info(multi_json)
        self.assertEqual(len(insts2), 2)
        running = [i for i in insts2 if i["process_running"]]
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["adb_addr"], "127.0.0.1:16416")

    def test_check_adb_resolution_heals_offline_and_rediscovers_mumu_port(self):
        calls = []

        def _fake_adb(cmd, timeout_sec=5.0, cancel_event=None, text=True):
            calls.append(cmd)
            joined = " ".join(cmd)
            if "connect 127.0.0.1:16384" in joined:
                return SimpleNamespace(returncode=0, stdout="already connected to 127.0.0.1:16384", stderr="")
            if "-s 127.0.0.1:16384 shell wm size" in joined:
                return SimpleNamespace(returncode=1, stdout="", stderr="adb.exe: device offline")
            if cmd[1:] == ["devices"]:
                return SimpleNamespace(returncode=0, stdout="List of devices attached\n127.0.0.1:16384\toffline\n", stderr="")
            if "disconnect 127.0.0.1:16384" in joined:
                return SimpleNamespace(returncode=0, stdout="disconnected 127.0.0.1:16384", stderr="")
            if "connect 127.0.0.1:16416" in joined:
                return SimpleNamespace(returncode=0, stdout="connected to 127.0.0.1:16416", stderr="")
            if "-s 127.0.0.1:16416 shell wm size" in joined:
                return SimpleNamespace(returncode=0, stdout="Physical size: 1920x1080\n", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="cannot connect")

        with patch.object(env_doctor, "_run_adb", side_effect=_fake_adb), \
             patch.object(env_doctor, "discover_mumu_manager", return_value="MuMuManager.exe"), \
             patch.object(
                 env_doctor,
                 "query_mumu_instances",
                 return_value=[{"index": "1", "process_running": True, "adb_addr": "127.0.0.1:16416", "player_state": "start_finished"}],
             ):
            res = env_doctor.check_adb_resolution("adb.exe", adb_address="127.0.0.1:16384")

        self.assertTrue(res["connected"])
        self.assertTrue(res["healed"])
        self.assertEqual(res["adb_address"], "127.0.0.1:16416")
        self.assertEqual((res["width"], res["height"]), (1920, 1080))
        self.assertTrue(any("disconnect" in " ".join(c) for c in calls))

    def test_wait_for_emulator_and_adb_ready_immediately_when_screencap_ok(self):
        cfg = {"emulator": {"adb_path": "", "adb_address": "127.0.0.1:16384", "auto_launch": True}}
        t0 = time.monotonic()
        with patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
             patch.object(maa_runner, "_run_cmd", side_effect=[
                 SimpleNamespace(returncode=0, stdout="connected to 127.0.0.1:16384", stderr=""),
                 SimpleNamespace(returncode=0, stdout="device\n", stderr=""),
             ]), \
             patch.object(maa_runner, "verify_adb_screencap", return_value=True) as mock_cap, \
             patch("time.sleep") as mock_sleep:
            info = maa_runner.wait_for_emulator_and_adb(cfg, timeout_sec=120)
        elapsed = time.monotonic() - t0
        self.assertTrue(info["ready"])
        self.assertFalse(info["launched_by_us"])
        self.assertEqual(info["adb_addr"], "127.0.0.1:16384")
        mock_cap.assert_called_once()
        mock_sleep.assert_not_called()
        self.assertLess(elapsed, 1.0)

    def test_wait_for_emulator_and_adb_fails_fast_on_mumu_adbd_hung(self):
        cfg = {"emulator": {"adb_path": "", "adb_address": "127.0.0.1:16384", "auto_launch": True}}
        t0 = time.monotonic()
        with patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
             patch.object(maa_runner, "_adb_device_ready", return_value=False), \
             patch.object(
                 maa_runner,
                 "check_adb_resolution",
                 return_value={
                     "connected": False,
                     "adb_address": "127.0.0.1:16384",
                     "mumu_adbd_hung": True,
                     "warning": "检测到 MuMu 模拟器已运行但内置 adbd 服务假死",
                 },
             ):
            with self.assertRaises(RuntimeError) as ctx:
                maa_runner.wait_for_emulator_and_adb(cfg, timeout_sec=120)
        self.assertIn("假死", str(ctx.exception))
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_run_cmd_cancellable_terminates_blocking_subprocess_within_1s(self):
        cancel = threading.Event()

        def _trigger():
            time.sleep(0.15)
            cancel.set()

        threading.Thread(target=_trigger, daemon=True).start()
        t0 = time.monotonic()
        with self.assertRaises(env_doctor.OperationCancelledError):
            env_doctor.run_cmd_cancellable(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout_sec=10.0,
                cancel_event=cancel,
            )
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)

    def test_wait_for_emulator_and_adb_aborts_within_1s_on_cancel_event(self):
        cfg = {"emulator": {"adb_path": "", "adb_address": "127.0.0.1:16384", "auto_launch": False}}
        cancel = threading.Event()

        def _trigger():
            time.sleep(0.2)
            cancel.set()

        threading.Thread(target=_trigger, daemon=True).start()
        t0 = time.monotonic()
        with patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
             patch.object(maa_runner, "_adb_device_ready", return_value=False), \
             patch.object(
                 maa_runner,
                 "check_adb_resolution",
                 return_value={"connected": False, "adb_address": "127.0.0.1:16384", "mumu_adbd_hung": False, "warning": "not ready"},
             ):
            with self.assertRaises(env_doctor.OperationCancelledError):
                maa_runner.wait_for_emulator_and_adb(cfg, timeout_sec=120, cancel_event=cancel)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)

    def test_run_reclamation_cancel_during_wait_returns_cancelled_within_1s(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"post_action": "shutdown"}, cfg_path)

            cancel = threading.Event()

            def _trigger():
                time.sleep(0.2)
                cancel.set()

            threading.Thread(target=_trigger, daemon=True).start()
            t0 = time.monotonic()
            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler.MaaEngine, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                 patch.object(maa_runner, "discover_adb_executable", return_value="adb.exe"), \
                 patch.object(maa_runner, "_adb_device_ready", return_value=False), \
                 patch.object(
                     maa_runner,
                     "check_adb_resolution",
                     return_value={"connected": False, "adb_address": "127.0.0.1:16384", "mumu_adbd_hung": False, "warning": "offline"},
                 ), \
                 patch("universal_engine.master_scheduler.execute_post_action") as mock_post:
                engine = master_scheduler.MaaEngine(config_path=cfg_path)
                res = engine.run_reclamation(cancel_event=cancel, refresh_cache_flag=False)
            elapsed = time.monotonic() - t0
            self.assertTrue(res.get("cancelled"))
            self.assertFalse(res.get("ok"))
            self.assertLess(elapsed, 1.0)
            mock_post.assert_not_called()

    def test_gui_stop_button_aborts_waiting_reclamation_and_resets_ui_within_1s(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            orig_engine = master_scheduler.MaaEngine
            eng = orig_engine(config_path=cfg_path)
            snap_stub = {
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": str(tmp),
                "integrity": {"valid": True, "maa_version": "v6.22.0", "reason": "ok"},
                "my_box": {"total_owned": 0, "e2_count": 0},
                "depot": {"syncTime": "", "count": 0},
                "growth_preview": {"ok": False, "recommended_stages": ["1-7"], "reason": "test"},
                "week_key": "2026-W47",
                "annihilation_process": None,
                "adb_info": {"connected": False, "adb_address": "", "width": None, "height": None, "is_16_9": False},
            }

            def _blocking_reclamation(**kwargs):
                cev = kwargs.get("cancel_event")
                env_doctor.wait_interruptible(60.0, cancel_event=cev, step=0.05)
                return {"ok": False, "cancelled": True}

            with patch.object(launcher_gui, "collect_dashboard_snapshot", return_value=snap_stub), \
                 patch("universal_engine.utils.maa_installer.resolve_maa_path", return_value=Path(tmp)), \
                 patch("universal_engine.utils.env_doctor.discover_adb_executable", return_value=None), \
                 patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(tmp)}), \
                 patch.object(eng, "probe_or_launch_emulator", return_value={"connected": True, "adb_address": "127.0.0.1:16384"}), \
                 patch.object(eng, "run_reclamation", side_effect=_blocking_reclamation):
                LauncherController = getattr(launcher_gui, "LauncherController", None)
                WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
                if LauncherController is None:
                    from universal_engine.gui.controller import LauncherController
                    from universal_engine.gui.bridge import WebViewJsApi

                controller = LauncherController(engine=eng, auto_start_refresh=False)
                api = WebViewJsApi(controller)

                # Start reclamation task
                api.toggle_reclamation()
                deadline = time.monotonic() + 2.0
                while not controller.task_running and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(controller.task_running)

                t0 = time.monotonic()
                api.toggle_reclamation()
                deadline = time.monotonic() + 2.0
                while controller.task_running and time.monotonic() < deadline:
                    time.sleep(0.02)
                elapsed = time.monotonic() - t0

                self.assertFalse(controller.task_running)
                self.assertLess(elapsed, 1.0)

                events = controller.drain_events()
                task_events = [e["payload"] for e in events if e.get("type") == "task_state"]
                last_state = task_events[-1] if task_events else controller.task_state
                self.assertFalse(last_state.get("running", True))
                self.assertIn("启动生息演算", last_state.get("ra_button_text", ""))
                self.assertFalse(last_state.get("stop_enabled", True))


class ReclamationValidationAndNavigationTests(unittest.TestCase):
    def test_reclamation_params_validation_and_accurate_labels(self):
        # Real MAA task type must be "Reclamation" (never "ReclamationAlgorithm")
        r1 = maa_runner.resolve_reclamation_settings({}, overrides={"theme": "RelaunchAnchor", "mode": "RA1"})
        self.assertEqual(r1["task_type"], "Reclamation")
        self.assertEqual(r1["theme"], "RelaunchAnchor")
        self.assertEqual(r1["params"]["mode"], 16)
        self.assertEqual(r1["mode_name"], "RA1")
        self.assertEqual(r1["stage_code"], "RA-1")
        self.assertEqual(r1["mode_warning"], "")

        r4 = maa_runner.resolve_reclamation_settings({}, overrides={"theme": "重启锚点", "mode": "RA-4"})
        self.assertEqual(r4["theme"], "RelaunchAnchor")
        self.assertEqual(r4["params"]["mode"], 32)
        self.assertEqual(r4["stage_code"], "RA-4")

        r15 = maa_runner.resolve_reclamation_settings({}, overrides={"theme": "RelaunchAnchor", "mode": 48})
        self.assertEqual(r15["params"]["mode"], 48)
        self.assertEqual(r15["stage_code"], "RA-15")

        # Mismatched mode for RelaunchAnchor gracefully normalizes to 16 (RA-1) with warning
        bad = maa_runner.resolve_reclamation_settings({}, overrides={"theme": "RelaunchAnchor", "mode": "ProsperityNoSave"})
        self.assertEqual(bad["params"]["mode"], 16)
        self.assertEqual(bad["stage_code"], "RA-1")
        self.assertTrue(bool(bad["mode_warning"]))

        # Verify GUI labels no longer mislabel RA-1/RA-4/RA-15 as "1日速刷/4日速刷/15日速刷"
        for lbl, _ in launcher_gui.RA_MODE_OPTIONS:
            self.assertNotIn("1日速刷", lbl)
            self.assertNotIn("4日速刷", lbl)
            self.assertNotIn("15日速刷", lbl)
        _, disp1 = launcher_gui._format_ra_mode_display("RelaunchAnchor", 16, "RA1")
        self.assertIn("RA-1", disp1)
        self.assertNotIn("1日速刷", disp1)

    def test_real_maa_reclamation_capabilities_detect_stage_pilot_need(self):
        if not (REPO_ROOT / "resource" / "tasks" / "RA" / "Reclamation3.json").is_file():
            self.skipTest("MAA Reclamation3.json not present")
        caps = maa_runner.inspect_maa_exploration_capabilities(REPO_ROOT)
        self.assertEqual(caps["reclamation_task_type"], "Reclamation")
        ra_caps = caps["reclamation_theme_capabilities"]
        self.assertIn("RelaunchAnchor", ra_caps)
        self.assertEqual(ra_caps["RelaunchAnchor"]["supported_modes"], {"RA1": 16, "RA4": 32, "RA15": 48})
        self.assertFalse(ra_caps["RelaunchAnchor"]["native_home_navigation"])
        self.assertTrue(ra_caps["RelaunchAnchor"]["requires_stage_pilot_entry"])
        self.assertTrue(ra_caps["Tales"]["native_home_navigation"])

    def test_stage_pilot_navigates_home_to_relaunch_anchor_and_skips_when_ready(self):
        dummy_img = Image.new("RGB", (1280, 720), (30, 30, 35))
        taps = []
        ocr_seq = [
            # Step 0: Home screen
            [{"compact": "终端", "cx": 960, "cy": 150}, {"compact": "干员", "cx": 960, "cy": 340}, {"compact": "基建", "cx": 960, "cy": 610}],
            # Step 1: Terminal
            [{"compact": "长期探索", "cx": 960, "cy": 665}],
            # Step 2: Long-term exploration
            [{"compact": "生息演算", "cx": 820, "cy": 485}],
            # Step 3: Reclamation hub
            [{"compact": "重启锚点", "cx": 420, "cy": 320}],
            # Step 4: RelaunchAnchor map with RA-1
            [{"compact": "RA-1", "cx": 635, "cy": 420}],
        ]

        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=ocr_seq), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            res = stage_pilot.navigate_to_reclamation_relaunch_anchor(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_stage="RA-1"
            )
        self.assertTrue(res["ok"])
        self.assertFalse(res["already_in_ra"])
        self.assertEqual(taps, [(960, 150), (960, 665), (820, 485), (420, 320), (635, 420)])

        # Already on 「开启建设」 screen -> returns immediately with zero taps
        taps.clear()
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", return_value=[{"compact": "开启建设", "cx": 1120, "cy": 620}]), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda *a, **kw: taps.append(a) or True):
            res2 = stage_pilot.navigate_to_reclamation_relaunch_anchor(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_stage="RA-1"
            )
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["already_in_ra"])
        self.assertEqual(taps, [])

    def test_stage_pilot_real_ui_flow_and_win_ocr_utf8(self):
        import base64

        dummy_img = Image.new("RGB", (1280, 720), (20, 20, 25))
        # 1. Verify run_win_ocr_on_images sets UTF-8 console output encoding & uses run_cmd_cancellable
        captured_cmds = []

        def _fake_cancellable(cmd, timeout_sec=5.0, cancel_event=None, text=True):
            captured_cmds.append((cmd, cancel_event))
            return subprocess.CompletedProcess(cmd, 0, '{"norm":[]}', "")

        ev = threading.Event()
        with patch.object(stage_pilot, "run_cmd_cancellable", side_effect=_fake_cancellable), \
             patch.object(sys, "platform", "win32"):
            out = stage_pilot.run_win_ocr_on_images([("norm", dummy_img)], timeout=5, cancel_event=ev)
        self.assertEqual(out, {"norm": []})
        self.assertEqual(len(captured_cmds), 1)
        cmd0, ev0 = captured_cmds[0]
        self.assertIs(ev0, ev)
        ps_decoded = base64.b64decode(cmd0[-1]).decode("utf-16le")
        self.assertIn("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8", ps_decoded)

        # 2. Verify full real-UI state sequence: Home (no literal 终端) -> Icon-only Terminal -> Long-Term -> Hub -> Lobby -> Map
        taps = []
        real_seq = [
            [{"compact": "理智/205", "cx": 846, "cy": 273}, {"compact": "角色管理", "cx": 986, "cy": 425}, {"compact": "好友", "cx": 369, "cy": 540}],
            [{"compact": "前往上一次作战", "cx": 1026, "cy": 566}, {"compact": "剿灭战模拟", "cx": 1095, "cy": 142}],
            [{"compact": "集成战略", "cx": 260, "cy": 499}, {"compact": "生息演算", "cx": 826, "cy": 499}, {"compact": "长期探索", "cx": 820, "cy": 670}],
            [{"compact": "重启锚点", "cx": 537, "cy": 282}, {"compact": "添加到终端", "cx": 940, "cy": 632}],
            [{"compact": "重启锚点", "cx": 537, "cy": 282}, {"compact": "添加到终端", "cx": 940, "cy": 632}],
            [{"compact": "ReclamationAlgorithm", "cx": 642, "cy": 197}],
            [{"compact": "罗德岛采购平台", "cx": 139, "cy": 646}, {"compact": "信息记录中心", "cx": 482, "cy": 646}, {"compact": "开始建设", "cx": 1125, "cy": 636}],
            [{"compact": "建设元件", "cx": 257, "cy": 21}, {"compact": "建设装置", "cx": 419, "cy": 21}, {"compact": "统筹系统", "cx": 422, "cy": 678}],
        ]
        with patch.object(stage_pilot, "adb_screencap_image", return_value=dummy_img), \
             patch.object(stage_pilot, "find_text_boxes_via_ocr", side_effect=real_seq), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, cancel_event=None: taps.append((x, y)) or True), \
             patch.object(stage_pilot, "wait_interruptible", return_value=False):
            res = stage_pilot.navigate_to_reclamation_relaunch_anchor(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, target_stage="RA-1"
            )
        self.assertTrue(res["ok"])
        self.assertEqual(res["last_screen"], "relaunch_anchor_map")
        self.assertEqual(
            taps,
            [(940, 185), (822, 670), (826, 499), (537, 282), (1150, 632), (1125, 636)],
        )


if __name__ == "__main__":
    unittest.main()
