from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .cases import create_case, show_case
from .investigation import read_investigation
from .workspace import BugCompassError, load_workspace, utc_now, write_json


ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class PracticeCase:
    case_id: str
    issue_id: int
    title: str
    difficulty: str
    symptom: str
    reproduction: tuple[str, ...]
    pre_fix_commit: str
    suggested_minutes: int
    suggested_ai_runs: int
    tags: tuple[str, ...]


@dataclass(frozen=True)
class PracticeReveal:
    answer: dict[str, Any]
    scores: dict[str, int]
    quality_total: int
    quality_max: int
    elapsed_seconds: int
    ai_runs: int
    submission: str


class PracticeManager:
    def __init__(self, project_root: str | Path, workspace_path: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace_path = Path(workspace_path).expanduser().resolve()
        self.pack_dir = self.project_root / "packs" / "blender" / "practice"

    def list_cases(self) -> list[PracticeCase]:
        data = self._read_json(self.pack_dir / "catalog.json", "练习案例")
        cases: list[PracticeCase] = []
        for raw in data.get("cases", []):
            cases.append(
                PracticeCase(
                    case_id=str(raw["id"]),
                    issue_id=int(raw["issue_id"]),
                    title=str(raw["title"]),
                    difficulty=str(raw["difficulty"]),
                    symptom=str(raw["reported_symptom"]),
                    reproduction=tuple(str(item) for item in raw["reproduction_outline"]),
                    pre_fix_commit=str(raw["pre_fix_commit"]),
                    suggested_minutes=int(raw["suggested_minutes"]),
                    suggested_ai_runs=int(raw["suggested_ai_runs"]),
                    tags=tuple(str(item) for item in raw["tags"]),
                )
            )
        return cases

    def get_case(self, practice_case_id: str) -> PracticeCase:
        for case in self.list_cases():
            if case.case_id == practice_case_id:
                return case
        raise BugCompassError(f"找不到历史练习案例：{practice_case_id}")

    def start_session(self, practice_case_id: str, progress: ProgressCallback | None = None) -> tuple[str, Path]:
        case = self.get_case(practice_case_id)
        workspace = load_workspace(self.workspace_path)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        case_id = f"practice-{case.issue_id}-{timestamp}"
        session_id = case_id
        session_dir = workspace.path / "practice" / "sessions" / session_id
        checkout = session_dir / "checkout"
        if session_dir.exists():
            raise BugCompassError(f"练习会话已经存在，不会覆盖：{session_id}")
        session_dir.mkdir(parents=True)
        if progress:
            progress("正在创建隔离的历史源码副本……")
        self._run_git(
            ["clone", "--shared", "--no-checkout", "--quiet", str(workspace.repo_path), str(checkout)],
            "无法创建历史练习源码副本",
        )
        self._run_git(["-C", str(checkout), "checkout", "--detach", "--quiet", case.pre_fix_commit], "无法切换到修复前 commit")
        actual = self._git_output(checkout, "rev-parse", "HEAD")
        if actual != case.pre_fix_commit:
            raise BugCompassError("历史练习源码没有切换到预期 commit。")

        issue_text = self._render_issue(case)
        input_dir = workspace.path / "inputs"
        input_dir.mkdir(parents=True, exist_ok=True)
        input_path = input_dir / f"{case_id}.md"
        input_path.write_text(issue_text, encoding="utf-8")
        create_case(workspace.path, case_id, input_path)
        metadata, paths = show_case(workspace.path, case_id)
        metadata.update(
            {
                "mode": "historical_practice",
                "practice_session_id": session_id,
                "practice_case_id": case.case_id,
                "repo_commit": case.pre_fix_commit,
            }
        )
        write_json(paths["case.json"], metadata)
        session = {
            "schema_version": 1,
            "id": session_id,
            "practice_case_id": case.case_id,
            "case_id": case_id,
            "checkout_path": str(checkout.resolve()),
            "pre_fix_commit": case.pre_fix_commit,
            "started_at": utc_now(),
            "status": "investigating",
            "ai_runs": 0,
        }
        write_json(session_dir / "session.json", session)
        return case_id, checkout.resolve()

    def session_for_case(self, case_id: str) -> dict[str, Any] | None:
        try:
            metadata, _ = show_case(self.workspace_path, case_id)
        except BugCompassError:
            return None
        session_id = metadata.get("practice_session_id")
        if not isinstance(session_id, str):
            return None
        return self._read_json(self.workspace_path / "practice" / "sessions" / session_id / "session.json", "练习会话")

    def increment_ai_runs(self, case_id: str) -> None:
        session = self.session_for_case(case_id)
        if not session:
            return
        session["ai_runs"] = int(session.get("ai_runs", 0)) + 1
        self._write_session(session)

    def submit_judgment(self, case_id: str, judgment: str) -> None:
        if not judgment.strip():
            raise BugCompassError("请先填写你的根因判断。")
        session = self._require_session(case_id)
        session_dir = self._session_dir(session["id"])
        write_json(
            session_dir / "submission.json",
            {"schema_version": 1, "case_id": case_id, "submitted_at": utc_now(), "root_cause_judgment": judgment.strip()},
        )
        session["status"] = "submitted"
        session["submitted_at"] = utc_now()
        self._write_session(session)

    def reveal(self, case_id: str) -> PracticeReveal:
        session = self._require_session(case_id)
        session_dir = self._session_dir(session["id"])
        submission_path = session_dir / "submission.json"
        if not submission_path.is_file():
            raise BugCompassError("请先提交自己的根因判断，再揭晓真实修复。")
        submission = self._read_json(submission_path, "根因判断")
        answer = self._answer(str(session["practice_case_id"]))
        _, paths = show_case(self.workspace_path, case_id)
        investigation = read_investigation(paths["investigation.json"])
        scores = self._score(investigation, answer)
        elapsed = self._elapsed_seconds(str(session["started_at"]), utc_now())
        reveal = {
            "schema_version": 1,
            "revealed_at": utc_now(),
            "scores": scores,
            "quality_total": sum(scores.values()),
            "quality_max": 10,
            "elapsed_seconds": elapsed,
            "ai_runs": int(session.get("ai_runs", 0)),
            "answer": answer,
            "submission": submission["root_cause_judgment"],
        }
        write_json(session_dir / "reveal.json", reveal)
        session["status"] = "revealed"
        session["revealed_at"] = reveal["revealed_at"]
        self._write_session(session)
        return PracticeReveal(
            answer=answer,
            scores=scores,
            quality_total=int(reveal["quality_total"]),
            quality_max=10,
            elapsed_seconds=elapsed,
            ai_runs=int(reveal["ai_runs"]),
            submission=str(reveal["submission"]),
        )

    def load_reveal(self, case_id: str) -> PracticeReveal | None:
        session = self.session_for_case(case_id)
        if not session:
            return None
        path = self._session_dir(session["id"]) / "reveal.json"
        if not path.is_file():
            return None
        data = self._read_json(path, "揭晓结果")
        return PracticeReveal(
            answer=data["answer"],
            scores={str(key): int(value) for key, value in data["scores"].items()},
            quality_total=int(data["quality_total"]),
            quality_max=int(data["quality_max"]),
            elapsed_seconds=int(data["elapsed_seconds"]),
            ai_runs=int(data["ai_runs"]),
            submission=str(data["submission"]),
        )

    def _answer(self, practice_case_id: str) -> dict[str, Any]:
        data = self._read_json(self.pack_dir / "answers.json", "练习答案")
        for answer in data.get("answers", []):
            if answer.get("case_id") == practice_case_id:
                return answer
        raise BugCompassError("找不到该练习的答案。")

    @staticmethod
    def _score(investigation: dict[str, Any], answer: dict[str, Any]) -> dict[str, int]:
        hypotheses = investigation.get("hypotheses", [])
        references = []
        exact_hypothesis_ids: set[str] = set()
        real_files = set(answer.get("relevant_files", []))
        for hypothesis in hypotheses[:3]:
            for reference in hypothesis.get("source_references", []):
                if not isinstance(reference, dict):
                    continue
                path = str(reference.get("path", ""))
                references.append(path)
                if path in real_files:
                    exact_hypothesis_ids.add(str(hypothesis.get("id", "")))
        exact_files = len(real_files.intersection(references))
        real_areas = {"/".join(Path(path).parts[:4]) for path in real_files}
        found_areas = {"/".join(Path(path).parts[:4]) for path in references}
        file_score = 2 if exact_files else (1 if real_areas.intersection(found_areas) else 0)
        subsystem_score = 2 if real_areas.intersection(found_areas) else (1 if references else 0)
        facts = [item for item in investigation.get("evidence", []) if item.get("kind") == "fact"]
        sourced_facts = [item for item in facts if item.get("source_references")]
        evidence_score = 2 if sourced_facts else (1 if facts else 0)
        experiments = investigation.get("suggested_experiments", [])
        executable = [item for item in experiments if item.get("command") and item.get("hypothesis_id")]
        relevant_experiments = [item for item in executable if str(item.get("hypothesis_id")) in exact_hypothesis_ids]
        experiment_score = 2 if relevant_experiments else (1 if executable else 0)
        unknowns = investigation.get("unknowns", [])
        lock_score = 2 if len(hypotheses) == 3 and unknowns else (1 if len(hypotheses) == 3 else 0)
        return {
            "true_subsystem_in_top3": subsystem_score,
            "relevant_files_found": file_score,
            "valid_evidence": evidence_score,
            "revealing_experiment": experiment_score,
            "premature_lock_in": lock_score,
        }

    def _require_session(self, case_id: str) -> dict[str, Any]:
        session = self.session_for_case(case_id)
        if not session:
            raise BugCompassError("当前 Case 不是历史练习。")
        return session

    def _write_session(self, session: dict[str, Any]) -> None:
        write_json(self._session_dir(str(session["id"])) / "session.json", session)

    def _session_dir(self, session_id: str) -> Path:
        return self.workspace_path / "practice" / "sessions" / session_id

    @staticmethod
    def _render_issue(case: PracticeCase) -> str:
        steps = "\n".join(f"{index}. {step}" for index, step in enumerate(case.reproduction, 1))
        return (
            f"# 历史练习 #{case.issue_id}：{case.title}\n\n"
            f"## 问题现象\n\n{case.symptom}\n\n## 复现轮廓\n\n{steps}\n\n"
            "> 这是历史练习。真实修复、根因和答案在提交判断前不可见。\n"
        )

    @staticmethod
    def _elapsed_seconds(start: str, end: str) -> int:
        return max(0, int((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()))

    @staticmethod
    def _run_git(args: list[str], message: str) -> None:
        try:
            result = subprocess.run(["git", *args], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError as exc:
            raise BugCompassError(f"{message}：{exc}") from exc
        if result.returncode != 0:
            raise BugCompassError(f"{message}：{result.stderr.strip() or result.stdout.strip()}")

    @staticmethod
    def _git_output(repo: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BugCompassError(f"无法读取{label}：{exc}") from exc
        if not isinstance(data, dict):
            raise BugCompassError(f"{label}格式无效。")
        return data
