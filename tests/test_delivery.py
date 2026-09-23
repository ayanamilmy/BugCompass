"""新模块的固定测试集：settings / metrics / telemetry / diagnostics / backup / resources / cli。"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass import backup, diagnostics, metrics, resources, settings, telemetry  # noqa: E402
from bugcompass.cli import build_parser  # noqa: E402


def _reset_caches() -> None:
    resources.bundle_root.cache_clear()
    resources.data_root.cache_clear()
    resources.user_root.cache_clear()


class IsolatedHomeTestCaseMixin:
    """每个测试用独立的 BUGCOMPASS_HOME，避免读写真实的用户目录。"""

    def setUp(self) -> None:
        self._home = TemporaryDirectory(prefix="bugcompass-test-home-")
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"BUGCOMPASS_HOME": self._home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_reset_caches)
        _reset_caches()


class SettingsTests(IsolatedHomeTestCaseMixin, __import__("unittest").TestCase):
    def test_defaults_and_roundtrip(self) -> None:
        data = settings.load_settings()
        self.assertEqual(data["ui_scale_percent"], 100)
        self.assertFalse(data["telemetry_enabled"])
        settings.set_value("ui_scale_percent", 125)
        self.assertEqual(settings.load_settings()["ui_scale_percent"], 125)

    def test_atomic_write_keeps_valid_json(self) -> None:
        settings.set_value("telemetry_enabled", True)
        path = resources.settings_path()
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["telemetry_enabled"])


class TelemetryTests(IsolatedHomeTestCaseMixin, __import__("unittest").TestCase):
    def test_disabled_by_default_records_nothing(self) -> None:
        self.assertFalse(telemetry.is_enabled())
        self.assertIsNone(telemetry.record_run_metrics(case_id="c", model="m", duration_seconds=1.0, turns=1, tool_calls=2, input_tokens=10, output_tokens=5, estimated_cost_usd=None, status="ok"))
        self.assertEqual(telemetry.pending(), [])

    def test_enabled_writes_local_outbox_only(self) -> None:
        telemetry.set_enabled(True)
        target = telemetry.record_run_metrics(case_id="c", model="m", duration_seconds=1.5, turns=1, tool_calls=2, input_tokens=10, output_tokens=5, estimated_cost_usd=0.01, status="ok")
        self.assertIsNotNone(target)
        self.assertEqual(len(telemetry.pending()), 1)
        event = json.loads(target.read_text(encoding="utf-8"))["event"]  # type: ignore[attr-defined]
        self.assertEqual(event["tool_calls"], 2)
        # 事件里只有计数，没有任何正文或路径
        self.assertNotIn("issue", json.dumps(event))
        # 模块内不存在网络调用
        import inspect

        source = inspect.getsource(telemetry)
        for forbidden in ("urllib", "requests", "http.client", "socket", "urlopen"):
            self.assertNotIn(forbidden, source)


class MetricsTests(__import__("unittest").TestCase):
    def test_parse_codex_log_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.jsonl"
            events = [
                json.dumps({"type": "thread.started"}),
                json.dumps({"type": "turn.started"}),
                json.dumps({"type": "item.started", "item": {"type": "command_execution"}}),
                json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}),
                json.dumps({"type": "item.completed", "item": {"type": "command_execution"}}),
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 50}}),
                "not json at all",
            ]
            log.write_text("\n".join(events) + "\n", encoding="utf-8")
            counts = metrics.parse_codex_log(log)
        self.assertEqual(counts["turns"], 1)
        self.assertEqual(counts["tool_calls"], 2)
        self.assertEqual(counts["input_tokens"], 100)
        self.assertEqual(counts["cached_input_tokens"], 40)
        self.assertEqual(counts["output_tokens"], 50)

    def test_estimate_cost_requires_user_pricing(self) -> None:
        pricing = {"model-x": {"input": 1.0, "cached_input": 0.1, "output": 4.0}}
        cost = metrics.estimate_cost("model-x", input_tokens=1_000_000, cached_input_tokens=200_000, output_tokens=500_000, pricing=pricing)
        self.assertAlmostEqual(cost, 0.8 * 1.0 + 0.2 * 0.1 + 0.5 * 4.0, places=6)
        self.assertIsNone(metrics.estimate_cost("unknown-model", input_tokens=10, cached_input_tokens=0, output_tokens=10, pricing=pricing))
        self.assertIsNone(metrics.estimate_cost("model-x", input_tokens=10, cached_input_tokens=0, output_tokens=10, pricing={}))

    def test_case_totals_cost_is_none_when_any_run_unpriced(self) -> None:
        case = metrics.CaseMetrics(case_id="c")
        case.runs.append(metrics.RunMetrics(model="a", estimated_cost_usd=0.5))
        case.runs.append(metrics.RunMetrics(model="b", estimated_cost_usd=None))
        self.assertIsNone(case.estimated_cost_usd)
        self.assertEqual(case.tool_calls, 0)

    def test_format_helpers(self) -> None:
        self.assertEqual(metrics.format_duration(59.9), "59.9s")
        self.assertEqual(metrics.format_duration(125), "2m05s")
        self.assertEqual(metrics.format_cost(None), "未配置单价")
        self.assertTrue(metrics.format_cost(0.004).startswith("$0.00"))


class DiagnosticsTests(IsolatedHomeTestCaseMixin, __import__("unittest").TestCase):
    def test_redact_secrets_and_paths(self) -> None:
        home = str(Path.home())
        text = f"path={home}/x key=sk-{'A' * 30} mail=a.b@example.com gh=ghp_{'B' * 30}"
        redacted = diagnostics.redact(text)
        self.assertNotIn("sk-", redacted)
        self.assertNotIn("ghp_", redacted)
        self.assertNotIn("example.com", redacted)
        if os.name != "nt":
            self.assertNotIn(home, redacted)

    def test_env_summary_never_contains_values(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-super-secret-value-123"}):
            summary = diagnostics.environment_summary()
        dumped = json.dumps(summary)
        self.assertNotIn("sk-super-secret-value-123", dumped)
        self.assertTrue(summary["env"]["OPENAI_API_KEY"]["present"])

    def test_bundle_rejects_secret_content(self) -> None:
        # 正常路径：日志里的密钥会被 redact() 清掉，verify 通过。
        with TemporaryDirectory() as tmp:
            bad_log = resources.logs_dir() / "crash-bad.log"
            bad_log.write_text("token=abcdef1234567890abcdef1234567890extra\n", encoding="utf-8")
            try:
                target = diagnostics.export_bundle(tmp)
                with zipfile.ZipFile(target) as archive:
                    content = archive.read("logs/crash-bad.log").decode("utf-8")
                self.assertNotIn("abcdef", content)
            finally:
                bad_log.unlink(missing_ok=True)
        # 安全网：如果未来有人忘了 redact，verify_bundle 必须把包判为不合格。
        with TemporaryDirectory() as tmp, mock.patch.object(diagnostics, "redact", lambda text: text):
            bad_log = resources.logs_dir() / "crash-bad2.log"
            bad_log.write_text("token=abcdef1234567890abcdef1234567890extra\n", encoding="utf-8")
            try:
                with self.assertRaises(AssertionError):
                    diagnostics.export_bundle(tmp)
            finally:
                bad_log.unlink(missing_ok=True)

    def test_bundle_ok_and_self_verified(self) -> None:
        crash = diagnostics.write_crash_log("正常崩溃信息 sk-REDACTED please\n")
        self.assertTrue(crash.is_file())
        with TemporaryDirectory() as tmp:
            target = diagnostics.export_bundle(tmp)
            diagnostics.verify_bundle(target)
            with zipfile.ZipFile(target) as archive:
                names = archive.namelist()
            self.assertIn("environment.json", names)
            self.assertIn("logs/crash-", " ".join(names))
            for name in names:
                self.assertNotIn(".md", name)


class BackupTests(__import__("unittest").TestCase):
    def _workspace(self, root: Path) -> Path:
        repo = root / "repo"
        (repo / "source" / "blender").mkdir(parents=True)
        (repo / "CMakeLists.txt").write_text("# x\n", encoding="utf-8")
        import subprocess

        for args in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-qm", "init"]):
            subprocess.run(args, cwd=repo, check=True)
        ws = root / "ws"
        ws.mkdir()
        (ws / "project.json").write_text(
            json.dumps({"schema_version": 1, "project": "blender", "pack_version": "0.1.0", "repo_path": str(repo), "created_at": "2026-01-01T00:00:00Z"}),
            encoding="utf-8",
        )
        case = ws / "cases" / "c1"
        case.mkdir(parents=True)
        (case / "case.json").write_text(json.dumps({"schema_version": 1, "id": "c1"}), encoding="utf-8")
        (case / "issue-original.md").write_text("# 标题\n", encoding="utf-8")
        (case / ".codex-run.lock").write_text("{}", encoding="utf-8")
        return ws

    def test_backup_roundtrip_and_migration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = self._workspace(root)
            archive = backup.create_backup(ws, dest=root / "b.zip")
            manifest = backup.inspect_backup(archive)
            self.assertEqual(manifest["file_count"], 3)  # lock 被排除
            restored = backup.restore_backup(archive, root / "restored")
            self.assertTrue((restored / "cases" / "c1" / "issue-original.md").is_file())
            # schema_version 1 → 2 迁移
            self.assertEqual(json.loads((restored / "project.json").read_text(encoding="utf-8"))["schema_version"], 2)
            self.assertEqual(json.loads((restored / "cases" / "c1" / "case.json").read_text(encoding="utf-8"))["schema_version"], 2)
            # 幂等
            self.assertEqual(backup.migrate_workspace(restored), [])

    def test_restore_rejects_zip_slip(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            evil = root / "evil.zip"
            with zipfile.ZipFile(evil, "w") as archive:
                archive.writestr(backup.MANIFEST_NAME, json.dumps({"schema_version": 2, "workspace_name": "ws", "files": []}))
                archive.writestr("../escape.txt", "pwn")
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(evil, root / "out")

    def test_restore_rejects_newer_schema(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "future.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(backup.MANIFEST_NAME, json.dumps({"schema_version": backup.BACKUP_SCHEMA_VERSION + 1, "workspace_name": "ws"}))
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(archive, root / "out")


class ResourcesTests(__import__("unittest").TestCase):
    def test_data_root_override(self) -> None:
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "packs" / "blender").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"BUGCOMPASS_DATA_ROOT": tmp}):
                _reset_caches()
                self.assertEqual(resources.data_root(), Path(tmp).resolve())
                self.assertTrue(resources.pack_root().is_dir())
            _reset_caches()

    def test_frozen_falls_back_to_executable_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            fake_meipass = Path(tmp) / "_MEIPASS"
            (fake_meipass / "packs").mkdir(parents=True)
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "_MEIPASS", str(fake_meipass), create=True):
                _reset_caches()
                self.assertEqual(resources.data_root(), fake_meipass)
            _reset_caches()


class CliParserTests(__import__("unittest").TestCase):
    def test_new_subcommands_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["backup", "--workspace", "w", "--output", "o.zip"])
        self.assertEqual(args.command, "backup")
        args = parser.parse_args(["restore", "--archive", "a.zip", "--dest", "d"])
        self.assertEqual(args.command, "restore")
        args = parser.parse_args(["diagnostics", "--dest", "d"])
        self.assertEqual(args.command, "diagnostics")
        args = parser.parse_args(["telemetry", "--status"])
        self.assertEqual(args.command, "telemetry")
        args = parser.parse_args(["case", "metrics", "--workspace", "w", "--id", "x"])
        self.assertEqual(args.case_command, "metrics")
        args = parser.parse_args(["migrate", "--workspace", "w"])
        self.assertEqual(args.command, "migrate")


class MindmapLogicTests(__import__("unittest").TestCase):
    def test_split_units_cjk_vs_words(self) -> None:
        from bugcompass.mindmap import MindMapCanvas

        units = MindMapCanvas._split_units("崩溃 crash the mesh 节点")
        self.assertIn("崩", units)
        self.assertIn("crash ", units)
        joined = "".join(units)
        self.assertEqual(joined, "崩溃 crash the mesh 节点")

    def test_border_point_stays_on_rectangle(self) -> None:
        from bugcompass.mindmap import MindMapCanvas

        rect = (0.0, 0.0, 100.0, 40.0)
        point = MindMapCanvas._border_point(rect, (200.0, 20.0))
        self.assertEqual(point[0], 100.0)
        self.assertTrue(0.0 <= point[1] <= 40.0)
