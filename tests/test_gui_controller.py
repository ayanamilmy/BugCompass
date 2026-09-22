from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from bugcompass.cli import main  # noqa: E402
from bugcompass.codex_runner import CodexRunBusyError, CodexRunner  # noqa: E402
from bugcompass.gui_controller import GuiController, generate_case_id  # noqa: E402
from bugcompass.gui import codex_status_for_case  # noqa: E402
from bugcompass.experiments import classify_command  # noqa: E402
from bugcompass.investigation import export_markdown, merge_user_decisions, write_investigation  # noqa: E402
from bugcompass.workspace import BugCompassError  # noqa: E402


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_blender_fixture(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    (path / "source" / "blender" / "editors").mkdir(parents=True)
    (path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\n", encoding="utf-8")
    git(path, "add", ".")
    git(
        path,
        "-c",
        "user.name=BugCompass GUI Test",
        "-c",
        "user.email=gui@example.invalid",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return path


class GuiControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = make_blender_fixture(self.root / "blender")
        self.workspace = self.root / "workspace"
        self.controller = GuiController(self.workspace)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_repository_validation_accepts_blender_and_rejects_invalid_paths(self) -> None:
        valid = self.controller.validate_repository(self.repo)
        self.assertTrue(valid.valid)
        self.assertIn("有效的 Blender", valid.message)

        missing = self.controller.validate_repository(self.root / "missing")
        self.assertFalse(missing.valid)
        self.assertIn("不存在", missing.message)

        plain = self.root / "plain"
        plain.mkdir()
        not_git = self.controller.validate_repository(plain)
        self.assertFalse(not_git.valid)
        self.assertIn("不是 Git", not_git.message)

        non_blender = self.root / "git-only"
        non_blender.mkdir()
        git(non_blender, "init", "-q")
        invalid_project = self.controller.validate_repository(non_blender)
        self.assertFalse(invalid_project.valid)
        self.assertIn("CMakeLists.txt", invalid_project.message)

    def test_generated_case_id_is_valid_and_predictable(self) -> None:
        case_id = generate_case_id(datetime(2026, 9, 21, 15, 30, 45))
        self.assertEqual(case_id, "blender-20260921-153045")
        self.assertRegex(case_id, r"^blender-\d{8}-\d{6}$")

    def test_codex_status_is_scoped_to_one_case(self) -> None:
        active = codex_status_for_case(
            active_case_id="case-a",
            run_state="working",
            run_detail="正在读取源码。",
            viewed_case_id="case-a",
            viewed_status="investigating",
        )
        self.assertEqual(active.label, "● Codex 工作中")
        self.assertTrue(active.can_stop)
        self.assertIn("case-a", active.detail)

        other = codex_status_for_case(
            active_case_id="case-a",
            run_state="working",
            run_detail="正在读取源码。",
            viewed_case_id="case-b",
            viewed_status="complete",
        )
        self.assertEqual(other.label, "● 其他案件调查中")
        self.assertFalse(other.can_stop)
        self.assertIn("当前案件没有在运行", other.detail)

        stale = codex_status_for_case(
            active_case_id=None,
            run_state="idle",
            run_detail="",
            viewed_case_id="case-c",
            viewed_status="investigating",
        )
        self.assertEqual(stale.label, "● Codex 已停止")
        self.assertIn("可能已中断", stale.detail)

        local_experiment = codex_status_for_case(
            active_case_id=None,
            run_state="idle",
            run_detail="正在运行本地实验，Codex 当前没有工作。",
            viewed_case_id="case-b",
            viewed_status="complete",
            operation_busy=True,
        )
        self.assertEqual(local_experiment.label, "● Codex 空闲")
        self.assertIn("本地实验", local_experiment.detail)

    def test_matching_workspace_is_reused(self) -> None:
        first = self.controller.ensure_workspace(self.repo)
        original = (self.workspace / "project.json").read_text(encoding="utf-8")
        second = self.controller.ensure_workspace(self.repo)
        self.assertEqual(first.path, second.path)
        self.assertEqual((self.workspace / "project.json").read_text(encoding="utf-8"), original)

    def test_mismatched_workspace_is_not_overwritten(self) -> None:
        self.controller.ensure_workspace(self.repo)
        original = (self.workspace / "project.json").read_bytes()
        other_repo = make_blender_fixture(self.root / "other-blender")
        with self.assertRaises(BugCompassError) as stopped:
            self.controller.ensure_workspace(other_repo)
        self.assertIn("不会覆盖", str(stopped.exception))
        self.assertEqual((self.workspace / "project.json").read_bytes(), original)

    def test_create_and_reload_case_contents(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n\n视图没有刷新。\n")
        self.assertTrue(view.awaiting_investigation)
        data = view.investigation
        data["stage"] = "paths"
        data["summary"]["problem"] = "已由 Codex 更新。"
        data["hypotheses"] = [self._hypothesis(f"H{index}") for index in range(1, 4)]
        write_investigation(view.case_dir / "investigation.json", data, require_complete=True)
        export_markdown(view.case_dir, data)
        refreshed = self.controller.load_case(view.case_id)
        self.assertIn("已由 Codex 更新", refreshed.contents["intake.md"])
        self.assertFalse(refreshed.awaiting_investigation)
        self.assertEqual(refreshed.contents["issue-original.md"], "# Bug\n\n视图没有刷新。\n")
        self.assertEqual(refreshed.investigation["semantic_diff"]["status"], "not_available")
        self.assertEqual(refreshed.investigation["causal_graph"], {"nodes": [], "edges": []})

        self.controller.set_case_status(view.case_id, "investigating")
        metadata = json.loads((view.case_dir / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "investigating")

    @staticmethod
    def _hypothesis(hypothesis_id: str) -> dict[str, object]:
        return {
            "id": hypothesis_id, "title": f"路径 {hypothesis_id}", "priority": "high",
            "status": "active", "claim": "待验证", "basis": ["源码线索"], "evidence_ids": [],
            "source_references": [], "next_step": "继续只读搜索", "supporting_result": "找到调用链",
            "weakening_result": "不存在调用", "risk": "低", "estimated_cost": "low", "user_note": "",
        }

    def test_instruction_contains_workspace_and_case_id(self) -> None:
        instruction = self.controller.investigation_instruction("blender-20260921-153045")
        self.assertIn("$blender-bug-investigator", instruction)
        self.assertIn("blender-20260921-153045", instruction)
        self.assertIn(str(self.workspace.resolve()), instruction)
        self.assertIn("不修改 Blender 源码", instruction)

    def test_reject_path_is_saved_and_survives_reload(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        data = view.investigation
        data["hypotheses"] = [self._hypothesis(f"H{index}") for index in range(1, 4)]
        write_investigation(view.case_dir / "investigation.json", data, require_complete=True)
        refreshed = self.controller.reject_path(view.case_id, "H2")
        rejected = next(item for item in refreshed.investigation["hypotheses"] if item["id"] == "H2")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("用户", rejected["user_note"])

    def test_source_reference_stays_inside_repository(self) -> None:
        self.controller.ensure_workspace(self.repo)
        source = self.repo / "source" / "blender" / "editors" / "demo.cc"
        source.write_text("line one\nline two\n", encoding="utf-8")
        path, line = self.controller.resolve_source_reference({"path": "source/blender/editors/demo.cc", "line": 2})
        self.assertEqual(path, source.resolve())
        self.assertEqual(line, 2)
        with self.assertRaises(BugCompassError):
            self.controller.resolve_source_reference({"path": "../outside", "line": 1})

    def test_json_is_primary_display_source(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        data = view.investigation
        data["summary"]["problem"] = "结构化内容"
        write_investigation(view.case_dir / "investigation.json", data)
        (view.case_dir / "intake.md").write_text("# 过期 Markdown\n", encoding="utf-8")
        refreshed = self.controller.load_case(view.case_id)
        self.assertEqual(refreshed.investigation["summary"]["problem"], "结构化内容")

    def test_experiment_permissions_are_recomputed(self) -> None:
        self.assertEqual(classify_command(["rg", "attribute", "source/blender"]), "green")
        self.assertEqual(classify_command(["git", "log", "-5"]), "green")
        self.assertEqual(classify_command(["cmake", "--build", "."]), "yellow")
        self.assertEqual(classify_command(["git", "push"]), "red")

    def test_green_experiment_records_execution(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        experiment = {
            "id": "A1", "hypothesis_id": "H1", "title": "读取状态", "purpose": "确认 commit",
            "description": "读取 Git 状态", "command": ["git", "status", "--short"], "cwd": ".",
            "permission": "green", "estimated_seconds": 2, "status": "suggested",
            "expected_support": "命中", "expected_weakening": "未命中", "result": "", "effect": "pending", "latest_run": "",
        }
        data = view.investigation
        data["suggested_experiments"] = [experiment]
        write_investigation(view.case_dir / "investigation.json", data)
        plan = self.controller.prepare_experiment(experiment)
        predicted = self.controller.record_prediction(view.case_id, "A1", "命令成功并返回空状态", "临时仓库当前没有未提交修改。")
        saved_prediction = predicted.investigation["suggested_experiments"][0]["prediction"]
        self.assertTrue(saved_prediction["predicted_at"])
        record = self.controller.run_experiment(view.case_id, plan)
        self.assertEqual(record["return_code"], 0)
        self.assertEqual(record["permission"], "green")
        self.assertEqual(record["blender_commit"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(record["prediction"]["choice"], "命令成功并返回空状态")
        run_dir = view.case_dir / "experiments" / record["run_id"]
        self.assertTrue((run_dir / "result.json").is_file())
        self.assertTrue((run_dir / "stdout.txt").is_file())
        self.assertTrue((run_dir / "stderr.txt").is_file())
        refreshed = self.controller.load_case(view.case_id)
        saved_experiment = refreshed.investigation["suggested_experiments"][0]
        self.assertEqual(saved_experiment["latest_run"], record["run_id"])
        self.assertEqual(saved_experiment["status"], "completed")

    def test_experiment_requires_prediction_before_running(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        experiment = {
            "id": "A1", "hypothesis_id": "H1", "title": "读取状态", "purpose": "确认状态",
            "description": "读取 Git 状态", "command": ["git", "status", "--short"], "cwd": ".",
            "permission": "green", "estimated_seconds": 2, "status": "suggested",
            "expected_support": "命中", "expected_weakening": "未命中", "result": "", "effect": "pending", "latest_run": "",
        }
        data = view.investigation
        data["suggested_experiments"] = [experiment]
        write_investigation(view.case_dir / "investigation.json", data)
        with self.assertRaises(BugCompassError) as stopped:
            self.controller.run_experiment(view.case_id, self.controller.prepare_experiment(experiment))
        self.assertIn("预测", str(stopped.exception))

    def test_causal_graph_edits_are_persisted(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        graph = {
            "nodes": [
                {"id": "C1", "label": "进入错误分支", "kind": "decision", "certainty": "inference", "evidence_ids": [], "x": 20, "y": 30},
                {"id": "C2", "label": "产生可见故障", "kind": "failure", "certainty": "fact", "evidence_ids": [], "x": 260, "y": 30},
            ],
            "edges": [{"id": "CE1", "from": "C1", "to": "C2", "label": "导致", "certainty": "inference", "user_created": True}],
        }
        refreshed = self.controller.save_causal_graph(view.case_id, graph)
        self.assertEqual(refreshed.investigation["causal_graph"]["nodes"][0]["x"], 20)
        self.assertEqual(refreshed.investigation["causal_graph"]["edges"][0]["to"], "C2")

    def test_codex_merge_keeps_user_graph_edits_and_locked_prediction(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        previous = view.investigation
        previous["causal_graph"] = {
            "nodes": [{"id": "C1", "label": "用户修改后的机制", "kind": "state", "certainty": "inference", "evidence_ids": [], "x": 77, "y": 88, "user_edited": True}],
            "edges": [],
        }
        previous["suggested_experiments"] = [{"id": "A1", "prediction": {"choice": "会失败", "rationale": "旧状态仍在", "predicted_at": "2026-09-21T00:00:00Z"}}]
        candidate = deepcopy(view.investigation)
        candidate["causal_graph"] = {
            "nodes": [{"id": "C1", "label": "AI 覆盖文本", "kind": "state", "certainty": "fact", "evidence_ids": [], "x": 10, "y": 10}],
            "edges": [],
        }
        candidate["suggested_experiments"] = [{"id": "A1", "prediction": {"choice": "", "rationale": "", "predicted_at": ""}}]
        merged = merge_user_decisions(previous, candidate)
        self.assertEqual(merged["causal_graph"]["nodes"][0]["label"], "用户修改后的机制")
        self.assertEqual((merged["causal_graph"]["nodes"][0]["x"], merged["causal_graph"]["nodes"][0]["y"]), (77, 88))
        self.assertEqual(merged["suggested_experiments"][0]["prediction"]["choice"], "会失败")

    def test_experiment_rejects_false_permission_and_path_escape(self) -> None:
        self.controller.ensure_workspace(self.repo)
        environment = {"checks": {"build_directories": {"value": {"found": []}}}}
        with patch.object(self.controller, "_read_environment", return_value=environment):
            with self.assertRaises(BugCompassError):
                self.controller.prepare_experiment({
                    "id": "A1", "hypothesis_id": "H1", "title": "危险", "command": ["git", "push"],
                    "cwd": ".", "permission": "green", "estimated_seconds": 10,
                })
            with self.assertRaises(BugCompassError):
                self.controller.prepare_experiment({
                    "id": "../escape", "hypothesis_id": "H1", "title": "越界", "command": ["git", "status"],
                    "cwd": ".", "permission": "green", "estimated_seconds": 10,
                })
            with self.assertRaises(BugCompassError):
                self.controller.prepare_experiment({
                    "id": "A2", "hypothesis_id": "H1", "title": "越界", "command": ["cat", "../secret"],
                    "cwd": ".", "permission": "green", "estimated_seconds": 10,
                })

    def test_gui_check_has_explicit_result(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["gui", "--check"])
        output = stdout.getvalue() + stderr.getvalue()
        self.assertIn(code, {0, 2})
        self.assertRegex(output, r"检查(通过|失败)：")
        self.assertIn("Tkinter", output)

    def test_importing_gui_module_does_not_create_window(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        result = subprocess.run(
            [sys.executable, "-c", "import bugcompass.gui; print('GUI 模块导入完成')"],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "GUI 模块导入完成")

    def test_codex_command_is_scoped_to_case_directory(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        runner = CodexRunner(ROOT, executable="/fake/codex")
        command = runner.build_command(view)
        self.assertEqual(command[:2], ["/fake/codex", "exec"])
        self.assertIn("workspace-write", command)
        self.assertIn("--ignore-user-config", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-terra")
        self.assertIn('model_reasoning_effort="low"', command)
        self.assertIn("--output-schema", command)
        self.assertIn("investigation.schema.json", command[command.index("--output-schema") + 1])
        self.assertEqual(command[command.index("-o") + 1], str(view.case_dir / "investigation.next.json"))
        self.assertEqual(command[command.index("--cd") + 1], str(view.case_dir))
        self.assertNotIn("--add-dir", command)
        prompt = command[-1]
        self.assertIn(str(view.case_dir), prompt)
        self.assertIn(str(view.repo_path), prompt)
        self.assertIn("只允许修改当前案件目录", prompt)
        self.assertIn("不要修改 Blender 源码", prompt)

        continue_prompt = runner.build_command(view, action="continue")[-1]
        self.assertIn("继续调查当前案件", continue_prompt)
        self.assertIn("更新同一个结果", continue_prompt)

    def test_output_schema_marks_every_object_property_required(self) -> None:
        schema = json.loads((ROOT / "packs" / "blender" / "investigation.schema.json").read_text(encoding="utf-8"))

        def check_strict_objects(value: object, location: str = "root") -> None:
            if isinstance(value, dict):
                if value.get("type") == "object" and "properties" in value:
                    properties = set(value["properties"])
                    required = set(value.get("required", []))
                    self.assertEqual(properties, required, f"{location} 不是严格对象 Schema")
                for key, child in value.items():
                    check_strict_objects(child, f"{location}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    check_strict_objects(child, f"{location}[{index}]")

        check_strict_objects(schema)

    def test_codex_runner_parses_progress_without_real_codex(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        runner = CodexRunner(ROOT, executable="/fake/codex")

        class FakeProcess:
            pid = 12345
            stdout = iter(
                (
                    '{"type":"thread.started","thread_id":"demo"}\n',
                    '{"type":"turn.started"}\n',
                    '{"type":"item.completed","item":{"type":"agent_message","text":"完成"}}\n',
                    '{"type":"turn.completed"}\n',
                )
            )

            def wait(self) -> int:
                return 0

            def poll(self) -> None:
                return None

        progress: list[str] = []
        with patch("bugcompass.codex_runner.subprocess.Popen", return_value=FakeProcess()) as popen:
            result = runner.run(view, progress.append)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.final_message, "完成")
        self.assertTrue(any("调查已开始" in message for message in progress))
        called = popen.call_args
        self.assertEqual(called.kwargs["cwd"], view.case_dir)
        self.assertIs(called.kwargs["stdin"], subprocess.DEVNULL)
        summary = json.loads((view.case_dir / "codex-last-run.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["model"], "gpt-5.6-terra")
        self.assertEqual(summary["reasoning_effort"], "low")
        self.assertFalse(summary["timed_out"])
        self.assertFalse((view.case_dir / ".codex-run.lock").exists())

    def test_codex_runner_stops_a_timed_out_process(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        runner = CodexRunner(ROOT, executable="/fake/codex", timeout_seconds=0.01)

        class SlowOutput:
            def __iter__(self):
                return self

            def __next__(self):
                time.sleep(0.05)
                raise StopIteration

        class FakeProcess:
            pid = 12345
            stdout = SlowOutput()

            def wait(self) -> int:
                return -15

            def poll(self) -> None:
                return None

        with (
            patch("bugcompass.codex_runner.subprocess.Popen", return_value=FakeProcess()),
            patch("bugcompass.codex_runner.os.killpg") as kill_group,
        ):
            result = runner.run(view)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.investigation_updated)
        self.assertIn("自动停止", result.error_detail)
        kill_group.assert_called_once_with(12345, signal.SIGTERM)
        summary = json.loads((view.case_dir / "codex-last-run.json").read_text(encoding="utf-8"))
        self.assertTrue(summary["timed_out"])

    def test_codex_runner_extracts_a_readable_error(self) -> None:
        detail = '\n'.join(
            (
                '{"type":"turn.started"}',
                '{"type":"error","message":"模型暂时不可用"}',
            )
        )
        self.assertEqual(CodexRunner.extract_error_message(detail), "模型暂时不可用")

    def test_codex_runner_refuses_a_second_process_for_the_same_case(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        lock_path = view.case_dir / ".codex-run.lock"
        lock_path.write_text(
            json.dumps({"case_id": view.case_id, "owner_pid": os.getpid(), "token": "other"}),
            encoding="utf-8",
        )
        runner = CodexRunner(ROOT, executable="/fake/codex")
        with (
            patch("bugcompass.codex_runner.subprocess.Popen") as popen,
            self.assertRaises(CodexRunBusyError) as stopped,
        ):
            runner.run(view)
        self.assertIn("另一个 BugCompass 窗口", str(stopped.exception))
        popen.assert_not_called()
        self.assertTrue(lock_path.is_file())

    def test_codex_runner_can_cancel_before_process_starts(self) -> None:
        view = self.controller.create_investigation(self.repo, "# Bug\n")
        runner = CodexRunner(ROOT, executable="/fake/codex")
        runner.cancel()
        with patch("bugcompass.codex_runner.subprocess.Popen") as popen:
            result = runner.run(view)
        self.assertTrue(result.cancelled)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
