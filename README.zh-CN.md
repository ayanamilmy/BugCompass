# BugCompass 0.1

BugCompass 是一个中文优先的本地工具，用来把 Blender Bug 描述整理成可持续调查的案件，并让 Codex 基于本地源码与 Git 证据给出可验证的调查路径。推荐使用图形界面；CLI 保留给高级使用和排错。

## 推荐：图形界面

安装后启动：

```bash
python -m bugcompass gui
# 或
bugcompass gui
```

完整操作不超过 5 步：

1. 点击“选择文件夹”，选择本地 Blender 源码根目录。
2. 粘贴 Bug 描述，或从 `.md` / `.txt` 文件导入。
3. 点击“创建并开始调查”。工作区和 Case ID 会自动处理。
4. 看右上角的 Codex 状态。运行中可点“停止 Codex”，停止、失败或完成后可点“继续调查”，它会接着更新同一个案件。
5. 调查完成后查看三条路径和实验卡片；确认命令与权限后运行实验，结果会自动记录并用于更新原假设。

想练习排查已经修好的真实历史问题，可以点击左侧“历史 PR 练习”：选择案例后，BugCompass 会在工作区建立一份隔离的修复前源码副本，自动开始调查。完成后先提交自己的根因判断，再揭晓真实修复与五项评分；原来的 Blender 工作树不会被切换。

GUI 不直接接入 OpenAI API，而是调用本机已经安装并登录的 Codex CLI。Codex 会复用本机登录状态，以非交互模式调查当前 Case；“复制调查指令（备用）”只在 Codex CLI 不可用或自动运行失败时使用。主要结果保存在 `investigation.json`，Markdown 只是便于分享的自动导出。

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
- GUI 依赖本机已安装且已登录的 Codex CLI；离线时只能创建和查看案件。当前也没有数据库、跨 Case 搜索、删除管理或协作同步。
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

## 下一阶段候选（尚未实现）

- 更多历史案例，以及更接近专家评审的语义评分。
- Case 状态流转、调查日志追加命令和更完整的案件管理界面。
- 更多开源项目 Pack，以及用户可维护的源码区域映射。
- 为黄色和红色实验增加容器或系统级隔离执行。
