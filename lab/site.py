"""Static site generator for the public Hannah Lab - a console-style UI.

Layout: a fixed left sidebar (Dashboard / Experiments), breadcrumbs, and a
clean light content area - the shape of a cloud console, applied to a
read-only research lab. Navigation is progressive: the dashboard summarizes
the lab, the experiments page lists compact tiles, a tile opens the
experiment's own section (tabs: Overview / Journal / Runs / Memory / Beliefs /
Questions / Timeline / Failures), and run detail pages carry the full
investigation path with secondary material collapsed.

    index.html                          dashboard: stats, recent runs, activity
    experiments.html                    experiment tiles
    experiments/<slug>/index.html       experiment overview
    experiments/<slug>/<tab>.html       journal / runs / memory / beliefs /
                                        questions / timeline / failures
    experiments/<slug>/runs/<id>.html   run detail (investigation path)
    experiments/<slug>/runs/<id>/*.json raw public artifacts
    lab_state.json                      machine-readable snapshot

Plain HTML + one stylesheet. No JavaScript frameworks, no backend, no forms -
strictly read-only, servable by any static host.
"""

import html
import json
import shutil
from datetime import datetime
from pathlib import Path

SITE_NAME = "Hannah Lab"
DEFAULT_GITHUB = "https://github.com/zackfeldstein/hannah"

ABOUT = ("Hannah is an open-source edge-AI lab. A local model running on a "
         "Jetson wakes up, receives a prompt, chooses tools, inspects its "
         "environment, records what it found, updates memory and beliefs, and "
         "publishes public-safe lab notes. Hannah does not receive prefilled "
         "telemetry; she investigates through tools.")

EXP_TABS = [
    ("index.html", "Overview"),
    ("journal.html", "Journal"),
    ("runs.html", "Runs"),
    ("memory.html", "Memory"),
    ("beliefs.html", "Beliefs"),
    ("questions.html", "Questions"),
    ("timeline.html", "Timeline"),
    ("failures.html", "Failures"),
]

EVENT_ICONS = {
    "experiment": "◇", "memory_created": "＋", "memory_updated": "↺",
    "belief_created": "✦", "belief_changed": "Δ", "question_opened": "?",
    "question_answered": "✓", "failures": "✗", "contradiction": "⚡",
}


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %Y %H:%M")
    except (TypeError, ValueError):
        return esc(iso or "")


def _fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return esc(iso or "")


def _truncate(text: str, n: int = 320) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut + " …"


# --- page chrome -----------------------------------------------------------------

def _page(title: str, body: str, root: str = "", github: str = DEFAULT_GITHUB,
          active: str = "", crumbs=None) -> str:
    """Console shell: fixed sidebar + breadcrumb bar + content column.

    active: "dashboard" | "experiments" (sidebar highlight)
    crumbs: [(label, href-or-None)] rendered in the top bar
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    crumb_html = ""
    if crumbs:
        parts = []
        for label, href in crumbs:
            if href:
                parts.append(f'<a href="{href}">{esc(label)}</a>')
            else:
                parts.append(f"<span>{esc(label)}</span>")
        crumb_html = ' <span class="sep">/</span> '.join(parts)

    def side(href, label, key, icon):
        cls = "active" if active == key else ""
        return (f'<a class="side-link {cls}" href="{root}{href}">'
                f'<span class="side-ico">{icon}</span>{label}</a>')

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{esc(ABOUT[:150])}">
<title>{esc(title)} · {SITE_NAME}</title>
<link rel="stylesheet" href="{root}style.css"></head>
<body>
<aside class="side">
  <a class="side-brand" href="{root}index.html">
    <span class="dotlive"></span>HANNAH<span class="brand-sub">LAB</span></a>
  <nav class="side-nav">
    {side("index.html", "Dashboard", "dashboard", "▦")}
    {side("experiments.html", "Experiments", "experiments", "◇")}
  </nav>
  <div class="side-foot">
    <a href="{github}">GitHub ↗</a>
    <span>public · read-only</span>
  </div>
</aside>
<div class="main">
  <div class="crumbbar">{crumb_html}</div>
  <div class="content">
{body}
  <footer>{SITE_NAME} — an open, read-only window into a local agent.
Built {now} on the machine Hannah lives on · <a href="{github}">source</a></footer>
  </div>
</div>
</body></html>"""


def _exp_header(name: str, meta: dict, active_tab: str, prefix: str = "",
                subtitle: str = "") -> str:
    """Experiment section header: title, status, tab bar."""
    status = (f' <span class="chip {_status_kind(meta.get("status", ""))}">'
              f'{esc(meta["status"])}</span>' if meta.get("status") else "")
    tabs = "".join(
        f'<a href="{prefix}{href}" class="{"active" if href == active_tab else ""}">'
        f'{label}</a>'
        for href, label in EXP_TABS)
    sub = f'<p class="pagesub">{esc(subtitle)}</p>' if subtitle else ""
    return (f'<div class="pagehead"><h1>{esc(name)}{status}</h1>{sub}'
            f'<div class="tabs">{tabs}</div></div>')


def _status_kind(status):
    return {"open": "warn", "investigating": "info", "answered": "ok",
            "abandoned": "bad", "active": "ok", "complete": "info",
            "paused": "warn", "pass": "ok", "fail": "bad",
            "na": "neutral"}.get(status, "neutral")


def _chip(text, kind="info"):
    return f'<span class="chip {kind}">{esc(text)}</span>'


def _tool_chip(text, used=False):
    return f'<span class="chip {"tool-used" if used else "tool"}">{esc(text)}</span>'


def _conf_chip(level):
    return _chip(level, {"high": "ok", "medium": "warn", "low": "bad"}
                 .get(level, "neutral"))


def _status_chip(status):
    return _chip(status, _status_kind(status))


def _run_link(run_id, prefix="runs/"):
    return f'<a href="{prefix}{esc(run_id)}.html" class="mono">{esc(run_id)}</a>'


def _stat_cards(pairs) -> str:
    return ('<div class="statrow">' + "".join(
        f'<div class="statcard"><div class="n">{n}</div>'
        f'<div class="l">{esc(label)}</div></div>'
        for n, label in pairs) + "</div>")


# --- entry rendering ---------------------------------------------------------------

def _entry_html(e, run_id, run_prefix="runs/", link_run=False):
    tools = "".join(_tool_chip(c["tool"], used=True) for c in e.tool_calls)
    trace = ""
    if e.tool_calls:
        rows = "".join(
            f"<details><summary>{esc(c['tool'])}</summary>"
            f"<div class='body'><pre>{esc(c['output'])}</pre></div></details>"
            for c in e.tool_calls)
        trace = (f'<details class="invest"><summary>{len(e.tool_calls)} tool '
                 f'call{"s" if len(e.tool_calls) != 1 else ""}</summary>'
                 f'<div class="chips">{tools}</div><div class="body">{rows}'
                 f'</div></details>')
    fails = "".join(
        f'<div class="failrow"><div class="when">{esc(f["type"])}</div>'
        f'{esc(f["detail"])}</div>' for f in e.failures)
    src = (f'<div class="from">from run {_run_link(run_id, run_prefix)}</div>'
           if link_run else "")
    return (f'<article class="entry card"><div class="when">{_fmt_time(e.time)}'
            f'</div><div class="prose">{esc(e.text)}</div>{trace}{fails}{src}'
            f'</article>')


def _changes_list(diff: dict) -> list:
    rows = [
        (len(diff.get("new_memories", [])), "new memories"),
        (len(diff.get("updated_memories", [])), "memories updated"),
        (len(diff.get("new_beliefs", [])), "new beliefs"),
        (len(diff.get("belief_confidence_changes", [])),
         "belief confidence changes"),
        (len(diff.get("new_questions", [])), "questions opened"),
        (diff.get("new_failures", 0), "failures recorded"),
        (len(diff.get("new_contradictions", [])), "contradictions detected"),
    ]
    out = [f"<li><b>{n}</b> {label}</li>" for n, label in rows if n]
    delta = diff.get("tool_use_delta", {})
    for key, label in (("started_using", "started using"),
                       ("stopped_using", "stopped using")):
        if delta.get(key):
            out.append(f"<li>{label}: " + "".join(
                _tool_chip(t) for t in delta[key]) + "</li>")
    return out


def _runs_table(runs, run_href=None, experiment_col=None, limit=None):
    """Runs table.

    run_href:       run_id -> href (default: runs/<id>.html, for pages inside
                    an experiment section)
    experiment_col: {run_id: (name, href)} to add an Experiment column
                    (used on the dashboard)
    """
    run_href = run_href or (lambda rid: f"runs/{rid}.html")
    head = ["<table><tr><th>Run</th>"]
    if experiment_col:
        head.append("<th>Experiment</th>")
    head.append("<th>Started</th><th>Duration</th><th>Model</th>"
                "<th>Entries</th><th>Tool calls</th><th>Failures</th>"
                "<th>Score</th><th>Published</th></tr>")
    rows = ["".join(head)]
    shown = list(reversed(runs))[:limit] if limit else list(reversed(runs))
    for r in shown:
        m = r.manifest
        blocked = sum(1 for e in r.entries if e.blocked)
        pub = (_chip("yes", "ok") if blocked == 0
               else _chip(f"partial ({blocked} withheld)", "warn"))
        exp_cell = ""
        if experiment_col:
            name, href = experiment_col[r.run_id]
            exp_cell = f"<td><a href='{href}'>{esc(name)}</a></td>"
        rows.append(
            f"<tr><td><a href='{run_href(r.run_id)}' class='mono'>"
            f"{esc(r.run_id)}</a></td>{exp_cell}"
            f"<td class='mono'>{_fmt_date(m.get('started_at'))}</td>"
            f"<td class='mono'>{esc(m.get('duration'))}</td>"
            f"<td class='mono'>{esc(m.get('model'))}</td>"
            f"<td>{m.get('entry_count')}</td><td>{m.get('tool_call_count')}</td>"
            f"<td>{r.failure_count}</td><td>{r.score.get('score')}</td>"
            f"<td>{pub}</td></tr>")
    rows.append("</table>")
    return "\n".join(rows)


# --- dashboard -----------------------------------------------------------------------

def render_dashboard(groups, states, global_state, run_slug, github):
    all_runs = sorted((r for g in groups for r in g["runs"]),
                      key=lambda r: r.manifest.get("started_at") or "")
    total_entries = sum(len(r.entries) for r in all_runs)
    total_tools = sum(r.manifest.get("tool_call_count", 0) for r in all_runs)
    total_failures = sum(r.failure_count for r in all_runs)

    body = [f"""<div class="pagehead">
<h1>Dashboard</h1>
<p class="pagesub">{esc(ABOUT)}</p>
</div>"""]

    body.append(_stat_cards([
        (len(groups), "experiments"),
        (len(all_runs), "runs"),
        (total_entries, "journal entries"),
        (total_tools, "tool calls"),
        (len(global_state.beliefs), "beliefs"),
        (total_failures, "failures"),
    ]))

    # Recent runs across the whole lab.
    exp_col = {}
    for g in groups:
        for r in g["runs"]:
            exp_col[r.run_id] = (g["name"], f"experiments/{g['slug']}/index.html")
    body.append('<div class="panel"><div class="panel-head"><h2>Recent runs'
                '</h2><a class="panel-link" href="experiments.html">all '
                'experiments →</a></div>')
    body.append(_runs_table(
        all_runs, experiment_col=exp_col, limit=10,
        run_href=lambda rid: f"experiments/{run_slug[rid]}/runs/{rid}.html"))
    body.append("</div>")

    # Latest journal entry + recent failures, side by side.
    latest_run = all_runs[-1] if all_runs else None
    body.append('<div class="cols">')
    if latest_run:
        slug = run_slug[latest_run.run_id]
        pick = next((e for e in reversed(latest_run.entries)
                     if e.text.strip() and not e.blocked), None)
        if pick:
            body.append(f"""<div class="panel">
<div class="panel-head"><h2>Latest journal entry</h2>
<a class="panel-link" href="experiments/{slug}/journal.html">journal →</a></div>
<div class="entry"><div class="when">{_fmt_time(pick.time)} · run
{_run_link(latest_run.run_id, f"experiments/{slug}/runs/")}</div>
<div class="prose">{esc(_truncate(pick.text, 600))}</div></div></div>""")
    recent_fails = [(e.time, f, r.run_id)
                    for g in groups for r in g["runs"]
                    for e in r.entries for f in e.failures]
    recent_fails.sort(key=lambda x: x[0], reverse=True)
    if recent_fails:
        rows = "".join(
            f'<div class="failrow"><div class="when">{_fmt_time(t)} · '
            f'{esc(f["type"])} · '
            f'{_run_link(rid, f"experiments/{run_slug[rid]}/runs/")}</div>'
            f'{esc(_truncate(f["detail"], 120))}</div>'
            for t, f, rid in recent_fails[:5])
        slug0 = run_slug[recent_fails[0][2]]
        body.append(f"""<div class="panel">
<div class="panel-head"><h2>Recent failures</h2>
<a class="panel-link" href="experiments/{slug0}/failures.html">failure wall
→</a></div>{rows}</div>""")
    body.append("</div>")

    # Lab-wide beliefs, compact.
    if global_state.beliefs:
        rows = []
        for b in global_state.beliefs[:6]:
            ev = " · ".join(
                _run_link(r, f"experiments/{run_slug[r]}/runs/")
                for r in dict.fromkeys(b.get("evidence_runs", []))
                if r in run_slug)
            contra = (_chip(f"{len(b['contradictions'])} contradiction(s)",
                            "bad") if b.get("contradictions") else "")
            rows.append(
                f"<tr><td>{esc(b['statement'])} {contra}</td>"
                f"<td>{_conf_chip(b['confidence'])}</td>"
                f"<td class='mono'>{b.get('observations')}</td>"
                f"<td>{ev}</td></tr>")
        body.append("""<div class="panel">
<div class="panel-head"><h2>What Hannah currently believes (lab-wide)</h2></div>
<p class="muted">Derived across every experiment; evidence links go to the
runs that support each belief.</p>
<table><tr><th>Belief</th><th>Confidence</th><th>Obs.</th><th>Evidence</th></tr>"""
                    + "".join(rows) + "</table></div>")

    return _page("Dashboard", "\n".join(body), github=github,
                 active="dashboard", crumbs=[("Dashboard", None)])


# --- experiments list -------------------------------------------------------------

def render_experiments_list(groups, registry, github):
    body = ["""<div class="pagehead"><h1>Experiments</h1>
<p class="pagesub">Each experiment is a deliberate change to Hannah's
conditions — prompt, senses, tools, model — and the runs collected under it.
Open one to see its runs, journal, memory, beliefs, questions, and
failures.</p></div>"""]

    # Control panel — hidden until the local control API confirms it is live
    # (i.e. you are running `hannah_lab.py preview`). On a plain static host
    # these controls never appear and the page stays read-only.
    body.append(_EXP_CONTROLS_HTML)

    ordered = sorted(groups, key=lambda g: g["runs"][-1].manifest.get(
        "started_at") or "", reverse=True)
    group_names = {g["name"] for g in groups}
    # Registry experiments with no collected runs yet (e.g. just created, or
    # currently running) show as "pending" tiles so creating one feels live.
    pending = [(name, meta) for name, meta in registry.items()
               if name not in group_names]

    body.append('<div class="tiles">')
    for name, meta in sorted(pending, key=lambda x: x[0].lower()):
        status = _status_chip(meta.get("status", "pending") or "pending")
        desc = _truncate(meta.get("description", ""), 150)
        body.append(f"""<div class="tile pending" data-exp="{esc(name)}">
<div class="tile-head"><span class="tile-name">{esc(name)}</span>{status}</div>
<p class="tile-desc">{esc(desc)}</p>
<div class="tile-meta"><span>awaiting first run</span></div>
<button class="tile-del" data-exp="{esc(name)}" title="Delete experiment"
  style="display:none">✕</button></div>""")
    for g in ordered:
        name, slug, runs = g["name"], g["slug"], g["runs"]
        meta = registry.get(name, {})
        latest = runs[-1]
        status = (_status_chip(meta["status"]) if meta.get("status") else "")
        desc = _truncate(meta.get("description", "")
                         or latest.manifest.get("note", ""), 150)
        body.append(f"""<div class="tile" data-exp="{esc(name)}">
<a class="tile-open" href="experiments/{slug}/index.html">
<div class="tile-head"><span class="tile-name">{esc(name)}</span>{status}</div>
<p class="tile-desc">{esc(desc)}</p>
<div class="tile-meta">
<span>{len(runs)} run{"s" if len(runs) != 1 else ""}</span>
<span>{sum(len(r.entries) for r in runs)} entries</span>
<span>last run {_fmt_date(latest.manifest.get('ended_at'))}</span>
</div></a>
<button class="tile-rerun" data-exp="{esc(name)}" title="Run this experiment again"
  style="display:none">↻</button>
<button class="tile-del" data-exp="{esc(name)}" title="Delete experiment"
  style="display:none">✕</button></div>""")
    body.append("</div>")
    if not groups and not pending:
        body.append('<p class="muted" id="noexp">No experiments yet. Start one '
                    "with the button above, or "
                    "<span class='mono'>python3 hannah_run.py start</span>.</p>")
    body.append(_EXP_CONTROLS_SCRIPT)
    return _page("Experiments", "\n".join(body), github=github,
                 active="experiments",
                 crumbs=[("Experiments", None)])


# The create/delete/collect control panel injected into experiments.html.
# It is inert markup + scoped CSS; the script below only activates it when the
# local preview control API answers, so a published static site is unaffected.
_EXP_CONTROLS_HTML = """
<style>
.labctl { margin:0 0 22px; }
.labctl-bar { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.daemon-bar { margin-bottom:12px; padding-bottom:12px;
  border-bottom:1px solid var(--border); }
.daemon-status { display:flex; align-items:center; gap:8px; font-size:13px;
  color:var(--body); margin-right:4px; }
.daemon-status .dot { width:9px; height:9px; border-radius:50%;
  background:var(--dim); }
.daemon-status .dot.on { background:var(--accent); box-shadow:0 0 6px var(--accent); }
.daemon-status .dot.off { background:var(--red); }
.labctl .ghost { background:#fff; color:var(--muted); border:1px solid var(--border);
  border-radius:8px; padding:6px 12px; font-size:12.5px; cursor:pointer; }
.labctl .ghost:hover { filter:brightness(0.98); }
.labctl .ghost:disabled { opacity:.45; cursor:default; }
.daemon-interval { display:flex; align-items:center; gap:6px; font-size:12.5px;
  color:var(--muted); }
.daemon-interval input { width:60px; background:#fff; color:var(--text);
  border:1px solid var(--border); border-radius:7px; padding:5px 7px;
  font-size:12.5px; }
.labctl .btn-primary { background:var(--accent); color:#fff; border:none;
  border-radius:8px; padding:8px 16px; font-size:13px; font-weight:600;
  cursor:pointer; }
.labctl .btn-primary:hover { filter:brightness(1.08); }
.labctl .btn-primary:disabled { opacity:.5; cursor:default; }
.active-box { font-size:13px; color:var(--body); background:var(--panel);
  border:1px solid var(--border); border-radius:8px; padding:8px 12px; }
.active-box button, .expform button.danger, #labmsg + * button.danger {
  margin-left:8px; }
button.danger { background:var(--red-soft); color:var(--red);
  border:1px solid #eec7c9; border-radius:7px; padding:5px 11px; font-size:12px;
  cursor:pointer; }
button.danger:hover { filter:brightness(1.04); }
.labmsg { font-size:12.5px; color:var(--muted); margin-top:8px; min-height:1em; }
.expform { border:1px solid var(--border); border-radius:12px;
  background:var(--panel); padding:18px 20px; margin-top:14px; }
.expform .ef-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px 16px; }
.expform label.field { display:flex; flex-direction:column; gap:5px;
  font-size:10.5px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--dim); }
.expform label.field.wide { grid-column:1 / -1; }
.expform input[type=text], .expform select, .expform textarea {
  background:#fff; color:var(--text); border:1px solid var(--border);
  border-radius:8px; padding:8px 10px; font-size:13px; font-family:inherit;
  letter-spacing:normal; text-transform:none; }
.expform textarea { min-height:200px; resize:vertical;
  font-family:Georgia,"Times New Roman",serif; font-size:14px; line-height:1.7; }
.ef-toolsrow { grid-column:1 / -1; }
.ef-toolshead { display:flex; align-items:center; gap:8px; margin-bottom:6px;
  font-size:10.5px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--dim); }
.ef-toolshead button { padding:2px 9px; font-size:11px; border-radius:20px;
  border:1px solid var(--border); background:#fff; color:var(--muted);
  cursor:pointer; }
.toolchecks { display:flex; gap:6px 8px; flex-wrap:wrap; }
.toolchecks label { display:flex; align-items:center; gap:5px; cursor:pointer;
  font-family:ui-monospace,Menlo,monospace; font-size:11.5px; color:var(--muted);
  background:#fff; border:1px solid var(--border); border-radius:16px;
  padding:3px 11px; user-select:none; }
.toolchecks label.on { color:#0b6b3d; background:var(--accent-soft);
  border-color:#bfe3cf; }
.ef-actions { display:flex; align-items:center; gap:10px; margin-top:14px; }
.ef-actions .btn-primary { background:var(--accent); color:#fff; border:none;
  border-radius:8px; padding:8px 16px; font-size:13px; font-weight:600;
  cursor:pointer; }
.ef-actions .ghost { background:#fff; color:var(--muted);
  border:1px solid var(--border); border-radius:8px; padding:8px 14px;
  font-size:13px; cursor:pointer; }
.ef-status { font-size:12.5px; color:var(--muted); }
.ef-chk { display:flex; align-items:center; gap:6px; font-size:12.5px;
  color:var(--body); text-transform:none; letter-spacing:normal; }
.tile { position:relative; }
.tile-open { display:block; text-decoration:none; color:inherit; }
.tile.pending { opacity:.85; border-style:dashed; }
.tile-del { position:absolute; top:10px; right:10px; width:24px; height:24px;
  border-radius:50%; border:1px solid var(--border); background:#fff;
  color:var(--red); font-size:12px; line-height:1; cursor:pointer; padding:0; }
.tile-del:hover { background:var(--red-soft); border-color:#eec7c9; }
.tile-rerun { position:absolute; top:10px; right:40px; width:24px; height:24px;
  border-radius:50%; border:1px solid var(--border); background:#fff;
  color:var(--accent); font-size:14px; line-height:1; cursor:pointer; padding:0; }
.tile-rerun:hover { background:var(--accent-soft); border-color:#bfe3cf; }
.lab-modal { position:fixed; inset:0; background:rgba(20,24,30,.45);
  display:flex; align-items:center; justify-content:center; z-index:50; }
.lab-modal-box { background:var(--panel); border:1px solid var(--border);
  border-radius:12px; padding:22px 24px; max-width:440px; width:calc(100% - 40px);
  box-shadow:0 12px 40px rgba(0,0,0,.25); }
.lab-modal-box h3 { margin:0 0 6px; }
.lab-modal-box .ef-chk { display:flex; align-items:flex-start; gap:8px;
  font-size:13px; color:var(--body); margin:8px 0; text-transform:none;
  letter-spacing:normal; cursor:pointer; }
.tile.running { border-style:solid; border-color:var(--accent);
  box-shadow:0 0 0 1px var(--accent) inset; opacity:1; }
.running-badge { position:absolute; bottom:12px; right:12px; text-decoration:none;
  background:var(--accent); color:#fff; font-size:11px; font-weight:600;
  border-radius:16px; padding:3px 11px; }
.running-badge:hover { filter:brightness(1.08); }
.running-dot { color:var(--accent); animation:pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.35;} }
</style>
<div class="labctl" id="labctl" style="display:none">
  <div class="labctl-bar daemon-bar">
    <span class="daemon-status"><span class="dot" id="daemonDot"></span>
      <span id="daemonState">checking…</span></span>
    <button class="ghost" id="dStart">Start</button>
    <button class="ghost" id="dStop">Stop</button>
    <button class="ghost" id="dRestart">Restart</button>
    <span class="daemon-interval">runs every
      <input type="number" id="intervalMin" min="0.25" step="0.25" value="">
      min <button class="ghost" id="intervalSave">Set</button></span>
    <span class="muted" id="daemonMsg"></span>
  </div>
  <div class="labctl-bar">
    <button class="btn-primary" id="newExpBtn">＋ New experiment</button>
    <span class="active-box" id="activeBox" style="display:none"></span>
  </div>
  <div class="expform" id="expForm" style="display:none">
    <div class="ef-grid">
      <label class="field">Label
        <input type="text" id="ef-label" placeholder="e.g. memory-only-v1"></label>
      <label class="field">Model
        <select id="ef-model"></select></label>
      <label class="field wide">Description
        <input type="text" id="ef-desc"
          placeholder="what changes in this experiment"></label>
      <label class="field wide">Goal
        <input type="text" id="ef-goal"
          placeholder="what you want to learn (optional)"></label>
      <label class="field wide">Hypothesis
        <input type="text" id="ef-hyp"
          placeholder="what you expect to happen (optional)"></label>
      <div class="ef-toolsrow">
        <div class="ef-toolshead">Tools offered to Hannah
          <button type="button" id="ef-tools-all">all</button>
          <button type="button" id="ef-tools-none">none</button></div>
        <div class="toolchecks" id="ef-tools"></div>
      </div>
      <label class="field wide">System prompt
        <textarea id="ef-prompt" spellcheck="false"></textarea></label>
    </div>
    <div class="ef-actions">
      <label class="ef-chk"><input type="checkbox" id="ef-fresh" checked>
        fresh start (reset rolling memory)</label>
      <button class="btn-primary" id="ef-start">Start experiment</button>
      <button class="ghost" id="ef-cancel">Cancel</button>
      <span class="ef-status" id="ef-status"></span>
    </div>
  </div>
  <div class="labmsg" id="labmsg"></div>
</div>"""


_EXP_CONTROLS_SCRIPT = """
<script>
(function(){
  var API = '/api/lab';
  var ctl = document.getElementById('labctl');
  var msg = document.getElementById('labmsg');
  function show(el, on){ if (el) el.style.display = on ? '' : 'none'; }
  function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,
    function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function val(id){ return (document.getElementById(id).value||'').trim(); }

  fetch(API + '/options').then(function(r){ if(!r.ok) throw 0; return r.json(); })
    .then(function(o){ if (o && o.control){ show(ctl, true); init(o); } })
    .catch(function(){ /* static host: controls stay hidden, page read-only */ });

  function init(o){
    var msel = document.getElementById('ef-model');
    msel.innerHTML = (o.models||[]).map(function(m){
      return '<option'+(m===o.current_model?' selected':'')+'>'+esc(m)+'</option>';
    }).join('');
    var tbox = document.getElementById('ef-tools');
    tbox.innerHTML = (o.tools||[]).map(function(t){
      var on = (o.enabled_tools||[]).indexOf(t) >= 0;
      return '<label class="'+(on?'on':'')+'" title="'+esc(o.tool_descriptions[t]||'')+'">'
        + '<input type="checkbox" value="'+esc(t)+'"'+(on?' checked':'')+'>'+esc(t)+'</label>';
    }).join('');
    tbox.querySelectorAll('input').forEach(function(cb){
      cb.onchange = function(){ cb.parentNode.className = cb.checked?'on':''; }; });
    document.getElementById('ef-prompt').value = o.current_prompt || '';
    document.querySelectorAll('.tile-del').forEach(function(b){
      show(b, true);
      b.onclick = function(e){ e.preventDefault(); e.stopPropagation();
        delExp(b.getAttribute('data-exp')); };
    });
    document.querySelectorAll('.tile-rerun').forEach(function(b){
      show(b, true);
      b.onclick = function(e){ e.preventDefault(); e.stopPropagation();
        rerunModal(b.getAttribute('data-exp')); };
    });
    document.getElementById('dStart').onclick = function(){ daemon('start'); };
    document.getElementById('dStop').onclick = function(){ daemon('stop'); };
    document.getElementById('dRestart').onclick = function(){ daemon('restart'); };
    document.getElementById('intervalSave').onclick = saveInterval;
    updateActive(o);
  }

  function saveInterval(){
    var el = document.getElementById('intervalMin');
    var dmsg = document.getElementById('daemonMsg');
    var mins = parseFloat(el.value);
    if (!(mins > 0)) { dmsg.textContent = 'enter minutes > 0'; return; }
    var secs = Math.round(mins * 60);
    dmsg.textContent = 'saving interval…';
    post('/daemon/interval', {heartbeat_s: secs}).then(function(d){
      if (d.ok){
        dmsg.textContent = '✓ runs every ' + fmtInterval(d.daemon_cadence.heartbeat_s)
          + ' (applies on the next cycle)';
        setTimeout(function(){ dmsg.textContent = ''; }, 6000);
      } else { dmsg.textContent = 'error: ' + (d.error||'failed'); }
    }).catch(function(){ dmsg.textContent = 'error'; });
  }
  function fmtInterval(s){
    if (s % 60 === 0) return (s/60) + ' min';
    return s + 's';
  }

  function refreshOptions(){
    fetch(API + '/options').then(function(r){ return r.json(); })
      .then(function(o){ updateActive(o); }).catch(function(){});
  }
  function daemon(action){
    var dmsg = document.getElementById('daemonMsg');
    dmsg.textContent = action + 'ing… (starting can take ~30s while the model loads)';
    post('/daemon', {action: action}).then(function(d){
      dmsg.textContent = d.ok ? '' : ('error: ' + (d.error||'failed'));
      refreshOptions();
    }).catch(function(){ dmsg.textContent = 'error'; });
  }
  function updateDaemon(o){
    var dot = document.getElementById('daemonDot');
    var st = document.getElementById('daemonState');
    if (!dot) return;
    var on = !!o.daemon_active;
    dot.className = 'dot ' + (on ? 'on' : 'off');
    st.textContent = on ? 'daemon running' : 'daemon stopped';
    document.getElementById('dStart').disabled = on;
    document.getElementById('dStop').disabled = !on;
    // Reflect the current interval, but don't clobber the field while editing.
    var el = document.getElementById('intervalMin');
    if (el && o.daemon_cadence && document.activeElement !== el){
      el.value = +(o.daemon_cadence.heartbeat_s / 60).toFixed(2);
    }
  }

  function setAll(on){
    document.querySelectorAll('#ef-tools input').forEach(function(cb){
      cb.checked = on; cb.parentNode.className = on?'on':''; }); }
  document.getElementById('ef-tools-all').onclick = function(){ setAll(true); };
  document.getElementById('ef-tools-none').onclick = function(){ setAll(false); };
  document.getElementById('newExpBtn').onclick = function(){
    var f = document.getElementById('expForm');
    show(f, f.style.display === 'none');
    if (f.style.display !== 'none') document.getElementById('ef-label').focus();
  };
  document.getElementById('ef-cancel').onclick = function(){
    show(document.getElementById('expForm'), false); };

  document.getElementById('ef-start').onclick = function(){
    var label = val('ef-label');
    var st = document.getElementById('ef-status');
    if (!label){ st.textContent = 'a label is required'; return; }
    var tools = [].map.call(
      document.querySelectorAll('#ef-tools input:checked'),
      function(cb){ return cb.value; });
    var payload = { label: label, description: val('ef-desc'), goal: val('ef-goal'),
      hypothesis: val('ef-hyp'), model: document.getElementById('ef-model').value,
      tools: tools, prompt: document.getElementById('ef-prompt').value,
      fresh: document.getElementById('ef-fresh').checked };
    var btn = this; btn.disabled = true;
    st.textContent = 'starting… (switching models can take ~30s)';
    post('/experiment/create', payload).then(function(d){
      if (d.ok){ st.textContent = '✓ started — reloading'; location.reload(); }
      else { st.textContent = 'error: ' + (d.error||'failed'); btn.disabled = false; }
    }).catch(function(){ st.textContent = 'error starting'; btn.disabled = false; });
  };

  function rerunModal(name){
    var ov = document.createElement('div');
    ov.className = 'lab-modal';
    ov.innerHTML =
      '<div class="lab-modal-box"><h3>Run “'+esc(name)+'” again</h3>'
      + '<p class="muted">Starts another run under the same experiment, reusing '
      + 'its model, tools, and prompt. The lab groups the runs together and '
      + 'tracks how beliefs and memory evolve across them.</p>'
      + '<label class="ef-chk"><input type="radio" name="rmem" value="fresh" checked> '
      + '<span><b>Fresh replicate</b> — reset rolling memory. An independent '
      + 'trial under the same conditions (best for reproducibility).</span></label>'
      + '<label class="ef-chk"><input type="radio" name="rmem" value="keep"> '
      + '<span><b>Continue from last run</b> — restore the previous run\\'s '
      + 'memory and build on it.</span></label>'
      + '<div class="ef-actions"><button class="btn-primary" id="rerunGo">Run again</button>'
      + '<button class="ghost" id="rerunCancel">Cancel</button>'
      + '<span class="ef-status" id="rerunStatus"></span></div></div>';
    document.body.appendChild(ov);
    function close(){ ov.remove(); }
    ov.addEventListener('click', function(e){ if (e.target === ov) close(); });
    ov.querySelector('#rerunCancel').onclick = close;
    ov.querySelector('#rerunGo').onclick = function(){
      var keep = ov.querySelector('input[name=rmem]:checked').value === 'keep';
      var st = ov.querySelector('#rerunStatus'); st.textContent = 'starting…';
      var go = ov.querySelector('#rerunGo'); go.disabled = true;
      post('/experiment/rerun', {name: name, keep_memory: keep}).then(function(d){
        if (d.ok){ st.textContent = '✓ started — reloading'; location.reload(); }
        else { st.textContent = 'error: ' + (d.error||'failed'); go.disabled = false; }
      }).catch(function(){ st.textContent = 'error'; go.disabled = false; });
    };
  }

  function delExp(name){
    var typed = window.prompt('Delete experiment "'+name+'" and ALL of its runs?\\n'
      + 'This is permanent. Type the experiment name to confirm:');
    if (typed === null) return;
    if (typed.trim() !== name){ msg.textContent = 'name did not match — not deleted'; return; }
    msg.textContent = 'deleting…';
    post('/experiment/delete', {name: name}).then(function(d){
      if (d.ok){ location.reload(); }
      else { msg.textContent = 'error: ' + (d.error||'failed'); }
    }).catch(function(){ msg.textContent = 'error deleting'; });
  }

  function collect(){
    if (!window.confirm('Stop the active experiment and collect it? This '
      + 'packages the run, writes a summary, and resets rolling memory.')) return;
    msg.textContent = 'collecting…';
    post('/experiment/collect', {}).then(function(d){
      if (d.ok){ poll(); } else { msg.textContent = 'error: ' + (d.error||'failed'); }
    });
  }

  function findTile(name){
    var hit = null;
    document.querySelectorAll('.tile').forEach(function(t){
      if (t.getAttribute('data-exp') === name) hit = t; });
    return hit;
  }
  function markRunning(o){
    document.querySelectorAll('.tile.running').forEach(function(t){
      t.classList.remove('running');
      var b = t.querySelector('.running-badge'); if (b) b.remove(); });
    if (!o.active) return;
    var t = findTile(o.active.label);
    if (!t) return;
    t.classList.add('running');
    if (!t.querySelector('.running-badge')){
      var a = document.createElement('a');
      a.className = 'running-badge'; a.href = 'live.html';
      a.innerHTML = '<span class="running-dot">●</span> live';
      t.appendChild(a);
    }
    // Pending tiles have no inner link — make the whole tile open the live view.
    if (t.classList.contains('pending') && !t.dataset.livewired){
      t.dataset.livewired = '1';
      t.style.cursor = 'pointer';
      t.addEventListener('click', function(ev){
        if (ev.target.closest('.tile-del')) return;
        location.href = 'live.html';
      });
    }
  }

  function updateActive(o){
    updateDaemon(o);
    markRunning(o);
    var box = document.getElementById('activeBox');
    var newBtn = document.getElementById('newExpBtn');
    var col = o.collecting || {};
    if (col.running){
      show(box, true); newBtn.disabled = true;
      box.textContent = 'Collecting… ' + ((col.log && col.log.length)
        ? col.log[col.log.length-1] : 'working');
      setTimeout(poll, 1500); return;
    }
    if (o.active){
      show(box, true); newBtn.disabled = true;
      var tools = (o.active.tools_available && o.active.tools_available.length)
        ? o.active.tools_available.join(', ') : 'none';
      box.innerHTML = 'Active: <b>'+esc(o.active.label)+'</b> · '
        + esc(o.active.elapsed) + ' · ' + o.active.entries_so_far
        + ' entries · tools: ' + esc(tools)
        + ' <button class="danger" id="collectBtn">Stop &amp; collect</button>';
      document.getElementById('collectBtn').onclick = collect;
    } else { show(box, false); newBtn.disabled = false; }
  }

  function poll(){
    fetch(API + '/options').then(function(r){ return r.json(); }).then(function(o){
      updateActive(o);
      if (o.collecting && o.collecting.done && !o.collecting.running){
        msg.textContent = '✓ collected — reloading'; setTimeout(function(){
          location.reload(); }, 600);
      }
    }).catch(function(){});
  }

  function post(path, payload){
    return fetch(API + path, { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload||{}) })
      .then(function(r){ return r.json(); });
  }
})();
</script>"""


# --- live view (running experiment, preview only) --------------------------------------

def render_live_page(github):
    """A real-time view of the currently running experiment.

    Static shell only; all content is fetched at runtime from the local
    control API, so this page shows data only while `hannah_lab.py preview`
    is running (otherwise it explains that)."""
    body = ["""<div class="pagehead">
<h1 id="live-title">Live experiment</h1>
<p class="pagesub" id="live-sub">A real-time view of the experiment that is
running right now — its journal, tool calls, and counts as they happen. Live
only while the local <span class="mono">hannah_lab.py preview</span> server is
running.</p></div>
<div id="live-root"><p class="muted">Loading…</p></div>""", _LIVE_SCRIPT]
    return _page("Live", "\n".join(body), github=github, active="experiments",
                 crumbs=[("Experiments", "experiments.html"), ("Live", None)])


_LIVE_SCRIPT = """
<script>
(function(){
  var API = '/api/lab';
  var root = document.getElementById('live-root');
  var timer = null;
  function schedule(ms){ clearTimeout(timer); timer = setTimeout(load, ms); }
  function esc(s){ return (s==null?'':String(s)).replace(/[&<>"]/g,
    function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function fmtTime(iso){ try { return new Date(iso).toLocaleString(undefined,
    {month:'short',day:'numeric',hour:'numeric',minute:'2-digit',second:'2-digit'}); }
    catch(e){ return esc(iso); } }
  function card(n,l){ return '<div class="statcard"><div class="n">'+n
    +'</div><div class="l">'+esc(l)+'</div></div>'; }
  function meta(k,v){ return '<div><div class="k">'+esc(k)+'</div>'+v+'</div>'; }

  function load(){
    Promise.all([
      fetch(API+'/options').then(function(r){ if(!r.ok) throw 0; return r.json(); }),
      fetch(API+'/journal?limit=80').then(function(r){ return r.ok?r.json():{entries:[]}; })
    ]).then(function(res){ render(res[0], res[1].entries || []); })
      .catch(function(){
        root.innerHTML = '<div class="panel"><h2>Live view unavailable</h2>'
          + '<p class="muted">This page is live only while the local '
          + '<span class="mono">hannah_lab.py preview</span> server is running. '
          + '<a href="experiments.html">Back to experiments</a></p></div>';
      });
  }

  function render(o, entries){
    var col = o.collecting || {};
    if (col.running){
      root.innerHTML = '<div class="panel"><h2>Collecting…</h2><p class="muted">'
        + esc((col.log && col.log.length) ? col.log[col.log.length-1]
              : 'packaging the run') + '</p></div>';
      schedule(1500); return;
    }
    if (col.done && !o.active){ location.href = 'experiments.html'; return; }
    var a = o.active;
    var title = document.getElementById('live-title');
    var sub = document.getElementById('live-sub');
    if (!a){
      title.textContent = 'Nothing running';
      sub.textContent = '';
      root.innerHTML = '<div class="panel"><p class="muted">No experiment is '
        + 'active right now. <a href="experiments.html">Browse experiments</a> '
        + 'or start one there.</p></div>';
      schedule(4000); return;
    }
    title.innerHTML = esc(a.label)
      + ' <span class="chip ok"><span class="running-dot">●</span> running</span>';
    sub.textContent = '';

    var toolcalls = 0, usedcount = 0;
    entries.forEach(function(e){
      toolcalls += (e.tool_trace || []).length || (e.tools || []).length;
      if ((e.tools || []).length) usedcount++;
    });
    var tools = (a.tools_available && a.tools_available.length)
      ? a.tools_available.join(', ') : 'none';

    var html = '<div class="statrow">'
      + card(a.entries_so_far, 'entries so far')
      + card(toolcalls, 'tool calls (recent)')
      + card(usedcount, 'entries using tools')
      + card(esc(a.elapsed), 'elapsed')
      + '</div>';
    html += '<div class="panel"><div class="metagrid">'
      + meta('model', '<span class="mono">'+esc(a.model)+'</span>')
      + meta('tools offered', esc(tools))
      + meta('prompt', '<span class="mono">'+esc(a.prompt_fingerprint||'')+'</span>')
      + meta('started', fmtTime(a.started_at))
      + '</div><div style="margin-top:14px">'
      + '<button class="danger" id="collectBtn">Stop &amp; collect</button> '
      + '<span class="muted" id="livemsg"></span></div></div>';

    html += '<h2 style="margin-top:24px">Live journal</h2>';
    if (!entries.length){
      html += '<p class="muted">No entries yet this run — waiting for her next '
        + 'wake-up.</p>';
    }
    entries.forEach(function(e){
      var chips = (e.tools || []).map(function(t){
        return '<span class="chip tool-used">'+esc(t)+'</span>'; }).join('');
      var trace = '';
      if (e.tool_trace && e.tool_trace.length){
        trace = '<details class="invest"><summary>'+e.tool_trace.length
          + ' tool call'+(e.tool_trace.length!==1?'s':'')+'</summary>'
          + '<div class="chips">'+chips+'</div><div class="body">'
          + e.tool_trace.map(function(c){
              return '<details><summary>'+esc(c.tool)+'</summary>'
                + '<div class="body"><pre>'+esc(c.output)+'</pre></div></details>';
            }).join('') + '</div></details>';
      } else if (chips){ trace = '<div style="margin-top:8px">'+chips+'</div>'; }
      html += '<article class="entry card"><div class="when">'+fmtTime(e.time)
        + (e.model ? (' · '+esc(e.model)) : '') + '</div><div class="prose">'
        + esc(e.entry) + '</div>' + trace + '</article>';
    });
    root.innerHTML = html;

    var cb = document.getElementById('collectBtn');
    if (cb) cb.onclick = function(){
      if (!window.confirm('Stop the active experiment and collect it? This '
        + 'packages the run, writes a summary, and resets rolling memory.')) return;
      document.getElementById('livemsg').textContent = 'collecting…';
      fetch(API+'/experiment/collect', { method:'POST',
        headers:{'Content-Type':'application/json'}, body:'{}' })
        .then(function(r){ return r.json(); }).then(function(d){
          if (d.ok){ schedule(800); }
          else { document.getElementById('livemsg').textContent =
            'error: ' + (d.error||'failed'); }
        });
    };
    schedule(4000);  // poll the running experiment
  }

  load();
})();
</script>"""


# --- experiment pages -----------------------------------------------------------------

def _exp_crumbs(g, tab_label=None, root="../../"):
    crumbs = [("Experiments", f"{root}experiments.html")]
    if tab_label:
        crumbs.append((g["name"], "index.html"))
        crumbs.append((tab_label, None))
    else:
        crumbs.append((g["name"], None))
    return crumbs


def render_exp_overview(g, state, registry, github):
    name, runs = g["name"], g["runs"]
    meta = registry.get(name, {})
    latest = runs[-1]
    m = latest.manifest
    open_qs = [q for q in state.questions
               if q["status"] in ("open", "investigating")]

    body = [_exp_header(name, meta, "index.html",
                        subtitle=meta.get("description", "")
                        or m.get("note", ""))]

    detail_rows = []
    for k in ("goal", "hypothesis", "notes"):
        if meta.get(k):
            detail_rows.append(f"<div><div class='k'>{k}</div>"
                               f"{esc(meta[k])}</div>")
    used = sorted({t for r in runs for t in (r.manifest.get("tools_used") or [])})
    tools_html = "".join(_tool_chip(t, used=t in used)
                         for t in m.get("tools_available", []))
    detail_rows.append(f"<div><div class='k'>tools (green = used)</div>"
                       f"{tools_html}</div>")
    detail_rows.append(f"<div><div class='k'>model</div>"
                       f"<span class='mono'>{esc(m.get('model'))}</span></div>")
    detail_rows.append(f"<div><div class='k'>prompt hash</div>"
                       f"<span class='mono'>{esc(m.get('prompt_hash'))}</span></div>")
    body.append(f'<div class="panel"><div class="metagrid">'
                f'{"".join(detail_rows)}</div></div>')

    body.append(_stat_cards([
        (len(runs), "runs"),
        (sum(len(r.entries) for r in runs), "entries"),
        (sum(r.manifest.get("tool_call_count", 0) for r in runs), "tool calls"),
        (sum(r.failure_count for r in runs), "failures"),
        (len(state.beliefs), "beliefs"),
        (len(open_qs), "open questions"),
        (latest.score.get("score"), "latest score"),
    ]))

    body.append('<div class="panel"><div class="panel-head"><h2>Runs</h2>'
                '<a class="panel-link" href="runs.html">all runs →</a></div>')
    body.append(_runs_table(runs, limit=5))
    body.append("</div>")

    diff = state.changes_by_run.get(latest.run_id, {})
    changed = _changes_list(diff)
    if changed:
        body.append('<details class="panel-details"><summary>What the latest '
                    'run changed</summary><div class="body"><ul>'
                    + "".join(changed)
                    + '</ul><p class="muted"><a href="timeline.html">Run-by-run '
                    'changes →</a></p></div></details>')

    body.append('<div class="cols">')
    if state.beliefs:
        rows = "".join(f"<li>{esc(b['statement'])} "
                       f"{_conf_chip(b['confidence'])}</li>"
                       for b in state.beliefs[:4])
        body.append(f"""<div class="panel">
<div class="panel-head"><h2>Beliefs</h2>
<a class="panel-link" href="beliefs.html">all beliefs →</a></div>
<ul class="cleanlist">{rows}</ul></div>""")
    if open_qs:
        rows = "".join(f"<li>{esc(q['text'])} {_status_chip(q['status'])}</li>"
                       for q in open_qs[:4])
        body.append(f"""<div class="panel">
<div class="panel-head"><h2>Open questions</h2>
<a class="panel-link" href="questions.html">all questions →</a></div>
<ul class="cleanlist">{rows}</ul></div>""")
    body.append("</div>")

    pick = next((e for e in reversed(latest.entries)
                 if e.text.strip() and not e.blocked), None)
    if pick:
        body.append(f"""<div class="panel">
<div class="panel-head"><h2>Latest journal entry</h2>
<a class="panel-link" href="journal.html">the full journal →</a></div>
<div class="entry"><div class="when">{_fmt_time(pick.time)} · run
{_run_link(latest.run_id)}</div>
<div class="prose">{esc(_truncate(pick.text, 600))}</div></div></div>""")

    return _page(name, "\n".join(body), root="../../", github=github,
                 active="experiments", crumbs=_exp_crumbs(g))


def render_exp_runs(g, state, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}), "runs.html"),
            "<p class='muted'>Every collected run of this experiment, newest "
            "first. Each run is one window in which Hannah wakes repeatedly, "
            "investigates through tools, and writes.</p>",
            '<div class="panel">', _runs_table(g["runs"]), "</div>"]
    return _page(f"Runs · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Runs"))


def render_exp_journal(g, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}), "journal.html"),
            "<p class='muted'>Every public-safe entry this experiment "
            "produced, newest first. Tool calls and detected failures are "
            "attached to the entry that caused them.</p>"]
    items = [(e, r.run_id) for r in g["runs"] for e in r.entries
             if e.text.strip()]
    items.sort(key=lambda x: x[0].time, reverse=True)
    day = None
    for e, rid in items:
        d = (e.time or "")[:10]
        if d != day:
            day = d
            body.append(f'<div class="dayhead">{esc(d)}</div>')
        body.append(_entry_html(e, rid, link_run=True))
    if not items:
        body.append("<p class='muted'>No entries published yet.</p>")
    return _page(f"Journal · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Journal"))


def render_run_detail(run, diff, state, g, registry, github):
    m = run.manifest
    name = g["name"]
    body = [_exp_header(name, registry.get(name, {}), "runs.html",
                        prefix="../")]
    body.append(f"<h1 class='runtitle'>Run <span class='mono'>"
                f"{esc(run.run_id)}</span></h1>"
                f"<p class='muted'>{_fmt_date(m.get('started_at'))} → "
                f"{_fmt_date(m.get('ended_at'))} ({esc(m.get('duration'))}) · "
                f"model <span class='mono'>{esc(m.get('model'))}</span> · "
                f"prompt <span class='mono'>{esc(m.get('prompt_hash'))}</span></p>")

    body.append(_stat_cards([
        (m.get("entry_count"), "entries"),
        (m.get("tool_call_count"), "tool calls"),
        (run.failure_count, "failures"),
        (run.score.get("score"), "score"),
    ]))

    used = set(m.get("tools_used") or [])
    body.append('<div class="panel"><div class="panel-head"><h2>Available '
                'tools</h2></div>'
                + "".join(_tool_chip(t, used=t in used)
                          for t in m.get("tools_available", []))
                + "<p class='muted'>Green = Hannah chose to use this tool "
                "during the run. She is never told to use tools; which ones "
                "she reaches for is part of the experiment.</p></div>")

    first_obs = next((e.observation for e in run.entries if e.observation), "")
    body.append('<details class="panel-details"><summary>Initial prompt'
                '</summary><div class="body">'
                f"<pre>{esc(first_obs) or '(observation not retained)'}</pre>"
                "</div></details>")

    # What this run changed.
    mem_by_id = {x["id"]: x for x in state.memories}
    bel_by_id = {b["id"]: b for b in state.beliefs}
    sections = [
        ("New memories", [mem_by_id[i]["content"] for i in
                          diff.get("new_memories", []) if i in mem_by_id]),
        ("Updated memories", [mem_by_id[i]["content"] for i in
                              diff.get("updated_memories", []) if i in mem_by_id]),
        ("New beliefs", [bel_by_id[i]["statement"] for i in
                         diff.get("new_beliefs", []) if i in bel_by_id]),
        ("Belief confidence changes",
         [f"{bel_by_id[i]['statement']} (now {bel_by_id[i]['confidence']})"
          for i in diff.get("belief_confidence_changes", []) if i in bel_by_id]),
        ("Questions opened", diff.get("new_questions", [])),
        ("Contradictions", diff.get("new_contradictions", [])),
    ]
    inner = []
    for label, items in sections:
        if not items:
            continue
        inner.append(f"<h3>{label}</h3><ul>")
        inner.extend(f"<li>{esc(x)}</li>" for x in items[:12])
        if len(items) > 12:
            inner.append(f"<li class='muted'>… and {len(items) - 12} more</li>")
        inner.append("</ul>")
    body.append('<details class="panel-details" open><summary>What this run '
                'changed</summary><div class="body">'
                + ("".join(inner) or
                   "<p class='muted'>No derived state changed in this run.</p>")
                + "</div></details>")

    # Score: headline number always visible, component table collapsed.
    comp_rows = "".join(
        f"<tr><td class='mono'>{esc(cname)}</td>"
        f"<td>{_chip(c['result'], _status_kind(c['result']))}</td>"
        f"<td class='muted'>{esc(c['detail'])}</td></tr>"
        for cname, c in run.score.get("components", {}).items())
    body.append(f"""<div class="panel"><div class="panel-head"><h2>Score</h2>
</div><div class="scorebar"><span class="score-big">{run.score.get('score')}
</span><span class="muted">{run.score.get('passed')}/{run.score.get('applicable')}
checks passed (rule-based, heuristic)</span></div>
<details><summary>Score components</summary><div class="body">
<table>{comp_rows}</table></div></details></div>""")

    if run.summary:
        body.append('<details class="panel-details"><summary>AI-written run '
                    'summary (sanitized)</summary><div class="body">'
                    f"<pre>{esc(run.summary)}</pre></div></details>")

    body.append('<div class="panel-head sect"><h2>The investigation, entry by '
                'entry</h2></div>')
    body.append("<p class='muted'>Each cycle: what Hannah was given, which "
                "tools she chose, what they returned, and what she wrote. "
                "Failure heuristics are flagged inline; tool outputs are "
                "collapsed.</p>")
    for e in run.entries:
        body.append(_entry_html(e, run.run_id))

    art_rows = "".join(
        f"<li><a href='{esc(run.run_id)}/{fn}' class='mono'>{fn}</a> — {label}"
        "</li>"
        for label, fn in [("Public manifest", "public_manifest.json"),
                          ("Journal (markdown)", "journal.md"),
                          ("Tool trace", "tool_trace.public.json"),
                          ("Memory changes", "memory_changes.public.json"),
                          ("Belief changes", "belief_changes.public.json"),
                          ("Questions", "questions.public.json"),
                          ("Score", "score.json"),
                          ("Failures", "failures.json"),
                          ("Run summary", "run_summary.json")])
    body.append('<details class="panel-details"><summary>Raw public artifacts '
                '(JSON)</summary><div class="body"><ul class="cleanlist">'
                + art_rows + "</ul></div></details>")

    crumbs = [("Experiments", "../../../experiments.html"),
              (name, "../index.html"), ("Runs", "../runs.html"),
              (run.run_id, None)]
    return _page(f"Run {run.run_id} · {name}", "\n".join(body),
                 root="../../../", github=github, active="experiments",
                 crumbs=crumbs)


def render_exp_memory(g, state, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}), "memory.html"),
            "<p class='muted'>What this experiment left behind, sanitized. "
            "Environment facts are derived directly from Hannah's tool "
            "observations; observations, patterns and reflections come from "
            "her journals.</p>"]
    types = sorted({m["type"] for m in state.memories})
    if types:
        body.append("<p>" + "".join(_chip(t, "neutral") for t in types) + "</p>")
    for mem in state.memories:
        runs_html = " · ".join(_run_link(r) for r in
                               dict.fromkeys(mem.get("source_runs",
                                                     [mem.get("source_run")])))
        updated = (f"<div><div class='k'>updated</div>"
                   f"{_fmt_date(mem['updated'])}</div>"
                   if mem.get("updated") and mem["updated"] != mem.get("created")
                   else "")
        tags = "".join(_chip(t, "neutral") for t in mem.get("tags", []) if t)
        body.append(f"""<div class="panel">
<div class="k mono">{esc(mem['id'])} · {esc(mem['type'])}</div>
<p>{esc(mem['content'])}</p>
<div class="metagrid">
<div><div class="k">created</div>{_fmt_date(mem.get('created'))}</div>
{updated}
<div><div class="k">confidence</div>{_conf_chip(mem.get('confidence', 'low'))}</div>
<div><div class="k">source run(s)</div>{runs_html}</div>
</div>
<p>{tags}</p></div>""")
    if not state.memories:
        body.append("<p class='muted'>No memories derived yet.</p>")
    return _page(f"Memory · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Memory"))


def render_exp_beliefs(g, state, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}), "beliefs.html"),
            "<p class='muted'>What this experiment's observations support "
            "believing about the environment. Confidence rises when runs "
            "agree and falls when they contradict; contradictions are kept "
            "visible, not smoothed over.</p>"]
    trend_sym = {"up": "▲ increased", "down": "▼ decreased",
                 "steady": "— steady", "new": "new"}
    for b in state.beliefs:
        contra = ("None currently known" if not b.get("contradictions") else
                  "; ".join(
                      f"{_fmt_date(c['time'])}: previously "
                      f"{esc(c['previous_value'])}, then {esc(c['new_value'])} "
                      f"(run {esc(c['run_id'])})"
                      for c in b["contradictions"]))
        ev_runs = " · ".join(_run_link(r) for r in
                             dict.fromkeys(b.get("evidence_runs", [])))
        detail = f"<p class='muted'>{esc(b['detail'])}</p>" if b.get("detail") else ""
        body.append(f"""<div class="panel">
<h3>{esc(b['statement'])}</h3>{detail}
<div class="metagrid">
<div><div class="k">confidence</div>{_conf_chip(b['confidence'])}</div>
<div><div class="k">direction</div>{trend_sym.get(b.get('trend'), '—')}</div>
<div><div class="k">observations</div>{b.get('observations')}
via <span class="mono">{esc(b.get('tool'))}</span></div>
<div><div class="k">last updated</div>{_fmt_date(b.get('last_updated'))}</div>
<div><div class="k">evidence</div>{ev_runs}</div>
<div><div class="k">contradictions</div>{contra}</div>
</div></div>""")
    if not state.beliefs:
        body.append("<p class='muted'>No beliefs formed yet — they appear once "
                    "tool observations accumulate.</p>")
    return _page(f"Beliefs · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Beliefs"))


def render_exp_questions(g, state, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}),
                        "questions.html"),
            "<p class='muted'>Questions Hannah asked during this experiment, "
            "deduplicated across its runs. A question asked again in the "
            "latest run is under investigation; one that stopped appearing is "
            "marked abandoned (abandonment is itself data).</p>"]
    order = {"open": 0, "investigating": 1, "answered": 2, "abandoned": 3}
    for q in sorted(state.questions,
                    key=lambda q: (order.get(q["status"], 9),
                                   q["last_asked"] or "")):
        nxt = (f"<div><div class='k'>possible next tool</div>"
               f"<span class='mono'>{esc(q['suggested_tool'])}</span></div>"
               if q.get("suggested_tool") else "")
        runs_html = " · ".join(_run_link(r) for r in q.get("runs", []))
        body.append(f"""<div class="panel">
<h3>{esc(q['text'])}</h3>
<div class="metagrid">
<div><div class="k">status</div>{_status_chip(q['status'])}</div>
<div><div class="k">first asked</div>{_fmt_date(q['created'])}</div>
<div><div class="k">last asked</div>{_fmt_date(q['last_asked'])}</div>
<div><div class="k">asked in</div>{len(q.get('runs', []))} run(s) — {runs_html}</div>
{nxt}
</div></div>""")
    if not state.questions:
        body.append("<p class='muted'>No questions extracted yet.</p>")
    return _page(f"Questions · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Questions"))


def render_exp_timeline(g, state, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}),
                        "timeline.html"),
            "<p class='muted'>How this experiment's memory and beliefs "
            "evolved, and what each run changed. Newest first.</p>"]

    for r in reversed(g["runs"]):
        d = state.changes_by_run.get(r.run_id)
        if not d:
            continue
        body.append(f'<details class="panel-details"><summary>What run '
                    f'{esc(r.run_id)} changed</summary><div class="body"><ul>')
        body.extend(_changes_list(d) or
                    ["<li class='muted'>no derived changes</li>"])
        body.append("</ul></div></details>")

    body.append('<div class="panel-head sect"><h2>Event stream</h2></div>')
    for ev in reversed(state.timeline[-400:]):
        icon = EVENT_ICONS.get(ev["kind"], "·")
        detail = (f"<div class='muted'>{esc(ev['detail'])}</div>"
                  if ev.get("detail") else "")
        body.append(f"""<div class="tl-row">
<div class="tl-icon {esc(ev['kind'])}">{icon}</div>
<div class="tl-body"><div class="when">{_fmt_time(ev['time'])} ·
{esc(ev['kind'].replace('_', ' '))} · run {_run_link(ev['run_id'])}</div>
{esc(ev['title'])}{detail}</div></div>""")
    return _page(f"Timeline · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Timeline"))


def render_exp_failures(g, github, registry):
    body = [_exp_header(g["name"], registry.get(g["name"], {}),
                        "failures.html"),
            "<p class='muted'>This experiment's failure wall. Failures are "
            "lab artifacts, not embarrassments: unsupported claims, calls to "
            "tools that don't exist, loops, tool errors, sanitizer blocks. "
            "Detection is heuristic and itself imperfect — which is also on "
            "display here.</p>"]
    all_fails = [(e.time, f, r.run_id)
                 for r in g["runs"] for e in r.entries for f in e.failures]
    by_type = {}
    for _, f, _ in all_fails:
        by_type[f["type"]] = by_type.get(f["type"], 0) + 1
    if by_type:
        body.append(_stat_cards([(n, t.replace("_", " ")) for t, n in
                                 sorted(by_type.items(), key=lambda kv: -kv[1])]))
    all_fails.sort(key=lambda x: x[0], reverse=True)
    for t, f, rid in all_fails:
        body.append(
            f'<div class="failrow"><div class="when">{_fmt_time(t)} · '
            f'{esc(f["type"])} · run {_run_link(rid)}</div>'
            f'{esc(f["detail"])}</div>')
    if not all_fails:
        body.append("<p class='muted'>No failures detected yet — suspicious "
                    "in itself.</p>")
    return _page(f"Failures · {g['name']}", "\n".join(body), root="../../",
                 github=github, active="experiments",
                 crumbs=_exp_crumbs(g, "Failures"))


# --- build -------------------------------------------------------------------------

def build_site(groups, states, global_state, manifests: dict, site_dir: Path,
               runs_dir: Path, registry: dict = None,
               github: str = DEFAULT_GITHUB) -> None:
    """Render the console-style site and copy public artifacts into it."""
    registry = registry or {}
    run_slug = {r.run_id: g["slug"] for g in groups for r in g["runs"]}

    # The site is fully generated; start clean so removed pages don't linger.
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True)
    (site_dir / "style.css").write_text(_CSS, encoding="utf-8")

    (site_dir / "index.html").write_text(
        render_dashboard(groups, states, global_state, run_slug, github),
        encoding="utf-8")
    (site_dir / "experiments.html").write_text(
        render_experiments_list(groups, registry, github),
        encoding="utf-8")
    (site_dir / "live.html").write_text(
        render_live_page(github), encoding="utf-8")

    for g in groups:
        name = g["name"]
        state = states[name]
        exp_dir = site_dir / "experiments" / g["slug"]
        (exp_dir / "runs").mkdir(parents=True)

        pages = {
            "index.html": render_exp_overview(g, state, registry, github),
            "journal.html": render_exp_journal(g, github, registry),
            "runs.html": render_exp_runs(g, state, github, registry),
            "memory.html": render_exp_memory(g, state, github, registry),
            "beliefs.html": render_exp_beliefs(g, state, github, registry),
            "questions.html": render_exp_questions(g, state, github, registry),
            "timeline.html": render_exp_timeline(g, state, github, registry),
            "failures.html": render_exp_failures(g, github, registry),
        }
        for fn, content in pages.items():
            (exp_dir / fn).write_text(content, encoding="utf-8")

        for run in g["runs"]:
            diff = state.changes_by_run.get(run.run_id, {})
            (exp_dir / "runs" / f"{run.run_id}.html").write_text(
                render_run_detail(run, diff, state, g, registry, github),
                encoding="utf-8")
            src = runs_dir / run.run_id / "public"
            dst = exp_dir / "runs" / run.run_id
            if src.exists():
                dst.mkdir(exist_ok=True)
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst / f.name)

    (site_dir / "lab_state.json").write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "experiments": [{
            "name": g["name"],
            "slug": g["slug"],
            "runs": [manifests.get(r.run_id, {}) for r in g["runs"]],
            "memories": states[g["name"]].memories,
            "beliefs": states[g["name"]].beliefs,
            "questions": states[g["name"]].questions,
            "contradictions": states[g["name"]].contradictions,
        } for g in groups],
        "lab_wide_beliefs": global_state.beliefs,
    }, indent=2, ensure_ascii=False), encoding="utf-8")


# --- stylesheet ----------------------------------------------------------------------

_CSS = """
:root {
  --side-bg:#151920; --side-text:#aab2bf; --side-active:#ffffff;
  --accent:#0e8a4f; --accent-soft:#e5f4ec;
  --bg:#f4f5f7; --panel:#ffffff; --border:#e2e5ea;
  --text:#1c2128; --body:#343b45; --muted:#69707c; --dim:#9aa1ac;
  --red:#b4232a; --red-soft:#fbeaea; --amber:#8a6116; --amber-soft:#faf3e0;
  --blue:#20537d; --blue-soft:#e8f0f8;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; background:var(--bg); color:var(--body);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,
  "Helvetica Neue",sans-serif; font-size:14px; line-height:1.6; }

/* ---- sidebar ---- */
.side { position:fixed; inset:0 auto 0 0; width:212px; background:var(--side-bg);
  display:flex; flex-direction:column; padding:18px 0 14px; z-index:20; }
.side-brand { color:#fff; text-decoration:none; font-size:15px; font-weight:700;
  letter-spacing:.12em; padding:2px 20px 16px; border-bottom:1px solid #232936;
  margin-bottom:10px; }
.side-brand .brand-sub { color:#57d992; font-weight:400; margin-left:6px;
  letter-spacing:.2em; font-size:11px; }
.dotlive { display:inline-block; width:7px; height:7px; border-radius:50%;
  background:#57d992; margin-right:9px; box-shadow:0 0 8px #57d99288;
  vertical-align:2px; }
.side-nav { display:flex; flex-direction:column; gap:2px; padding:6px 10px; }
.side-link { color:var(--side-text); text-decoration:none; font-size:13.5px;
  padding:9px 12px; border-radius:8px; display:flex; align-items:center; gap:10px;
  border-left:3px solid transparent; }
.side-link:hover { background:#1d232d; color:#e6eaf0; }
.side-link.active { background:#1f2a24; color:var(--side-active);
  border-left-color:#57d992; }
.side-ico { width:16px; text-align:center; color:#57d992; font-size:12px; }
.side-foot { margin-top:auto; padding:14px 22px 0; border-top:1px solid #232936;
  display:flex; flex-direction:column; gap:4px; }
.side-foot a { color:#8791a0; font-size:12.5px; text-decoration:none; }
.side-foot a:hover { color:#c6cdd8; }
.side-foot span { color:#525b69; font-size:11px; letter-spacing:.08em;
  text-transform:uppercase; }

/* ---- main column ---- */
.main { margin-left:212px; min-height:100vh; display:flex; flex-direction:column; }
.crumbbar { background:var(--panel); border-bottom:1px solid var(--border);
  padding:12px 28px; font-size:12.5px; color:var(--muted);
  position:sticky; top:0; z-index:10; }
.crumbbar a { color:var(--muted); text-decoration:none; }
.crumbbar a:hover { color:var(--text); text-decoration:underline; }
.crumbbar span { color:var(--text); }
.crumbbar .sep { color:var(--dim); }
.content { padding:26px 28px 60px; max-width:1080px; width:100%; }

/* ---- headers ---- */
.pagehead { margin-bottom:20px; }
.pagehead h1 { font-size:22px; font-weight:600; color:var(--text); margin:0 0 6px; }
.pagesub { color:var(--muted); max-width:76ch; margin:0 0 4px; }
.runtitle { font-size:19px; font-weight:600; color:var(--text); margin:18px 0 4px; }
h2 { font-size:12px; font-weight:600; letter-spacing:.1em; text-transform:uppercase;
  color:var(--muted); margin:0; }
h3 { font-size:14.5px; font-weight:600; color:var(--text); margin:14px 0 6px; }
a { color:#0c6e40; }
.k { font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--dim); margin-bottom:2px; }
.mono { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
.muted { color:var(--muted); font-size:13px; }

/* tabs inside an experiment */
.tabs { display:flex; gap:2px; flex-wrap:wrap; margin-top:14px;
  border-bottom:1px solid var(--border); }
.tabs a { color:var(--muted); text-decoration:none; font-size:13px;
  padding:7px 13px; border-radius:8px 8px 0 0;
  border-bottom:2px solid transparent; }
.tabs a:hover { color:var(--text); background:#eceef1; }
.tabs a.active { color:var(--text); font-weight:600;
  border-bottom-color:var(--accent); }

/* ---- stat cards ---- */
.statrow { display:grid; grid-template-columns:repeat(auto-fit,minmax(118px,1fr));
  gap:12px; margin:0 0 20px; }
.statcard { background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:13px 16px; }
.statcard .n { font-size:24px; font-weight:600; color:var(--text); line-height:1.15; }
.statcard .l { font-size:10.5px; letter-spacing:.09em; text-transform:uppercase;
  color:var(--dim); margin-top:2px; }

/* ---- panels ---- */
.panel { background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:16px 18px; margin-bottom:16px; }
.panel-head { display:flex; align-items:baseline; gap:12px; margin-bottom:10px; }
.panel-head.sect { margin-top:28px; }
.panel-link { margin-left:auto; font-size:12.5px; text-decoration:none;
  white-space:nowrap; }
.panel-link:hover { text-decoration:underline; }
.cols { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
  gap:16px; margin-bottom:16px; }
.cols .panel { margin-bottom:0; }
.metagrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
  gap:12px 20px; }
.cleanlist { margin:0; padding-left:18px; }
.cleanlist li { margin:6px 0; }

/* collapsed sections */
.panel-details { background:var(--panel); border:1px solid var(--border);
  border-radius:10px; margin-bottom:16px; }
details > summary { cursor:pointer; list-style:none; user-select:none;
  font-size:13.5px; color:var(--text); font-weight:600; padding:13px 18px; }
details > summary::-webkit-details-marker { display:none; }
details > summary::before { content:"\\25B8"; color:var(--dim); margin-right:9px; }
details[open] > summary::before { content:"\\25BE"; }
details .body { padding:0 18px 16px; }
details details { border:1px solid var(--border); border-radius:8px;
  margin:8px 0; background:#fafbfc; }
details details > summary { font-weight:500; font-size:12.5px; padding:9px 13px; }
details details .body { padding:0 13px 12px; }

/* ---- experiment tiles ---- */
.tiles { display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:16px; }
.tile { display:block; background:var(--panel); border:1px solid var(--border);
  border-radius:12px; padding:18px 20px; text-decoration:none;
  transition:border-color .12s, box-shadow .12s; }
.tile:hover { border-color:var(--accent); box-shadow:0 2px 10px #0e8a4f1a; }
.tile-head { display:flex; align-items:center; gap:10px; margin-bottom:6px; }
.tile-name { font-size:15.5px; font-weight:600; color:var(--text); }
.tile-desc { color:var(--muted); font-size:13px; margin:0 0 12px; min-height:2.6em; }
.tile-meta { display:flex; gap:14px; flex-wrap:wrap; color:var(--dim);
  font-size:11.5px; letter-spacing:.03em; }

/* ---- chips ---- */
.chip { display:inline-block; padding:1px 9px; border-radius:20px; font-size:11px;
  border:1px solid transparent; margin:1px 2px; vertical-align:1px; }
.chip.ok { background:var(--accent-soft); color:#0b6b3d; border-color:#bfe3cf; }
.chip.bad { background:var(--red-soft); color:var(--red); border-color:#eec7c9; }
.chip.warn { background:var(--amber-soft); color:var(--amber); border-color:#ecd9a8; }
.chip.info, .chip.neutral { background:#eef0f3; color:var(--muted);
  border-color:#dde1e7; }
.chip.tool { background:#f2f4f7; color:var(--muted); border-color:#dde1e7;
  font-family:ui-monospace,monospace; font-size:10.5px; }
.chip.tool-used { background:var(--accent-soft); color:#0b6b3d;
  border-color:#bfe3cf; font-family:ui-monospace,monospace; font-size:10.5px; }

/* ---- tables ---- */
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); }
th { color:var(--dim); font-size:10.5px; letter-spacing:.09em;
  text-transform:uppercase; font-weight:600; }
tr:last-child td { border-bottom:none; }
tr:hover td { background:#f7f8fa; }
td a { text-decoration:none; }
td a:hover { text-decoration:underline; }

/* ---- journal entries ---- */
.entry.card { background:var(--panel); border:1px solid var(--border);
  border-radius:10px; padding:16px 18px; margin-bottom:14px; }
.entry .when { font-size:11.5px; color:var(--dim); letter-spacing:.04em;
  margin-bottom:8px; }
.entry .prose { font-family:Georgia,"Times New Roman",serif; font-size:15.5px;
  line-height:1.75; color:var(--body); white-space:pre-wrap; }
.entry .from { margin-top:10px; font-size:12px; color:var(--dim); }
.entry .from a { color:var(--muted); }
.dayhead { font-size:12px; font-weight:600; letter-spacing:.1em;
  text-transform:uppercase; color:var(--dim); margin:26px 0 12px;
  padding-bottom:6px; border-bottom:1px solid var(--border); }
.invest { margin-top:12px; border:1px solid var(--border); border-radius:8px;
  background:#fafbfc; }
.invest > summary { color:var(--muted); font-size:12px; font-weight:500;
  padding:8px 13px; }
.invest .chips { padding:2px 13px 8px; display:flex; gap:6px; flex-wrap:wrap; }
.invest .body { padding:0 13px 12px; }
pre { white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-size:11.5px; color:#3c434d; background:#f6f8fa; border:1px solid var(--border);
  border-radius:8px; padding:12px; overflow-x:auto; margin:6px 0; }

/* ---- score & failures ---- */
.scorebar { display:flex; align-items:baseline; gap:14px; margin-bottom:8px; }
.score-big { font-size:32px; font-weight:600; color:#0b6b3d; line-height:1; }
.failrow { border-left:3px solid #d9a0a3; background:var(--panel);
  border-top:1px solid var(--border); border-right:1px solid var(--border);
  border-bottom:1px solid var(--border);
  padding:9px 14px; margin:8px 0; border-radius:0 8px 8px 0; font-size:13px; }
.failrow .when { font-size:11px; color:var(--dim); margin-bottom:2px; }

/* ---- timeline ---- */
.tl-row { display:flex; gap:14px; margin:0 0 12px; }
.tl-icon { flex:0 0 30px; height:30px; border-radius:50%;
  border:1px solid var(--border); background:var(--panel); display:flex;
  align-items:center; justify-content:center; font-size:13px; color:var(--muted); }
.tl-icon.belief_created, .tl-icon.belief_changed { color:#0b6b3d; }
.tl-icon.failures, .tl-icon.contradiction { color:var(--red); }
.tl-icon.question_opened { color:var(--amber); }
.tl-body { border-left:1px solid var(--border); padding-left:14px; flex:1;
  font-size:13px; padding-bottom:4px; }
.tl-body .when { font-size:11px; color:var(--dim); margin-bottom:2px; }

footer { margin-top:60px; padding-top:16px; border-top:1px solid var(--border);
  color:var(--dim); font-size:12px; }
footer a { color:var(--muted); }

/* ---- responsive ---- */
@media (max-width:760px) {
  .side { position:static; width:auto; flex-direction:row; align-items:center;
    padding:10px 14px; gap:8px; }
  .side-brand { border:none; padding:0 10px 0 0; margin:0; }
  .side-nav { flex-direction:row; padding:0; }
  .side-link { border-left:none; border-bottom:2px solid transparent;
    border-radius:8px; padding:6px 10px; }
  .side-link.active { border-left:none; }
  .side-foot { display:none; }
  .main { margin-left:0; }
  .content { padding:18px 14px 50px; }
  .crumbbar { padding:10px 14px; }
}
"""
