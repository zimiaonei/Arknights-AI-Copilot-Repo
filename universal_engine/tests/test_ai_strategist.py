import json
import unittest
from unittest.mock import patch

from universal_engine import ai_strategist


class _JsonResp:
    def __init__(self, payload: dict):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class AiStrategistTests(unittest.TestCase):
    def setUp(self):
        self.my_box = {
            "e2_operators": [
                {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
                {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                {"name": "桃金娘", "elite": 2, "level": 30, "rarity": 4},
            ],
            "all_owned": {
                "维什戴尔": {"name": "维什戴尔", "elite": 2, "level": 90, "rarity": 6},
                "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                "桃金娘": {"name": "桃金娘", "elite": 2, "level": 30, "rarity": 4},
            },
        }
        self.guide = {
            "id": 1001,
            "like": 50,
            "views": 5000,
            "content": {
                "stage_name": "TL-9",
                "opers": [
                    {"name": "桃金娘", "skill": 1, "skill_usage": 1, "requirements": {"elite": 2, "level": 70}},
                    {"name": "山", "skill": 2, "skill_usage": 1, "requirements": {"elite": 2, "level": 60}},
                ],
                "groups": [],
                "actions": [
                    {"type": "Deploy", "name": "山", "location": [3, 4]},
                    {"type": "Retreat", "name": "魂灵之影"},
                ],
            },
        }

    def test_client_configuration_endpoint_and_fenced_json(self):
        self.assertFalse(ai_strategist.OpenAICompatClient(api_key="").is_configured())
        client = ai_strategist.OpenAICompatClient(
            base_url="http://127.0.0.1:11434/v1",
            api_key="ollama",
            model_name="qwen2.5",
        )
        self.assertTrue(client.is_configured())
        self.assertEqual(client.endpoint_url(), "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(
            ai_strategist.OpenAICompatClient(base_url="https://api.deepseek.com").endpoint_url(),
            "https://api.deepseek.com/v1/chat/completions",
        )

        body = {"choices": [{"message": {"content": "```json\n{\"substitutions\": {\"山\": \"棘刺\"}}\n```"}}]}
        with patch("urllib.request.urlopen", return_value=_JsonResp(body)):
            parsed = client.chat_json("sys", "usr")
            self.assertEqual(parsed, {"substitutions": {"山": "棘刺"}})

        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            self.assertIsNone(client.chat_json("sys", "usr"))

        bad_body = {"choices": [{"message": {"content": "not valid json"}}]}
        with patch("urllib.request.urlopen", return_value=_JsonResp(bad_body)):
            self.assertIsNone(client.chat_json("sys", "usr"))

    def test_adapt_copilot_zero_key_ai_assisted_and_hallucination_fallback(self):
        res_no_key = ai_strategist.adapt_copilot(
            self.guide, self.my_box, "TL-9", client=ai_strategist.OpenAICompatClient()
        )
        self.assertEqual(res_no_key["substitutions"].get("山"), "棘刺")
        self.assertEqual(res_no_key["mode"], "pure_maa")
        self.assertTrue(all(a.get("name") != "魂灵之影" for a in res_no_key["copilot"]["actions"]))

        # AI suggests valid owned operator 维什戴尔 (when 山 missing) -> applied and mode="ai_assisted"
        client = ai_strategist.OpenAICompatClient(api_key="sk-test")
        with patch.object(
            client,
            "chat_json",
            return_value={"substitutions": {"山": "棘刺"}, "skill_overrides": {"棘刺": {"skill": 3, "skill_usage": 1}}},
        ):
            res_ai = ai_strategist.adapt_copilot(self.guide, self.my_box, "TL-9", client=client)
            self.assertEqual(res_ai["mode"], "ai_assisted")
            replaced = [o for o in res_ai["copilot"]["opers"] if o["name"] == "棘刺"][0]
            self.assertEqual(replaced["skill"], 3)

        # LLM returning unowned operator falls back safely to pure_maa_fallback
        with patch.object(client, "chat_json", return_value={"substitutions": {"山": "不存在干员"}}):
            res_bad_ai = ai_strategist.adapt_copilot(self.guide, self.my_box, "TL-9", client=client)
            self.assertEqual(res_bad_ai["substitutions"].get("山"), "棘刺")
            self.assertEqual(res_bad_ai["mode"], "pure_maa_fallback")

    def test_repair_copilot_rule_baseline_and_ai_patch(self):
        cands = [
            {"guide_id": 1001, "copilot": self.guide["content"]},
            {"guide_id": 1002, "copilot": self.guide["content"]},
        ]
        report = {
            "metrics": {"kills": 5, "costs": 10, "last_3_actions": ["BulletTime"], "errors": [], "failed_flag": False},
            "log_tail": [],
        }
        cur = {"opers": [{"name": "棘刺", "skill_usage": 0}], "actions": [{"type": "BulletTime", "costs": 12}]}
        idx, rep, strat = ai_strategist.repair_copilot(cands, 0, cur, report, self.my_box, round_idx=1, client=None)
        self.assertEqual(idx, 0)
        self.assertEqual(rep["actions"][0]["costs"], 9)
        self.assertEqual(strat["deployment_with_pause"], "1")
        self.assertEqual(strat["mode"], "pure_maa")

        # AI repair patch adjusts skill_usage safely
        client = ai_strategist.OpenAICompatClient(api_key="sk-test")
        fail_report = {"metrics": {"kills": 10, "costs": 20, "last_3_actions": ["Deploy:棘刺"], "errors": ["Failed"], "failed_flag": True}, "log_tail": ["err"]}
        with patch.object(client, "chat_json", return_value={
            "deployment_with_pause": "1",
            "ignore_requirements": True,
            "skill_usage_overrides": {"棘刺": 1},
            "reason": "AI调整棘刺自动开技能",
        }):
            idx2, rep2, strat2 = ai_strategist.repair_copilot(cands, 0, cur, fail_report, self.my_box, round_idx=1, client=client)
            self.assertEqual(idx2, 0)
            self.assertEqual(strat2["mode"], "ai_assisted")
            self.assertEqual(rep2["opers"][0]["skill_usage"], 1)
            self.assertTrue(strat2["ignore_requirements"])

    def test_from_config_respects_enabled_flag_and_rejects_malformed_llm_types(self):
        # ai.enabled=False must disable LLM even when api_key is present
        c_disabled = ai_strategist.OpenAICompatClient.from_config({
            "ai": {"enabled": False, "api_key": "sk-secret", "base_url": "https://api.openai.com/v1"}
        })
        self.assertFalse(c_disabled.is_configured())

        c_enabled = ai_strategist.OpenAICompatClient.from_config({
            "ai": {"enabled": True, "api_key": "sk-secret", "base_url": "https://api.openai.com/v1"}
        })
        self.assertTrue(c_enabled.is_configured())

        # Non-int skill / bool / null index in LLM response must fall back instead of raising
        with patch.object(c_enabled, "chat_json", return_value={
            "substitutions": {"山": "棘刺"},
            "skill_overrides": {"棘刺": {"skill": "三技能", "skill_usage": True}},
        }):
            res = ai_strategist.adapt_copilot(self.guide, self.my_box, "TL-9", client=c_enabled)
            self.assertEqual(res["mode"], "pure_maa_fallback")

        # Duplicate operator across guide opers must fall back cleanly
        dup_guide = {
            "id": 1002,
            "content": {
                "stage_name": "TL-9",
                "opers": [
                    {"name": "棘刺", "skill": 3, "skill_usage": 1},
                    {"name": "山", "skill": 2, "skill_usage": 1},
                ],
                "groups": [],
                "actions": [{"type": "Deploy", "name": "山"}],
            },
        }
        with patch.object(c_enabled, "chat_json", return_value={"substitutions": {"山": "棘刺"}, "skill_overrides": {}}):
            res_dup = ai_strategist.adapt_copilot(dup_guide, self.my_box, "TL-9", client=c_enabled)
            self.assertEqual(res_dup["mode"], "pure_maa_fallback")

        cands = [{"guide_id": 1001, "copilot": self.guide["content"]}]
        report = {"metrics": {"kills": 5, "costs": 10, "last_3_actions": [], "errors": [], "failed_flag": True}, "log_tail": []}
        cur = {"opers": [{"name": "棘刺", "skill_usage": 0}], "actions": [{"type": "BulletTime", "costs": 12}]}
        with patch.object(c_enabled, "chat_json", return_value={"reason": "bad", "action_patches": [{"index": None}]}):
            _, _, strat = ai_strategist.repair_copilot(cands, 0, cur, report, self.my_box, round_idx=1, client=c_enabled)
            self.assertEqual(strat["mode"], "pure_maa_fallback")

    def test_stage_entry_failure_distinguished_from_battle_failure(self):
        # Exact event sequence observed when Copilot runs on an unlocked/missing stage (TL-9 while map is at TL-2)
        entry_events = [
            {"message": "TaskChainStart", "details": {"taskchain": "Copilot"}},
            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartPre"], "details": {}}},
            {"message": "SubTaskStart", "details": {"subtask": "BattleFormationTask", "details": {}}},
            {"message": "SubTaskCompleted", "details": {"subtask": "BattleFormationTask", "details": {}}},
            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartAll"], "details": {}}},
            {"message": "TaskChainError", "details": {"taskchain": "Copilot"}},
        ]
        entry_logs = [
            'Assistant::append_callback | SubTaskStart {"subtask":"BattleFormationTask"}',
            'Assistant::append_callback | SubTaskError {"first":["BattleStartAll"],"subtask":"ProcessTask"}',
        ]
        m = ai_strategist.parse_battle_metrics(entry_logs, entry_events)
        self.assertIsNone(m["kills"])
        self.assertTrue(m["stage_entry_failed"])
        self.assertIn("BattleStartPre", m["entry_failed_tasks"])
        self.assertIn("BattleStartAll", m["entry_failed_tasks"])
        self.assertTrue(ai_strategist.is_stage_entry_failure({"metrics": m, "log_tail": entry_logs}))

        # repair_copilot MUST abort retry without touching LLM or falsely matching BattleFormation
        cands = [{"guide_id": 1001, "copilot": self.guide["content"]}, {"guide_id": 1002, "copilot": self.guide["content"]}]
        client = ai_strategist.OpenAICompatClient(api_key="sk-test")
        with patch.object(client, "chat_json") as mock_llm:
            idx, _, strat = ai_strategist.repair_copilot(
                cands, 0, self.guide["content"], {"metrics": m, "log_tail": entry_logs}, self.my_box, round_idx=1, client=client
            )
            self.assertEqual(idx, 0)
            self.assertTrue(strat.get("abort_retry"))
            self.assertEqual(strat.get("mode"), "stage_entry_failed")
            self.assertFalse(strat.get("ignore_requirements"))
            mock_llm.assert_not_called()

        # Real battle failure (kills present or FightMissionFailed) is NOT a stage entry failure
        battle_fail_events = [
            {"message": "SubTaskStart", "details": {"subtask": "BattleProcessTask", "details": {}}},
            {"message": "SubTaskExtraInfo", "details": {"what": "Battle", "details": {"kills": 18, "cost": 15}}},
            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartPre"], "details": {}}},
        ]
        m_battle = ai_strategist.parse_battle_metrics(["Copilot@FightMissionFailed"], battle_fail_events)
        self.assertEqual(m_battle["kills"], 18)
        self.assertFalse(m_battle["stage_entry_failed"])
        self.assertFalse(ai_strategist.is_stage_entry_failure({"metrics": m_battle}))

    def test_battle_start_pre_with_formation_unavailable_classified_and_substituted(self):
        # Exact TL-2 failure sequence: stage_pilot already clicked Start -> harmless BattleStartPre ->
        # BattleFormationTask fails on 德克萨斯 (Skill 1 level 6 < require 7 -> Unavailable).
        tl2_events = [
            {"message": "TaskChainStart", "details": {"taskchain": "Copilot"}},
            {"message": "SubTaskError", "details": {"subtask": "ProcessTask", "first": ["BattleStartPre"], "details": {}}},
            {"message": "SubTaskStart", "details": {"subtask": "BattleFormationTask", "details": {}}},
            {"message": "SubTaskStart", "details": {"subtask": "ProcessTask", "details": {"task": "BattleQuickFormationRole-Pioneer"}}},
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
                    "class": "asst::BattleFormationTask",
                    "subtask": "BattleFormationTask",
                    "why": "OperatorMissing",
                    "details": {"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}},
                },
            },
            {"message": "SubTaskError", "details": {"class": "asst::BattleFormationTask", "subtask": "BattleFormationTask", "details": {}}},
            {"message": "TaskChainError", "details": {"taskchain": "Copilot"}},
        ]
        tl2_logs = [
            "[2026-11-21 20:38:44.786][TRC] asst::BattleProcessTask::set_stage_name | enter",
            '[2026-11-21 20:38:47.071][INF] Assistant::append_callback | SubTaskError {"first":["BattleStartPre"],"subtask":"ProcessTask"}',
            "[2026-11-21 20:38:59.006][WRN] asst::BattleFormationTask::check_and_select_skill | Skill 1 level 6 , require: 7",
            '[2026-11-21 20:39:03.216][INF] Assistant::append_callback | SubTaskError {"class":"asst::BattleFormationTask","details":{"opers":{"德克萨斯":[{"name":"德克萨斯","reason":"Unavailable"}]}},"subtask":"BattleFormationTask","why":"OperatorMissing"}',
        ]
        m = ai_strategist.parse_battle_metrics(tl2_logs, tl2_events, entered_formation=True)
        self.assertFalse(m["battle_started"])
        self.assertFalse(m["stage_entry_failed"])
        self.assertTrue(m["formation_unavailable"])
        self.assertEqual(m["unavailable_opers"], ["德克萨斯"])
        self.assertEqual(m["unavailable_oper_reasons"].get("德克萨斯"), "skill_level")
        self.assertIn("BattleStartPre", m["harmless_entry_probes"])
        self.assertNotIn("BattleStartPre", m["entry_failed_tasks"])

        report = {"metrics": m, "events_tail": tl2_events, "log_tail": tl2_logs}
        self.assertTrue(ai_strategist.is_formation_unavailable(report))
        self.assertFalse(ai_strategist.is_stage_entry_failure(report))

        # Even a minimal report with only BattleStartPre + BattleFormationTask error string must NEVER be stage_entry_failed
        minimal_report = {
            "metrics": {
                "kills": None,
                "errors": [
                    'SubTaskError:BattleFormationTask:{"opers": {"德克萨斯": [{"name": "德克萨斯", "reason": "Unavailable"}]}}',
                    "SubTaskError:ProcessTask:BattleStartPre",
                ],
                "entry_failed_tasks": ["BattleStartPre"],
            }
        }
        info_min = ai_strategist.extract_formation_unavailable_info(minimal_report)
        self.assertTrue(info_min["is_formation_unavailable"])
        self.assertEqual(info_min["unavailable_opers"], ["德克萨斯"])
        self.assertFalse(ai_strategist.is_stage_entry_failure(minimal_report))

        # Step 1: When failure is purely skill_level (6 < require 7) on owned 德克萨斯, MUST relax requirements first!
        rich_box = {
            "e2_operators": [
                {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                {"name": "忍冬", "elite": 2, "level": 60, "rarity": 6},
                {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
            ],
            "all_owned": {
                "棘刺": {"name": "棘刺", "elite": 2, "level": 60, "rarity": 6},
                "德克萨斯": {"name": "德克萨斯", "elite": 1, "level": 50, "rarity": 5},
                "极境": {"name": "极境", "elite": 0, "level": 50, "rarity": 5},
                "忍冬": {"name": "忍冬", "elite": 2, "level": 60, "rarity": 6},
                "桃金娘": {"name": "桃金娘", "elite": 2, "level": 60, "rarity": 4},
            },
        }
        tl2_copilot = {
            "stage_name": "act55side_02",
            "opers": [
                {"name": "棘刺", "skill": 3, "skill_usage": 1, "requirements": {"elite": 2, "level": 1, "skill_level": 7}},
                {"name": "德克萨斯", "skill": 1, "requirements": {"elite": 1, "level": 40, "skill_level": 7}},
            ],
            "groups": [],
            "actions": [
                {"name": "棘刺", "type": "Deploy", "location": [7, 3], "direction": "Down"},
                {"name": "德克萨斯", "type": "Deploy", "location": [8, 2], "direction": "Down"},
            ],
        }
        cands = [{"guide_id": 108603, "like": 100, "compat_score": 2500, "copilot": tl2_copilot}]
        idx_rel, relaxed, strat_rel = ai_strategist.repair_copilot(cands, 0, tl2_copilot, report, rich_box, round_idx=1)
        self.assertEqual(idx_rel, 0)
        self.assertTrue(strat_rel.get("formation_unavailable"))
        self.assertEqual(strat_rel.get("repair_action"), "relax_requirements")
        self.assertTrue(strat_rel.get("ignore_requirements"))
        self.assertEqual(strat_rel.get("deployment_with_pause"), "1")
        self.assertEqual([o["name"] for o in relaxed["opers"]], ["棘刺", "德克萨斯"])
        self.assertEqual(relaxed["opers"][1]["requirements"]["skill_level"], 1)

        # Step 2: Once relaxation is already tried (or op genuinely unavailable), MUST substitute strictly by same-branch
        # blocking vanguard (E2 Lv60 忍冬), and STRICTLY FORBID 0-block flagbearers (桃金娘 / 极境)!
        idx_sub, patched, strat_sub = ai_strategist.repair_copilot(
            cands, 0, relaxed, report, rich_box, round_idx=2, same_guide_relax_attempts=1
        )
        self.assertEqual(idx_sub, 0)
        self.assertEqual(strat_sub.get("repair_action"), "substitute_operator")
        self.assertEqual(strat_sub.get("deployment_with_pause"), "1")
        self.assertEqual(strat_sub.get("substitutions"), {"德克萨斯": "忍冬"})
        self.assertEqual([o["name"] for o in patched["opers"]], ["棘刺", "忍冬"])
        self.assertEqual([a["name"] for a in patched["actions"]], ["棘刺", "忍冬"])
        self.assertEqual(patched["opers"][1]["requirements"]["skill_level"], 1)

        # Even if 忍冬 is blacklisted, 0-block flagbearers (桃金娘/极境) MUST NOT be chosen for blocking pioneer 德克萨斯!
        rep_forbidden = ai_strategist.pick_best_substitute("德克萨斯", rich_box, blacklist=["德克萨斯", "忍冬"])
        self.assertIsNone(rep_forbidden)


if __name__ == "__main__":
    unittest.main()
