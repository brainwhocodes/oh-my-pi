"""Manually enqueue an issue as if a webhook arrived.

Shared by the `robomp triage` CLI and the dashboard's POST /api/trigger.
"""

from __future__ import annotations

import re
from typing import Any

from robomp.db import Database, issue_key
from robomp.github_client import GitHubClient

_ISSUE_REF = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^#\s]+)#(?P<number>\d+)$")


class InvalidIssueRef(ValueError):
    """Raised when the user-supplied issue reference can't be parsed."""


def parse_issue_ref(ref: str) -> tuple[str, int]:
    """Parse `owner/repo#NN` into `("owner/repo", NN)`."""
    match = _ISSUE_REF.match(ref.strip())
    if match is None:
        raise InvalidIssueRef(f"expected owner/repo#NN, got {ref!r}")
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("number"))


def manual_delivery_id(repo_full: str, number: int) -> str:
    """Stable delivery id for manually-triggered triage. Re-runs reuse it."""
    return f"manual-{repo_full.replace('/', '__')}-{number}"


async def build_issues_opened_payload(
    github: GitHubClient, repo_full: str, number: int
) -> dict[str, Any]:
    """Fetch the issue + repo metadata and synthesize an `issues.opened` payload."""
    issue = await github.get_issue(repo_full, number)
    repo = await github.get_repo(repo_full)
    return {
        "action": "opened",
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "user": {"login": issue.author},
            "labels": [{"name": lbl} for lbl in issue.labels],
        },
        "repository": {
            "full_name": repo.full_name,
            "default_branch": repo.default_branch,
            "clone_url": repo.clone_url,
            "private": repo.private,
        },
    }


async def enqueue_manual_triage(
    *, db: Database, github: GitHubClient, repo_full: str, number: int
) -> str:
    """Fetch the issue from GitHub and queue it for the worker pool.

    Returns the delivery_id. A row may already exist from a previous manual
    triage; we drop it so the fresh payload (and reset attempt counter) wins.
    """
    payload = await build_issues_opened_payload(github, repo_full, number)
    delivery = manual_delivery_id(repo_full, number)
    db.remove_event(delivery)
    db.record_event(
        delivery_id=delivery,
        event_type="issues",
        repo=repo_full,
        issue_key=issue_key(repo_full, number),
        payload=payload,
        state="queued",
    )
    return delivery


__all__ = [
    "InvalidIssueRef",
    "build_issues_opened_payload",
    "enqueue_manual_triage",
    "manual_delivery_id",
    "parse_issue_ref",
]
