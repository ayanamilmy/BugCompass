from __future__ import annotations

import platform
import shutil
import sys
import os
from pathlib import Path
from typing import Any

from .workspace import BugCompassError, git_output, load_workspace, utc_now, write_json


COMMON_SOURCE_DIRS = (
    "source/blender",
    "source/blender/blenkernel",
    "source/blender/editors",
    "source/blender/depsgraph",
    "intern",
)


def _item(status: str, value: Any, message: str) -> dict[str, Any]:
    return {"status": status, "value": value, "message": message}


def _find_build_directories(repo: Path) -> list[Path]:
    candidates: set[Path] = set()
    for parent in (repo, repo.parent):
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and (child.name.lower().startswith("build") or (child / "CMakeCache.txt").is_file()):
                if (child / "CMakeCache.txt").is_file() or any((child / name).exists() for name in ("bin", "build.ninja", "Makefile")):
                    candidates.add(child.resolve())
    return sorted(candidates)


def _find_blender_executables(build_dirs: list[Path]) -> list[Path]:
    relative_candidates = (
        "bin/blender",
        "bin/Release/blender",
        "bin/Debug/blender",
        "bin/blender.app/Contents/MacOS/Blender",
        "bin/Blender.app/Contents/MacOS/Blender",
        "bin/Release/blender.exe",
        "bin/Debug/blender.exe",
    )
    found: list[Path] = []
    for build in build_dirs:
        for relative in relative_candidates:
            candidate = build / relative
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                found.append(candidate.resolve())
    return found


def _cmake_source_directory(build_dir: Path) -> Path | None:
    cache = build_dir / "CMakeCache.txt"
    try:
        for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CMAKE_HOME_DIRECTORY:INTERNAL="):
                return Path(line.split("=", 1)[1]).expanduser().resolve()
    except OSError:
        return None
    return None


def _binary_sync_status(repo: Path, commit: str | None, build_dirs: list[Path], executables: list[Path]) -> tuple[str, dict[str, Any], str]:
    mismatched = [str(path) for path in build_dirs if (source := _cmake_source_directory(path)) is not None and source != repo.resolve()]
    if mismatched:
        return "warning", {"state": "source_mismatch", "build_directories": mismatched}, "部分构建目录来自另一个源码目录，二进制可能不匹配。"
    if not executables or not commit:
        return "warning", {"state": "unknown"}, "没有足够信息判断二进制与源码是否同步。"
    timestamp = git_output(repo, "show", "-s", "--format=%ct", commit)
    try:
        commit_time = int(timestamp or "")
    except ValueError:
        return "warning", {"state": "unknown"}, "无法读取当前 commit 时间。"
    stale = [str(path) for path in executables if path.stat().st_mtime < commit_time]
    if stale:
        return "warning", {"state": "possibly_stale", "older_than_commit": stale}, "Blender 二进制早于当前 commit，可能不同步。"
    return "ok", {"state": "possibly_current", "older_than_commit": []}, "二进制不早于当前 commit，但仍不能保证完全同步。"


def run_doctor(workspace_path: str | Path) -> dict[str, Any]:
    workspace = load_workspace(workspace_path)
    repo = workspace.repo_path
    checks: dict[str, dict[str, Any]] = {}

    checks["repo_path"] = _item(
        "ok" if repo.is_dir() else "error",
        str(repo),
        "Blender 源码目录存在。" if repo.is_dir() else "Blender 源码目录不存在。",
    )
    git_path = shutil.which("git")
    checks["git"] = _item(
        "ok" if git_path else "error",
        git_path,
        "可以使用 Git。" if git_path else "找不到 Git。",
    )

    branch: str | None = None
    commit: str | None = None
    dirty: bool | None = None
    if git_path and repo.is_dir():
        commit = git_output(repo, "rev-parse", "HEAD")
        branch = git_output(repo, "symbolic-ref", "--short", "-q", "HEAD")
        status_text = git_output(repo, "status", "--porcelain")
        dirty = None if status_text is None else bool(status_text)

    checks["head"] = _item(
        "ok" if commit else "error",
        {"branch": branch, "detached": commit is not None and branch is None, "commit": commit},
        (f"当前分支：{branch}" if branch else "当前为 detached HEAD。") if commit else "无法读取 Git HEAD。",
    )
    checks["working_tree"] = _item(
        "warning" if dirty else ("ok" if dirty is False else "error"),
        {"dirty": dirty},
        "源码仓库有未提交修改；调查时应注意区分本地变化。" if dirty else (
            "源码仓库工作树干净。" if dirty is False else "无法读取工作树状态。"
        ),
    )

    found_dirs = [name for name in COMMON_SOURCE_DIRS if (repo / name).is_dir()]
    checks["source_directories"] = _item(
        "ok" if found_dirs else "warning",
        {"found": found_dirs, "expected_any": list(COMMON_SOURCE_DIRS)},
        f"找到 {len(found_dirs)} 个常见 Blender 源码目录。" if found_dirs else "未找到常见 Blender 源码目录。",
    )
    cmake = repo / "CMakeLists.txt"
    checks["cmake"] = _item(
        "ok" if cmake.is_file() else "warning",
        str(cmake) if cmake.is_file() else None,
        "找到顶层 CMakeLists.txt。" if cmake.is_file() else "未找到顶层 CMakeLists.txt。",
    )
    build_dirs = _find_build_directories(repo)
    checks["build_directories"] = _item(
        "ok" if build_dirs else "warning",
        {"found": [str(path) for path in build_dirs]},
        f"找到 {len(build_dirs)} 个候选构建目录。" if build_dirs else "未找到现有构建目录；不会自动开始完整构建。",
    )
    executables = _find_blender_executables(build_dirs)
    checks["blender_executables"] = _item(
        "ok" if executables else "warning",
        {"found": [str(path) for path in executables]},
        f"找到 {len(executables)} 个 Blender 可执行文件。" if executables else "未找到已构建的 Blender 可执行文件。",
    )
    sync_status, sync_value, sync_message = _binary_sync_status(repo, commit, build_dirs, executables)
    checks["binary_source_sync"] = _item(sync_status, sync_value, sync_message)
    build_tool = next((path for name in ("ninja", "make", "cmake") if (path := shutil.which(name))), None)
    ctest = shutil.which("ctest")
    capabilities = {
        "investigate": "ok" if commit and found_dirs else "error",
        "build": "ok" if build_dirs and build_tool else "warning",
        "test": "ok" if build_dirs and ctest else "warning",
    }
    checks["capabilities"] = _item(
        "error" if "error" in capabilities.values() else ("warning" if "warning" in capabilities.values() else "ok"),
        capabilities,
        "调查能力、构建能力和测试能力已分别评估；本检查不会启动构建。",
    )
    py_ok = sys.version_info >= (3, 11)
    checks["python"] = _item(
        "ok" if py_ok else "error",
        {"version": platform.python_version(), "executable": sys.executable},
        "Python 版本满足 3.11+。" if py_ok else "需要 Python 3.11 或更高版本。",
    )

    statuses = {item["status"] for item in checks.values()}
    overall = "error" if "error" in statuses else ("warning" if "warning" in statuses else "ok")
    result = {
        "schema_version": 1,
        "checked_at": utc_now(),
        "overall_status": overall,
        "checks": checks,
    }
    try:
        write_json(workspace.path / "environment.json", result)
    except OSError as exc:
        raise BugCompassError(f"无法保存环境检查结果：{exc}") from exc
    return result
