import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from universal_engine import auto_growth, launcher_gui, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor, maa_installer

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _build_zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class NewEventCompatAndUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        maa_runner._PRTS_LEVELS_CACHE["fetched_at"] = 0.0
        maa_runner._PRTS_LEVELS_CACHE["levels"] = []

    def test_resolve_stage_info_online_lookup_prefers_normal_over_hard_and_caches_disk(self):
        levels_payload = {
            "data": [
                {
                    "level_id": "activities/act46side/level_act46side_01",
                    "stage_id": "act46side_01#f#",
                    "cat_one": "活动关卡",
                    "cat_two": "雪山降临1101",
                    "cat_three": "OS-1",
                    "name": "呼号声中",
                },
                {
                    "level_id": "activities/act46side/level_act46side_01",
                    "stage_id": "act46side_01",
                    "cat_one": "活动关卡",
                    "cat_two": "雪山降临1101",
                    "cat_three": "OS-1",
                    "name": "呼号声中",
                },
                {
                    "level_id": "activities/act46side/level_act46side_02",
                    "stage_id": "act46side_02",
                    "cat_one": "活动关卡",
                    "cat_two": "雪山降临1101",
                    "cat_three": "OS-2",
                    "name": "甜蜜的邻人",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            base = Path(tmp) / "engine"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text("[]", encoding="utf-8")

            with patch.object(maa_runner, "_fetch_json", return_value=levels_payload) as mock_fetch:
                info = maa_runner.resolve_stage_info("OS-1", maa, base_dir=base)
                self.assertEqual(info["code"], "OS-1")
                self.assertEqual(info["stage_id"], "act46side_01")
                self.assertEqual(info["stage_id_source"], "prts_online")
                self.assertTrue((base / "cache" / "prts_levels.json").is_file())
                self.assertEqual(mock_fetch.call_count, 1)

                # Clear memory cache -> second lookup hits disk cache with zero network calls
                maa_runner._PRTS_LEVELS_CACHE["fetched_at"] = 0.0
                maa_runner._PRTS_LEVELS_CACHE["levels"] = []
                info2 = maa_runner.resolve_stage_info("OS-2", maa, base_dir=base)
                self.assertEqual(info2["stage_id"], "act46side_02")
                self.assertEqual(mock_fetch.call_count, 1)

    def test_fetch_prts_guides_online_stage_id_and_strict_keyword_fallback(self):
        levels_payload = {
            "data": [
                {
                    "level_id": "activities/act46side/level_act46side_01",
                    "stage_id": "act46side_01",
                    "cat_three": "OS-1",
                    "name": "呼号声中",
                }
            ]
        }
        query_payload = {
            "data": {
                "data": [
                    {
                        "id": 73176,
                        "like": 7738,
                        "dislike": 10,
                        "views": 30479,
                        "content": json.dumps({"stage_name": "act46side_01", "doc": {"title": "OS-1 - 呼号声中"}}),
                    },
                    {
                        "id": 73177,
                        "like": 9999,
                        "dislike": 0,
                        "views": 99999,
                        "content": json.dumps({"stage_name": "act46side_01#f#", "doc": {"title": "OS-1 突袭"}}),
                    },
                ]
            }
        }
        detail_payload = {
            "data": {
                "content": json.dumps({"stage_name": "act46side_01", "opers": [{"name": "棘刺"}], "actions": []})
            }
        }

        def _fake_fetch(url: str, timeout: int = 15):
            if "arknights/level" in url:
                return levels_payload
            if "copilot/query" in url:
                self.assertIn("level_keyword=act46side_01", url)
                return query_payload
            if "copilot/get/73176" in url:
                return detail_payload
            return {"data": {"data": []}}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(maa_runner, "_fetch_json", side_effect=_fake_fetch):
                guides = maa_runner.fetch_prts_guides("OS-1", "OS-1", max_details=3, base_dir=Path(tmp))
        self.assertEqual(len(guides), 1)
        self.assertEqual(guides[0]["id"], 73176)

        # Case 2: Online level table doesn't have the brand-new stage yet -> fallback to document=/keyword= search
        # and strictly filter out OS-10, OS-11, OS-EX-1, BOS-1, range summaries, and #f#.
        maa_runner._PRTS_LEVELS_CACHE["fetched_at"] = 0.0
        maa_runner._PRTS_LEVELS_CACHE["levels"] = []
        fallback_items = {
            "data": {
                "data": [
                    {
                        "id": 1001,
                        "like": 9000,
                        "views": 50000,
                        "content": json.dumps({"stage_name": "act99_10", "doc": {"title": "OS-10 挂机"}}),
                    },
                    {
                        "id": 1002,
                        "like": 8000,
                        "views": 40000,
                        "content": json.dumps({"stage_name": "act99_ex01", "doc": {"title": "OS-EX-1 突袭"}}),
                    },
                    {
                        "id": 1003,
                        "like": 7000,
                        "views": 30000,
                        "content": json.dumps({"stage_name": "act99_01#f#", "doc": {"title": "OS-1 突袭"}}),
                    },
                    {
                        "id": 1004,
                        "like": 6000,
                        "views": 20000,
                        "content": json.dumps({"stage_name": "act99_02", "doc": {"title": "OS-1~OS-10 合集 OS-2"}}),
                    },
                    {
                        "id": 1005,
                        "like": 5000,
                        "views": 10000,
                        "content": json.dumps({"stage_name": "act99_bos01", "doc": {"title": "BOS-1 攻略"}}),
                    },
                    {
                        "id": 2001,
                        "like": 320,
                        "views": 2500,
                        "content": json.dumps({"stage_name": "act99_01", "doc": {"title": "[OS-1] 单核低配"}}),
                    },
                ]
            }
        }

        def _fake_fetch_fb(url: str, timeout: int = 15):
            if "arknights/level" in url:
                return {"data": []}
            if "document=OS-1" in url and "page=1" in url:
                return fallback_items
            if "copilot/get/2001" in url:
                return {"data": {"content": json.dumps({"stage_name": "act99_01", "opers": [{"name": "煌"}], "actions": []})}}
            return {"data": {"data": []}}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(maa_runner, "_fetch_json", side_effect=_fake_fetch_fb):
                guides_fb = maa_runner.fetch_prts_guides("OS-1", "OS-1", max_details=5, base_dir=Path(tmp))
        self.assertEqual([g["id"] for g in guides_fb], [2001])

    def test_autonomous_clear_stage_returns_clear_message_when_prts_has_no_guide(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            base = Path(tmp) / "engine"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text("[]", encoding="utf-8")
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(json.dumps({"all_owned": {}, "e2_operators": []}), encoding="utf-8")

            with patch.object(maa_runner, "_fetch_json", return_value={"data": {"data": []}}):
                ok, summary = maa_runner.autonomous_clear_stage("XX-1", maa_dir=maa, config={}, base_dir=base)
        self.assertFalse(ok)
        self.assertTrue(summary.get("no_prts_guide"))
        self.assertIn("PRTS 作业站暂未收录该新关作业", summary["error"])

    def test_discover_event_from_map_image_synthetic_and_real_os1_screenshot(self):
        # 1. Synthetic map: OS-TR-1 cleared (blue), OS-1 uncleared white card
        canvas = Image.new("RGB", (1280, 720), (35, 38, 46))
        # Blue cleared hexagon left of OS-TR-1
        for px in range(440, 475):
            for py in range(138, 170):
                canvas.putpixel((px, py), (25, 145, 240))
        # White card around OS-1
        for px in range(580, 700):
            for py in range(405, 445):
                canvas.putpixel((px, py), (242, 244, 247))

        ocr_boxes = [
            {"text": "OS-TR-1", "norm": "OS-TR-1", "x": 484, "y": 146, "w": 89, "h": 18, "pass": "tut_inv"},
            {"text": "OS-1", "norm": "OS-1", "x": 611, "y": 416, "w": 58, "h": 20, "pass": "white_card_roi"},
        ]
        with patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(stage_pilot, "find_stage_boxes_via_ocr", return_value=ocr_boxes):
            res = stage_pilot.discover_event_from_map_image(canvas, maa_dir=REPO_ROOT)
        self.assertTrue(res["found"])
        self.assertEqual(res["prefix"], "OS")
        self.assertEqual(res["frontier_stage"], "OS-1")
        self.assertEqual(res["push_sequence"], [f"OS-{i}" for i in range(1, 11)])

        # 2. Synthetic map: OS-ST-1 uncleared frontier
        canvas2 = Image.new("RGB", (1280, 720), (35, 38, 46))
        with patch.object(stage_pilot, "analyze_screen_state", return_value={"on_stage_map": True}), \
             patch.object(
                 stage_pilot,
                 "find_stage_boxes_via_ocr",
                 return_value=[{"text": "OS-ST-1", "norm": "OS-ST-1", "x": 200, "y": 320, "w": 86, "h": 20, "pass": "norm"}],
             ):
            res2 = stage_pilot.discover_event_from_map_image(canvas2, maa_dir=REPO_ROOT)
        self.assertTrue(res2["found"])
        self.assertEqual(res2["prefix"], "OS")
        self.assertEqual(res2["frontier_stage"], "OS-ST-1")
        self.assertEqual(res2["push_sequence"], ["OS-ST-1"] + [f"OS-{i}" for i in range(1, 11)])

        # 3. Real screenshot: reports/push_abort_OS-1_20261122_203321.png
        real_os = REPO_ROOT / "reports" / "push_abort_OS-1_20261122_203321.png"
        if real_os.is_file() and (REPO_ROOT / "resource" / "template").is_dir():
            with Image.open(real_os) as im:
                real_res = stage_pilot.discover_event_from_map_image(im, maa_dir=REPO_ROOT)
            self.assertTrue(real_res["found"])
            self.assertEqual(real_res["prefix"], "OS")
            self.assertEqual(real_res["frontier_stage"], "OS-1")
            self.assertEqual(real_res["push_sequence"], [f"OS-{i}" for i in range(1, 11)])

    def test_run_push_auto_uses_visual_event_fallback_when_local_stages_json_outdated(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text("[]", encoding="utf-8")
            (base / "cache" / "gui").mkdir(parents=True)
            (base / "cache" / "gui" / "StageActivityV2.json").write_text("{}", encoding="utf-8")
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            pushed = []

            def _fake_clear(st, **kwargs):
                pushed.append((st, kwargs.get("next_stage")))
                return True, {"stage": st, "victory": True}

            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path)
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch(
                         "universal_engine.master_scheduler.ensure_emulator_ready",
                         return_value={"ready": True, "adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"},
                     ), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch("universal_engine.master_scheduler.resolve_event_battle_stages_from_prts", return_value=[]), \
                     patch.object(stage_pilot, "discover_next_uncleared_stage_on_map", return_value={"found": False, "on_stage_map": False, "all_cleared": False, "reason": "not_on_stage_map"}), \
                     patch(
                         "universal_engine.master_scheduler.stage_pilot.discover_active_event_from_map",
                         return_value={
                             "found": True,
                             "on_stage_map": True,
                             "prefix": "OS",
                             "visible_nodes": [],
                             "frontier_stage": "OS-1",
                             "frontier_type": "battle",
                             "push_sequence": [f"OS-{i}" for i in range(1, 11)],
                         },
                     ), \
                     patch("universal_engine.master_scheduler.stage_pilot.discover_unlocked_special_prereqs", return_value=[]), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=_fake_clear):
                    res = eng.run_push(["auto"], refresh_cache_flag=False)

            self.assertTrue(res["ok"])
            self.assertEqual([s for s, _ in pushed], [f"OS-{i}" for i in range(1, 11)])
            self.assertEqual(pushed[0], ("OS-1", "OS-2"))

    def test_version_compare_check_update_and_incremental_update_preserves_user_data(self):
        self.assertTrue(maa_installer.is_newer_version("v6.23.0", "v6.20.1"))
        self.assertTrue(maa_installer.is_newer_version("6.23.0", "v6.23.0-beta.1"))
        self.assertFalse(maa_installer.is_newer_version("v6.20.1", "v6.20.1"))
        self.assertFalse(maa_installer.is_newer_version("v6.19.9", "v6.20.1"))

        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "maa"
            (maa / "data").mkdir(parents=True)
            (maa / "resource" / "tasks").mkdir(parents=True)
            (maa / "Python" / "asst").mkdir(parents=True)
            (maa / "config").mkdir(parents=True)
            (maa / "universal_engine" / "config").mkdir(parents=True)

            # Old core files
            (maa / "MaaCore.dll").write_bytes(b"old_dll")
            (maa / "Python" / "asst" / "asst.py").write_text("# old", encoding="utf-8")
            (maa / "resource" / "stages.json").write_text("[]", encoding="utf-8")
            (maa / "resource" / "item_index.json").write_text("{}", encoding="utf-8")
            (maa / "resource" / "recruitment.json").write_text("{}", encoding="utf-8")
            (maa / "resource" / "config.json").write_text("{}", encoding="utf-8")
            (maa / "data" / "CHANGELOG.md").write_text("## v6.20.1\n", encoding="utf-8")

            # Personal / engine files that MUST NOT be overwritten
            (maa / "config" / "gui.new.json").write_text('{"personal":"keep"}', encoding="utf-8")
            (maa / "universal_engine" / "config" / "launcher_config.json").write_text('{"server":"Bilibili"}', encoding="utf-8")
            (maa / "Arknights_AI_Launcher.exe").write_bytes(b"my_launcher_exe")
            (maa / "data" / "OperBoxData.json").write_text('{"own_opers":[1]}', encoding="utf-8")
            (maa / "data" / "DepotData.json").write_text('{"data":{"30012":99}}', encoding="utf-8")

            # Build fake v6.23.0 release zip containing overlapping config/ and user files to ensure protection
            zip_bytes = _build_zip_bytes({
                "MAA-v6.23.0-win-x64/MaaCore.dll": b"new_dll_v623",
                "MAA-v6.23.0-win-x64/Python/asst/asst.py": b"# new",
                "MAA-v6.23.0-win-x64/resource/tasks/t.json": b"{}",
                "MAA-v6.23.0-win-x64/resource/stages.json": b'[{"code":"OS-1","stageId":"act46side_01"}]',
                "MAA-v6.23.0-win-x64/resource/item_index.json": b"{}",
                "MAA-v6.23.0-win-x64/resource/recruitment.json": b"{}",
                "MAA-v6.23.0-win-x64/resource/config.json": b"{}",
                "MAA-v6.23.0-win-x64/data/CHANGELOG.md": b"## v6.23.0\n",
                "MAA-v6.23.0-win-x64/config/gui.new.json": b'{"overwritten":"BAD"}',
                "MAA-v6.23.0-win-x64/universal_engine/config/launcher_config.json": b'{"overwritten":"BAD"}',
                "MAA-v6.23.0-win-x64/Arknights_AI_Launcher.exe": b"BAD_EXE",
                "MAA-v6.23.0-win-x64/data/OperBoxData.json": b"{}",
                "MAA-v6.23.0-win-x64/data/DepotData.json": b"{}",
            })
            rel_info = {
                "ok": True,
                "version": "v6.23.0",
                "asset_name": "MAA-v6.23.0-win-x64.zip",
                "total_size": len(zip_bytes),
                "urls": ["https://mirror.test/MAA-v6.23.0-win-x64.zip"],
            }

            chk = maa_installer.check_maa_update(maa_dir=maa, release_info=rel_info)
            self.assertTrue(chk["ok"])
            self.assertEqual(chk["local_version"], "v6.20.1")
            self.assertEqual(chk["remote_version"], "v6.23.0")
            self.assertTrue(chk["has_update"])

            class _ZipResp:
                status = 200
                headers = {"Content-Type": "application/zip", "Content-Length": str(len(zip_bytes))}

                def __init__(self):
                    self._bio = io.BytesIO(zip_bytes)

                def read(self, n=-1):
                    return self._bio.read(n)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            # Also simulate PermissionError once on MaaCore.dll to verify .old/ move-aside fallback
            real_copy2 = maa_installer.shutil.copy2
            failed_once = {"done": False}

            def _flaky_copy(src, dst, *a, **kw):
                if Path(dst).name == "MaaCore.dll" and not failed_once["done"]:
                    failed_once["done"] = True
                    raise PermissionError("DLL in use")
                return real_copy2(src, dst, *a, **kw)

            with patch("urllib.request.urlopen", return_value=_ZipResp()), \
                 patch.object(maa_installer.shutil, "copy2", side_effect=_flaky_copy):
                upd = maa_installer.update_maa_core(maa_dir=maa, release_info=rel_info)

            self.assertTrue(upd["ok"])
            self.assertEqual(upd["old_version"], "v6.20.1")
            self.assertEqual(upd["new_version"], "v6.23.0")
            self.assertTrue(upd["restart_recommended"])
            self.assertGreaterEqual(upd["skipped_preserved"], 5)
            # Core & resource updated
            self.assertEqual((maa / "MaaCore.dll").read_bytes(), b"new_dll_v623")
            self.assertIn("OS-1", (maa / "resource" / "stages.json").read_text(encoding="utf-8"))
            self.assertEqual(env_doctor.get_maa_version(maa), "v6.23.0")
            # Old DLL safely moved to .old/
            self.assertEqual((maa / ".old" / "MaaCore.dll").read_bytes(), b"old_dll")
            # Personal config, universal_engine, launcher exe, and user data strictly preserved
            self.assertEqual((maa / "config" / "gui.new.json").read_text(encoding="utf-8"), '{"personal":"keep"}')
            self.assertEqual(
                (maa / "universal_engine" / "config" / "launcher_config.json").read_text(encoding="utf-8"),
                '{"server":"Bilibili"}',
            )
            self.assertEqual((maa / "Arknights_AI_Launcher.exe").read_bytes(), b"my_launcher_exe")
            self.assertEqual((maa / "data" / "OperBoxData.json").read_text(encoding="utf-8"), '{"own_opers":[1]}')
            self.assertEqual((maa / "data" / "DepotData.json").read_text(encoding="utf-8"), '{"data":{"30012":99}}')

    def test_gui_check_update_button_layout_and_queue_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            orig_engine = master_scheduler.MaaEngine
            test_eng = orig_engine(config_path=cfg_path)
            snap_stub = {
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": tmp,
                "integrity": {"valid": True, "maa_version": "v6.20.1", "reason": "ok"},
                "my_box": {"total_owned": 0, "e2_count": 0},
                "depot": {"syncTime": "", "count": 0},
                "growth_preview": {"ok": True, "recommended_stages": ["1-7"], "reason": "test"},
                "week_key": "2026-W47",
                "annihilation_process": None,
                "adb_info": {"connected": False, "adb_address": "", "width": None, "height": None, "is_16_9": False},
            }
            LauncherController = getattr(launcher_gui, "LauncherController", None)
            WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
            if LauncherController is None:
                from universal_engine.gui.controller import LauncherController
                from universal_engine.gui.bridge import WebViewJsApi

            with patch.object(launcher_gui, "collect_dashboard_snapshot", return_value=snap_stub):
                controller = LauncherController(engine=test_eng, auto_start_refresh=False)
                api = WebViewJsApi(controller)

                # Check update trigger
                rel_info = {"version": "v6.23.0", "total_size": 1024 * 1024, "urls": ["https://test/a.zip"]}
                with patch.object(test_eng, "check_maa_update", return_value={
                    "ok": True,
                    "has_update": True,
                    "local_version": "v6.20.1",
                    "remote_version": "v6.23.0",
                    "total_size_mb": 1.0,
                    "release_info": rel_info,
                    "error": "",
                }):
                    api.check_maa_update()
                    deadline = time.monotonic() + 2.0
                    check_done = None
                    while time.monotonic() < deadline:
                        evs = controller.drain_events()
                        for e in evs:
                            if e.get("type") == "maa_update_check_done":
                                check_done = e["payload"]
                                break
                        if check_done:
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(check_done)
                    self.assertTrue(check_done["has_update"])
                    self.assertEqual(check_done["remote_version"], "v6.23.0")

                # Confirm update trigger
                with patch.object(test_eng, "update_maa_core", return_value={
                    "ok": True,
                    "old_version": "v6.20.1",
                    "new_version": "v6.23.0",
                    "updated_files": 12,
                    "skipped_preserved": 5,
                    "restart_recommended": False,
                    "error": "",
                }), patch.object(controller, "_refresh_snapshot_async") as mock_refresh:
                    api.confirm_maa_update(rel_info, "v6.20.1", "v6.23.0")
                    deadline = time.monotonic() + 2.0
                    apply_done = None
                    while time.monotonic() < deadline:
                        evs = controller.drain_events()
                        for e in evs:
                            if e.get("type") == "maa_update_apply_done":
                                apply_done = e["payload"]
                                break
                        if apply_done:
                            break
                        time.sleep(0.02)
                    self.assertIsNotNone(apply_done)
                    self.assertTrue(apply_done["ok"])
                    self.assertEqual(apply_done["new_version"], "v6.23.0")
                    mock_refresh.assert_called_once_with(check_adb=False)


if __name__ == "__main__":
    unittest.main()
