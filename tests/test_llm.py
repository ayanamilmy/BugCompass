"""大模型接入的固定测试集：llm.py（配置/客户端）、llm_runner.py（引擎与工具防护）、GUI 纯函数。"""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass import llm, llm_runner, metrics  # noqa: E402
from bugcompass.gui import codex_status_for_case  # noqa: E402
from bugcompass.investigation import read_investigation, write_investigation  # noqa: E402
from bugcompass.llm import (  # noqa: E402
    ensure_providers,
    providers_path,
    LLMError,
    LLMProviderConfig,
    chat_completion,
    load_providers,
    provider_by_id,
    resolve_api_key,
    write_providers_template,
)
from bugcompass.llm_runner import LLMInvestigator, LLMRunResult, ToolError  # noqa: E402
from bugcompass.settings import load_settings  # noqa: E402

import unittest


def _reset_caches() -> None:
    from bugcompass import resources

    resources.bundle_root.cache_clear()
    resources.data_root.cache_clear()
    resources.user_root.cache_clear()


class IsolatedHomeTestCase(unittest.TestCase):
    """每个测试用独立的 BUGCOMPASS_HOME，避免读写真实用户目录。"""

    def setUp(self) -> None:
        self._home = TemporaryDirectory(prefix="bugcompass-llm-test-")
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"BUGCOMPASS_HOME": self._home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_reset_caches)
        _reset_caches()


def make_provider(**overrides) -> LLMProviderConfig:
    values = dict(
        id="test",
        label="测试服务",
        base_url="https://example.test/v1",
        model="test-model",
        api_key_env="TEST_API_KEY",
        max_turns=4,
        timeout_seconds=5,
    )
    values.update(overrides)
    return LLMProviderConfig(**values)


def valid_investigation(case_id: str) -> dict:
    return {
        "schema_version": 1,
        "case_id": case_id,
        "summary": {
            "problem": "测试问题",
            "expected_behavior": "应当正常",
            "actual_behavior": "实际异常",
            "reproduction_steps": ["步骤"],
            "known_environment": [],
            "missing_information": [],
        },
        "hypotheses": [
            {
                "id": f"H{index}",
                "title": f"路径 {index}",
                "claim": "说明",
                "priority": "high" if index == 1 else "medium",
                "status": "open",
                "basis": ["依据"],
                "source_references": [{"path": "source/blender/a.cc", "line": 10}],
                "next_step": "下一步",
                "evidence_ids": [],
            }
            for index in range(1, 4)
        ],
        "evidence": [{"id": "E1", "kind": "fact", "statement": "事实", "source_references": []}],
        "unknowns": [{"id": "U1", "question": "未知"}],
        "suggested_experiments": [],
        "causal_graph": {"nodes": [], "edges": []},
        "semantic_diff": {
            "status": "not_available",
            "summary": "",
            "old_rule": "",
            "new_rule": "",
            "changed_invariants": [],
            "affected_paths": [],
            "remaining_risks": [],
            "source_references": [],
        },
    }


# ----------------------------------------------------------------- 配置层
class ProviderConfigTests(IsolatedHomeTestCase):
    def test_presets_load_without_file(self) -> None:
        providers = load_providers()
        ids = {provider.id for provider in providers}
        self.assertIn("deepseek", ids)
        self.assertIn("ollama", ids)
        for provider in providers:
            self.assertTrue(provider.base_url.startswith("http"))

    def test_template_contains_no_secrets(self) -> None:
        path = write_providers_template()
        content = path.read_text(encoding="utf-8")
        self.assertIn("api_key_env", content)
        # 模板里绝不能出现任何密钥样式的值（只有环境变量名）。
        self.assertNotIn("sk-", content)
        self.assertNotIn("Bearer ", content)
        # 写模板后再读，内容一致。
        providers = load_providers()
        self.assertGreater(len(providers), 0)

    def test_resolve_api_key_from_env_and_missing(self) -> None:
        provider = make_provider()
        with mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-test-123"}):
            self.assertEqual(resolve_api_key(provider), "sk-test-123")
        env = {k: v for k, v in os.environ.items() if k != "TEST_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(LLMError) as ctx:
                resolve_api_key(provider)
            self.assertIn("TEST_API_KEY", str(ctx.exception))  # 错误信息带设置指引

    def test_no_key_provider_resolves_none(self) -> None:
        provider = make_provider(api_key_env="")
        self.assertIsNone(resolve_api_key(provider))

    def test_provider_by_id(self) -> None:
        providers = load_providers()
        self.assertIsNotNone(provider_by_id(providers, "deepseek"))
        self.assertIsNone(provider_by_id(providers, "nope"))

    def test_invalid_config_rejected(self) -> None:
        with tempfile_providers([{"id": "X", "base_url": "ftp://x", "model": "m"}]):
            with self.assertRaises(LLMError):
                load_providers()

    def test_duplicate_ids_rejected(self) -> None:
        entry = {"id": "dup", "label": "d", "base_url": "https://x.test/v1", "model": "m"}
        with tempfile_providers([entry, dict(entry)]):
            with self.assertRaises(LLMError):
                load_providers()


class tempfile_providers:
    """把临时的 providers.json 放进隔离的 BUGCOMPASS_HOME。"""

    def __init__(self, providers: list[dict]) -> None:
        self.providers = providers

    def __enter__(self):
        path = llm.providers_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "providers": self.providers}, ensure_ascii=False), encoding="utf-8")
        return path

    def __exit__(self, *args) -> None:
        llm.providers_path().unlink(missing_ok=True)


# ----------------------------------------------------------------- HTTP 客户端
def fake_urlopen(payload: dict, code: int = 200):
    body = json.dumps(payload).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return body

    return Response()


def http_error(code: int, body: str = ""):
    return urllib.error.HTTPError("https://example.test/v1/chat/completions", code, "err", {}, io.BytesIO(body.encode("utf-8")))


def completion_payload(content: str = "ok", tool_calls: list | None = None, usage: dict | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }


class ChatClientTests(unittest.TestCase):
    def test_success_and_normalization(self) -> None:
        provider = make_provider()
        payload = completion_payload(
            content="你好",
            tool_calls=[{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}],
            usage={"prompt_tokens": 100, "completion_tokens": 40, "prompt_tokens_details": {"cached_tokens": 30}},
        )
        with mock.patch("urllib.request.urlopen", return_value=fake_urlopen(payload)) as urlopen:
            response = chat_completion(provider, [{"role": "user", "content": "hi"}], api_key="k")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer k")
        self.assertEqual(response.content, "你好")
        self.assertEqual(response.tool_calls[0]["name"], "read_file")
        self.assertEqual(response.tool_calls[0]["arguments"], {"path": "a.py"})
        self.assertEqual(response.usage["input_tokens"], 100)
        self.assertEqual(response.usage["cached_input_tokens"], 30)

    def test_auth_error_is_friendly(self) -> None:
        provider = make_provider()
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401, json.dumps({"error": {"message": "bad key"}}))):
            with self.assertRaises(LLMError) as ctx:
                chat_completion(provider, [{"role": "user", "content": "hi"}], api_key="bad")
        self.assertIn("TEST_API_KEY", str(ctx.exception))

    def test_retry_on_429_then_success(self) -> None:
        provider = make_provider()
        calls = [http_error(429, "slow down"), fake_urlopen(completion_payload("ok"))]
        with mock.patch("urllib.request.urlopen", side_effect=calls), mock.patch("time.sleep"):
            response = chat_completion(provider, [{"role": "user", "content": "hi"}], api_key="k")
        self.assertEqual(response.content, "ok")

    def test_no_retry_on_400(self) -> None:
        provider = make_provider()
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400, json.dumps({"error": {"message": "bad request"}}))) as urlopen:
            with self.assertRaises(LLMError):
                chat_completion(provider, [{"role": "user", "content": "hi"}], api_key="k")
        self.assertEqual(urlopen.call_count, 1)

    def test_connection_error_mentions_base_url(self) -> None:
        provider = make_provider()
        import socket as socket_module

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(socket_module.gaierror("name resolution failed"))):
            with self.assertRaises(LLMError) as ctx:
                chat_completion(provider, [{"role": "user", "content": "hi"}], api_key="k")
        self.assertIn("example.test", str(ctx.exception))

    def test_missing_key_raises_before_request(self) -> None:
        provider = make_provider()
        with mock.patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(LLMError) as ctx:
                chat_completion(provider, [{"role": "user", "content": "hi"}], api_key=None)
        urlopen.assert_not_called()
        self.assertIn("TEST_API_KEY", str(ctx.exception))

    def test_timeout_is_friendly(self) -> None:
        provider = make_provider(timeout_seconds=3)
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            with self.assertRaises(LLMError) as ctx:
                chat_completion(provider, [{"role": "user", "content": "hi"}], api_key="k")
        self.assertIn("3 秒", str(ctx.exception))


# ----------------------------------------------------------------- 工具防护
class ToolGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "source" / "blender").mkdir(parents=True)
        (self.repo / "source" / "blender" / "a.cc").write_text("line1\nBUG_MARKER_HERE\nline3\n", encoding="utf-8")
        (self.repo / "source" / "blender" / "big.cc").write_text("\n".join(f"l{i}" for i in range(1, 1001)), encoding="utf-8")
        (self.repo / "asset.png").write_bytes(b"\x89PNG fake")
        self.investigator = LLMInvestigator(Path(self._tmp.name), make_provider())

    def view(self):
        return SimpleNamespace(repo_path=self.repo, case_dir=self.repo, case_id="c1")

    def test_read_file_happy_path(self) -> None:
        result = self.investigator._read_file(self.repo, {"path": "source/blender/a.cc"})
        self.assertIn("2: BUG_MARKER_HERE", result)

    def test_read_file_line_clamp(self) -> None:
        result = self.investigator._read_file(self.repo, {"path": "source/blender/big.cc", "start_line": 1})
        lines = [line for line in result.splitlines() if line and line[0].isdigit()]
        self.assertLessEqual(len(lines), 400)

    def test_read_binary_rejected(self) -> None:
        result = self.investigator._read_file(self.repo, {"path": "asset.png"})
        self.assertIn("二进制", result)

    def test_path_traversal_rejected(self) -> None:
        for bad in ("../outside.txt", "a/../../outside.txt", "/etc/passwd", "C:/Windows/system32"):
            with self.assertRaises(ToolError):
                self.investigator._safe_resolve(self.repo, bad)

    def test_list_dir(self) -> None:
        result = self.investigator._list_dir(self.repo, "source/blender")
        self.assertIn("a.cc", result)
        self.assertNotIn("asset.png", result)

    def test_search_finds_marker_with_line_number(self) -> None:
        result = self.investigator._search_files(self.repo, {"pattern": "BUG_MARKER"})
        self.assertIn("source/blender/a.cc:2:", result)

    def test_search_invalid_regex_falls_back_to_literal(self) -> None:
        (self.repo / "source" / "blender" / "odd.cc").write_text("BUG_[MARKER]\n", encoding="utf-8")
        result = self.investigator._search_files(self.repo, {"pattern": "BUG_[MARKER"})
        self.assertIn("odd.cc:1:", result)

    def test_read_case_file_whitelist(self) -> None:
        case_dir = self.repo.parent / "case"
        case_dir.mkdir()
        (case_dir / "investigation.json").write_text("{}", encoding="utf-8")
        (case_dir / "secret.json").write_text("{}", encoding="utf-8")
        (case_dir / "experiments" / "E1").mkdir(parents=True)
        (case_dir / "experiments" / "E1" / "result.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.investigator._read_case_file(case_dir, "investigation.json"), "{}")
        self.assertEqual(self.investigator._read_case_file(case_dir, "experiments/E1/result.json"), "{}")
        denied = self.investigator._read_case_file(case_dir, "secret.json")
        self.assertIn("只允许读取", denied)

    def test_extract_json_with_fences(self) -> None:
        text = "说明文字\n```json\n{\"a\": 1}\n```\n结尾"
        self.assertEqual(LLMInvestigator._extract_json(text), {"a": 1})
        with self.assertRaises(ValueError):
            LLMInvestigator._extract_json("完全没有 JSON")


# ----------------------------------------------------------------- 引擎主流程
class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home_patcher = mock.patch.dict(os.environ, {"BUGCOMPASS_HOME": self._tmp.name, "BUGCOMPASS_KEY_STORE": "file"})
        home_patcher.start()
        self.addCleanup(home_patcher.stop)
        self.addCleanup(_reset_caches)
        _reset_caches()
        base = Path(self._tmp.name)
        self.repo = base / "repo"
        (self.repo / "source" / "blender").mkdir(parents=True)
        (self.repo / "source" / "blender" / "a.cc").write_text("void f() { BUG_MARKER_HERE; }\n", encoding="utf-8")
        self.case_dir = base / "ws" / "cases" / "case-1"
        self.case_dir.mkdir(parents=True)
        (self.case_dir / "issue-original.md").write_text("# 标题\n复现步骤：打开文件后崩溃。", encoding="utf-8")
        (self.case_dir / "case.json").write_text(json.dumps({"schema_version": 1, "id": "case-1"}), encoding="utf-8")
        write_investigation(self.case_dir / "investigation.json", {"schema_version": 1, "case_id": "case-1", "summary": {}, "hypotheses": [], "evidence": [], "unknowns": [], "suggested_experiments": [], "causal_graph": {"nodes": [], "edges": []}, "semantic_diff": {"status": "not_available", "summary": "", "old_rule": "", "new_rule": "", "changed_invariants": [], "affected_paths": [], "remaining_risks": [], "source_references": []}})
        self.view = SimpleNamespace(
            case_id="case-1",
            case_dir=self.case_dir,
            workspace_path=self.case_dir.parents[1],
            repo_path=self.repo,
            investigation={},
        )

    def investigator(self) -> LLMInvestigator:
        return LLMInvestigator(Path(self._tmp.name) / "repo", make_provider())

    def test_full_run_with_tool_call(self) -> None:
        investigator = self.investigator()
        tool_response = llm.ChatResponse(
            content="",
            tool_calls=[{"id": "c1", "name": "read_file", "arguments": {"path": "source/blender/a.cc"}}],
            usage={"input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 0},
            finish_reason="tool_calls",
        )
        final_response = llm.ChatResponse(
            content="```json\n" + json.dumps(valid_investigation("case-1"), ensure_ascii=False) + "\n```",
            tool_calls=[],
            usage={"input_tokens": 200, "output_tokens": 80, "cached_input_tokens": 0},
            finish_reason="stop",
        )
        messages_seen: list[list[dict]] = []

        def fake_chat(provider, msgs, **kwargs):
            messages_seen.append(msgs)
            return tool_response if len(messages_seen) == 1 else final_response

        with mock.patch.object(llm_runner, "chat_completion", side_effect=fake_chat), \
             mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-ok"}):
            result = investigator.run(self.view, action="initial")

        self.assertIsInstance(result, LLMRunResult)
        self.assertTrue(result.investigation_updated, result.error_detail)
        self.assertEqual(result.returncode, 0)
        # 结构化结果已写入 investigation.json（3 条路径）。
        data = read_investigation(self.case_dir / "investigation.json")
        self.assertEqual(len(data["hypotheses"]), 3)
        # 工具调用以角色消息回传（模型收到了文件内容）。
        tool_messages = [m for m in messages_seen[1] if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("BUG_MARKER_HERE", tool_messages[0]["content"])
        # 事件日志可被指标解析器读取。
        log_files = list((self.case_dir / "llm-runs").glob("*.jsonl"))
        self.assertEqual(len(log_files), 1)
        counts = metrics.parse_codex_log(log_files[0])
        self.assertEqual(counts["turns"], 2)
        self.assertEqual(counts["tool_calls"], 1)
        self.assertEqual(counts["input_tokens"], 300)  # 100 + 200
        # 运行摘要与指标汇总。
        summary = json.loads((self.case_dir / "llm-last-run.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["return_code"], 0)
        self.assertIn("测试服务", summary["model"])
        case_metrics = metrics.collect_case_metrics(self.case_dir)
        self.assertEqual(len(case_metrics.runs), 1)
        self.assertEqual(case_metrics.tool_calls, 1)
        self.assertEqual(case_metrics.models, ["测试服务·test-model"])

    def test_missing_key_returns_friendly_failure(self) -> None:
        investigator = self.investigator()
        env = {k: v for k, v in os.environ.items() if k != "TEST_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = investigator.run(self.view, action="initial")
        self.assertFalse(result.investigation_updated)
        self.assertEqual(result.returncode, 1)
        self.assertIn("TEST_API_KEY", result.error_detail)

    def test_cancelled_before_start(self) -> None:
        investigator = self.investigator()
        investigator.cancel()
        with mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-ok"}):
            result = investigator.run(self.view, action="initial")
        self.assertTrue(result.cancelled)
        self.assertFalse(result.investigation_updated)

    def test_invalid_final_json_fails_without_partial_write(self) -> None:
        investigator = self.investigator()
        garbage = llm.ChatResponse(content="抱歉，我无法输出 JSON。", tool_calls=[], usage={}, finish_reason="stop")
        # 第一轮垃圾 → 强制无工具终轮仍是垃圾 → 失败。
        with mock.patch.object(llm_runner, "chat_completion", return_value=garbage), \
             mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-ok"}):
            result = investigator.run(self.view, action="initial")
        self.assertFalse(result.investigation_updated)
        self.assertNotEqual(result.returncode, 0)
        # 原有 investigation.json 不被破坏。
        data = read_investigation(self.case_dir / "investigation.json")
        self.assertEqual(data["hypotheses"], [])

    def test_unknown_action_rejected(self) -> None:
        investigator = self.investigator()
        with mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-ok"}):
            result = investigator.run(self.view, action="explode")
        self.assertFalse(result.investigation_updated)


# ----------------------------------------------------------------- GUI 纯函数与设置
class EngineLabelTests(unittest.TestCase):
    def test_default_label_unchanged(self) -> None:
        presentation = codex_status_for_case(
            active_case_id=None, run_state="working", run_detail="d", viewed_case_id=None
        )
        self.assertIn("Codex 工作中", presentation.label)

    def test_custom_engine_label(self) -> None:
        presentation = codex_status_for_case(
            active_case_id=None, run_state="working", run_detail="d", viewed_case_id=None, engine_label="模型"
        )
        self.assertIn("模型 工作中", presentation.label)


class SettingsDefaultTests(IsolatedHomeTestCase):
    def test_default_engine_is_codex(self) -> None:
        self.assertEqual(load_settings()["active_engine"], "codex")


class PrivacyInvariantTests(IsolatedHomeTestCase):
    def test_event_log_and_summary_never_contain_key(self) -> None:
        """端到端跑一次引擎，确认密钥没有进任何落盘文件。"""
        base = Path(self._home.name)
        repo = base / "repo"
        (repo / "source").mkdir(parents=True)
        (repo / "source" / "a.cc").write_text("BUG_X\n", encoding="utf-8")
        case_dir = base / "ws" / "cases" / "c"
        case_dir.mkdir(parents=True)
        (case_dir / "issue-original.md").write_text("问题", encoding="utf-8")
        (case_dir / "case.json").write_text("{}", encoding="utf-8")
        view = SimpleNamespace(case_id="c", case_dir=case_dir, workspace_path=base / "ws", repo_path=repo, investigation={})
        investigator = LLMInvestigator(base, make_provider())
        final = llm.ChatResponse(content=json.dumps(valid_investigation("c"), ensure_ascii=False), tool_calls=[], usage={}, finish_reason="stop")
        with mock.patch.object(llm_runner, "chat_completion", return_value=final), \
             mock.patch.dict(os.environ, {"TEST_API_KEY": "sk-SECRET-VALUE-DO-NOT-LEAK"}):
            result = investigator.run(view, action="initial")
        self.assertTrue(result.investigation_updated)
        for path in case_dir.rglob("*"):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn("sk-SECRET-VALUE-DO-NOT-LEAK", content, f"密钥泄漏到 {path}")


class CliParserTests(unittest.TestCase):
    def test_llm_subcommands_registered(self) -> None:
        from bugcompass.cli import build_parser

        parser = build_parser()
        self.assertEqual(parser.parse_args(["llm", "list"]).llm_command, "list")
        self.assertEqual(parser.parse_args(["llm", "init-config"]).llm_command, "init-config")
        args = parser.parse_args(["llm", "test", "--provider", "deepseek"])
        self.assertEqual(args.provider, "deepseek")


class EnsureProvidersTests(IsolatedHomeTestCase):
    """零命令行初始化：首次启动自动落盘预设（绝不覆盖已有文件）。"""

    def test_ensure_creates_template_when_missing(self) -> None:
        self.assertFalse(providers_path().is_file())
        providers = ensure_providers()
        self.assertGreaterEqual(len(providers), 6)
        self.assertTrue(providers_path().is_file())  # 模板已写盘，可自行改模型/端点
        ids = {provider.id for provider in providers}
        self.assertIn("deepseek", ids)
        self.assertIn("ollama", ids)

    def test_ensure_never_overwrites_user_file(self) -> None:
        providers_path().parent.mkdir(parents=True, exist_ok=True)
        providers_path().write_text(
            json.dumps({
                "schema_version": 1,
                "providers": [
                    {"id": "custom", "label": "自定义", "base_url": "https://example.test/v1",
                     "model": "m", "api_key_env": "CUSTOM_KEY"},
                ],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        providers = ensure_providers()
        self.assertEqual([provider.id for provider in providers], ["custom"])  # 不覆盖


if __name__ == "__main__":
    unittest.main()
