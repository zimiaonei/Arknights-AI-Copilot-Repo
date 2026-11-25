import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from universal_engine.utils import env_doctor, maa_installer


def _make_fake_maa(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "MaaCore.dll").write_bytes(b"dll")
    (root / "Python" / "asst").mkdir(parents=True, exist_ok=True)
    (root / "Python" / "asst" / "asst.py").write_text("# stub", encoding="utf-8")
    (root / "resource" / "tasks").mkdir(parents=True, exist_ok=True)
    for fn in ("stages.json", "item_index.json", "recruitment.json", "config.json"):
        (root / "resource" / fn).write_text("{}", encoding="utf-8")
    return root


def _build_zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class _FakeResp:
    def __init__(self, payload: bytes, status: int = 200, headers: dict = None):
        self._bio = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}

    def read(self, n: int = -1) -> bytes:
        return self._bio.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class MaaInstallerTests(unittest.TestCase):
    def test_discovery_priority_and_pinned_manual_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "parent_maa" / "universal_engine"
            parent_maa = _make_fake_maa(base.parent)
            manual_maa = _make_fake_maa(Path(tmp) / "custom_maa")
            cfg_path = base / "config" / "launcher_config.json"

            with patch.object(maa_installer, "get_base_dir", return_value=base), \
                 patch.object(env_doctor, "get_base_dir", return_value=base):
                # Unpinned: parent wins over config when sibling absent
                env_doctor.save_launcher_config({"maa_path": str(manual_maa), "maa_path_pinned": False}, cfg_path)
                self.assertEqual(maa_installer.resolve_maa_path(cfg_path), parent_maa.resolve())

                # Sibling MAA_Core beats parent
                sibling_maa = _make_fake_maa(base / "MAA_Core")
                self.assertEqual(maa_installer.resolve_maa_path(cfg_path), sibling_maa.resolve())

                # Pinned manual bind overrides sibling and parent
                res = maa_installer.bind_manual_maa_path(manual_maa, cfg_path)
                self.assertTrue(res["ok"])
                self.assertEqual(maa_installer.resolve_maa_path(cfg_path), manual_maa.resolve())

                # Invalid manual bind fails cleanly without overwriting pinned path
                bad_res = maa_installer.bind_manual_maa_path(Path(tmp) / "not_exist", cfg_path)
                self.assertFalse(bad_res["ok"])
                self.assertTrue(bad_res["missing_items"])

    def test_fetch_latest_release_info_github_and_api_maa_plus_fallback(self):
        gh_empty = json.dumps({"tag_name": "v6.20.0", "assets": []}).encode("utf-8")
        gh_release = json.dumps({
            "tag_name": "v6.20.0",
            "assets": [{
                "name": "MAA-v6.20.0-win-x64.zip",
                "size": 1234,
                "browser_download_url": "https://github.com/MaaAssistantArknights/MaaRelease/releases/download/v6.20.0/MAA-v6.20.0-win-x64.zip",
            }],
        }).encode("utf-8")

        with patch("urllib.request.urlopen", side_effect=[_FakeResp(gh_empty), _FakeResp(gh_release)]):
            info = maa_installer.fetch_latest_release_info()
            self.assertEqual(info["version"], "v6.20.0")
            self.assertEqual(info["asset_name"], "MAA-v6.20.0-win-x64.zip")
            self.assertTrue(any("gh" in u for u in info["urls"]))

        # When both GitHub endpoints fail, fall back to api.maa.plus summary/detail
        summary = json.dumps({
            "stable": {
                "version": "v6.20.1",
                "detail": "https://api.maa.plus/MaaAssistantArknights/api/version/stable.json",
            }
        }).encode("utf-8")
        detail = json.dumps({
            "details": {
                "assets": [{
                    "name": "MAA-v6.20.1-win-x64.zip",
                    "size": 2048,
                    "browser_download_url": "https://github.com/MaaAssistantArknights/MaaRelease/releases/download/v6.20.1/MAA-v6.20.1-win-x64.zip",
                    "mirrors": ["https://s3.maa-org.net/MAA-v6.20.1-win-x64.zip"],
                }]
            }
        }).encode("utf-8")
        with patch("urllib.request.urlopen", side_effect=[OSError("gh1"), OSError("gh2"), _FakeResp(summary), _FakeResp(detail)]):
            info2 = maa_installer.fetch_latest_release_info()
            self.assertEqual(info2["version"], "v6.20.1")
            self.assertIn("https://s3.maa-org.net/MAA-v6.20.1-win-x64.zip", info2["urls"])

    def test_download_range_resume_wrapper_strip_cancel_and_zip_slip(self):
        payload = _build_zip_bytes({
            "MAA-v6.20.0-win-x64/MaaCore.dll": b"dll",
            "MAA-v6.20.0-win-x64/Python/asst/asst.py": b"# stub",
            "MAA-v6.20.0-win-x64/resource/tasks/t.json": b"{}",
            "MAA-v6.20.0-win-x64/resource/stages.json": b"{}",
            "MAA-v6.20.0-win-x64/resource/item_index.json": b"{}",
            "MAA-v6.20.0-win-x64/resource/recruitment.json": b"{}",
            "MAA-v6.20.0-win-x64/resource/config.json": b"{}",
        })
        split = len(payload) // 2

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            target = base / "MAA_Core"
            part = target / "_downloads" / "MAA-v6.20.0-win-x64.zip.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(payload[:split])
            cfg_path = base / "config" / "launcher_config.json"

            seen_range = []

            def fake_urlopen(req, timeout=15):
                seen_range.append(req.get_header("Range"))
                if "mirror1.bad" in req.full_url:
                    raise OSError("mirror1 down")
                return _FakeResp(
                    payload[split:],
                    status=206,
                    headers={"Content-Length": str(len(payload) - split), "Content-Range": f"bytes {split}-{len(payload)-1}/{len(payload)}"},
                )

            events = []
            with patch.object(maa_installer, "get_base_dir", return_value=base), \
                 patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = maa_installer.download_and_install_maa(
                    target_dir=target,
                    config_path=cfg_path,
                    progress_callback=lambda d, t, m: events.append((d, t, m)),
                    release_info={
                        "version": "v6.20.0",
                        "asset_name": "MAA-v6.20.0-win-x64.zip",
                        "total_size": len(payload),
                        "urls": ["https://mirror1.bad/a.zip", "https://mirror2.ok/a.zip"],
                    },
                )
            self.assertTrue(res["ok"])
            self.assertEqual(seen_range[0], f"bytes={split}-")
            self.assertTrue((target / "MaaCore.dll").is_file())
            self.assertTrue(events)

            # Cancel event stops cleanly
            cancel = threading.Event()
            cancel.set()
            res_cancel = maa_installer.download_and_install_maa(
                target_dir=target,
                config_path=cfg_path,
                cancel_event=cancel,
                release_info={"version": "v6.20.0", "asset_name": "a.zip", "total_size": 100, "urls": ["https://mirror2.ok/a.zip"]},
            )
            self.assertFalse(res_cancel["ok"])
            self.assertIn("取消", res_cancel["error"])

            # Zip Slip rejection
            evil_zip = base / "evil.zip"
            evil_zip.write_bytes(_build_zip_bytes({"../../evil.txt": b"x"}))
            with self.assertRaises(ValueError):
                maa_installer._safe_extract_zip(evil_zip, target)

    def test_download_rejects_html_error_page_and_oversized_part(self):
        payload = _build_zip_bytes({
            "MaaCore.dll": b"dll",
            "Python/asst/asst.py": b"# stub",
            "resource/tasks/t.json": b"{}",
            "resource/stages.json": b"{}",
            "resource/item_index.json": b"{}",
            "resource/recruitment.json": b"{}",
            "resource/config.json": b"{}",
        })
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "engine"
            target = base / "MAA_Core"
            cfg_path = base / "config" / "launcher_config.json"

            # Mirror 1 returns 200 HTML error page (smaller than zip); mirror 2 returns full zip
            seen_headers = []
            def fake_urlopen(req, timeout=15):
                seen_headers.append((req.full_url, req.get_header("Range")))
                if "badhtml" in req.full_url:
                    return _FakeResp(b"<html>502 Bad Gateway</html>", status=200, headers={"Content-Type": "text/html", "Content-Length": "28"})
                return _FakeResp(payload, status=200, headers={"Content-Type": "application/zip", "Content-Length": str(len(payload))})

            with patch.object(maa_installer, "get_base_dir", return_value=base), \
                 patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = maa_installer.download_and_install_maa(
                    target_dir=target,
                    config_path=cfg_path,
                    release_info={
                        "version": "v6.20.0",
                        "asset_name": "MAA-v6.20.0-win-x64.zip",
                        "total_size": len(payload),
                        "urls": ["https://badhtml.test/a.zip", "https://good.test/a.zip"],
                    },
                )
            self.assertTrue(res["ok"])
            self.assertEqual(seen_headers[1][1], None)

            # HTML with unknown total_size=0 must fail cleanly
            with patch.object(maa_installer, "get_base_dir", return_value=base), \
                 patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch("urllib.request.urlopen", return_value=_FakeResp(b"<html>err</html>", status=200, headers={"Content-Type": "text/html"})):
                res_html = maa_installer.download_and_install_maa(
                    target_dir=target,
                    config_path=cfg_path,
                    release_info={
                        "version": "v6.20.0",
                        "asset_name": "MAA-v6.20.0-win-x64.zip",
                        "total_size": 0,
                        "urls": ["https://badhtml.test/a.zip"],
                    },
                )
            self.assertFalse(res_html["ok"])

            # Oversized stale .part must be rejected / truncated rather than falsely accepted
            part = target / "_downloads" / "MAA-v6.20.0-win-x64.zip.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            part.write_bytes(b"corrupt" * 500)
            with patch.object(maa_installer, "get_base_dir", return_value=base), \
                 patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch("urllib.request.urlopen", return_value=_FakeResp(payload, status=200, headers={"Content-Type": "application/zip", "Content-Length": str(len(payload))})):
                res2 = maa_installer.download_and_install_maa(
                    target_dir=target,
                    config_path=cfg_path,
                    release_info={
                        "version": "v6.20.0",
                        "asset_name": "MAA-v6.20.0-win-x64.zip",
                        "total_size": len(payload),
                        "urls": ["https://good.test/a.zip"],
                    },
                )
            self.assertTrue(res2["ok"])


if __name__ == "__main__":
    unittest.main()
