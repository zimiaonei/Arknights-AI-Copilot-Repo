# MAA Universal Backend Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained, zero-hardcoding, Python-stdlib-only universal MAA backend engine under `universal_engine/` with environment diagnostics, auto-discovery/download of MAA core, OpenAI-compatible AI strategist (with zero-key fallback), Depot+Box autonomous growth planner, and GUI-ready scheduler APIs.

**Architecture:** Six focused Python modules under `universal_engine/` sharing a layered JSON config (`config/launcher_config.json`) anchored to `get_base_dir()`. `env_doctor` and `maa_installer` locate or download MAA without importing `Asst` early; `maa_runner` dynamically injects `<maa_dir>/Python` while isolating `user_dir`/`cache` inside `universal_engine/`; `ai_strategist` and `auto_growth` wrap deterministic MAA rules around optional OpenAI enhancement and Depot/Box shortage scoring; `master_scheduler` exposes `MaaEngine` for future GUI callers and CLI execution.

**Tech Stack:** Python 3.10+ standard library only (`urllib.request`, `zipfile`, `ctypes`, `subprocess`, `dataclasses`, `threading`, `unittest`, `pathlib`, `json`, `re`, `shutil`), Windows subprocess/ADB integration, dynamic `asst.asst.Asst` binding.

**Spec:** `universal_engine/docs/2026-11-18-universal-backend-design.md`

## Global Constraints

- Output all new code, configs, docs, and tests strictly inside `universal_engine/`; do not modify root scripts (`master_scheduler.py`, `autonomous_agent.py`, `config/`, `一键启动全自动日常.bat`).
- Zero third-party `pip` runtime dependencies (pure Python 3.10+ standard library only).
- Zero hardcoded personal paths (`<LOCAL_MAA_DIR>`, `<PERSONAL_PATH>`), zero hardcoded `penguin_id="<PERSONAL_PENGUIN_ID>"`, zero hardcoded `"Bilibili"` fallback (default server is `"Official"`).
- `get_base_dir()` returns `Path(sys.executable).resolve().parent` when `getattr(sys, "frozen", False)` is truthy, else `Path(__file__).resolve().parent.parent` (`universal_engine/` root).
- Library functions must never call `sys.exit()` or blocking `input()`; only CLI `main()` may return exit codes or exit.
- Workspace is not a git repository (skip git commit steps). Run tests with `python -m unittest discover -s universal_engine/tests -v`.

## Review Focus

1. **PyInstaller frozen executable path (`sys.frozen` + `_MEIPASS`)**: When frozen, `get_base_dir()` must resolve to `sys.executable`'s parent directory, never `sys._MEIPASS`, so configs and `MAA_Core/` persist.
2. **Zip Slip traversal & top-level wrapper folder on extraction**: `download_and_install_maa()` must reject archive entries escaping `MAA_Core/` and strip a single top-level wrapper folder (e.g., `MAA-v6.20.1-win-x64/`) so `MaaCore.dll` lands directly under `MAA_Core/`.
3. **LLM malformed output, fenced JSON, non-owned operators, and HTTP failures**: `ai_strategist` must strip ```` ```json ```` fences, reject hallucinated operators absent from `my_box`, and seamlessly return the deterministic rule baseline on network/parse failures.
4. **TTL pre-fight sync, formula folding, bounded chip demand, and 1-retry-then-`1-7` anti-deadlock**: `auto_growth` + `master_scheduler` must sync `Depot`/`OperBox` only when missing or older than `depot_sync_ttl_hours`, fold T2->T3 stock via `item_index.json` `formula`, cap untargeted chips at `chip_untargeted_cap=2`, skip `""` when `last_battle_is_annihilation=True`, and fall back to `"1-7"` after max 1 retry on failed stages.
5. **Pinned manual `maa_path` override & external read-only `maa_path` isolation**: `bind_manual_maa_path()` sets `maa_path_pinned=True` so auto-discovery does not shadow manual choices, while unpinned startup strictly searches `sibling -> parent -> common -> config`, and `init_maa_sdk()` always sets `user_dir=get_base_dir()`.

---

### Task 1: Scaffolding, Config Persistence & Environment Doctor (`utils/env_doctor.py`)

**Files:**
- Create: `universal_engine/__init__.py`
- Create: `universal_engine/utils/__init__.py`
- Create: `universal_engine/config/launcher_config.json`
- Create: `universal_engine/utils/env_doctor.py`
- Test: `universal_engine/tests/test_env_doctor.py`

**Interfaces:**
- Produces in `utils/env_doctor.py`:
  - `DEFAULT_LAUNCHER_CONFIG: Dict[str, Any]`
  - `SERVER_PACKAGES: Dict[str, Dict[str, str]]` (`Official` -> `com.hypergryph.arknights`, `Bilibili` -> `com.hypergryph.arknights.bilibili`)
  - `get_base_dir() -> Path`
  - `resolve_path(*parts: str) -> Path`
  - `ensure_runtime_dirs() -> Dict[str, Path]`
  - `load_launcher_config(config_path: Optional[Path] = None) -> Dict[str, Any]`
  - `save_launcher_config(config: Dict[str, Any], config_path: Optional[Path] = None) -> Path`
  - `check_write_permission(target_dir: Optional[Path] = None) -> Dict[str, Any]`
  - `check_maa_integrity(maa_dir: Union[str, Path]) -> Dict[str, Any]`
  - `discover_adb_executable(config: Optional[Dict[str, Any]] = None, maa_dir: Optional[Path] = None) -> Optional[str]`
  - `check_adb_resolution(adb_path: str, adb_address: str = "", timeout_sec: int = 8) -> Dict[str, Any]`
  - `run_env_diagnostics(config: Optional[Dict[str, Any]] = None, maa_dir: Optional[Path] = None, check_adb: bool = True) -> Dict[str, Any]`

- [ ] **Step 1: Write the failing tests in `universal_engine/tests/test_env_doctor.py`**

```python
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
            cfg_path.write_text(json.dumps({"server": "Bilibili", "ai": {"model_name": "deepseek-chat"}}), encoding="utf-8")
            cfg = env_doctor.load_launcher_config(cfg_path)
            self.assertEqual(cfg["server"], "Bilibili")
            self.assertEqual(cfg["ai"]["model_name"], "deepseek-chat")
            self.assertEqual(cfg["ai"]["api_key"], "")
            self.assertEqual(cfg["daily"]["penguin_id"], "")
            self.assertEqual(cfg["growth"]["depot_sync_ttl_hours"], 24)

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

    def test_check_adb_resolution_16_9_and_override_and_mismatch(self):
        with patch.object(env_doctor, "_run_adb") as mock_adb:
            mock_adb.side_effect = [
                SimpleNamespace(returncode=0, stdout="List of devices attached\n127.0.0.1:16384\tdevice\n"),
                SimpleNamespace(returncode=0, stdout="Physical size: 1920x1080\nOverride size: 1280x720\n"),
            ]
            res = env_doctor.check_adb_resolution("adb.exe")
            self.assertTrue(res["connected"])
            self.assertEqual((res["width"], res["height"]), (1280, 720))
            self.assertTrue(res["is_16_9"])

        with patch.object(env_doctor, "_run_adb") as mock_adb:
            mock_adb.side_effect = [
                SimpleNamespace(returncode=0, stdout="connected to 127.0.0.1:5555"),
                SimpleNamespace(returncode=0, stdout="Physical size: 1280x800\n"),
            ]
            res2 = env_doctor.check_adb_resolution("adb.exe", "127.0.0.1:5555")
            self.assertTrue(res2["connected"])
            self.assertFalse(res2["is_16_9"])
            self.assertIn("16:9", res2["warning"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest universal_engine.tests.test_env_doctor -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'universal_engine'`.

- [ ] **Step 3: Create package files, `config/launcher_config.json`, and implement `utils/env_doctor.py`**

Implement `get_base_dir()`, deep-merge `load_launcher_config()` / `save_launcher_config()`, `ensure_runtime_dirs()`, `check_write_permission()`, `check_maa_integrity()` (checking `MaaCore.dll` on Windows or `.so`/`.dylib` on POSIX, `Python/asst/asst.py`, and required `resource/` files without loading DLL), `discover_adb_executable()` (checking `config.emulator.adb_path`, `<maa_dir>/config/gui.new.json`, `shutil.which("adb")`, and common Windows emulator directories via `os.environ` / drive roots — zero personal paths), `_run_adb(cmd, timeout_sec)` subprocess wrapper + `check_adb_resolution()` (preferring `Override size` over `Physical size`, checking `abs(max(w,h)/min(w,h) - 16/9) <= 0.02` and `min(w,h) >= 720`), and `run_env_diagnostics()`. Also generate default `universal_engine/config/launcher_config.json` matching Design Doc §5.2.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest universal_engine.tests.test_env_doctor -v`
Expected: PASS (all 5 tests pass).

---

### Task 2: MAA Auto-Discovery & Release Downloader (`utils/maa_installer.py`)

**Files:**
- Create: `universal_engine/utils/maa_installer.py`
- Test: `universal_engine/tests/test_maa_installer.py`

**Interfaces:**
- Consumes: `get_base_dir`, `load_launcher_config`, `save_launcher_config`, `check_maa_integrity` from `utils/env_doctor.py`.
- Produces in `utils/maa_installer.py`:
  - `find_existing_maa(config_path: Optional[Path] = None, extra_search_dirs: Optional[List[Path]] = None) -> Optional[Path]`
  - `resolve_maa_path(config_path: Optional[Path] = None) -> Optional[Path]`
  - `bind_manual_maa_path(user_path: Union[str, Path], config_path: Optional[Path] = None) -> Dict[str, Any]`
  - `build_mirror_urls(github_url: str, official_mirrors: Optional[List[str]] = None) -> List[str]`
  - `fetch_latest_release_info(timeout_sec: int = 15) -> Dict[str, Any]`
  - `download_and_install_maa(target_dir: Optional[Path] = None, config_path: Optional[Path] = None, progress_callback: Optional[Callable[[int, int, str], None]] = None, cancel_event: Optional[threading.Event] = None, release_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]`

- [ ] **Step 1: Write the failing tests in `universal_engine/tests/test_maa_installer.py`**

```python
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

    def test_fetch_latest_release_info_github_fallback_to_maarelease(self):
        gh_empty = json.dumps({"tag_name": "v6.20.0", "assets": []}).encode("utf-8")
        gh_release = json.dumps({
            "tag_name": "v6.20.0",
            "assets": [{
                "name": "MAA-v6.20.0-win-x64.zip",
                "size": 1234,
                "browser_download_url": "https://github.com/MaaAssistantArknights/MaaRelease/releases/download/v6.20.0/MAA-v6.20.0-win-x64.zip",
            }],
        }).encode("utf-8")

        class _Resp:
            def __init__(self, payload: bytes):
                self._b = payload
            def read(self):
                return self._b
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with patch("urllib.request.urlopen", side_effect=[_Resp(gh_empty), _Resp(gh_release)]):
            info = maa_installer.fetch_latest_release_info()
            self.assertEqual(info["version"], "v6.20.0")
            self.assertEqual(info["asset_name"], "MAA-v6.20.0-win-x64.zip")
            self.assertTrue(any("gh" in u for u in info["urls"]))

    def test_download_range_resume_wrapper_strip_and_zip_slip_guard(self):
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

            class _FakeHttpResp:
                def __init__(self, chunk: bytes, status: int = 206):
                    self._bio = io.BytesIO(chunk)
                    self.status = status
                    self.headers = {"Content-Length": str(len(chunk)), "Content-Range": f"bytes {split}-{len(payload)-1}/{len(payload)}"}
                def read(self, n=-1):
                    return self._bio.read(n)
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            events = []
            with patch.object(maa_installer, "get_base_dir", return_value=base), \
                 patch.object(env_doctor, "get_base_dir", return_value=base), \
                 patch("urllib.request.urlopen", side_effect=[OSError("mirror1 down"), _FakeHttpResp(payload[split:])]):
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
            self.assertTrue((target / "MaaCore.dll").is_file())
            self.assertTrue(events)

            # Zip Slip rejection
            evil_zip = base / "evil.zip"
            evil_zip.write_bytes(_build_zip_bytes({"../../evil.txt": b"x"}))
            with self.assertRaises(ValueError):
                maa_installer._safe_extract_zip(evil_zip, target)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest universal_engine.tests.test_maa_installer -v`
Expected: FAIL with `ImportError` / `ModuleNotFoundError`.

- [ ] **Step 3: Implement `universal_engine/utils/maa_installer.py`**

Implement strict 4-step search (`find_existing_maa`), `maa_path_pinned` support in `resolve_maa_path()` and `bind_manual_maa_path()`, 3-tier release probing (`MaaAssistantArknights/releases/latest` -> `MaaRelease/releases/latest` -> `api.maa.plus`), domestic GitHub mirror expansion (`ghfast.top`, `gh-proxy.com`, `mirror.ghproxy.com`), HTTP `Range` 206 resume writing `.part` files with `cancel_event` and `progress_callback`, `_safe_extract_zip()` with Zip Slip validation and single top-level wrapper folder stripping, and post-extract `check_maa_integrity()` validation + config update.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest universal_engine.tests.test_maa_installer -v`
Expected: PASS.

---

### Task 3: AI Strategist & Pure-MAA Copilot Adapter/Repair (`ai_strategist.py`)

**Files:**
- Create: `universal_engine/ai_strategist.py`
- Test: `universal_engine/tests/test_ai_strategist.py`

**Interfaces:**
- Consumes: `recruitment.json` path (passed via `maa_dir`), `my_box` dict (`all_owned`, `e2_operators`).
- Produces in `ai_strategist.py`:
  - `OpenAICompatClient(base_url: str = "https://api.openai.com/v1", api_key: str = "", model_name: str = "gpt-4o-mini", timeout_sec: int = 30)`
    - `.is_configured() -> bool`
    - `.chat_json(system_prompt: str, user_prompt: str, temperature: float = 0.2) -> Optional[Dict[str, Any]]`
  - `load_recruitment_roles(maa_dir: Optional[Path] = None) -> Dict[str, str]`
  - `parse_battle_metrics(log_lines: List[str], events: List[Dict[str, Any]]) -> Dict[str, Any]`
  - `adapt_copilot(guide: Dict[str, Any], my_box: Dict[str, Any], stage_id: str, maa_dir: Optional[Path] = None, client: Optional[OpenAICompatClient] = None) -> Dict[str, Any]`
  - `repair_copilot(candidates: List[Dict[str, Any]], cand_idx: int, cur_copilot: Dict[str, Any], report: Dict[str, Any], my_box: Dict[str, Any], round_idx: int, client: Optional[OpenAICompatClient] = None) -> Tuple[int, Dict[str, Any], Dict[str, Any]]`

- [ ] **Step 1: Write the failing tests in `universal_engine/tests/test_ai_strategist.py`**

```python
import json
import unittest
from unittest.mock import patch

from universal_engine import ai_strategist


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

    def test_client_is_configured_and_json_fenced_parsing(self):
        self.assertFalse(ai_strategist.OpenAICompatClient(api_key="").is_configured())
        client = ai_strategist.OpenAICompatClient(base_url="http://127.0.0.1:11434/v1", api_key="ollama", model_name="qwen2.5")
        self.assertTrue(client.is_configured())

        fake_body = json.dumps({
            "choices": [{"message": {"content": "```json\n{\"substitutions\": {\"山\": \"棘刺\"}}\n```"}}]
        }).encode("utf-8")

        class _R:
            def read(self): return fake_body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=_R()):
            parsed = client.chat_json("sys", "usr")
            self.assertEqual(parsed, {"substitutions": {"山": "棘刺"}})

        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            self.assertIsNone(client.chat_json("sys", "usr"))

    def test_adapt_copilot_zero_key_and_llm_hallucination_fallback(self):
        # Pure MAA fallback replaces 山 -> 棘刺 and drops unused 魂灵之影 retreat
        res_no_key = ai_strategist.adapt_copilot(self.guide, self.my_box, "TL-9", client=ai_strategist.OpenAICompatClient())
        self.assertEqual(res_no_key["substitutions"].get("山"), "棘刺")
        self.assertEqual(res_no_key["mode"], "pure_maa")
        self.assertTrue(all(a["name"] != "魂灵之影" for a in res_no_key["copilot"]["actions"]))

        # LLM returning unowned operator falls back safely to pure MAA baseline
        client = ai_strategist.OpenAICompatClient(api_key="sk-test")
        with patch.object(client, "chat_json", return_value={"substitutions": {"山": "不存在干员"}}):
            res_bad_ai = ai_strategist.adapt_copilot(self.guide, self.my_box, "TL-9", client=client)
            self.assertEqual(res_bad_ai["substitutions"].get("山"), "棘刺")
            self.assertEqual(res_bad_ai["mode"], "pure_maa_fallback")

    def test_repair_copilot_rule_baseline_and_ai_patch(self):
        cands = [{"guide_id": 1001, "copilot": self.guide["content"]}, {"guide_id": 1002, "copilot": self.guide["content"]}]
        report = {"metrics": {"kills": 5, "costs": 10, "last_3_actions": ["BulletTime"], "errors": [], "failed_flag": False}, "log_tail": []}
        cur = {"opers": [{"name": "棘刺", "skill_usage": 0}], "actions": [{"type": "BulletTime", "costs": 12}]}
        idx, rep, strat = ai_strategist.repair_copilot(cands, 0, cur, report, self.my_box, round_idx=1, client=None)
        self.assertEqual(idx, 0)
        self.assertEqual(rep["actions"][0]["costs"], 9)
        self.assertEqual(strat["deployment_with_pause"], "1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest universal_engine.tests.test_ai_strategist -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `universal_engine/ai_strategist.py`**

Port and generalize `ROLE_SUBSTITUTES`, `OPERATOR_DEFAULT_ROLE`, `PROF_TAG_TO_ROLE`, `load_recruitment_roles()`, `parse_battle_metrics()`, deterministic `adapt_guide_to_box()`, and `diagnose_and_repair_copilot()` from root `autonomous_agent.py` (taking explicit `maa_dir` instead of globals). Wrap with `OpenAICompatClient` (`/chat/completions` over `urllib.request`, extracting JSON from fenced code blocks) and strict post-validation (`mode` tagged `"ai_assisted"` / `"pure_maa"` / `"pure_maa_fallback"`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest universal_engine.tests.test_ai_strategist -v`
Expected: PASS.

---

### Task 4: Autonomous Growth & Stage Planner (`auto_growth.py`)

**Files:**
- Create: `universal_engine/auto_growth.py`
- Test: `universal_engine/tests/test_auto_growth.py`

**Interfaces:**
- Consumes: `get_base_dir`, `load_launcher_config` from `utils/env_doctor.py`; `load_recruitment_roles` from `ai_strategist.py`.
- Produces in `auto_growth.py`:
  - `GrowthDecision` (`@dataclass`: `recommended_stages: List[str]`, `reason: str`, `shortages: List[Dict[str, Any]]`, `open_today: Dict[str, bool]`, `active_event: Optional[Dict[str, Any]]`, `data_staleness: Dict[str, Any]`)
  - `sync_my_box(maa_dir: Path, base_dir: Optional[Path] = None, force: bool = False) -> Dict[str, Any]`
  - `load_depot_data(maa_dir: Path, base_dir: Optional[Path] = None) -> Dict[str, Any]`
  - `is_data_stale(maa_dir: Path, base_dir: Optional[Path] = None, ttl_hours: float = 24.0, now: Optional[datetime] = None) -> Dict[str, bool]`
  - `get_open_stages_today(now: Optional[datetime] = None, activity_data: Optional[Dict[str, Any]] = None, server: str = "Official") -> Dict[str, bool]`
  - `fold_craftable_inventory(raw_depot: Dict[str, int], item_index: Dict[str, Any]) -> Dict[str, int]`
  - `decide_optimal_stages(maa_dir: Path, config: Dict[str, Any], my_box: Optional[Dict[str, Any]] = None, depot_data: Optional[Dict[str, Any]] = None, last_battle_is_annihilation: bool = False, base_dir: Optional[Path] = None, now: Optional[datetime] = None) -> GrowthDecision`

- [ ] **Step 1: Write the failing tests in `universal_engine/tests/test_auto_growth.py`**

```python
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
        "30012": {"name": "固源岩", "rarity": 1},
        "30013": {"name": "固源岩组", "rarity": 2, "formula": {"30012": 5}},
        "30083": {"name": "轻锰矿", "rarity": 2},
        "31083": {"name": "晶体元件", "rarity": 2},
        "3241": {"name": "狙击芯片", "rarity": 2},
        "3242": {"name": "狙击芯片组", "rarity": 3},
        "4001": {"name": "龙门币", "rarity": 3},
    }
    stages = [
        {"code": "1-7", "apCost": 6, "stageId": "main_01-07", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30012"}]},
        {"code": "4-7", "apCost": 18, "stageId": "main_04-07", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "30083"}]},
        {"code": "PR-B-1", "apCost": 18, "stageId": "pro_b_1", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "3241"}]},
        {"code": "PR-B-2", "apCost": 36, "stageId": "pro_b_2", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "3242"}]},
        {"code": "CE-6", "apCost": 36, "stageId": "wk_melee_6", "dropInfos": []},
        {"code": "LS-6", "apCost": 36, "stageId": "wk_kc_6", "dropInfos": []},
        {"code": "TL-8", "apCost": 18, "stageId": "act_tl_8", "dropInfos": [{"dropType": "NORMAL_DROP", "itemId": "31083"}]},
    ]
    (maa / "resource" / "item_index.json").write_text(json.dumps(item_index), encoding="utf-8")
    (maa / "resource" / "stages.json").write_text(json.dumps(stages), encoding="utf-8")
    (maa / "resource" / "recruitment.json").write_text(json.dumps({"operators": []}), encoding="utf-8")
    return maa, base


class AutoGrowthTests(unittest.TestCase):
    def test_schedule_0400_reset_and_resource_collection_override(self):
        tz8 = timezone(timedelta(hours=8))
        # Tuesday 03:30 UTC+8 is still Monday game-day (PR-A, PR-B, AP, SK open; CE closed)
        mon_night = datetime(2026, 11, 17, 3, 30, tzinfo=tz8)
        open_mon = auto_growth.get_open_stages_today(now=mon_night)
        self.assertTrue(open_mon["PR-A"])
        self.assertTrue(open_mon["PR-B"])
        self.assertFalse(open_mon["PR-C"])
        self.assertFalse(open_mon["CE"])

        # ResourceCollection event opens all
        act = {"Official": {"resourceCollection": {
            "IsResourceCollection": True, "TimeZone": 8,
            "UtcStartTime": "2026/11/01 04:00:00", "UtcExpireTime": "2026/11/30 03:59:59",
        }}}
        open_all = auto_growth.get_open_stages_today(now=mon_night, activity_data=act)
        self.assertTrue(open_all["PR-C"] and open_all["CE"])

    def test_formula_folding_and_bounded_chips_and_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            maa, base = _setup_mock_maa_and_base(Path(tmp))
            tz8 = timezone(timedelta(hours=8))
            tue = datetime(2026, 11, 17, 12, 0, tzinfo=tz8)  # PR-B open

            # Many unbuilt 6* snipers/casters in Box, but T3 rocks foldable from 200x 30012 (200//5=40 >= 30)
            opers = [{"id": f"c{i}", "name": f"狙击{i}", "rarity": 6, "elite": 0, "level": 1, "own": True, "potential": 1} for i in range(10)]
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
            self.assertNotIn("", dec.recommended_stages)
            self.assertEqual(dec.recommended_stages[-1], "1-7")
            # 30013 folded (5 + 200//5 = 45 >= 30), so 30013 should NOT appear in shortages
            self.assertFalse(any(s["item_id"] == "30013" for s in dec.shortages))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest universal_engine.tests.test_auto_growth -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `universal_engine/auto_growth.py`**

Implement:
- `sync_my_box()` writing relative/portable `my_box.json` under `<base_dir>/data/` with read fallback to `<maa_dir>/data/OperBoxData.json` when `<base_dir>/data/OperBoxData.json` is absent or older.
- `load_depot_data()` reading `<base_dir>/data/DepotData.json` with read fallback to `<maa_dir>/data/DepotData.json`.
- `is_data_stale()` checking `DepotData.json` `syncTime` (and file mtime) plus `OperBoxData.json` mtime against `ttl_hours`.
- `get_open_stages_today()` shifting UTC+8 by `-4h` for server reset and respecting `StageActivityV2.json` `resourceCollection`.
- `fold_craftable_inventory()` doing 1-hop synthesis folding via `item_index.json` `formula`.
- Curated offline permanent main-story stage table for T3 materials (`30013`–`31113`), validated against `stages.json`.
- `decide_optimal_stages()` enforcing strict ranking (`Active SideStory > Open Chip Stage (bounded by chip_untargeted_cap) > Permanent T3 Material > CE-6/LS-6 > "" (unless last_battle_is_annihilation) > "1-7"`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest universal_engine.tests.test_auto_growth -v`
Expected: PASS.

---

### Task 5: MAA Core Runner, Generic Emulator/ADB & Post-Actions (`maa_runner.py`)

**Files:**
- Create: `universal_engine/maa_runner.py`
- Test: `universal_engine/tests/test_maa_runner.py`

**Interfaces:**
- Consumes: `get_base_dir`, `SERVER_PACKAGES`, `discover_adb_executable` from `utils/env_doctor.py`; `resolve_maa_path` from `utils/maa_installer.py`; `OpenAICompatClient`, `adapt_copilot`, `repair_copilot`, `parse_battle_metrics` from `ai_strategist.py`; `sync_my_box` from `auto_growth.py`.
- Produces in `maa_runner.py`:
  - `init_maa_sdk(maa_dir: Path, base_dir: Optional[Path] = None, use_cache_incremental: bool = True) -> bool`
  - `ensure_emulator_ready(config: Dict[str, Any], maa_dir: Optional[Path] = None, timeout_sec: int = 120) -> Dict[str, Any]`
  - `ensure_game_running(config: Dict[str, Any], maa_dir: Optional[Path] = None, wait_after_launch: int = 8) -> str`
  - `capture_adb_screenshot(adb_path: str, adb_addr: str, out_path: Path) -> bool`
  - `execute_post_action(action: str, config: Dict[str, Any], maa_dir: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]`
  - `MaaSession(maa_dir: Path, config: Dict[str, Any], base_dir: Optional[Path] = None, event_callback: Optional[Callable[[Dict[str, Any]], None]] = None, log_callback: Optional[Callable[[str], None]] = None, cancel_event: Optional[threading.Event] = None)`
  - `run_task_with_recovery(session_holder: Dict[str, MaaSession], task_type: str, params: Dict[str, Any], config: Dict[str, Any], timeout_sec: int = 1800, max_retries: int = 1) -> Dict[str, Any]`
  - `resolve_stage_info(query: str, maa_dir: Path) -> Dict[str, Any]`
  - `fetch_prts_guides(stage_id: str, stage_code: str, max_details: int = 8) -> List[Dict[str, Any]]`
  - `autonomous_clear_stage(stage_query: str, maa_dir: Path, config: Dict[str, Any], max_rounds: int = 5, base_dir: Optional[Path] = None, progress_callback: Optional[Callable[[int, int, str], None]] = None, cancel_event: Optional[threading.Event] = None) -> Tuple[bool, Dict[str, Any]]`

- [ ] **Step 1: Write the failing tests in `universal_engine/tests/test_maa_runner.py`**

```python
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from universal_engine import maa_runner


class MaaRunnerTests(unittest.TestCase):
    def test_init_maa_sdk_uses_isolated_user_dir_and_dynamic_python_path(self):
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
            (base / "cache" / "resource").mkdir(parents=True)

            with patch.dict(sys.modules, {"asst": fake_asst_pkg, "asst.asst": fake_asst_mod, "asst.utils": fake_utils_mod}), \
                 patch.object(maa_runner, "_LOADED_MAA_DIR", None):
                ok = maa_runner.init_maa_sdk(maa, base_dir=base, use_cache_incremental=True)
                self.assertTrue(ok)
                self.assertEqual(calls["path"], maa)
                self.assertEqual(calls["user_dir"], base)
                self.assertEqual(calls["incremental_path"], base / "cache")

    def test_ensure_game_running_switches_official_and_bilibili(self):
        for server, expected_pkg in (("Official", "com.hypergryph.arknights"), ("Bilibili", "com.hypergryph.arknights.bilibili")):
            cfg = {"server": server, "emulator": {"adb_path": "adb.exe", "adb_address": "127.0.0.1:16384"}}
            with patch.object(maa_runner, "ensure_emulator_ready", return_value={"ready": True, "adb_path": "adb.exe", "adb_addr": "127.0.0.1:16384"}), \
                 patch.object(maa_runner, "_run_cmd", side_effect=[SimpleNamespace(returncode=0, stdout=""), SimpleNamespace(returncode=0, stdout="")]) as mock_cmd:
                pkg = maa_runner.ensure_game_running(cfg, wait_after_launch=0)
                self.assertEqual(pkg, expected_pkg)
                launch_cmd = mock_cmd.call_args_list[1][0][0]
                self.assertIn(f"{expected_pkg}/com.u8.sdk.U8UnityContext", launch_cmd)

    def test_execute_post_action_none_close_emulator_and_shutdown_dry_run(self):
        none_res = maa_runner.execute_post_action("none", {})
        self.assertTrue(none_res["executed"] is False)

        with patch.object(maa_runner, "_run_cmd", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as mock_cmd:
            shut_res = maa_runner.execute_post_action("shutdown", {}, dry_run=True)
            self.assertTrue(shut_res["dry_run"])
            self.assertIn("shutdown", shut_res["command"])
            mock_cmd.assert_not_called()

            emu_cfg = {"emulator": {"adb_path": "adb.exe", "adb_address": "127.0.0.1:5555"}}
            close_res = maa_runner.execute_post_action("close_emulator", emu_cfg, dry_run=True)
            self.assertEqual(close_res["action"], "close_emulator")
            self.assertTrue(close_res["dry_run"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest universal_engine.tests.test_maa_runner -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `universal_engine/maa_runner.py`**

Implement dynamic `Asst` loading (`user_dir=base_dir`, `incremental_path=<base_dir>/cache` with `<maa_dir>/cache` fallback), generic emulator/ADB discovery & launch (MuMu12/LDPlayer/BlueStacks/Nox/ADB-only), server-aware `ensure_game_running()` (`Official` vs `Bilibili`), `MaaSession` with cancel/event/log callbacks and recruit rare-tag alert, `_has_real_disconnect_error` + `run_task_with_recovery`, `execute_post_action()` (`none`/`close_emulator`/`shutdown` with `dry_run`), PRTS guide fetching + `autonomous_clear_stage()` integrating `ai_strategist`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest universal_engine.tests.test_maa_runner -v`
Expected: PASS.

---

### Task 6: Master Scheduler, `MaaEngine` GUI API & CLI Self-Check (`master_scheduler.py`)

**Files:**
- Modify: `universal_engine/__init__.py` (export public `MaaEngine` and key symbols)
- Create: `universal_engine/master_scheduler.py`
- Test: `universal_engine/tests/test_master_scheduler.py`

**Interfaces:**
- Consumes: all symbols from `utils/env_doctor.py`, `utils/maa_installer.py`, `ai_strategist.py`, `auto_growth.py`, `maa_runner.py`.
- Produces in `master_scheduler.py`:
  - `MaaEngine(config_path: Optional[Path] = None)`
    - `.check_environment(check_adb: bool = True) -> Dict[str, Any]`
    - `.ensure_maa_ready(auto_download: bool = False, progress_callback=None, cancel_event=None) -> Dict[str, Any]`
    - `.bind_maa_path(maa_path: Union[str, Path]) -> Dict[str, Any]`
    - `.refresh_cache() -> Dict[str, Any]`
    - `.sync_box_and_depot(run_maa_tasks: bool = False) -> Dict[str, Any]`
    - `.plan_auto_growth(now: Optional[datetime] = None) -> Dict[str, Any]`
    - `.run_daily(overrides: Optional[Dict[str, Any]] = None, event_callback=None, log_callback=None, progress_callback=None, cancel_event=None, dry_run_post_action: bool = False) -> Dict[str, Any]`
    - `.run_annihilation(force: bool = False, max_rounds: int = 5, ...) -> Dict[str, Any]`
    - `.run_fight(stage: str = "", fight_mode: Optional[str] = None, medicine: int = 0, stone: int = 0, times: int = 2147483647, auto_push: Optional[bool] = None, max_rounds: int = 5, ...) -> Dict[str, Any]`
    - `.run_push(stages: List[str], max_rounds: int = 5, ...) -> Dict[str, Any]`
  - `main(argv: Optional[List[str]] = None) -> int`

- [ ] **Step 1: Write the failing tests in `universal_engine/tests/test_master_scheduler.py`**

```python
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from universal_engine.master_scheduler import MaaEngine, run_sanity_fight_pipeline
from universal_engine.utils import env_doctor


class MasterSchedulerTests(unittest.TestCase):
    def test_sanity_fight_anti_deadlock_retries_once_then_falls_back_to_1_7(self):
        fake_sess = MagicMock()
        # Non-1-7 stage fails twice with UnableToAgent -> falls back immediately to 1-7 and succeeds
        fail_res = {
            "ok": False, "timed_out": False, "elapsed": 2.0,
            "events": [{"message": "SubTaskError", "details": {"first": ["FightBegin"]}}],
            "chain_errors": [], "sub_errors": [{}],
            "log_lines": ["PipelineAnalyzer::analyze UnableToAgent"],
        }
        ok_res = {
            "ok": True, "timed_out": False, "elapsed": 10.0,
            "events": [{"message": "SubTaskExtraInfo", "details": {"what": "StageDrops", "details": {"stage": {"stageCode": "1-7"}, "stats": []}}}],
            "chain_errors": [], "sub_errors": [], "log_lines": [],
        }
        fake_sess.run_single_task.side_effect = [fail_res, fail_res, ok_res]
        holder = {"session": fake_sess}
        cfg = env_doctor.DEFAULT_LAUNCHER_CONFIG
        state = {"last_battle_is_annihilation": False}

        with patch("universal_engine.master_scheduler.save_state"):
            out = run_sanity_fight_pipeline(holder, cfg, state=state, candidate_stages=["TL-9", "1-7"], reason="test")
            self.assertEqual(out["selected_stage"], "1-7")
            self.assertEqual(len(out["attempted"]), 2)
            self.assertEqual(out["attempted"][0]["stage"], "TL-9")
            self.assertEqual(fake_sess.run_single_task.call_count, 3)

    def test_engine_check_and_auto_growth_ttl_sync_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "launcher_config.json"
            engine = MaaEngine(config_path=cfg_path)
            report = engine.check_environment(check_adb=False)
            self.assertIn("base_dir", report)
            self.assertIn("write_permission", report)

            # Verify AUTO mode triggers Depot/OperBox pre-sync when stale, and falls back smoothly if sync fails
            with patch.object(engine, "ensure_maa_ready", return_value={"ok": True, "maa_path": str(Path(tmp))}), \
                 patch("universal_engine.master_scheduler.is_data_stale", return_value={"depot_stale": True, "box_stale": True}), \
                 patch("universal_engine.master_scheduler.init_maa_sdk", return_value=True), \
                 patch("universal_engine.master_scheduler.ensure_game_running"), \
                 patch("universal_engine.master_scheduler.MaaSession") as mock_sess_cls, \
                 patch("universal_engine.master_scheduler.decide_optimal_stages") as mock_decide:
                sess = mock_sess_cls.return_value
                sess.connect.return_value = True
                sess.run_single_task.return_value = {"ok": True, "events": [], "log_lines": [], "elapsed": 1.0, "chain_errors": [], "sub_errors": []}
                mock_decide.return_value = MagicMock(recommended_stages=["1-7"], reason="fallback", shortages=[], open_today={}, active_event=None, data_staleness={})
                res = engine.run_fight(fight_mode="AUTO")
                submitted = [c.args[0] for c in sess.run_single_task.call_args_list]
                self.assertIn("Depot", submitted)
                self.assertIn("Fight", submitted)
                self.assertEqual(res["selected_stage"], "1-7")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest universal_engine.tests.test_master_scheduler -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `universal_engine/master_scheduler.py` and export public API in `universal_engine/__init__.py`**

Implement `load_state()` / `save_state()` (using `<base_dir>/data/scheduler_state.json`), `refresh_maa_cache()` (writing to `<base_dir>/cache/`), `run_annihilation_pipeline()`, `run_sanity_fight_pipeline()` (with `auto_growth` `AUTO` stage queue or manual override, plus exact 1-retry-then-`1-7` anti-deadlock rule), `MaaEngine` class, and CLI `main()` supporting `--mode {check,install,daily,annihilation,fight,push}`, `--server`, `--fight-mode`, `--post-action`, etc.

- [ ] **Step 4: Run full unit test suite + headless live self-check**

Run:
1. `python -m unittest discover -s universal_engine/tests -v`
2. `python -m universal_engine.master_scheduler --mode check --no-refresh-cache` (verifying parent MAA discovery in `<LOCAL_MAA_DIR>` without connecting/modifying anything!)
Expected: All unit tests pass with exit code 0, and CLI `--mode check` prints valid JSON discovering parent MAA integrity OK and zero personal hardcoded paths.
