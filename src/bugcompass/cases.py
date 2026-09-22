from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .models import Workspace
from .investigation import empty_investigation, write_investigation
from .workspace import BugCompassError, git_output, load_workspace, utc_now, write_json


CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


INTAKE_TEMPLATE = """# 问题整理

> 等待 `blender-bug-investigator` Skill 根据原始问题与源码证据填写。

## 问题摘要

待调查。

## 预期行为

待整理。

## 实际行为

待整理。

## 复现步骤

待整理。

## 已知环境

待整理。

## 缺失信息

待整理。
"""

HYPOTHESES_TEMPLATE = """# 调查路径

> 等待 `blender-bug-investigator` Skill 基于证据生成恰好 3 条优先调查路径。

尚未生成调查路径。
"""

EVIDENCE_TEMPLATE = """# 证据记录

## 已观察事实

- 待调查。

## 推测

- 待验证。

## 未知信息

- 待补充。
"""

REPORT_TEMPLATE = """# 调查报告

## 当前结论

尚未形成结论。

## 验证记录

尚未开始验证。

## 后续行动

等待调查路径生成后补充。
"""


def validate_case_id(case_id: str) -> str:
    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise BugCompassError(
            "Case ID 非法：只能使用 1-64 个英文字母、数字、点、下划线或连字符，且必须以字母或数字开头。"
        )
    return case_id


def _case_dir(workspace: Workspace, case_id: str) -> Path:
    return workspace.path / "cases" / validate_case_id(case_id)


def create_case(workspace_path: str | Path, case_id: str, input_path: str | Path) -> dict[str, Any]:
    workspace = load_workspace(workspace_path)
    case_dir = _case_dir(workspace, case_id)
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise BugCompassError(f"Bug 描述文件不存在：{source}")
    if case_dir.exists():
        raise BugCompassError(f"Case 已经存在，不会覆盖：{case_id}")

    commit = git_output(workspace.repo_path, "rev-parse", "HEAD")
    metadata = {
        "schema_version": 1,
        "id": case_id,
        "project": "blender",
        "status": "new",
        "created_at": utc_now(),
        "input_source": str(source),
        "repo_commit": commit,
    }
    try:
        case_dir.mkdir(parents=True)
        shutil.copyfile(source, case_dir / "issue-original.md")
        write_json(case_dir / "case.json", metadata)
        write_investigation(case_dir / "investigation.json", empty_investigation(case_id))
        (case_dir / "intake.md").write_text(INTAKE_TEMPLATE, encoding="utf-8")
        (case_dir / "hypotheses.md").write_text(HYPOTHESES_TEMPLATE, encoding="utf-8")
        (case_dir / "evidence.md").write_text(EVIDENCE_TEMPLATE, encoding="utf-8")
        (case_dir / "report.md").write_text(REPORT_TEMPLATE, encoding="utf-8")
    except OSError as exc:
        raise BugCompassError(f"无法创建 Case：{exc}") from exc
    return metadata


def show_case(workspace_path: str | Path, case_id: str) -> tuple[dict[str, Any], dict[str, Path]]:
    workspace = load_workspace(workspace_path)
    case_dir = _case_dir(workspace, case_id)
    metadata_path = case_dir / "case.json"
    if not metadata_path.is_file():
        raise BugCompassError(f"找不到 Case：{case_id}")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BugCompassError(f"无法读取 Case：{exc}") from exc
    names = ("case.json", "investigation.json", "issue-original.md", "intake.md", "hypotheses.md", "evidence.md", "report.md")
    return metadata, {name: case_dir / name for name in names}


def update_case_status(workspace_path: str | Path, case_id: str, status: str) -> dict[str, Any]:
    allowed = {"new", "investigating", "complete", "failed", "cancelled"}
    if status not in allowed:
        raise BugCompassError(f"不支持的 Case 状态：{status}")
    metadata, paths = show_case(workspace_path, case_id)
    metadata["status"] = status
    try:
        write_json(paths["case.json"], metadata)
    except OSError as exc:
        raise BugCompassError(f"无法更新 Case 状态：{exc}") from exc
    return metadata
