import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).with_name("vip_activity_alert.py")
SPEC = importlib.util.spec_from_file_location("vip_activity_alert", SCRIPT)
assert SPEC and SPEC.loader
alert = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(alert)


MAPPING = """
users:
  somebody: person@example.com
vip:
  terrytangyuan: "Red Hat"
  LittleChimera: "Caffeine"
priorities:
  P1-high: 2
"""


class VipActivityAlertTest(unittest.TestCase):
    def test_extracts_only_vip_mapping(self):
        self.assertEqual(alert.vip_logins(MAPPING), {"terrytangyuan", "littlechimera"})

    def test_ignores_non_vip_actor(self):
        payload = {"sender": {"login": "somebody"}}
        self.assertIsNone(alert.build_alert(MAPPING, "issues", payload))

    def test_formats_opened_issue(self):
        payload = {
            "action": "opened",
            "sender": {"login": "terrytangyuan"},
            "repository": {"full_name": "omnigent-ai/omnigent"},
            "issue": {
                "number": 42,
                "title": "Kubernetes & sandbox",
                "html_url": "https://github.com/omnigent-ai/omnigent/issues/42",
            },
        }
        text = alert.build_alert(MAPPING, "issues", payload)
        self.assertIn("*terrytangyuan* opened", text)
        self.assertIn("issue #42: Kubernetes &amp; sandbox", text)

    def test_formats_pr_comment_case_insensitively(self):
        payload = {
            "action": "created",
            "sender": {"login": "littlechimera"},
            "issue": {
                "number": 99,
                "title": "Add auth",
                "html_url": "https://github.com/omnigent-ai/omnigent/pull/99",
                "pull_request": {},
            },
            "comment": {"html_url": "https://github.com/x/y/pull/99#issuecomment-1"},
        }
        text = alert.build_alert(MAPPING, "issue_comment", payload)
        self.assertIn("*littlechimera* commented on", text)
        self.assertIn("PR #99", text)
        self.assertIn("#issuecomment-1", text)

    def test_formats_review_state(self):
        payload = {
            "action": "submitted",
            "sender": {"login": "terrytangyuan"},
            "pull_request": {"number": 7, "title": "K8s", "html_url": "https://example/pull/7"},
            "review": {"state": "approved", "html_url": "https://example/pull/7#review"},
        }
        text = alert.build_alert(MAPPING, "pull_request_review", payload)
        self.assertIn("submitted an approved review on", text)


if __name__ == "__main__":
    unittest.main()
