# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — 在 Windows 上构建发行版 exe。
#
#   cd BugCompass
#   pip install pyinstaller
#   pyinstaller --clean --noconfirm packaging/bugcompass.spec
#
# 产物：dist/BugCompass/BugCompass.exe（onedir，启动比 onefile 快得多）。
# 再用 packaging/installer.iss 生成安装包。

import sys
from pathlib import Path

REPO = Path(SPECPATH).resolve().parent  # spec 文件在 packaging/ 下

a = Analysis(
    [str(REPO / "src" / "bugcompass" / "__main__.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=[
        # 知识包与 Skill 会进入 _MEIPASS（frozen 下由 resources.data_root() 定位）。
        (str(REPO / "packs"), "packs"),
        (str(REPO / ".agents"), ".agents"),
        (str(REPO / "LICENSE"), "."),
        (str(REPO / "THIRD_PARTY.md"), "."),
    ],
    hiddenimports=["bugcompass.gui"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BugCompass",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序；崩溃日志走 ~/.bugcompass/logs，不需要控制台
    disable_windowed_traceback=False,
    icon=str(REPO / "packaging" / "bugcompass.ico") if (REPO / "packaging" / "bugcompass.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="BugCompass",
)
