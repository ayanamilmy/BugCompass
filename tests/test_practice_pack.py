from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRACTICE = ROOT / "packs" / "blender" / "practice"
SHA1 = re.compile(r"^[0-9a-f]{40}$")


class PracticePackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = json.loads((PRACTICE / "catalog.json").read_text(encoding="utf-8"))
        self.answers = json.loads((PRACTICE / "answers.json").read_text(encoding="utf-8"))

    def test_first_pack_has_exactly_ten_unique_cases(self) -> None:
        cases = self.catalog["cases"]
        self.assertEqual(len(cases), 10)
        self.assertEqual(len({case["id"] for case in cases}), 10)
        self.assertEqual(len({case["issue_id"] for case in cases}), 10)

    def test_public_catalog_does_not_reveal_fix(self) -> None:
        for case in self.catalog["cases"]:
            self.assertNotIn("fix_commit", case)
            self.assertNotIn("root_cause", case)
            self.assertRegex(case["pre_fix_commit"], SHA1)
            self.assertGreater(case["suggested_minutes"], 0)
            self.assertGreater(case["suggested_ai_runs"], 0)

    def test_every_case_has_one_separate_answer(self) -> None:
        case_ids = {case["id"] for case in self.catalog["cases"]}
        answer_ids = {answer["case_id"] for answer in self.answers["answers"]}
        self.assertEqual(answer_ids, case_ids)
        self.assertEqual(len(answer_ids), len(self.answers["answers"]))
        for answer in self.answers["answers"]:
            self.assertRegex(answer["fix_commit"], SHA1)
            self.assertTrue(answer["true_subsystems"])
            self.assertTrue(answer["relevant_files"])
            self.assertTrue(answer["evidence"])
            self.assertTrue(answer["revealing_experiment"])

    def test_rubric_covers_quality_and_resource_metrics(self) -> None:
        rubric = json.loads((PRACTICE / "rubric.json").read_text(encoding="utf-8"))
        quality_ids = {item["id"] for item in rubric["quality_dimensions"]}
        self.assertEqual(
            quality_ids,
            {
                "true_subsystem_in_top3",
                "relevant_files_found",
                "valid_evidence",
                "revealing_experiment",
                "premature_lock_in",
            },
        )
        self.assertEqual({item["id"] for item in rubric["resource_metrics"]}, {"elapsed_seconds", "ai_runs"})


if __name__ == "__main__":
    unittest.main()
