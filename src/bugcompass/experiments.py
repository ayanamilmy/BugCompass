from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import BugCompassError, git_output, load_workspace, utc_now, write_json


GREEN_COMMANDS = {"rg", "grep", "cat", "head", "tail", "sed"}
GREEN_GIT_COMMANDS = {"blame", "diff", "grep", "log", "rev-list", "rev-parse", "show", "status"}
YELLOW_COMMANDS = {"blender", "cmake", "ctest", "make", "ninja"}
RED_COMMANDS = {"cp", "git-apply", "mv", "patch", "rm"}
EXPERIMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ExperimentPlan:
    experiment_id: str
    hypothesis_id: str
    title: str
    purpose: str
    command: tuple[str, ...]
    cwd: Path
    permission: str
    estimated_seconds: int

    @property
    def command_text(self) -> str:
        return subprocess.list2cmdline(self.command)


def classify_command(command: list[str]) -> str:
    if not command or not all(isinstance(part, str) and part for part in command):
        raise BugCompassError("实验命令必须是非空参数数组。")
    executable = Path(command[0]).name.lower()
    if executable == "git":
        subcommand = next((part for part in command[1:] if not part.startswith("-")), "")
        if subcommand in GREEN_GIT_COMMANDS:
            return "green"
        if subcommand in {"push", "commit", "reset", "clean", "checkout", "switch", "merge", "rebase", "apply"}:
            return "red"
        return "red"
    if executable in GREEN_COMMANDS:
        return "green"
    if executable in YELLOW_COMMANDS or executable.startswith("blender"):
        return "yellow"
    if executable in RED_COMMANDS:
        return "red"
    return "red"


def _allowed_roots(workspace_path: str | Path, environment: dict[str, Any], repo_root: Path | None = None) -> list[Path]:
    workspace = load_workspace(workspace_path)
    roots = [workspace.repo_path.resolve()]
    if repo_root is not None and repo_root.resolve() not in roots:
        roots.append(repo_root.resolve())
    build_value = environment.get("checks", {}).get("build_directories", {}).get("value", {})
    for raw in build_value.get("found", []) if isinstance(build_value, dict) else []:
        path = Path(raw).resolve()
        if path.is_dir():
            roots.append(path)
    return roots


def prepare_experiment(
    workspace_path: str | Path,
    experiment: dict[str, Any],
    environment: dict[str, Any],
    repo_path: str | Path | None = None,
) -> ExperimentPlan:
    command = experiment.get("command")
    if not isinstance(command, list):
        raise BugCompassError("实验缺少 command 参数数组。")
    experiment_id = experiment.get("id")
    hypothesis_id = experiment.get("hypothesis_id")
    if not isinstance(experiment_id, str) or not EXPERIMENT_ID_PATTERN.fullmatch(experiment_id):
        raise BugCompassError("实验 ID 非法。")
    if not isinstance(hypothesis_id, str) or not EXPERIMENT_ID_PATTERN.fullmatch(hypothesis_id):
        raise BugCompassError("实验关联的假设 ID 非法。")
    permission = classify_command(command)
    declared = experiment.get("permission")
    if declared != permission:
        raise BugCompassError(f"实验权限声明不可信：声明为 {declared}，实际判定为 {permission}。")
    for argument in command[1:]:
        path_like = Path(argument)
        if path_like.is_absolute() or ".." in path_like.parts:
            raise BugCompassError("实验参数不能引用允许范围外的绝对路径或上级目录。")
    workspace = load_workspace(workspace_path)
    repo = Path(repo_path).resolve() if repo_path is not None else workspace.repo_path.resolve()
    raw_cwd = experiment.get("cwd", ".")
    if not isinstance(raw_cwd, str):
        raise BugCompassError("实验工作目录无效。")
    cwd = (repo / raw_cwd).resolve() if not Path(raw_cwd).is_absolute() else Path(raw_cwd).resolve()
    if not any(cwd == root or root in cwd.parents for root in _allowed_roots(workspace_path, environment, repo)):
        raise BugCompassError("实验工作目录必须位于 Blender 源码或已识别的构建目录内。")
    if not cwd.is_dir():
        raise BugCompassError(f"实验工作目录不存在：{cwd}")
    timeout = experiment.get("estimated_seconds", 30)
    if not isinstance(timeout, int) or timeout < 1 or timeout > 3600:
        raise BugCompassError("实验预计耗时必须在 1 到 3600 秒之间。")
    return ExperimentPlan(
        experiment_id=experiment_id,
        hypothesis_id=hypothesis_id,
        title=str(experiment.get("title", "未命名实验")),
        purpose=str(experiment.get("purpose") or experiment.get("description") or ""),
        command=tuple(command),
        cwd=cwd,
        permission=permission,
        estimated_seconds=timeout,
    )


def _snapshot_files(root: Path, limit: int = 5000) -> set[str]:
    found: set[str] = set()
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name != ".git"]
        for name in files:
            try:
                found.add(str((Path(directory) / name).relative_to(root)))
            except ValueError:
                continue
            if len(found) >= limit:
                return found
    return found


def run_experiment(
    workspace_path: str | Path,
    case_id: str,
    plan: ExperimentPlan,
    *,
    timeout_seconds: int | None = None,
    prediction: dict[str, str] | None = None,
) -> dict[str, Any]:
    workspace = load_workspace(workspace_path)
    case_dir = workspace.path / "cases" / case_id
    if not (case_dir / "case.json").is_file():
        raise BugCompassError(f"找不到 Case：{case_id}")
    started_at = utc_now()
    run_id = started_at.replace(":", "").replace("-", "").replace("T", "-").replace("Z", "") + f"-{uuid.uuid4().hex[:8]}"
    run_dir = case_dir / "experiments" / f"{plan.experiment_id}-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=False)
    before = _snapshot_files(plan.cwd)
    timed_out = False
    try:
        completed = subprocess.run(
            list(plan.command),
            cwd=plan.cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds or max(plan.estimated_seconds * 3, 30),
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + "\n实验超时，已终止。"
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = str(exc)
    after = _snapshot_files(plan.cwd)
    produced = sorted(after.difference(before))
    ended_at = utc_now()
    (run_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (run_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    record = {
        "schema_version": 1,
        "run_id": run_dir.name,
        "experiment_id": plan.experiment_id,
        "hypothesis_id": plan.hypothesis_id,
        "permission": plan.permission,
        "command": list(plan.command),
        "command_text": plan.command_text,
        "working_directory": str(plan.cwd),
        "blender_commit": git_output(plan.cwd, "rev-parse", "HEAD"),
        "started_at": started_at,
        "ended_at": ended_at,
        "return_code": returncode,
        "timed_out": timed_out,
        "stdout_file": "stdout.txt",
        "stderr_file": "stderr.txt",
        "produced_files": produced,
        "hypothesis_effect": "pending_codex_review",
        "prediction": prediction or {"choice": "", "rationale": "", "predicted_at": ""},
        "prediction_assessment": "pending_codex_review",
    }
    write_json(run_dir / "result.json", record)
    return record


def latest_experiment_record(case_dir: str | Path, experiment_id: str) -> Path | None:
    candidates = sorted((Path(case_dir) / "experiments").glob(f"{experiment_id}-*/result.json"))
    return candidates[-1] if candidates else None
