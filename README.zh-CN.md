# BugCompass 0.5

[English README](README.md)

BugCompass 是一个中文优先的本地工具。普通 Blender 用户可以在图形界面中整理可复现 Bug 报告；需要深入排查时，也可以把问题交给源码调查工作台。CLI 保留给高级使用和排错。

## 0.2 版本变更（稳定性与交付）

- **修复 Windows 高 DPI 下的拖影、断层与裁切**：进程在启动时声明 DPI aware（默认系统级，
  可用 `BUGCOMPASS_DPI_MODE=permonitor` 切 per-monitor v2）；滚动容器改为整像素滚动并在内容
  变化后立即同步 scrollregion；重渲染前统一销毁旧控件；所有卡片文本改为随窗口宽度自适应换行。
- **滚轮与滚动条**：滚轮事件按指针位置路由到可滚动区域（鼠标停在卡片上也能滚动）；
  Ctrl+滚轮缩放界面（设置里保存）；滚动条按需显示。
- **因果链改为思维导图交互**：拖动节点不再整画布重画（旧实现是残影的直接来源）；
  新增平移、以鼠标为中心缩放、框选多节点、就地改名、Delete 删除、Ctrl+Z/Ctrl+Y 撤销重做、
  自动分层布局、适应内容、网格吸附（按住 Alt 临时关闭）。所有编辑即时落盘。
- **发行与交付**：版本号 0.2.0；Windows 打包配置（PyInstaller + Inno Setup，见
  `packaging/README.md`）；崩溃日志与脱敏诊断包（`~/.bugcompass/logs/`，
  GUI「导出诊断包」或 `bugcompass diagnostics`，自动剔除 API key/源码/正文并通过自检）；
  工作区备份/恢复/迁移（`docs/MIGRATION.md`）。
- **运行指标**：每个 Case 显示模型、运行次数、累计耗时、对话轮次、工具调用、token 与估计成本
  （`metrics.json`，本地保存）。成本单价由你填写 `~/.bugcompass/pricing.json`（可运行
  `bugcompass metrics` 查看提示），未配置就显示“未配置单价”，不会凭空估算。
- **统计上报默认关闭**：开启后也只写本地 outbox，且不含正文；导出需手动执行。
  见 `telemetry` 命令与设置界面。
- **许可**：补齐 MIT `LICENSE` 与第三方材料审计 `THIRD_PARTY.md`（练习数据逐案核查表
  `packs/blender/practice/PROVENANCE.md`）。
- 打包版资源定位修复：安装包内 `packs/`、`.agents/` 可被正确找到（源码版的
  `parents[2]` 在 frozen 后无效）。

## 0.3 版本变更：调查引擎可接入大模型 API

除了 Codex CLI，现在可以用**任意 OpenAI 兼容端点**驱动调查：DeepSeek、通义千问、Kimi、
智谱 GLM、OpenAI、Ollama（本地，无需密钥），或自定义端点。核心设计：

- **密钥只从环境变量读取**（如 `DEEPSEEK_API_KEY`），`providers.json` 只保存端点、
  模型名和环境变量名——密钥永不写入任何文件、备份或诊断包（有测试强制这一条）。
- 模型通过 4 个**只读工具**查阅源码（列目录 / 读文件 / 正则搜索 / 读案件记录），
  全部限制在 Blender 仓库与案件目录白名单内，无 shell、无写操作；轮次与总时长有硬预算。
- 与 Codex 共用同一套结果管线：结构化结果写 `investigation.json`，运行摘要写
  `llm-last-run.json`，指标卡、统计上报、继续调查、练习模式的规则全部一致。
- **默认引擎仍是 Codex CLI**——不配置 API 时，应用行为与从前完全一致。

使用方法（图形界面，零命令行）：

⚙ 设置 → 模型服务 → 选择服务 → **导入密钥…**（存入 macOS 钥匙串，其他平台为
权限 600 的本地文件）→ **测试连接** → **设为当前引擎**。在「新建调查」页切换到
该服务时，如果没有密钥会自动弹出导入窗口。密钥永不写入 providers.json、备份、
诊断包或统计上报（有测试强制）。

首次启动会自动在 ~/.bugcompass/ 生成默认服务列表（providers.json），想换端点直接编辑该文件即可——全程不需要终端。**切换模型在 ⚙ 设置 → 模型服务里下拉选择，也可输入任意模型名，保存即生效。点「⟳ 拉取模型」可直接从服务商获取你账号当前可用的全部模型（永远最新）。**

命令行方式（可选，效果相同）：

```bash
bugcompass llm init-config   # 生成 ~/.bugcompass/providers.json 模板（含预设）
bugcompass llm list          # 查看可用服务与密钥环境变量名
export DEEPSEEK_API_KEY=你的密钥
bugcompass llm test --provider deepseek   # 测试连接（只发一次最小请求）
```

然后在 GUI「新建调查」页选择调查引擎，或设置里点「设为当前引擎」。

## 0.4 版本变更：AI 挑选 Issue（Issue Scout）

不知道从哪个 Bug 入手？点侧栏 **🔭 AI 挑选 Issue**：按模块/类型/数量筛选 Blender tracker
（projects.blender.org 公开数据，点击筛选时才联网抓取），用你配置的大模型逐个打分
（调查价值 1-10、难度、一句话理由），结果按分排序并可**一键创建调查案件**。全程图形界面。

- **筛掉已有人接手的 issue**：自动抓取指派与评论区——已指派、评论里出现 PR 链接直接判定占用，AI 再读评论识别"我来修/补丁在路上"等认领表述；默认只显示没人占用的，可切换查看全部；建案时二次确认；
- **Good First Issue 单独开关**：只看带 `Meta/Good First Issue` 标签的入门 issue（列表中带 ★）；
- 抓取与评分结果缓存在 `~/.bugcompass/scout/`，断网可查看上次筛选；
- 评分眼光可私有化：`~/.bugcompass/scout-prompt.md` 写入你的补充标准，会附加到默认评分规则之后（此文件属于你，不进仓库）；
- 评分引擎复用已导入密钥的大模型服务，纯文本打分，无需工具循环，成本极低。

## 推荐：图形界面

Windows 安装包用户可从开始菜单打开 **BugCompass**（安装时也可选择桌面图标），无需打开终端。下面的命令只供源码运行或偏好命令行的用户使用：

```bash
python -m bugcompass gui
# 或
bugcompass gui
```

### 报告 Bug：无需源码、终端或 AI

GUI 默认打开“报告 Bug”模式。点击“新建 Bug 报告”，按“描述问题 → 复现材料 → 核对与提交”填写；点击“保存草稿”或关闭编辑窗口时，草稿保存在本机用户数据目录的 `reports/` 下，稍后可在“本地报告草稿”继续编辑。这里不需要 Blender 源码仓库，也不会启动 Codex、模型服务或 Blender。

建议先在 Blender 中按“帮助 → 报告 Bug”，这会在官方表单预填部分版本和系统信息；也可以按“帮助 → 保存系统信息”取得 `system-info.txt`。BugCompass 引导填写简短标题、出错与最后正常版本、实际与预期行为、逐步复现操作，并手动选择可以公开的简化 `.blend`、截图或日志。请亲自复现、搜索已有及已关闭报告，并检查文件内的私人或项目资料。

“核对与提交”会显示所有缺项和建议，预览按 Blender 报告表单栏目整理的正文。标题与正文可以分别复制到官方表单；附件需在官方页面单独上传。可选导出 ZIP 留档，内有正文、发布前清单、清单元数据和所选附件。未补齐材料时可以保存草稿，界面不会将它标为可提交；导出未完成草稿会再次提示。BugCompass 不会自动提交、运行附件或判断 Bug 已由第三方确认。适用于 Blender 程序本身；扩展或文档问题请先查看[官方报告指南](https://developer.blender.org/docs/handbook/bug_reports/making_good_bug_reports/)选择对应项目。

已有源码调查 Case 可以在“报告 Bug”页点击“从当前调查导入”，建立一份独立草稿。调查中的推断和附件路径仍需逐项人工核对。

### 调查 Bug：需要本地 Blender 源码

源码调查操作不超过 5 步：

1. 点击“选择文件夹”，选择本地 Blender 源码根目录。
2. 粘贴 Bug 描述，或从 `.md` / `.txt` 文件导入。
3. 点击“创建并开始调查”。工作区和 Case ID 会自动处理。
4. 看右上角的 Codex 状态。运行中可点“停止 Codex”，停止、失败或完成后可点“继续调查”，它会接着更新同一个案件。
5. 调查完成后查看三条路径和实验卡片；确认命令与权限后运行实验，结果会自动记录并用于更新原假设。

想练习排查已经修好的真实历史问题，可以点击左侧“历史 PR 练习”：选择案例后，BugCompass 会在工作区建立一份隔离的修复前源码副本，自动开始调查。完成后先提交自己的根因判断，再揭晓真实修复与五项评分；原来的 Blender 工作树不会被切换。

调查模式默认调用本机已安装并登录的 Codex CLI，也可以选择已配置的大模型 API 引擎；“复制调查指令（备用）”用于自动运行不可用的情况。调查结果保存在 `investigation.json`。独立报告 Bug 模式不会调用任何 AI。

### 调查 Case 的可复现报告包

打开一个 Case 后，点击案件概览中的“整理可复现报告包…”。核对标题、出错版本、复现步骤、预期与实际行为，再填写系统信息或选择 `system-info.txt`。可以手动选择简化的 `.blend`、截图与崩溃日志；只有所选附件会进入导出的 ZIP。草稿保存在 Case 的 `repro-report.json` 中，断网时也能编辑和导出。草稿只记录附件路径；迁移电脑或移动附件后需要重新选择。若要使用面向普通用户的核对与提交界面，可将当前 Case 导入独立的“报告 Bug”模式。

“附件与检查”会提示尚缺的材料，以及最近版本复测、已关闭报告查重、简化示例文件等建议。导出的 ZIP 包含 `报告正文.md`、`发布前检查清单.md`、`manifest.json` 和所选附件。提交前请阅读清单、检查附件是否含敏感内容，将标题填入官方标题栏、报告正文复制到描述栏，并分别上传所需附件。BugCompass 不会自动上传、运行附件、判断问题已经被第三方复现，或替代 Blender 分诊团队确认 Bug。参照 [Blender 报告手册](https://developer.blender.org/docs/handbook/bug_reports/making_good_bug_reports/)、[分诊说明](https://developer.blender.org/docs/handbook/bug_reports/help_triaging_bugs/)与[分诊手册](https://developer.blender.org/docs/handbook/bug_reports/triaging_playbook/)。

为避免一次调查无限消耗额度，GUI 调查固定使用 `gpt-5.6-terra` 和 `low` 推理强度，不继承用户配置中的高强度模式；单次最多运行 180 秒，并要求最多执行 8 组只读搜索。超时后会自动停止，Case 保持可继续。每次运行的原始 JSONL 和最近一次摘要分别保存在 Case 下的 `codex-runs/` 与 `codex-last-run.json`，用于排错。

界面使用纯 Tkinter/ttk 自绘的深色调查工作台风格：统一的侧栏、阶段导航、卡片、状态色和 Blender 橙色强调。没有引入主题包或其他运行时依赖。

启动前可确认 Codex CLI 可用：

```bash
codex --version
```

若尚未登录，请先在终端启动一次 `codex` 并完成登录。BugCompass 使用 `codex exec --sandbox workspace-write`，且把可写工作目录限制在单个 Case 文件夹；Blender 源码仍保持只读。相关机制见 [Codex 非交互模式官方说明](https://learn.chatgpt.com/docs/non-interactive-mode)。

如果窗口无法启动，先运行：

```bash
python -m bugcompass gui --check
```

该命令不会打开窗口，只检查 Tkinter、GUI 模块和 Codex CLI。若提示 Tkinter 不可用，请安装带 Tk 支持的 Python 3.11+，或改用下方 CLI 流程。若提示没有显示环境，请在本地图形桌面终端中启动。

## 这个版本能做什么

- 将本地 Blender Git 工作树登记为只读调查目标。
- 记录分支、commit、工作树状态、常见源码目录、CMake、Git 和 Python 检查结果。
- 从本地 Markdown/TXT 创建结构化 Case，并原样保留 Bug 描述。
- 通过仓库级 `$blender-bug-investigator` Skill 生成中文问题整理、恰好 3 条优先调查路径与证据记录。
- 将所有配置、输入和调查产物保存在本地 JSON/Markdown 文件中。
- 通过 Tkinter GUI 完成选仓库、输入报告、一键启动 Codex 调查、停止、继续、自动刷新结果和打开最近案件；右上角会明确显示 Codex 是空闲、工作中、正在停止、已完成、失败还是已停止。运行状态绑定到具体 Case：切换到其他案件时会显示“其他案件调查中”，不会把所有案件都标成运行中，也不会误停其他案件。
- 以卡片展示三条路径，明确分开事实、推测和未知；路径可否定，也可继续深入。
- 点击源码引用可打开本地文件；若安装 VS Code 的 `code` 命令会定位到行号，否则使用系统默认程序打开文件。
- “运行实验”会先展示目的、命令、工作目录、权限和预计耗时；执行记录落盘后，Codex 才会读取结果并更新同一条假设。
- 实验使用参数数组直接执行，不经过 shell。绿色是读取源码和 Git 历史；黄色是构建、测试或启动 Blender，执行前再次确认；红色可能修改或删除数据，必须输入“明确授权”。
- 环境检查会寻找已有构建目录和 Blender 可执行文件，提示二进制是否可能落后或来自其他源码目录，并分别显示“可调查 / 可构建 / 可运行测试”。它不会自动开始完整构建。
- 提供 10 个 Blender 历史 PR 练习：图形界面内选题、隔离切换到修复前 commit、隐藏答案、提交判断、揭晓真实修复，并按子系统、相关文件、证据、实验和是否过早锁定五项自动评分；同时记录用时与 AI 调查次数。
- Codex 会生成一份可编辑的因果链初稿。在 GUI 中可以拖动节点整理逻辑，从节点右侧橙色圆点拖到另一节点建立连线，双击修改文字，或添加和删除连线；用户布局与编辑会写回原 Case，并在后续调查中保留。
- 每次实验第一次运行前必须先预测结果并写一句理由。预测会在执行前锁定，Codex 读取真实输出后再标记“吻合、部分吻合、未命中或无法判断”，防止事后改写解释。
- “语义 Diff”把代码变化解释成旧规则、新规则、改变的不变量、受影响路径和剩余风险，而不是重复文本 diff。点击“分析当前代码改动”会让 Codex 只读检查当前 `git diff`；没有实际改动时会明确显示为预期变化或尚不可用。

## 不能做什么

0.1 不会自动修复 Bug，也不会一打开就运行 Blender 或完整构建。只有用户在实验卡片上确认后，才会执行显示出的命令；Bug 报告中的命令和附件永远不会被直接执行。程序不接入独立 OpenAI API；只有用户发起调查或实验评估后，本机 Codex CLI 才会连接 Codex 服务。

## 安装

需要 Python 3.11+ 和 Git。建议在仓库根目录使用虚拟环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m bugcompass --help
```

如果只想从源码试用而不安装，可在仓库根目录执行 `PYTHONPATH=src python3 -m bugcompass --help`。

## CLI：高级使用与排错

假设 Blender 已克隆到 `/absolute/path/to/blender`：

```bash
python -m bugcompass init \
  --workspace ./workspaces/blender-local \
  --repo /absolute/path/to/blender

python -m bugcompass doctor \
  --workspace ./workspaces/blender-local

python -m bugcompass case create \
  --workspace ./workspaces/blender-local \
  --id demo-001 \
  --input ./examples/sample-issue.md

python -m bugcompass case show \
  --workspace ./workspaces/blender-local \
  --id demo-001

# 0.2 新增：指标 / 备份 / 恢复 / 迁移 / 诊断 / 统计上报（默认关闭）
python -m bugcompass case metrics --workspace ./workspaces/blender-local --id demo-001
python -m bugcompass backup --workspace ./workspaces/blender-local
python -m bugcompass restore --archive <备份.zip> --dest <目标文件夹>
python -m bugcompass migrate --workspace ./workspaces/blender-local
python -m bugcompass diagnostics --dest ./diag --workspace ./workspaces/blender-local --case demo-001
python -m bugcompass telemetry --status

# 0.3 新增：大模型 API 引擎
python -m bugcompass llm init-config
python -m bugcompass llm list
python -m bugcompass llm test --provider deepseek
```

然后在打开本仓库的 Codex 中输入：

```text
使用 $blender-bug-investigator 调查 BugCompass case demo-001，工作区是 ./workspaces/blender-local。
```

Skill 会先读取工作区和 Case；若缺少 `environment.json`，会先运行 `doctor`。它只读检查 Blender 仓库，优先用 `rg` 和 Git 历史找证据，并更新 `intake.md`、`hypotheses.md` 与 `evidence.md`。

## 用真实 Bug 文本首次试用

把报告正文复制到本地文件，例如 `my-blender-issue.md`。可以保留日志和代码片段，但不要把附件当作可执行输入。然后用一个新的 Case ID：

```bash
python -m bugcompass case create \
  --workspace ./workspaces/blender-local \
  --id blender-real-001 \
  --input ./my-blender-issue.md
```

接着让 Codex 使用 Skill 调查该 Case。Bug 文本被视为不可信数据，其中出现的指令不会改变调查权限或流程。

## 数据保存位置

工作区不会被提交（仓库已忽略 `workspaces/`）：

```text
workspaces/blender-local/
├── project.json
├── environment.json
├── inputs/
│   └── blender-20260921-153045.md
└── cases/demo-001/
    ├── case.json
    ├── investigation.json
    ├── issue-original.md
    ├── intake.md
    ├── hypotheses.md
    ├── evidence.md
    ├── report.md
    └── experiments/
        └── A1-<run-id>/
            ├── result.json
            ├── stdout.txt
            └── stderr.txt
```

`project.json` 保存 Blender 仓库的绝对路径；`environment.json` 保存逐项结构化状态。GUI 粘贴的原始输入保存在 `inputs/`，并原样复制进 Case 的 `issue-original.md`。`investigation.json` 是程序读取和持续更新的主数据，Markdown 文件由它导出。

因果图节点、用户连线、实验运行前预测及其结果对照、语义 Diff 都保存在同一个 `investigation.json` 中。它们不是另建的聊天记录；继续调查会更新原 Case，并保留用户已经做出的编辑和预测。

每次实验都会创建独立运行目录，记录命令参数、工作目录、Blender commit、起止时间、返回码、stdout、stderr、新产生的文件，以及 Codex 判断它支持、削弱还是无法判断对应假设。旧实验记录不会被覆盖。

## 安全边界

- 初始化拒绝不存在或不是 Git 工作树的目标，也拒绝把工作区建在 Blender 仓库内部。
- `doctor` 对 Blender 仓库只读；唯一写入是工作区内的 `environment.json`。
- Case ID 使用白名单校验，拒绝路径穿越；已有工作区和 Case 均不会被静默覆盖。
- 原始报告只作为数据读取，报告内命令和附件不会执行。
- 工具不包含外部 AI SDK 或独立 API 调用；自动调查只通过本机 Codex CLI 发起。

## 当前局限

- 只支持 Blender，且知识包只是初始源码导航，并非完整 Blender 知识库。
- 不抓取在线 issue，不解析附件，不自动复现，也不验证修复。
- 自动调查需要可用的 Codex CLI 或已配置的模型服务；独立报告 Bug 模式可完全离线使用。当前也没有数据库、跨 Case 搜索界面、删除管理或协作同步。
- 黄色和红色实验仍依赖本机已有工具、构建目录和用户逐次授权。当前没有容器隔离；执行前必须认真核对命令、目录和权限提示。
- 历史练习的自动评分是结构化启发式评分，适合复盘路径质量，但不等于人工专家评审。答案在提交前不会显示在 GUI 或练习副本中；它仍保存在本项目的本地 Pack，属于产品层面的隐藏，不是对能主动绕过规则的用户提供密码学隔离。

## 历史 PR 练习数据

`packs/blender/practice/catalog.json` 保存练习时可见的问题与修复前 commit，`answers.json` 单独保存真实修复和根因，`rubric.json` 保存评分规则。首批案例均由本地 Blender Git 历史手工筛选并核对 diff，没有进行全网批量抓取。

练习流程：

1. 在 GUI 左侧进入“历史 PR 练习”，选择一个案例并点击开始；第一次使用时直接选择本地 Blender 源码目录即可。
2. 工具在 `workspaces/blender-local/practice/sessions/` 建立隔离的本地 Git 副本，并以 detached HEAD 打开修复前 commit；不会切换或修改原始 Blender 仓库。
3. Codex 只根据问题描述和修复前源码生成三条路径。你可以继续深入、运行获准实验，并查看调查过程。
4. 点击“提交根因判断”，写下自己的结论。提交之前不能揭晓。
5. 点击“揭晓真实修复”，查看真实根因、fix commit、PR、相关文件、总分、分项得分、用时和 AI 运行次数。

## 发布与验证

- 发布前人工检查单（Windows 100%/150%/200% DPI、三台机器、10 案例全链路、断网浏览）：
  `docs/RELEASE-CHECKLIST.md`；配套检查单生成器 `tools/visual_checklist.py`。
- 无头 GUI 冒烟测试（Linux/CI 上回归拖影相关代码路径）：`xvfb-run -a python tools/gui_smoke.py`。
- 打包：`packaging/README.md`；备份/迁移：`docs/MIGRATION.md`。

## 下一阶段候选（尚未实现）

- 更多历史案例，以及更接近专家评审的语义评分。
- Case 状态流转、调查日志追加命令和更完整的案件管理界面。
- 更多开源项目 Pack，以及用户可维护的源码区域映射。
- 为黄色和红色实验增加容器或系统级隔离执行。
- per-monitor DPI 的实时重排（受 Tk 8.6 限制，需评估升级 Tk 或迁移 UI 框架）。
