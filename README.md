# PRTS · 明日方舟 AI 智能协同与全自动调度控制台 (Arknights AI Copilot)

> [!WARNING]
> ### ⚠️ 重要免责与合规声明 (Disclaimer)
> 1. **独立第三方开源工具**：本项目为独立的第三方开源工具，与 **MaaAssistantArknights (MAA) 官方开发团队**、**上海鹰角网络科技有限公司 (Hypergryph)** 或 **Yostar, Inc.** 没有任何商业关联、官方背书或授权关系。
> 2. **严禁向官方反馈本衍生工具问题**：使用本修改版 / AI 协同工具过程中遇到的任何问题、Bug、异常或建议，**严禁** 前往 MAA 官方 QQ 交流群、Discord 社区或 MAA 官方 GitHub Issues 进行提问或反馈！如遇问题请在本项目的 GitHub Issues 独立讨论。
> 3. **游戏账号风险自负**：自动化辅助工具可能违反游戏运营方的用户服务协议（Terms of Service）。请在知情并自愿承担一切潜在风险的前提下使用本项目，因使用本项目产生的任何账号限制、封禁或损失，本项目及开发者概不负责。
> 4. **非分发声明**：本项目遵循开源合规标准，仓库与发行版均 **不打包、不分发** 任何官方 MAA 预编译二进制文件（如 `MaaCore.dll`、`MAA.exe` 等）、OCR 神经网络模型及明日方舟游戏美术资源。详见 [NOTICE.md](NOTICE.md)。

---

## 📖 项目简介 (Overview)

**Arknights AI Copilot (Universal Engine)** 是基于 **MAA Core SDK** 与现代化 **EdgeChromium WebView2 (`pywebview`)** 桌面界面打造的明日方舟全自动日常托管、自主养成决策与智能 AI 协同推图系统。

系统提供精美的响应式 Web 现代桌面控制台（位于 `universal_engine/gui/web/`），告别臃肿的传统 GUI，并创新支持零 API Key 门槛的 **「外部桌面 AI Agent 直连协同」** 与 **「内置大模型兼容 API」** 双模架构。

---

## ✨ 核心特性

- **轻量现代桌面端**：基于 `pywebview` + Microsoft Edge WebView2 构建，单文件高颜值响应式 UI，零冗余第三方界面库负担。
- **免 API Key 的桌面 AI 直连协同**：
  - 支持 **Antigravity、Claude Code、Cursor、OpenClaw** 等本地 AI 编程 Agent。
  - 遇到作业断轴、卡费或缺干员时，自动导出结构化上下文现场 `agent_task.json`。
  - 监听作业修改，桌面 AI 修复作业后自动热重载并无缝恢复战斗。
- **内置大模型自动修轴**：支持标准 OpenAI 格式接口（OpenAI, DeepSeek, Qwen, Ollama 等），无需外部 Agent 亦可静默完成智能替补与修轴。
- **全套托管模式**：一键日常收菜、智能自主养成规划、当期剿灭自动分流与推通、新活动/主线自动推图、干员与仓库档案同步、集成战略（肉鸽）自动刷取与策略通关、生息演算长期托管。
- **MAA 极速引导与生态兼容**：内置 3 步开箱向导，自动检测/绑定本地 MAA 或一键加速下载官方最新 Release，完全继承原版 MAA 配置。

---

## 🚀 快速上手 (Quick Start)

### 1. 环境准备

确保已安装 **Python 3.10+** 及 **Windows 10/11**（系统自带 Edge Chromium WebView2 运行时）。

```bash
# 克隆本仓库
git clone https://github.com/your-username/Arknights-AI-Copilot-Repo.git
cd Arknights-AI-Copilot-Repo

# 安装运行依赖
pip install -r requirements.txt
```

### 2. 启动桌面控制台 (GUI)

```bash
python universal_engine/launcher_gui.py
```

首次运行会自动弹出 **3 步极速向导**：
1. **绑定 MAA 内核**：自动识别同级或常见路径，支持一键加速下载官方原版 `win-x64.zip`。
2. **模拟器体检**：自动检测 MuMu / 雷电等模拟器 ADB 端口与 16:9 分辨率。
3. **选择 AI 协同方案**：选择「桌面 AI Agent 直连（推荐，免 API Key）」或填写兼容 API Key。

### 3. 无头命令行运行 (CLI)

脱离图形界面时，可直接通过总控调度器执行任务：

```bash
# 环境与养成决策体检
python -m universal_engine.master_scheduler --mode check

# 全自动日常托管（完成后自动关闭模拟器）
python -m universal_engine.master_scheduler --mode daily --server Bilibili --post-action close_emulator

# 自动推图（自动识别活动关卡或指定关卡）
python -m universal_engine.master_scheduler --mode push --stages auto
python -m universal_engine.master_scheduler --mode push --stages TL-9

# 集成战略（肉鸽）刷取或开启 AI 辅助策略
python -m universal_engine.master_scheduler --mode roguelike --theme BlackFlow --rl-mode Exp
python -m universal_engine.master_scheduler --mode roguelike --theme BlackFlow --ai-copilot

# 生息演算托管与商店兑换
python -m universal_engine.master_scheduler --mode reclamation --theme RelaunchAnchor --ra-mode RA1 --clear-store
```

---

## 🤖 双模式 AI 协同架构

```
┌────────────────────────────────────────────────────────┐
│           Arknights AI Copilot (Universal Engine)      │
└───────────────────────────┬────────────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
   【模式 A: 桌面 Agent 直连】    【模式 B: 内置兼容 API】
   - 免 API Key / 零额外成本       - 支持 OpenAI / DeepSeek / Ollama
   - 导出 agent_task.json 现场    - 自动请求结构化修轴 Patch
   - Antigravity / Claude Code    - 本地 Box 语法合法性自校验
   - 文件监听，作业保存即自恢复   - 异常自动无感降级本地规则
```

### 模式 A：直连桌面 AI Agent（默认 · 免 API Key）
推图或剿灭遇阻（缺干员、练度不足或战斗翻车）时：
1. 引擎在工作区根目录生成 `agent_task.json`（包含当前关卡、击杀数、费用、失败截图、干员 Box 库及作业路径）。
2. 控制台顶部提示条提供一键复制指令，通知你的桌面 AI（例如 Antigravity）。
3. 桌面 AI 调整作业 JSON 并保存后，引擎后台文件监视器自动捕获更新并立即发起新一轮挑战。

### 模式 B：内置 OpenAI 兼容 API
在控制台「设置 → AI 协同与内核」配置 Base URL（支持 DeepSeek、OpenAI、Gemini 或本地 Ollama）、API Key 与模型名。在遇阻时直接静默调用模型推理生成补丁，并经本地干员库校验后生效。

---

## 📦 七大任务模式一览

| 模式 | 运行机制与核心亮点 |
| :--- | :--- |
| **一键日常收菜** | 串联基建换班、信用商店、公招招募、日常战斗、自动领奖与战报导出 |
| **智能自主养成** | 动态分析干员 Box 练度与仓库储备，依据当期活动/素材缺口优先级自动分配理智 |
| **当期剿灭托管** | 智能识别周常合成玉上限与刷新周期，未解锁时自动拉取高赞作业实装推通 |
| **新活动/主线推图** | 填入关卡代码（留空自动探索），支持作业解析、缺失干员自适应与剧情关卡自动交接 |
| **干员与仓库档案同步** | 周期性拉取本地干员练度 `data/my_box.json` 与仓库物资表，为养成与修轴提供依据 |
| **集成战略（肉鸽）** | 支持纯 MAA 自动刷取与 AI 局外策略通关双模式，内置主题奖励一键领取与科技树升级 |
| **生息演算长期托管** | 支持重启锚点关卡循环与沙洲遗闻，全自动入口导航、建筑/采集与商店资源搬空 |

---

## 🛠️ 源码构建单文件可执行程序 (Building from Source)

项目内置了针对现代 Web 界面的 PyInstaller 构建脚本，可打包出轻量无黑框控制台的单文件可执行文件 `Arknights_AI_Launcher.exe`：

```bash
# 确保安装了构建依赖
pip install pyinstaller packaging

# 执行打包构建脚本
python universal_engine/gui/build_launcher.py
```

构建生成的独立运行程序位于项目根目录 `Arknights_AI_Launcher.exe`。

---

## 📜 开源许可与第三方致谢 (License & Notice)

- 本项目采用 **[GNU Affero General Public License v3.0 (AGPL-3.0-only)](LICENSE)** 开放源代码。
- 本项目严守开源许可与版权合规要求，第三方组件、底层 MAA 运行时关联及非分发声明详情请参见 **[NOTICE.md](NOTICE.md)**。
