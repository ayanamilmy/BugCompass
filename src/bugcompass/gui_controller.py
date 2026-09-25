from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .cases import create_case, show_case, update_case_status
from .doctor import COMMON_SOURCE_DIRS, run_doctor
from .experiments import ExperimentPlan, prepare_experiment, run_experiment
from .investigation import (
    empty_investigation,
    export_markdown,
    read_investigation,
    record_conclusion,
    record_experiment_prediction,
    reject_hypothesis,
    update_causal_graph,
    write_investigation,
)
from .models import Workspace
from .repro_report import PackageResult, ReportReview, export_package, load_draft, review_draft, save_draft
from .workspace import BugCompassError, init_workspace, is_git_repository, load_workspace


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class RepositoryValidation:
    valid: bool
    repo_path: Path
    message: str
    found_source_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecentCase:
    case_id: str
    created_at: str
    status: str


@dataclass(frozen=True)
class CaseView:
    case_id: str
    workspace_path: Path
    repo_path: Path
    case_dir: Path
    created_at: str
    status: str
    commit: str | None
    environment_status: str
    environment: dict[str, Any]
    contents: dict[str, str]
    investigation: dict[str, Any]
    awaiting_investigation: bool
    practice_session_id: str | None = None

    @property
    def short_commit(self) -> str:
        return self.commit[:12] if self.commit else "未知"


def generate_case_id(now: datetime | None = None) -> str:
    timestamp = now or datetime.now().astimezone()
    return f"blender-{timestamp:%Y%m%d-%H%M%S}"


class GuiController:
    def __init__(self, workspace_path: str | Path | None = None) -> None:
        default = Path.cwd() / "workspaces" / "blender-local"
        self.workspace_path = Path(workspace_path or default).expanduser().resolve()

    def validate_repository(self, repo_path: str | Path) -> RepositoryValidation:
        repo = Path(repo_path).expanduser().resolve()
        if not repo.is_dir():
            return RepositoryValidation(False, repo, "所选目录不存在或不是文件夹。")
        try:
            if not is_git_repository(repo):
                return RepositoryValidation(False, repo, "所选目录不是 Git 工作树。")
        except BugCompassError as exc:
            return RepositoryValidation(False, repo, str(exc))

        if not (repo / "CMakeLists.txt").is_file():
            return RepositoryValidation(False, repo, "未找到顶层 CMakeLists.txt，看起来不是 Blender 源码根目录。")

        found = tuple(name for name in COMMON_SOURCE_DIRS if (repo / name).is_dir())
        if not found:
            return RepositoryValidation(False, repo, "未找到典型 Blender 源码目录（例如 source/blender）。")
        return RepositoryValidation(True, repo, "看起来是有效的 Blender 源码仓库", found)

    def ensure_workspace(self, repo_path: str | Path) -> Workspace:
        repo = Path(repo_path).expanduser().resolve()
        if not self.workspace_path.exists():
            return init_workspace(self.workspace_path, repo)

        workspace = load_workspace(self.workspace_path)
        if workspace.repo_path.resolve() != repo:
            raise BugCompassError(
                "现有工作区指向另一个 Blender 仓库，不会覆盖。"
                f"\n现有仓库：{workspace.repo_path}\n所选仓库：{repo}"
            )
        return workspace

    def prepare_workspace(
        self,
        repo_path: str | Path,
        progress: ProgressCallback | None = None,
    ) -> Workspace:
        validation = self.validate_repository(repo_path)
        if not validation.valid:
            raise BugCompassError(validation.message)
        workspace = self.ensure_workspace(validation.repo_path)
        if progress:
            progress("正在检查 Blender 环境……")
        run_doctor(workspace.path)
        return workspace

    def create_investigation(
        self,
        repo_path: str | Path,
        bug_text: str,
        progress: ProgressCallback | None = None,
    ) -> CaseView:
        if not bug_text.strip():
            raise BugCompassError("Bug 描述不能为空。")

        workspace = self.prepare_workspace(repo_path, progress)

        if progress:
            progress("正在创建调查案件……")
        case_id = self._available_case_id()
        inputs_dir = workspace.path / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        input_path = inputs_dir / f"{case_id}.md"
        try:
            with input_path.open("x", encoding="utf-8") as stream:
                stream.write(bug_text)
            create_case(workspace.path, case_id, input_path)
        except OSError as exc:
            raise BugCompassError(f"无法保存 Bug 描述：{exc}") from exc
        return self.load_case(case_id)

    def _available_case_id(self) -> str:
        base = generate_case_id()
        cases_dir = self.workspace_path / "cases"
        inputs_dir = self.workspace_path / "inputs"

        def exists(case_id: str) -> bool:
            return (cases_dir / case_id).exists() or (inputs_dir / f"{case_id}.md").exists()

        if not exists(base):
            return base
        suffix = 2
        while exists(f"{base}-{suffix}"):
            suffix += 1
        return f"{base}-{suffix}"

    def load_case(self, case_id: str) -> CaseView:
        workspace = load_workspace(self.workspace_path)
        metadata, paths = show_case(workspace.path, case_id)
        contents: dict[str, str] = {}
        for name in ("intake.md", "hypotheses.md", "evidence.md", "issue-original.md"):
            try:
                contents[name] = paths[name].read_text(encoding="utf-8")
            except OSError as exc:
                raise BugCompassError(f"无法读取案件文件 {name}：{exc}") from exc

        environment = self._read_environment()
        if not paths["investigation.json"].is_file():
            write_investigation(paths["investigation.json"], empty_investigation(case_id))
        investigation = read_investigation(paths["investigation.json"])
        awaiting = len(investigation["hypotheses"]) == 0
        practice_session_id = metadata.get("practice_session_id")
        repo_path = workspace.repo_path
        if isinstance(practice_session_id, str):
            session_path = workspace.path / "practice" / "sessions" / practice_session_id / "session.json"
            try:
                session = json.loads(session_path.read_text(encoding="utf-8"))
                candidate = Path(session["checkout_path"]).resolve()
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise BugCompassError(f"无法读取历史练习会话：{exc}") from exc
            if not candidate.is_dir():
                raise BugCompassError(f"历史练习源码副本不存在：{candidate}")
            repo_path = candidate
        return CaseView(
            case_id=str(metadata.get("id", case_id)),
            workspace_path=workspace.path,
            repo_path=repo_path,
            case_dir=paths["case.json"].parent,
            created_at=str(metadata.get("created_at", "未知")),
            status=str(metadata.get("status", "unknown")),
            commit=metadata.get("repo_commit"),
            environment_status=str(environment.get("overall_status", "unknown")),
            environment=environment,
            contents=contents,
            investigation=investigation,
            awaiting_investigation=awaiting,
            practice_session_id=practice_session_id if isinstance(practice_session_id, str) else None,
        )

    def _read_environment(self) -> dict[str, Any]:
        path = self.workspace_path / "environment.json"
        if not path.is_file():
            return {"overall_status": "unknown", "message": "尚无环境检查结果。"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BugCompassError(f"无法读取环境检查结果：{exc}") from exc
        if not isinstance(data, dict):
            raise BugCompassError("环境检查结果格式无效。")
        return data

    def recent_cases(self, limit: int = 10) -> list[RecentCase]:
        cases_dir = self.workspace_path / "cases"
        if not cases_dir.is_dir():
            return []
        cases: list[RecentCase] = []
        for metadata_path in cases_dir.glob("*/case.json"):
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
                cases.append(
                    RecentCase(
                        case_id=str(data["id"]),
                        created_at=str(data.get("created_at", "未知")),
                        status=str(data.get("status", "unknown")),
                    )
                )
            except (OSError, KeyError, TypeError, json.JSONDecodeError):
                continue
        cases.sort(key=lambda item: item.created_at, reverse=True)
        return cases[:limit]

    def investigation_instruction(self, case_id: str) -> str:
        return (
            f"使用 $blender-bug-investigator 调查 BugCompass case {case_id}，"
            f"工作区是 {self.workspace_path}。只进行只读调查，不修改 Blender 源码。"
        )

    def set_case_status(self, case_id: str, status: str) -> None:
        update_case_status(self.workspace_path, case_id, status)

    def load_repro_report_draft(self, case_id: str) -> dict[str, Any]:
        _, paths = show_case(self.workspace_path, case_id)
        return load_draft(paths["case.json"].parent)

    def save_repro_report_draft(self, case_id: str, draft: dict[str, Any]) -> ReportReview:
        _, paths = show_case(self.workspace_path, case_id)
        save_draft(paths["case.json"].parent, draft)
        return review_draft(draft)

    def export_repro_report_package(self, case_id: str, output_path: str | Path) -> PackageResult:
        _, paths = show_case(self.workspace_path, case_id)
        return export_package(paths["case.json"].parent, output_path)

    def reject_path(self, case_id: str, hypothesis_id: str) -> CaseView:
        _, paths = show_case(self.workspace_path, case_id)
        reject_hypothesis(paths["investigation.json"], hypothesis_id)
        return self.load_case(case_id)

    def record_conclusion(self, case_id: str, hypothesis_id: str, statement: str) -> CaseView:
        _, paths = show_case(self.workspace_path, case_id)
        record_conclusion(paths["investigation.json"], hypothesis_id, statement)
        return self.load_case(case_id)

    def save_causal_graph(self, case_id: str, graph: dict[str, Any]) -> CaseView:
        _, paths = show_case(self.workspace_path, case_id)
        update_causal_graph(paths["investigation.json"], graph)
        return self.load_case(case_id)

    def record_prediction(self, case_id: str, experiment_id: str, choice: str, rationale: str) -> CaseView:
        _, paths = show_case(self.workspace_path, case_id)
        record_experiment_prediction(paths["investigation.json"], experiment_id, choice, rationale)
        return self.load_case(case_id)

    def resolve_source_reference(self, reference: dict[str, Any], repo_path: str | Path | None = None) -> tuple[Path, int | None]:
        workspace = load_workspace(self.workspace_path)
        repo = Path(repo_path).resolve() if repo_path is not None else workspace.repo_path.resolve()
        raw = reference.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise BugCompassError("源码引用缺少文件路径。")
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise BugCompassError("源码引用必须是 Blender 仓库内的相对路径。")
        target = (repo / relative).resolve()
        try:
            target.relative_to(repo)
        except ValueError as exc:
            raise BugCompassError("源码引用超出 Blender 仓库范围。") from exc
        if not target.is_file():
            raise BugCompassError(f"找不到引用的源码文件：{raw}")
        line = reference.get("line")
        if line is not None and (not isinstance(line, int) or line < 1):
            raise BugCompassError("源码引用行号无效。")
        return target, line

    def open_source_reference(self, reference: dict[str, Any], repo_path: str | Path | None = None) -> None:
        target, line = self.resolve_source_reference(reference, repo_path)
        code = shutil.which("code")
        if code:
            command = [code, "--goto", f"{target}:{line or 1}"]
        elif os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
            return
        elif sys.platform == "darwin":
            command = ["open", str(target)]
        else:
            opener = shutil.which("xdg-open")
            if not opener:
                raise BugCompassError("找不到可用于打开源码文件的编辑器或系统程序。")
            command = [opener, str(target)]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise BugCompassError(f"无法打开源码文件：{exc}") from exc

    def prepare_experiment(self, experiment: dict[str, Any], repo_path: str | Path | None = None) -> ExperimentPlan:
        return prepare_experiment(self.workspace_path, experiment, self._read_environment(), repo_path)

    def run_experiment(self, case_id: str, plan: ExperimentPlan) -> dict[str, Any]:
        _, paths = show_case(self.workspace_path, case_id)
        data = read_investigation(paths["investigation.json"])
        matching = next((item for item in data["suggested_experiments"] if item.get("id") == plan.experiment_id), None)
        prediction = matching.get("prediction") if matching else None
        if not isinstance(prediction, dict) or not prediction.get("predicted_at"):
            raise BugCompassError("运行实验前必须先记录你的结果预测。")
        record = run_experiment(self.workspace_path, case_id, plan, prediction=prediction)
        for experiment in data["suggested_experiments"]:
            if experiment.get("id") == plan.experiment_id:
                experiment["status"] = "completed"
                experiment["latest_run"] = record["run_id"]
                experiment["result"] = f"实验返回码 {record['return_code']}，等待 Codex 评估输出。"
                experiment["effect"] = "pending"
                break
        write_investigation(paths["investigation.json"], data)
        export_markdown(paths["case.json"].parent, data)
        return record
