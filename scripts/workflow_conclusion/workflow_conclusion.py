#!/usr/bin/env python3
"""Resolve a workflow run's conclusion from its completed jobs.

This is a drop-in replacement for technote-space/workflow-conclusion-action
v3.0.3. Run it with ``uv run`` so the project's dependencies are available.
"""

import os
import sys

from ghapi.all import GhApi, paged

CONCLUSIONS = [
    "neutral",
    "skipped",
    "success",
    "cancelled",
    "timed_out",
    "action_required",
    "failure",
]


def main():
    """Resolve a workflow run's conclusion from its completed jobs."""
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repository = os.getenv("GITHUB_REPOSITORY", "")
    api_url = os.getenv("GITHUB_API_URL", "https://api.github.com")
    run_id = os.getenv("TARGET_RUN_ID") or os.getenv("GITHUB_RUN_ID")
    set_env_name = os.getenv("SET_ENV_NAME", "WORKFLOW_CONCLUSION")
    fallback = os.getenv("FALLBACK_CONCLUSION", "skipped")
    strict_success = get_bool(os.getenv("STRICT_SUCCESS", "false"))

    if "/" not in repository:
        fail("GITHUB_REPOSITORY must be set to 'owner/repo'.")
    if not run_id or not str(run_id).isdigit():
        fail("TARGET_RUN_ID (or GITHUB_RUN_ID) must be a numeric run id.")
    if not token:
        fail("GITHUB_TOKEN (or GH_TOKEN) must be set.")

    owner, repo = repository.split("/", 1)
    api = GhApi(owner=owner, repo=repo, token=token, gh_host=api_url)

    jobs = get_jobs(api, int(run_id))
    conclusions = get_job_conclusions(jobs)
    conclusion = get_workflow_conclusion(conclusions, fallback, strict_success)

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
    if set_env_name:
        write_kv(os.getenv("GITHUB_ENV"), set_env_name, conclusion)

    print(conclusion)


def get_bool(value):
    """Match github-action-helper's loose truthiness parsing."""
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def fail(message):
    """Print an error message and terminate with a nonzero exit status."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def get_jobs(api, run_id):
    """Return all jobs for a run, following pagination."""
    jobs = []
    try:
        for page in paged(
            api.actions.list_jobs_for_workflow_run,
            run_id=run_id,
            filter="latest",
            per_page=100,
        ):
            jobs.extend(page["jobs"])
    # pylint: disable-next=broad-exception-caught
    except Exception as error:  # noqa: BLE001 - retain API context in CI logs
        fail(f"GitHub API request failed for run {run_id}: {error}")
    return jobs


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
