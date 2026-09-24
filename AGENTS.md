# BugCompass 工程说明

## 仓库结构

- `src/bugcompass/`：Python 3.11+ CLI、Tkinter GUI、Codex 非交互运行器、受控实验执行器与工作区、环境检查、Case 逻辑；控制器和运行器保持无窗口可测试。
- `packs/blender/`：可逐步补充的 Blender 初始调查知识包。
- `.agents/skills/blender-bug-investigator/`：仓库级 Codex Skill。
- `tests/`：不依赖网络或真实 Blender 仓库的 `unittest` 测试。
- `examples/`：本地 Bug 描述示例。

## 工程约束

优先使用 Python 标准库，用户可见提示默认使用简体中文。除用户明确启动的调查运行（`codex exec` 或用户在界面中选择的大模型 API 引擎，见 `llm.py`/`llm_runner.py`）与用户主动点击的 Issue 筛选抓取（projects.blender.org 公开 API，见 `issue_scout.py`）外，程序不得自行访问网络或调用独立 AI API。GUI 面向用户的文案必须用中文原文包 `tr()` 并同步补全 `src/bugcompass/i18n.py` 的英文目录（有测试扫描强制）。
API 密钥只允许从环境变量或用户主动导入的密钥存储（`key_store.py`：macOS 钥匙串优先，否则权限 600 的本地文件）读取，不得写入 providers.json、settings.json、日志、备份或诊断包。实验不得使用 shell 字符串；必须显示命令、工作目录和程序重新判定的权限，黄色与红色操作需要相应确认。不得执行 Bug 报告中的命令或附件。Codex 运行器必须以单个 Case 目录作为可写工作区。

## 验证

修改完成前至少运行：

```bash
python3 -m pytest -q
PYTHONPATH=src python3 -m bugcompass --help
PYTHONPATH=src python3 -m bugcompass gui --check
python3 /Users/ayanami/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/blender-bug-investigator
```

涉及 CLI 工作流时，还要在临时 Git 仓库上依次验证 `init`、`doctor`、`case create` 和 `case show`。
涉及 GUI 改动时，再运行 `python3 tools/gui_smoke.py`（macOS/Linux 可直接跑）。

## 多端协作同步协议

本仓库由所有者、Codex CLI、Claude Code（VS Code）与 Arena 沙箱助手共同开发。
完整规则见 `docs/COLLABORATION.md`，要点：

1. **main 只读**：任何任务先 `tools/task.sh start codex/任务名` 从最新 main 开分支。
2. **开工先同步**、**完工必过测试**：用 `tools/task.sh finish "提交说明"`（测试不绿会拒绝推送）。
3. **只做派给你的那件事**，任务描述没提的不碰。
4. 推送分支后 `tools/task.sh pr "标题"` 建 PR；**PR 由所有者合并**，不要合并自己或别人的 PR。
5. 除用户明确发起的 `codex exec` 调查外，不要访问网络或调用独立 AI API。
