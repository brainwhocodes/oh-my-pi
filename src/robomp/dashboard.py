"""Status dashboard helpers: log tail + the single-page HTML served at `/`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Tail at most this many bytes from the end of the log file. Caps work for any
# `limit`, even pathologically large ones, on a multi-MB rotating file.
_TAIL_MAX_BYTES = 2 * 1024 * 1024


def tail_jsonl(path: Path, *, limit: int) -> list[dict[str, Any]]:
    """Return up to `limit` JSON log records from the tail of `path` (oldest first).

    Lines that fail to parse are returned as `{"level": "RAW", "msg": <line>}`
    so a malformed final line never blanks the whole view.
    """
    if limit <= 0 or not path.exists():
        return []

    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []

    read_size = min(size, _TAIL_MAX_BYTES)
    with path.open("rb") as fh:
        fh.seek(size - read_size)
        chunk = fh.read(read_size)

    # If we started mid-line, drop the partial leading line.
    if read_size < size:
        nl = chunk.find(b"\n")
        if nl == -1:
            return []
        chunk = chunk[nl + 1 :]

    lines = chunk.splitlines()
    out: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
                continue
        except json.JSONDecodeError:
            pass
        out.append({"level": "RAW", "logger": "raw", "msg": line.decode("utf-8", errors="replace")})
    return out


# Self-contained dashboard page. Vanilla JS, no external assets, no build step.
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>robomp</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #0b0d10;
    --panel: #15181d;
    --panel-2: #1c2026;
    --border: #262b33;
    --fg: #e6e8eb;
    --muted: #8a93a0;
    --accent: #5aa9ff;
    --ok: #4ade80;
    --warn: #facc15;
    --err: #f87171;
    --info: #60a5fa;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--fg);
    font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header { display: flex; align-items: baseline; gap: 16px; padding: 14px 20px;
    border-bottom: 1px solid var(--border); }
  header h1 { font-size: 16px; margin: 0; letter-spacing: 0.4px; }
  header .meta { color: var(--muted); font-size: 12px; }
  header .meta span + span { margin-left: 14px; }
  header .meta b { color: var(--fg); font-weight: 500; }
  main { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; padding: 14px 20px; }
  section { background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  section h2 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.6px; color: var(--muted);
    margin: 0; padding: 10px 14px; border-bottom: 1px solid var(--border); background: var(--panel-2); }
  .full { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
  th, td { text-align: left; padding: 6px 14px; border-bottom: 1px solid var(--border);
    vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.4px; background: var(--panel-2); }
  tbody tr:last-child td { border-bottom: none; }
  .pill { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px;
    border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
  .pill.queued  { color: var(--info); border-color: rgba(96,165,250,0.35); }
  .pill.running { color: var(--warn); border-color: rgba(250,204,21,0.40); }
  .pill.done    { color: var(--ok);   border-color: rgba(74,222,128,0.35); }
  .pill.failed  { color: var(--err);  border-color: rgba(248,113,113,0.40); }
  .pill.skipped { color: var(--muted); }
  .stats { display: flex; gap: 10px; padding: 12px 14px; flex-wrap: wrap; }
  .stat { flex: 1 1 110px; background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 5px; padding: 8px 12px; }
  .stat .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.4px; }
  .stat .v { font-size: 22px; font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }
  .muted { color: var(--muted); }
  .empty { padding: 14px; color: var(--muted); font-style: italic; }
  .logs { max-height: 60vh; overflow: auto; padding: 0; font-family:
    ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
  .logs .row { padding: 4px 14px; border-bottom: 1px solid #1a1d22; display: grid;
    grid-template-columns: 90px 60px 150px 1fr; gap: 10px; align-items: baseline; }
  .logs .row:hover { background: #161a1f; }
  .logs .ts { color: var(--muted); white-space: nowrap; }
  .logs .lvl { font-weight: 600; }
  .logs .lvl.INFO { color: var(--info); }
  .logs .lvl.WARNING { color: var(--warn); }
  .logs .lvl.ERROR { color: var(--err); }
  .logs .lvl.DEBUG { color: var(--muted); }
  .logs .lvl.RAW { color: var(--muted); }
  .logs .logger { color: var(--muted); }
  .logs .msg { white-space: pre-wrap; word-break: break-word; }
  .logs .extras { color: var(--muted); }
  .logs .extras b { color: #b6bcc6; font-weight: 500; }
  .toolbar { display: flex; align-items: center; gap: 10px; padding: 8px 14px;
    border-bottom: 1px solid var(--border); background: var(--panel-2); }
  .toolbar label { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.4px; display: flex; align-items: center; gap: 6px; }
  .toolbar input[type=text], .toolbar select {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 4px; padding: 3px 6px; font: inherit; }
  .toolbar input[type=checkbox] { accent-color: var(--accent); }
  .err-cell { color: var(--err); white-space: pre-wrap; word-break: break-word; max-width: 480px; }
  code { background: var(--panel-2); padding: 0 4px; border-radius: 3px; }
  button { font: inherit; background: var(--panel-2); color: var(--fg);
    border: 1px solid var(--border); border-radius: 4px; padding: 4px 10px; cursor: pointer; }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.primary { background: var(--accent); color: #0b1220; border-color: var(--accent); font-weight: 600; }
  button.primary:hover:not(:disabled) { filter: brightness(1.1); color: #0b1220; }
  button.small { padding: 1px 8px; font-size: 11px; }
  .trigger-form { display: flex; flex-wrap: wrap; gap: 10px; padding: 12px 14px;
    align-items: center; }
  .trigger-form input[type=text], .trigger-form input[type=password] {
    background: var(--bg); color: var(--fg); border: 1px solid var(--border);
    border-radius: 4px; padding: 5px 8px; font: inherit; min-width: 220px; }
  .trigger-form .row-label { color: var(--muted); font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.4px; }
  .trigger-status { padding: 0 14px 12px; font-size: 12px; min-height: 18px; }
  .trigger-status.err { color: var(--err); }
  .trigger-status.ok  { color: var(--ok); }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>robomp</h1>
  <div class="meta">
    <span>bot <b id="m-bot">…</b></span>
    <span>uptime <b id="m-uptime">…</b></span>
    <span>concurrency <b id="m-conc">…</b></span>
    <span>model <b id="m-model">…</b></span>
    <span>allowlist <b id="m-allow">…</b></span>
    <span class="muted" id="m-refresh">—</span>
  </div>
</header>

<main>
  <section class="full">
    <h2>trigger</h2>
    <div class="trigger-form">
      <span class="row-label">issue</span>
      <input id="t-issue" type="text" placeholder="owner/repo#42" autocomplete="off" />
      <button class="primary" id="t-triage">Fetch &amp; triage</button>
      <button id="t-retry">Retry latest event</button>
      <span class="row-label" style="margin-left:auto">token</span>
      <input id="t-token" type="password" placeholder="X-Robomp-Replay-Token" autocomplete="off" />
    </div>
    <div class="trigger-status" id="t-status"></div>
  </section>

  <section class="full">
    <h2>queue</h2>
    <div class="stats" id="stats"></div>
  </section>

  <section>
    <h2>currently working</h2>
    <div id="working"></div>
  </section>

  <section>
    <h2>active issues</h2>
    <div id="issues"></div>
  </section>

  <section class="full">
    <h2>recent events</h2>
    <div id="events"></div>
  </section>

  <section class="full">
    <h2>agent logs</h2>
    <div class="toolbar">
      <label>level
        <select id="log-level">
          <option value="">all</option>
          <option value="DEBUG">debug+</option>
          <option value="INFO" selected>info+</option>
          <option value="WARNING">warn+</option>
          <option value="ERROR">error</option>
        </select>
      </label>
      <label>filter <input id="log-filter" type="text" placeholder="substring" /></label>
      <label><input id="log-follow" type="checkbox" checked /> follow</label>
      <span class="muted" id="log-count"></span>
    </div>
    <div class="logs" id="logs"></div>
  </section>
</main>

<script>
const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, RAW: 20 };
const TERMINAL_ISSUE_STATES = new Set(["merged", "closed", "abandoned"]);

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtDuration(seconds) {
  if (seconds == null || !isFinite(seconds)) return "—";
  if (seconds < 60) return Math.max(0, Math.round(seconds)) + "s";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m" + Math.round(seconds % 60) + "s";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h" + Math.floor((seconds % 3600) / 60) + "m";
  return Math.floor(seconds / 86400) + "d" + Math.floor((seconds % 86400) / 3600) + "h";
}

function fmtAge(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return iso;
  return fmtDuration((Date.now() - t) / 1000) + " ago";
}

function issueLink(repo, number) {
  if (!repo || !number) return esc(repo || "");
  return `<a href="https://github.com/${esc(repo)}/issues/${esc(number)}" target="_blank" rel="noopener">${esc(repo)}#${esc(number)}</a>`;
}

function prLink(repo, prNumber) {
  if (!repo || !prNumber) return "—";
  return `<a href="https://github.com/${esc(repo)}/pull/${esc(prNumber)}" target="_blank" rel="noopener">#${esc(prNumber)}</a>`;
}

function renderStats(counts) {
  const order = ["queued", "running", "done", "failed", "skipped"];
  $("stats").innerHTML = order.map((k) =>
    `<div class="stat"><div class="k">${k}</div><div class="v">${counts[k] ?? 0}</div></div>`).join("");
}

function renderWorking(running, inflight) {
  const inflightSet = new Set(inflight || []);
  if (!running.length && !inflightSet.size) {
    $("working").innerHTML = '<div class="empty">idle</div>';
    return;
  }
  const seen = new Set();
  const rows = running.map((e) => {
    const key = e.issue_key || e.delivery_id;
    seen.add(key);
    const started = e.started_at || e.received_at;
    const elapsed = started ? fmtDuration((Date.now() - Date.parse(started)) / 1000) : "—";
    const [repo, number] = (e.issue_key || "").split("#");
    return `<tr>
      <td>${number ? issueLink(repo, number) : `<code>${esc(e.delivery_id.slice(0, 8))}</code>`}</td>
      <td>${esc(e.event_type)}</td>
      <td><span class="pill running">running</span></td>
      <td>${elapsed}</td>
      <td class="muted">attempt ${e.attempts}</td>
    </tr>`;
  });
  // Workers that grabbed an issue key but haven't started a DB row yet (or finished).
  for (const key of inflightSet) {
    if (seen.has(key)) continue;
    const [repo, number] = key.split("#");
    rows.push(`<tr>
      <td>${number ? issueLink(repo, number) : esc(key)}</td>
      <td class="muted">—</td>
      <td><span class="pill running">inflight</span></td>
      <td>—</td>
      <td class="muted">held by pool</td>
    </tr>`);
  }
  $("working").innerHTML =
    `<table><thead><tr><th>issue</th><th>event</th><th>state</th><th>elapsed</th><th></th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function renderIssues(issues) {
  const active = issues.filter((i) => !TERMINAL_ISSUE_STATES.has(i.state));
  if (!active.length) {
    $("issues").innerHTML = '<div class="empty">no active issues</div>';
    return;
  }
  const rows = active.map((i) =>
    `<tr>
      <td>${issueLink(i.repo, i.number)}</td>
      <td><span class="pill">${esc(i.state)}</span></td>
      <td>${esc(i.classification || "")}</td>
      <td>${i.branch ? `<code>${esc(i.branch)}</code>` : '<span class="muted">—</span>'}</td>
      <td>${prLink(i.repo, i.pr_number)}</td>
      <td class="muted">${fmtAge(i.updated_at)}</td>
    </tr>`).join("");
  $("issues").innerHTML =
    `<table><thead><tr><th>issue</th><th>state</th><th>class</th><th>branch</th><th>pr</th><th>updated</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderEvents(events) {
  if (!events.length) {
    $("events").innerHTML = '<div class="empty">no events recorded yet</div>';
    return;
  }
  const rows = events.map((e) => {
    const [repo, number] = (e.issue_key || "").split("#");
    const canRetry = e.state !== "running" && e.state !== "queued";
    const retryBtn = canRetry
      ? `<button class="small" data-retry="${esc(e.delivery_id)}">retry</button>`
      : '<span class="muted">—</span>';
    return `<tr>
      <td class="muted">${fmtAge(e.received_at)}</td>
      <td>${esc(e.event_type)}</td>
      <td>${number ? issueLink(repo, number) : esc(e.repo || "—")}</td>
      <td><span class="pill ${esc(e.state)}">${esc(e.state)}</span></td>
      <td class="muted">${e.attempts}</td>
      <td class="err-cell">${esc(e.last_error || "")}</td>
      <td>${retryBtn}</td>
    </tr>`;
  }).join("");
  $("events").innerHTML =
    `<table><thead><tr><th>received</th><th>event</th><th>where</th><th>state</th><th>tries</th><th>error</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}

let lastLogTs = "";

function renderLogs(entries) {
  const wantLevel = $("log-level").value;
  const filter = $("log-filter").value.trim().toLowerCase();
  const minOrd = wantLevel ? LEVEL_ORDER[wantLevel] ?? 0 : 0;
  const rows = [];
  for (const e of entries) {
    const lvl = e.level || "INFO";
    if ((LEVEL_ORDER[lvl] ?? 20) < minOrd) continue;
    const msg = String(e.msg ?? "");
    const extras = Object.keys(e)
      .filter((k) => !["ts", "level", "logger", "msg", "exc"].includes(k))
      .map((k) => `<b>${esc(k)}</b>=${esc(typeof e[k] === "object" ? JSON.stringify(e[k]) : e[k])}`)
      .join(" ");
    const haystack = (msg + " " + extras).toLowerCase();
    if (filter && !haystack.includes(filter)) continue;
    rows.push(`<div class="row">
      <span class="ts">${esc((e.ts || "").replace("T", " ").replace("Z", ""))}</span>
      <span class="lvl ${esc(lvl)}">${esc(lvl)}</span>
      <span class="logger">${esc(e.logger || "")}</span>
      <span><span class="msg">${esc(msg)}</span>${extras ? ` <span class="extras">${extras}</span>` : ""}${e.exc ? `<br><span class="err-cell">${esc(e.exc)}</span>` : ""}</span>
    </div>`);
  }
  const box = $("logs");
  const follow = $("log-follow").checked;
  box.innerHTML = rows.join("");
  $("log-count").textContent = rows.length + " / " + entries.length;
  if (follow) box.scrollTop = box.scrollHeight;
  if (entries.length) lastLogTs = entries[entries.length - 1].ts || lastLogTs;
}

async function tick() {
  try {
    const [status, logs] = await Promise.all([
      fetch("api/status").then((r) => r.json()),
      fetch("api/logs?limit=400").then((r) => r.json()),
    ]);
    $("m-bot").textContent = status.runtime.bot_login;
    $("m-uptime").textContent = fmtDuration(status.runtime.uptime_seconds);
    $("m-conc").textContent = status.runtime.max_concurrency;
    $("m-model").textContent = status.runtime.model;
    $("m-allow").textContent = status.runtime.repo_allowlist.length
      ? status.runtime.repo_allowlist.join(", ") : "(none)";
    $("m-refresh").textContent = "updated " + new Date().toLocaleTimeString();
    renderStats(status.event_counts);
    renderWorking(status.running_events, status.inflight);
    renderIssues(status.issues);
    renderEvents(status.recent_events);
    renderLogs(logs.entries || []);
  } catch (err) {
    $("m-refresh").textContent = "error: " + err.message;
  }
}

// ----- trigger -----
const TOKEN_KEY = "robomp.replay_token";
$("t-token").value = localStorage.getItem(TOKEN_KEY) || "";

function setStatus(text, kind) {
  const el = $("t-status");
  el.textContent = text;
  el.className = "trigger-status" + (kind ? " " + kind : "");
}

async function postTrigger(body) {
  const token = $("t-token").value.trim();
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-Robomp-Replay-Token"] = token;
  setStatus("…", "");
  let resp;
  try {
    resp = await fetch("api/trigger", { method: "POST", headers, body: JSON.stringify(body) });
  } catch (err) {
    setStatus("network error: " + err.message, "err");
    return;
  }
  let data = null;
  try { data = await resp.json(); } catch (_) { /* may be empty */ }
  if (!resp.ok) {
    const msg = (data && (data.detail || data.message)) || resp.statusText;
    setStatus(`error ${resp.status}: ${msg}`, "err");
    return;
  }
  setStatus(`queued ${data.mode}: ${data.delivery}`, "ok");
  tick();  // refresh dashboard so the new event shows immediately
}

$("t-triage").addEventListener("click", () => {
  const issue = $("t-issue").value.trim();
  if (!issue) { setStatus("enter owner/repo#NN", "err"); return; }
  postTrigger({ mode: "triage", issue });
});
$("t-retry").addEventListener("click", () => {
  const issue = $("t-issue").value.trim();
  if (!issue) { setStatus("enter owner/repo#NN", "err"); return; }
  postTrigger({ mode: "retry", issue });
});
$("t-issue").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") $("t-triage").click();
});
$("events").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-retry]");
  if (!btn) return;
  postTrigger({ mode: "retry", delivery_id: btn.dataset.retry });
});

$("log-level").addEventListener("change", tick);
$("log-filter").addEventListener("input", tick);
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


__all__ = ["INDEX_HTML", "tail_jsonl"]
