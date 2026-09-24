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
TEXT_FIELDS = ("title", "broken_version", "working_version", "latest_tested_version", "steps", "expected", "actual", "system_info")
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


def _report_dir(case_dir: str | Path) -> Path:
    path = Path(case_dir).expanduser().resolve()
    if not (path / "case.json").is_file() and not (path / "report.json").is_file():
        raise BugCompassError(f"找不到报告或 Case：{path}")
    return path


def blank_draft() -> dict[str, Any]:
    """创建不依赖源码、调查或网络的空报告。"""
    return {"schema_version": 1, **{name: "" for name in TEXT_FIELDS}, **{name: False for name in BOOL_FIELDS}, "attachments": []}


def _default_draft(case_dir: Path) -> dict[str, Any]:
    draft = blank_draft()
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
    case = _report_dir(case_dir)
    path = case / DRAFT_NAME
    if not path.exists():
        return _default_draft(case)
    try:
        return validate_draft(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BugCompassError(f"无法读取可复现报告草稿：{exc}") from exc


def save_draft(case_dir: str | Path, draft: dict[str, Any]) -> None:
    case = _report_dir(case_dir)
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
    filenames = []
    invalid_files = []
    for value in data["attachments"]:
        source = Path(value).expanduser()
        try:
            if source.is_symlink() or not source.is_file():
                raise OSError("附件不可用")
            with source.open("rb") as stream:
                stream.read(1)
        except OSError:
            invalid_files.append(source.name or "未命名附件")
        else:
            filenames.append(source.name.casefold())
    if invalid_files:
        missing.append("所选附件不存在或无法读取：" + "、".join(invalid_files))
    if not data["system_info"] and "system-info.txt" not in filenames:
        missing.append("系统、显卡与驱动信息，或 system-info.txt 附件")
    if not data["factory_startup"] and not any(name.endswith(".blend") for name in filenames):
        missing.append("简化的 .blend 文件，或确认可从默认场景复现")
    if not data["reproduced"]:
        missing.append("按所列步骤再次复现并确认结果")
    if data["tested_latest"] and not data["latest_tested_version"]:
        missing.append("已复测的最新 Blender 完整版本")

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


def format_official_body(draft: dict[str, Any]) -> str:
    """按 Blender Bug Report 表单的正文栏目生成可直接复制的文本；标题单独填写。"""
    data = validate_draft(draft)
    def value(name: str) -> str:
        return data[name] or "（待补充）"

    filenames = [Path(name).name.casefold() for name in data["attachments"]]
    has_system_info_file = "system-info.txt" in filenames
    system_info = data["system_info"] or ("详见附件中的 system-info.txt（请在官方表单单独上传）。" if has_system_info_file else "（待补充）")
    working = data["working_version"] or "未知"
    latest = f"\n最新版本复测：{data['latest_tested_version']}" if data["latest_tested_version"] else ""
    startup = "从默认场景开始。" if data["factory_startup"] else "打开单独上传的简化 .blend 文件（如适用）。"
    return (
        f"**System Information**\n{system_info}\n\n"
        f"**Blender Version**\nBroken: {value('broken_version')}\nWorked: {working}{latest}\n\n"
        f"**Short description of error**\n实际行为：{value('actual')}\n预期行为：{value('expected')}\n\n"
        f"**Exact steps for others to reproduce the error**\n{startup}\n{value('steps')}\n"
    )


def _checklist_markdown(review: ReportReview) -> str:
    required = "\n".join(f"- [ ] {item}" for item in review.missing_required) or "- [x] 必填材料已填写；仍需人工核对真实性与可复现性。"
    suggestions = "\n".join(f"- [ ] {item}" for item in review.suggestions)
    return (
        "# 发布前检查清单\n\n"
        "此清单仅根据用户填写内容检查缺项，不会运行 Blender，也不代表 Blender 分诊团队已确认 Bug。\n\n"
        f"## 尚缺材料\n\n{required}\n\n"
        f"## 建议核对\n\n{suggestions}\n\n"
        "请先人工检查报告与附件。将报告标题填写到官方标题栏，将 `报告正文.md` 复制到描述栏，"
        "并单独上传需要公开的附件；不要把整个 ZIP 直接粘贴进正文。\n\n"
        f"官方参考：\n- {REPORT_GUIDE}\n- {TRIAGE_GUIDE}\n- {TRIAGE_PLAYBOOK}\n"
    )


def export_draft_package(draft: dict[str, Any], output_path: str | Path, *, report_id: str = "", case_id: str = "") -> PackageResult:
    """只打包所选附件；不运行附件，也不读取或打包 Blender 源码。"""
    data = validate_draft(draft)
    review = review_draft(data)
    files = _attachment_files(data)
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise BugCompassError(f"报告包已存在，不会覆盖：{output}")
    manifest = {
        "schema_version": 1,
        "title": data["title"],
        "generated_at": utc_now(),
        "ready_for_review": review.ready_for_review,
        "missing_required": review.missing_required,
        "attachments": [name for _, name in files],
    }
    if report_id:
        manifest["report_id"] = report_id
    if case_id:
        manifest["case_id"] = case_id
    created = False
    try:
        with output.open("xb") as stream:
            created = True
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
                archive.writestr("报告正文.md", format_official_body(data))
                archive.writestr("发布前检查清单.md", _checklist_markdown(review))
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                for source, name in files:
                    archive.write(source, arcname=name)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if created:
            output.unlink(missing_ok=True)
        raise BugCompassError(f"无法生成可复现报告包：{exc}") from exc
    return PackageResult(output, review)


def export_package(case_dir: str | Path, output_path: str | Path, draft: dict[str, Any] | None = None) -> PackageResult:
    case = _report_dir(case_dir)
    data = validate_draft(draft) if draft is not None else load_draft(case)
    return export_draft_package(data, output_path, case_id=case.name if (case / "case.json").is_file() else "", report_id=case.name if (case / "report.json").is_file() else "")
