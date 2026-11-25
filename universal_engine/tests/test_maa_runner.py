import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from universal_engine import maa_runner


class MaaRunnerTests(unittest.TestCase):
    def test_init_maa_sdk_isolated_user_dir_and_cache_fallback(self):
        calls = {}

        class _FakeAsst:
            @staticmethod
            def load(path, incremental_path=None, user_dir=None):
                calls["path"] = Path(path)
                calls["incremental_path"] = Path(incremental_path) if incremental_path else None
                calls["user_dir"] = Path(user_dir)
                return True

        fake_asst_pkg = types.ModuleType("asst")
        fake_asst_mod = types.ModuleType("asst.asst")
        fake_asst_mod.Asst = _FakeAsst
        fake_utils_mod = types.ModuleType("asst.utils")
        fake_utils_mod.InstanceOptionType = SimpleNamespace(touch_type=2, deployment_with_pause=3)
        fake_utils_mod.Message = SimpleNamespace()

        with tempfile.TemporaryDirectory() as tmp:
            maa = Path(tmp) / "ext_maa"
            base = Path(tmp) / "engine"
            (maa / "Python" / "asst").mkdir(parents=True)
            (maa / "cache" / "resource").mkdir(parents=True)
            base.mkdir(parents=True)

            with patch.dict(sys.modules, {"asst": fake_asst_pkg, "asst.asst": fake_asst_mod, "asst.utils": fake_utils_mod}), \
                 patch.object(maa_runner, "_LOADED_MAA_DIR", None):
                ok = maa_runner.init_maa_sdk(maa, base_dir=base, use_cache_incremental=True)
                self.assertTrue(ok)
                self.assertEqual(calls["path"], maa)
                self.assertEqual(calls["user_dir"], base)
                # Cold start: base/cache/resource missing -> falls back to maa/cache
                self.assertEqual(calls["incremental_path"], maa / "cache")

            (base / "cache" / "resource").mkdir(parents=True)
            with patch.dict(sys.modules, {"asst": fake_asst_pkg, "asst.asst": fake_asst_mod, "asst.utils": fake_utils_mod}), \
                 patch.object(maa_runner, "_LOADED_MAA_DIR", None):
                ok2 = maa_runner.init_maa_sdk(maa, base_dir=base, use_cache_incremental=True)
                self.assertTrue(ok2)
                self.assertEqual(calls["incremental_path"], base / "cache")

    def test_ensure_game_running_official_vs_bilibili(self):
        for server, expected_pkg in (("Official", "com.hypergryph.arknights"), ("Bilibili", "com.hypergryph.arknights.bilibili")):
            cfg = {"server": server, "emulator": {"adb_path": "adb.exe", "adb_address": "127.0.0.1:16384"}}
            with patch.object(maa_runner, "ensure_emulator_ready", return_value={"ready": True, "adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                 patch.object(maa_runner, "_run_cmd", side_effect=[SimpleNamespace(returncode=0, stdout="", stderr=""), SimpleNamespace(returncode=0, stdout="", stderr="")]) as mock_cmd:
                pkg = maa_runner.ensure_game_running(cfg, wait_after_launch=0)
                self.assertEqual(pkg, expected_pkg)
                launch_cmd = mock_cmd.call_args_list[1][0][0]
                self.assertIn(f"{expected_pkg}/com.u8.sdk.U8UnityContext", launch_cmd)

    def test_execute_post_action_none_close_emulator_and_shutdown(self):
        none_res = maa_runner.execute_post_action("none", {})
        self.assertFalse(none_res["executed"])

        with patch.object(maa_runner, "_run_cmd", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as mock_cmd:
            shut_dry = maa_runner.execute_post_action("shutdown", {}, dry_run=True)
            self.assertTrue(shut_dry["dry_run"])
            self.assertIn("shutdown", shut_dry["command"])
            mock_cmd.assert_not_called()

            emu_cfg = {"emulator": {"adb_path": "adb.exe", "adb_address": "127.0.0.1:5555"}}
            close_dry = maa_runner.execute_post_action("close_emulator", emu_cfg, dry_run=True)
            self.assertEqual(close_dry["action"], "close_emulator")
            self.assertTrue(close_dry["dry_run"])

    def test_run_task_with_recovery_disconnect_vs_stage_error(self):
        bad_conn_res = {
            "ok": False, "timed_out": False, "elapsed": 1.0,
            "events": [{"message": "ConnectionInfo", "details": {"what": "Disconnect"}}],
            "chain_errors": [], "sub_errors": [], "log_lines": [],
        }
        ok_res = {
            "ok": True, "timed_out": False, "elapsed": 2.0,
            "events": [{"message": "TaskChainCompleted", "details": {"taskchain": "Fight"}}],
            "chain_errors": [], "sub_errors": [], "log_lines": [],
        }
        sess1 = MagicMock()
        sess1.run_single_task.return_value = bad_conn_res
        sess2 = MagicMock()
        sess2.connect.return_value = True
        sess2.run_single_task.side_effect = [ok_res, ok_res]  # StartUp + retried Fight
        holder = {"session": sess1}

        with patch.object(maa_runner, "ensure_game_running"), \
             patch.object(maa_runner, "MaaSession", return_value=sess2):
            res = maa_runner.run_task_with_recovery(holder, "Fight", {"stage": "1-7"}, {"server": "Official"}, max_retries=1)
            self.assertTrue(res["ok"])
            self.assertIs(holder["session"], sess2)

        # Business error (UnableToAgent) does NOT trigger session rebuild
        stage_err_res = {
            "ok": False, "timed_out": False, "elapsed": 1.0,
            "events": [{"message": "SubTaskError", "details": {"first": ["FightBegin"]}}],
            "chain_errors": [], "sub_errors": [{}], "log_lines": ["UnableToAgent"],
        }
        sess3 = MagicMock()
        sess3.run_single_task.return_value = stage_err_res
        holder2 = {"session": sess3}
        res2 = maa_runner.run_task_with_recovery(holder2, "Fight", {"stage": "TL-9"}, {"server": "Official"}, max_retries=1)
        self.assertFalse(res2["ok"])
        self.assertEqual(sess3.run_single_task.call_count, 1)

    def test_ensure_emulator_ready_fails_fast_when_adb_missing(self):
        cfg = {"emulator": {"type": "auto", "path": "", "adb_path": "", "adb_address": "", "auto_launch": True}}
        with patch.object(maa_runner, "discover_adb_executable", return_value=None), \
             patch("shutil.which", return_value=None), \
             patch("time.sleep") as mock_sleep:
            with self.assertRaises(RuntimeError) as ctx:
                maa_runner.ensure_emulator_ready(cfg, timeout_sec=120)
            self.assertIn("adb", str(ctx.exception).lower())
            mock_sleep.assert_not_called()

    def test_autonomous_clear_stage_entry_failure_forbids_agent_task_and_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-9", "stageId": "act55side_09", "apCost": 21}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({"total_owned": 1, "e2_count": 1, "e2_operators": [{"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}], "all_owned": {"棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6}}}),
                encoding="utf-8",
            )
            guides = [{
                "id": 9001,
                "like": 100,
                "views": 2000,
                "content": {
                    "stage_name": "act55side_09",
                    "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}],
                    "groups": [],
                    "actions": [{"type": "Deploy", "name": "棘刺", "location": [3, 4], "costs": 18}],
                },
            }]
            entry_fail_report = {
                "round": 1,
                "victory": False,
                "elapsed_seconds": 22.0,
                "metrics": {
                    "kills": None,
                    "costs": None,
                    "last_3_actions": [],
                    "errors": ["SubTaskError:ProcessTask:BattleStartPre", "SubTaskError:ProcessTask:BattleStartAll"],
                    "failed_flag": False,
                    "completed_flag": False,
                    "stage_entry_failed": True,
                    "entry_failed_tasks": ["BattleStartPre", "BattleStartAll"],
                },
                "events_tail": [
                    {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartPre"]}},
                    {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartAll"]}},
                ],
                "log_tail": ['SubTaskStart {"subtask":"BattleFormationTask"}'],
                "screenshot": "",
                "report_path": "",
            }
            collab_calls = []

            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", return_value=(1, entry_fail_report)) as mock_round:
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-9",
                    maa_dir=maa,
                    config={"ai": {"mode": "agent_collab", "enabled": False}},
                    max_rounds=5,
                    base_dir=base,
                    collab_handler=collab_calls.append,
                )
            self.assertFalse(ok)
            self.assertTrue(summary.get("stage_entry_failed"))
            self.assertEqual(mock_round.call_count, 1)
            self.assertEqual(len(collab_calls), 0)
            self.assertFalse((base / "agent_task.json").exists())

    def test_autonomous_clear_stage_daily_mode_aborts_tutorial_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            tut_calls = []
            with patch.object(maa_runner.stage_pilot, "wait_for_tutorial_completion") as mock_wait, \
                 patch.object(maa_runner, "fetch_prts_guides") as mock_fetch:
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-TR-1",
                    maa_dir=maa,
                    config={},
                    base_dir=base,
                    tutorial_handler=tut_calls.append,
                    daily_mode=True,
                )
            self.assertFalse(ok)
            self.assertTrue(summary.get("blocked_by_tutorial"))
            mock_wait.assert_not_called()
            mock_fetch.assert_not_called()
            self.assertEqual(len(tut_calls), 0)

    def test_session_early_stop_does_not_mark_user_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (base / "debug").mkdir(parents=True)
            maa.mkdir(parents=True)

            sess = maa_runner.MaaSession(maa_dir=maa, config={}, base_dir=base)
            fake_asst = MagicMock()
            fake_asst.append_task.return_value = 7
            fake_asst.start.return_value = True
            fake_asst.running.side_effect = [True, True, False]
            sess._asst = fake_asst

            def _tick(s, _evs):
                s.request_early_stop("stage_not_found")

            with patch("time.sleep"):
                res = sess.run_single_task("Fight", {"stage": "TL-9"}, timeout_sec=60, tick_callback=_tick)
            self.assertFalse(res["ok"])
            self.assertFalse(res["cancelled"])
            self.assertTrue(res["early_stopped"])
            self.assertEqual(res["early_stop_reason"], "stage_not_found")
            fake_asst.stop.assert_called_once()

    def test_formation_unavailable_self_heal_substitute_and_win_without_agent_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-2", "stageId": "act55side_02", "apCost": 9}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({
                    "total_owned": 5,
                    "e2_count": 3,
                    "e2_operators": [
                        {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                        {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                        {"name": "忍冬", "elite": 2, "level": 60, "rarity": 6},
                    ],
                    "all_owned": {
                        "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                        "德克萨斯": {"name": "德克萨斯", "elite": 1, "level": 50, "rarity": 5},
                        "极境": {"name": "极境", "elite": 0, "level": 50, "rarity": 5},
                        "桃金娘": {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                        "忍冬": {"name": "忍冬", "elite": 2, "level": 60, "rarity": 6},
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            guides = [{
                "id": 108603,
                "like": 120,
                "views": 3000,
                "content": {
                    "stage_name": "act55side_02",
                    "opers": [
                        {"name": "棘刺", "skill": 3, "skill_usage": 1, "requirements": {"elite": 2, "level": 1, "skill_level": 7}},
                        {"name": "德克萨斯", "skill": 1, "requirements": {"elite": 1, "level": 40, "skill_level": 7}},
                    ],
                    "groups": [],
                    "actions": [
                        {"type": "Deploy", "name": "棘刺", "location": [7, 3], "direction": "Down"},
                        {"type": "Deploy", "name": "德克萨斯", "location": [8, 2], "direction": "Down"},
                    ],
                },
            }]

            written_copilots = []
            seen_ignore_reqs = []

            def _fake_round(**kwargs):
                cop_data = json.loads(Path(kwargs["copilot_file"]).read_text(encoding="utf-8"))
                written_copilots.append(cop_data)
                seen_ignore_reqs.append(bool(kwargs.get("ignore_requirements")))
                if kwargs["round_idx"] == 1:
                    return 1, {
                        "round": 1,
                        "victory": False,
                        "elapsed_seconds": 19.5,
                        "metrics": {
                            "kills": None,
                            "costs": None,
                            "last_3_actions": [],
                            "errors": [
                                'SubTaskError:BattleFormationTask:{"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}}',
                                "SubTaskError:BattleFormationTask:{}",
                                "TaskChainError::{}",
                                "SubTaskError:ProcessTask:BattleStartPre",
                            ],
                            "failed_flag": False,
                            "completed_flag": False,
                            "entry_failed_tasks": ["BattleStartPre"],
                        },
                        "events_tail": [
                            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartPre"]}},
                            {
                                "message": "SubTaskExtraInfo",
                                "details": {
                                    "subtask": "BattleFormationTask",
                                    "what": "BattleFormationOperUnavailable",
                                    "details": {"oper_name": "德克萨斯", "requirement_type": "skill_level"},
                                },
                            },
                            {
                                "message": "SubTaskError",
                                "details": {
                                    "subtask": "BattleFormationTask",
                                    "why": "OperatorMissing",
                                    "details": {"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}},
                                },
                            },
                        ],
                        "log_tail": [],
                        "screenshot": str(base / "reports" / "push_TL-2_fail_round_1.png"),
                        "report_path": str(base / "reports" / "push_TL-2_round_1_report.json"),
                    }
                if kwargs["round_idx"] == 2:
                    # Simulate relaxed 德克萨斯 still unavailable -> triggers same-branch substitution to 忍冬 (NOT 桃金娘)
                    return 1, {
                        "round": 2,
                        "victory": False,
                        "elapsed_seconds": 18.0,
                        "metrics": {
                            "kills": None,
                            "costs": None,
                            "last_3_actions": [],
                            "errors": [
                                'SubTaskError:BattleFormationTask:{"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}}',
                            ],
                            "failed_flag": False,
                            "completed_flag": False,
                            "entry_failed_tasks": [],
                        },
                        "events_tail": [],
                        "log_tail": [],
                        "screenshot": str(base / "reports" / "push_TL-2_fail_round_2.png"),
                        "report_path": str(base / "reports" / "push_TL-2_round_2_report.json"),
                    }
                return 0, {
                    "round": 3,
                    "victory": True,
                    "elapsed_seconds": 42.0,
                    "metrics": {"kills": 30, "costs": 20, "last_3_actions": [], "errors": [], "failed_flag": False, "completed_flag": True},
                    "screenshot": "",
                    "report_path": "",
                }

            collab_calls = []
            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=_fake_round):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-2",
                    maa_dir=maa,
                    config={"ai": {"mode": "agent_collab", "enabled": False}},
                    max_rounds=5,
                    base_dir=base,
                    collab_handler=collab_calls.append,
                )

            self.assertTrue(ok)
            self.assertTrue(summary["victory"])
            self.assertFalse(summary.get("stage_entry_failed", False))
            self.assertEqual(len(written_copilots), 3)
            # Round 2 first relaxed 德克萨斯 skill_level + enabled ignore_requirements
            self.assertEqual([o["name"] for o in written_copilots[1]["opers"]], ["棘刺", "德克萨斯"])
            self.assertEqual(written_copilots[1]["opers"][1]["requirements"]["skill_level"], 1)
            self.assertTrue(seen_ignore_reqs[1])
            # Round 3 substituted 德克萨斯 -> same-branch blocking vanguard 忍冬 (strictly forbidding 0-block 桃金娘!)
            self.assertEqual([o["name"] for o in written_copilots[2]["opers"]], ["棘刺", "忍冬"])
            self.assertEqual([a["name"] for a in written_copilots[2]["actions"]], ["棘刺", "忍冬"])
            # Zero external agent interruption when self-healing succeeded
            self.assertEqual(len(collab_calls), 0)
            self.assertFalse((base / "agent_task.json").exists())

    def test_formation_unavailable_switches_to_clean_prts_guide_avoiding_blacklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-2", "stageId": "act55side_02", "apCost": 9}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            # No other Vanguard in Box besides 德克萨斯 -> same-guide substitution is infeasible, must switch PRTS guide!
            (base / "data" / "my_box.json").write_text(
                json.dumps({
                    "total_owned": 3,
                    "e2_count": 2,
                    "e2_operators": [
                        {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                        {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
                    ],
                    "all_owned": {
                        "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                        "维什戴尔": {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
                        "德克萨斯": {"name": "德克萨斯", "elite": 1, "level": 50, "rarity": 5},
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            guides = [
                {
                    "id": 108603, "like": 150, "views": 3000,
                    "content": {
                        "stage_name": "act55side_02",
                        "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}, {"name": "德克萨斯", "skill": 1}],
                        "groups": [],
                        "actions": [{"type": "Deploy", "name": "棘刺"}, {"type": "Deploy", "name": "德克萨斯"}],
                    },
                },
                {
                    "id": 108604, "like": 110, "views": 2500,
                    "content": {
                        "stage_name": "act55side_02",
                        "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}, {"name": "德克萨斯", "skill": 2}, {"name": "维什戴尔", "skill": 3}],
                        "groups": [],
                        "actions": [{"type": "Deploy", "name": "德克萨斯"}],
                    },
                },
                {
                    "id": 108605, "like": 80, "views": 2000,
                    "content": {
                        "stage_name": "act55side_02",
                        "opers": [{"name": "棘刺", "skill": 3, "skill_usage": 1}, {"name": "维什戴尔", "skill": 3, "skill_usage": 1}],
                        "groups": [],
                        "actions": [{"type": "Deploy", "name": "棘刺"}, {"type": "Deploy", "name": "维什戴尔"}],
                    },
                },
            ]
            seen_ops = []

            def _fake_round(**kwargs):
                cop = json.loads(Path(kwargs["copilot_file"]).read_text(encoding="utf-8"))
                ops = [o["name"] for o in cop["opers"]]
                seen_ops.append(ops)
                if "德克萨斯" in ops:
                    return 1, {
                        "round": kwargs["round_idx"],
                        "victory": False,
                        "metrics": {
                            "kills": None,
                            "errors": [
                                'SubTaskError:BattleFormationTask:{"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}}',
                                "SubTaskError:ProcessTask:BattleStartPre",
                            ],
                            "failed_flag": False,
                            "completed_flag": False,
                            "entry_failed_tasks": ["BattleStartPre"],
                        },
                        "events_tail": [],
                        "log_tail": [],
                        "screenshot": "",
                        "report_path": "",
                    }
                return 0, {
                    "round": kwargs["round_idx"],
                    "victory": True,
                    "metrics": {"kills": 25, "costs": 15, "last_3_actions": [], "errors": [], "failed_flag": False, "completed_flag": True},
                    "screenshot": "",
                    "report_path": "",
                }

            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=_fake_round):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-2", maa_dir=maa, config={"ai": {"mode": "agent_collab", "enabled": False}}, max_rounds=5, base_dir=base
                )

            self.assertTrue(ok)
            self.assertEqual(summary["final_guide_id"], 108605)
            self.assertEqual(seen_ops, [["棘刺", "德克萨斯"], ["棘刺", "维什戴尔"]])
            self.assertFalse((base / "agent_task.json").exists())

    def test_formation_unavailable_exhausted_writes_agent_task_with_reason_and_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            (maa / "resource").mkdir(parents=True)
            (maa / "resource" / "stages.json").write_text(
                json.dumps([{"code": "TL-2", "stageId": "act55side_02", "apCost": 9}]), encoding="utf-8"
            )
            (base / "data").mkdir(parents=True)
            (base / "data" / "my_box.json").write_text(
                json.dumps({
                    "total_owned": 4,
                    "e2_count": 3,
                    "e2_operators": [
                        {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                        {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                        {"name": "忍冬", "elite": 2, "level": 60, "rarity": 6},
                    ],
                    "all_owned": {
                        "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                        "德克萨斯": {"name": "德克萨斯", "elite": 1, "level": 50, "rarity": 5},
                        "桃金娘": {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
                        "忍冬": {"name": "忍冬", "elite": 2, "level": 60, "rarity": 6},
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            guides = [{
                "id": 108603, "like": 120, "views": 3000,
                "content": {
                    "stage_name": "act55side_02",
                    "opers": [{"name": "棘刺", "skill": 3}, {"name": "德克萨斯", "skill": 1}],
                    "groups": [],
                    "actions": [{"type": "Deploy", "name": "棘刺"}, {"type": "Deploy", "name": "德克萨斯"}],
                },
            }]
            fail_shot = str(base / "reports" / "push_TL-2_fail_round_2.png")

            def _always_form_fail(**kwargs):
                cop = json.loads(Path(kwargs["copilot_file"]).read_text(encoding="utf-8"))
                vg_name = cop["opers"][1]["name"]
                return 1, {
                    "round": kwargs["round_idx"],
                    "victory": False,
                    "metrics": {
                        "kills": None,
                        "errors": [
                            f'SubTaskError:BattleFormationTask:{{"opers": {{"{vg_name}": [{{"name": "{vg_name}", "reason": "Unavailable"}}]}}}}',
                            "SubTaskError:ProcessTask:BattleStartPre",
                        ],
                        "failed_flag": False,
                        "completed_flag": False,
                        "entry_failed_tasks": ["BattleStartPre"],
                    },
                    "events_tail": [],
                    "log_tail": [],
                    "screenshot": fail_shot,
                    "report_path": str(base / "reports" / f"push_TL-2_round_{kwargs['round_idx']}_report.json"),
                }

            collab_payloads = []
            with patch.object(maa_runner, "fetch_prts_guides", return_value=guides), \
                 patch.object(maa_runner, "_run_copilot_round", side_effect=_always_form_fail):
                ok, summary = maa_runner.autonomous_clear_stage(
                    "TL-2",
                    maa_dir=maa,
                    config={"ai": {"mode": "agent_collab", "enabled": False}},
                    max_rounds=5,
                    base_dir=base,
                    collab_handler=collab_payloads.append,
                )

            self.assertFalse(ok)
            self.assertTrue(summary["formation_unavailable"])
            self.assertFalse(summary["stage_entry_failed"])
            self.assertEqual(summary["error"], "formation_unavailable")
            # Must ONLY try same-branch blocking vanguard 忍冬 and NEVER cross-branch to 0-block 桃金娘
            self.assertEqual(summary["unavailable_opers"], ["德克萨斯", "忍冬"])
            self.assertEqual(summary["rounds_used"], 2)
            self.assertEqual(len(collab_payloads), 1)

            task_file = base / "agent_task.json"
            self.assertTrue(task_file.is_file())
            task_data = json.loads(task_file.read_text(encoding="utf-8"))
            self.assertEqual(task_data["reason"], "formation_unavailable")
            self.assertEqual(task_data["trigger_reason"], "formation_unavailable")
            self.assertEqual(task_data["unavailable_opers"], ["德克萨斯", "忍冬"])
            self.assertEqual(task_data["screenshot_path"], fail_shot)
            self.assertTrue(task_data["target_copilot_path"].endswith("push_TL-2_r2.json"))
            self.assertEqual(task_data["battle_state"]["unavailable_opers"], ["德克萨斯", "忍冬"])

    def test_prepare_battle_entry_exits_formation_select_before_retry(self):
        from PIL import Image
        dummy = Image.new("RGB", (1280, 720), (20, 20, 20))
        taps = []
        states = [
            {"in_formation": False, "in_battle": False, "in_formation_select": True, "return_xy": (89, 38), "has_skip_button": False, "has_skip_confirm": False, "has_story_dialog": False, "on_stage_map": False, "has_stage_start_btn": False},
            {"in_formation": True, "in_battle": False, "in_formation_select": False, "return_xy": (89, 38)},
        ]
        with patch.object(maa_runner.stage_pilot, "adb_screencap_image", return_value=dummy), \
             patch.object(maa_runner.stage_pilot, "analyze_screen_state", side_effect=states), \
             patch.object(maa_runner.stage_pilot, "adb_tap_norm", side_effect=lambda a, b, x, y, w, h: taps.append((x, y)) or True), \
             patch("time.sleep"):
            res = maa_runner.stage_pilot.prepare_battle_entry_if_on_map("TL-2", "adb.exe", "127.0.0.1:16384", Path("."))
        self.assertTrue(res["handled"])
        self.assertTrue(res["entered"])
        self.assertTrue(res["exited_formation_select"])
        self.assertEqual(res["outcome"], "formation")
        self.assertEqual(taps, [(89, 38)])

    def test_master_scheduler_preserves_formation_unavailable_on_chain_abort(self):
        from universal_engine import master_scheduler
        from universal_engine.utils import env_doctor

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            maa = Path(tmp) / "maa"
            maa.mkdir(parents=True)
            cfg_path = base / "config" / "launcher_config.json"
            env_doctor.save_launcher_config({"first_run_done": True, "ai": {"mode": "agent_collab", "enabled": False}}, cfg_path)
            task_file, _ = maa_runner.write_agent_task(
                base_dir=base,
                trigger_reason="formation_unavailable",
                stage_code="TL-2",
                stage_id="act55side_02",
                round_idx=2,
                max_rounds=5,
                target_copilot_path=base / "copilots" / "push_TL-2_r2.json",
                screenshot_path=str(base / "reports" / "push_TL-2_fail_round_2.png"),
                unavailable_opers=["德克萨斯"],
                battle_state={"formation_unavailable": True, "unavailable_opers": ["德克萨斯"]},
            )
            fake_summary = {
                "stage": "TL-2",
                "stage_id": "act55side_02",
                "victory": False,
                "stage_entry_failed": False,
                "formation_unavailable": True,
                "unavailable_opers": ["德克萨斯"],
                "copilot_file": str(base / "copilots" / "push_TL-2_r2.json"),
                "screenshot": str(base / "reports" / "push_TL-2_fail_round_2.png"),
                "rounds_used": 2,
                "history": [],
                "error": "formation_unavailable",
                "agent_task_written": True,
                "agent_task_path": str(task_file),
            }
            with patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch.object(master_scheduler, "get_base_dir", return_value=base):
                eng = master_scheduler.MaaEngine(config_path=cfg_path, launch_source="cli")
                with patch.object(eng, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(maa)}), \
                     patch.object(eng, "refresh_cache"), \
                     patch("universal_engine.master_scheduler.ensure_emulator_ready", return_value={"adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                     patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                     patch("universal_engine.master_scheduler.autonomous_clear_stage", return_value=(False, fake_summary)):
                    res = eng.run_push(["TL-2", "TL-3"], refresh_cache_flag=False)

            self.assertFalse(res["ok"])
            self.assertEqual(res.get("abort_reason"), "formation_unavailable")
            saved_task = json.loads(task_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_task["trigger_reason"], "formation_unavailable")
            self.assertEqual(saved_task["reason"], "formation_unavailable")
            self.assertEqual(saved_task["unavailable_opers"], ["德克萨斯"])
            self.assertEqual(saved_task["battle_state"]["remaining_stages"], ["TL-3"])


if __name__ == "__main__":
    unittest.main()
