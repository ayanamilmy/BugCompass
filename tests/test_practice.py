from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bugcompass.investigation import write_investigation  # noqa: E402
from bugcompass.practice import PracticeManager  # noqa: E402
from bugcompass.workspace import init_workspace  # noqa: E402


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


class PracticeWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "blender"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        source = self.repo / "source" / "blender" / "nodes"
        source.mkdir(parents=True)
        (self.repo / "CMakeLists.txt").write_text("project(Blender)\n", encoding="utf-8")
        (source / "demo.cc").write_text("int broken = 1;\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Practice Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "before")
        self.pre_fix = git(self.repo, "rev-parse", "HEAD")
        (source / "demo.cc").write_text("int fixed = 1;\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Practice Test", "-c", "user.email=test@example.invalid", "commit", "-q", "-m", "fix")
        self.fix = git(self.repo, "rev-parse", "HEAD")
        self.original_head = self.fix
        self.workspace = self.root / "workspace"
        init_workspace(self.workspace, self.repo)
        pack = self.root / "pack"
        pack.mkdir()
        (pack / "catalog.json").write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "practice-demo",
                            "issue_id": 123,
                            "title": "演示问题",
                            "difficulty": "easy",
                            "reported_symptom": "某个节点行为错误。",
                            "reproduction_outline": ["创建节点", "观察错误"],
                            "pre_fix_commit": self.pre_fix,
                            "suggested_minutes": 10,
                            "suggested_ai_runs": 2,
                            "tags": ["nodes"],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (pack / "answers.json").write_text(
            json.dumps(
                {
                    "answers": [
                        {
                            "case_id": "practice-demo",
                            "fix_commit": self.fix,
                            "pull_request": 999,
                            "true_subsystems": ["Nodes"],
                            "root_cause": "旧代码使用了错误状态。",
                            "relevant_files": ["source/blender/nodes/demo.cc"],
                            "relevant_symbols": ["broken"],
                            "fix_summary": "替换错误状态。",
                            "revealing_experiment": "读取 demo.cc。",
                            "misleading_paths": [],
                            "evidence": ["修复修改 demo.cc"],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.manager = PracticeManager(ROOT, self.workspace)
        self.manager.pack_dir = pack

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_session_uses_isolated_pre_fix_checkout_and_hides_answer(self) -> None:
        case_id, checkout = self.manager.start_session("practice-demo")
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), self.pre_fix)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.original_head)
        self.assertFalse((checkout / "answers.json").exists())
        metadata = json.loads((self.workspace / "cases" / case_id / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["mode"], "historical_practice")
        self.assertEqual(metadata["repo_commit"], self.pre_fix)
        session = self.manager.session_for_case(case_id)
        self.assertEqual(Path(session["checkout_path"]), checkout)
        self.assertEqual(session["ai_runs"], 0)

    def test_submit_then_reveal_scores_and_records_resources(self) -> None:
        case_id, _checkout = self.manager.start_session("practice-demo")
        case_dir = self.workspace / "cases" / case_id
        investigation = json.loads((case_dir / "investigation.json").read_text(encoding="utf-8"))
        investigation["hypotheses"] = [
            {
                "id": f"H{index}",
                "title": "节点状态问题",
                "priority": "high" if index == 1 else "medium",
                "status": "active",
                "claim": "节点文件包含错误状态。",
                "basis": ["源码"],
                "evidence_ids": ["E1"],
                "source_references": [{"path": "source/blender/nodes/demo.cc", "line": 1, "symbol": "broken", "note": "错误"}],
                "next_step": "读取文件",
                "supporting_result": "存在 broken",
                "weakening_result": "不存在 broken",
                "risk": "低",
                "estimated_cost": "low",
                "user_note": "",
            }
            for index in range(1, 4)
        ]
        investigation["evidence"] = [
            {
                "id": "E1",
                "kind": "fact",
                "statement": "demo.cc 存在 broken。",
                "source_type": "source",
                "source_references": [{"path": "source/blender/nodes/demo.cc", "line": 1, "symbol": "broken", "note": "错误"}],
            }
        ]
        investigation["unknowns"] = [{"id": "U1", "question": "为何进入该路径？", "impact": "影响根因"}]
        investigation["suggested_experiments"] = [{"id": "A1", "hypothesis_id": "H1", "command": ["rg", "broken"], "permission": "green"}]
        write_investigation(case_dir / "investigation.json", investigation, require_complete=True)
        self.manager.increment_ai_runs(case_id)
        self.manager.submit_judgment(case_id, "我认为 demo.cc 使用了错误状态。")
        reveal = self.manager.reveal(case_id)
        self.assertEqual(reveal.quality_total, 10)
        self.assertEqual(reveal.ai_runs, 1)
        self.assertEqual(reveal.answer["fix_commit"], self.fix)
        session = self.manager.session_for_case(case_id)
        self.assertEqual(session["status"], "revealed")
        self.assertTrue((self.workspace / "practice" / "sessions" / session["id"] / "reveal.json").is_file())


if __name__ == "__main__":
    unittest.main()
