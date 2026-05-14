"""Host tools exposed to the agent through `omp_rpc.host_tool`.

The agent uses these for any side effect that touches GitHub, the
reproduction transcript store, or the orchestrator's bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from omp_rpc import HostTool, HostToolContext, RpcCommandError, host_tool

from robomp.db import Database, issue_key
from robomp.github_client import GitHubClient, GitHubError, IssueInfo, RepoInfo
from robomp.sandbox import Workspace

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ToolBindings:
    """Per-task closure that the host tools capture."""

    db: Database
    github: GitHubClient
    repo: RepoInfo
    issue: IssueInfo
    workspace: Workspace
    loop: asyncio.AbstractEventLoop
    author_name: str
    author_email: str

    @property
    def issue_key(self) -> str:
        return issue_key(self.issue.repo, self.issue.number)


def _run_coro(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """Block the agent thread until an async call completes on the worker loop."""
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def _audit(bindings: ToolBindings, name: str, args: Mapping[str, Any], result: Any | None = None,
           error: str | None = None) -> None:
    bindings.db.log_tool_call(
        issue_key=bindings.issue_key,
        tool=name,
        args=args,
        result=result if isinstance(result, Mapping) else ({"value": result} if result is not None else None),
        error=error,
    )


def _raise_command(message: str) -> Any:
    raise RpcCommandError(message, error={"message": message})


# ---------- gh_post_comment ----------
def _build_post_comment(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        body = args.get("body")
        if not isinstance(body, str) or not body.strip():
            _raise_command("gh_post_comment requires a non-empty 'body'.")
        target_number = bindings.issue.number
        if isinstance(args.get("number"), int):
            target_number = int(args["number"])
        try:
            comment = _run_coro(
                bindings.loop,
                bindings.github.post_comment(bindings.repo.full_name, target_number, body),
            )
        except GitHubError as exc:
            _audit(bindings, "gh_post_comment", args, error=str(exc))
            _raise_command(f"GitHub rejected comment: {exc.status} {exc.message}")
        _audit(bindings, "gh_post_comment", args, result={"comment_id": comment.id})
        return f"comment posted: id={comment.id}"

    return host_tool(
        name="gh_post_comment",
        description="Post a comment on the originating issue or PR thread.",
        parameters={
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Markdown body of the comment."},
                "number": {
                    "type": "integer",
                    "description": "Optional issue/PR number. Defaults to the originating issue.",
                },
            },
            "required": ["body"],
            "additionalProperties": False,
        },
        execute=execute,
    )


# ---------- gh_push_branch ----------
def _build_push_branch(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        branch = str(args.get("branch") or bindings.workspace.branch)
        if branch != bindings.workspace.branch:
            _raise_command(
                f"refusing to push: branch={branch!r} does not match workspace branch "
                f"{bindings.workspace.branch!r}."
            )
        repo_dir = str(bindings.workspace.repo_dir)
        # Re-pin the configured identity right before push (cheap; idempotent).
        subprocess.run(
            ["git", "config", "user.email", bindings.author_email],
            cwd=repo_dir, check=False, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", bindings.author_name],
            cwd=repo_dir, check=False, capture_output=True, text=True,
        )
        # Verify there's at least one commit on the branch.
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        if rev.returncode != 0:
            _audit(bindings, "gh_push_branch", args, error=rev.stderr.strip())
            _raise_command(f"git rev-parse failed: {rev.stderr.strip()}")

        # Identity gate: every commit between the base branch and HEAD must
        # carry the configured author. Refuse to push otherwise so the agent
        # fixes it (`git commit --amend --reset-author --no-edit`).
        base = bindings.repo.default_branch
        identities = subprocess.run(
            ["git", "log", "--format=%H%x09%ae%x09%an", f"origin/{base}..HEAD"],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        offending: list[str] = []
        for line in (identities.stdout or "").strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            sha, email, name = parts[0], parts[1], parts[2]
            if email != bindings.author_email or name != bindings.author_name:
                offending.append(f"{sha[:12]} {name} <{email}>")
        if offending:
            details = "\n  ".join(offending)
            msg = (
                "refusing to push: commit author identity mismatch. "
                f"Expected `{bindings.author_name} <{bindings.author_email}>`. "
                f"Offending commits:\n  {details}\n"
                "Amend each commit with `git commit --amend --reset-author --no-edit` "
                "(or rebase with `git rebase -i origin/" + base + " --exec "
                "'git commit --amend --reset-author --no-edit'`) and try again."
            )
            _audit(bindings, "gh_push_branch", args, error=msg)
            _raise_command(msg)
        # Working-tree cleanliness gate. Any uncommitted change (edits the agent
        # forgot to `git add && git commit`, files dropped by `bun install`, etc.)
        # would silently land in the PR review delta but not in the commit history.
        # Reject so the agent either commits or stashes them.
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        if status.stdout.strip():
            dirty = "\n  ".join(status.stdout.strip().splitlines())
            msg = (
                "refusing to push: working tree is dirty.\n  "
                f"{dirty}\n"
                "Commit (or `git stash`) every change before pushing — anything in the "
                "worktree that isn't in a commit won't appear in the PR."
            )
            _audit(bindings, "gh_push_branch", args, error=msg)
            _raise_command(msg)

        # Lint/format gate. Best-effort: run the project's `fix` script (typically
        # `bun run fix:tools` → biome). If the script exists, succeeds, AND
        # produces changes, the commits aren't formatted — refuse so the agent
        # amends them. If the script isn't available, we silently proceed (other
        # repos may not define it).
        if (bindings.workspace.repo_dir / "package.json").exists():
            for script in ("fix:tools", "fix"):
                proc_fix = subprocess.run(
                    ["bun", "run", "--silent", script],
                    cwd=repo_dir, capture_output=True, text=True, check=False,
                    timeout=180,
                )
                if proc_fix.returncode != 0:
                    # Script not defined / bun missing / install missing — try next or skip.
                    continue
                status2 = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=repo_dir, capture_output=True, text=True, check=False,
                )
                if status2.stdout.strip():
                    dirty = "\n  ".join(status2.stdout.strip().splitlines())
                    msg = (
                        f"refusing to push: `bun run {script}` produced unformatted-file "
                        "changes that aren't in any commit. The commit history won't pass "
                        f"CI as-is. Diff:\n  {dirty}\n"
                        "Amend the offending commit(s) with the formatter output: "
                        "`git add -A && git commit --amend --no-edit --reset-author`."
                    )
                    _audit(bindings, "gh_push_branch", args, error=msg)
                    _raise_command(msg)
                break  # successful and clean — done


        proc = subprocess.run(
            ["git", "push", "--set-upstream", "origin", branch],
            cwd=repo_dir, capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            _audit(bindings, "gh_push_branch", args, error=err)
            _raise_command(f"git push failed: {err}")
        _audit(bindings, "gh_push_branch", args, result={"head": rev.stdout.strip(), "branch": branch})
        return (
            f"pushed {branch} at {rev.stdout.strip()[:12]} "
            f"as {bindings.author_name} <{bindings.author_email}>"
        )

    return host_tool(
        name="gh_push_branch",
        description="Push the workspace branch to origin. Uses credentials configured by the orchestrator.",
        parameters={
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Optional explicit branch name; defaults to the workspace branch.",
                },
            },
            "additionalProperties": False,
        },
        execute=execute,
    )


# ---------- gh_open_pr ----------
def _build_open_pr(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        title = args.get("title")
        body = args.get("body")
        if not isinstance(title, str) or not title.strip():
            _raise_command("gh_open_pr requires a non-empty 'title'.")
        if not isinstance(body, str) or not body.strip():
            _raise_command("gh_open_pr requires a non-empty 'body'.")
        for required in ("## Repro", "## Cause", "## Fix", "## Verification"):
            if required not in body:
                _raise_command(
                    f"PR body missing required section header {required!r}. "
                    "Follow the template in the system prompt verbatim."
                )
        # Auto-close keyword. GitHub closes the linked issue on merge only when
        # one of `Fixes / Closes / Resolves #<n>` is present in the PR body.
        n = bindings.issue.number
        accepted = [f"{kw} #{n}" for kw in ("Fixes", "Closes", "Resolves", "fixes", "closes", "resolves")]
        if not any(form in body for form in accepted):
            _raise_command(
                f"PR body must include `Fixes #{n}` (or `Closes #{n}` / `Resolves #{n}`) so "
                "GitHub auto-closes the issue when the PR merges. Put it at the end of the "
                "Verification section per the template."
            )
        # Make sure the branch is pushed (idempotent).
        push_proc = subprocess.run(
            ["git", "push", "--set-upstream", "origin", bindings.workspace.branch],
            cwd=str(bindings.workspace.repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        if push_proc.returncode != 0:
            err = (push_proc.stderr or push_proc.stdout).strip()
            _audit(bindings, "gh_open_pr", args, error=err)
            _raise_command(f"branch push failed: {err}")
        base = args.get("base") or bindings.repo.default_branch
        try:
            pr = _run_coro(
                bindings.loop,
                bindings.github.open_pull_request(
                    repo=bindings.repo.full_name,
                    head=bindings.workspace.branch,
                    base=str(base),
                    title=title,
                    body=body,
                    draft=bool(args.get("draft", False)),
                ),
            )
        except GitHubError as exc:
            _audit(bindings, "gh_open_pr", args, error=str(exc))
            _raise_command(f"GitHub rejected PR: {exc.status} {exc.message}")
        bindings.db.set_issue_pr(bindings.issue_key, pr.number)
        bindings.db.set_issue_state(bindings.issue_key, "opened")
        artifact = bindings.workspace.artifacts_dir / "pr.json"
        artifact.write_text(
            json.dumps(
                {
                    "repo": pr.repo,
                    "number": pr.number,
                    "url": pr.html_url,
                    "head": pr.head_ref,
                    "base": pr.base_ref,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        _audit(bindings, "gh_open_pr", args, result={"pr_number": pr.number, "url": pr.html_url})
        return f"opened #{pr.number}: {pr.html_url}"

    return host_tool(
        name="gh_open_pr",
        description="Open a pull request from the workspace branch using the PR body template.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {
                    "type": "string",
                    "description": (
                        "Markdown body. MUST include the four template sections in order: "
                        "`## Repro`, `## Cause`, `## Fix`, `## Verification`."
                    ),
                },
                "base": {"type": "string", "description": "Override the base branch (default: repo default)."},
                "draft": {"type": "boolean", "default": False},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        execute=execute,
    )


# ---------- gh_request_review ----------
def _build_request_review(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        reviewers = args.get("reviewers") or []
        assignees = args.get("assignees") or []
        if not isinstance(reviewers, list) or not isinstance(assignees, list):
            _raise_command("gh_request_review expects 'reviewers' and 'assignees' to be arrays of logins.")
        issue_row = bindings.db.get_issue(bindings.issue_key)
        pr_number = issue_row.pr_number if issue_row else None
        if pr_number is None:
            _raise_command("no PR recorded for this issue yet; call gh_open_pr first.")
        try:
            if reviewers:
                _run_coro(
                    bindings.loop,
                    bindings.github.request_reviewers(
                        repo=bindings.repo.full_name,
                        pr_number=pr_number,
                        reviewers=[str(r) for r in reviewers],
                    ),
                )
            if assignees:
                _run_coro(
                    bindings.loop,
                    bindings.github.add_assignees(
                        bindings.repo.full_name,
                        pr_number,
                        [str(a) for a in assignees],
                    ),
                )
        except GitHubError as exc:
            _audit(bindings, "gh_request_review", args, error=str(exc))
            _raise_command(f"GitHub rejected review request: {exc.status} {exc.message}")
        _audit(bindings, "gh_request_review", args, result={"pr": pr_number})
        return f"updated review/assignees on #{pr_number}"

    return host_tool(
        name="gh_request_review",
        description="Request reviewers and/or add assignees on the open PR.",
        parameters={
            "type": "object",
            "properties": {
                "reviewers": {"type": "array", "items": {"type": "string"}},
                "assignees": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        execute=execute,
    )


# ---------- repro_record ----------
def _build_repro_record(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        title = args.get("title")
        command = args.get("command")
        output = args.get("output")
        exit_code = args.get("exit_code")
        if not isinstance(title, str) or not title.strip():
            _raise_command("repro_record requires a non-empty 'title'.")
        if not isinstance(command, str) or not command.strip():
            _raise_command("repro_record requires a non-empty 'command'.")
        if not isinstance(output, str):
            _raise_command("repro_record requires 'output' (may be empty string).")
        if not isinstance(exit_code, int):
            _raise_command("repro_record requires an integer 'exit_code'.")
        bindings.workspace.repro_dir.mkdir(parents=True, exist_ok=True)
        slug = "".join(c if c.isalnum() else "-" for c in title.lower()).strip("-")[:48] or "repro"
        ts = int(time.time())
        target = bindings.workspace.repro_dir / f"{ts}-{slug}.md"
        target.write_text(
            f"# {title}\n\n"
            f"- exit_code: {exit_code}\n"
            f"- command:\n\n```\n{command}\n```\n\n"
            f"## Output\n\n```\n{output}\n```\n",
            encoding="utf-8",
        )
        _audit(bindings, "repro_record", args, result={"path": str(target.relative_to(bindings.workspace.root))})
        rel = target.relative_to(bindings.workspace.root)
        return f"saved transcript to {rel}"

    return host_tool(
        name="repro_record",
        description="Persist a reproduction transcript (command, output, exit code) for the issue.",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "command": {"type": "string"},
                "output": {"type": "string"},
                "exit_code": {"type": "integer"},
                "reproduced": {"type": "boolean", "description": "True when the recorded run demonstrates the bug."},
            },
            "required": ["title", "command", "output", "exit_code"],
            "additionalProperties": False,
        },
        execute=execute,
    )


# ---------- mark_unable_to_reproduce ----------
def _build_mark_unable(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        diagnosis = args.get("diagnosis")
        needed = args.get("info_needed")
        if not isinstance(diagnosis, str) or not diagnosis.strip():
            _raise_command("mark_unable_to_reproduce requires a 'diagnosis'.")
        if not isinstance(needed, str) or not needed.strip():
            _raise_command("mark_unable_to_reproduce requires 'info_needed' explaining what to ask for.")
        body = (
            "## Could not reproduce\n\n"
            f"{diagnosis}\n\n"
            "## Information needed\n\n"
            f"{needed}\n"
        )
        try:
            comment = _run_coro(
                bindings.loop,
                bindings.github.post_comment(bindings.repo.full_name, bindings.issue.number, body),
            )
        except GitHubError as exc:
            _audit(bindings, "mark_unable_to_reproduce", args, error=str(exc))
            _raise_command(f"GitHub rejected comment: {exc.status} {exc.message}")
        bindings.db.set_issue_state(bindings.issue_key, "abandoned")
        _audit(bindings, "mark_unable_to_reproduce", args, result={"comment_id": comment.id})
        return f"posted abandonment comment id={comment.id}"

    return host_tool(
        name="mark_unable_to_reproduce",
        description="Close the loop without a PR: comment with diagnosis + info request, mark issue abandoned.",
        parameters={
            "type": "object",
            "properties": {
                "diagnosis": {"type": "string"},
                "info_needed": {"type": "string"},
            },
            "required": ["diagnosis", "info_needed"],
            "additionalProperties": False,
        },
        execute=execute,
    )


# ---------- fetch_issue_thread ----------
def _build_fetch_thread(bindings: ToolBindings) -> HostTool[Any, Any]:
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        try:
            issue = _run_coro(
                bindings.loop,
                bindings.github.get_issue(bindings.repo.full_name, bindings.issue.number),
            )
            comments = _run_coro(
                bindings.loop,
                bindings.github.list_comments(bindings.repo.full_name, bindings.issue.number),
            )
        except GitHubError as exc:
            _audit(bindings, "fetch_issue_thread", args, error=str(exc))
            _raise_command(f"GitHub fetch failed: {exc.status} {exc.message}")
        lines = [
            f"# {issue.repo}#{issue.number} ({issue.state})",
            f"title: {issue.title}",
            f"author: @{issue.author}",
            f"labels: {', '.join(issue.labels) if issue.labels else '(none)'}",
            "",
            "## Body",
            issue.body.strip() or "(empty)",
            "",
            f"## Comments ({len(comments)})",
        ]
        for c in comments:
            lines.extend(["", f"### @{c.author} at {c.created_at}", c.body.strip()])
        rendered = "\n".join(lines)
        _audit(bindings, "fetch_issue_thread", args, result={"comments": len(comments)})
        return rendered

    return host_tool(
        name="fetch_issue_thread",
        description="Refetch the originating issue and its comments (use sparingly).",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        execute=execute,
    )


_PRIMARY_TYPES = ("bug", "enhancement", "question", "proposal", "documentation", "invalid", "duplicate")
_PRIORITIES = ("prio:p0", "prio:p1", "prio:p2", "prio:p3")
_FUNCTIONAL = ("agent", "tool", "tui", "cli", "prompting", "sdk", "auth", "setup", "ux", "providers")
_PLATFORMS = ("platform:linux", "platform:macos", "platform:windows", "platform:wsl")


def _build_set_issue_labels(bindings: ToolBindings) -> HostTool[Any, Any]:
    """Append labels to the originating issue (or PR)."""
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        labels = args.get("labels")
        if not isinstance(labels, list) or not labels:
            _raise_command("set_issue_labels requires a non-empty 'labels' array.")
        cleaned = [str(l).strip() for l in labels if isinstance(l, str) and l.strip()]
        if not cleaned:
            _raise_command("set_issue_labels requires at least one non-empty label.")
        target_number = bindings.issue.number
        if isinstance(args.get("number"), int):
            target_number = int(args["number"])
        try:
            applied = _run_coro(
                bindings.loop,
                bindings.github.add_issue_labels(bindings.repo.full_name, target_number, cleaned),
            )
        except GitHubError as exc:
            _audit(bindings, "set_issue_labels", args, error=str(exc))
            _raise_command(f"GitHub rejected labels: {exc.status} {exc.message}")
        _audit(bindings, "set_issue_labels", args, result={"labels": list(applied)})
        return f"labels now: {', '.join(applied)}"

    return host_tool(
        name="set_issue_labels",
        description="Append labels to the originating issue/PR. Never removes existing labels.",
        parameters={
            "type": "object",
            "properties": {
                "labels": {"type": "array", "items": {"type": "string"}},
                "number": {"type": "integer", "description": "Optional override; defaults to the originating issue."},
            },
            "required": ["labels"],
            "additionalProperties": False,
        },
        execute=execute,
    )


def _build_classify_issue(bindings: ToolBindings) -> HostTool[Any, Any]:
    """Triage step. Pick a primary type, optional priority/functional/provider/platform,
    apply labels on GitHub, persist the primary type in sqlite, and signal which workflow
    branch the agent should follow."""
    def execute(args: dict[str, Any], _ctx: HostToolContext[Any]) -> str:
        primary = args.get("primary")
        if primary not in _PRIMARY_TYPES:
            _raise_command(
                f"classify_issue 'primary' must be one of {_PRIMARY_TYPES}; got {primary!r}."
            )
        priority = args.get("priority")
        if primary == "bug":
            if priority not in _PRIORITIES:
                _raise_command(
                    f"classify_issue requires 'priority' in {_PRIORITIES} when primary=='bug'."
                )
        elif priority is not None and priority != "":
            _raise_command("classify_issue 'priority' is only valid when primary=='bug'.")
        rationale = args.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            _raise_command("classify_issue requires a one-sentence 'rationale'.")

        labels: list[str] = [primary]
        if primary == "bug" and isinstance(priority, str):
            labels.append(priority)
        for fn in args.get("functional") or ():
            if isinstance(fn, str) and fn in _FUNCTIONAL:
                labels.append(fn)
        provider = args.get("provider")
        if isinstance(provider, str) and provider.strip():
            if not provider.startswith("provider:"):
                _raise_command("classify_issue 'provider' must start with 'provider:' (e.g. provider:openai).")
            labels.append("providers")
            labels.append(provider)
        platform = args.get("platform")
        if isinstance(platform, str) and platform.strip():
            if platform not in _PLATFORMS:
                _raise_command(f"classify_issue 'platform' must be one of {_PLATFORMS}.")
            labels.append(platform)
        labels.append("triaged")

        try:
            applied = _run_coro(
                bindings.loop,
                bindings.github.add_issue_labels(
                    bindings.repo.full_name, bindings.issue.number, labels,
                ),
            )
        except GitHubError as exc:
            _audit(bindings, "classify_issue", args, error=str(exc))
            _raise_command(f"GitHub rejected labels: {exc.status} {exc.message}")

        bindings.db.set_issue_classification(bindings.issue_key, primary)
        _audit(
            bindings, "classify_issue", args,
            result={"primary": primary, "labels": list(applied), "rationale": rationale},
        )
        # Echo back the workflow the agent should now follow. The persona prompt
        # already describes each branch; the tool result reminds it.
        if primary == "bug":
            next_step = "reproduce → diagnose → fix → PR"
        elif primary == "documentation":
            next_step = "fix the docs and open a PR using the four-section template"
        elif primary == "question":
            next_step = "answer in a single gh_post_comment; no PR, no repro"
        elif primary in ("enhancement", "proposal"):
            next_step = "post one thoughtful gh_post_comment on feasibility/scope; no PR"
        else:
            next_step = "post one explanatory gh_post_comment; no further action"
        return f"classified as {primary}; labels applied: {', '.join(applied)}. Next: {next_step}."

    return host_tool(
        name="classify_issue",
        description=(
            "First triage step. Classify the issue, apply labels on GitHub, and pick the "
            "workflow branch (bug → repro+fix+PR, question → reply only, etc.). MUST be "
            "called before any other gh_* action on a new issue."
        ),
        parameters={
            "type": "object",
            "properties": {
                "primary": {
                    "type": "string",
                    "enum": list(_PRIMARY_TYPES),
                    "description": "Exactly one primary classification.",
                },
                "priority": {
                    "type": "string",
                    "enum": list(_PRIORITIES),
                    "description": "Required when primary=='bug'; one of prio:p0..p3.",
                },
                "functional": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(_FUNCTIONAL)},
                    "description": "Zero or more functional labels.",
                },
                "provider": {
                    "type": "string",
                    "description": "Only if explicitly provider-scoped; format provider:<name>.",
                },
                "platform": {
                    "type": "string",
                    "enum": list(_PLATFORMS),
                    "description": "Only if platform materially affects reproduction.",
                },
                "rationale": {"type": "string", "description": "One sentence explaining the classification."},
            },
            "required": ["primary", "rationale"],
            "additionalProperties": False,
        },
        execute=execute,
    )


def build(bindings: ToolBindings) -> tuple[HostTool[Any, Any], ...]:
    """Return the full set of host tools bound to one task's context."""
    return (
        _build_classify_issue(bindings),
        _build_set_issue_labels(bindings),
        _build_post_comment(bindings),
        _build_push_branch(bindings),
        _build_open_pr(bindings),
        _build_request_review(bindings),
        _build_repro_record(bindings),
        _build_mark_unable(bindings),
        _build_fetch_thread(bindings),
    )


__all__ = ["ToolBindings", "build"]
