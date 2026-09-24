from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bugcompass.repro_report import export_package, load_draft, review_draft, save_draft  # noqa: E402
from bugcompass.workspace import BugCompassError  # noqa: E402


class ReproReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.case = self.root / "case-a"
        self.case.mkdir()
        (self.case / "case.json").write_text('{"id": "case-a"}', encoding="utf-8")
        (self.case / "issue-original.md").write_text("# 物体消失\n原始报告内容", encoding="utf-8")
        (self.case / "investigation.json").write_text(
            json.dumps({"summary": {"expected_behavior": "物体可见", "actual_behavior": "物体消失", "reproduction_steps": ["打开文件", "切换模式"]}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def complete_draft(self) -> dict:
        draft = load_draft(self.case)
        draft.update({
            "broken_version": "4.5.1",
            "working_version": "4.4.3",
            "latest_tested_version": "4.5.1",
            "system_info": "Windows 11；NVIDIA GPU；驱动 555",
            "reproduced": True,
            "tested_latest": True,
            "duplicate_checked": True,
            "simplified_file": True,
        })
        sample = self.root / "小场景.blend"
        sample.write_bytes(b"BLENDER-file-fixture")
        draft["attachments"] = [str(sample)]
        return draft

    def test_prefills_from_case_without_claiming_a_version(self) -> None:
        draft = load_draft(self.case)
        self.assertEqual(draft["title"], "物体消失")
        self.assertEqual(draft["steps"], "1. 打开文件\n2. 切换模式")
        self.assertEqual(draft["expected"], "物体可见")
        self.assertEqual(draft["actual"], "物体消失")
        self.assertEqual(draft["broken_version"], "")
        self.assertFalse(draft["reproduced"])

    def test_unusable_investigation_does_not_block_manual_report(self) -> None:
        (self.case / "investigation.json").write_text("[]", encoding="utf-8")
        draft = load_draft(self.case)
        self.assertEqual(draft["title"], "物体消失")
        self.assertEqual(draft["steps"], "")

    def test_missing_materials_are_explicit_and_export_is_still_a_draft(self) -> None:
        draft = load_draft(self.case)
        review = review_draft(draft)
        self.assertFalse(review.ready_for_review)
        self.assertIn("出现问题的 Blender 版本", review.missing_required)
        self.assertIn("简化的 .blend 文件，或确认可从默认场景复现", review.missing_required)
        self.assertTrue(any("重复" in suggestion for suggestion in review.suggestions))

        output = self.root / "draft.zip"
        result = export_package(self.case, output, draft)
        self.assertFalse(result.review.ready_for_review)
        with zipfile.ZipFile(output) as archive:
            checklist = archive.read("发布前检查清单.md").decode("utf-8")
            report = archive.read("报告正文.md").decode("utf-8")
        self.assertIn("出现问题的 Blender 版本", checklist)
        self.assertIn("不代表 Blender 分诊团队已确认", checklist)
        self.assertIn("（待补充）", report)

    def test_save_load_and_export_only_selected_public_files(self) -> None:
        draft = self.complete_draft()
        private = self.case / "private-notes.txt"
        private.write_text("不要公开", encoding="utf-8")
        save_draft(self.case, draft)
        self.assertEqual(load_draft(self.case), draft)
        output = self.root / "report.zip"
        result = export_package(self.case, output)
        self.assertTrue(result.review.ready_for_review)
        with zipfile.ZipFile(output) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {"报告正文.md", "发布前检查清单.md", "manifest.json", "附件/01-小场景.blend"})
            self.assertEqual(archive.read("附件/01-小场景.blend"), b"BLENDER-file-fixture")
            report = archive.read("报告正文.md").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json"))
        self.assertIn("4.5.1", report)
        self.assertIn("1. 打开文件", report)
        self.assertNotIn(str(self.root), report)
        self.assertTrue(manifest["ready_for_review"])
        self.assertNotIn("private-notes.txt", names)

    def test_system_info_attachment_and_factory_startup_satisfy_checklist(self) -> None:
        draft = self.complete_draft()
        draft["system_info"] = ""
        draft["factory_startup"] = True
        system_info = self.root / "system-info.txt"
        system_info.write_text("OS and GPU", encoding="utf-8")
        draft["attachments"] = [str(system_info)]
        self.assertTrue(review_draft(draft).ready_for_review)
        output = self.root / "factory.zip"
        export_package(self.case, output, draft)
        with zipfile.ZipFile(output) as archive:
            report = archive.read("报告正文.md").decode("utf-8")
        self.assertIn("详见附件中的 system-info.txt", report)

    def test_crash_log_is_advice_not_a_false_requirement(self) -> None:
        draft = self.complete_draft()
        draft["crash"] = True
        review = review_draft(draft)
        self.assertTrue(review.ready_for_review)
        self.assertTrue(any("崩溃日志" in suggestion for suggestion in review.suggestions))

    def test_rejects_missing_or_symlinked_attachments_and_existing_output(self) -> None:
        draft = self.complete_draft()
        draft["attachments"] = [str(self.root / "missing.blend")]
        with self.assertRaisesRegex(BugCompassError, "附件"):
            export_package(self.case, self.root / "missing.zip", draft)
        self.assertFalse((self.root / "missing.zip").exists())

        link = self.root / "link.blend"
        link.symlink_to(self.root / "小场景.blend")
        draft["attachments"] = [str(link)]
        with self.assertRaisesRegex(BugCompassError, "符号链接"):
            export_package(self.case, self.root / "link.zip", draft)

        output = self.root / "existing.zip"
        output.write_bytes(b"keep")
        draft["attachments"] = []
        with self.assertRaisesRegex(BugCompassError, "不会覆盖"):
            export_package(self.case, output, draft)
        self.assertEqual(output.read_bytes(), b"keep")

    def test_invalid_saved_draft_is_reported(self) -> None:
        (self.case / "repro-report.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(BugCompassError, "草稿"):
            load_draft(self.case)


if __name__ == "__main__":
    unittest.main()
