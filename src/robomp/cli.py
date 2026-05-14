"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
import sys

import click
import uvicorn

from robomp.config import Settings, get_settings
from robomp.db import get_database
from robomp.github_client import GitHubClient
from robomp.logging_config import configure_logging
from robomp.manual_triage import InvalidIssueRef, enqueue_manual_triage, parse_issue_ref
from robomp.queue import WorkerPool
from robomp.sandbox import SandboxManager
from robomp.server import create_app


def _settings_or_die() -> Settings:
    try:
        return get_settings()
    except Exception as exc:
        click.echo(f"configuration error: {exc}", err=True)
        sys.exit(2)


@click.group()
def main() -> None:
    """robomp control surface."""


@main.command()
def serve() -> None:
    """Run the webhook receiver + worker pool."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port, log_config=None)


@main.command()
@click.argument("issue_ref")
def triage(issue_ref: str) -> None:
    """Fetch a live issue and queue it as if a webhook arrived.

    ISSUE_REF is `owner/repo#NN`.
    """
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    try:
        repo_full, number = parse_issue_ref(issue_ref)
    except InvalidIssueRef as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    if not cfg.allows(repo_full):
        click.echo(f"refusing: {repo_full} not in ROBOMP_REPO_ALLOWLIST", err=True)
        sys.exit(2)

    async def _go() -> None:
        github = GitHubClient(cfg.github_token.get_secret_value())
        db = get_database(cfg.sqlite_path)
        delivery = await enqueue_manual_triage(
            db=db, github=github, repo_full=repo_full, number=number,
        )
        sandbox = SandboxManager(cfg.workspace_root)
        pool = WorkerPool(settings=cfg, db=db, github=github, sandbox=sandbox)
        await pool.start()
        pool.wake()
        # Drain until the event finishes.
        while True:
            await asyncio.sleep(2.0)
            row = db.get_event(delivery)
            if row is None:
                break
            if row.state in ("done", "failed", "skipped"):
                click.echo(json.dumps({"delivery": delivery, "state": row.state, "error": row.last_error}, indent=2))
                break
        await pool.stop()

    asyncio.run(_go())


@main.command()
@click.argument("delivery_id")
def replay(delivery_id: str) -> None:
    """Force a stored event back into the queue and run a one-shot drain."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    if db.get_event(delivery_id) is None:
        click.echo(f"unknown delivery: {delivery_id}", err=True)
        sys.exit(2)
    db.requeue_event(delivery_id)

    async def _drain() -> None:
        github = GitHubClient(cfg.github_token.get_secret_value())
        sandbox = SandboxManager(cfg.workspace_root)
        pool = WorkerPool(settings=cfg, db=db, github=github, sandbox=sandbox)
        await pool.start()
        pool.wake()
        while True:
            await asyncio.sleep(2.0)
            row = db.get_event(delivery_id)
            if row is None or row.state in ("done", "failed", "skipped"):
                break
        await pool.stop()
        if row is not None:
            click.echo(json.dumps({"delivery": delivery_id, "state": row.state, "error": row.last_error}, indent=2))

    asyncio.run(_drain())


@main.command()
def status() -> None:
    """Dump the issue table."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    rows = db.list_issues()
    for r in rows:
        click.echo(
            f"{r.key:<40} state={r.state:<12} pr={r.pr_number or '-'} branch={r.branch or '-'} updated={r.updated_at}"
        )


@main.command()
@click.argument("issue_key")
def cleanup(issue_key: str) -> None:
    """Force-remove the workspace for an issue (does not touch the remote)."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    row = db.get_issue(issue_key)
    if row is None:
        click.echo(f"unknown issue: {issue_key}", err=True)
        sys.exit(2)
    sandbox = SandboxManager(cfg.workspace_root)
    sandbox.remove_workspace(repo=row.repo, number=row.number)
    db.set_issue_state(issue_key, "abandoned")
    click.echo(f"cleaned up {issue_key}")


if __name__ == "__main__":
    main()
