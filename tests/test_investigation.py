"""调查数据层：展示排序等纯函数的固定测试集（不需要窗口、网络或真实 Blender 仓库）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bugcompass.investigation import order_hypotheses  # noqa: E402


def _hypothesis(hypothesis_id: str, priority: str, status: str = "active") -> dict:
    return {
        "id": hypothesis_id,
        "title": hypothesis_id,
        "priority": priority,
        "status": status,
        "claim": "",
        "basis": [],
        "evidence_ids": [],
        "source_references": [],
        "next_step": "",
    }


class OrderHypothesesTests(unittest.TestCase):
    """三条路径必须按 高→中→低 排列、已否定沉底——界面编号 ①②③ 直接依赖这个顺序。"""

    def test_sorted_by_priority(self) -> None:
        items = [_hypothesis("c", "low"), _hypothesis("a", "high"), _hypothesis("b", "medium")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["a", "b", "c"])

    def test_rejected_sinks_even_when_high(self) -> None:
        items = [_hypothesis("a", "high", "rejected"), _hypothesis("b", "low")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["b", "a"])

    def test_only_rejected_sinks(self) -> None:
        items = [_hypothesis("weakened", "low", "weakened"), _hypothesis("supported", "high", "supported")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["supported", "weakened"])

    def test_stable_within_same_priority(self) -> None:
        items = [_hypothesis("first", "high"), _hypothesis("second", "high"), _hypothesis("third", "high")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["first", "second", "third"])

    def test_unknown_priority_goes_last(self) -> None:
        items = [_hypothesis("mystery", "unknown"), _hypothesis("known", "low")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["known", "mystery"])

    def test_missing_priority_goes_last(self) -> None:
        items = [{"id": "no-priority"}, _hypothesis("known", "low")]
        self.assertEqual([item["id"] for item in order_hypotheses(items)], ["known", "no-priority"])

    def test_empty_list(self) -> None:
        self.assertEqual(order_hypotheses([]), [])

    def test_original_order_is_not_mutated(self) -> None:
        items = [_hypothesis("c", "low"), _hypothesis("a", "high")]
        order_hypotheses(items)
        self.assertEqual([item["id"] for item in items], ["c", "a"])


if __name__ == "__main__":
    unittest.main()
