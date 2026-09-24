"""无需 Blender 源码或调查引擎的本地 Bug 报告库。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .repro_report import (
    PackageResult,
    ReportReview,
    blank_draft,
    export_draft_package,
    format_official_body,
    load_draft,
    review_draft,
    save_draft,
)
from .resources import user_root
from .workspace import BugCompassError, utc_now, write_json


REPORT_ID = re.compile(r"report-[0-9a-f]{12}\Z")


@dataclass(frozen=True)
class SavedReport:
    report_id: str
    title: str
    updated_at: float
    ready_for_review: bool


class ReportStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else user_root() / "reports"

    def _directory(self, report_id: str) -> Path:
        if not REPORT_ID.fullmatch(report_id):
            raise BugCompassError("报告编号无效。")
        path = self.root / report_id
        if path.is_symlink() or not (path / "report.json").is_file():
            raise BugCompassError(f"找不到报告：{report_id}")
        return path

    def create(self, *, from_case: str | Path | None = None) -> str:
        draft = load_draft(from_case) if from_case is not None else blank_draft()
        self.root.mkdir(parents=True, exist_ok=True)
        for _ in range(5):
            report_id = f"report-{uuid4().hex[:12]}"
            directory = self.root / report_id
            try:
                directory.mkdir()
                break
            except FileExistsError:
                continue
        else:
            raise BugCompassError("无法生成新的报告编号。")
        try:
            write_json(directory / "report.json", {"schema_version": 1, "id": report_id, "created_at": utc_now()})
            save_draft(directory, draft)
        except (OSError, BugCompassError):
            for child in directory.iterdir():
                child.unlink()
            directory.rmdir()
            raise
        return report_id

    def list_reports(self) -> list[SavedReport]:
        if not self.root.is_dir():
            return []
        reports = []
        for directory in self.root.iterdir():
            if not REPORT_ID.fullmatch(directory.name) or directory.is_symlink():
                continue
            try:
                meta = json.loads((directory / "report.json").read_text(encoding="utf-8"))
                if meta.get("id") != directory.name:
                    continue
                draft = load_draft(directory)
                modified = (directory / "repro-report.json").stat().st_mtime
            except (OSError, UnicodeError, ValueError, AttributeError, BugCompassError):
                continue
            reports.append(SavedReport(directory.name, draft["title"] or "未命名报告", modified, review_draft(draft).ready_for_review))
        reports.sort(key=lambda item: item.updated_at, reverse=True)
        return reports

    def load(self, report_id: str) -> dict:
        return load_draft(self._directory(report_id))

    def save(self, report_id: str, draft: dict) -> ReportReview:
        save_draft(self._directory(report_id), draft)
        return review_draft(draft)

    def preview(self, report_id: str) -> tuple[str, str, ReportReview]:
        draft = self.load(report_id)
        return draft["title"], format_official_body(draft), review_draft(draft)

    def export(self, report_id: str, output_path: str | Path) -> PackageResult:
        draft = self.load(report_id)
        return export_draft_package(draft, output_path, report_id=report_id)
