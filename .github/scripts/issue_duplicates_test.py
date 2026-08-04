import unittest

from issue_duplicates import (
    AUTO_CLOSE_CONFIDENCE,
    build_duplicate_comment,
    build_search_terms,
    merge_candidates,
    validate_duplicate_decision,
)


class IssueDuplicatesTest(unittest.TestCase):
    def test_build_search_terms_prefers_meaningful_title_words(self):
        issue = {
            "title": "The runner crashes when reconnecting a session",
            "body": "Ignore this template boilerplate and unrelated detail.",
        }

        self.assertEqual(build_search_terms(issue), "runner crashes reconnecting session")

    def test_merge_candidates_keeps_older_non_duplicate_issues(self):
        candidates = merge_candidates(
            20,
            [
                {"number": 20, "title": "current"},
                {"number": 19, "title": "newer duplicate", "labels": ["duplicate"]},
                {"number": 18, "title": "open", "state": "open"},
            ],
            [
                {"number": 18, "title": "repeated"},
                {"number": 17, "title": "closed", "state": "closed"},
                {"number": 21, "title": "newer"},
            ],
        )

        self.assertEqual([candidate["number"] for candidate in candidates], [18, 17])
        self.assertEqual(candidates[0]["state"], "OPEN")

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
