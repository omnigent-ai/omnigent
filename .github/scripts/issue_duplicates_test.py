import unittest

from issue_duplicates import (
    AUTO_CLOSE_CONFIDENCE,
    build_duplicate_comment,
    build_search_queries,
    extract_issue_references,
    rank_candidates,
    validate_duplicate_decision,
)


class IssueDuplicatesTest(unittest.TestCase):
    def test_build_search_queries_spread_short_phrases_across_title(self):
        issue = {
            "title": (
                "[Bug] Host runners inherit the daemon's cwd; a deleted launch dir "
                "breaks every new native session"
            ),
            "body": "Ignore this template boilerplate and unrelated detail.",
        }

        self.assertEqual(
            build_search_queries(issue),
            ["host runners", "daemon cwd", "launch dir", "native session"],
        )

    def test_build_search_queries_keep_short_technical_terms(self):
        issue = {"title": "[Feature] No Go client for the session API"}

        self.assertIn("go client", build_search_queries(issue))

    def test_extract_issue_references_supports_shorthand_and_urls(self):
        issue = {
            "number": 4000,
            "title": "Related to #3101",
            "body": (
                "See omnigent-ai/omnigent#2386 and "
                "https://github.com/omnigent-ai/omnigent/issues/3085. "
                "Ignore newer #4001 and repeated #3101."
            ),
        }

        self.assertEqual(extract_issue_references(issue), [3101, 2386, 3085])

    def test_rank_candidates_prioritizes_explicit_and_repeated_matches(self):
        issue = {
            "number": 20,
            "title": "Runner inherits host daemon cwd",
            "body": "Related implementation path: #17.",
        }
        candidates = rank_candidates(
            issue,
            [
                {"number": 20, "title": "current"},
                {"number": 19, "title": "newer duplicate", "labels": ["duplicate"]},
                {"number": 18, "title": "Runner daemon cwd", "state": "open"},
                {"number": 18, "title": "Runner daemon cwd", "state": "open"},
                {"number": 16, "title": "Merged PR", "state": "merged"},
                {"number": 21, "title": "newer"},
            ],
            [{"number": 17, "title": "Host cwd", "state": "closed"}],
        )

        self.assertEqual([candidate["number"] for candidate in candidates], [17, 18])
        self.assertTrue(candidates[0]["explicitReference"])
        self.assertEqual(candidates[1]["searchHits"], 2)

    def test_high_confidence_allowlisted_duplicate_is_closeable(self):
        result = validate_duplicate_decision(
            {
                "duplicate_decision": "duplicate",
                "duplicate_of": 12,
                "similar_issues": [],
                "duplicate_confidence": AUTO_CLOSE_CONFIDENCE,
                "duplicate_reasoning": "Both report the same reconnect crash.",
            },
            [{"number": 12}],
        )

        self.assertEqual(result["duplicate_decision"], "duplicate")
        self.assertEqual(result["duplicate_of"], 12)

    def test_low_confidence_duplicate_is_downgraded_to_similar(self):
        result = validate_duplicate_decision(
            {
                "duplicate_decision": "duplicate",
                "duplicate_of": 12,
                "similar_issues": [11],
                "duplicate_confidence": AUTO_CLOSE_CONFIDENCE - 0.01,
                "duplicate_reasoning": "The symptoms overlap.",
            },
            [{"number": 12}, {"number": 11}],
        )

        self.assertEqual(result["duplicate_decision"], "similar")
        self.assertIsNone(result["duplicate_of"])
        self.assertEqual(result["similar_issues"], [12, 11])

    def test_hallucinated_issue_numbers_are_discarded(self):
        result = validate_duplicate_decision(
            {
                "duplicate_decision": "duplicate",
                "duplicate_of": 999,
                "similar_issues": [998],
                "duplicate_confidence": 1.0,
                "duplicate_reasoning": "Exact match.",
            },
            [{"number": 12}],
        )

        self.assertEqual(result["duplicate_decision"], "none")
        self.assertIsNone(result["duplicate_of"])
        self.assertEqual(result["similar_issues"], [])
        self.assertNotEqual(result["duplicate_reasoning"], "Exact match.")

    def test_malformed_duplicate_number_is_discarded(self):
        result = validate_duplicate_decision(
            {
                "duplicate_decision": "duplicate",
                "duplicate_of": [12],
                "similar_issues": [True, 12],
                "duplicate_confidence": 1.0,
                "duplicate_reasoning": "Exact match.",
            },
            [{"number": 12}],
        )

        self.assertEqual(result["duplicate_decision"], "none")
        self.assertIsNone(result["duplicate_of"])
        self.assertEqual(result["similar_issues"], [])

    def test_similar_references_are_allowlisted_unique_and_limited(self):
        result = validate_duplicate_decision(
            {
                "duplicate_decision": "similar",
                "duplicate_of": None,
                "similar_issues": [12, 12, 11, 10, 9, 999],
                "duplicate_confidence": 0.8,
                "duplicate_reasoning": "These touch the same subsystem.",
            },
            [{"number": number} for number in [9, 10, 11, 12]],
        )

        self.assertEqual(result["duplicate_decision"], "similar")
        self.assertEqual(result["similar_issues"], [12, 11, 10])

    def test_public_comment_sanitizes_mentions_and_links(self):
        decision = validate_duplicate_decision(
            {
                "duplicate_decision": "similar",
                "similar_issues": [12],
                "duplicate_confidence": 0.8,
                "duplicate_reasoning": "Ask @admin at https://example.com about #999.",
            },
            [{"number": 12}],
        )

        comment = build_duplicate_comment(decision)

        self.assertIn("<!-- omnigent-duplicate-check -->", comment)
        self.assertIn("#12", comment)
        self.assertNotIn("@admin", comment)
        self.assertNotIn("https://example.com", comment)
        self.assertIn("leaving this issue open", comment)


if __name__ == "__main__":
    unittest.main()
