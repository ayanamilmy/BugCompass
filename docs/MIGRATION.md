# 备份、恢复与迁移指南

## 数据都在哪里

| 位置 | 内容 | 是否随备份走 |
| --- | --- | --- |
| `<工作区>/project.json` | 工作区配置（指向的 Blender 仓库路径） | ✅ |
| `<工作区>/cases/<Case>/` | 每个案件的全部内容（问题描述、调查结果、因果链、实验记录、指标） | ✅ |
| `<工作区>/inputs/` | 创建案件时保存的原始输入副本 | ✅ |
| `~/.bugcompass/settings.json`（Windows: `%APPDATA%\BugCompass\`） | 界面缩放、统计上报开关 | ❌（与案件无关，见下） |
| `~/.bugcompass/logs/` | 崩溃日志 | ❌（诊断包会用） |
| `~/.bugcompass/backups/` | 默认备份输出位置 | ❌ |
| `~/.bugcompass/telemetry-outbox/` | 本地统计事件（默认关闭且为空） | ❌（有意不带走） |
| `~/.bugcompass/pricing.json` | 你自己填的模型单价 | ❌ |

备份 zip 不包含 Blender 仓库本身；恢复后若仓库路径变化，改恢复出来的 `project.json` 里的 `repo_path` 即可。

## 备份

GUI：打开任意案件 → 工具栏「备份」（默认存到 `~/.bugcompass/backups/`）。

CLI：

```bash
bugcompass backup --workspace <工作区路径> --output <目标.zip>
```

备份包内含 `backup-manifest.json`：文件清单 + 每个文件的 sha256 + 应用版本 + 创建时间。

## 恢复

GUI：工具栏「恢复备份…」选择 zip，再选恢复目标文件夹。
恢复后的工作区目录名与备份时一致，放在你选的目标文件夹之下。

CLI：

```bash
bugcompass restore --archive <备份.zip> --dest <目标文件夹> [--overwrite]
```

恢复时自动执行格式迁移（见下）。`--overwrite` 才会覆盖已存在的非空目录。

## 版本迁移

- 备份格式 `schema_version` 1 → 2（v0.2.0 起）：`project.json` 与 `case.json` 升到 schema 2，并登记 `metrics.json`。
- 迁移是**幂等**的：重复执行无副作用。
- 打开旧工作区（GUI 直接打开案件）或恢复备份时自动迁移；也可以手动：

```bash
bugcompass migrate --workspace <工作区路径>
```

- 备份由更新版本创建时（schema 高于当前程序）恢复会被拒绝并提示先升级程序——不会写坏数据。

## 从 0.1.x 迁移到 0.2.x 的注意事项

1. 0.1.x 工作区无需任何手工操作，直接用 0.2.x 打开即可（自动迁移）。
2. `project.json` 里的 `repo_path` 是绝对路径；换机器恢复后请确认路径仍有效。
3. 0.2.x 新增的用户目录（`~/.bugcompass/`）首次运行时自动创建。
4. 历史练习的会话数据在 `<工作区>/practice/`，随备份一起走，无需单独处理。
