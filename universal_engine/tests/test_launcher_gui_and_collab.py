import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from universal_engine import ai_strategist, launcher_gui, maa_runner, master_scheduler
from universal_engine.gui import contract
from universal_engine.master_scheduler import MaaEngine
from universal_engine.utils import env_doctor


class LauncherGuiAndCollabTests(unittest.TestCase):
    def test_config_defaults_and_agent_collab_disables_builtin_client(self):
        cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG
        self.assertIn("first_run_done", cfg)
        self.assertFalse(cfg["first_run_done"])
        self.assertEqual(cfg["ai"]["mode"], "agent_collab")
        self.assertIn("notifications", cfg)

        # Even if api_key exists and enabled=True, mode='agent_collab' must keep built-in client off
        client = ai_strategist.OpenAICompatClient.from_config({
            "ai": {"mode": "agent_collab", "enabled": True, "api_key": "sk-abc"}
        })
        self.assertFalse(client.is_configured())

    def test_get_maa_version_from_changelog(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "MAA-v6.17.5-win-x64"
            (maa / "data").mkdir(parents=True)
            (maa / "data" / "CHANGELOG.md").write_text("## v6.20.1\n\n### Highlights\n", encoding="utf-8")
            self.assertEqual(env_doctor.get_maa_version(maa), "v6.20.1")

    def test_agent_task_write_watch_and_auto_resume_in_autonomous_clear_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource" / "tasks").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-9", "stageId": "act_tl_9", "apCost": 18}]), encoding="utf-8"
            )
            (maa / "resource" / "recruitment.json").write_text("{}", encoding="utf-8")
            (maa / "resource" / "item_index.json").write_text("{}", encoding="utf-8")
            (maa / "resource" / "config.json").write_text("{}", encoding="utf-8")
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({"total_owned": 1, "e2_count": 1, "e2_operators": [{"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}], "all_owned": {"棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}}}),
                encoding="utf-8",
            )

            sample_guides = [{
                "id": 9001,
                "like": 100,
                "views": 2000,
                "content": {
                    "stage_name": "TL-9",
                    "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}],
                    "groups": [],
                    "actions": [{"type": "Deploy", "name": "棘刺", "location": [3, 4], "costs": 18}],
                },
            }]

            banner_events = []

            def _on_collab(payload: dict):
                banner_events.append(payload)
                target_path = Path(payload["target_copilot_path"])

                def _external_agent_edit():
                    time.sleep(0.25)
                    cur = json.loads(target_path.read_text(encoding="utf-8"))
                    cur["actions"][0]["costs"] = 12
                    cur["doc"] = {"details": "fixed_by_external_agent"}
                    target_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")

                threading.Thread(target=_external_agent_edit, daemon=True).start()

            round_reports = [
                (1, {"round": 1, "victory": False, "metrics": {"kills": 38, "costs": 18, "last_3_actions": [], "errors": ["Failed"], "failed_flag": True}, "log_tail": ["err"], "screenshot": "fail.png", "report_path": "r1.json"}),
                (0, {"round": 2, "victory": True, "metrics": {"kills": 50, "costs": 25, "last_3_actions": [], "errors": [], "failed_flag": False}, "log_tail": [], "screenshot": "win.png", "report_path": "r2.json"}),
            ]

            with patch.object(maa_runner, "fetch_prts_guides", return_value=sample_guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=round_reports):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-9",
                    maa_dir=maa,
                    config={"ai": {"mode": "agent_collab", "enabled": False}},
                    max_rounds=3,
                    base_dir=base,
                    collab_handler=_on_collab,
                    agent_wait_timeout_sec=5.0,
                )

            self.assertTrue(ok)
            self.assertEqual(summary["rounds_used"], 2)
            self.assertEqual(len(banner_events), 1)
            self.assertEqual(banner_events[0]["battle_state"]["kills"], 38)
            task_json = json.loads((base / "agent_task.json").read_text(encoding="utf-8"))
            self.assertEqual(task_json["status"], "applied")
            self.assertEqual(task_json["battle_state"]["kills"], 38)
            r2_saved = json.loads(Path(summary["copilot_file"]).read_text(encoding="utf-8"))
            self.assertEqual(r2_saved["actions"][0]["costs"], 12)

    def test_prompt_builders_and_webhook_payload(self):
        base = Path(r"D:\test\universal_engine")
        p1 = launcher_gui.build_init_agent_prompt(base, Path(r"D:\test\MAA"))
        self.assertIn("launcher_config.json", p1)
        self.assertIn("README.md", p1)

        p2 = launcher_gui.build_repair_agent_prompt(
            base,
            {"stage_code": "TL-9", "trigger_reason": "battle_failed", "battle_state": {"kills": 41}, "target_copilot_path": r"D:\test\push_TL-9_r2.json"},
        )
        self.assertIn("agent_task.json", p2)
        self.assertIn("41", p2)
        self.assertIn("push_TL-9_r2.json", p2)

        captured = {}

        class _Resp:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=8):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            res = maa_runner.send_webhook_notification(
                {"notifications": {"webhook_enabled": True, "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xyz"}},
                "测试标题",
                "测试正文",
            )
        self.assertTrue(res["sent"])
        self.assertEqual(captured["body"]["msg_type"], "text")

    def test_gui_renders_light_mode_wizard_agent_dialog_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "ai": {"mode": "agent_collab", "enabled": False}}, cfg_path)

            orig_engine = master_scheduler.MaaEngine
            eng = orig_engine(config_path=cfg_path)
            LauncherController = getattr(launcher_gui, "LauncherController", None)
            WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
            if LauncherController is None:
                from universal_engine.gui.controller import LauncherController
                from universal_engine.gui.bridge import WebViewJsApi

            snap_stub = {
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": "",
                "integrity": {"valid": False, "maa_version": "", "reason": "test"},
                "my_box": {"total_owned": 0, "e2_count": 0},
                "depot": {"syncTime": "", "count": 0},
                "growth_preview": {"ok": False, "recommended_stages": ["1-7"], "reason": "test"},
                "week_key": "2026-W47",
                "annihilation_process": None,
                "adb_info": {"connected": False, "adb_address": "", "width": None, "height": None, "is_16_9": False},
            }

            with patch.object(launcher_gui, "collect_dashboard_snapshot", return_value=snap_stub):
                controller = LauncherController(engine=eng, auto_start_refresh=False)
                api = WebViewJsApi(controller)

                # 1. Test get_initial_state
                init_state = api.get_initial_state()
                self.assertIn("snapshot", init_state)
                self.assertIn("options", init_state)
                self.assertIn("task_state", init_state)
                self.assertEqual(init_state["snapshot"]["ai"]["mode"], "agent_collab")
                self.assertIn("桌面 Agent 协同", init_state["snapshot"]["ai"]["label"])
                self.assertEqual(init_state["options"]["bridge_version"], contract.BRIDGE_API_VERSION)

                # 2. Test agent banner trigger
                controller._show_agent_banner({
                    "stage_code": "TL-9",
                    "round": 1,
                    "trigger_reason": "battle_failed",
                    "battle_state": {"kills": 42},
                    "target_copilot_path": str(Path(tmp) / "copilots" / "push_TL-9_r2.json"),
                })
                self.assertTrue(controller.agent_banner_state.get("visible", False))
                self.assertEqual(controller.agent_banner_state.get("task_info", {}).get("stage_code"), "TL-9")
                evs = controller.drain_events()
                banner_evs = [e["payload"] for e in evs if e.get("type") == "agent_banner"]
                self.assertTrue(len(banner_evs) >= 1)
                self.assertTrue(banner_evs[-1].get("visible", False))

                # 3. Test agent prompts and dialog data
                prompts = api.get_agent_prompts()
                self.assertTrue(prompts.get("ok", True))
                self.assertIn("launcher_config.json", prompts["init_prompt"])
                self.assertIn("README.md", prompts["init_prompt"])

                # 4. Test settings retrieval & save
                st = api.get_settings()
                self.assertIn("target_operators", st)
                self.assertIn("medicine", st)
                save_payload = dict(st)
                save_payload["target_operators"] = "棘刺, 维什戴尔"
                save_payload["medicine"] = 5
                with patch.object(controller, "_refresh_snapshot_async"):
                    saved_res = api.save_settings(save_payload)
                self.assertTrue(saved_res.get("ok", True))
                loaded_cfg = eng.get_config()
                self.assertEqual(loaded_cfg["growth"]["target_operators"], ["棘刺", "维什戴尔"])
                self.assertEqual(loaded_cfg["daily"]["medicine"], 5)

                # 5. Test onboarding wizard bridge methods
                scan_res = api.wizard_scan_maa()
                self.assertTrue(scan_res.get("ok", True))

                with patch.object(eng, "check_environment", return_value={"write_permission": {}, "maa_integrity": {}, "adb_resolution": {}, "base_dir": str(tmp)}):
                    diag_res = api.wizard_run_diagnostics("auto", "")
                    self.assertTrue(diag_res.get("ok", True))
                    deadline = time.monotonic() + 1.0
                    while time.monotonic() < deadline:
                        evs = controller.drain_events()
                        if any(e.get("type") == "wizard_diag_done" for e in evs):
                            break
                        time.sleep(0.02)

                with patch.object(controller, "_refresh_snapshot_async"):
                    finish_res = api.wizard_finish({"first_run_done": True, "server": "Official"})
                self.assertTrue(finish_res.get("ok", True))

    def test_roguelike_and_reclamation_gui_cards_settings_sync_and_log_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "config" / "launcher_config.json"
            maa_dir = root / "maa"
            (maa_dir / "config").mkdir(parents=True)
            (maa_dir / "config" / "gui.new.json").write_text(
                json.dumps(
                    {
                        "Current": "Default",
                        "Configurations": {
                            "Default": {
                                "TaskQueue": [
                                    {
                                        "TaskType": "Roguelike",
                                        "Theme": "Sarkaz",
                                        "Mode": "Investment",
                                        "Squad": "蓝图测绘分队",
                                        "Roles": "稳扎稳打",
                                        "CoreChar": "维什戴尔",
                                        "StartCount": 12,
                                    },
                                    {
                                        "TaskType": "Reclamation",
                                        "Theme": "Tales",
                                        "Mode": "ProsperityInSave",
                                        "ToolToCraft": "荧光棒, 便携式种植槽",
                                        "ClearStore": True,
                                    },
                                ]
                            }
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            orig_engine = master_scheduler.MaaEngine
            test_engine = orig_engine(config_path=cfg_path)

            # Verify engine standalone sync & post-settlement helper
            sync_res = test_engine.sync_exploration_from_maa_gui(maa_dir=maa_dir, save=False)
            self.assertTrue(sync_res["ok"])
            self.assertEqual(sync_res["roguelike"]["theme"], "Sarkaz")
            self.assertEqual(sync_res["roguelike"]["mode"], "Investment")
            self.assertEqual(sync_res["reclamation"]["theme"], "Tales")
            self.assertTrue(sync_res["reclamation"]["clear_store"])

            with patch.object(test_engine, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa_dir)}), \
                 patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                 patch(
                     "universal_engine.roguelike_ai_copilot.run_roguelike_post_settlement",
                     return_value={
                         "theme": "BlackFlow",
                         "rewards": {"ok": True, "level_rewards_claimed": 2, "commission_rewards_claimed": 1},
                         "tech_tree": {"ok": True, "upgraded_count": 3},
                         "cancelled": False,
                     },
                 ):
                post_res = test_engine.run_roguelike_post_settlement(theme="BlackFlow")
                self.assertTrue(post_res["ok"])
                self.assertEqual(post_res["rewards"]["level_rewards_claimed"], 2)
                self.assertEqual(post_res["tech_tree"]["upgraded_count"], 3)

            snap_stub = {
                "config": env_doctor.load_launcher_config(cfg_path),
                "maa_dir": str(maa_dir),
                "integrity": {"valid": True, "maa_version": "v6.22.0", "reason": "ok"},
                "my_box": {"total_owned": 10, "e2_count": 5},
                "depot": {"syncTime": "2026-11-19", "count": 40},
                "growth_preview": {"ok": True, "recommended_stages": ["1-7"], "reason": "test"},
                "roguelike_resolved": {
                    "theme": "BlackFlow",
                    "mode_label": "Exp",
                    "params": {"mode": 0},
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
                "week_key": "2026-W47",
                "annihilation_process": [1800, 1800],
                "adb_info": {"connected": True, "adb_address": "127.0.0.1:16384", "width": 1280, "height": 720, "is_16_9": True},
            }

            LauncherController = getattr(launcher_gui, "LauncherController", None)
            WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
            if LauncherController is None:
                from universal_engine.gui.controller import LauncherController
                from universal_engine.gui.bridge import WebViewJsApi

            with patch.object(launcher_gui, "collect_dashboard_snapshot", return_value=snap_stub):
                controller = LauncherController(engine=test_engine, auto_start_refresh=False)
                api = WebViewJsApi(controller)
                controller._apply_snapshot(snap_stub)

                # Verify Roguelike mode toggle and persistence
                api.set_roguelike_mode(contract.RL_CARD_MODE_AI)
                self.assertTrue(test_engine.get_config()["roguelike"]["ai_copilot_enabled"])
                api.set_roguelike_mode(contract.RL_CARD_MODE_PURE)
                self.assertFalse(test_engine.get_config()["roguelike"]["ai_copilot_enabled"])

                # Verify Reclamation card mode display & start/stop toggle state
                controller._set_running_state(True, "运行生息演算", task_kind="reclamation")
                self.assertIn("停止生息演算", controller.task_state["ra_button_text"])
                controller._set_running_state(False, "空闲")
                self.assertIn("启动生息演算", controller.task_state["ra_button_text"])

                # Verify Roguelike decision banner & prompt builder
                rl_payload = {
                    "task_type": "roguelike_decision",
                    "theme": "BlackFlow",
                    "decision_type": "recruitment",
                    "timeout_sec": 45,
                    "auto_fallback_on_timeout": True,
                    "response_file": str(root / "agent_decision.json"),
                }
                controller._show_agent_banner(rl_payload)
                self.assertTrue(controller.agent_banner_state.get("visible", False))
                evs = controller.drain_events()
                banner_evs = [e["payload"] for e in evs if e.get("type") == "agent_banner"]
                self.assertTrue(len(banner_evs) >= 1)
                self.assertEqual(banner_evs[-1].get("task_type"), "roguelike_decision")
                rl_prompt = launcher_gui.build_repair_agent_prompt(root, rl_payload)
                self.assertIn("agent_decision.json", rl_prompt)
                self.assertIn("recruitment", rl_prompt)

                # Verify [肉鸽AI] and [生息演算] and [教学关交接] log tags
                controller._append_log("[肉鸽AI] 选人决策完成：维什戴尔")
                controller._append_log("[生息演算] 完成第 1 轮 RA1 周期")
                controller._append_log("[教学关交接] 当前遇到无法跳过的教学关卡")
                evs_log = controller.drain_events()
                log_batches = [e["payload"] for e in evs_log if e.get("type") == "log_batch"]
                tags = [item.get("tag") for b in log_batches for item in b]
                self.assertIn("roguelike", tags)
                self.assertIn("reclamation", tags)
                self.assertIn("tutorial", tags)

                # Verify Settings exploration sync & save
                sync_out = api.sync_exploration_from_maa_gui(str(maa_dir))
                self.assertTrue(sync_out["ok"])
                self.assertEqual(sync_out["roguelike"]["theme"], "Sarkaz")
                self.assertEqual(sync_out["roguelike"]["mode"], "Investment")
                self.assertEqual(sync_out["reclamation"]["theme"], "Tales")
                self.assertEqual(sync_out["reclamation"]["mode"], "ProsperityInSave")
                self.assertTrue(sync_out["reclamation"]["clear_store"])

                # Save exploration settings
                settings_payload = api.get_settings()
                save_expl = dict(settings_payload)
                save_expl["rl_timeout_fallback"] = False
                save_expl["rl_timeout_sec"] = 60.0
                with patch.object(controller, "_refresh_snapshot_async"):
                    save_res = api.save_settings(save_expl)
                self.assertTrue(save_res.get("ok", True))
                saved = test_engine.get_config()
                self.assertEqual(saved["roguelike"]["theme"], "Sarkaz")
                self.assertEqual(saved["roguelike"]["mode"], "Investment")
                self.assertFalse(saved["roguelike"]["ai_auto_fallback_on_timeout"])
                self.assertEqual(saved["roguelike"]["ai_decision_timeout_sec"], 60.0)
                self.assertEqual(saved["reclamation"]["theme"], "Tales")
                self.assertEqual(saved["reclamation"]["mode"], "ProsperityInSave")
                self.assertEqual(saved["reclamation"]["tools_to_craft"], ["荧光棒", "便携式种植槽"])
                self.assertTrue(saved["reclamation"]["clear_store"])

    def test_frozen_bridge_contract_coverage_and_introspection_safety(self):
        LauncherController = getattr(launcher_gui, "LauncherController", None)
        WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
        if LauncherController is None:
            from universal_engine.gui.controller import LauncherController
            from universal_engine.gui.bridge import WebViewJsApi

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            eng = master_scheduler.MaaEngine(config_path=cfg_path)
            controller = LauncherController(engine=eng, auto_start_refresh=False)
            api = WebViewJsApi(controller)

            # 1. Every frozen bridge method exists on WebViewJsApi and is callable
            for method_name in contract.FROZEN_BRIDGE_METHODS:
                self.assertTrue(
                    hasattr(api, method_name),
                    f"WebViewJsApi is missing required frozen method: {method_name}",
                )
                self.assertTrue(
                    callable(getattr(api, method_name)),
                    f"WebViewJsApi.{method_name} is not callable",
                )

            # 2. Safety check: No unprefixed internal non-callable attributes leaked on WebViewJsApi
            public_attrs = [attr for attr in dir(api) if not attr.startswith("_")]
            for attr in public_attrs:
                self.assertIn(
                    attr,
                    contract.FROZEN_BRIDGE_METHODS,
                    f"WebViewJsApi leaks unexpected public attribute: {attr}",
                )
                self.assertTrue(
                    callable(getattr(api, attr)),
                    f"WebViewJsApi exposes non-callable public attribute: {attr}",
                )

    def test_frontend_static_assets_audit(self):
        web_dir = Path(__file__).resolve().parent.parent / "gui" / "web"
        expected_files = [
            web_dir / "index.html",
            web_dir / "styles" / "tokens.css",
            web_dir / "styles" / "app.css",
            web_dir / "js" / "bridge.js",
            web_dir / "js" / "app.js",
        ]
        for f in expected_files:
            self.assertTrue(f.is_file(), f"Missing required frontend asset: {f}")

        # Emoji regex matching Unicode emoji pictographs and symbols
        emoji_pattern = re.compile(
            r"[\U0001F000-\U0001FAFF]|[\U0001F600-\U0001F64F]|[\U0001F300-\U0001F5FF]|"
            r"[\U0001F680-\U0001F6FF]|[\U0001F1E0-\U0001F1FF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]"
        )
        url_pattern = re.compile(r'https?://[a-zA-Z0-9\.\-\_\/]+')

        # Check each file for zero external CDN links and zero Emoji
        for f in expected_files:
            content = f.read_text(encoding="utf-8")
            emojis_found = emoji_pattern.findall(content)
            self.assertEqual(
                emojis_found,
                [],
                f"File {f.name} contains forbidden Unicode Emoji characters: {emojis_found}",
            )
            # In HTML and CSS files, no external http:// or https:// links
            if f.suffix in (".html", ".css"):
                external_urls = [u for u in url_pattern.findall(content) if not u.startswith("http://127.0.0.1") and not u.startswith("http://localhost")]
                self.assertEqual(
                    external_urls,
                    [],
                    f"File {f.name} contains forbidden external CDN/network links: {external_urls}",
                )

        # index.html must have inline SVG symbols
        index_html = (web_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("<svg", index_html)
        self.assertIn("<symbol", index_html)

        # CSS must have strictly non-overshoot M3 cubic-bezier, prefers-reduced-motion, and Material You tokens
        tokens_css = (web_dir / "styles" / "tokens.css").read_text(encoding="utf-8")
        app_css = (web_dir / "styles" / "app.css").read_text(encoding="utf-8")
        css_content = app_css + tokens_css
        self.assertIn("cubic-bezier(0.2, 0, 0, 1)", css_content)
        self.assertIn("prefers-reduced-motion", css_content)

        # Ban overshoot cubic-bezier (y1 or y2 outside [0, 1]) and 9999px radius permanently
        for match in re.finditer(r"cubic-bezier\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)", css_content):
            y1 = float(match.group(2))
            y2 = float(match.group(4))
            self.assertTrue(0.0 <= y1 <= 1.0 and 0.0 <= y2 <= 1.0, f"Forbidden overshoot bezier curve detected: {match.group(0)}")
        self.assertNotIn("9999px", css_content, "9999px radius is banned to prevent radius animation distortion")

        # Ban inline display:none on tab/wizard panes in index.html
        pane_tags = re.findall(r'<div[^>]+class="[^"]*(?:settings-tab-pane|agent-tab-pane|wizard-step-pane)[^"]*"[^>]*>', index_html)
        self.assertGreaterEqual(len(pane_tags), 10)
        for tag in pane_tags:
            self.assertNotRegex(tag, r"display\s*:\s*none", f"Forbidden inline display:none on pane tag: {tag}")

        # Verify 4 Material You palettes, dark mode, and core M3 roles exist
        for token in (
            'data-palette="sage"',
            'data-palette="lavender"',
            'data-palette="sand"',
            'data-theme="dark"',
            "--md-sys-color-surface-container-high",
            "--md-sys-color-secondary-container",
        ):
            self.assertIn(token, tokens_css, f"Missing required Material You token/selector: {token}")

    def test_settings_five_tabs_full_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            eng = master_scheduler.MaaEngine(config_path=cfg_path)
            LauncherController = getattr(launcher_gui, "LauncherController", None)
            WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
            if LauncherController is None:
                from universal_engine.gui.controller import LauncherController
                from universal_engine.gui.bridge import WebViewJsApi

            controller = LauncherController(engine=eng, auto_start_refresh=False)
            api = WebViewJsApi(controller)

            payload_5tabs = {
                "daily": {
                    "fight_mode": "MANUAL",
                    "override_stage": "14-20",
                    "medicine": 3,
                    "stone": 1,
                    "max_push_rounds": 8,
                    "auto_push": True,
                    "recruit_preserve_tags": ["高级资深干员", "支援机械"],
                    "recruit_times": 6,
                    "penguin_id": "123456",
                    "report_to_penguin": True,
                    "report_to_yituliu": True,
                    "skip_tasks": ["mall", "award"],
                },
                "growth": {
                    "prefer_event": False,
                    "allow_chip_stages": False,
                    "allow_resource_stages": False,
                    "target_operators": ["维什戴尔", "玛恩纳"],
                    "material_safety_stock": {"T3_DEFAULT": 45},
                },
                "roguelike": {
                    "use_gui_config": False,
                    "theme": "Sarkaz",
                    "mode": "Investment",
                    "squad": "蓝图测绘分队",
                    "roles": "稳扎稳打",
                    "core_char": "维什戴尔",
                    "starts_count": 10,
                    "ai_copilot_enabled": True,
                    "ai_auto_fallback_on_timeout": False,
                    "ai_decision_timeout_sec": 75.0,
                    "boss_prts_assist": False,
                    "auto_claim_rewards": False,
                    "auto_upgrade_tech_tree": False,
                },
                "reclamation": {
                    "use_gui_config": False,
                    "theme": "Tales",
                    "mode": "ProsperityNoSave",
                    "tools_to_craft": ["荧光棒"],
                    "clear_store": True,
                },
                "ai": {
                    "mode": "builtin_api",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "sk-test-123",
                    "model_name": "deepseek-chat",
                    "timeout_sec": 45,
                },
                "emulator": {
                    "adb_address": "127.0.0.1:16416",
                },
                "notifications": {
                    "webhook_enabled": True,
                    "webhook_url": "https://example.invalid/hook",
                    "notify_on_daily_finish": False,
                    "notify_on_recruit_alert": False,
                    "notify_on_agent_task": False,
                },
            }

            with patch.object(controller, "_refresh_snapshot_async"):
                res = api.save_settings(payload_5tabs)
            self.assertTrue(res.get("ok", True))

            # Reload via a brand new MaaEngine + LauncherController + api2.get_settings() + api2.get_initial_state()["snapshot"]
            eng2 = master_scheduler.MaaEngine(config_path=cfg_path)
            controller2 = LauncherController(engine=eng2, auto_start_refresh=False)
            api2 = WebViewJsApi(controller2)

            st2 = api2.get_settings()
            init_state2 = api2.get_initial_state()
            snapshot = init_state2["snapshot"]

            # Tab 1: daily + growth
            self.assertEqual(st2["fight_mode"], "MANUAL")
            self.assertEqual(st2["override_stage"], "14-20")
            self.assertEqual(st2["medicine"], 3)
            self.assertEqual(st2["stone"], 1)
            self.assertEqual(st2["max_push_rounds"], 8)
            self.assertTrue(st2["auto_push"])
            self.assertFalse(st2["prefer_event"])
            self.assertFalse(st2["allow_chip_stages"])
            self.assertFalse(st2["allow_resource_stages"])
            if isinstance(st2["target_operators"], list):
                self.assertEqual(st2["target_operators"], ["维什戴尔", "玛恩纳"])
            else:
                self.assertIn("维什戴尔", st2["target_operators"])
                self.assertIn("玛恩纳", st2["target_operators"])
            self.assertEqual(st2["t3_stock"], 45)

            self.assertEqual(st2["daily"]["fight_mode"], "MANUAL")
            self.assertEqual(st2["daily"]["override_stage"], "14-20")
            self.assertEqual(st2["daily"]["medicine"], 3)
            self.assertEqual(st2["daily"]["stone"], 1)
            self.assertEqual(st2["daily"]["max_push_rounds"], 8)
            self.assertTrue(st2["daily"]["auto_push"])
            self.assertFalse(st2["growth"]["prefer_event"])
            self.assertFalse(st2["growth"]["allow_chip_stages"])
            self.assertFalse(st2["growth"]["allow_resource_stages"])
            self.assertEqual(st2["growth"]["target_operators"], ["维什戴尔", "玛恩纳"])
            self.assertEqual(st2["growth"]["material_safety_stock"]["T3_DEFAULT"], 45)

            # Tab 2: recruit & daily tasks
            self.assertEqual(st2["recruit_preserve_tags"], ["高级资深干员", "支援机械"])
            self.assertEqual(st2["recruit_times"], 6)
            self.assertEqual(st2["penguin_id"], "123456")
            self.assertTrue(st2["report_to_penguin"])
            self.assertTrue(st2["report_to_yituliu"])
            self.assertEqual(st2["skip_tasks"], ["mall", "award"])

            self.assertEqual(st2["daily"]["recruit_preserve_tags"], ["高级资深干员", "支援机械"])
            self.assertEqual(st2["daily"]["recruit_times"], 6)
            self.assertEqual(st2["daily"]["penguin_id"], "123456")
            self.assertTrue(st2["daily"]["report_to_penguin"])
            self.assertTrue(st2["daily"]["report_to_yituliu"])
            self.assertEqual(st2["daily"]["skip_tasks"], ["mall", "award"])

            # Tab 3: roguelike + reclamation
            self.assertFalse(st2["use_gui_config"])
            self.assertEqual(st2["rl_theme"], "Sarkaz")
            self.assertEqual(st2["rl_mode"], "Investment")
            self.assertEqual(st2["rl_squad"], "蓝图测绘分队")
            self.assertEqual(st2["rl_roles"], "稳扎稳打")
            self.assertEqual(st2["rl_core_char"], "维什戴尔")
            self.assertEqual(st2["rl_starts_count"], 10)
            self.assertTrue(st2["rl_ai_copilot"])
            self.assertFalse(st2["rl_timeout_fallback"])
            self.assertEqual(st2["rl_timeout_sec"], 75.0)
            self.assertFalse(st2["rl_boss_prts"])
            self.assertFalse(st2["rl_claim_rewards"])
            self.assertFalse(st2["rl_upgrade_tech"])

            self.assertFalse(st2["roguelike"]["use_gui_config"])
            self.assertEqual(st2["roguelike"]["theme"], "Sarkaz")
            self.assertEqual(st2["roguelike"]["mode"], "Investment")
            self.assertEqual(st2["roguelike"]["squad"], "蓝图测绘分队")
            self.assertEqual(st2["roguelike"]["roles"], "稳扎稳打")
            self.assertEqual(st2["roguelike"]["core_char"], "维什戴尔")
            self.assertEqual(st2["roguelike"]["starts_count"], 10)
            self.assertTrue(st2["roguelike"]["ai_copilot_enabled"])
            self.assertFalse(st2["roguelike"]["ai_auto_fallback_on_timeout"])
            self.assertEqual(st2["roguelike"]["ai_decision_timeout_sec"], 75.0)
            self.assertFalse(st2["roguelike"]["boss_prts_assist"])
            self.assertFalse(st2["roguelike"]["auto_claim_rewards"])
            self.assertFalse(st2["roguelike"]["auto_upgrade_tech_tree"])

            self.assertEqual(st2["ra_theme"], "Tales")
            self.assertEqual(st2["ra_mode"], "ProsperityNoSave")
            if isinstance(st2["ra_tools"], list):
                self.assertEqual(st2["ra_tools"], ["荧光棒"])
            else:
                self.assertIn("荧光棒", st2["ra_tools"])
            self.assertTrue(st2["ra_clear_store"])

            self.assertEqual(st2["reclamation"]["theme"], "Tales")
            self.assertEqual(st2["reclamation"]["mode"], "ProsperityNoSave")
            self.assertEqual(st2["reclamation"]["tools_to_craft"], ["荧光棒"])
            self.assertTrue(st2["reclamation"]["clear_store"])

            # Tab 4: ai + emulator
            self.assertEqual(st2["ai_mode"], "builtin_api")
            self.assertEqual(st2["base_url"], "https://api.deepseek.com/v1")
            self.assertEqual(st2["api_key"], "sk-test-123")
            self.assertEqual(st2["model_name"], "deepseek-chat")
            self.assertEqual(st2["timeout_sec"], 45)
            self.assertEqual(st2["adb_address"], "127.0.0.1:16416")

            self.assertEqual(st2["ai"]["mode"], "builtin_api")
            self.assertEqual(st2["ai"]["base_url"], "https://api.deepseek.com/v1")
            self.assertEqual(st2["ai"]["api_key"], "sk-test-123")
            self.assertEqual(st2["ai"]["model_name"], "deepseek-chat")
            self.assertEqual(st2["ai"]["timeout_sec"], 45)
            self.assertEqual(st2["emulator"]["adb_address"], "127.0.0.1:16416")

            # Tab 5: notifications
            self.assertTrue(st2["webhook_enabled"])
            self.assertEqual(st2["webhook_url"], "https://example.invalid/hook")
            self.assertFalse(st2["notify_on_daily_finish"])
            self.assertFalse(st2["notify_on_recruit_alert"])
            self.assertFalse(st2["notify_on_agent_task"])

            self.assertTrue(st2["notifications"]["webhook_enabled"])
            self.assertEqual(st2["notifications"]["webhook_url"], "https://example.invalid/hook")
            self.assertFalse(st2["notifications"]["notify_on_daily_finish"])
            self.assertFalse(st2["notifications"]["notify_on_recruit_alert"])
            self.assertFalse(st2["notifications"]["notify_on_agent_task"])

            # Assert quick_summary in snapshot
            self.assertEqual(snapshot["quick_summary"]["fight_mode"], "MANUAL")
            self.assertIs(snapshot["quick_summary"]["auto_push"], True)
            self.assertIn("MANUAL", snapshot["quick_summary"]["text"])
            self.assertIn("开", snapshot["quick_summary"]["text"])

            # Test toggling auto_push=False and "" ("跟随原版 MAA") round-trip
            toggle_payload = {
                "daily": {"auto_push": False},
                "roguelike": {"theme": "", "mode": ""},
                "reclamation": {"theme": "", "mode": ""},
            }
            with patch.object(controller2, "_refresh_snapshot_async"):
                api2.save_settings(toggle_payload)

            st3 = api2.get_settings()
            self.assertFalse(st3["daily"]["auto_push"])
            self.assertFalse(st3["auto_push"])
            self.assertEqual(st3["roguelike"]["theme"], "")
            self.assertEqual(st3["rl_theme"], "")
            self.assertEqual(st3["roguelike"]["mode"], "")
            self.assertEqual(st3["rl_mode"], "")
            self.assertEqual(st3["reclamation"]["theme"], "")
            self.assertEqual(st3["ra_theme"], "")
            self.assertEqual(st3["reclamation"]["mode"], "")
            self.assertEqual(st3["ra_mode"], "")

            # Test legacy flat mutation persists medicine == 9
            st_legacy = api2.get_settings()
            st_legacy["medicine"] = 9
            with patch.object(controller2, "_refresh_snapshot_async"):
                api2.save_settings(st_legacy)
            st4 = api2.get_settings()
            self.assertEqual(st4["medicine"], 9)
            self.assertEqual(st4["daily"]["medicine"], 9)

    def test_agent_prompts_and_readme_non_empty_in_source_and_frozen_isolated_dir(self):
        LauncherController = getattr(launcher_gui, "LauncherController", None)
        WebViewJsApi = getattr(launcher_gui, "WebViewJsApi", None)
        if LauncherController is None:
            from universal_engine.gui.controller import LauncherController
            from universal_engine.gui.bridge import WebViewJsApi

        # (a) Normal temp workspace engine
        with tempfile.TemporaryDirectory() as tmp_a:
            cfg_path = Path(tmp_a) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            (Path(tmp_a) / "README.md").write_text("# PRTS Guide\nMAA launcher_config.json agent_task.json", encoding="utf-8")
            eng_a = master_scheduler.MaaEngine(config_path=cfg_path)
            ctrl_a = LauncherController(engine=eng_a, auto_start_refresh=False)
            api_a = WebViewJsApi(ctrl_a)
            res_a = api_a.get_agent_prompts()

            self.assertTrue(res_a.get("ok", True))
            for k in ("init_prompt", "prompt_init", "repair_prompt", "prompt_repair", "readme_content"):
                self.assertIn(k, res_a)
                self.assertIsInstance(res_a[k], str)
                self.assertTrue(len(res_a[k].strip()) > 0, f"{k} is empty in scenario (a)")
            self.assertEqual(res_a["init_prompt"], res_a["prompt_init"])
            self.assertEqual(res_a["repair_prompt"], res_a["prompt_repair"])
            self.assertIn("launcher_config.json", res_a["init_prompt"])
            self.assertIn("agent_task.json", res_a["repair_prompt"])
            self.assertIn("MAA", res_a["init_prompt"])

        # (b) Isolated empty temp directory with NO README.md (must fall back to non-empty README)
        with tempfile.TemporaryDirectory() as tmp_b:
            cfg_path = Path(tmp_b) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            eng_b = master_scheduler.MaaEngine(config_path=cfg_path)
            ctrl_b = LauncherController(engine=eng_b, auto_start_refresh=False)
            api_b = WebViewJsApi(ctrl_b)
            res_b = api_b.get_agent_prompts()

            self.assertTrue(res_b.get("ok", True))
            for k in ("init_prompt", "prompt_init", "repair_prompt", "prompt_repair", "readme_content"):
                self.assertIn(k, res_b)
                self.assertIsInstance(res_b[k], str)
                self.assertTrue(len(res_b[k].strip()) > 0, f"{k} is empty in scenario (b)")
            self.assertEqual(res_b["init_prompt"], res_b["prompt_init"])
            self.assertEqual(res_b["repair_prompt"], res_b["prompt_repair"])
            self.assertIn("launcher_config.json", res_b["init_prompt"])
            self.assertIn("agent_task.json", res_b["repair_prompt"])
            self.assertIn("MAA", res_b["init_prompt"])

        # (c) Simulated PyInstaller frozen mode (sys.frozen = True, sys._MEIPASS = tmp_meipass with bundled README.md)
        with tempfile.TemporaryDirectory() as tmp_c, tempfile.TemporaryDirectory() as tmp_meipass:
            bundled_dir = Path(tmp_meipass) / "universal_engine"
            bundled_dir.mkdir(parents=True)
            (bundled_dir / "README.md").write_text("# Bundled Frozen PRTS Doc\nMAA launcher_config.json", encoding="utf-8")

            cfg_path = Path(tmp_c) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)

            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "_MEIPASS", str(tmp_meipass), create=True):
                eng_c = master_scheduler.MaaEngine(config_path=cfg_path)
                ctrl_c = LauncherController(engine=eng_c, auto_start_refresh=False)
                api_c = WebViewJsApi(ctrl_c)
                res_c = api_c.get_agent_prompts()

                self.assertTrue(res_c.get("ok", True))
                for k in ("init_prompt", "prompt_init", "repair_prompt", "prompt_repair", "readme_content"):
                    self.assertIn(k, res_c)
                    self.assertIsInstance(res_c[k], str)
                    self.assertTrue(len(res_c[k].strip()) > 0, f"{k} is empty in scenario (c)")
                self.assertEqual(res_c["init_prompt"], res_c["prompt_init"])
                self.assertEqual(res_c["repair_prompt"], res_c["prompt_repair"])
                self.assertIn("launcher_config.json", res_c["init_prompt"])
                self.assertIn("agent_task.json", res_c["repair_prompt"])
                self.assertIn("MAA", res_c["init_prompt"])
                self.assertIn("Bundled Frozen PRTS Doc", res_c["readme_content"])

    def test_overview_annihilation_formatting_and_zero_emoji(self):
        format_snapshot_payload = getattr(launcher_gui, "format_snapshot_payload", None)
        LauncherController = getattr(launcher_gui, "LauncherController", None)
        if format_snapshot_payload is None:
            from universal_engine.gui.controller import format_snapshot_payload, LauncherController

        emoji_pattern = re.compile(
            r"[\U0001F000-\U0001FAFF]|[\U0001F600-\U0001F64F]|[\U0001F300-\U0001F5FF]|"
            r"[\U0001F680-\U0001F6FF]|[\U0001F1E0-\U0001F1FF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]"
        )
        forbidden_chars = ("✅", "▶", "⏹", "🔔", "🎮")

        # 1. Test format_snapshot_payload with 4 cases
        cases = [
            ([1800, 1800], "1800 / 1800", True),
            ([900, 1800], "900 / 1800", False),
            (None, "-- / 1800", False),
            ("1800/1800", "1800 / 1800", True),
        ]
        for proc_input, expected_proc, expected_done in cases:
            raw = {
                "config": {},
                "integrity": {},
                "my_box": {},
                "depot": {},
                "growth_preview": {},
                "annihilation_process": proc_input,
            }
            snap = format_snapshot_payload(raw)
            annih = snap["annihilation"]
            self.assertEqual(annih["process"], expected_proc)
            self.assertNotEqual(annih["process"], "1800,1800")
            self.assertIsInstance(annih["process"], str)
            self.assertEqual(annih["done"], expected_done)
            self.assertNotIn("✅", annih["label"])
            self.assertEqual(emoji_pattern.findall(annih["label"]), [])

        # 2. Test controller task_state ra_button_text and banners contain zero emoji
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            eng = master_scheduler.MaaEngine(config_path=cfg_path)
            ctrl = LauncherController(engine=eng, auto_start_refresh=False)

            # Check ra_button_text in running and idle states
            ctrl._set_running_state(True, "运行生息演算", task_kind="reclamation")
            ra_running = ctrl.task_state.get("ra_button_text", "")
            self.assertIn("停止生息演算", ra_running)
            for c in forbidden_chars:
                self.assertNotIn(c, ra_running)
            self.assertEqual(emoji_pattern.findall(ra_running), [])

            ctrl._set_running_state(False, "空闲")
            ra_idle = ctrl.task_state.get("ra_button_text", "")
            self.assertIn("启动生息演算", ra_idle)
            for c in forbidden_chars:
                self.assertNotIn(c, ra_idle)
            self.assertEqual(emoji_pattern.findall(ra_idle), [])

            # Check agent_banner title
            ctrl._show_agent_banner({
                "stage_code": "14-20",
                "round": 1,
                "trigger_reason": "battle_failed",
                "battle_state": {"kills": 40},
                "target_copilot_path": "push_14-20.json",
            })
            evs = ctrl.drain_events()
            agent_evs = [e["payload"] for e in evs if e.get("type") == "agent_banner"]
            self.assertTrue(len(agent_evs) >= 1)
            ab_title = agent_evs[-1].get("title", "")
            for c in forbidden_chars:
                self.assertNotIn(c, ab_title)
            self.assertEqual(emoji_pattern.findall(ab_title), [])

            # Check roguelike agent_banner title
            ctrl._show_agent_banner({
                "task_type": "roguelike_decision",
                "theme": "Sarkaz",
                "decision_type": "recruitment",
            })
            evs_rl = ctrl.drain_events()
            rl_evs = [e["payload"] for e in evs_rl if e.get("type") == "agent_banner"]
            self.assertTrue(len(rl_evs) >= 1)
            rl_title = rl_evs[-1].get("title", "")
            for c in forbidden_chars:
                self.assertNotIn(c, rl_title)
            self.assertEqual(emoji_pattern.findall(rl_title), [])

            # Check tutorial_banner title
            ctrl._show_tutorial_banner({"stage_code": "0-1", "next_stage": "0-2"})
            evs_tut = ctrl.drain_events()
            tut_evs = [e["payload"] for e in evs_tut if e.get("type") == "tutorial_banner"]
            self.assertTrue(len(tut_evs) >= 1)
            tut_title = tut_evs[-1].get("title", "")
            for c in forbidden_chars:
                self.assertNotIn(c, tut_title)
            self.assertEqual(emoji_pattern.findall(tut_title), [])

    def test_frontend_m3_expressive_responsive_and_bindings_audit(self):
        web_dir = Path(__file__).resolve().parent.parent / "gui" / "web"
        app_js = (web_dir / "js" / "app.js").read_text(encoding="utf-8")
        index_html = (web_dir / "index.html").read_text(encoding="utf-8")
        app_css = (web_dir / "styles" / "app.css").read_text(encoding="utf-8")
        tokens_css = (web_dir / "styles" / "tokens.css").read_text(encoding="utf-8")

        # 1. app.js checks
        self.assertTrue(
            "#set-fight-mode" in app_js,
            "app.js must reference #set-fight-mode",
        )
        self.assertTrue(
            "updateSnapshotUI" in app_js and "snapshot" in app_js,
            "app.js must call updateSnapshotUI with res.snapshot on saveSettings",
        )
        self.assertTrue(
            ("init_prompt" in app_js or "prompt_init" in app_js) and ("repair_prompt" in app_js or "prompt_repair" in app_js),
            "app.js must read agent prompts (init_prompt/prompt_init and repair_prompt/prompt_repair)",
        )

        # 2. index.html checks
        self.assertIn("Official", index_html)
        self.assertIn("Bilibili", index_html)
        self.assertIn("close_emulator", index_html)
        self.assertTrue(
            "icon-check" in index_html or "seg-check" in index_html,
            "index.html must include checkmark SVG icon for segmented-btn",
        )

        # 3. app.css checks
        app_shell_match = re.search(r"\.app-shell\s*\{([^}]+)\}", app_css)
        if app_shell_match:
            shell_rules = app_shell_match.group(1)
            self.assertNotIn("min-width: 920px", shell_rules)
            self.assertNotIn("min-height: 600px", shell_rules)
            self.assertNotIn("min-width:920px", shell_rules)
            self.assertNotIn("min-height:600px", shell_rules)

        self.assertIn("white-space: nowrap", app_css)
        self.assertIn("flex-shrink: 0", app_css)
        self.assertIn(".nav-rail-bottom", app_css)
        self.assertIn(".bottom-log-drawer", app_css)

        # 4. Zero emoji & zero external URL checks across all 5 assets
        emoji_pattern = re.compile(
            r"[\U0001F000-\U0001FAFF]|[\U0001F600-\U0001F64F]|[\U0001F300-\U0001F5FF]|"
            r"[\U0001F680-\U0001F6FF]|[\U0001F1E0-\U0001F1FF]|[\u2600-\u27BF]|[\u2300-\u23FF]|[\u2B50-\u2B55]"
        )
        url_pattern = re.compile(r'https?://[a-zA-Z0-9\.\-\_\/]+')
        expected_files = [
            web_dir / "index.html",
            web_dir / "styles" / "tokens.css",
            web_dir / "styles" / "app.css",
            web_dir / "js" / "bridge.js",
            web_dir / "js" / "app.js",
        ]
        for f in expected_files:
            content = f.read_text(encoding="utf-8")
            emojis_found = emoji_pattern.findall(content)
            self.assertEqual(
                emojis_found,
                [],
                f"File {f.name} contains forbidden Unicode Emoji characters: {emojis_found}",
            )
            if f.suffix in (".html", ".css"):
                external_urls = [u for u in url_pattern.findall(content) if not u.startswith("http://127.0.0.1") and not u.startswith("http://localhost")]
                self.assertEqual(
                    external_urls,
                    [],
                    f"File {f.name} contains forbidden external CDN/network links: {external_urls}",
                )


if __name__ == "__main__":
    unittest.main()
