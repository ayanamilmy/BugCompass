# CLAUDE.md — Claude Code 工作规则

工程约束与仓库结构见 `AGENTS.md`（同样适用）；多端同步协议详见 `docs/COLLABORATION.md`。要点：

## 硬性规则

1. **main 只读**。任何修改先 `tools/task.sh start claude/任务名` 开分支。
2. **开工先同步**：如果分支开工时没跑 start，至少跑一次 `tools/task.sh sync`。
3. **只做派给你的那一件事**，不改无关文件。任务描述里没提的就是禁区。
4. **完工必过测试**：`tools/task.sh finish "提交说明"`（内部会跑全部测试，
   不绿不推）。禁止跳过测试直接 `git push`。
5. 优先使用 Python 标准库；用户可见文案用简体中文；除用户明确发起的
   `codex exec` 外程序不得自行访问网络或调用独立 AI API（详见 AGENTS.md）。

## 验证命令

```bash
python3 -m pytest -q                 # 固定测试集，必须全绿
python3 -m bugcompass --help         # CLI 可用
# 涉及 GUI 改动时（Mac 可直接跑）：
python3 tools/gui_smoke.py
```

## 提交后

`tools/task.sh finish` 会推送分支；然后 `tools/task.sh pr "标题"` 建 PR。
PR 由仓库所有者合并，不要自行合并或改 main。
