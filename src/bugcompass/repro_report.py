"""按 Blender 报告与分诊要求整理可人工核对的报告包。"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import BugCompassError, utc_now, write_json


DRAFT_NAME = "repro-report.json"
REPORT_GUIDE = "https://developer.blender.org/docs/handbook/bug_reports/making_good_bug_reports/"
TRIAGE_GUIDE = "https://developer.blender.org/docs/handbook/bug_reports/help_triaging_bugs/"
TRIAGE_PLAYBOOK = "https://developer.blender.org/docs/handbook/bug_reports/triaging_playbook/"
TEXT_FIELDS = ("title", "broken_version", "working_version", "steps", "expected", "actual", "system_info")
BOOL_FIELDS = ("reproduced", "factory_startup", "tested_latest", "duplicate_checked", "simplified_file", "crash")


@dataclass(frozen=True)
class ReportReview:
    missing_required: tuple[str, ...]
    suggestions: tuple[str, ...]

    @property
    def ready_for_review(self) -> bool:
        """表示必填材料齐全，不代表 Bug 已由第三方确认。"""
        return not self.missing_required


@dataclass(frozen=True)
class PackageResult:
    path: Path
    review: ReportReview


def _case_dir(case_dir: str | Path) -> Path:
    path = Path(case_dir).expanduser().resolve()
    if not (path / "case.json").is_file():
        raise BugCompassError(f"找不到 Case：{path}")
    return path


def _default_draft(case_dir: Path) -> dict[str, Any]:
    draft: dict[str, Any] = {"schema_version": 1, **{name: "" for name in TEXT_FIELDS}, **{name: False for name in BOOL_FIELDS}, "attachments": []}
    source = case_dir / "issue-original.md"
    try:
        for line in source.read_text(encoding="utf-8").splitlines():
            if line.startswith("# ") and line[2:].strip():
                draft["title"] = line[2:].strip()
                break
    except (OSError, UnicodeError):
        pass
    try:
        investigation = json.loads((case_dir / "investigation.json").read_text(encoding="utf-8"))
        summary = investigation.get("summary", {}) if isinstance(investigation, dict) else {}
        if isinstance(summary, dict):
            for field, source_field in (("expected", "expected_behavior"), ("actual", "actual_behavior")):
                value = summary.get(source_field)
                if isinstance(value, str):
                    draft[field] = value
            steps = summary.get("reproduction_steps")
            if isinstance(steps, list):
                draft["steps"] = "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1) if isinstance(step, str))
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return draft


def validate_draft(draft: Any) -> dict[str, Any]:
    if not isinstance(draft, dict) or draft.get("schema_version") != 1:
        raise BugCompassError("可复现报告草稿格式无效。")
    clean: dict[str, Any] = {"schema_version": 1}
    for name in TEXT_FIELDS:
        value = draft.get(name, "")
        if not isinstance(value, str):
            raise BugCompassError(f"报告字段 {name} 必须是文字。")
        clean[name] = value.strip()
    for name in BOOL_FIELDS:
        value = draft.get(name, False)
        if not isinstance(value, bool):
            raise BugCompassError(f"报告字段 {name} 必须是勾选状态。")
        clean[name] = value
    attachments = draft.get("attachments", [])
    if not isinstance(attachments, list) or not all(isinstance(item, str) for item in attachments):
        raise BugCompassError("报告附件列表格式无效。")
    clean["attachments"] = list(dict.fromkeys(attachments))
    return clean


def load_draft(case_dir: str | Path) -> dict[str, Any]:
    case = _case_dir(case_dir)
    path = case / DRAFT_NAME
    if not path.exists():
        return _default_draft(case)
    try:
        return validate_draft(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BugCompassError(f"无法读取可复现报告草稿：{exc}") from exc


def save_draft(case_dir: str | Path, draft: dict[str, Any]) -> None:
    case = _case_dir(case_dir)
    try:
        write_json(case / DRAFT_NAME, validate_draft(draft))
    except OSError as exc:
        raise BugCompassError(f"无法保存可复现报告草稿：{exc}") from exc


def review_draft(draft: dict[str, Any]) -> ReportReview:
    data = validate_draft(draft)
    missing = []
    for field, label in (
        ("title", "简明标题"),
        ("broken_version", "出现问题的 Blender 版本"),
        ("steps", "逐步复现操作"),
        ("expected", "预期行为"),
        ("actual", "实际行为"),
    ):
        if not data[field]:
            missing.append(label)
    filenames = [Path(value).name.casefold() for value in data["attachments"]]
    if not data["system_info"] and "system-info.txt" not in filenames:
        missing.append("系统、显卡与驱动信息，或 system-info.txt 附件")
    if not data["factory_startup"] and not any(name.endswith(".blend") for name in filenames):
        missing.append("简化的 .blend 文件，或确认可从默认场景复现")
    if not data["reproduced"]:
        missing.append("按所列步骤再次复现并确认结果")

    suggestions = []
    if not data["working_version"]:
        suggestions.append("若已知，请补充最后正常工作的 Blender 版本。")
    if not data["tested_latest"]:
        suggestions.append("建议在最新稳定版或开发版复测，并写明测试版本。")
    if not data["duplicate_checked"]:
        suggestions.append("发布前请搜索已有的开放与已关闭报告，检查是否重复。")
    if any(name.endswith(".blend") for name in filenames) and not data["simplified_file"]:
        suggestions.append("请删减 .blend 文件中的无关内容，尽量使打开后只需零到一步操作。")
    if data["crash"] and not any("crash" in name or "backtrace" in name for name in filenames):
        suggestions.append("崩溃问题建议附上崩溃日志或回溯；日志作为附件，不要粘贴长篇正文。")
    suggestions.append("公开前请检查 .blend、图片和日志是否包含私人或项目敏感内容。")
    return ReportReview(tuple(missing), tuple(suggestions))


def _attachment_files(draft: dict[str, Any]) -> list[tuple[Path, str]]:
    files = []
    for index, raw in enumerate(draft["attachments"], 1):
        source = Path(raw).expanduser()
        if source.is_symlink() or not source.is_file():
            raise BugCompassError(f"附件不存在、不可读取或是符号链接：{source}")
        name = re.sub(r'[\x00-\x1f\x7f\\/:*?"<>|]', "_", source.name)
        files.append((source, f"附件/{index:02d}-{name}"))
    return files


def _report_markdown(draft: dict[str, Any], files: list[tuple[Path, str]]) -> str:
    def value(name: str) -> str:
        return draft[name] or "（待补充）"

    attachment_list = "\n".join(f"- {name}" for _, name in files) or "- （尚无附件）"
    has_system_info_file = any(Path(name).name.casefold().endswith("system-info.txt") for _, name in files)
    system_info = draft["system_info"] or ("详见附件中的 system-info.txt。" if has_system_info_file else "（待补充）")
    working = draft["working_version"] or "未知，尚未确认是否为回归"
    reproduction = "已由报告者按所列步骤复现" if draft["reproduced"] else "尚未由报告者再次确认复现"
    startup = "可从默认场景复现" if draft["factory_startup"] else "请使用附件中的简化 .blend 文件（如有）"
    return (
        f"# {value('title')}\n\n"
        f"## Blender 版本\n\n出现问题：{value('broken_version')}\n\n最后正常：{working}\n\n"
        f"## 系统信息\n\n{system_info}\n\n"
        f"## 复现步骤\n\n{value('steps')}\n\n"
        f"复现状态：{reproduction}。{startup}。\n\n"
        f"## 预期行为\n\n{value('expected')}\n\n"
        f"## 实际行为\n\n{value('actual')}\n\n"
        f"## 附件\n\n{attachment_list}\n"
    )


def _checklist_markdown(review: ReportReview) -> str:
    required = "\n".join(f"- [ ] {item}" for item in review.missing_required) or "- [x] 必填材料已填写；仍需人工核对真实性与可复现性。"
    suggestions = "\n".join(f"- [ ] {item}" for item in review.suggestions)
    return (
        "# 发布前检查清单\n\n"
        "此清单仅根据用户填写内容检查缺项，不会运行 Blender，也不代表 Blender 分诊团队已确认 Bug。\n\n"
        f"## 尚缺材料\n\n{required}\n\n"
        f"## 建议核对\n\n{suggestions}\n\n"
        "请先人工检查报告与附件。将 `报告正文.md` 复制到 Blender 的问题提交表单，"
        "并单独上传需要公开的附件；不要把整个 ZIP 直接粘贴进正文。\n\n"
        f"官方参考：\n- {REPORT_GUIDE}\n- {TRIAGE_GUIDE}\n- {TRIAGE_PLAYBOOK}\n"
    )


def export_package(case_dir: str | Path, output_path: str | Path, draft: dict[str, Any] | None = None) -> PackageResult:
    """只打包所选附件；不运行附件，也不读取或打包 Blender 源码。"""
    case = _case_dir(case_dir)
    data = validate_draft(draft) if draft is not None else load_draft(case)
    review = review_draft(data)
    files = _attachment_files(data)
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise BugCompassError(f"报告包已存在，不会覆盖：{output}")
    manifest = {
        "schema_version": 1,
        "case_id": case.name,
        "generated_at": utc_now(),
        "ready_for_review": review.ready_for_review,
        "missing_required": review.missing_required,
        "attachments": [name for _, name in files],
    }
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
                archive.writestr("报告正文.md", _report_markdown(data, files))
                archive.writestr("发布前检查清单.md", _checklist_markdown(review))
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                for source, name in files:
                    archive.write(source, arcname=name)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if created:
            output.unlink(missing_ok=True)
        raise BugCompassError(f"无法生成可复现报告包：{exc}") from exc
    return PackageResult(output, review)
