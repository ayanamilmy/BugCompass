"""AI Issue 筛选（issue_scout）的固定测试集。全部网络与模型调用都 mock，测试不联网。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass import issue_scout, llm  # noqa: E402
from bugcompass.issue_scout import (  # noqa: E402
    IssueRecord,
    IssueScore,
    ScoutError,
    build_scoring_messages,
    export_markdown,
    fetch_open_issues,
    filter_issues,
    issue_to_bug_text,
    load_last_scan,
    save_scan,
    scan_records_from_cache,
    scan_scores_from_cache,
    score_issues,
)

import unittest


def _reset_caches() -> None:
    from bugcompass import resources

    resources.bundle_root.cache_clear()
    resources.data_root.cache_clear()
    resources.user_root.cache_clear()


class IsolatedHomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._home = TemporaryDirectory(prefix="bugcompass-scout-test-")
        self.addCleanup(self._home.cleanup)
        patcher = mock.patch.dict(os.environ, {"BUGCOMPASS_HOME": self._home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_reset_caches)
        _reset_caches()


def api_issue(number: int, labels: list[str], title: str = "t", body: str = "b") -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://projects.blender.org/blender/blender/issues/{number}",
        "body": body,
        "labels": [{"name": name} for name in labels],
        "created_at": "2026-09-01T10:00:00+02:00",
        "comments": 2,
    }


def record(number: int, labels: list[str] | None = None, **kwargs) -> IssueRecord:
    values = dict(
        number=number,
        title=f"issue {number}",
        url=f"https://example.test/{number}",
        body="复现步骤：打开文件",
        labels=labels if labels is not None else ["Type/Bug", "Module/Core"],
        created_at="2026-09-01T10:00:00+02:00",
        comments=1,
    )
    values.update(kwargs)
    return IssueRecord(**values)


class FetchTests(IsolatedHomeTestCase):
    def test_fetch_pagination_and_parsing(self) -> None:
        page1 = [api_issue(i, ["Type/Bug", "Module/Core"]) for i in range(1, 51)]
        page2 = [api_issue(i, ["Type/Report", "Module/Modeling"]) for i in range(51, 61)]
        pages = [json.dumps(page1).encode(), json.dumps(page2).encode()]

        class Response:
            def __init__(self, body: bytes):
                self._body = body

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return self._body

        urls_requested: list[str] = []

        def fake_urlopen(request, timeout=None):
            urls_requested.append(request.full_url)
            return Response(pages[0] if "page=1" in request.full_url else pages[1])

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            records = fetch_open_issues(limit=60, progress=lambda _t: None)
        self.assertEqual(len(records), 60)
        self.assertEqual(records[0].number, 1)
        self.assertEqual(records[59].number, 60)
        self.assertEqual(records[0].module_labels, ["Module/Core"])
        self.assertEqual(len(urls_requested), 2)  # 第二页返回 <50 即停

    def test_fetch_hard_cap(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=ScoutError("stop")):
            with self.assertRaises(ScoutError):
                fetch_open_issues(limit=9999)  # 先被钳制，再立刻失败于网络

    def test_network_error_is_friendly(self) -> None:
        import socket as socket_module
        import urllib.error

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(socket_module.gaierror("no dns"))):
            with self.assertRaises(ScoutError) as ctx:
                fetch_open_issues()
        self.assertIn("projects.blender.org", str(ctx.exception))

    def test_from_api_defensive(self) -> None:
        self.assertIsNone(IssueRecord.from_api("not a dict"))
        self.assertIsNone(IssueRecord.from_api({"number": 0}))
        raw = api_issue(9, [])
        raw["body"] = None
        raw["labels"] = None
        parsed = IssueRecord.from_api(raw)
        self.assertEqual(parsed.body, "")
        self.assertEqual(parsed.labels, [])


class FilterTests(unittest.TestCase):
    def test_module_filter(self) -> None:
        records = [
            record(1, ["Type/Bug", "Module/Core"]),
            record(2, ["Type/Bug", "Module/Render & Cycles"]),
            record(3, ["Type/Bug"]),
        ]
        kept = filter_issues(records, modules=["Module/Render & Cycles"])
        self.assertEqual([r.number for r in kept], [2])
        all_kept = filter_issues(records, modules=[])
        self.assertEqual(len(all_kept), 3)

    def test_type_filter_and_status_exclusion(self) -> None:
        records = [
            record(1, ["Type/Bug", "Status/Confirmed"]),
            record(2, ["Type/Report"]),
            record(3, ["Type/Bug", "Status/Archived"]),
            record(4, ["Type/Bug", "Status/Duplicate"]),
        ]
        kept = filter_issues(records, types=["Type/Bug"])
        self.assertEqual([r.number for r in kept], [1])


class ScoringTests(IsolatedHomeTestCase):
    def _provider(self) -> llm.LLMProviderConfig:
        return llm.LLMProviderConfig(
            id="test", label="测试", base_url="https://example.test/v1",
            model="m", api_key_env="",
        )

    def test_score_batches_and_merge(self) -> None:
        records = [record(i) for i in range(1, 13)]  # 12 个 → 2 批
        calls: list[list[dict]] = []

        def fake_chat(provider, messages, **kwargs):
            calls.append(messages)
            numbers = [r.number for r in records][len(calls) * 10 - 10 : len(calls) * 10]
            if len(numbers) > 10:
                numbers = numbers[:10]
            content = json.dumps(
                {"results": [{"number": n, "score": 8, "difficulty": "进阶", "reason": "复现清晰"} for n in numbers]},
                ensure_ascii=False,
            )
            return llm.ChatResponse(content=content, tool_calls=[], usage={}, finish_reason="stop")

        with mock.patch.object(issue_scout, "chat_completion", side_effect=fake_chat):
            scores = score_issues(self._provider(), records, progress=lambda _t: None)
        self.assertEqual(len(scores), 12)
        self.assertEqual(scores[5].score, 8)
        self.assertEqual(scores[5].difficulty, "进阶")
        self.assertEqual(len(calls), 2)

    def test_bad_batch_does_not_kill_scan(self) -> None:
        records = [record(i) for i in range(1, 11)]

        def fake_chat(provider, messages, **kwargs):
            return llm.ChatResponse(content="评分失败，无法输出", tool_calls=[], usage={}, finish_reason="stop")

        with mock.patch.object(issue_scout, "chat_completion", side_effect=fake_chat):
            scores = score_issues(self._provider(), records)
        self.assertEqual(len(scores), 10)
        self.assertTrue(all(entry.score is None for entry in scores.values()))
        self.assertIn("评分失败", scores[1].reason)

    def test_score_clamped_and_difficulty_normalized(self) -> None:
        records = [record(1)]
        content = json.dumps({"results": [{"number": 1, "score": 99, "difficulty": "超难", "reason": "x"}]})
        with mock.patch.object(issue_scout, "chat_completion", return_value=llm.ChatResponse(content=content, tool_calls=[], usage={}, finish_reason="stop")):
            scores = score_issues(self._provider(), records)
        self.assertEqual(scores[1].score, 10)  # 钳制到 1-10
        self.assertEqual(scores[1].difficulty, "未知")  # 非法难度归一

    def test_custom_prompt_file_is_appended(self) -> None:
        custom = self.user_custom_prompt()
        custom.write_text("我偏爱几何节点相关的崩溃。", encoding="utf-8")
        messages = build_scoring_messages([record(1)])
        self.assertIn("用户的补充评估标准", messages[0]["content"])
        self.assertIn("几何节点", messages[0]["content"])

    def test_custom_prompt_missing_is_noop(self) -> None:
        messages = build_scoring_messages([record(1)])
        self.assertNotIn("用户的补充评估标准", messages[0]["content"])

    def user_custom_prompt(self) -> Path:
        from bugcompass.issue_scout import custom_prompt_path

        return custom_prompt_path()


class CacheTests(IsolatedHomeTestCase):
    def test_roundtrip(self) -> None:
        records = [record(1), record(2, ["Type/Report", "Module/Modeling"])]
        scores = {1: IssueScore(1, 9, "入门", "好上手"), 2: IssueScore(2, None, "未知", "失败")}
        save_scan(records, scores, {"modules": [], "types": [], "limit": 2, "engine": "test"})
        cache = load_last_scan()
        self.assertIsNotNone(cache)
        restored_records = scan_records_from_cache(cache)
        restored_scores = scan_scores_from_cache(cache)
        self.assertEqual(restored_records[1].title, "issue 2")
        self.assertEqual(restored_scores[1].score, 9)
        self.assertIsNone(restored_scores[2].score)

    def test_missing_cache_returns_none(self) -> None:
        self.assertIsNone(load_last_scan())


class ExportAndTextTests(IsolatedHomeTestCase):
    def test_issue_to_bug_text(self) -> None:
        rec = record(164285, ["Type/Bug", "Module/Python API"], title="处理器崩溃", body="打开 .blend 后崩溃")
        text = issue_to_bug_text(rec, IssueScore(164285, 8, "进阶", "路径清晰"))
        self.assertIn("# 处理器崩溃", text)
        self.assertIn("https://example.test/164285", text)
        self.assertIn("8/10", text)
        self.assertIn("路径清晰", text)
        self.assertIn("AI 挑选 Issue", text)

    def test_export_markdown_ranking(self) -> None:
        from pathlib import Path as P

        records = [record(1), record(2), record(3)]
        scores = {1: IssueScore(1, 5, "入门", "一般"), 2: IssueScore(2, 9, "进阶", "很好"), 3: IssueScore(3, None, "未知", "未评")}
        target = P(self._home.name) / "out.md"
        export_markdown(records, scores, target)
        content = target.read_text(encoding="utf-8")
        # 高分在前，未评分垫底。
        self.assertLess(content.index("issue 2"), content.index("issue 1"))
        self.assertLess(content.index("issue 1"), content.index("issue 3"))
        self.assertIn("9/10", content)


class FallbackModulesTests(unittest.TestCase):
    def test_fallback_list_is_real_taxonomy(self) -> None:
        # 实测标签（2026-09）：确保兜底列表没有拼写错误的关键模块。
        for must in ("Module/Nodes & Physics", "Module/Render & Cycles", "Module/User Interface", "Module/Core"):
            self.assertIn(must, issue_scout.FALLBACK_MODULES)


if __name__ == "__main__":
    unittest.main()
