# MAA 通用后端引擎架构设计文档

**日期**: 2026-11-18  
**输出目录**: `universal_engine/`（位于工作区根目录下，完全独立于原根目录脚本）  
**目标状态**: 独立可导入/可 CLI 运行的通用 Python 后端引擎（暂不含 GUI 界面与 PyInstaller 打包脚本）

---

## 1. 意图与背景（Why）

现有的 [master_scheduler.py](../../master_scheduler.py) 与 [autonomous_agent.py](../../autonomous_agent.py) 验证了"全自动日常 + 周常剿灭分流 + PRTS 抄作业自主推图 + 日志自愈修轴"的闭环能力，但代码强耦合于作者本机环境：
1. 硬编码 `<LOCAL_MAA_DIR>`、`<LOCAL_EMULATOR_DIR>`、`<PERSONAL_PATH>`、启动脚本中的 Python 路径；
2. 硬编码 `ADB_ADDR = "127.0.0.1:16384"`、`PACKAGE_BILIBILI`、`penguin_id = "<PERSONAL_PENGUIN_ID>"` 与 MuMu12 专属命令；
3. 假设用户本机已安装 MAA 且 `my_box.json` 已就绪，缺乏环境自检、自动寻址/下载 MAA、通用 OpenAI AI 军师（含无 Key 降级）以及基于仓库缺口的自主养成决策能力。

本次重构旨在将上述能力沉淀为一个**零个人硬编码、脱离 Antigravity 独立运行、开箱可自检/自安装 MAA、且为下一步浅色桌面 GUI 提供清晰无状态/回调友好接口**的通用后端引擎。

---

## 2. 目标、非目标与反目标

### 目标（Goals）
- **全相对路径与打包态兼容**：以 `get_base_dir()` 统一锚定读写目录（源码态为 `universal_engine/` 根目录，PyInstaller 打包态为 `sys.executable` 所在目录，绝不写入临时 `_MEIPASS`）。
- **环境体检（`utils/env_doctor.py`）**：写权限检查（防 `C:\Program Files` 无权限报错）、MAA 核心文件与资源完整性校验（`MaaCore.dll`、`resource/`）、ADB 自动定位与模拟器 16:9 分辨率体检。
- **MAA 自动检索与安装（`utils/maa_installer.py`）**：按“同级目录 -> 父级目录 -> 常见安装路径 -> `config/launcher_config.json`”检索有效 MAA；未找到时支持“手动绑定路径”或“一键拉取最新 `win-x64.zip`”（GitHub Releases API + `MaaRelease`/`api.maa.plus`/国内镜像兜底 + `Range` 断点续传 + 进度/取消回调），解压至 `<base_dir>/MAA_Core/` 并写回配置。
- **AI 军师双模运行（`ai_strategist.py`）**：标准库 HTTP 客户端对接 OpenAI 兼容 `/chat/completions` 接口（支持 Gemini / DeepSeek / 本地 Ollama），辅助 Box 作业适配与日志驱动修轴；未填 `api_key` 或 LLM 返回异常/非法 JSON 时无感降级为纯 MAA 确定性规则引擎。
- **自主养成决策（`auto_growth.py`）**：结合 `DepotData.json`（或现跑 `Depot`）与 `my_box.json` 缺口、星期开放日历及当期 `StageActivityV2.json`，在 `AUTO` 模式下动态决策今日最优材料/芯片/活动关；完整保留"指定关卡失败最多重试 1 次后自动回退 `1-7`"的防卡死保底规则。
- **通用配置与收尾动作**：支持 `Official`（官服）/`Bilibili`（B服）切换、通用多模拟器/ADB 连接，以及完成后动作（`none` / `close_emulator` / `shutdown`）。
- **面向未来 GUI 的接口**：全部核心能力可作为 Python API 导入调用，支持结构化返回值、日志回调、`progress_callback` 和 `cancel_event`。

### 非目标（Non-Goals）
- 本阶段**不实现**任何 GUI 界面（Tkinter/PySide/Fluent/WebView 等）与打包配置（`.spec`/InnoSetup）。
- 本阶段**不修改**原根目录下的 `master_scheduler.py`、`autonomous_agent.py`、`config/`、`一键启动全自动日常.bat` 等既有文件。
- 本阶段**不引入**任何第三方 `pip` 运行时依赖（如 `requests`、`openai`、`pydantic`）。

### 反目标（Anti-Goals）
- **禁止**破坏或覆盖根目录现有的个人日常脚本。
- **禁止**在未配置 API Key 时阻塞或报错（必须平滑降级至纯 MAA 模式）。
- **禁止**在 `AUTO` 模式下因未解锁关卡或材料关失败导致反复刷图卡死或空打已满 1800 的剿灭。
- **禁止**库函数内部调用 `sys.exit()` 或阻塞式 `input()`，以免未来接入 GUI 主线程时崩溃。

---

## 3. 约束条件（Constraints）

1. **运行环境**：Windows（主要目标平台，兼容 Windows 路径、CREATE_NO_WINDOW、写权限探测与 `shutdown`），Python 3.10+ 标准库实现（零 `pip` 外部依赖）。
2. **工作区隔离**：所有新文件放在 `universal_engine/` 下；引擎读写的用户配置、状态、报告、日志、下载的 `MAA_Core/` 默认均置于 `get_base_dir()` 内部（当检索到外部已有 MAA 如父目录时，仅将外部路径记录于 `launcher_config.json` 并把 `user_dir` / 引擎状态保存在引擎自身目录或明确区分）。
3. **MAA Core 动态加载**：`MAA-v6.17.5-win-x64` 不自带 `adb.exe`；`Asst` 类位于 `<maa_path>/Python/asst/asst.py`。引擎需在确定 `maa_path` 后动态将其 `Python/` 注入 `sys.path` 并通过 `Asst.load(path=..., incremental_path=..., user_dir=...)` 加载，且进程内需防止重复 `Asst.load` 到错误路径。
4. **隐私与通用性**：清除所有个人路径（`<LOCAL_MAA_DIR>`、`<PERSONAL_PATH>`）、个人企鹅物流 ID（默认 `""`）、硬编码 `"Bilibili"` 兜底与固定模拟器端口假设。

---

## 4. 架构方案对比（Approaches）

| 维度 | 方案 1（已选定）：纯标准库 + 动态加载目标 MAA `Python/asst` + 双模规则基线 | 方案 2（弃用）：`requests`/`openai` 依赖 + 内置静态 `asst` 副本 |
| :--- | :--- | :--- |
| **依赖与打包** | 零第三方包，仅用 Python 标准库（`urllib.request`、`http.client`、`zipfile`、`ctypes`、`subprocess`、`threading`），GUI 打包最轻 | 需用户额外 `pip install`，PyInstaller 打包需处理更多 SSL/依赖钩子 |
| **MAA 内核兼容** | 从定位/下载到的 `maa_path/Python` 动态导入 `Asst`，与用户实际下载的最新 `MaaCore.dll` 版本始终匹配 | 内置旧版 `asst.py` 绑定在 MAA 升级 C 接口后可能崩溃 |
| **下载器可控性** | 标准库重写支持 `Range` 断点续传、临时 `.part` 校验、国内镜像回退和字节级回调 | 原 `Python/asst/downloader.py` 依赖 `requests`、写死相对 `temp/` 且无结构化进度回调 |
| **AI 降级能力** | 规则引擎与 LLM 严格分离，无 Key 或请求失败零感知回退 | 若耦合 `openai` SDK，无 Key 初始化易引入额外状态分支 |

---

## 5. 系统架构与模块详细设计

### 5.1 目录结构

```text
universal_engine/
├── __init__.py
├── config/
│   └── launcher_config.json      # 通用默认配置模板（首次缺失时自动生成）
├── utils/
│   ├── __init__.py
│   ├── env_doctor.py             # 相对路径基址、权限/完整性/ADB 16:9 体检
│   └── maa_installer.py          # MAA 自动检索、手动绑定、GitHub/镜像下载与解压
├── ai_strategist.py              # OpenAI 兼容客户端 + 无 Key 纯 MAA 规则适配器与修轴器
├── auto_growth.py                # Depot + Box 缺口分析 + 活动/芯片/材料关卡智能决策
├── maa_runner.py                 # 动态加载 Asst/MaaCore、模拟器/ADB 桥接、会话与动作收尾
├── master_scheduler.py           # 总控调度引擎（API + CLI 入口）
├── docs/
│   └── 2026-11-18-universal-backend-design.md
└── tests/
    └── test_backend_selfcheck.py # 无界面导入、单元测试与自检套件
```

运行时自动生成目录（全部相对 `get_base_dir()`）：
- `config/launcher_config.json`（配置持久化）
- `data/scheduler_state.json`（周常剿灭缓存、`last_battle_is_annihilation`、`last_daily`）
- `data/my_box.json`（由 MAA 的 `data/OperBoxData.json` 标准化生成，**不写入绝对 source 路径**或仅存相对/可移植描述）
- `copilots/`（适配与修轴生成的临时作业 JSON）
- `reports/`（每日战报、推图战报、现场截图）
- `MAA_Core/`（自动下载解压时的默认安装目录）

---

### 5.2 配置模型（`config/launcher_config.json`）

采用分层字典设计，加载时与内置 `DEFAULT_LAUNCHER_CONFIG` 深度合并（向后兼容新增字段）：

```json
{
  "maa_path": "",
  "maa_path_pinned": false,
  "server": "Official",
  "emulator": {
    "type": "auto",
    "path": "",
    "adb_path": "",
    "adb_address": "",
    "instance_index": 0,
    "touch_type": "minitouch",
    "auto_launch": true
  },
  "ai": {
    "enabled": false,
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model_name": "gpt-4o-mini",
    "timeout_sec": 30
  },
  "daily": {
    "fight_mode": "AUTO",
    "override_stage": "",
    "medicine": 0,
    "stone": 0,
    "auto_push": false,
    "max_push_rounds": 5,
    "sync_box_after_daily": false,
    "skip_tasks": [],
    "report_to_penguin": false,
    "penguin_id": "",
    "report_to_yituliu": false,
    "drones": "Money",
    "dorm_threshold": 0.3,
    "fiammetta_enabled": false,
    "fiammetta_targets": [],
    "continue_training": true,
    "mall_first": ["招聘许可", "赤金", "龙门币", "技巧概要·卷2", "技巧概要·卷1", "基础作战记录", "初级作战记录", "固源岩", "糖", "酯原料", "异铁", "酮凝集", "装置"],
    "mall_blacklist": ["碳", "家具", "加急许可", "讯使", "嘉维尔", "坚雷"]
  },
  "growth": {
    "prefer_event": true,
    "allow_chip_stages": true,
    "allow_resource_stages": true,
    "auto_sync_before_fight": true,
    "depot_sync_ttl_hours": 24,
    "chip_untargeted_cap": 2,
    "fold_craftable_materials": true,
    "target_operators": [],
    "material_safety_stock": {
      "T3_DEFAULT": 30,
      "CHIP_T1": 5,
      "CHIP_T2": 8
    }
  },
  "post_action": "none"
}
```

- **`server` 映射表**（来自 MAA `resource/config.json`）：
  - `"Official"` -> `package = "com.hypergryph.arknights"`, `maa_server = "CN"`, `client_type = "Official"`
  - `"Bilibili"` -> `package = "com.hypergryph.arknights.bilibili"`, `maa_server = "CN"`, `client_type = "Bilibili"`
- **`post_action` 枚举**：`"none"` | `"close_emulator"` | `"shutdown"`

---

### 5.3 模块一：`utils/env_doctor.py`

**职责**：统一相对路径计算与运行前环境体检（不依赖 `MAA_Core` 已加载即可执行）。

**核心机制**：
1. **`get_base_dir() -> Path`**：
   - 若 `getattr(sys, "frozen", False)` 为真：返回 `Path(sys.executable).resolve().parent`（兼容 PyInstaller onedir/onefile，确保配置/缓存落在 exe 同级可写目录而非临时解包目录）。
   - 否则（源码态）：返回 `universal_engine` 根目录（即 `Path(__file__).resolve().parent.parent`）。
   - 同步提供 `resolve_path(*parts: str) -> Path` 与 `ensure_runtime_dirs() -> Dict[str, Path]`。
2. **写权限检查 `check_write_permission(target_dir: Optional[Path] = None) -> Dict[str, Any]`**：
   - 在目标目录创建并写入一个随机临时探测文件（如 `.write_probe_<pid>.tmp`），写入后立即 `unlink()`；捕获 `PermissionError`/`OSError`，重点防御解压在 `C:\Program Files` 等受保护目录时的静默失败。
3. **MAA 内核完整性检查 `check_maa_integrity(maa_dir: Union[str, Path]) -> Dict[str, Any]`**：
   - 校验目录存在且包含：
     - 核心库：`MaaCore.dll`（Windows；非 Windows 可兼容 `libMaaCore.so`/`libMaaCore.dylib`）、`MaaUtils.dll`（若存在则记录）、`Python/asst/asst.py`
     - 资源目录：`resource/stages.json`、`resource/item_index.json`、`resource/recruitment.json`、`resource/config.json`、`resource/tasks/`
   - 返回 `{valid: bool, maa_dir: str, missing_items: List[str], version_hint: str, reason: str}`。注意不直接加载 DLL 以免占用文件锁（适合安装器覆盖升级前检查）。
4. **ADB 定位与 16:9 分辨率体检**：
   - `discover_adb_executable(config: Optional[Dict[str, Any]] = None, maa_dir: Optional[Path] = None) -> Optional[str]`：
     按优先级探测：`config.emulator.adb_path` -> `maa_dir/config/gui.new.json` 中 `ConnectSettings.AdbPath` -> `shutil.which("adb")` -> 常见 Windows 模拟器安装目录（MuMu12 `nx_main\adb.exe`、雷电 `LDPlayer*\adb.exe`、蓝叠 `HD-Adb.exe`、夜神 `nox_adb.exe`）。
   - `check_adb_resolution(adb_path: str, adb_address: str = "", timeout_sec: int = 8) -> Dict[str, Any]`：
     - 若未指定 `adb_address`，先尝试 `adb devices` 列出在线设备，或探测常见端口（`127.0.0.1:16384`、`127.0.0.1:5555`、`emulator-5554`）。
     - 执行 `adb -s <addr> shell wm size`，兼容 `Physical size: 1920x1080` 与 `Override size: 1280x720`（优先取 `Override size`）；计算长宽比（取 `max(w,h) / min(w,h)` 兼容横竖屏报告），判断 `abs(ratio - 16/9) <= 0.02` 且最小边 `>= 720`。
     - 返回 `{connected: bool, adb_path: str, adb_address: str, width: Optional[int], height: Optional[int], is_16_9: bool, warning: str}`。
5. **一键体检汇总 `run_env_diagnostics(config: Optional[Dict[str, Any]] = None, check_adb: bool = True) -> Dict[str, Any]`**：
   聚合 `base_dir`、写权限、当前发现的 MAA 目录完整性、ADB 16:9 检查，返回结构化报告供 CLI `--mode check` 与未来 GUI 诊断面板直接渲染。

---

### 5.4 模块二：`utils/maa_installer.py`

**职责**：MAA 目录自动发现、用户手动绑定校验、最新版释放接口查询、国内镜像兜底与断点续传解压。

**核心机制**：
1. **自动检索与配置固定优先级 `resolve_maa_path(config_path)` / `find_existing_maa(config_path)`**：
   - 若 `launcher_config.json` 中 `maa_path_pinned == true` 且 `maa_path` 通过完整性校验，直接优先采用该手动绑定路径（若已失效则自动回退到下方自动检索）。
   - 未固定（默认自动发现）时，`find_existing_maa()` 严格按以下顺序检索（每步调用 `env_doctor.check_maa_integrity()`，首个通过即返回，不加载 DLL）：
     1. **同级目录**：`<base_dir>` 自身，以及 `<base_dir>/MAA_Core`、`<base_dir>` 下同级子目录中名称含 `MAA` 的目录；
     2. **父级目录**：`<base_dir>.parent` 自身（覆盖引擎放在 `MAA-v6.17.5-win-x64/universal_engine/` 下的场景），以及 `<base_dir>.parent` 下名称含 `MAA` 的兄弟目录（如 `../MAA-*`）；
     3. **常见安装路径**：`C:\MAA`、`D:\MAA`、`C:\Program Files\MAA`、`%LOCALAPPDATA%\MAA`、`%USERPROFILE%\Desktop\MAA*`；
     4. **配置文件记录**：`config/launcher_config.json` 中保存的 `maa_path`。
2. **手动指定路径 `bind_manual_maa_path(user_path: Union[str, Path], config_path: Optional[Path] = None) -> Dict[str, Any]`**：
   - 调用 `check_maa_integrity` 校验路径；合法则将路径与 `"maa_path_pinned": true` 持久化写入 `config/launcher_config.json` 并返回成功结果，否则返回缺失项清单。
3. **版本探测与镜像链接构造 `fetch_latest_release_info(timeout_sec: int = 15) -> Dict[str, Any]`**：
   - 首选请求用户指定的 GitHub Releases API：`https://api.github.com/repos/MaaAssistantArknights/MaaAssistantArknights/releases/latest`，在 `assets` 中匹配 `win-x64.zip`（正则如 `^MAA-.*-win-x64\.zip$` 或后缀 `win-x64.zip`）。
   - **现实兜底**：若上述仓库无 `win-x64.zip` 资产（因为 MAA 官方已将预编译包分流至 `MaaRelease` 仓库与 `api.maa.plus`），自动串联兜底：
     1. `https://api.github.com/repos/MaaAssistantArknights/MaaRelease/releases/latest`
     2. `https://api.maa.plus/MaaAssistantArknights/api/version/summary.json` -> `stable.detail`（读取其中的 `assets` 与官方镜像列表）。
   - 为所有 GitHub `github.com` / `release-assets` 下载 URL 自动派生国内镜像候选列表（如 `https://ghfast.top/<url>`、`https://gh-proxy.com/<url>`、`https://mirror.ghproxy.com/<url>`，以及 `api.maa.plus` 提供的官方 CDN 镜像），形成有序候选 URL 队列。
4. **断点续传下载与解压 `download_and_install_maa(...) -> Dict[str, Any]`**：
   - 支持回调：`progress_callback: Optional[Callable[[int, int, str], None]]`（`(downloaded_bytes, total_bytes, status_msg)`）与 `cancel_event: Optional[threading.Event]`。
   - **HTTP Range 断点续传**（纯标准库 `urllib.request`）：
     - 下载至 `<base_dir>/MAA_Core/_downloads/<asset_name>.part`；
     - 若 `.part` 已存在且服务器支持 `Accept-Ranges: bytes`（`206 Partial Content`），携带 `Range: bytes=<existing_size>-` 续传；若服务器返回 `200 OK` 则重头覆盖写入；
     - 单个镜像超时/报错自动切换下一镜像，保留已下载的 `.part` 供后续镜像断点续传。
   - **安全解压**：
     - 解压到 `<base_dir>/MAA_Core/`（防范 Zip Slip 路径穿越，校验目标路径在 `MAA_Core` 内；若压缩包顶层包裹了单一 `MAA-vX.Y.Z-win-x64/` 根目录，自动剥离顶层目录使 `MaaCore.dll` 直接位于 `MAA_Core/` 下）。
     - 校验 `check_maa_integrity(target_dir)` 通过后删除 `.part`/`.zip`，将相对或绝对 `maa_path` 写入 `config/launcher_config.json`。

---

### 5.5 模块三：`ai_strategist.py`

**职责**：独立的 OpenAI 兼容 HTTP 推理客户端 + 双模 Box 作业适配 & 日志驱动修轴引擎。

**核心机制**：
1. **`OpenAICompatClient`（纯标准库 `urllib.request`）**：
   - 初始化参数：`base_url: str`、`api_key: str`、`model_name: str`、`timeout_sec: int = 30`。
   - `is_configured() -> bool`：判断 `api_key.strip()`（若本地 Ollama 等 `base_url` 明确为 `localhost`/`127.0.0.1` 且用户传入 `"ollama"` 等占位 Key 也视为启用；空 `api_key` 一律视为未配置并触发无感降级）。
   - `chat_json(system_prompt: str, user_prompt: str, temperature: float = 0.2) -> Optional[Dict[str, Any]]`：自动拼接 `/chat/completions`（兼容带或不带 `/v1` 尾缀的 `base_url`），解析返回文本并提取 JSON（兼容 Markdown ```json 代码块），解析或请求失败时捕获异常返回 `None` 并记录原因，**绝不抛出未处理异常阻断执行**。
2. **双模作业适配 `adapt_copilot(guide: Dict[str, Any], my_box: Dict[str, Any], stage_id: str, client: Optional[OpenAICompatClient] = None) -> Dict[str, Any]`**：
   - **纯 MAA 规则基线（零 Key 模式 / 兜底）**：完整继承并通用化原 [autonomous_agent.py](../../autonomous_agent.py) 的 `adapt_guide_to_box()` 逻辑：
     - 从 `<maa_path>/resource/recruitment.json` 动态补全干员职业映射；
     - 按技能所需最低精英化等级（1技能 E0、2技能 E1、3技能 E2）优先保留原作业精二/可用干员并钳制 `requirements`；
     - 缺失/未练干员按同职业定位在用户 `my_box` 中优先挑选高练度精二替补；
     - 同步改写 `actions` 中干员名，并清理被替换后残留的无效召唤物撤退动作；计算 `compat_score`。
   - **AI 军师增强模式（已填 Key）**：
     - 仅当作业存在缺失干员（`missing_ops` 非空）或发生高风险替补（`substitutions` 非空）时，将原始作业摘要 + 用户拥有干员精二清单 + 规则基线初稿发给 `OpenAICompatClient`；
     - 要求 LLM 返回标准化替补方案（`{"substitutions": {...}, "skill_overrides": {...}, "notes": "..."}`），由本地代码应用并校验所有干员确实存在于 `my_box`、技能序号合法且 `actions` 结构完整；若校验失败则自动回退到规则基线产物。
3. **双模日志修轴 `repair_copilot(candidates, cand_idx, cur_copilot, report, my_box, round_idx, client=None) -> Tuple[int, Dict[str, Any], Dict[str, Any]]`**：
   - **纯 MAA 规则基线**：完整继承原 `diagnose_and_repair_copilot()`（编队/练度校验失败下调需求+开 `ignore_requirements`；未进战斗换候选作业；`BulletTime` 等费卡顿下调 `costs`；第 1 轮战斗失败启用暂停部署 `deployment_with_pause="1"` + 剔除开局二倍速 + 核心输出改自动开技能；第 2 轮起自动切换下一高赞候选）。
   - **AI 军师增强模式**：将 `metrics`（`kills`、`costs`、`last_3_actions`、`errors`）、`log_tail` 最近 40 行与当前 `copilot` 的 `opers`/`actions` 发给 LLM，请其输出局部动作补丁（调整特定 `BulletTime`/`Deploy` 延迟或费用阈值、`skill_usage`、切换候选、或建议启用 `deployment_with_pause`）；经结构校验安全合入后执行，异常则无感回退规则修轴。

---

### 5.6 模块四：`auto_growth.py`

**职责**：读取仓库库存（`DepotData.json`）与干员库（`my_box.json`），在 `AUTO` 模式下计算缺口并动态规划今日最优刷取关卡（纯离线确定性计算，不引入额外效率站点网络依赖）。

**核心数据来源与隔离回退读取**：
- **用户数据读取（优先 `<base_dir>/data/`，冷启动/较新时回退 `<maa_path>/data/`）**：
  - `DepotData.json`（由 `Depot` 任务采集的 `{itemId: count}` 与 `syncTime`）
  - `my_box.json`（由 `OperBoxData.json` 标准化同步，含所有拥有干员 `elite`/`level`/`rarity`）
- **静态资源与热更缓存（读取自 `<maa_path>/resource/`，缓存优先读 `<base_dir>/cache/` 并支持回退 `<maa_path>/cache/`）**：
  - `<maa_path>/resource/item_index.json`（物品名称、稀有度、合成配方 `formula`）
  - `<maa_path>/resource/stages.json`（用于校验关卡合法性与 `apCost`，排除已关闭的往期活动关）
  - `cache/gui/StageActivityV2.json`（当期 SideStory 活动关及是否开启 `resourceCollection` 全天开放）

**决策优先级与算法（`decide_optimal_stages(...) -> GrowthDecision`）**：
1. **输入参数**：`maa_dir`、`my_box`、`depot_data`、`config`、`last_battle_is_annihilation: bool`、`now: Optional[datetime] = None`（支持测试注入时间）。
2. **服务器日历计算**（UTC+8，凌晨 `04:00` 换日）：
   - 先检查 `StageActivityV2.json` 的 `resourceCollection` 是否在 `UtcStartTime`~`UtcExpireTime` 内，若是则当日全资源/芯片本开放。
   - 否则按游戏内常规轮换开放表判断今日开放关卡：
     - 芯片本（映射与 `stages.json` 实测一致）：
       - `PR-A-1/2`（重装 `3231/3232`、医疗 `3261/3262`）：周一、周四、周五、周日
       - `PR-B-1/2`（狙击 `3241/3242`、术师 `3251/3252`）：周一、周二、周五、周六
       - `PR-C-1/2`（先锋 `3211/3212`、辅助 `3271/3272`）：周三、周四、周六、周日
       - `PR-D-1/2`（近卫 `3221/3222`、特种 `3281/3282`）：周二、周三、周六、周日
     - 物资筹备：`LS-6`（每天）、`CE-6`（二四六日）、`CA-5`（二三五日）、`AP-5`（一四六日）、`SK-5`（一三五六）。
3. **可合成材料折算（`fold_craftable_materials=True`）**：
   - 利用 `item_index.json` 的 `formula`（如 5 个固源岩 `30012` 合成 1 个固源岩组 `30013`），将 T2 低级材料折算进对应 T3 材料的等效库存（`effective_qty = direct_qty + sum(craftable_from_formula)`），避免仓库囤积大量 T2 材料时仍误判 T3 紧缺。
4. **分类缺口评估与严格跨类排序**：
   - **优先级 1 — 当期活动推荐材料关**（当 `prefer_event=True` 且 `StageActivityV2.json` 存在进行中的 SideStory）：提取当期推荐材料关（排除 `"代理"`、`"搓玉"` 条目），按 `Stages[].Drop` 对应材料 ID 在仓库中的等效库存缺口从大到小排序（缺口越大越靠前）。
   - **优先级 2 — 芯片缺口本**（当 `allow_chip_stages=True`）：
     - 若配置了非空 `target_operators`：按目标列表干员的职业计算精二芯片缺口（双芯片需 2 个对应芯片组）。
     - 若 `target_operators` 为空：仅检查 `CHIP_T1`/`CHIP_T2` 安全水位，外加最多前 `chip_untargeted_cap`（默认 `2`）名高星未精二干员（优先 `rarity == 6` 且 `elite < 2`，再看 5★）的芯片需求，**绝不**汇总全部冷门未精二干员。
     - 仅推荐**今日实际开放**的 `PR-A/B/C/D-2`（或 `CHIP_T1` 缺口更大时用 `-1`）。
   - **优先级 3 — 常驻主线 T3 材料关（内置离线精选效率表）**：
     - 因 MAA 自带 `stages.json` 不含掉率数值且混有往期关闭活动关，引擎内置精选常驻主线最优掉率映射表（如 `30013` 固源岩组 -> `1-7`/`10-11`、`30023` 糖组 -> `4-2`/`S3-1`、`30033` 聚酸酯组 -> `7-4`/`2-6`、`30043` 异铁组 -> `S4-1`/`9-10`、`30053` 酮凝集组 -> `3-1`/`JT8-3`、`30063` 全新装置 -> `7-15`/`9-10`、`30073` 扭转醇 -> `6-11`/`4-4`、`30083` 轻锰矿 -> `4-7`/`6-2`、`30093` 研磨石 -> `7-17`/`4-8`、`30103` RMA70-12 -> `7-10`/`4-9` 等），结合 `stages.json` 校验证实存在，按等效库存缺口排序。
   - **优先级 4 — 基础资源本**（当 `allow_resource_stages=True`）：若龙门币 `4001 < 500,000` 且今日开放 `CE-6`，或高阶作战记录不足且 `LS-6` 开放，加入尾部候选。
   - **优先级 5 — 保底防卡死队列**：若 `last_battle_is_annihilation=False` 且尚未命中更高优推荐时可保留 `""`；无论前置推荐为何，**候选队列末尾永远追加 `"1-7"`**（若已有 `"1-7"` 则去重保留）。
5. **候选队列与防卡死保障**：
   - 返回结构化 `GrowthDecision`：`recommended_stages: List[str]`、`reason: str`、`shortages: List[Dict]`、`open_today: Dict[str, bool]`、`active_event: Optional[Dict]`、`data_staleness: Dict[str, Any]`。
   - **与调度器联动防卡死**：无论 `auto_growth` 给出何种最优候选关，一旦在 `Fight` 执行中出现未解锁/`UnableToAgent`/`FightBegin` 失败：
     - 若开启 `auto_push`：尝试调用 1 次自主推图解锁后再重试；
     - 否则：最多仅允许该非 `"1-7"` 关卡重试 **1 次**，仍失败则立即跳过并回退到队列下一个候选直至 `"1-7"` 清空剩余理智。
   - 若用户上一次战斗记录为已满剿灭（`last_battle_is_annihilation=True`），自动过滤掉 `""`（`<LastBattle>`）避免白打 0 合成玉的剿灭。

---

### 5.7 模块五与总控：`maa_runner.py` & `master_scheduler.py`

1. **`maa_runner.py`**（解耦自原 `autonomous_agent.py` 与 `MaaSession`）：
   - **动态 SDK 加载 `init_maa_sdk(maa_dir: Path, user_dir: Optional[Path] = None, incremental_path: Optional[Path] = None, use_cache_incremental: bool = True)`**：
     - 将 `<maa_dir>/Python` 注入 `sys.path`，延迟导入 `asst.asst.Asst`。
     - `user_dir` 默认强制设为 `get_base_dir()`，保证 `debug/asst.log` 与 `Depot`/`OperBox` 输出的 `data/` 文件始终落在可写的引擎目录，**绝不向外部只读 `maa_dir`（如 `C:\Program Files\MAA`）写入**。
     - `incremental_path` 默认指向 `<base_dir>/cache`（当 `refresh_cache()` 写入 `<base_dir>/cache/` 后可直接加载；若 `<base_dir>/cache/` 尚无资源而 `<maa_dir>/cache/resource` 存在，冷启动时回退使用 `<maa_dir>/cache`）。
   - **通用模拟器与 ADB 管理**：
     - `ensure_emulator_ready(config)`：根据 `emulator.type`（`auto`/`mumu12`/`ldplayer`/`bluestacks`/`adb_only`）检查目标 ADB 地址是否在线；未在线且配置了模拟器路径或自动检测到常见安装路径时，按对应模拟器方式拉起（MuMu12 用 `MuMuManager.exe`、雷电用 `dnconsole.exe`、蓝叠复用 `Bluestacks`、通用执行可执行文件）并轮询等待 `sys.boot_completed == 1`。
     - `ensure_game_running(config)`：根据 `server`（`Official` -> `com.hypergryph.arknights` / `Bilibili` -> `com.hypergryph.arknights.bilibili`）检查前台包名并启动 `com.u8.sdk.U8UnityContext`。
     - `execute_post_action(action: str, config: Dict[str, Any]) -> Dict[str, Any]`：
       - `"none"`：不执行操作；
       - `"close_emulator"`：优先通过模拟器命令行优雅退出（如 `MuMuManager control -v <idx> shutdown`、`dnconsole quit`），否则通过 ADB `shell reboot -p` 或匹配进程退出；
       - `"shutdown"`：在 Windows 上调用 `shutdown /s /t 30 /c "MAA 任务已完成，将在30秒后关机"`（提供 `dry_run` 参数供测试验证而不真关机）。
   - **`MaaSession`**：支持 `event_callback`、`log_callback`、`cancel_event`，完整保留公招 5★/6★ 词条告警与断线自愈重连（`_has_real_disconnect_error`）。

2. **`master_scheduler.py`**（总控引擎与 CLI）：
   - **Engine API（供未来 GUI 调用）**：提供 `MaaEngine` 类（或纯函数入口），封装：
     - `check_environment(check_adb=True)`
     - `ensure_maa_ready(auto_download=False, progress_callback=None, cancel_event=None)`
     - `refresh_cache()`（自动刷新 `StageActivityV2.json` 和热更 `tasks.json`，支持 `urllib` + Windows `curl.exe -4` 回退）
     - `sync_box_and_depot(run_maa_tasks=False)`（运行 `OperBox` + `Depot` 或直接同步本地最新数据）
     - `plan_auto_growth()`（返回 `GrowthDecision` 供 GUI 预览今日推荐刷取关）
     - `run_daily(options, callbacks)` / `run_annihilation(...)` / `run_fight(...)` / `run_push(...)`
   - **日常 `Fight` 两种策略**：
     - `fight_mode == "AUTO"`：若 `auto_sync_before_fight=True` 且本地 `DepotData.json`（参考 `syncTime`/mtime）或 `OperBoxData.json` 缺失或过期超过 `depot_sync_ttl_hours`（默认 24 小时），先自动执行一次 `Depot`（及缺失/过期时的 `OperBox`）同步并更新 `my_box.json`；同步异常时静默沿用可用缓存 -> 调用 `auto_growth.decide_optimal_stages()` 生成候选关列表（末尾兜底 `"1-7"`）-> 走统一防卡死执行器；
     - `fight_mode == "MANUAL"` 或指定 `--stage`：直接使用 `[stage, "1-7"]`（若未指定则 `["", "1-7"]` 或跳过已满剿灭后的 `["1-7"]`）。
   - **任务收尾**：保存战报到 `reports/daily_<ts>.json` 与 `data/latest_daily_report.json`（均记录相对/当前路径），更新 `data/scheduler_state.json`，最后按配置执行 `post_action`（`none` / `close_emulator` / `shutdown`）。
   - **CLI 入口**：支持 `--mode {check,install,daily,annihilation,fight,push}`、`--server {Official,Bilibili}`、`--fight-mode {AUTO,MANUAL}`、`--post-action {none,close_emulator,shutdown}` 等参数，脱离 Antigravity 可直接通过命令行或双击脚本运行。

---

## 6. 留给实施阶段决定的细节（Left to Builder）

1. 各模块具体的内部辅助函数拆分、`dataclass` / `TypedDict` 字段补充与日志格式模板。
2. 单元测试中对网络请求（GitHub API / `prts.maa.plus` / OpenAI `/chat/completions`）、ADB 子进程、`Asst` C-API 与 `shutdown` 命令的 Mock 夹具实现。
3. 除本设计第 5 节已固定的核心公共签名外，辅助参数默认值与边界异常提示文案。

---

## 7. 设计师拍板记录与已确认口径（Designer Calls & Confirmed Policies）

1. **完全隔离与冷启动回退（Designer Call）**：`AsstSetUserDir` 与 `refresh_cache()` 始终写入 `get_base_dir()` 下的 `debug/`、`data/`、`cache/`，避免向外部 `maa_path`（含只读 `Program Files`）写入；读取 `DepotData.json`、`OperBoxData.json` 与缓存时支持在冷启动或外部数据更新时回退读取 `<maa_path>`。
2. **手动绑定优先级控制（Designer Call）**：未手动绑定时严格遵循“同级 → 父级 → 常见路径 → `launcher_config.json`”顺序；调用 `bind_manual_maa_path()` 时写入 `maa_path_pinned: true`，确保用户手动指定的路径不会被父目录或常见目录已有安装覆盖。
3. **Release 下载源兜底（Designer Call）**：`maa_installer` 优先请求 `MaaAssistantArknights/releases/latest`，若未匹配到 `win-x64.zip` 资产则自动降级到 `MaaRelease/releases/latest` 与 `api.maa.plus`，并派生国内镜像加速节点。
4. **`AUTO` 同步 TTL、离线效率表与跨类优先级（User Confirmed）**：
   - `Fight` 前按 `depot_sync_ttl_hours = 24` 按需自动跑 `Depot`/`OperBox`；
   - 常驻关卡采用内置离线精选效率表 + `item_index.json` 的 `formula` 低级材料折算；
   - `target_operators` 为空时芯片需求受 `CHIP_T1`/`CHIP_T2` 安全库存与 `chip_untargeted_cap = 2` 上限约束；跨类优先级为：当期活动材料关 > 今日开放芯片本 > 常驻主线材料关 > 资源本(`CE-6`/`LS-6`) > 保底 `1-7`。
5. **关机安全策略（Designer Call）**：`post_action == "shutdown"` 使用 30 秒倒计时（`shutdown /s /t 30`）并提示 `shutdown /a` 取消。
