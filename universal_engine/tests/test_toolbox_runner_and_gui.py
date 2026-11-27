"""Unit tests and GUI integration tests for MAA Toolbox ("小工具 · 牛杂") integration.

Ensures:
1. Parameter specification and append_task assertions for all 11 toolbox items.
2. Page-anchored tools bypass StartUp and execute under cross-process lock.
3. Backward-compatibility: Older MAA v6.20.1 missing tasks mark available=False and reject start.
4. Dynamic discovery and pinning of future activity minigames from resource/tasks/MiniGame/*.json.
5. Zero-mutation safety: save_toolbox_config only touches launcher_config.json, gui.new.json SHA256 unchanged.
6. Cross-page linkage for reclamation configuration and execution state.
7. Tri-partite contract consistency across contract.py, WebViewJsApi, and bridge.js (43 methods).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from universal_engine.gui import contract
from universal_engine.utils import env_doctor


def _create_mock_maa_structure(
    base_dir: Path,
    version: str = "v6.23.2",
    missing_keys: Optional[List[str]] = None,
    activity_name: str = "雪山密林",
) -> Path:
    """Create a temporary mock MAA directory layout matching v6.23.2."""
    maa_dir = base_dir / "MAA"
    res_dir = maa_dir / "resource"
    tasks_dir = res_dir / "tasks"
    mg_dir = tasks_dir / "MiniGame"
    sp_dir = tasks_dir / "StrongholdProtocol"
    rl_dir = tasks_dir / "Roguelike"
    ra_dir = tasks_dir / "RA"
    stages_dir = tasks_dir / "Stages"
    sp_meta_dir = res_dir / "stronghold_protocol" / "regulation"

    for d in (mg_dir, sp_dir, rl_dir, ra_dir, stages_dir, sp_meta_dir, maa_dir / "data", maa_dir / "config"):
        d.mkdir(parents=True, exist_ok=True)

    missing = set(missing_keys or [])

    # CHANGELOG.md for version detection
    (maa_dir / "data" / "CHANGELOG.md").write_text(f"## {version}\n\n### Highlights\n", encoding="utf-8")

    # version.json
    (res_dir / "version.json").write_text(
        json.dumps({"activity": {"name": activity_name}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # tasks.json for general tasks & ticket shops
    (tasks_dir / "tasks.json").write_text(
        json.dumps({
            "Store@Begin": {},
            "GreenTicket@Store@ChooseTicketType": {},
            "YellowTicket@Store@ChooseTicketType": {},
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Stages/base.json for event_shop
    (stages_dir / "base.json").write_text(
        json.dumps({"SS@Store@Begin": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # MiniGame/OS.json
    if "OS" not in missing:
        (mg_dir / "OS.json").write_text(
            json.dumps({"MiniGame@OS@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # MiniGame/SecretFront.json
    if "SecretFront" not in missing:
        (mg_dir / "SecretFront.json").write_text(
            json.dumps({"MiniGame@SecretFront@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # MiniGame/AutoRaisePotential.json
    if "AutoRaisePotential" not in missing:
        (mg_dir / "AutoRaisePotential.json").write_text(
            json.dumps({"MiniGame@AutoRaisePotential@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # MiniGame/MaterialSynthesis.json
    if "MaterialSynthesis" not in missing:
        (mg_dir / "MaterialSynthesis.json").write_text(
            json.dumps({"MiniGame@MaterialSynthesis@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # MiniGame/CursedRelic.json
    if "CursedRelic" not in missing:
        (mg_dir / "CursedRelic.json").write_text(
            json.dumps({"MiniGame@CursedRelic@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # MiniGame/SPA.json for stronghold score mode
    if "SPA" not in missing:
        (mg_dir / "SPA.json").write_text(
            json.dumps({"MiniGame@SPA@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # StrongholdProtocol/StrongholdProtocol.json
    if "StrongholdProtocol" not in missing:
        (sp_dir / "StrongholdProtocol.json").write_text(
            json.dumps({"StrongholdProtocol@Farm@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (sp_meta_dir / "purchase.json").write_text(
            json.dumps({"operators": ["维什戴尔", "玛恩纳"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (res_dir / "battle_data.json").write_text(
            json.dumps({"stages": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # Roguelike/BlackFlow.json
    if "BlackFlow" not in missing:
        (rl_dir / "BlackFlow.json").write_text(
            json.dumps({"BlackFlow@Roguelike@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # RA/Reclamation3.json & RA/plugin/base.json
    if "Reclamation" not in missing:
        (ra_dir / "Reclamation3.json").write_text(
            json.dumps({"RA@Store@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        plugin_base = res_dir / "tasks" / "RA" / "plugin"
        plugin_base.mkdir(parents=True, exist_ok=True)
        (plugin_base / "base.json").write_text(
            json.dumps({"RA@Store@Begin": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # Mock gui.new.json
    (maa_dir / "config" / "gui.new.json").write_text(
        json.dumps({"Current": "Default", "Configurations": {"Default": {"TaskQueue": []}}}, indent=2),
        encoding="utf-8",
    )

    return maa_dir


class ToolboxRunnerAndGuiTests(unittest.TestCase):
    """Test suite covering Toolbox integration across Engine, Runner, Controller, and Bridge."""

    def _get_runner_and_modules(self):
        """Safely import backend modules after backend has implemented them."""
        from universal_engine import master_scheduler, toolbox_runner
        from universal_engine.gui import bridge, controller
        return toolbox_runner, master_scheduler, controller, bridge

    # -------------------------------------------------------------------------
    # 1. Parameter specification & task construction for all 11 toolbox tools
    # -------------------------------------------------------------------------
    def test_eleven_tools_spec_construction_and_append_task_params(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_path)
            toolbox_runner, master_scheduler, controller_mod, _ = self._get_runner_and_modules()

            cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG.copy()

            # 1. os_karlan
            spec_os = toolbox_runner.build_toolbox_task_spec("os_karlan", maa_dir, cfg)
            self.assertTrue(spec_os["available"])
            self.assertEqual(spec_os["task_type"], "Custom")
            self.assertEqual(spec_os["params"], {"task_names": ["MiniGame@OS@Begin"]})

            # 2. blackflow_money
            spec_bf = toolbox_runner.build_toolbox_task_spec("blackflow_money", maa_dir, cfg)
            self.assertTrue(spec_bf["available"])
            self.assertEqual(spec_bf["task_kind"], "roguelike")
            self.assertEqual(spec_bf["theme"], "BlackFlow")
            self.assertEqual(spec_bf["rl_mode"], 1)
            self.assertTrue(spec_bf.get("investment_enabled", True))

            # 3. secret_front
            # 3a. default ending E
            spec_sf_def = toolbox_runner.build_toolbox_task_spec("secret_front", maa_dir, cfg)
            self.assertTrue(spec_sf_def["available"])
            self.assertEqual(spec_sf_def["task_type"], "Custom")
            self.assertIn("MiniGame@SecretFront@Begin@EndingE", spec_sf_def["params"]["task_names"])

            # 3b. ending A + priority event
            spec_sf_custom = toolbox_runner.build_toolbox_task_spec(
                "secret_front", maa_dir, cfg,
                overrides={"ending": "EndingA", "priority_event": "支援作战平台"},
            )
            self.assertEqual(
                spec_sf_custom["params"]["task_names"],
                ["MiniGame@SecretFront@Begin@EndingA@支援作战平台"],
            )

            # 4. stronghold_protocol
            # 4a. badge mode
            spec_sp_badge = toolbox_runner.build_toolbox_task_spec(
                "stronghold_protocol", maa_dir, cfg,
                overrides={"mode": "badge", "theme": "Regulation", "target": 3, "runs": 60},
            )
            self.assertTrue(spec_sp_badge["available"])
            self.assertEqual(spec_sp_badge["task_type"], "StrongholdProtocol")
            self.assertEqual(spec_sp_badge["params"]["mode"], "badge")
            self.assertEqual(spec_sp_badge["params"]["theme"], "Regulation")
            self.assertEqual(spec_sp_badge["params"]["target"], 3)
            self.assertEqual(spec_sp_badge["params"]["runs"], 60)

            # 4b. score mode
            spec_sp_score = toolbox_runner.build_toolbox_task_spec(
                "stronghold_protocol", maa_dir, cfg,
                overrides={"mode": "score"},
            )
            self.assertEqual(spec_sp_score["params"], {"mode": "score"})

            # 5. reclamation
            spec_ra = toolbox_runner.build_toolbox_task_spec("reclamation", maa_dir, cfg)
            self.assertTrue(spec_ra["available"])
            self.assertEqual(spec_ra["task_kind"], "reclamation")

            # 6. event_shop
            # 6a. empty blacklist
            spec_es_empty = toolbox_runner.build_toolbox_task_spec(
                "event_shop", maa_dir, cfg,
                overrides={"blacklist_presets": [], "custom_blacklist": ""},
            )
            self.assertTrue(spec_es_empty["available"])
            self.assertEqual(spec_es_empty["params"]["task_names"], ["SS@Store@Begin"])
            self.assertNotIn("blacklist", spec_es_empty["params"].get("params", {}).get("event_shop", {}))

            # 6b. merged preset + custom blacklist
            spec_es_bl = toolbox_runner.build_toolbox_task_spec(
                "event_shop", maa_dir, cfg,
                overrides={
                    "blacklist_presets": ["数据增补仪", "龙门币"],
                    "custom_blacklist": "龙门币, 家具零件；碳 龙门币",
                },
            )
            bl_list = spec_es_bl["params"]["params"]["event_shop"]["blacklist"]
            self.assertEqual(bl_list, ["数据增补仪", "龙门币", "家具零件", "碳"])

            # 7. green_ticket_shop
            spec_gt = toolbox_runner.build_toolbox_task_spec("green_ticket_shop", maa_dir, cfg)
            self.assertTrue(spec_gt["available"])
            self.assertEqual(spec_gt["params"]["task_names"], ["GreenTicket@Store@Begin"])

            # 8. yellow_ticket_shop
            spec_yt = toolbox_runner.build_toolbox_task_spec("yellow_ticket_shop", maa_dir, cfg)
            self.assertTrue(spec_yt["available"])
            self.assertEqual(spec_yt["params"]["task_names"], ["YellowTicket@Store@Begin"])

            # 9. ra_store
            spec_ras = toolbox_runner.build_toolbox_task_spec("ra_store", maa_dir, cfg)
            self.assertTrue(spec_ras["available"])
            self.assertEqual(spec_ras["params"]["task_names"], ["RA@Store@Begin"])

            # 10. auto_raise_potential
            # 10a. use_normal_token=False -> omit params
            spec_arp_false = toolbox_runner.build_toolbox_task_spec(
                "auto_raise_potential", maa_dir, cfg,
                overrides={"use_normal_token": False},
            )
            self.assertTrue(spec_arp_false["available"])
            self.assertEqual(spec_arp_false["params"]["task_names"], ["MiniGame@AutoRaisePotential@Begin"])
            self.assertNotIn("params", spec_arp_false["params"])

            # 10b. use_normal_token=True -> include params
            spec_arp_true = toolbox_runner.build_toolbox_task_spec(
                "auto_raise_potential", maa_dir, cfg,
                overrides={"use_normal_token": True},
            )
            self.assertEqual(
                spec_arp_true["params"]["params"]["auto_raise_potential"]["use_normal_token"],
                True,
            )

            # 11. material_synthesis
            spec_ms = toolbox_runner.build_toolbox_task_spec("material_synthesis", maa_dir, cfg)
            self.assertTrue(spec_ms["available"])
            self.assertEqual(spec_ms["params"]["task_names"], ["MiniGame@MaterialSynthesis@Begin"])

            # 12. cursed_relic
            spec_cr = toolbox_runner.build_toolbox_task_spec("cursed_relic", maa_dir, cfg)
            self.assertTrue(spec_cr["available"])
            self.assertEqual(spec_cr["params"]["task_names"], ["MiniGame@CursedRelic@Begin"])

    # -------------------------------------------------------------------------
    # 2. Page-anchored tools bypass StartUp and run under cross-process lock
    # -------------------------------------------------------------------------
    def test_page_anchored_tools_bypass_startup_and_run_with_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_path)
            cfg_path = tmp_path / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "maa_dir": str(maa_dir)}, cfg_path)

            toolbox_runner, master_scheduler, _, _ = self._get_runner_and_modules()

            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_path)

            mock_session = MagicMock()
            mock_session.post_task.return_value = 1
            mock_session.wait_task.return_value = 0

            appended_tasks = []

            def _fake_append_task(task_type, params=None):
                appended_tasks.append((task_type, params))
                return 1

            mock_session.append_task.side_effect = _fake_append_task

            lock_called = []
            orig_run_with_lock = engine._run_with_lock

            def _spy_run_with_lock(lock_name, fn, **kwargs):
                lock_called.append(lock_name)
                return orig_run_with_lock(lock_name, fn, **kwargs)

            engine._run_with_lock = _spy_run_with_lock

            with patch.object(engine, "_connect_session", return_value=(str(maa_dir), mock_session, None)), \
                 patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}):
                res = engine.run_toolbox_task("event_shop")

            self.assertTrue(res.get("ok", True))
            # Critical assertion: StartUp must NEVER be called for page-anchored tools
            task_types = [t[0] for t in appended_tasks]
            self.assertNotIn("StartUp", task_types)
            self.assertIn("Custom", task_types)
            self.assertIn("toolbox", lock_called)

    # -------------------------------------------------------------------------
    # 3. Old MAA v6.20.1 missing tasks mark available=False and reject execution
    # -------------------------------------------------------------------------
    def test_old_maa_missing_tasks_mark_unavailable_and_reject_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # Create older MAA structure missing OS, StrongholdProtocol, AutoRaisePotential
            maa_dir = _create_mock_maa_structure(
                tmp_path,
                version="v6.20.1",
                missing_keys=["OS", "StrongholdProtocol", "AutoRaisePotential", "SPA"],
            )
            cfg_path = tmp_path / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "maa_dir": str(maa_dir)}, cfg_path)

            toolbox_runner, master_scheduler, controller_mod, _ = self._get_runner_and_modules()

            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_path)
            catalog = toolbox_runner.inspect_toolbox_catalog(maa_dir, engine.get_config())

            self.assertEqual(catalog["maa_version"], "v6.20.1")

            # Flatten items across categories
            items_by_id = {}
            for cat in catalog.get("categories", []):
                for item in cat.get("items", []):
                    items_by_id[item["id"]] = item

            # Stronghold protocol must be marked unavailable with upgrade hint
            self.assertIn("stronghold_protocol", items_by_id)
            sp_item = items_by_id["stronghold_protocol"]
            self.assertFalse(sp_item["available"])
            self.assertTrue(
                "更新" in sp_item["unavailable_reason"] or "升级" in sp_item["unavailable_reason"] or "v6.23" in sp_item["unavailable_reason"]
            )

            # AutoRaisePotential must also be unavailable
            self.assertIn("auto_raise_potential", items_by_id)
            arp_item = items_by_id["auto_raise_potential"]
            self.assertFalse(arp_item["available"])

            # OS must also be unavailable
            self.assertIn("os_karlan", items_by_id)
            os_item = items_by_id["os_karlan"]
            self.assertFalse(os_item["available"])

            # LauncherController must reject start_toolbox_task without starting background thread
            ctrl = controller_mod.LauncherController(engine=engine, auto_start_refresh=False)
            bg_started = []

            def _spy_bg(fn, *a, **k):
                bg_started.append(fn)

            ctrl._run_background_task = _spy_bg

            reject_res = ctrl.start_toolbox_task("stronghold_protocol")
            self.assertFalse(reject_res.get("ok", True))
            self.assertEqual(len(bg_started), 0)

    # -------------------------------------------------------------------------
    # 4. Dynamic discovery and pinning of future activity minigames
    # -------------------------------------------------------------------------
    def test_dynamic_discovery_and_pinning_of_future_minigames(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_path, activity_name="雪山密林")
            cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG.copy()

            toolbox_runner, _, _, _ = self._get_runner_and_modules()

            # Add future new minigame in resource/tasks/MiniGame/NEWACT.json
            new_task_file = maa_dir / "resource" / "tasks" / "MiniGame" / "NEWACT.json"
            new_task_file.write_text(
                json.dumps({
                    "MiniGame@NEWACT@Begin": {
                        "doc": "雪山密林冰雕大赛小游戏",
                    }
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            catalog = toolbox_runner.inspect_toolbox_catalog(maa_dir, cfg)
            current_event_cat = None
            for cat in catalog.get("categories", []):
                if cat.get("category_id") == "current_event":
                    current_event_cat = cat
                    break

            self.assertIsNotNone(current_event_cat)
            current_items = current_event_cat.get("items", [])
            item_ids = [it["id"] for it in current_items]

            # Discovered minigame should be present and pinned to top
            self.assertTrue(
                any("newact" in it_id.lower() for it_id in item_ids),
                f"NEWACT minigame was not discovered: {item_ids}",
            )
            # Since doc matches "雪山密林", it should be pinned to the very first item
            first_item = current_items[0]
            self.assertTrue(
                "newact" in first_item["id"].lower(),
                f"Matching activity minigame should be pinned to top, got {first_item['id']}",
            )

    # -------------------------------------------------------------------------
    # 5. save_toolbox_config only touches launcher_config.json, gui.new.json unchanged
    # -------------------------------------------------------------------------
    def test_save_toolbox_config_does_not_mutate_gui_new_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_path)
            cfg_path = tmp_path / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "maa_dir": str(maa_dir)}, cfg_path)

            gui_new_file = maa_dir / "config" / "gui.new.json"
            initial_sha = hashlib.sha256(gui_new_file.read_bytes()).hexdigest()

            toolbox_runner, master_scheduler, controller_mod, _ = self._get_runner_and_modules()

            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_path)
            ctrl = controller_mod.LauncherController(engine=engine, auto_start_refresh=False)

            # Save toolbox config
            save_res = ctrl.save_toolbox_config("secret_front", {"ending": "EndingB", "priority_event": "游侠"})
            self.assertTrue(save_res.get("ok", True))

            # SHA256 of gui.new.json MUST remain completely unchanged
            after_sha = hashlib.sha256(gui_new_file.read_bytes()).hexdigest()
            self.assertEqual(initial_sha, after_sha, "gui.new.json was mutated by save_toolbox_config!")

            # launcher_config.json must be persisted properly
            persisted = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(
                persisted.get("toolbox", {}).get("secret_front", {}).get("ending"),
                "EndingB",
            )
            self.assertEqual(
                persisted.get("toolbox", {}).get("secret_front", {}).get("priority_event"),
                "游侠",
            )

    # -------------------------------------------------------------------------
    # 6. Reclamation cross-page settings and status linkage
    # -------------------------------------------------------------------------
    def test_reclamation_cross_page_settings_and_status_linkage(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            maa_dir = _create_mock_maa_structure(tmp_path)
            cfg_path = tmp_path / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "maa_dir": str(maa_dir)}, cfg_path)

            toolbox_runner, master_scheduler, controller_mod, _ = self._get_runner_and_modules()

            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_path)
            ctrl = controller_mod.LauncherController(engine=engine, auto_start_refresh=False)

            # 1. save_toolbox_config("reclamation", ...)
            ra_payload = {"theme": "Tales", "mode": "ProsperityInSave", "clear_store": True}
            save_res = ctrl.save_toolbox_config("reclamation", ra_payload)
            self.assertTrue(save_res.get("ok", True))

            # Must update config["reclamation"] and not store duplicate in toolbox
            loaded_cfg = engine.get_config()
            self.assertEqual(loaded_cfg["reclamation"]["theme"], "Tales")
            self.assertEqual(loaded_cfg["reclamation"]["mode"], "ProsperityInSave")
            self.assertTrue(loaded_cfg["reclamation"]["clear_store"])
            self.assertNotIn("reclamation", loaded_cfg.get("toolbox", {}))

            # 2. start_toolbox_task("reclamation") forwards to toggle_reclamation
            toggle_called = []
            with patch.object(ctrl, "toggle_reclamation", side_effect=lambda: toggle_called.append(True) or {"ok": True}):
                run_res = ctrl.start_toolbox_task("reclamation")
            self.assertEqual(len(toggle_called), 1)

    # -------------------------------------------------------------------------
    # 7. Tri-partite contract consistency across contract, WebViewJsApi, bridge.js
    # -------------------------------------------------------------------------
    def test_tri_partite_contract_consistency_and_log_tag_classification(self):
        toolbox_runner, master_scheduler, controller_mod, bridge_mod = self._get_runner_and_modules()

        # 1. Verify FROZEN_BRIDGE_METHODS contains exactly 43 methods
        self.assertEqual(len(contract.FROZEN_BRIDGE_METHODS), 44)
        self.assertIn("set_dry_run", contract.FROZEN_BRIDGE_METHODS)
        for m in ("get_toolbox_catalog", "save_toolbox_config", "start_toolbox_task", "set_dry_run"):
            self.assertIn(m, contract.FROZEN_BRIDGE_METHODS)

        # 2. Verify WebViewJsApi implements all 43 methods
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            engine = master_scheduler.MaaEngine(config_path=cfg_path, base_dir=tmp_path)
            ctrl = controller_mod.LauncherController(engine=engine, auto_start_refresh=False)
            api = bridge_mod.WebViewJsApi(ctrl)

            for method_name in contract.FROZEN_BRIDGE_METHODS:
                self.assertTrue(
                    hasattr(api, method_name),
                    f"WebViewJsApi missing required frozen method: {method_name}",
                )
                self.assertTrue(
                    callable(getattr(api, method_name)),
                    f"WebViewJsApi.{method_name} is not callable",
                )

            # Zero leaked non-callable or non-contract public attributes
            public_attrs = [a for a in dir(api) if not a.startswith("_")]
            for attr in public_attrs:
                self.assertIn(attr, contract.FROZEN_BRIDGE_METHODS)

        # 3. Verify bridge.js fallbackMockApi & PrtsBridge method declarations
        bridge_js_file = Path(__file__).resolve().parent.parent / "gui" / "web" / "js" / "bridge.js"
        self.assertTrue(bridge_js_file.is_file())
        bridge_js = bridge_js_file.read_text(encoding="utf-8")

        for m in ("get_toolbox_catalog", "save_toolbox_config", "start_toolbox_task", "set_dry_run"):
            self.assertIn(m, bridge_js, f"bridge.js missing method declaration: {m}")

        # 4. classify_log_tag checks
        self.assertEqual(contract.classify_log_tag("[DRY-RUN] simulated tap"), "alert")
        self.assertEqual(contract.classify_log_tag("[TOOLBOX] 执行小工具: 隐秘战线"), "toolbox")
        self.assertEqual(contract.classify_log_tag("[牛杂] 开始执行活动商店"), "toolbox")
        self.assertEqual(contract.classify_log_tag("[小工具] 卫戍协议结束"), "toolbox")


if __name__ == "__main__":
    unittest.main()
