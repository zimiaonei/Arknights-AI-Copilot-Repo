# PRTS · MAA 通用智能自主桌面控制台（Universal Engine）

基于 **CustomTkinter 浅色现代桌面面板** 与 **MAA Core SDK** 构建的明日方舟全自动日常、自主养成规划与 AI 协同推图系统。支持零 API Key 的「外部桌面 AI Agent 直连协同」与「内置 OpenAI 兼容 API」双模式。

---

## 1. 快速启动

```bash
# 进入 universal_engine 目录后直接启动桌面控制台
python launcher_gui.py

# 或从上级目录启动
python universal_engine/launcher_gui.py
```

首次启动（或未检测到 `MaaCore.dll`）时，会自动弹出 **3 步新手引导向导**：
1. **定位 MAA 内核**：自动扫描同级目录、父级目录、常见安装目录；未检测到时支持手动浏览绑定或一键自动下载最新版 `win-x64.zip`（含国内镜像源回退与断点续传）。
2. **连接模拟器与环境体检**：自动探测 MuMu / 雷电等模拟器 ADB 端口（默认 `127.0.0.1:16384`），校验目录写权限、核心资源完整性及 `16:9 (>=720p)` 分辨率。
3. **选择 AI 协同方式**：默认推荐「直连桌面 AI Agent（免 API Key）」，亦可随时切换为「内置 API Key」。

---

## 2. 双模式 AI 协同架构

### 模式 A：直连桌面 AI Agent（默认 · 免 API Key）

无需购买或填写任何 API Key，兼容 **Antigravity、Claude Code、Cursor、OpenClaw** 等桌面或终端 AI Agent：

- **日常零打扰全速运行**：日常收菜、每周剿灭分流、基于仓库与干员 Box 的 `auto_growth` 规则养成均在本地全速运行，不依赖任何大模型调用。
- **遇阻自动生成 `agent_task.json` 并监听恢复**：
  - 当推图（`run_push` / `auto_push` / 剿灭解锁）遇到**缺失关键干员**或**战斗翻车**时，引擎会自动将现场状态写入工作区根目录下的 `agent_task.json`：
    - `stage_code` / `stage_id`：当前关卡信息
    - `battle_state`：翻车击杀数 `kills`、费用 `costs`、最后 3 个动作、报错日志切片
    - `target_copilot_path`：待修改或新建的目标作业 JSON 文件路径
    - `screenshot_path`：现场失败截图路径
    - `my_box_path`：`data/my_box.json` 干员练度库路径
    - `tile_pos_file`：关卡地图格子坐标参考文件
  - 界面顶部会立即弹出醒目提醒条：**“已生成 agent_task.json，点击复制指令发给你的 AI Agent（如 Antigravity / Claude Code）”**。
  - 引擎后台开启文件监听：一旦你的桌面 AI Agent 修改并保存了 `target_copilot_path` 指向的作业 JSON，面板会自动检测文件变化并立即恢复下一轮战斗！
  - 如需人工跳过等待，也可随时点击提示条上的 **“▶ 用规则继续”** 使用本地确定性规则修轴。

### 模式 B：内置 OpenAI 兼容 API（静默自动调用）

在「设置 → AI 协同与内核」或引导向导第 3 步中填入：
- **Base URL**：支持 OpenAI、DeepSeek (`https://api.deepseek.com/v1`)、Gemini OpenAI 兼容端点、本地 Ollama (`http://127.0.0.1:11434/v1`)
- **API Key**：对应服务商密钥（本地 Ollama 可填 `ollama`）
- **Model Name**：如 `gpt-4o-mini`、`deepseek-chat`、`qwen2.5` 等

开启后，引擎在缺失干员替补与翻车修轴时自动静默请求模型输出结构化补丁，经本地干员 Box 与动作合法性校验后执行；若网络异常或输出非法 JSON，自动无感降级到本地纯 MAA 规则引擎。

---

## 3. 七大核心任务模式

| 模式卡片 | 功能说明 |
| :--- | :--- |
| **一键日常收菜** | 串联 `StartUp -> Recruit -> Infrast -> Mall -> Annihilation -> Fight -> Award`，自动保存战报与结束截图 |
| **智能自主养成** | 读取 `OperBoxData.json` 与 `DepotData.json`，按「当期活动材料 > 今日开放芯片本 > 常驻主线 T3 缺口 > 龙门币/经验 > 1-7 保底」自动决策 |
| **当期剿灭托管** | 自动识别本周一 04:00 刷新的剿灭上限；若未解锁当期剿灭，自动下载 PRTS 高赞作业并托管推通解锁 |
| **新活动/主线推图** | 输入关卡代码（如 `TL-9`、`14-20`，留空或填 `auto` 自动推当期活动关），自动抄作业、Box 替补适配、跳过剧情关与教学关交接 |
| **刷新干员与仓库档案** | 运行 MAA `OperBox` 与 `Depot` 识别任务，同步更新 `data/my_box.json` 与仓库材料库存 |
| **集成战略（肉鸽）** | 支持切换 **「纯 MAA 自动刷取」** 与 **「AI 辅助策略通关」**（局内战斗 100% 交由 MAA，局外选人/选路/事件与 Boss 战前 PRTS 接管由 AI/规则协同，支持超时自动规则兜底防卡死），内置 **「一键领奖与加点」** 自动领取等级奖励（如黑流树海「源流堆栈」）、月度委托（「本月委托栏」）并按「开局希望/费用/招募折扣 > 战斗属性 > 商店刷新」升级科技树（如「生命游戏」） |
| **生息演算** | 自动托管生息演算（支持重启锚点 `RA-1`/`RA-4`/`RA-15` 关卡循环与沙洲遗闻 `ProsperityNoSave`/`ProsperityInSave`），内置主界面「终端 -> 长期探索 -> 生息演算（重启锚点）」入口自动导航、自动搬空商店与卡片内一键启停 |

> **原版 MAA 配置同步与继承**：未在启动器显式覆盖的肉鸽/生息演算字段会自动继承 MAA 原版 `config/gui.new.json`；亦可在「设置 → 肉鸽与生息演算」中点击 **「一键同步原版 MAA 配置」** 直接导入主题、分队、职业组与刷取模式。

---

## 4. 目录结构与核心文件

```text
universal_engine/
├── launcher_gui.py              # 浅色现代风 CustomTkinter 桌面启动面板
├── master_scheduler.py          # 总控调度引擎（GUI API & CLI 入口）
├── maa_runner.py                # 动态 MaaCore SDK 桥接、模拟器控制、肉鸽/生息与 Agent 文件监听
├── roguelike_ai_copilot.py      # 集成战略局外 AI 策略副驾驶 + 多主题自动领奖与科技树加点
├── stage_pilot.py               # ADB 视觉/OCR 导航、剧情跳过与教学关人工交接
├── ai_strategist.py             # OpenAI 兼容多模态客户端 + 纯 MAA 规则适配与修轴基线
├── auto_growth.py               # 仓库/Box 缺口分析与刷图决策引擎
├── utils/
│   ├── env_doctor.py            # 路径锚定、写权限/资源完整性/ADB 16:9 体检
│   └── maa_installer.py         # MAA 自动检索、手动绑定、镜像加速下载与断点续传
├── config/launcher_config.json  # 启动器全量配置文件
├── data/                        # my_box.json / scheduler_state.json / latest_*_report.json
├── copilots/                    # 自动适配与 AI 修轴生成的作业 JSON
├── reports/                     # 每日战报、推图/肉鸽/生息战报与现场截图
└── agent_task.json              # 外部 AI Agent 协同任务文件（按需动态生成）
```

---

## 5. 无头 CLI 命令行用法

脱离图形界面时，同样可以直接通过命令行调度：

```bash
# 环境与养成决策体检
python -m universal_engine.master_scheduler --mode check

# 执行全自动日常（指定 B 服，完成后关闭模拟器）
python -m universal_engine.master_scheduler --mode daily --server Bilibili --post-action close_emulator

# 指定推图（如当期活动全关或单关）
python -m universal_engine.master_scheduler --mode push --stages auto
python -m universal_engine.master_scheduler --mode push --stages TL-9

# 集成战略（肉鸽）自动刷取或开启 AI 策略辅助
python -m universal_engine.master_scheduler --mode roguelike --theme BlackFlow --rl-mode Exp
python -m universal_engine.master_scheduler --mode roguelike --theme BlackFlow --ai-copilot

# 生息演算自动刷取
python -m universal_engine.master_scheduler --mode reclamation --theme RelaunchAnchor --ra-mode RA1 --clear-store
```
