import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from universal_engine.utils import env_doctor


class EnvDoctorTests(unittest.TestCase):
    def test_get_base_dir_source_and_frozen(self):
        src_base = env_doctor.get_base_dir()
        self.assertEqual(src_base.name, "universal_engine")

        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "MAAEngine.exe"
            fake_exe.write_bytes(b"")
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(fake_exe)), \
                 patch.object(sys, "_MEIPASS", str(Path(tmp) / "_mei"), create=True):
                self.assertEqual(env_doctor.get_base_dir(), Path(tmp).resolve())

    def test_launcher_config_deep_merge_zero_personal_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "launcher_config.json"
            cfg_path.write_text(
                json.dumps({"server": "Bilibili", "ai": {"model_name": "deepseek-chat"}}),
                encoding="utf-8",
            )
            cfg = env_doctor.load_launcher_config(cfg_path)
            self.assertEqual(cfg["server"], "Bilibili")
            self.assertEqual(cfg["ai"]["model_name"], "deepseek-chat")
            self.assertEqual(cfg["ai"]["api_key"], "")
            self.assertEqual(cfg["daily"]["penguin_id"], "")
            self.assertEqual(cfg["growth"]["depot_sync_ttl_hours"], 24)
            self.assertFalse(cfg["maa_path_pinned"])

    def test_check_write_permission_and_denied_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            ok = env_doctor.check_write_permission(Path(tmp))
            self.assertTrue(ok["writable"])
            with patch("pathlib.Path.write_text", side_effect=PermissionError("Access denied")):
                denied = env_doctor.check_write_permission(Path(tmp))
                self.assertFalse(denied["writable"])
                self.assertIn("Access denied", denied["error"])

    def test_check_maa_integrity_valid_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = env_doctor.check_maa_integrity(root)
            self.assertFalse(bad["valid"])
            self.assertIn("MaaCore.dll", bad["missing_items"])

            (root / "MaaCore.dll").write_bytes(b"dll")
            (root / "Python" / "asst").mkdir(parents=True)
            (root / "Python" / "asst" / "asst.py").write_text("# stub", encoding="utf-8")
            res_dir = root / "resource"
            (res_dir / "tasks").mkdir(parents=True)
            for fn in ("stages.json", "item_index.json", "recruitment.json", "config.json"):
                (res_dir / fn).write_text("{}", encoding="utf-8")
            good = env_doctor.check_maa_integrity(root)
            self.assertTrue(good["valid"])
            self.assertEqual(good["missing_items"], [])

    def test_discover_adb_and_check_adb_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_adb = Path(tmp) / "adb.exe"
            fake_adb.write_bytes(b"exe")
            maa_dir = Path(tmp) / "maa"
            (maa_dir / "config").mkdir(parents=True)
            (maa_dir / "config" / "gui.new.json").write_text(
                json.dumps({"Configurations": {"Default": {"Gui": {"ConnectSettings": {"AdbPath": str(fake_adb)}}}}}),
                encoding="utf-8",
            )
            found = env_doctor.discover_adb_executable(config={}, maa_dir=maa_dir)
            self.assertEqual(Path(found).resolve(), fake_adb.resolve())

        with patch.object(env_doctor, "_run_adb") as mock_adb:
            mock_adb.side_effect = [
                SimpleNamespace(returncode=0, stdout="List of devices attached\n127.0.0.1:16384\tdevice\n", stderr=""),
                SimpleNamespace(returncode=0, stdout="Physical size: 1920x1080\nOverride size: 1280x720\n", stderr=""),
            ]
            res = env_doctor.check_adb_resolution("adb.exe")
            self.assertTrue(res["connected"])
            self.assertEqual((res["width"], res["height"]), (1280, 720))
            self.assertTrue(res["is_16_9"])

        with patch.object(env_doctor, "_run_adb") as mock_adb:
            mock_adb.side_effect = [
                SimpleNamespace(returncode=0, stdout="connected to 127.0.0.1:5555", stderr=""),
                SimpleNamespace(returncode=0, stdout="Physical size: 1280x800\n", stderr=""),
            ]
            res2 = env_doctor.check_adb_resolution("adb.exe", "127.0.0.1:5555")
            self.assertTrue(res2["connected"])
            self.assertFalse(res2["is_16_9"])
            self.assertIn("16:9", res2["warning"])


if __name__ == "__main__":
    unittest.main()
