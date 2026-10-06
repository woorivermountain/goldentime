import unittest

from evaluation.summarize_known_item import parse_rank, summarize_rows


class KnownItemSummaryTest(unittest.TestCase):
    def test_rank_parser_handles_misses(self):
        self.assertEqual(parse_rank("3"), 3)
        self.assertIsNone(parse_rank(">10"))

    def test_metrics_use_all_queries_as_denominator(self):
        rows = [
            {"질병구분": "A", "원본 순위": "1"},
            {"질병구분": "A", "원본 순위": "3"},
            {"질병구분": "B", "원본 순위": ">10"},
            {"질병구분": "B", "원본 순위": "10"},
        ]
        summary = summarize_rows(rows)
        overall = summary["overall"]
        self.assertEqual(overall["n"], 4)
        self.assertEqual(overall["hit_at_1"], 0.25)
        self.assertEqual(overall["hit_at_3"], 0.5)
        self.assertEqual(overall["hit_at_10"], 0.75)
        self.assertAlmostEqual(overall["mrr"], (1 + 1 / 3 + 0 + 1 / 10) / 4)


if __name__ == "__main__":
    unittest.main()
