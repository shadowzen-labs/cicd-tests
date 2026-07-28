#!/usr/bin/env python3
"""Resolve a workflow run's conclusion from its completed jobs.

This is a drop-in replacement for technote-space/workflow-conclusion-action
v3.0.3. Run it with ``uv run`` so the project's dependencies are available.
"""

import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

from ghapi.all import GhApi

JOBS_PER_PAGE = 100
DEFAULT_API_VERSION = "2026-03-10"

CONCLUSIONS = [
    "neutral",
    "skipped",
    "success",
    "cancelled",
    "timed_out",
    "action_required",
    "failure",
]


@dataclass(frozen=True)
class JobsRequest:
    """Configuration for listing jobs from one workflow run."""

    repository: str
    run_id: int
    api_version: str
    timeout: float
    max_pages: int

    @property
    def endpoint(self):
        """Return the workflow jobs REST endpoint."""
        return f"/repos/{self.repository}/actions/runs/{self.run_id}/jobs"


def main():
    """Resolve a workflow run's conclusion from its completed jobs."""
    config = get_config()
    owner, repo = config["repository"].split("/", 1)
    api = GhApi(
        owner=owner,
        repo=repo,
        token=config["token"],
        gh_host=config["api_url"],
    )

    print(
        f"Inspecting {config['repository']} workflow run {config['run_id']} "
        f"(API timeout={config['api_timeout']:g}s, "
        f"max pages={config['max_pages']})",
        flush=True,
    )
    started_at = time.monotonic()
    jobs = get_jobs(
        api,
        JobsRequest(
            repository=config["repository"],
            run_id=int(config["run_id"]),
            api_version=config["api_version"],
            timeout=config["api_timeout"],
            max_pages=config["max_pages"],
        ),
    )
    print(
        f"Fetched {len(jobs)} jobs in {time.monotonic() - started_at:.2f}s",
        flush=True,
    )
    conclusions = get_job_conclusions(jobs)
    conclusion = get_workflow_conclusion(
        conclusions,
        config["fallback"],
        config["strict_success"],
    )

    print(f"::group::Jobs ({len(jobs)})")
    for job in jobs:
        print(f"  {job.get('name')}: {job.get('conclusion')}")
    print("::endgroup::")
    print("::group::Conclusions")
    print(f"  {conclusions}")
    print("::endgroup::")
    print("::group::Conclusion")
    print(f"  {conclusion}")
    print("::endgroup::")

    write_kv(os.getenv("GITHUB_OUTPUT"), "conclusion", conclusion)
    if config["set_env_name"]:
        write_kv(os.getenv("GITHUB_ENV"), config["set_env_name"], conclusion)

    print(conclusion)


def get_config():
    """Read and validate configuration from the environment."""
    config = {
        "api_timeout": get_positive_float(
            os.getenv("API_TIMEOUT_SECONDS", "30"), "API_TIMEOUT_SECONDS"
        ),
        "api_url": os.getenv("GITHUB_API_URL", "https://api.github.com"),
        "api_version": os.getenv("GITHUB_API_VERSION", DEFAULT_API_VERSION),
        "fallback": os.getenv("FALLBACK_CONCLUSION", "skipped"),
        "max_pages": get_positive_int(os.getenv("MAX_PAGES", "100"), "MAX_PAGES"),
        "repository": os.getenv("GITHUB_REPOSITORY", ""),
        "run_id": os.getenv("TARGET_RUN_ID") or os.getenv("GITHUB_RUN_ID"),
        "set_env_name": os.getenv("SET_ENV_NAME", "WORKFLOW_CONCLUSION"),
        "strict_success": get_bool(os.getenv("STRICT_SUCCESS", "false")),
        "token": os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"),
    }
    if "/" not in config["repository"]:
        fail("GITHUB_REPOSITORY must be set to 'owner/repo'.")
    if not config["run_id"] or not str(config["run_id"]).isdigit():
        fail("TARGET_RUN_ID (or GITHUB_RUN_ID) must be a numeric run id.")
    if not config["token"]:
        fail("GITHUB_TOKEN (or GH_TOKEN) must be set.")
    return config


def get_bool(value):
    """Parse common truthy forms used by environment variables."""
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def get_positive_float(value, name):
    """Parse a positive floating-point configuration value."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        fail(f"{name} must be a positive number, got {value!r}.")
    if parsed <= 0:
        fail(f"{name} must be a positive number, got {value!r}.")
    return parsed


def get_positive_int(value, name):
    """Parse a positive integer configuration value."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        fail(f"{name} must be a positive integer, got {value!r}.")
    if parsed <= 0:
        fail(f"{name} must be a positive integer, got {value!r}.")
    return parsed


def fail(message):
    """Print an error message and terminate with a nonzero exit status."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def get_jobs(api, request):
    """Return all jobs, stopping when the API returns the final page."""
    jobs = []
    started_at = time.monotonic()

    for page_number in range(1, request.max_pages + 1):
        print(
            f"Requesting jobs page {page_number} "
            f"({len(jobs)} jobs collected so far)...",
            flush=True,
        )
        response, page_jobs, response_headers, request_elapsed = fetch_jobs_page(
            api,
            request,
            page_number,
        )
        jobs.extend(page_jobs)
        total_elapsed = time.monotonic() - started_at
        reported_total = response.get("total_count", "unknown")
        print(
            f"Received {len(page_jobs)} jobs from page {page_number} in "
            f"{request_elapsed:.2f}s ({len(jobs)} collected, "
            f"reported total={reported_total}, total elapsed={total_elapsed:.2f}s, "
            f"{format_rate_limit(response_headers)})",
            flush=True,
        )

        link_header = get_header(response_headers, "link")
        if link_header is not None and 'rel="next"' not in link_header:
            return jobs
        if link_header is None and (
            len(page_jobs) < JOBS_PER_PAGE
            or (isinstance(reported_total, int) and len(jobs) >= reported_total)
        ):
            return jobs
        if get_header(response_headers, "x-ratelimit-remaining") == "0":
            fail(
                f"GitHub API rate limit exhausted after page {page_number}; "
                f"another page is required. {format_rate_limit(response_headers)}"
            )

    return fail(
        f"Reached MAX_PAGES={request.max_pages} while listing jobs for run "
        f"{request.run_id}; "
        f"collected {len(jobs)} jobs in {time.monotonic() - started_at:.2f}s."
    )


def fetch_jobs_page(api, request, page_number):
    """Request and validate one page of workflow jobs."""
    request_started_at = time.monotonic()
    try:
        # Call GhApi.__call__ directly: ghapi 1.x forwards this method's
        # per-request timeout to urlsend. Generated endpoint methods do not.
        response = api(
            request.endpoint,
            "GET",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": request.api_version,
            },
            query={
                "filter": "latest",
                "page": page_number,
                "per_page": JOBS_PER_PAGE,
            },
            timeout=request.timeout,
        )
    # pylint: disable-next=broad-exception-caught
    except Exception as error:  # noqa: BLE001 - retain API context in CI logs
        elapsed = time.monotonic() - request_started_at
        error_headers = getattr(error, "headers", {}) or {}
        retry_after = get_header(error_headers, "retry-after")
        retry_context = f", retry after={retry_after}s" if retry_after else ""
        fail(
            f"GitHub API request failed for run {request.run_id}, "
            f"page {page_number}, "
            f"after {elapsed:.2f}s: {error}; "
            f"{format_rate_limit(error_headers)}{retry_context}"
        )

    response_headers = dict(getattr(api, "recv_hdrs", {}) or {})
    page_jobs = response.get("jobs", [])
    if not isinstance(page_jobs, Sequence) or isinstance(page_jobs, (str, bytes)):
        fail(
            f"GitHub API returned an invalid jobs payload for run "
            f"{request.run_id}, page {page_number}."
        )
    return (
        response,
        list(page_jobs),
        response_headers,
        time.monotonic() - request_started_at,
    )


def get_header(headers, name):
    """Return a response header using case-insensitive lookup."""
    expected = name.lower()
    for key, value in headers.items():
        if str(key).lower() == expected:
            return str(value)
    return None


def format_rate_limit(headers):
    """Format rate-limit response headers without another API request."""
    remaining = get_header(headers, "x-ratelimit-remaining")
    limit = get_header(headers, "x-ratelimit-limit")
    reset = get_header(headers, "x-ratelimit-reset")
    resource = get_header(headers, "x-ratelimit-resource")
    if remaining is None and limit is None:
        return "rate limit headers unavailable"
    return (
        f"rate limit remaining={remaining or 'unknown'}/{limit or 'unknown'}, "
        f"reset={reset or 'unknown'}, resource={resource or 'unknown'}"
    )


def get_job_conclusions(jobs):
    """Return distinct, non-null conclusions in first-seen order."""
    seen = []
    for job in jobs:
        conclusion = job.get("conclusion")
        if conclusion is not None and conclusion not in seen:
            seen.append(conclusion)
    return seen


def get_workflow_conclusion(conclusions, fallback, strict_success):
    """Return the workflow conclusion from job conclusions."""
    if not conclusions:
        return fallback
    if strict_success:
        return (
            "success"
            if all(conclusion == "success" for conclusion in conclusions)
            else "failure"
        )
    ranked = [conclusion for conclusion in CONCLUSIONS if conclusion in conclusions]
    return ranked[-1] if ranked else fallback


def write_kv(path, key, value):
    """Append a key/value pair to a GitHub Actions environment file."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
