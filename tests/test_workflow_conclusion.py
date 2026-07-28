"""Regression tests for the workflow conclusion API client."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = (
    Path(__file__).parents[1]
    / "scripts"
    / "workflow_conclusion"
    / "workflow_conclusion.py"
)
SPEC = importlib.util.spec_from_file_location("workflow_conclusion", SCRIPT_PATH)
WORKFLOW_CONCLUSION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = WORKFLOW_CONCLUSION
SPEC.loader.exec_module(WORKFLOW_CONCLUSION)


class FakeApi:
    """Return configured responses and expose their response headers."""

    def __init__(self, pages):
        self.calls = []
        self.pages = list(pages)
        self.recv_hdrs = {}

    def __call__(self, endpoint, method, **kwargs):
        self.calls.append((endpoint, method, kwargs))
        response, self.recv_hdrs = self.pages.pop(0)
        return response


class GhApiCompatibilityTest(unittest.TestCase):
    """Verify behavior relied upon from the pinned ghapi major version."""

    def test_direct_call_forwards_per_request_timeout(self):
        api = WORKFLOW_CONCLUSION.GhApi(
            owner="owner",
            repo="repo",
            authenticate=False,
        )
        response_headers = {"Content-Type": "application/json"}

        with patch(
            "ghapi.core.urlsend",
            return_value=(b'{"total_count": 0, "jobs": []}', response_headers),
        ) as urlsend:
            response = api(
                "/repos/owner/repo/actions/runs/123/jobs",
                "GET",
                timeout=7.5,
            )

        self.assertEqual([], response["jobs"])
        self.assertEqual(7.5, urlsend.call_args.kwargs["timeout"])


class GetJobsTest(unittest.TestCase):
    """Test bounded, rate-limit-aware workflow job pagination."""

    def request(self, max_pages=10):
        """Build a standard test request."""
        return WORKFLOW_CONCLUSION.JobsRequest(
            repository="owner/repo",
            run_id=123,
            api_version=WORKFLOW_CONCLUSION.DEFAULT_API_VERSION,
            timeout=7.5,
            max_pages=max_pages,
        )

    def test_single_page_uses_one_request(self):
        api = FakeApi(
            [
                (
                    {
                        "total_count": 2,
                        "jobs": [
                            {"name": "one", "conclusion": "success"},
                            {"name": "two", "conclusion": "skipped"},
                        ],
                    },
                    {
                        "X-RateLimit-Limit": "5000",
                        "X-RateLimit-Remaining": "4999",
                    },
                )
            ]
        )

        jobs = WORKFLOW_CONCLUSION.get_jobs(api, self.request())

        self.assertEqual(2, len(jobs))
        self.assertEqual(1, len(api.calls))
        _, method, kwargs = api.calls[0]
        self.assertEqual("GET", method)
        self.assertEqual(100, kwargs["query"]["per_page"])
        self.assertEqual(7.5, kwargs["timeout"])
        self.assertEqual(
            WORKFLOW_CONCLUSION.DEFAULT_API_VERSION,
            kwargs["headers"]["X-GitHub-Api-Version"],
        )

    def test_link_header_controls_pagination(self):
        api = FakeApi(
            [
                (
                    {
                        "total_count": 101,
                        "jobs": [
                            {"name": str(index), "conclusion": "success"}
                            for index in range(100)
                        ],
                    },
                    {
                        "Link": '<https://api.github.test/jobs?page=2>; rel="next"',
                        "X-RateLimit-Remaining": "4999",
                    },
                ),
                (
                    {
                        "total_count": 101,
                        "jobs": [{"name": "last", "conclusion": "failure"}],
                    },
                    {"X-RateLimit-Remaining": "4998"},
                ),
            ]
        )

        jobs = WORKFLOW_CONCLUSION.get_jobs(api, self.request())

        self.assertEqual(101, len(jobs))
        self.assertEqual([1, 2], [call[2]["query"]["page"] for call in api.calls])

    def test_full_page_without_next_link_stops(self):
        api = FakeApi(
            [
                (
                    {
                        "total_count": 100,
                        "jobs": [
                            {"name": str(index), "conclusion": "success"}
                            for index in range(100)
                        ],
                    },
                    {"X-RateLimit-Remaining": "4999"},
                )
            ]
        )

        jobs = WORKFLOW_CONCLUSION.get_jobs(api, self.request())

        self.assertEqual(100, len(jobs))
        self.assertEqual(1, len(api.calls))

    def test_rate_limit_exhaustion_does_not_request_another_page(self):
        api = FakeApi(
            [
                (
                    {
                        "total_count": 101,
                        "jobs": [
                            {"name": str(index), "conclusion": "success"}
                            for index in range(100)
                        ],
                    },
                    {
                        "Link": '<https://api.github.test/jobs?page=2>; rel="next"',
                        "X-RateLimit-Limit": "5000",
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1785211200",
                    },
                )
            ]
        )

        with self.assertRaises(SystemExit):
            WORKFLOW_CONCLUSION.get_jobs(api, self.request())

        self.assertEqual(1, len(api.calls))


class ConclusionTest(unittest.TestCase):
    """Verify conclusion behavior remains compatible."""

    def test_conclusion_precedence(self):
        cases = [
            ([], "skipped"),
            (["skipped", "success"], "success"),
            (["success", "cancelled"], "cancelled"),
            (["success", "failure"], "failure"),
        ]
        for conclusions, expected in cases:
            with self.subTest(conclusions=conclusions):
                actual = WORKFLOW_CONCLUSION.get_workflow_conclusion(
                    conclusions,
                    "skipped",
                    False,
                )
                self.assertEqual(expected, actual)

    def test_strict_success(self):
        self.assertEqual(
            "success",
            WORKFLOW_CONCLUSION.get_workflow_conclusion(
                ["success"],
                "skipped",
                True,
            ),
        )
        self.assertEqual(
            "failure",
            WORKFLOW_CONCLUSION.get_workflow_conclusion(
                ["success", "skipped"],
                "skipped",
                True,
            ),
        )


if __name__ == "__main__":
    unittest.main()
