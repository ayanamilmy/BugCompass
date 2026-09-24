# 多端协作同步协议（BugCompass）

> 适用对象：仓库所有者本人、Codex CLI（本地）、Claude Code（VS Code）、Arena 沙箱助手。
> 目标：四边改同一份代码，永远不冲突、不丢工作、main 永远是可发布状态。

## 一、核心原则：GitHub 是唯一的"真本"

```
                 ┌──────────────────┐
                 │  GitHub  main    │   ← 唯一权威版本，永远全绿可发布
                 └────┬────┬────┬───┘
          push/PR ↑   │    │    │  ↑ push/PR
     ┌───────────────┘    │    └────────────────┐
     │                    │                     │
 你本地 ~/BugCompass   你本地 ~/BugCompass    Arena 沙箱
 （Codex CLI 在这干活）（Claude Code 在这）   （新对话自动从 GitHub
     同一目录，两个工具      重新克隆 = 天然最新）
```

三条铁律：

1. **任何改动只发生在分支上，main 永不直接提交**。每个任务一个分支：
   `codex/任务名`、`claude/任务名`、`arena/任务名`。
2. **开工先同步，完工立刻推**。`tools/task.sh` 把这两步做成了一条命令。
   推上 GitHub 的工作才算"存在"——没推的本地改动对其他三边都是不可见的。
3. **main 只通过 PR 合并，合并权只在仓库所有者手里**。Agent 可以建 PR，
   不能自己合并。合并前 CI 自动跑全部测试。

## 二、每边的日常操作

### 你本地（Mac，一次性的准备）

```bash
cd ~/BugCompass
gh auth login                # 按提示浏览器授权，之后 push 不再要密码
git config pull.rebase true  # pull 时自动变基，历史保持一条线
```

（没装 gh 也没关系：第一次 `git push` 时会弹浏览器授权，效果相同。）

### 给 Codex / Claude Code 派活的固定话术

开工时对它说（以 Codex 为例，Claude Code 同理）：

```text
用 tools/task.sh start codex/任务名 开一个分支，只做下面这一件事：
〈任务描述〉。
做完用 tools/task.sh finish 提交并推送，然后把 PR 链接给我。
不要碰 main，不要改无关文件。
```

Codex 会自动读 `AGENTS.md`、Claude Code 会自动读 `CLAUDE.md`，
里面已写明这套协议，所以它知道该怎么做。

### Arena 沙箱这边（你在这里找我干活）

- **每次新开对话，我会自动从 GitHub 重新克隆**——所以只要你本地推了，
  我这边天然就是最新的，不需要任何操作。
- 同一次对话里连续做多个任务时，我每个任务开始前 `git fetch` 一次。
- **我推送需要你提供令牌**（GitHub 的规则，匿名推不了）。推荐做法见下。

### 令牌策略（给 Arena 推送用）

在 GitHub → Settings → Developer settings → Fine-grained tokens 创建：

| 项 | 建议值 |
| --- | --- |
| Repository access | Only select repositories → BugCompass |
| Contents | Read and write |
| Workflows | Read and write（推 CI 文件必需） |
| 有效期 | 30 天（到期换新，随时可吊销） |

使用规则：**每次需要我推送的新对话开头贴一次**。令牌只出现在我执行的
命令参数里，不写入任何文件（上次推送后已验证过这一点）。感觉有风险
就随时去 token 页面 Delete，立即失效。不贴令牌时我照样能干活，
只是成果以 patch/zip 形式给你，你网页上传。

## 三、tools/task.sh —— 协议的可执行版

| 命令 | 作用 |
| --- | --- |
| `tools/task.sh start codex/修滚动` | 拉取最新 main 并从它开一个新分支 |
| `tools/task.sh sync` | 把当前分支变基到最新 main（开工前/别人合并后） |
| `tools/task.sh finish "提交说明"` | 跑全部测试 → 全绿才 commit+push |
| `tools/task.sh pr "PR 标题"` | 创建 PR（没装 gh 就给出网页链接） |

测试不绿它直接拒绝推送——这就是"main 永远可发布"的保证。

## 四、冲突怎么办（其实很难发生）

- 每个任务一个分支、任务范围写清楚"只做这一件事"→ 两个任务基本不会改同一处。
- 真撞上了：后完成的一方跑 `tools/task.sh sync`，git 会指出冲突文件，
  解完再 finish。Agent 解决不了的冲突，把 `git status` 输出贴给所有者或
  Arena 处理。
- 大改动（会动很多文件的）优先派给 Arena 或你本人，别让两个本地 agent
  同时开工大改动。

## 五、版本节奏

- 小改动：分支 → PR → 你在网页上点 Merge（选 **Squash and merge**，
  一个任务在 main 上就是一条干净的提交）。
- 发版（如 0.3.0）：改 `__init__.py` / `pyproject.toml` / `installer.iss`
  三处版本号 → 打 tag `v0.3.0` → CI 自动产出 Windows 构建产物
  （Action 页面下载）。
- 每次合并到 main 后，本地跑一次 `tools/task.sh sync` 即可保持同步。
