from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bugcompass.search import SEARCH_FILES, format_report, search_workspace  # noqa: E402


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tempdir.name)
        self.case_a = self.workspace / "cases" / "case-a"
        self.case_b = self.workspace / "cases" / "case-b"
        self.case_a.mkdir(parents=True)
        self.case_b.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write(self, case_dir: Path, filename: str, content: str) -> None:
        (case_dir / filename).write_text(content, encoding="utf-8")

    def test_basic_hits_are_grouped_and_contain_source_location(self) -> None:
        self.write(self.case_a, "issue-original.md", "无关内容\nBlender 闪退\n")
        self.write(self.case_b, "report.md", "闪退已修复\n")
        report = search_workspace(self.workspace, "闪退")
        self.assertEqual(list(report["cases"]), ["case-a", "case-b"])
        self.assertEqual(
            report["cases"]["case-a"],
            [{"case_id": "case-a", "filename": "issue-original.md", "line_number": 2, "line": "Blender 闪退"}],
        )
        self.assertEqual(report["cases"]["case-b"][0]["filename"], "report.md")
        self.assertEqual(report["warnings"], [])

    def test_searches_every_named_file_and_ignores_others(self) -> None:
        for filename in SEARCH_FILES:
            content = json.dumps({"text": "目标词"}, ensure_ascii=False) if filename.endswith(".json") else "目标词\n"
            self.write(self.case_a, filename, content)
        self.write(self.case_a, "other.md", "目标词\n")
        report = search_workspace(self.workspace, "目标词")
        self.assertEqual({hit["filename"] for hit in report["cases"]["case-a"]}, set(SEARCH_FILES))

    def test_multiple_terms_must_match_the_same_line(self) -> None:
        self.write(self.case_a, "intake.md", "红色\n蓝色\n红色 和 蓝色\n")
        hits = search_workspace(self.workspace, "红色 蓝色")["cases"]["case-a"]
        self.assertEqual([(hit["line_number"], hit["line"]) for hit in hits], [(3, "红色 和 蓝色")])

    def test_case_insensitive_matching(self) -> None:
        self.write(self.case_a, "evidence.md", "BLENDER Render Failure\n")
        hits = search_workspace(self.workspace, "blender failure")["cases"]["case-a"]
        self.assertEqual(hits[0]["line"], "BLENDER Render Failure")

    def test_regex_matching_and_invalid_pattern(self) -> None:
        self.write(self.case_a, "hypotheses.md", "Error 42\nError abc\n")
        hits = search_workspace(self.workspace, r"error\s+\d+", regex=True)["cases"]["case-a"]
        self.assertEqual([hit["line_number"] for hit in hits], [1])
        with self.assertRaisesRegex(ValueError, "正则表达式无效"):
            search_workspace(self.workspace, "[", regex=True)

    def test_limit_is_per_case(self) -> None:
        self.write(self.case_a, "issue-original.md", "目标\n目标\n目标\n")
        self.write(self.case_b, "report.md", "目标\n目标\n")
        report = search_workspace(self.workspace, "目标", limit_per_case=2)
        self.assertEqual({case_id: len(hits) for case_id, hits in report["cases"].items()}, {"case-a": 2, "case-b": 2})
        with self.assertRaisesRegex(ValueError, "结果上限"):
            search_workspace(self.workspace, "目标", limit_per_case=0)

    def test_bad_json_is_skipped_and_other_files_continue(self) -> None:
        self.write(self.case_a, "case.json", '{"text": "目标"')
        self.write(self.case_a, "investigation.json", '{"text": "目标"}')
        self.write(self.case_a, "report.md", "目标\n")
        report = search_workspace(self.workspace, "目标")
        self.assertEqual([hit["filename"] for hit in report["cases"]["case-a"]], ["investigation.json", "report.md"])
        self.assertEqual(report["warnings"][0]["case_id"], "case-a")
        self.assertEqual(report["warnings"][0]["filename"], "case.json")
        self.assertIn("case-a/case.json：JSON 格式无效", format_report(report))

    def test_unreadable_text_is_skipped_and_warned(self) -> None:
        (self.case_a / "intake.md").write_bytes(b"\xff")
        self.write(self.case_a, "report.md", "目标\n")
        report = search_workspace(self.workspace, "目标")
        self.assertEqual(len(report["cases"]["case-a"]), 1)
        self.assertEqual(report["warnings"][0]["filename"], "intake.md")

    def test_empty_result_and_format_report(self) -> None:
        self.write(self.case_a, "report.md", "没有命中\n")
        report = search_workspace(self.workspace, "目标")
        self.assertEqual(report["cases"], {})
        self.assertIn("未找到匹配结果", format_report(report))
        self.write(self.case_a, "report.md", "目标在这里\n")
        text = format_report(search_workspace(self.workspace, "目标"))
        self.assertIn("Case：case-a", text)
        self.assertIn("report.md:1: 目标在这里", text)
        self.assertEqual(search_workspace(self.workspace / "missing", "目标")["cases"], {})

    def test_module_entrypoint(self) -> None:
        self.write(self.case_a, "report.md", "目标在这里\n")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        result = subprocess.run(
            [sys.executable, "-m", "bugcompass.search", "--workspace", str(self.workspace), "目标"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("report.md:1: 目标在这里", result.stdout)


if __name__ == "__main__":
    unittest.main()
