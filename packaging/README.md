# 构建发行包（Windows）

BugCompass 是零第三方 Python 依赖的 Tkinter 应用，打包流程刻意保持简单：
**PyInstaller（exe） + Inno Setup（安装器）**，全部在 Windows 机器上执行。

## 前置

- Windows 10/11 x64
- Python 3.11+（官方 python.org 构建，勾选 tcl/tk）
- `pip install pyinstaller`
- [Inno Setup 6](https://jrsoftware.org/isinfo.php)（含中文语言包）

## 步骤

```bat
git clone https://github.com/ayanamilmy/BugCompass.git
cd BugCompass

:: 1) 固定测试集必须全绿
python -m pytest -q

:: 2) 构建 exe（onedir，放 dist\BugCompass\）
pyinstaller --clean --noconfirm packaging\bugcompass.spec

:: 3) 快速自检（不需要 Codex，图形窗口应能打开）
dist\BugCompass\BugCompass.exe

:: 4) 生成安装包（产物在 packaging\Output\）
iscc packaging\installer.iss
```

升级版本时改三处并保持一致：`src/bugcompass/__init__.py` 的 `__version__`、
`pyproject.toml` 的 `version`、`packaging/installer.iss` 的 `MyAppVersion`。

## 发行前检查（完整清单见 docs/RELEASE-CHECKLIST.md）

1. 固定测试集全绿：`python -m pytest -q`（64 项）+ `xvfb-run -a python3 tools/gui_smoke.py`（Linux/CI 上的 19 项无头冒烟）。
2. 安装包在 100% / 150% / 200% DPI 的干净 Windows 上人工检查（无拖影/断层/裁切）。
3. 10 个真实案例从安装包进入 → 调查 → 保存 → 恢复全链路。
4. 断网状态下打开安装版，确认历史 Case 可以离线浏览（GUI 与浏览不依赖网络，只有 Codex 调查需要网络）。
5. 诊断包导出后抽查：不含 API key、不含源码、不含问题描述正文。

## 数据与资源

- 知识包（`packs/`、`.agents/`）由 PyInstaller 打进应用，运行时用 `bugcompass.resources.data_root()` 定位；
- 用户数据（设置、日志、诊断、备份、统计 outbox）全部在 `%APPDATA%\BugCompass\`；
- 工作区由用户选择位置；卸载不会删除用户数据。

## 关于 DPI

进程在创建 Tk 窗口前调用 `bugcompass.dpi.enable_windows_dpi_awareness()`
（默认系统级 DPI aware；`BUGCOMPASS_DPI_MODE=permonitor` 可切 per-monitor v2）。
若在多显示器混合 DPI 之间拖动窗口出现模糊，重启应用即可按新主屏 DPI 重建
（Tk 8.6 的已知限制，已在 docs/RELEASE-CHECKLIST.md 记录验证方法）。
