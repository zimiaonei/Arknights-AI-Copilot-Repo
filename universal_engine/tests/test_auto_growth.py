import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from universal_engine import auto_growth


def _setup_mock_maa_and_base(tmp_path: Path):
    maa = tmp_path / "maa"
    base = tmp_path / "engine"
    (maa / "resource").mkdir(parents=True)
    (maa / "data").mkdir(parents=True)
    (base / "data").mkdir(parents=True)
    (base / "cache" / "gui").mkdir(parents=True)

    item_index = {
        "2001": {"name": "基础作战记录", "rarity": 1},
        "2002": {"name": "初级作战记录", "rarity": 2},
        "2003": {"name": "中级作战记录", "rarity": 3},
        "2004": {"name": "高级作战记录", "rarity": 4},
        "30012": {"name": "固源岩", "rarity": 1},
        "30013": {"name": "固源岩组", "rarity": 2, "formula": {"30012": 5}},
        "30083": {"name": "轻锰矿", "rarity": 2},
        "31083": {"name": "晶体元件", "rarity": 2},
        "3241": {"name": "狙击芯片", "rarity": 2},
        "3242": {"name": "狙击芯片组", "rarity": 3},
        "3231": {"name": "重装芯片", "rarity": 2},
        "3232": {"name": "重装芯片组", "rarity": 3},
        "4001": {"name": "龙门币", "rarity": 3},
    }
    stages = [
        {"code": "1-7", "apCost": 6, "stageId": "main_01-07", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30012"}]},
        {"code": "4-7", "apCost": 18, "stageId": "main_04-07", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30083"}]},
        {"code": "PR-A-1", "apCost": 18, "stageId": "pro_a_1", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "3231"}]},
        {"code": "PR-A-2", "apCost": 36, "stageId": "pro_a_2", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "3232"}]},
        {"code": "PR-B-1", "apCost": 18, "stageId": "pro_b_1", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "3241"}]},
        {"code": "PR-B-2", "apCost": 36, "stageId": "pro_b_2", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "3242"}]},
        {"code": "CE-6", "apCost": 36, "stageId": "wk_melee_6", "dropInfos": []},
        {"code": "LS-6", "apCost": 36, "stageId": "wk_kc_6", "dropInfos": []},
        {"code": "TL-8", "apCost": 18, "stageId": "act_tl_8", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "31083"}]},
    ]
    (maa / "resource" / "item_index.json").write_text(json.dumps(item_index), encoding="utf-8")
    (maa / "resource" / "stages.json").write_text(json.dumps(stages), encoding="utf-8")
    (maa / "resource" / "recruitment.json").write_text(json.dumps({
        "operators": [
            {"name": "维什戴尔", "tags": ["狙击干员"]},
            {"name": "斥罪", "tags": ["重装干员"]},
            *[{"name": f"狙击{i}", "tags": ["狙击干员"]} for i in range(1, 10)],
        ]
    }), encoding="utf-8")
    return maa, base


class AutoGrowthTests(unittest.TestCase):
    def test_schedule_0400_reset_and_resource_collection_override(self):
        tz8 = timezone(timedelta(hours=8))
        # Tuesday 03:30 UTC+8 is still Monday game-day (PR-A, PR-B, AP, SK open; PR-C, CE closed)
        mon_night = datetime(2026, 11, 17, 3, 30, tzinfo=tz8)
        open_mon = auto_growth.get_open_stages_today(now=mon_night)
        self.assertTrue(open_mon["PR-A"])
        self.assertTrue(open_mon["PR-B"])
        self.assertFalse(open_mon["PR-C"])
        self.assertFalse(open_mon["CE"])

        act = {"Official": {"resourceCollection": {
            "IsResourceCollection": True, "TimeZone": 8,
            "UtcStartTime": "2026/11/01 04:00:00", "UtcExpireTime": "2026/11/30 03:59:59",
        }}}
        open_all = auto_growth.get_open_stages_today(now=mon_night, activity_data=act)
        self.assertTrue(open_all["PR-C"] and open_all["CE"])

    def test_formula_folding_bounded_chips_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa, base = _setup_mock_maa_and_base(Path(tmp))
            tz8 = timezone(timedelta(hours=8))
            tue = datetime(2026, 11, 17, 12, 0, tzinfo=tz8)  # PR-B open, CE open

            # 10 unbuilt 6* snipers in Box + 200x 30012 (200//5=40 -> 45 >= 30)
            opers = [{"id": f"c{i}", "name": "维什戴尔" if i == 0 else f"狙击{i}", "rarity": 6, "elite": 0, "level": 1, "own": True, "potential": 1} for i in range(10)]
            (base / "data" / "OperBoxData.json").write_text(json.dumps({"own_opers": opers}), encoding="utf-8")
            (base / "data" / "DepotData.json").write_text(json.dumps({
                "syncTime": tue.isoformat(),
                "data": {"30012": 200, "30013": 5, "30083": 4, "31083": 2, "3241": 2, "3242": 0, "4001": 100000},
            }), encoding="utf-8")
            (base / "cache" / "gui" / "StageActivityV2.json").write_text(json.dumps({
                "Official": {"sideStoryStage": {"TL": {
                    "Activity": {"StageName": "测试活动", "TimeZone": 8, "UtcStartTime": "2026/11/01 04:00:00", "UtcExpireTime": "2026/11/30 03:59:59"},
                    "Stages": [{"Display": "TL-8", "Value": "TL-8", "Drop": "31083"}],
                }}}
            }), encoding="utf-8")

            cfg = {
                "server": "Official",
                "growth": {
                    "prefer_event": True, "allow_chip_stages": True, "allow_resource_stages": True,
                    "chip_untargeted_cap": 2, "fold_craftable_materials": True, "target_operators": [],
                    "material_safety_stock": {"T3_DEFAULT": 30, "CHIP_T1": 5, "CHIP_T2": 8},
                },
            }
            my_box = auto_growth.sync_my_box(maa, base_dir=base, force=True)
            depot = auto_growth.load_depot_data(maa, base_dir=base)
            dec = auto_growth.decide_optimal_stages(
                maa, cfg, my_box=my_box, depot_data=depot, last_battle_is_annihilation=True, base_dir=base, now=tue,
            )
            self.assertEqual(dec.recommended_stages[0], "TL-8")
            self.assertIn("PR-B-2", dec.recommended_stages)
            self.assertIn("4-7", dec.recommended_stages)
            self.assertIn("CE-6", dec.recommended_stages)
            self.assertNotIn("", dec.recommended_stages)
            self.assertEqual(dec.recommended_stages[-1], "1-7")
            # 30013 folded (5 + 200//5 = 45 >= 30), so 30013 should NOT appear in shortages
            self.assertFalse(any(s["item_id"] == "30013" for s in dec.shortages))
            # Bounded chip demand: chip_untargeted_cap=2 caps target at 8 + 2*4 = 16 (not 8 + 10*4 = 48)
            sniper_chip = next(s for s in dec.shortages if s["item_id"] == "3242")
            self.assertEqual(sniper_chip["target"], 16)

    def test_cold_start_fallback_from_maa_dir_and_ttl_staleness(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa, base = _setup_mock_maa_and_base(Path(tmp))
            tz8 = timezone(timedelta(hours=8))
            now = datetime(2026, 11, 17, 12, 0, tzinfo=tz8)
            old_ts = (now - timedelta(hours=30)).isoformat()

            # Only maa/data has files (base/data empty -> cold-start fallback)
            (maa / "data" / "OperBoxData.json").write_text(json.dumps({
                "own_opers": [{"id": "c1", "name": "斥罪", "rarity": 6, "elite": 1, "level": 50, "own": True, "potential": 1}]
            }), encoding="utf-8")
            (maa / "data" / "DepotData.json").write_text(json.dumps({
                "syncTime": old_ts,
                "data": {"30083": 10},
            }), encoding="utf-8")

            stale = auto_growth.is_data_stale(maa, base_dir=base, ttl_hours=24, now=now)
            self.assertTrue(stale["depot_stale"])

            depot = auto_growth.load_depot_data(maa, base_dir=base)
            self.assertEqual(depot["data"]["30083"], 10)
            my_box = auto_growth.sync_my_box(maa, base_dir=base, persist=False)
            self.assertEqual(my_box["total_owned"], 1)
            self.assertNotIn("D:\\", str(my_box.get("source", "")))
            self.assertFalse((base / "data" / "my_box.json").exists())

    def test_event_stage_filtering_excludes_story_tutorial_furniture_and_non_navigable(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa, base = _setup_mock_maa_and_base(Path(tmp))
            tz8 = timezone(timedelta(hours=8))
            now = datetime(2026, 11, 19, 12, 0, tzinfo=tz8)

            # Expand stages.json with TL-7/8/9/10 + story/tutorial/0-ap entries
            stages = json.loads((maa / "resource" / "stages.json").read_text(encoding="utf-8"))
            stages.extend([
                {"code": "TL-ST-1", "apCost": 0, "stageId": "act55side_st01", "dropInfos": []},
                {"code": "TL-TR-1", "apCost": 0, "stageId": "act55side_tr01", "dropInfos": []},
                {"code": "TL-7", "apCost": 21, "stageId": "act55side_07", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "31043"}]},
                {"code": "TL-9", "apCost": 21, "stageId": "act55side_09", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "31113"}]},
                {"code": "TL-10", "apCost": 10, "stageId": "act55side_10", "dropInfos": [{"dropType": "FURNITURE", "itemId": "furni"}]},
            ])
            (maa / "resource" / "stages.json").write_text(json.dumps(stages), encoding="utf-8")

            # Navigable tasks only define TL-7 and TL-8 (simulate TL-9 not yet navigable/open in resource/tasks/Stages)
            (maa / "resource" / "tasks" / "Stages").mkdir(parents=True)
            (maa / "resource" / "tasks" / "Stages" / "TL.json").write_text(json.dumps({
                "TL-7": {"algorithm": "JustReturn"},
                "TL-8": {"algorithm": "JustReturn"},
                "TL-4": {"algorithm": "JustReturn"},
            }), encoding="utf-8")

            (base / "data" / "DepotData.json").write_text(json.dumps({
                "syncTime": now.isoformat(),
                "data": {"31083": 5, "31043": 50, "31113": 0, "4001": 999999},
            }), encoding="utf-8")
            (base / "cache" / "gui" / "StageActivityV2.json").write_text(json.dumps({
                "Official": {"sideStoryStage": {"TL": {
                    "Activity": {"StageName": "时间啊，向前！", "TimeZone": 8, "UtcStartTime": "2026/11/01 12:00:00", "UtcExpireTime": "2026/11/22 03:59:59"},
                    "Stages": [
                        {"Display": "TL-ST-1", "Value": "TL-ST-1", "Drop": ""},
                        {"Display": "TL-TR-1", "Value": "TL-TR-1", "Drop": ""},
                        {"Display": "TL-10", "Value": "TL-10", "Drop": "31113"},
                        {"Display": "TL-99", "Value": "TL-99", "Drop": "31113"},
                        {"Display": "TL-9", "Value": "TL-9", "Drop": "31113"},
                        {"Display": "TL-8", "Value": "TL-8", "Drop": "31083"},
                        {"Display": "TL-7", "Value": "TL-7", "Drop": "31043"},
                        {"Display": "TL-4", "Value": "TL-4", "Drop": "搓玉效率0.69"},
                    ],
                }}}
            }), encoding="utf-8")

            cfg = {"server": "Bilibili", "growth": {"prefer_event": True, "allow_chip_stages": False, "allow_resource_stages": False}}
            dec = auto_growth.decide_optimal_stages(maa, cfg, my_box={"all_owned": {}}, base_dir=base, now=now)
            for forbidden in ("TL-ST-1", "TL-TR-1", "TL-10", "TL-99", "TL-9", "TL-4"):
                self.assertNotIn(forbidden, dec.recommended_stages)
            self.assertEqual(dec.recommended_stages[0], "TL-8")
            self.assertIn("TL-7", dec.recommended_stages)
            self.assertIn("推荐关 TL-8", dec.reason)


if __name__ == "__main__":
    unittest.main()
