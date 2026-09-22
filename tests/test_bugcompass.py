from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bugcompass.cases import create_case, show_case  # noqa: E402
from bugcompass.cli import main  # noqa: E402
from bugcompass.doctor import run_doctor  # noqa: E402
from bugcompass.workspace import BugCompassError, init_workspace  # noqa: E402


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class BugCompassTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "blender"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / "source" / "blender" / "blenkernel").mkdir(parents=True)
        (self.repo / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\n", encoding="utf-8")
        (self.repo / "README.md").write_text("temporary blender fixture\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(
            self.repo,
            "-c",
            "user.name=BugCompass Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        )
        self.workspace = self.root / "workspace"
        self.issue = self.root / "issue.md"
        self.issue.write_text("# 原始问题\n\n保持原样。\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_init_valid_workspace(self) -> None:
        workspace = init_workspace(self.workspace, self.repo)
        data = json.loads((self.workspace / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(workspace.repo_path, self.repo.resolve())
        self.assertEqual(data["repo_path"], str(self.repo.resolve()))
        self.assertEqual(data["project"], "blender")

    def test_init_rejects_missing_repository(self) -> None:
        with self.assertRaises(BugCompassError):
            init_workspace(self.workspace, self.root / "missing")

    def test_doctor_writes_structured_output(self) -> None:
        init_workspace(self.workspace, self.repo)
        result = run_doctor(self.workspace)
        saved = json.loads((self.workspace / "environment.json").read_text(encoding="utf-8"))
        self.assertIn(saved["overall_status"], {"ok", "warning", "error"})
        self.assertEqual(result["checks"]["head"]["value"]["commit"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(saved["checks"]["cmake"]["status"], "ok")
        self.assertIn("build_directories", saved["checks"])
        self.assertIn("blender_executables", saved["checks"])
        self.assertIn("binary_source_sync", saved["checks"])
        self.assertEqual(saved["checks"]["capabilities"]["value"]["investigate"], "ok")
        for check in saved["checks"].values():
            self.assertIn(check["status"], {"ok", "warning", "error"})

    def test_doctor_finds_build_binary_and_possible_staleness(self) -> None:
        init_workspace(self.workspace, self.repo)
        build = self.root / "build-blender"
        executable = build / "bin" / "blender"
        executable.parent.mkdir(parents=True)
        executable.write_text("fixture", encoding="utf-8")
        executable.chmod(0o755)
        (build / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={self.repo.resolve()}\n", encoding="utf-8"
        )
        os.utime(executable, (1, 1))
        saved = run_doctor(self.workspace)
        self.assertIn(str(build.resolve()), saved["checks"]["build_directories"]["value"]["found"])
        self.assertIn(str(executable.resolve()), saved["checks"]["blender_executables"]["value"]["found"])
        self.assertEqual(saved["checks"]["binary_source_sync"]["value"]["state"], "possibly_stale")

    def test_create_case_preserves_input_and_creates_files(self) -> None:
        init_workspace(self.workspace, self.repo)
        metadata = create_case(self.workspace, "demo-001", self.issue)
        case_dir = self.workspace / "cases" / "demo-001"
        self.assertEqual(metadata["status"], "new")
        self.assertEqual((case_dir / "issue-original.md").read_bytes(), self.issue.read_bytes())
        expected = {"case.json", "investigation.json", "issue-original.md", "intake.md", "hypotheses.md", "evidence.md", "report.md"}
        self.assertEqual({path.name for path in case_dir.iterdir()}, expected)
        investigation = json.loads((case_dir / "investigation.json").read_text(encoding="utf-8"))
        self.assertEqual(investigation["case_id"], "demo-001")
        self.assertEqual(investigation["hypotheses"], [])

    def test_create_case_rejects_duplicate(self) -> None:
        init_workspace(self.workspace, self.repo)
        create_case(self.workspace, "demo-001", self.issue)
        with self.assertRaises(BugCompassError):
            create_case(self.workspace, "demo-001", self.issue)

    def test_create_case_rejects_path_traversal(self) -> None:
        init_workspace(self.workspace, self.repo)
        with self.assertRaises(BugCompassError):
            create_case(self.workspace, "../escape", self.issue)
        self.assertFalse((self.workspace.parent / "escape").exists())

    def test_show_reads_case_and_cli_prints_paths(self) -> None:
        init_workspace(self.workspace, self.repo)
        create_case(self.workspace, "demo-001", self.issue)
        metadata, paths = show_case(self.workspace, "demo-001")
        self.assertEqual(metadata["id"], "demo-001")
        self.assertTrue(paths["report.md"].is_file())
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["case", "show", "--workspace", str(self.workspace), "--id", "demo-001"])
        self.assertEqual(exit_code, 0)
        self.assertIn("状态：new", output.getvalue())
        self.assertIn("hypotheses.md", output.getvalue())

    def test_help_uses_chinese_labels(self) -> None:
        output = StringIO()
        with self.assertRaises(SystemExit) as stopped, redirect_stdout(output):
            main(["--help"])
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("用法：", output.getvalue())
        self.assertIn("选项", output.getvalue())
        self.assertIn("显示帮助并退出", output.getvalue())


if __name__ == "__main__":
    unittest.main()
