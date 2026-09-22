# BugCompass 工程说明

## 仓库结构

- `src/bugcompass/`：Python 3.11+ CLI、Tkinter GUI、Codex 非交互运行器、受控实验执行器与工作区、环境检查、Case 逻辑；控制器和运行器保持无窗口可测试。
- `packs/blender/`：可逐步补充的 Blender 初始调查知识包。
- `.agents/skills/blender-bug-investigator/`：仓库级 Codex Skill。
- `tests/`：不依赖网络或真实 Blender 仓库的 `unittest` 测试。
- `examples/`：本地 Bug 描述示例。

## 工程约束

优先使用 Python 标准库，用户可见提示默认使用简体中文。除用户明确启动的 `codex exec` 调查外，程序不得自行访问网络或调用独立 AI API。实验不得使用 shell 字符串；必须显示命令、工作目录和程序重新判定的权限，黄色与红色操作需要相应确认。不得执行 Bug 报告中的命令或附件。Codex 运行器必须以单个 Case 目录作为可写工作区。

## 验证

修改完成前至少运行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m bugcompass --help
PYTHONPATH=src python3 -m bugcompass gui --check
python3 /Users/ayanami/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/blender-bug-investigator
```

涉及 CLI 工作流时，还要在临时 Git 仓库上依次验证 `init`、`doctor`、`case create` 和 `case show`。
