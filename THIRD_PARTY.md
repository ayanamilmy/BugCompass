# 第三方材料清单与再发布权利核查

本文件是 `pyproject.toml` 中 MIT 声明的配套审计（v0.2.0，2026-09-23）。
结论分两类：

- **【已核验】**：可以通过检查仓库内容直接核验的事实；
- **【需负责人确认】**：涉及仓库外部的事实或法律判断，只有仓库所有者能拍板。

> 本文件不构成法律意见。若计划商业分发或仍有疑虑，请咨询律师。

---

## 1. 本仓库自有代码

| 范围 | 许可 | 说明 |
| --- | --- | --- |
| `src/bugcompass/`、`tests/`、`tools/` | MIT | 本项目代码，`LICENSE` 文件已就位。 |
| `packs/blender/*.json`（结构定义、source-map、manifest） | MIT | 自有数据。 |

运行时**零第三方 Python 依赖**（`dependencies = []`，仅用标准库 + Tkinter），因此没有随包分发任何第三方 Python 库。

## 2. 打包工具链（不进入发行包）

| 工具 | 许可 | 是否随发行包分发 |
| --- | --- | --- |
| PyInstaller | GPL，带 Bootloader 例外 | 否。仅用于构建；PyInstaller 官方明确允许闭源/任意许可的被打包程序商业分发。 |
| Inno Setup | Inno Setup License（可免费商用） | 否。仅用于生成安装器，安装器本身不视为 Inno Setup 的衍生作品。 |
| Python / Tk / Tcl | PSF / Tcl/Tk License | 以系统 Python 打包时由 PyInstaller 附带，许可允许再分发；构建时所用 Python 需为官方构建。 |

## 3. 字体

代码中引用的字体族名称（`SF Pro Text` / `SF Pro Display` / `SF Mono`）**仅作为族名请求**；仓库和发行包中**没有嵌入任何字体文件**。在 Windows/Linux 上 Tk 找不到这些族名时会回退到系统默认字体。因此不存在分发 Apple 字体的问题。若未来想在 Windows 上获得更接近的观感，请使用开源字体（如 Inter / JetBrains Mono）并遵循其 OFL 许可——**不要**把 SF Pro 打进安装包。

## 4. Blender 练习数据（`packs/blender/practice/`）

详细逐案核查见 [`PROVENANCE.md`](packs/blender/practice/PROVENANCE.md)。要点：

| 材料 | 仓库中是否存在 | 权利分析 |
| --- | --- | --- |
| Blender 源码 diff / 代码片段 | **不存在**（已用 `+++` / `@@` / `diff --git` / `#include` / `bpy.ops` 全文检索确认为零） | 无 Blender 源码再发布问题。 |
| 原始 issue 全文（英文原文） | **不存在** | 仓库只包含中文改写摘要。 |
| commit SHA、PR 编号、修复前 commit | 存在 | 纯事实性标识符，通常不构成可版权表达。 |
| Blender 源码文件路径 / 函数符号名 | 存在 | 事实性引用；Blender 源码为 GPL-2.0-or-later，但路径与符号名本身为事实引用，风险极低。 |
| 中文症状/根因摘要 | 存在 | **【需负责人确认】** 由仓库作者手工撰写（见 practice/README.md 的声明）。若确为自行改写而非逐句翻译，则权利属于作者，可按 MIT 一并授权；若接近直译，建议标注出处链接而非依赖摘要文本。 |
| 附件 / 截图 / `.blend` 文件 | **不存在**（`find` 全仓库无 png/jpg/blend/pdf） | 无需处理。 |

## 5. 问题描述（用户输入）与生成内容

- 用户在 GUI 里粘贴的 Bug 描述保存在**用户本地工作区**，不进入仓库，也不进入发行包。
- 诊断包（diagnostics）按设计排除问题描述正文与源码（有测试强制：`tests/test_delivery.py::DiagnosticsTests`）。

## 6. 遗留待办（负责人签署项）

1. 确认 `catalog.json` 的 `reported_symptom` 与 `answers.json` 的 `root_cause`/`fix_summary` 均为自行改写（在 `PROVENANCE.md` 表中逐项打勾）。
2. 若未来加入截图/附件/源码片段：必须先在本文件登记来源与许可，再提交。
3. 练习数据若保持“仅元数据”的边界，请把 `PROVENANCE.md` 的核查表作为发布前检查项。
