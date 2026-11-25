import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from universal_engine import ai_strategist, maa_runner, master_scheduler, stage_pilot
from universal_engine.utils import env_doctor

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class CopilotFileErrorAndMapResilienceTests(unittest.TestCase):
    def setUp(self):
        self.my_box_e0 = {
            "total_owned": 4,
            "e2_count": 2,
            "e2_operators": [
                {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                {"name": "山", "elite": 2, "level": 60, "rarity": 6},
            ],
            "all_owned": {
                "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                "山": {"name": "山", "elite": 2, "level": 60, "rarity": 6},
                "歌蕾蒂娅": {"name": "歌蕾蒂娅", "elite": 0, "level": 1, "rarity": 6},
                "史都华德": {"name": "史都华德", "elite": 0, "level": 1, "rarity": 3},
                "艾雅法拉": {"name": "艾雅法拉", "elite": 2, "level": 75, "rarity": 6},
            },
        }

    # ==================================================================
    # 1. CopilotFileReadError classification & guide-switch recovery
    # ==================================================================
    def test_copilot_file_read_error_classified_distinctly_and_never_triggers_battle_failed(self):
        # Exact callback & log sequence observed in debug/asst.log for push_16-5_r1.json
        events = [
            {"message": "TaskChainStart", "details": {"taskchain": "Copilot", "taskid": 0}},
            {
                "message": "SubTaskError",
                "details": {
                    "class": "asst::CopilotTask",
                    "details": {},
                    "subtask": "CopilotTask",
                    "taskchain": "Copilot",
                    "taskid": 0,
                    "uuid": "bba9c3cb7e7490bf",
                    "what": "CopilotFileReadError",
                },
            },
        ]
        logs = [
            '[2026-11-23 00:04:11.551][INF][Px30116][Tx31277] asst::Assistant::append_task Copilot {"filename": "C:\\\\MAA\\\\copilots\\\\push_16-5_r1.json", "formation": true, "add_trust": false, "ignore_requirements": false}',
            "[2026-11-23 00:04:11.551][ERR][Px30116][Tx31277] asst::CopilotConfig::parse_oper_usage | Oper 歌蕾蒂娅 has higher elite requirement: 1 , but elite requirement is set to 0",
            "[2026-11-23 00:04:11.552][ERR][Px30116][Tx31277] CopilotConfig parse failed",
            '[2026-11-23 00:04:11.552][INF][Px30116][Tx31277] Assistant::append_callback | SubTaskError {"class":"asst::CopilotTask","details":{},"subtask":"CopilotTask","taskchain":"Copilot","taskid":0,"uuid":"bba9c3cb7e7490bf","what":"CopilotFileReadError"}',
        ]

        m = ai_strategist.parse_battle_metrics(logs, events, entered_formation=True)
        self.assertTrue(m["copilot_file_error"])
        self.assertIn("higher elite requirement", m["copilot_file_error_detail"])
        self.assertFalse(m["battle_failed"])
        self.assertFalse(m["stage_entry_failed"])
        self.assertFalse(m["formation_unavailable"])
        self.assertTrue(any("CopilotFileReadError" in e for e in m["errors"]))

        verdict = ai_strategist.evaluate_copilot_victory(m, require_map_verify=True)
        self.assertFalse(verdict["victory"])
        self.assertFalse(verdict["battle_failed"])
        self.assertTrue(verdict["copilot_file_error"])

        report = {
            "copilot_file": r"C:\MAA\copilots\push_16-5_r1.json",
            "metrics": m,
            "events_tail": events,
            "log_tail": logs,
        }
        self.assertTrue(ai_strategist.is_copilot_file_error(report))
        self.assertFalse(ai_strategist.is_stage_entry_failure(report))
        self.assertFalse(ai_strategist.is_formation_unavailable(report))

    def test_autonomous_clear_stage_switches_prts_guide_on_copilot_file_error_without_agent_collab(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "16-5", "stageId": "main_16-04", "apCost": 21}]),
                encoding="utf-8",
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps(self.my_box_e0, ensure_ascii=False), encoding="utf-8"
            )

            guides = [
                {
                    "id": 70706,
                    "like": 200,
                    "views": 5000,
                    "content": {
                        "stage_name": "main_16-04",
                        "opers": [
                            {"name": "棘刺", "skill": 3, "skill_usage": 1, "requirements": {"elite": 2, "skill_level": 7}},
                            {"name": "山", "skill": 2, "skill_usage": 2, "requirements": {"elite": 2, "skill_level": 7}},
                            {"name": "歌蕾蒂娅", "skill": 1, "skill_usage": 0, "requirements": {"elite": 0, "skill_level": 7}},
                        ],
                        "groups": [],
                        "actions": [{"type": "Deploy", "name": "棘刺", "location": [4, 2], "direction": "Down"}],
                    },
                },
                {
                    "id": 99001,
                    "like": 150,
                    "views": 3000,
                    "content": {
                        "stage_name": "main_16-04",
                        "opers": [
                            {"name": "棘刺", "skill": 3, "skill_usage": 1, "requirements": {"elite": 2, "skill_level": 7}},
                            {"name": "艾雅法拉", "skill": 2, "skill_usage": 1, "requirements": {"elite": 2, "skill_level": 7}},
                        ],
                        "groups": [],
                        "actions": [{"type": "Deploy", "name": "棘刺", "location": [4, 2], "direction": "Down"}],
                    },
                },
            ]

            written_files: list[Path] = []
            round_logs: list[str] = []

            def _fake_round(**kwargs):
                cf = Path(kwargs["copilot_file"])
                written_files.append(cf)
                if kwargs["round_idx"] == 1:
                    return 1, {
                        "round": 1,
                        "victory": False,
                        "battle_failed": False,
                        "copilot_file_error": True,
                        "copilot_file_error_detail": "Oper 歌蕾蒂娅 has higher elite requirement: 1 , but elite requirement is set to 0",
                        "copilot_file": str(cf.resolve()),
                        "metrics": {
                            "kills": None,
                            "costs": None,
                            "last_3_actions": [],
                            "errors": ["SubTaskError:CopilotTask:CopilotFileReadError"],
                            "failed_flag": False,
                            "battle_failed": False,
                            "completed_flag": False,
                            "copilot_file_error": True,
                            "copilot_file_error_detail": "Oper 歌蕾蒂娅 has higher elite requirement: 1 , but elite requirement is set to 0",
                        },
                        "events_tail": [
                            {"message": "SubTaskError", "details": {"subtask": "CopilotTask", "what": "CopilotFileReadError"}}
                        ],
                        "log_tail": ["[ERR] CopilotConfig parse failed"],
                        "screenshot": "",
                        "report_path": "",
                    }
                return 0, {
                    "round": 2,
                    "victory": True,
                    "map_clear_verified": True,
                    "copilot_file": str(cf.resolve()),
                    "metrics": {"kills": 40, "costs": 30, "last_3_actions": [], "errors": [], "failed_flag": False, "completed_flag": True},
                    "screenshot": "",
                    "report_path": "",
                }

            collab_calls = []
            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=_fake_round):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "16-5",
                    maa_dir=maa,
                    config={"ai": {"mode": "agent_collab", "enabled": False}},
                    max_rounds=5,
                    base_dir=base,
                    log_callback=round_logs.append,
                    collab_handler=collab_calls.append,
                )

            self.assertTrue(ok)
            self.assertTrue(summary["victory"])
            self.assertEqual(summary["final_guide_id"], 99001)
            self.assertEqual(len(written_files), 2)
            self.assertEqual(len(collab_calls), 0)
            self.assertFalse((base / "agent_task.json").exists())
            self.assertTrue(any("CopilotFileReadError" in l and "push_16-5_r1.json" in l for l in round_logs))

            # When no more valid PRTS guides exist, abort cleanly with copilot_file_error=True and never retry the broken file
            def _always_file_err(**kwargs):
                cf = Path(kwargs["copilot_file"])
                return 1, {
                    "round": kwargs["round_idx"],
                    "victory": False,
                    "battle_failed": False,
                    "copilot_file_error": True,
                    "copilot_file_error_detail": "CopilotConfig parse failed",
                    "copilot_file": str(cf.resolve()),
                    "metrics": {
                        "kills": None,
                        "costs": None,
                        "errors": ["SubTaskError:CopilotTask:CopilotFileReadError"],
                        "failed_flag": False,
                        "battle_failed": False,
                        "completed_flag": False,
                        "copilot_file_error": True,
                        "copilot_file_error_detail": "CopilotConfig parse failed",
                    },
                    "events_tail": [],
                    "log_tail": [],
                    "screenshot": "",
                    "report_path": "",
                }

            with patch.object(maa_runner, "fetch_prts_guides", return_value=[guides[0]]), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=_always_file_err) as mock_r:
                ok_ex, sum_ex = maa_runner.autonomous_clear_stage(
                    "16-5",
                    maa_dir=maa,
                    config={"ai": {"mode": "agent_collab", "enabled": False}},
                    max_rounds=5,
                    base_dir=base,
                    collab_handler=collab_calls.append,
                )
            self.assertFalse(ok_ex)
            self.assertTrue(sum_ex.get("copilot_file_error"))
            self.assertFalse(sum_ex.get("battle_failed"))
            self.assertEqual(mock_r.call_count, 1)
            self.assertEqual(len(collab_calls), 0)

    # ==================================================================
    # 2. Copilot JSON sanitization & strict disk validation
    # ==================================================================
    def test_copilot_clamp_sanitization_bom_and_run_round_prevalidation(self):
        # _clamp_requirements must NEVER emit elite=0 with skill_level=7
        clamped = ai_strategist._clamp_requirements(
            {"elite": 0, "level": 1, "skill_level": 7, "module": 1, "potentiality": 1},
            {"name": "歌蕾蒂娅", "elite": 0, "level": 1, "rarity": 6},
            skill=1,
        )
        self.assertEqual(clamped["elite"], 0)
        self.assertLessEqual(clamped["skill_level"], 4)
        self.assertEqual(clamped["skill_level"], 1)
        self.assertEqual(clamped["module"], -1)

        # adapt_copilot must substitute E0 3-star 史都华德 when guide uses skill_usage=1
        guide_stward = {
            "id": 87643,
            "like": 50,
            "views": 1000,
            "content": {
                "stage_name": "main_16-04",
                "opers": [
                    {"name": "棘刺", "skill": 3, "skill_usage": 1, "requirements": {"elite": 2, "skill_level": 7}},
                    {"name": "史都华德", "skill": 1, "skill_usage": 1, "requirements": {"elite": 0, "skill_level": 7}},
                ],
                "groups": [],
                "actions": [
                    {"type": "Deploy", "name": "棘刺", "location": [4, 2], "direction": "Down"},
                    {"type": "Deploy", "name": "史都华德", "location": [5, 4], "direction": "Down"},
                ],
            },
        }
        maa_opt = REPO_ROOT if (REPO_ROOT / "resource" / "battle_data.json").is_file() else None
        adapted = ai_strategist.adapt_copilot(guide_stward, self.my_box_e0, "main_16-04", maa_dir=maa_opt)
        if maa_opt is not None:
            self.assertEqual(adapted["substitutions"].get("史都华德"), "艾雅法拉")
        else:
            self.assertIn(adapted["substitutions"].get("史都华德"), ("艾雅法拉", "山"))
        ok_ad, errs_ad = ai_strategist.validate_copilot_payload(adapted["copilot"], maa_dir=maa_opt)
        self.assertTrue(ok_ad, msg=f"Adapted payload invalid: {errs_ad}")

        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            # write_validated_copilot_file sanitizes inconsistent E0 skill_level=7 & writes BOM-free UTF-8
            dirty = {
                "stage_name": "main_16-04",
                "opers": [
                    {"name": "歌蕾蒂娅", "skill": 1, "skill_usage": 0, "requirements": {"elite": 0, "skill_level": 7, "module": 1}},
                ],
                "groups": [],
                "actions": [{"type": "Deploy", "name": "歌蕾蒂娅", "location": [3, 3], "direction": "Right"}],
            }
            out_p, clean = ai_strategist.write_validated_copilot_file(tdir / "sub" / "ok.json", dirty)
            self.assertTrue(out_p.is_absolute())
            raw = out_p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertLessEqual(clean["opers"][0]["requirements"]["skill_level"], 4)
            self.assertEqual(clean["opers"][0]["requirements"]["module"], -1)
            ok_f, err_f, _ = ai_strategist.validate_copilot_file(out_p)
            self.assertTrue(ok_f, msg=err_f)

            # Reject missing, empty, BOM, malformed JSON, missing stage_name, missing actions, invalid E0 skill_level=7
            self.assertFalse(ai_strategist.validate_copilot_file(tdir / "missing.json")[0])
            empty_p = tdir / "empty.json"
            empty_p.write_text("", encoding="utf-8")
            self.assertFalse(ai_strategist.validate_copilot_file(empty_p)[0])

            bom_p = tdir / "bom.json"
            bom_p.write_bytes(b"\xef\xbb\xbf" + raw)
            ok_bom, err_bom, _ = ai_strategist.validate_copilot_file(bom_p)
            self.assertFalse(ok_bom)
            self.assertIn("BOM", err_bom)

            bad_json_p = tdir / "bad.json"
            bad_json_p.write_text("{broken", encoding="utf-8")
            self.assertFalse(ai_strategist.validate_copilot_file(bad_json_p)[0])

            bad_schema_p = tdir / "bad_schema.json"
            bad_schema_p.write_text(json.dumps(dirty, ensure_ascii=False), encoding="utf-8")
            ok_bs, err_bs, _ = ai_strategist.validate_copilot_file(bad_schema_p)
            self.assertFalse(ok_bs)
            self.assertIn("skill_level", err_bs)

            no_act_p = tdir / "no_actions.json"
            no_act_p.write_text(json.dumps({"stage_name": "main_16-04", "opers": [{"name": "棘刺"}]}), encoding="utf-8")
            self.assertFalse(ai_strategist.validate_copilot_file(no_act_p)[0])

            bad_act_p = tdir / "bad_action.json"
            bad_act_p.write_text(
                json.dumps({"stage_name": "main_16-04", "opers": [{"name": "棘刺"}], "actions": [{}]}),
                encoding="utf-8",
            )
            self.assertFalse(ai_strategist.validate_copilot_file(bad_act_p)[0])

            # _run_copilot_round must block invalid file before calling sess.run_single_task and pass resolved path for valid file
            base = tdir / "engine"
            maa = tdir / "maa"
            base.mkdir()
            maa.mkdir()
            fake_sess = unittest.mock.MagicMock()
            fake_sess.adb_path = "adb.exe"
            fake_sess.adb_addr = "127.0.0.1:16384"
            fake_sess.connect.return_value = True
            fake_sess.run_single_task.return_value = {
                "ok": True, "cancelled": False, "elapsed": 10.0, "events": [], "log_lines": [],
            }
            with patch.object(maa_runner, "ensure_game_running"), \
                 patch.object(maa_runner, "init_maa_sdk", return_value=True), \
                 patch.object(maa_runner, "MaaSession", return_value=fake_sess), \
                 patch.object(maa_runner, "capture_adb_screenshot", return_value=False), \
                 patch.object(maa_runner.stage_pilot, "prepare_battle_entry_if_on_map", return_value={"handled": True, "entered": True, "outcome": "formation"}):
                rc_bad, rep_bad = maa_runner._run_copilot_round(
                    1, bad_schema_p, maa, {}, base, stage_code="16-5"
                )
                self.assertEqual(rc_bad, 1)
                self.assertTrue(rep_bad["copilot_file_error"])
                self.assertFalse(rep_bad["battle_failed"])
                fake_sess.run_single_task.assert_not_called()

                rc_ok, _ = maa_runner._run_copilot_round(
                    2, out_p, maa, {}, base, stage_code="16-5", verify_map_clear=False
                )
                self.assertEqual(rc_ok, 1)  # no TaskChainCompleted in fake_sess
                fake_sess.run_single_task.assert_called_once()
                passed_params = fake_sess.run_single_task.call_args[0][1]
                self.assertEqual(passed_params["filename"], str(out_p.resolve()))

    # ==================================================================
    # 3. Pre-push non-map auto-reset & real 16-5 screenshot regression
    # ==================================================================
    def test_pre_push_resets_formation_select_and_settlement_to_map_and_detects_real_16_5(self):
        dummy = Image.new("RGB", (1280, 720), (30, 32, 40))

        # A. Real stuck formation screenshot reports/push_16-5_fail_round_2.png
        real_form = REPO_ROOT / "reports" / "push_16-5_fail_round_2.png"
        if real_form.is_file() and (REPO_ROOT / "resource" / "template").is_dir():
            with Image.open(real_form) as im_form:
                form_img = im_form.convert("RGB")
            taps_real: list[tuple[int, int]] = []
            with patch.object(stage_pilot, "adb_screencap_image", side_effect=[form_img, dummy]), \
                 patch.object(
                     stage_pilot,
                     "analyze_screen_state",
                     side_effect=[
                         stage_pilot.analyze_screen_state(form_img, REPO_ROOT, use_ocr_fallback=False),
                         {"in_formation": False, "in_formation_select": False, "in_battle": False, "on_stage_map": True, "has_stage_start_btn": False},
                     ],
                 ), \
                 patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, **kw: taps_real.append((x, y)) or True), \
                 patch("time.sleep"):
                res_real = stage_pilot.ensure_on_stage_map_before_search("adb.exe", "127.0.0.1:16384", REPO_ROOT)
            self.assertTrue(res_real["reset_needed"])
            self.assertTrue(res_real["recovered"])
            self.assertEqual(res_real["initial_page"], "formation_overview")
            self.assertEqual(res_real["final_page"], "stage_map")
            self.assertEqual(len(taps_real), 1)

        # B. formation_select (清空选择) -> formation_overview (快捷编队) -> stage_map (2 back clicks)
        taps_multi: list[tuple[int, int]] = []
        states_multi = [
            {"in_formation": False, "in_formation_select": True, "in_battle": False, "on_stage_map": False, "return_xy": (89, 38)},
            {"in_formation": True, "in_formation_select": False, "in_battle": False, "on_stage_map": False, "return_xy": (89, 38)},
            {"in_formation": False, "in_formation_select": False, "in_battle": False, "on_stage_map": True, "has_stage_start_btn": False},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[dummy, dummy, dummy]), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=states_multi), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, **kw: taps_multi.append((x, y)) or True), \
             patch("time.sleep"):
            res_multi = stage_pilot.ensure_on_stage_map_before_search("adb.exe", "127.0.0.1:16384", REPO_ROOT)
        self.assertTrue(res_multi["recovered"])
        self.assertEqual(res_multi["initial_page"], "formation_select")
        self.assertEqual(taps_multi, [(89, 38), (89, 38)])

        # C. enter_stage_node("16-5", ...) starting from formation_overview resets '<' before searching 16-5
        taps_enter: list[tuple[int, int]] = []
        states_enter = [
            {"in_formation": True, "in_formation_select": False, "in_battle": False, "on_stage_map": False, "return_xy": (89, 38)},
            {"in_formation": False, "in_formation_select": False, "in_battle": False, "on_stage_map": True, "has_stage_start_btn": False},
            {"in_formation": False, "in_formation_select": False, "in_battle": False, "on_stage_map": True, "has_stage_start_btn": False},
            {"in_formation": False, "in_formation_select": False, "in_battle": False, "on_stage_map": True, "has_stage_start_btn": True, "stage_start_xy": (1110, 650)},
        ]
        with patch.object(stage_pilot, "adb_screencap_image", side_effect=[dummy, dummy, dummy, dummy]), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=states_enter), \
             patch.object(stage_pilot, "find_stage_center_on_image", return_value=(640, 375, {"x": 614, "y": 365, "w": 52, "h": 21})), \
             patch.object(stage_pilot, "inspect_stage_detail_panel", return_value={"displayed_stage": "16-5", "title_matches": True, "already_cleared_3star": False}), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, **kw: taps_enter.append((x, y)) or True), \
             patch("time.sleep"):
            res_ent = stage_pilot.enter_stage_node("16-5", "adb.exe", "127.0.0.1:16384", REPO_ROOT, click_start=True)
        self.assertTrue(res_ent["entered"])
        self.assertEqual(taps_enter, [(89, 38), (640, 375), (1110, 650)])

        # D. MaaEngine.run_push(["16-5"]) calls ensure_on_stage_map_before_search before autonomous_clear_stage
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True}, cfg_path)
            call_order: list[str] = []
            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch.object(stage_pilot, "ensure_on_stage_map_before_search", side_effect=lambda *a, **kw: call_order.append("reset") or {"on_stage_map": True}), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", side_effect=lambda st, **kw: call_order.append(f"clear:{st}") or (True, {"stage": st, "victory": True, "map_clear_verified": True})):
                    res_push = eng.run_push(["16-5"], refresh_cache_flag=False)
            self.assertTrue(res_push["ok"])
            self.assertEqual(call_order, ["reset", "clear:16-5"])

        # E. Real map screenshot push_16-5_fail_round_1.png (OCR '16 ． 5') must detect 16-5 as uncleared
        real_map = REPO_ROOT / "reports" / "push_16-5_fail_round_1.png"
        if real_map.is_file() and (REPO_ROOT / "resource" / "template").is_dir() and sys.platform == "win32":
            with Image.open(real_map) as im_map:
                nodes = stage_pilot.inspect_map_stage_nodes(im_map.convert("RGB"), "")
                by_code = {n["code"]: n for n in nodes}
                self.assertIn("16-5", by_code)
                self.assertFalse(by_code["16-5"]["cleared"])
                self.assertIsNotNone(stage_pilot.find_stage_center_on_image(im_map.convert("RGB"), "16-5"))

    # ==================================================================
    # 4. Dynamic map left-swipe pre-close drawer & Y-band retry fallback
    # ==================================================================
    def test_dynamic_map_swipe_closes_detail_drawer_first_and_retries_new_y_band_on_low_mse(self):
        page1_drawer = Image.new("RGB", (1280, 720), (210, 215, 222))
        page1_clean = Image.new("RGB", (1280, 720), (208, 214, 220))
        page2_new = Image.new("RGB", (1280, 720), (120, 155, 195))

        boxes_p1 = [{"text": "16-4", "norm": "16-4", "x": 280, "y": 445, "w": 60, "h": 22, "pass": "norm"}]
        boxes_p2 = [{"text": "16-5", "norm": "16-5", "x": 620, "y": 365, "w": 60, "h": 22, "pass": "norm"}]

        actions_order: list[tuple] = []
        sleeps: list[float] = []

        # Sequence:
        # 1) initial cap: page1_clean (on_stage_map=True)
        # 2) pre-swipe check inside loop sees has_stage_start_btn=True -> closes drawer -> re-caps page1_clean
        # 3) swipe #1 (y=396, dur=650) returns page1_clean (low MSE -> unchanged_swipes=1)
        # 4) swipe #2 switches y=252, dur=800 -> returns page2_new -> finds 16-5!
        cap_seq = [page1_clean, page1_clean, page1_clean, page2_new]
        state_seq = [
            {"on_stage_map": True, "has_stage_start_btn": False, "has_story_start_btn": False},
            {"on_stage_map": True, "has_stage_start_btn": True, "has_story_start_btn": False},  # pre-swipe #1 drawer open!
            {"on_stage_map": True, "has_stage_start_btn": False, "has_story_start_btn": False},  # pre-swipe #2 clean
        ]
        nodes_seq = [
            [{"code": "16-4", "node_type": "battle", "cleared": True, "badge_full_3star": True, "partial_star": False, "cx": 310, "cy": 456, "x": 280}],
            [{"code": "16-4", "node_type": "battle", "cleared": True, "badge_full_3star": True, "partial_star": False, "cx": 310, "cy": 456, "x": 280}],
            [{"code": "16-5", "node_type": "battle", "cleared": False, "badge_full_3star": False, "partial_star": False, "cx": 650, "cy": 376, "x": 620}],
        ]

        with patch.object(stage_pilot, "adb_screencap_image", side_effect=cap_seq), \
             patch.object(stage_pilot, "analyze_screen_state", side_effect=state_seq), \
             patch.object(stage_pilot, "inspect_map_stage_nodes", side_effect=nodes_seq), \
             patch.object(stage_pilot, "find_safe_blank_tap_point", return_value=(420, 160)), \
             patch.object(stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h, **kw: actions_order.append(("tap", x, y)) or True), \
             patch.object(stage_pilot, "adb_swipe_norm", side_effect=lambda a, b, x1, y1, x2, y2, duration_ms=0, **kw: actions_order.append(("swipe", x1, y1, x2, y2, duration_ms)) or True), \
             patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
            res = stage_pilot.discover_next_uncleared_stage_on_map(
                "adb.exe", "127.0.0.1:16384", REPO_ROOT, max_swipes=4, unchanged_swipe_limit=2
            )

        self.assertTrue(res["found"])
        self.assertEqual(res["next_stage"], "16-5")
        self.assertEqual(res["swipes_done"], 2)
        # Detail drawer MUST be tapped closed BEFORE the 1st swipe
        self.assertEqual(actions_order[0], ("tap", 420, 160))
        self.assertEqual(actions_order[1], ("swipe", 998, 396, 282, 396, 650))
        # After low MSE, 2nd swipe MUST switch Y-band (396 -> 252) and increase duration (650 -> 800)
        self.assertEqual(actions_order[2], ("swipe", 998, 252, 282, 252, 800))
        # Post-swipe settle wait must be >= 1.0s
        self.assertGreaterEqual(sleeps.count(1.0), 2)


if __name__ == "__main__":
    unittest.main()
