# Notice & Third-Party Software Declarations

This project, **Arknights AI Copilot** (Universal Engine), is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0-only)**. See [LICENSE](LICENSE) for the full license text.

---

## Non-Distribution Declaration (非分发声明)

> [!IMPORTANT]
> **This repository and its binary releases do NOT bundle, distribute, or mirror any official MAA binaries, models, resource packs, or *Arknights* game assets.**

Specifically:
- **No MAA Executables or Dynamic Link Libraries**: Neither `MAA.exe`, `MaaCore.dll`, `DirectML.dll`, `onnxruntime_maa.dll`, `opencv_world4_maa.dll`, nor other compiled C/C++ runtime artifacts are stored or distributed in this repository or its release packages.
- **No OCR Models or Pipeline Templates**: No OCR neural network models (`*.onnx`), pipeline task definitions, or `resource/` templates are bundled.
- **No Game Assets**: No images, audio, characters, graphics, or proprietary assets belonging to Hypergryph (上海鹰角网络科技有限公司) or Yostar are included.

**Runtime Binding & Acquisition**:
Users must provide their own local installation of [MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights), or use the built-in wizard to bind an existing MAA path or download the official release directly from official upstream release channels at runtime.

---

## Upstream Project Attribution

- **MaaAssistantArknights (MAA)**
  - **Homepage**: https://github.com/MaaAssistantArknights/MaaAssistantArknights
  - **License**: GNU Affero General Public License v3.0 (`AGPL-3.0-only`)
  - **Role**: Upstream core automation framework and native runtime (`MaaCore.dll`). This repository dynamically links and interacts with MAA via its C/C++ FFI and CLI/API interfaces at runtime; it is not bundled.

---

## Third-Party Dependencies and Licenses

The following dependencies are used by Arknights AI Copilot. Their licenses and attributions have been verified from package metadata:

### 1. Runtime Libraries

| Package | Version | License | License Identifier (SPDX) | Notes / Metadata |
| :--- | :--- | :--- | :--- | :--- |
| **pywebview** | `6.2.1` | BSD 3-Clause License | `BSD-3-Clause` | GUI engine (EdgeChromium WebView2 bridge) |
| **pythonnet** | `3.2.1` | MIT License | `MIT` | Python-.NET interop runtime |
| **clr_loader** | `0.3.1` | MIT License | `MIT` | CLR hosting loader for pythonnet |
| **bottle** | `0.13.4` | MIT License | `MIT` | Internal lightweight HTTP server for WebView2 |
| **proxy_tools** | `0.1.0` | MIT License | `MIT` | Proxy helper utilities |
| **Pillow** | `13.0.0` | HPND / MIT-CMU | `MIT-CMU` | Image processing & analysis |
| **numpy** | `2.5.5` | BSD 3-Clause License | `BSD-3-Clause` | Metadata: `BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0` |
| **typing_extensions** | `4.16.0` | Python Software Foundation License 2.0 | `PSF-2.0` | Backport of typing enhancements |
| **cffi** | `2.1.1` | MIT License | `MIT-0` | C Foreign Function Interface |
| **pycparser** | `3.12` | BSD 3-Clause License | `BSD-3-Clause` | C parser in Python (cffi dependency) |

### 2. Build & Packaging Tools

| Package | Version | License | License Identifier (SPDX) | Notes / Metadata |
| :--- | :--- | :--- | :--- | :--- |
| **PyInstaller** | `6.22.3` | GPL-2.0-or-later with Bootloader Exception | `GPL-2.0-or-later` | Used to build standalone binaries; output is not tainted by GPL due to exception |
| **pyinstaller-hooks-contrib** | `2026.10` | Apache-2.0 OR GPL-2.0-or-later | `Apache-2.0 OR GPL-2.0-or-later` | Community hooks for PyInstaller |
| **pefile** | `2024.8.26` | MIT License | `MIT` | Portable Executable parsing library |
| **pywin32-ctypes** | `0.2.3` | BSD 3-Clause License | `BSD-3-Clause` | Windows ctypes utilities |
| **altgraph** | `0.17.5` | MIT License | `MIT` | Graph computation utilities for packaging |
| **packaging** | `26.3` | Apache-2.0 OR BSD-2-Clause | `Apache-2.0 OR BSD-2-Clause` | Core packaging utilities |

---

## Trademark & Copyright Notices

- **Arknights (明日方舟)** and related character names, art, icons, and game content are registered trademarks and copyrights of **Hypergryph Co., Ltd. (上海鹰角网络科技有限公司)** and **Yostar, Inc.**.
- **MaaAssistantArknights (MAA)** is copyright © 2021-present by the MAA Project Team.
- All other trademarks and registered trademarks are the property of their respective owners.
