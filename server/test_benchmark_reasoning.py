import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark_reasoning import (
    arm_order,
    compare_pairs,
    original_timings,
    select_photos,
    summarize,
)


class BenchmarkTests(unittest.TestCase):
    def test_sample_is_unique_and_spans_selection(self):
        sample = select_photos(list(range(230)), 30)
        self.assertEqual(len(set(sample)), 30)
        self.assertEqual((sample[0], sample[-1]), (0, 229))
        self.assertEqual(select_photos([1, 2], 1), [1])
        with self.assertRaises(ValueError):
            select_photos([1], 2)

    def test_historical_timings_are_restricted_to_job_and_sample(self):
        log = (
            "photo_review_timing job_id=job.1 photo_id=a duration_seconds=12.5\n"
            "photo_review_timing job_id=job.1 photo_id=b duration_seconds=50\n"
            "photo_review_timing job_id=jobX1 photo_id=a duration_seconds=99\n"
        )
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(Path, "read_text", return_value=log),
        ):
            self.assertEqual(
                original_timings(Path("unused"), "job.1", {"a"}), {"a": 12.5}
            )

    def test_order_is_balanced(self):
        self.assertEqual(sum(arm_order(i)[0] == "medium" for i in range(30)), 15)

    def test_failures_retries_and_missing_usage_are_not_hidden(self):
        rows = [
            {
                "photo_id": "a",
                "arm": "low",
                "duration_seconds": 40,
                "error": "failed",
                "diagnostics": {"attempts": [{}, {}]},
            }
        ]
        summary = summarize(rows)["low"]
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["picks"], 0)
        self.assertEqual(summary["rejects"], 0)
        self.assertEqual(summary["retries"], 1)
        self.assertEqual(summary["mean_seconds"], 40)
        self.assertIsNone(summary["reasoning_tokens_total"])
        self.assertEqual(compare_pairs(rows), [])

    def test_comparison_uses_current_pair_and_original_threshold(self):
        def row(arm, score, severity):
            return {
                "photo_id": "a",
                "filename": "a.jpg",
                "threshold": 56,
                "arm": arm,
                "result": {"score": score, "verdict": "YES" if score >= 56 else "NO"},
                "diagnostics": {
                    "assessment": {"composition_defect_severity": severity},
                    "composition_policy": {
                        "effective_composition_defect_severity": severity
                    },
                    "release_restriction": "unrestricted",
                },
            }

        pair = compare_pairs([row("medium", 55, "major"), row("low", 60, "minor")])[0]
        self.assertEqual(pair["score_delta"], 5)
        self.assertEqual(pair["threshold"], 56)
        self.assertTrue(pair["verdict_changed"])
        self.assertTrue(pair["effective_severity_changed"])
        self.assertFalse(pair["release_restriction_changed"])


if __name__ == "__main__":
    unittest.main()
