"""资源定位：源码树、PyInstaller 打包后、便携版三种形态都要能找到 packs/ 和 .agents/。

打包成 Windows 安装包后 ``Path(__file__).resolve().parents[2]`` 不再指向仓库根目录，
所有读取知识包的地方必须走这里，否则安装版会在启动时崩溃。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


def is_frozen() -> bool:
    """是否被 PyInstaller / 类似工具打包。"""
    return bool(getattr(sys, "frozen", False))


@lru_cache(maxsize=1)
def bundle_root() -> Path:
    """打包后的只读资源目录（PyInstaller 的 _MEIPASS 或 exe 所在目录）。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(str(meipass)).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def data_root() -> Path:
    """packs/ 与 .agents/ 所在目录。

    优先级：
    1. 环境变量 ``BUGCOMPASS_DATA_ROOT``（便携版/自定义安装可用）；
    2. 打包资源目录；
    3. 源码树根目录；
    4. 用户数据目录 ``~/.bugcompass/data``（安装版可写副本）。
    """
    override = os.environ.get("BUGCOMPASS_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    for candidate in (bundle_root(), bundle_root() / "_bugcompass_data"):
        if (candidate / "packs").is_dir():
            return candidate
    return bundle_root()


@lru_cache(maxsize=1)
def user_root() -> Path:
    """用户可写目录（设置、日志、诊断包、备份默认位置）。"""
    override = os.environ.get("BUGCOMPASS_HOME")
    if override:
        base = Path(override).expanduser().resolve()
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "BugCompass" if appdata else Path.home() / ".bugcompass"
    else:
        base = Path.home() / ".bugcompass"
    base.mkdir(parents=True, exist_ok=True)
    return base


def pack_root(project: str = "blender") -> Path:
    return data_root() / "packs" / project


def skill_path(skill_name: str = "blender-bug-investigator") -> Path:
    return data_root() / ".agents" / "skills" / skill_name / "SKILL.md"


def settings_path() -> Path:
    return user_root() / "settings.json"


def logs_dir() -> Path:
    path = user_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backups_dir() -> Path:
    path = user_root() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_info() -> dict[str, str]:
    """诊断包里的运行环境摘要（不含任何密钥或源码）。"""
    return {
        "bugcompass_version": _version(),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "os_name": os.name,
        "frozen": str(is_frozen()),
        "data_root": str(data_root()),
        "user_root": str(user_root()),
    }


def _version() -> str:
    from . import __version__

    return __version__
