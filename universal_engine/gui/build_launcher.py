"""PyInstaller Build Script for Arknights_AI_Launcher.exe.

Produces a single-file, no-console, lightweight modern executable with bundled
web assets, EdgeChromium/pywebview bindings, and zero CustomTkinter bloat.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRY_SCRIPT = REPO_ROOT / "universal_engine" / "launcher_gui.py"
WEB_DIR = REPO_ROOT / "universal_engine" / "gui" / "web"
ICON_PATH = REPO_ROOT / "assets" / "app_icon.ico"
EXE_TARGET = REPO_ROOT / "Arknights_AI_Launcher.exe"


def build_launcher() -> int:
    """Invoke PyInstaller to compile Arknights_AI_Launcher.exe."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    if not ICON_PATH.is_file():
        raise FileNotFoundError(f"Application icon not found at: {ICON_PATH}")

    from PyInstaller.utils.hooks import collect_submodules

    ue_submodules = collect_submodules(
        "universal_engine",
        filter=lambda name: name != "universal_engine.tests" and not name.startswith("universal_engine.tests."),
    )
    if "universal_engine.gui.app" not in ue_submodules:
        raise RuntimeError(
            f"collect_submodules failed to locate universal_engine.gui.app! Found modules: {ue_submodules}"
        )

    sep = ";" if sys.platform == "win32" else ":"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name",
        "Arknights_AI_Launcher",
        "--icon",
        str(ICON_PATH),
        "--paths",
        str(REPO_ROOT),
        "--distpath",
        str(REPO_ROOT),
        "--add-data",
        f"{WEB_DIR}{sep}universal_engine/gui/web",
        "--add-data",
        f"{REPO_ROOT / 'universal_engine' / 'README.md'}{sep}universal_engine",
        "--collect-all",
        "webview",
        "--collect-all",
        "pythonnet",
        "--collect-all",
        "clr_loader",
    ]
    for sub in ue_submodules:
        cmd.extend(["--hidden-import", sub])

    cmd.extend([
        "--hidden-import",
        "webview.platforms.edgechromium",
        "--hidden-import",
        "clr_loader",
        "--hidden-import",
        "pythonnet",
        "--hidden-import",
        "bottle",
        "--hidden-import",
        "proxy_tools",
        "--hidden-import",
        "PIL",
        "--hidden-import",
        "PIL.Image",
        "--exclude-module",
        "customtkinter",
        "--exclude-module",
        "universal_engine.tests",
        str(ENTRY_SCRIPT),
    ])

    print("[BUILD] Compiling Arknights_AI_Launcher.exe with PyInstaller...")
    print(f"[BUILD] Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        print(f"[BUILD ERROR] PyInstaller failed with exit code {result.returncode}", file=sys.stderr)
        return result.returncode

    # Clean up intermediate build folders and spec file
    build_dir = REPO_ROOT / "build"
    spec_file = REPO_ROOT / "Arknights_AI_Launcher.spec"
    if build_dir.is_dir():
        shutil.rmtree(build_dir, ignore_errors=True)
    if spec_file.is_file():
        try:
            spec_file.unlink()
        except Exception:
            pass

    if EXE_TARGET.is_file():
        size_mb = EXE_TARGET.stat().st_size / (1024 * 1024)
        print(f"[BUILD SUCCESS] Arknights_AI_Launcher.exe created successfully ({size_mb:.2f} MB)")
        return 0

    print(f"[BUILD ERROR] Expected executable not found at: {EXE_TARGET}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(build_launcher())
