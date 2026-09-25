"""追问引擎（右侧面板）的固定测试集：纯对话落盘、案件简报、两个引擎的 ask 路径。

不需要窗口、网络或真实 Codex —— 进程与 HTTP 全部替换成假实现。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass import llm, llm_runner, qa  # noqa: E402
from bugcompass.codex_runner import CodexRunner, CodexRunBusyError  # noqa: E402
from bugcompass.investigation import empty_investigation, write_investigation  # noqa: E402
from bugcompass.workspace import BugCompassError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def hypothesis(hypothesis_id: str, title: str, priority: str, status: str = "active") -> dict:
    return {
        "id": hypothesis_id,
        "title": title,
        "priority": priority,
        "status": status,
        "claim": f"{title} 的假设",
        "basis": [],
        "evidence_ids": [],
        "source_references": [],
        "next_step": "跑一下实验",
    }


def full_investigation() -> dict:
    data = empty_investigation("case-1")
    data["summary"]["problem"] = "脚本节点属性缺失时崩溃"
    data["summary"]["actual_behavior"] = "读取 None 属性后崩溃"
    data["hypotheses"] = [
        hypothesis("h1", "属性缺失未检查", "high"),
        hypothesis("h2", "节点缓存过期", "medium"),
        hypothesis("h3", "UI 刷新顺序", "low", "rejected"),
    ]
    data["evidence"] = [
        {"id": "e1", "kind": "fact", "statement": "NODE_OT_foo 直接读了 props.x"},
        {"id": "e2", "kind": "inference", "statement": "可能是缓存没刷新"},
    ]
    data["unknowns"] = [{"id": "u1", "question": "哪个版本开始出现的？"}]
    data["suggested_experiments"] = [
        {"id": "X1", "hypothesis_id": "h1", "command": ["blender", "-b"], "status": "completed", "result": "返回码 0"},
        {"id": "X2", "hypothesis_id": "h2", "status": "pending"},
    ]
    data["conclusion"] = {"hypothesis_id": "h1", "statement": "缺属性时没有做检查。", "recorded_at": "now"}
    return data


class QaStoreTests(unittest.TestCase):
    """对话记录必须能坏能空：追问出问题不能连累案件页打开。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.case_dir = Path(self._tmp.name)

    def test_missing_file_reads_as_empty(self) -> None:
        self.assertEqual(qa.load_turns(self.case_dir), [])

    def test_corrupt_file_reads_as_empty(self) -> None:
        qa.conversation_path(self.case_dir).write_text("{不是 JSON", encoding="utf-8")
        self.assertEqual(qa.load_turns(self.case_dir), [])

    def test_round_trip(self) -> None:
        qa.append_turn(self.case_dir, "user", "  为什么路径 ① 排在前面？  ")
        qa.append_turn(self.case_dir, "assistant", "因为它优先级最高。", engine="Codex CLI（默认）")
        turns = qa.load_turns(self.case_dir)
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["content"], "为什么路径 ① 排在前面？")
        self.assertEqual(turns[1]["engine"], "Codex CLI（默认）")
        self.assertTrue(turns[0]["at"])

    def test_malformed_entries_are_dropped_not_fatal(self) -> None:
        qa.conversation_path(self.case_dir).write_text(
            json.dumps({"turns": [{"role": "user", "content": "好的"}, {"role": "system", "content": "x"}, {"role": "user", "content": "  "}, "垃圾"]}),
            encoding="utf-8",
        )
        self.assertEqual([turn["content"] for turn in qa.load_turns(self.case_dir)], ["好的"])

    def test_rejects_empty_content_and_unknown_role(self) -> None:
        with self.assertRaises(BugCompassError):
            qa.append_turn(self.case_dir, "user", "   ")
        with self.assertRaises(BugCompassError):
            qa.append_turn(self.case_dir, "system", "你好")

    def test_keeps_only_the_newest_turns(self) -> None:
        for index in range(qa.MAX_TURNS + 5):
            qa.append_turn(self.case_dir, "user", f"第 {index} 问")
        turns = qa.load_turns(self.case_dir)
        self.assertEqual(len(turns), qa.MAX_TURNS)
        self.assertEqual(turns[-1]["content"], f"第 {qa.MAX_TURNS + 4} 问")

    def test_clear(self) -> None:
        qa.append_turn(self.case_dir, "user", "在吗")
        qa.clear_turns(self.case_dir)
        self.assertEqual(qa.load_turns(self.case_dir), [])

    def test_recent_messages_windows_and_skips_blanks(self) -> None:
        turns = [{"role": "user", "content": f"q{index}"} for index in range(10)]
        self.assertEqual([m["content"] for m in qa.recent_messages(turns, 3)], ["q7", "q8", "q9"])
        self.assertEqual(qa.recent_messages(turns, 0), [])
        self.assertEqual(qa.recent_messages([{"role": "user", "content": "  "}]), [])


class CaseBriefTests(unittest.TestCase):
    """简报是追问质量的根：路径、状态、结论、实验都得在，且不能无限膨胀。"""

    def test_includes_paths_status_and_conclusion(self) -> None:
        brief = qa.case_brief(full_investigation(), "# 标题\n打开文件后崩溃。")
        self.assertIn("①", brief)
        self.assertIn("属性缺失未检查", brief)
        self.assertIn("已被用户否定", brief)
        self.assertIn("跑一下实验", brief)
        self.assertIn("NODE_OT_foo", brief)
        self.assertIn("缺属性时没有做检查。", brief)
        self.assertIn("打开文件后崩溃", brief)
        self.assertIn("返回码 0", brief)
        self.assertIn("哪个版本开始出现的？", brief)

    def test_empty_investigation_has_a_placeholder(self) -> None:
        self.assertEqual(qa.case_brief(empty_investigation("c1")), "（案件还没有调查结果）")
        self.assertEqual(qa.case_brief({}), "（案件还没有调查结果）")

    def test_tolerates_garbage_input(self) -> None:
        brief = qa.case_brief({"hypotheses": ["不是对象", None], "evidence": "不是数组", "unknowns": None})
        self.assertIsInstance(brief, str)

    def test_truncates_to_limit(self) -> None:
        data = full_investigation()
        data["summary"]["problem"] = "很长的问题" * 500
        brief = qa.case_brief(data, "很长的题面" * 500, limit=800)
        self.assertLessEqual(len(brief), 800 + len("\n…（已截断）"))
        self.assertIn("已截断", brief)


class CodexAskTests(unittest.TestCase):
    """Codex 追问：只读、无结构化输出、不写 investigation、不计一次调查运行。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.case_dir = root / "cases" / "case-1"
        self.case_dir.mkdir(parents=True)
        self.repo = root / "blender"
        self.repo.mkdir()
        self.investigation_path = self.case_dir / "investigation.json"
        write_investigation(self.investigation_path, full_investigation())
        (self.case_dir / "issue-original.md").write_text("# 标题\n崩溃了。", encoding="utf-8")
        self.view = SimpleNamespace(
            case_id="case-1",
            case_dir=self.case_dir,
            workspace_path=root,
            repo_path=self.repo,
            investigation=full_investigation(),
        )
        self.runner = CodexRunner(ROOT, executable="/fake/codex")

    def _fake_process(self, answer: str = "因为它的优先级最高。"):
        class FakeProcess:
            pid = 12345
            stdout = iter(
                (
                    '{"type":"thread.started","thread_id":"demo"}\n',
                    '{"type":"turn.started"}\n',
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}, ensure_ascii=False) + "\n",
                    '{"type":"turn.completed"}\n',
                )
            )

            def wait(self) -> int:
                return 0

            def poll(self) -> None:
                return None

        return FakeProcess()

    def test_command_is_read_only_and_has_no_structured_output(self) -> None:
        command = self.runner.build_ask_command(self.view, "为什么？", [])
        self.assertEqual(command[:2], ["/fake/codex", "exec"])
        self.assertIn("read-only", command)
        self.assertNotIn("workspace-write", command)
        self.assertNotIn("--output-schema", command)
        self.assertNotIn("-o", command)
        self.assertNotIn("investigation.next.json", command)
        self.assertEqual(command[command.index("--cd") + 1], str(self.case_dir))
        prompt = command[-1]
        self.assertIn("追问", prompt)
        self.assertIn("为什么？", prompt)
        self.assertIn("case-1", prompt)
        self.assertIn("属性缺失未检查", prompt)

    def test_prompt_carries_the_earlier_conversation(self) -> None:
        turns = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
        ]
        prompt = self.runner.build_ask_command(self.view, "第二问", turns)[-1]
        self.assertIn("用户：第一问", prompt)
        self.assertIn("你：第一答", prompt)

    def test_ask_returns_the_answer_and_leaves_the_case_untouched(self) -> None:
        before = self.investigation_path.read_bytes()
        progress: list[str] = []
        with mock.patch("bugcompass.codex_runner.subprocess.Popen", return_value=self._fake_process()) as popen:
            answer = self.runner.ask(self.view, "为什么路径 ① 排在前面？", [], progress.append)

        self.assertEqual(answer, "因为它的优先级最高。")
        self.assertEqual(self.investigation_path.read_bytes(), before, "追问不得改动 investigation.json")
        self.assertFalse((self.case_dir / "investigation.next.json").exists())
        self.assertFalse((self.case_dir / "codex-runs").exists(), "追问不算一次调查运行")
        self.assertFalse((self.case_dir / "codex-last-run.json").exists())
        self.assertFalse((self.case_dir / ".codex-run.lock").exists(), "追问结束要释放案件锁")
        self.assertEqual(popen.call_args.kwargs["cwd"], self.case_dir)
        self.assertTrue(any("读源码回答" in message for message in progress))

    def test_ask_refuses_empty_question(self) -> None:
        with mock.patch("bugcompass.codex_runner.subprocess.Popen") as popen:
            with self.assertRaises(BugCompassError):
                self.runner.ask(self.view, "   ", [])
        popen.assert_not_called()

    def test_ask_surfaces_a_readable_error(self) -> None:
        class FailingProcess:
            pid = 12345
            stdout = iter(('{"type":"error","message":"模型暂时不可用"}\n',))

            def wait(self) -> int:
                return 1

            def poll(self) -> None:
                return None

        with mock.patch("bugcompass.codex_runner.subprocess.Popen", return_value=FailingProcess()):
            with self.assertRaises(BugCompassError) as raised:
                self.runner.ask(self.view, "为什么？", [])
        self.assertIn("模型暂时不可用", str(raised.exception))
        self.assertFalse((self.case_dir / ".codex-run.lock").exists())

    def test_ask_is_blocked_while_another_window_investigates(self) -> None:
        lock_path = self.case_dir / ".codex-run.lock"
        lock_path.write_text(
            json.dumps({"case_id": "case-1", "owner_pid": os.getpid(), "token": "other"}), encoding="utf-8"
        )
        with mock.patch("bugcompass.codex_runner.subprocess.Popen") as popen:
            with self.assertRaises(CodexRunBusyError):
                self.runner.ask(self.view, "为什么？", [])
        popen.assert_not_called()

    def test_ask_is_not_reported_as_a_cancelled_investigation(self) -> None:
        self.runner.cancel()
        with mock.patch("bugcompass.codex_runner.subprocess.Popen") as popen:
            with self.assertRaises(BugCompassError) as raised:
                self.runner.ask(self.view, "为什么？", [])
        self.assertIn("取消", str(raised.exception))
        popen.assert_not_called()


class LlmAskTests(unittest.TestCase):
    """大模型 API 追问：回答是纯文本，同样不碰 investigation.json。"""

    def setUp(self) -> None:
        self._home = tempfile.TemporaryDirectory(prefix="bugcompass-qa-test-")
        self.addCleanup(self._home.cleanup)
        home_patcher = mock.patch.dict(
            os.environ, {"BUGCOMPASS_HOME": self._home.name, "BUGCOMPASS_KEY_STORE": "file", "TEST_API_KEY": "sk-ok"}
        )
        home_patcher.start()
        self.addCleanup(home_patcher.stop)

        root = Path(self._home.name)
        self.case_dir = root / "cases" / "case-1"
        self.case_dir.mkdir(parents=True)
        self.investigation_path = self.case_dir / "investigation.json"
        write_investigation(self.investigation_path, full_investigation())
        self.repo = root / "blender"
        (self.repo / "source" / "blender").mkdir(parents=True)
        (self.repo / "source" / "blender" / "a.cc").write_text("void f() { BUG_MARKER_HERE; }\n", encoding="utf-8")
        self.view = SimpleNamespace(
            case_id="case-1",
            case_dir=self.case_dir,
            workspace_path=root,
            repo_path=self.repo,
            investigation=full_investigation(),
        )
        self.investigator = llm_runner.LLMInvestigator(root, self._provider())

    @staticmethod
    def _provider() -> llm.LLMProviderConfig:
        return llm.LLMProviderConfig(
            id="test",
            label="测试服务",
            base_url="https://example.test/v1",
            model="test-model",
            api_key_env="TEST_API_KEY",
            max_turns=4,
            timeout_seconds=5,
        )

    def test_ask_returns_plain_text_and_leaves_the_case_untouched(self) -> None:
        before = self.investigation_path.read_bytes()
        response = llm.ChatResponse(
            content="因为它的优先级最高。\n\n- source/blender/a.cc:1 是依据。",
            tool_calls=[],
            usage={"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0},
            finish_reason="stop",
        )
        with mock.patch.object(llm_runner, "chat_completion", return_value=response) as chat:
            answer = self.investigator.ask(self.view, "为什么路径 ① 排在前面？", [])

        self.assertIn("优先级最高", answer)
        self.assertEqual(self.investigation_path.read_bytes(), before, "追问不得改动 investigation.json")
        self.assertFalse((self.case_dir / "llm-runs").exists(), "追问不算一次调查运行")
        system = chat.call_args.args[1][0]["content"]
        self.assertIn("属性缺失未检查", system)
        self.assertIn("不要输出 JSON", system)

    def test_ask_can_use_read_only_tools_then_answer(self) -> None:
        tool_response = llm.ChatResponse(
            content="",
            tool_calls=[{"id": "c1", "name": "read_file", "arguments": {"path": "source/blender/a.cc"}}],
            usage={},
            finish_reason="tool_calls",
        )
        final_response = llm.ChatResponse(content="看到 BUG_MARKER_HERE。", tool_calls=[], usage={}, finish_reason="stop")
        messages_seen: list[list[dict]] = []

        def fake_chat(provider, messages, **kwargs):
            messages_seen.append(messages)
            return tool_response if len(messages_seen) == 1 else final_response

        with mock.patch.object(llm_runner, "chat_completion", side_effect=fake_chat):
            answer = self.investigator.ask(self.view, "那里到底写了什么？", [])

        self.assertEqual(answer, "看到 BUG_MARKER_HERE。")
        self.assertEqual(messages_seen[1][-1]["role"], "tool")
        self.assertIn("BUG_MARKER_HERE", messages_seen[1][-1]["content"])

    def test_ask_refuses_empty_question_without_calling_the_api(self) -> None:
        with mock.patch.object(llm_runner, "chat_completion") as chat:
            with self.assertRaises(BugCompassError):
                self.investigator.ask(self.view, "  ", [])
        chat.assert_not_called()

    def test_ask_rejects_an_empty_answer(self) -> None:
        empty = llm.ChatResponse(content="   ", tool_calls=[], usage={}, finish_reason="stop")
        with mock.patch.object(llm_runner, "chat_completion", return_value=empty):
            with self.assertRaises(BugCompassError):
                self.investigator.ask(self.view, "为什么？", [])


if __name__ == "__main__":
    unittest.main()
