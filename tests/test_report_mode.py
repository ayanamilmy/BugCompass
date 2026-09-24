from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bugcompass.report_mode import ReportStore  # noqa: E402
from bugcompass.repro_report import format_official_body  # noqa: E402
from bugcompass.workspace import BugCompassError  # noqa: E402


class ReportModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.store = ReportStore(self.root / "reports")

    def test_create_and_resume_without_git_or_investigation(self) -> None:
        report_id = self.store.create()
        self.assertEqual(self.store.load(report_id)["title"], "")
        self.assertFalse((self.root / "reports" / report_id / "case.json").exists())
        draft = self.store.load(report_id)
        draft["title"] = "移动物体后崩溃"
        draft["steps"] = "1. 启动 Blender\n2. 移动物体"
        self.store.save(report_id, draft)
        resumed = ReportStore(self.root / "reports")
        self.assertEqual(resumed.load(report_id)["steps"], draft["steps"])
        self.assertEqual(resumed.list_reports()[0].title, "移动物体后崩溃")
        self.assertFalse(resumed.list_reports()[0].ready_for_review)

    def test_official_form_text_and_package_use_selected_files_only(self) -> None:
        report_id = self.store.create()
        draft = self.store.load(report_id)
        draft.update({
            "title": "移动物体后崩溃",
            "broken_version": "5.0.1, hash abc123",
            "working_version": "4.5.4",
            "latest_tested_version": "5.0.1",
            "system_info": "Windows 11；RTX 4060；驱动 580",
            "steps": "1. 打开默认场景\n2. 移动立方体",
            "expected": "物体移动",
            "actual": "程序退出",
            "reproduced": True,
            "factory_startup": True,
            "tested_latest": True,
            "duplicate_checked": True,
        })
        attachment = self.root / "crash.txt"
        attachment.write_text("trace", encoding="utf-8")
        private = self.root / "private.txt"
        private.write_text("secret", encoding="utf-8")
        draft["attachments"] = [str(attachment)]
        review = self.store.save(report_id, draft)
        self.assertTrue(review.ready_for_review)
        title, body, _ = self.store.preview(report_id)
        self.assertEqual(title, draft["title"])
        self.assertEqual(body, format_official_body(draft))
        for heading in ("**System Information**", "**Blender Version**", "**Short description of error**", "**Exact steps for others to reproduce the error**"):
            self.assertIn(heading, body)
        self.assertNotIn("private.txt", body)
        self.assertNotIn("# 移动物体后崩溃", body)  # 标题单独粘贴到标题栏

        output = self.root / "report.zip"
        self.store.export(report_id, output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"报告正文.md", "发布前检查清单.md", "manifest.json", "附件/01-crash.txt"})
            self.assertEqual(archive.read("报告正文.md").decode("utf-8"), body)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["title"], title)
            self.assertEqual(manifest["report_id"], report_id)
            self.assertNotIn("case_id", manifest)
            self.assertTrue(manifest["ready_for_review"])

    def test_import_existing_case_as_independent_draft(self) -> None:
        case = self.root / "old-case"
        case.mkdir()
        (case / "case.json").write_text('{"id":"old-case"}', encoding="utf-8")
        (case / "issue-original.md").write_text("# 旧报告标题\n原始描述", encoding="utf-8")
        (case / "investigation.json").write_text(json.dumps({"summary": {"reproduction_steps": ["打开文件"]}}), encoding="utf-8")
        report_id = self.store.create(from_case=case)
        draft = self.store.load(report_id)
        self.assertEqual(draft["title"], "旧报告标题")
        self.assertIn("打开文件", draft["steps"])
        self.assertFalse(draft["reproduced"])
        self.assertFalse((self.store.root / report_id / "case.json").exists())

    def test_unverified_latest_version_is_called_out(self) -> None:
        report_id = self.store.create()
        draft = self.store.load(report_id)
        draft["tested_latest"] = True
        review = self.store.save(report_id, draft)
        self.assertIn("已复测的最新 Blender 完整版本", review.missing_required)

    def test_bad_report_id_cannot_escape_store(self) -> None:
        with self.assertRaisesRegex(BugCompassError, "编号无效"):
            self.store.load("../other")

    def test_moved_attachment_is_not_marked_ready(self) -> None:
        report_id = self.store.create()
        draft = self.store.load(report_id)
        sample = self.root / "example.blend"
        sample.write_bytes(b"BLENDER")
        draft.update({
            "title": "场景显示异常", "broken_version": "5.0.1", "system_info": "macOS；Apple GPU",
            "steps": "1. 打开附件", "expected": "物体可见", "actual": "物体消失", "reproduced": True,
            "attachments": [str(sample)],
        })
        self.assertTrue(self.store.save(report_id, draft).ready_for_review)
        sample.unlink()
        review = self.store.preview(report_id)[2]
        self.assertFalse(review.ready_for_review)
        self.assertTrue(any("example.blend" in item for item in review.missing_required))
        self.assertIn("简化的 .blend 文件，或确认可从默认场景复现", review.missing_required)


if __name__ == "__main__":
    unittest.main()
