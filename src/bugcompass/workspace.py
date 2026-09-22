from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Project, Workspace


class BugCompassError(Exception):
    """可安全展示给 CLI 用户的错误。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def is_git_repository(repo: Path) -> bool:
    if shutil.which("git") is None:
        raise BugCompassError("找不到 Git，请先安装 Git 并确保它位于 PATH 中。")
    result = _git(repo, "rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def init_workspace(workspace_path: str | Path, repo_path: str | Path) -> Workspace:
    workspace = Path(workspace_path).expanduser().resolve()
    repo = Path(repo_path).expanduser().resolve()

    if not repo.is_dir():
        raise BugCompassError(f"目标源码目录不存在或不是目录：{repo}")
    if not is_git_repository(repo):
        raise BugCompassError(f"目标目录看起来不是 Git 工作树：{repo}")
    if workspace == repo or repo in workspace.parents:
        raise BugCompassError("工作区不能位于 Blender 源码仓库内部，以免写入目标仓库。")
    if workspace.exists():
        raise BugCompassError(f"工作区已经存在，不会覆盖：{workspace}")

    project = Project(
        schema_version=1,
        project="blender",
        pack_version="0.1.0",
        repo_path=str(repo),
        created_at=utc_now(),
    )
    workspace.mkdir(parents=True)
    write_json(workspace / "project.json", project.to_dict())
    return Workspace(workspace, project)


def load_workspace(workspace_path: str | Path) -> Workspace:
    workspace = Path(workspace_path).expanduser().resolve()
    config_path = workspace / "project.json"
    if not config_path.is_file():
        raise BugCompassError(f"不是有效的 BugCompass 工作区，缺少 project.json：{workspace}")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        project = Project(
            schema_version=int(raw["schema_version"]),
            project=str(raw["project"]),
            pack_version=str(raw["pack_version"]),
            repo_path=str(raw["repo_path"]),
            created_at=str(raw["created_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise BugCompassError(f"无法读取工作区配置：{exc}") from exc
    if project.project != "blender":
        raise BugCompassError(f"当前版本只支持 Blender，配置项目为：{project.project}")
    if not Path(project.repo_path).is_absolute():
        raise BugCompassError("project.json 中的 repo_path 必须是绝对路径。")
    return Workspace(workspace, project)


def git_output(repo: Path, *args: str) -> str | None:
    try:
        result = _git(repo, *args)
    except FileNotFoundError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None
