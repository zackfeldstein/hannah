#!/usr/bin/env python3
"""A tiny, dependency-free web window into Hannah.

Serves a single calm page that shows a live "now" panel (time, uptime, heat,
power, presence, and whether Hannah is awake) plus her journal feed. Everything
is read-only and built on the Python standard library.

Run directly (uses config.json for host/port):
    python3 hannah_web.py
"""

import json
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import hannah
import hannah_run as hr

# Tracks a backgrounded "stop experiment" (collect) so the UI can poll progress.
_collect = {"running": False, "log": [], "done": False, "error": None, "result": None}


def _collect_worker(cfg, summarize, local):
    _collect.update(running=True, done=False, error=None, result=None, log=[])

    def log(msg):
        _collect["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    try:
        _collect["result"] = hr.collect_run(
            summarize=summarize, local=local, cfg=cfg, log=log)
    except Exception as exc:  # surface any failure to the UI
        _collect["error"] = str(exc)
        log(f"ERROR: {exc}")
    finally:
        _collect.update(running=False, done=True)

# Cache live telemetry briefly so a refresh-happy browser can't spin sysfs/`who`.
_now_cache = {"time": 0.0, "data": None}


def _status_from_heartbeat(hb, sense_tick_s: int) -> str:
    """Infer whether Hannah is awake, resting, offline, or unknown."""
    if not hb or "time" not in hb:
        return "unknown"
    age = time.time() - hb["time"]
    if age <= max(60, 3 * sense_tick_s):
        return "awake"
    return "resting" if hb.get("graceful") else "offline"


def _build_now(cfg: dict) -> dict:
    """Assemble the live 'now' snapshot for the header panel."""
    m = hannah.collect_metrics()
    hb = hannah.read_heartbeat()
    status = _status_from_heartbeat(hb, cfg["daemon"]["sense_tick_s"])

    presence = "no one is logged in"
    if m.get("sessions"):
        presence = "; ".join(m.get("session_detail", m.get("users", [])))

    return {
        "now": datetime.now().strftime("%A %H:%M:%S"),
        "status": status,
        "uptime": hannah._format_duration(m["uptime_s"]) if m.get("uptime_s") else None,
        "temp_c": m.get("temp_max_c"),
        "temp_zone": m.get("temp_max_zone"),
        "power_w": m.get("power_w"),
        "cpu_mhz": m.get("cpu_mhz"),
        "load1": m.get("load1"),
        "cores": m.get("cores"),
        "mem_used_mib": m.get("mem_used_mib"),
        "mem_total_mib": m.get("mem_total_mib"),
        "presence": presence,
        "themes": hannah.load_themes(),
        "model": hannah.selected_model_name(cfg),
    }


def _get_now(cfg: dict) -> dict:
    age = time.time() - _now_cache["time"]
    if _now_cache["data"] is None or age >= cfg["web"]["cache_s"]:
        _now_cache["data"] = _build_now(cfg)
        _now_cache["time"] = time.time()
    return _now_cache["data"]


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hannah</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: #0c0d10; color: #d6d8de;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif;
    line-height: 1.6;
  }
  .wrap { max-width: 760px; margin: 0 auto; padding: 32px 20px 80px; }
  header h1 { font-weight: 300; letter-spacing: .12em; margin: 0 0 4px; font-size: 28px; }
  header .sub { color: #6b6f7a; font-size: 13px; margin-bottom: 24px; }
  .now {
    border: 1px solid #1c1e24; border-radius: 12px; padding: 16px 18px;
    background: #101217; margin-bottom: 36px;
  }
  .now .line1 { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #444; flex: none; }
  .dot.awake { background: #4ad07d; box-shadow: 0 0 8px #4ad07d88; }
  .dot.resting { background: #d0a24a; }
  .dot.offline { background: #d0574a; }
  .status { text-transform: uppercase; letter-spacing: .1em; font-size: 12px; color: #9aa0ad; }
  .clock { margin-left: auto; color: #6b6f7a; font-size: 13px; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px 18px; }
  .metric .k { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #6b6f7a; }
  .metric .v { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 15px; color: #cdd0d7; }
  .presence { margin-top: 12px; font-size: 13px; color: #8b909b; }
  .modelrow { margin-top: 12px; display: flex; align-items: center; gap: 10px; font-size: 13px; flex-wrap: wrap; }
  .modelrow label { color: #6b6f7a; text-transform: uppercase; letter-spacing: .08em; font-size: 11px; }
  .modelrow select {
    background: #0a0b0e; color: #cdd0d7; border: 1px solid #23262e; border-radius: 8px;
    padding: 6px 10px; font-size: 13px; font-family: inherit;
  }
  .themes { margin-top: 12px; padding-top: 12px; border-top: 1px solid #1c1e24; font-size: 13px; color: #8b909b; font-style: italic; }
  .themes-label { display: block; font-style: normal; font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #6b6f7a; margin-bottom: 6px; }
  .feed h2 { font-weight: 400; font-size: 13px; letter-spacing: .1em; text-transform: uppercase; color: #6b6f7a; margin: 0 0 16px; }
  .entry { border-left: 2px solid #23262e; padding: 0 0 22px 16px; position: relative; }
  .entry .ts { font-size: 12px; color: #6b6f7a; margin-bottom: 6px; }
  .entry .body { font-family: Georgia, "Times New Roman", serif; font-size: 16px; color: #d9dbe1; white-space: pre-wrap; }
  .empty { color: #6b6f7a; font-style: italic; }
  footer { margin-top: 40px; color: #43464f; font-size: 12px; text-align: center; }

  .editor { margin-bottom: 36px; }
  .editor > details > summary { cursor: pointer; color: #9aa0ad; font-size: 13px;
    letter-spacing: .08em; text-transform: uppercase; list-style: none; user-select: none; }
  .editor > details > summary::before { content: "\25B8  "; color: #6b6f7a; }
  .editor > details[open] > summary::before { content: "\25BE  "; }
  .editor .hint { color: #6b6f7a; font-size: 12px; margin: 10px 0; }
  textarea#prompt {
    width: 100%; min-height: 380px; resize: vertical; background: #0a0b0e;
    color: #e2e4ea; border: 1px solid #23262e; border-radius: 10px; padding: 16px 18px;
    font-family: Georgia, "Times New Roman", serif; font-size: 15px; line-height: 1.85;
  }
  textarea#prompt:focus { outline: none; border-color: #3a5a45; }
  .btns { display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }
  .btns button {
    background: #1b2f22; color: #b6e6c6; border: 1px solid #2a4a35; border-radius: 8px;
    padding: 7px 14px; font-size: 13px; cursor: pointer;
  }
  .btns button.ghost { background: #14161b; color: #9aa0ad; border-color: #23262e; }
  .btns button:hover { filter: brightness(1.15); }
  .pstatus { font-size: 12px; color: #6b6f7a; }
  .defbox { margin-top: 12px; }
  .defbox summary { cursor: pointer; color: #6b6f7a; font-size: 12px; }
  .defbox pre { white-space: pre-wrap; color: #8b909b; font-size: 12px;
    background: #0a0b0e; border: 1px solid #1c1e24; border-radius: 8px; padding: 12px; margin-top: 8px; }
  .ts .abs { color: #8b909b; }
  .ts .rel { color: #6b6f7a; }
  .ts .entry-model { color: #6f7d9a; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; }

  .control { border: 1px solid #23262e; border-radius: 12px; padding: 14px 16px; margin-bottom: 36px; background: #101217; }
  .ctl-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 6px 0; }
  .ctl-label { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: #6b6f7a; min-width: 92px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: #cdd0d7; }
  .control input[type=text], .control input:not([type]) { background:#0a0b0e; color:#e2e4ea; border:1px solid #23262e; border-radius:8px; padding:6px 10px; font-size:13px; }
  .control button { background:#1b2f22; color:#b6e6c6; border:1px solid #2a4a35; border-radius:8px; padding:6px 12px; font-size:13px; cursor:pointer; }
  .control button.ghost { background:#14161b; color:#9aa0ad; border-color:#23262e; }
  .control button:hover { filter: brightness(1.15); }
  .control button:disabled { opacity:.5; cursor:default; }
  .chk { font-size:12px; color:#8b909b; display:flex; align-items:center; gap:4px; }
  .expform { border:1px solid #23262e; border-radius:10px; background:#0d0f13;
    padding:14px 16px; margin:10px 0; }
  .ef-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px 14px; margin-bottom:12px; }
  .ef-grid label { display:flex; flex-direction:column; gap:4px; font-size:11px;
    letter-spacing:.08em; text-transform:uppercase; color:#6b6f7a; }
  .ef-grid label.wide { grid-column:1 / -1; }
  .ef-grid input, .ef-grid select { background:#0a0b0e; color:#e2e4ea;
    border:1px solid #23262e; border-radius:8px; padding:7px 10px; font-size:13px;
    font-family:inherit; letter-spacing:normal; text-transform:none; }
  .ef-grid .req { color:#d0a24a; }
  .ef-tools { display:flex; flex-direction:column; gap:8px; margin-bottom:12px; }
  .ef-toolshead { font-size:11px; letter-spacing:.08em; text-transform:uppercase;
    color:#6b6f7a; display:flex; align-items:center; gap:8px; }
  button.tiny { padding:2px 9px; font-size:11px; }
  .ef-prompt { border:1px solid #23262e; border-radius:8px; background:#0a0b0e;
    margin-bottom:12px; }
  .ef-prompt > summary { cursor:pointer; padding:9px 12px; font-size:12px; color:#9aa0ad;
    list-style:none; user-select:none; }
  .ef-prompt > summary::-webkit-details-marker { display:none; }
  .ef-prompt > summary::before { content:"\25B8  "; color:#6b6f7a; }
  .ef-prompt[open] > summary::before { content:"\25BE  "; }
  .ef-prompt textarea { display:block; width:calc(100% - 24px); margin:0 12px 12px;
    min-height:220px; resize:vertical; background:#0a0b0e; color:#e2e4ea;
    border:1px solid #23262e; border-radius:8px; padding:12px;
    font-family:Georgia,"Times New Roman",serif; font-size:14px; line-height:1.8; }
  .toolchecks { display:flex; gap:4px 10px; flex-wrap:wrap; }
  .toolchecks label { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size:11.5px; color:#8b909b; display:flex; align-items:center; gap:4px;
    background:#0a0b0e; border:1px solid #23262e; border-radius:16px; padding:3px 10px 3px 6px;
    cursor:pointer; user-select:none; }
  .toolchecks label:hover { border-color:#3a5a45; }
  .toolchecks label.on { color:#b6e6c6; border-color:#2a4a35; background:#14231b; }
  .toolchecks input { accent-color:#4ad07d; margin:0; }
  .collectlog { margin-top:8px; background:#0a0b0e; border:1px solid #1c1e24; border-radius:8px; padding:10px; font-family: ui-monospace, monospace; font-size:11px; color:#8b909b; white-space:pre-wrap; max-height:160px; overflow:auto; }
  .experiments > details > summary { cursor:pointer; color:#9aa0ad; font-size:13px; letter-spacing:.08em; text-transform:uppercase; }
  .overview { white-space:pre-wrap; font-size:13px; color:#cdd0d7; background:#101217; border:1px solid #1c1e24; border-radius:10px; padding:14px; margin:12px 0; }
  .exp-h { font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:#6b6f7a; }
  table.runs { width:100%; border-collapse:collapse; font-size:12px; }
  table.runs th, table.runs td { text-align:left; padding:6px 8px; border-bottom:1px solid #1c1e24; }
  table.runs tr { cursor:pointer; }
  table.runs tr:hover td { background:#14161b; }
  details.sub, details.exp-item { border:1px solid #1c1e24; border-radius:8px; margin:8px 0; background:#0d0f13; }
  details.sub > summary, details.exp-item > summary { cursor:pointer; list-style:none; user-select:none; padding:9px 12px; font-size:12px; color:#cdd0d7; }
  details.sub > summary::-webkit-details-marker, details.exp-item > summary::-webkit-details-marker { display:none; }
  details.sub > summary::before, details.exp-item > summary::before { content:"\25B8  "; color:#6b6f7a; }
  details.sub[open] > summary::before, details.exp-item[open] > summary::before { content:"\25BE  "; }
  .exp-meta { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:11px; color:#6b6f7a; padding:0 12px 6px; }
  .exp-body { padding:0 12px 10px; }
  .sub-body, .note-body { white-space:pre-wrap; font-size:12px; color:#cdd0d7; padding:0 12px 12px; }
  .raw-ctl { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:0 12px 12px; }
  .raw-ctl button { background:#1b2f22; color:#b6e6c6; border:1px solid #2a4a35; border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
  .raw-ctl button.ghost { background:#14161b; color:#9aa0ad; border-color:#23262e; }
  .raw-ctl button:hover { filter:brightness(1.15); }
  textarea.rawfallback { display:none; width:calc(100% - 24px); margin:0 12px 12px; min-height:160px; background:#0a0b0e; color:#d9dbe1; border:1px solid #23262e; border-radius:8px; padding:10px; font-family: ui-monospace, monospace; font-size:11px; }
  .note-form { display:flex; flex-direction:column; gap:8px; padding:6px 12px 12px; }
  .note-form input { background:#0a0b0e; color:#e2e4ea; border:1px solid #23262e; border-radius:8px; padding:6px 10px; font-size:13px; }
  .note-form textarea { background:#0a0b0e; color:#e2e4ea; border:1px solid #23262e; border-radius:8px; padding:10px; font-size:13px; min-height:120px; font-family: Georgia, serif; }
  .note-form button { align-self:flex-start; background:#1b2f22; color:#b6e6c6; border:1px solid #2a4a35; border-radius:8px; padding:6px 14px; font-size:13px; cursor:pointer; }
  .exp-actions { padding:8px 12px 12px; display:flex; align-items:center; gap:10px; }
  button.danger { background:#2d1618; color:#e6b6b6; border:1px solid #4a2a2a; border-radius:8px; padding:6px 12px; font-size:12px; cursor:pointer; }
  button.danger:hover { filter:brightness(1.2); }

  /* Collapsible panels */
  details.collapse { margin-bottom: 22px; }
  details.collapse > summary,
  details.content-panel > summary {
    cursor:pointer; list-style:none; user-select:none; font-size:12px;
    letter-spacing:.1em; text-transform:uppercase; color:#9aa0ad;
  }
  details.collapse > summary::-webkit-details-marker,
  details.content-panel > summary::-webkit-details-marker { display:none; }
  details.collapse > summary::before,
  details.content-panel > summary::before { content:"\25B8  "; color:#6b6f7a; }
  details.collapse[open] > summary::before,
  details.content-panel[open] > summary::before { content:"\25BE  "; }
  details.collapse > summary { padding:6px 0; border-bottom:1px solid #23262e; margin-bottom:12px; }
  /* inside collapsible now/control, drop the inner card's own frame */
  #nowpanel .now, #controlpanel .control { border:none; background:transparent; padding:0; margin:0; }
  details.content-panel { border:1px solid #23262e; border-radius:12px; background:#101217; padding:2px 16px 4px; margin-bottom:22px; }
  details.content-panel > summary { padding:12px 0; }
  .experiments { margin:0; }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>HANNAH</h1>
      <div class="sub">a mind observing itself inside a machine</div>
    </header>

    <details class="collapse" id="nowpanel" open>
      <summary>Now</summary>
      <div class="now" id="now">
      <div class="line1">
        <span class="dot" id="dot"></span>
        <span class="status" id="status">connecting…</span>
        <span class="clock" id="clock"></span>
      </div>
      <div class="metrics" id="metrics"></div>
      <div class="presence" id="presence"></div>
      <div class="modelrow">
        <label for="model">Model</label>
        <select id="model"></select>
        <span id="mstatus" class="pstatus"></span>
      </div>
      <div class="themes" id="themes" style="display:none">
        <span class="themes-label">Themes so far</span>
        <span id="themestext"></span>
      </div>
      </div>
    </details>

    <details class="collapse" id="controlpanel" open>
      <summary>Controls &amp; experiments</summary>
      <section class="control">
      <div class="ctl-row">
        <span class="ctl-label">Daemon</span>
        <span class="dot" id="ddot"></span><span id="dstate" class="mono">…</span>
        <button id="dstart" class="ghost">Start</button>
        <button id="dstop" class="ghost">Stop</button>
        <button id="drestart" class="ghost">Restart</button>
      </div>
      <div class="ctl-row" id="newexp">
        <span class="ctl-label">New experiment</span>
        <button id="newexpbtn">&#65291; Create experiment&hellip;</button>
        <span class="pstatus">configure the label, prompt, model, and tools in one place</span>
      </div>
      <div id="expform" class="expform" style="display:none">
        <div class="ef-grid">
          <label>Label <span class="req">*</span>
            <input id="ef-label" placeholder="e.g. memory-only-v1"></label>
          <label>Model
            <select id="ef-model"></select></label>
          <label class="wide">Description
            <input id="ef-desc" placeholder="what changes in this experiment (shown on the public lab)"></label>
          <label class="wide">Goal
            <input id="ef-goal" placeholder="what you want to learn (optional)"></label>
          <label class="wide">Hypothesis
            <input id="ef-hyp" placeholder="what you expect to happen (optional)"></label>
        </div>
        <div class="ef-tools">
          <span class="ef-toolshead">Tools offered to Hannah
            <button id="ef-tools-all" class="ghost tiny">all</button>
            <button id="ef-tools-none" class="ghost tiny">none</button>
          </span>
          <span id="ef-tools" class="toolchecks"></span>
          <span class="pstatus" id="ef-tools-hint"></span>
        </div>
        <details class="ef-prompt">
          <summary>System prompt for this experiment (leave as-is or edit)</summary>
          <textarea id="ef-prompt" spellcheck="false" placeholder="loading current prompt…"></textarea>
        </details>
        <div class="ctl-row">
          <label class="chk"><input type="checkbox" id="ef-fresh" checked> fresh start (reset rolling memory)</label>
        </div>
        <div class="ctl-row">
          <button id="ef-start">Start experiment</button>
          <button id="ef-cancel" class="ghost">Cancel</button>
          <span id="ef-status" class="pstatus"></span>
        </div>
      </div>
      <div class="ctl-row" id="toolsrow">
        <span class="ctl-label">Tools</span>
        <span id="toolchecks" class="toolchecks"></span>
        <span id="tstatus" class="pstatus"></span>
      </div>
      <div class="ctl-row" id="activeexp" style="display:none">
        <span class="ctl-label">Experiment</span>
        <span id="expinfo" class="mono"></span>
        <label class="chk"><input type="checkbox" id="expsum" checked> summarize</label>
        <button id="expstop">Stop &amp; collect</button>
      </div>
      <div id="collectlog" class="collectlog" style="display:none"></div>
      </section>
    </details>

    <section class="editor">
      <details class="content-panel" id="promptBox">
        <summary>Edit Hannah's system prompt</summary>
        <p class="hint">This is the voice and identity Hannah writes from. Edits are saved to
          <code>prompts/system_prompt.txt</code> and take effect on her next entry — no restart needed.</p>
        <textarea id="prompt" spellcheck="false" placeholder="loading…"></textarea>
        <div class="btns">
          <button id="save">Save</button>
          <button id="reset" class="ghost">Reset to default</button>
          <span id="pstatus" class="pstatus"></span>
        </div>
        <details class="defbox">
          <summary>View the built-in default prompt</summary>
          <pre id="default"></pre>
        </details>
      </details>
    </section>

    <details class="content-panel" id="journalpanel" open>
      <summary>Journal</summary>
      <div id="feed"><div class="empty">loading…</div></div>
    </details>

    <section class="experiments">
      <details class="content-panel" id="expbox">
        <summary>Experiments &amp; overview</summary>
        <details class="sub" id="overviewbox">
          <summary>Evolving overview</summary>
          <div class="overview" id="overview">No overview yet — collect an experiment to generate one.</div>
        </details>
        <h3 class="exp-h">Experiments</h3>
        <div id="runs"></div>
      </details>
    </section>

    <footer>refreshes every 5s</footer>
  </div>

<script>
function ago(iso) {
  const t = new Date(iso).getTime();
  if (isNaN(t)) return iso;
  let s = Math.floor((Date.now() - t) / 1000);
  if (s < 60) return s + "s ago";
  let m = Math.floor(s / 60);
  if (m < 60) return m + "m ago";
  let h = Math.floor(m / 60);
  if (h < 24) return h + "h " + (m % 60) + "m ago";
  let d = Math.floor(h / 24);
  return d + "d " + (h % 24) + "h ago";
}
function fmtAbs(iso) {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
    hour: "numeric", minute: "2-digit", second: "2-digit"
  });
}
function metric(k, v) {
  return '<div class="metric"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>';
}
async function refreshNow() {
  try {
    const r = await fetch('/api/now'); const n = await r.json();
    const dot = document.getElementById('dot');
    dot.className = 'dot ' + n.status;
    document.getElementById('status').textContent = n.status;
    document.getElementById('clock').textContent = n.now;
    let m = '';
    if (n.uptime) m += metric('uptime', n.uptime);
    if (n.temp_c != null) m += metric('heat', n.temp_c.toFixed(1) + '\u00b0C');
    if (n.power_w != null) m += metric('power', n.power_w.toFixed(2) + ' W');
    if (n.load1 != null) m += metric('cpu load', n.load1.toFixed(2) + (n.cores ? ' / ' + n.cores : ''));
    if (n.cpu_mhz != null) m += metric('clock', n.cpu_mhz + ' MHz');
    if (n.mem_used_mib != null) m += metric('memory', n.mem_used_mib + ' / ' + n.mem_total_mib + ' MiB');
    document.getElementById('metrics').innerHTML = m;
    document.getElementById('presence').textContent = '\u{1F464} ' + n.presence;
    const th = document.getElementById('themes');
    if (n.themes) {
      th.style.display = 'block';
      document.getElementById('themestext').textContent = n.themes;
    } else { th.style.display = 'none'; }
  } catch (e) { document.getElementById('status').textContent = 'offline'; }
}
var feedSig = "";
async function refreshFeed(force) {
  // Don't churn the feed when the panel is collapsed.
  const panel = document.getElementById('journalpanel');
  if (panel && !panel.open) return;
  try {
    const r = await fetch('/api/entries?limit=100'); const items = await r.json();
    const feed = document.getElementById('feed');
    if (!items.length) { feed.innerHTML = '<div class="empty">no entries yet</div>'; return; }
    // Only rebuild when the data actually changed...
    const sig = items.length + ':' + (items[0] ? items[0].time : '');
    if (!force && sig === feedSig) return;
    // ...and don't yank the page while you're scrolled down reading.
    if (!force && feedSig && window.scrollY > 300) return;
    feedSig = sig;
    feed.innerHTML = items.map(function(it) {
      var model = it.model ? ' \u00b7 <span class="entry-model">' + it.model + '</span>' : '';
      return '<div class="entry"><div class="ts"><span class="abs">' + fmtAbs(it.time) +
             '</span> \u00b7 <span class="rel">' + ago(it.time) + '</span>' + model +
             '</div><div class="body"></div></div>';
    }).join('');
    // set text via textContent to avoid HTML injection
    const bodies = feed.querySelectorAll('.body');
    items.forEach(function(it, i) { bodies[i].textContent = it.entry; });
  } catch (e) {}
}
// Refresh the feed when its panel is opened (so it's current when you look).
(function(){ var jp = document.getElementById('journalpanel');
  if (jp) jp.addEventListener('toggle', function(){ if (jp.open) refreshFeed(true); }); })();
// Accordion: opening one big panel closes the others, for one-thing-at-a-time focus.
document.querySelectorAll('details.content-panel').forEach(function(d){
  d.addEventListener('toggle', function(){
    if (d.open) document.querySelectorAll('details.content-panel').forEach(function(o){
      if (o !== d) o.open = false;
    });
  });
});
var DEFAULT_PROMPT = "";
// Put each sentence on its own line: collapse existing line breaks, then break
// after sentence-ending punctuation. Makes the prompt easy to read and edit.
function oneSentencePerLine(text) {
  return (text || "")
    .replace(/\s*\n\s*/g, " ")
    .replace(/([.!?])\s+/g, "$1\n")
    .trim();
}
async function loadPrompt() {
  try {
    const r = await fetch('/api/prompt'); const d = await r.json();
    DEFAULT_PROMPT = d.default || "";
    document.getElementById('prompt').value = oneSentencePerLine(d.content || "");
    document.getElementById('default').textContent = oneSentencePerLine(DEFAULT_PROMPT);
  } catch (e) {}
}
async function savePrompt() {
  const content = document.getElementById('prompt').value;
  const st = document.getElementById('pstatus');
  st.textContent = 'saving…';
  try {
    const r = await fetch('/api/prompt', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: content})
    });
    const d = await r.json();
    st.textContent = d.ok ? '\u2713 saved — applies on her next entry'
                          : ('error: ' + (d.error || 'failed'));
  } catch (e) { st.textContent = 'error saving'; }
  setTimeout(function(){ st.textContent = ''; }, 6000);
}
document.getElementById('save').onclick = savePrompt;
document.getElementById('reset').onclick = function() {
  document.getElementById('prompt').value = oneSentencePerLine(DEFAULT_PROMPT);
};
loadPrompt();

async function loadModels() {
  try {
    const r = await fetch('/api/models'); const d = await r.json();
    const sel = document.getElementById('model');
    sel.innerHTML = (d.models || []).map(function(m) {
      return '<option value="' + m + '"' + (m === d.current ? ' selected' : '') + '>' + m + '</option>';
    }).join('');
  } catch (e) {}
}
// --- tool selection: which read-only tools Hannah is offered ---
// Applies on her next entry; recorded in the run manifest at experiment start.
async function loadTools() {
  try {
    const r = await fetch('/api/tools'); const d = await r.json();
    const box = document.getElementById('toolchecks');
    const enabled = d.enabled || [];
    box.innerHTML = (d.tools || []).map(function(t) {
      const on = enabled.indexOf(t) >= 0;
      return '<label class="' + (on ? 'on' : '') + '" title="' + esc(d.descriptions[t] || '') + '">'
           + '<input type="checkbox" value="' + t + '"' + (on ? ' checked' : '') + '>' + t + '</label>';
    }).join('');
    box.querySelectorAll('input').forEach(function(cb) { cb.onchange = saveTools; });
  } catch (e) {}
}
async function saveTools() {
  const box = document.getElementById('toolchecks');
  const names = Array.from(box.querySelectorAll('input:checked')).map(function(cb){ return cb.value; });
  const st = document.getElementById('tstatus');
  st.textContent = 'saving…';
  try {
    const d = await postJSON('/api/tools', {tools: names});
    st.textContent = d.ok ? ('\u2713 ' + (names.length ? names.length + ' tool(s) offered' : 'no tools — pure reflection')
                             + ' — applies on her next entry')
                          : ('error: ' + (d.error || 'failed'));
    box.querySelectorAll('label').forEach(function(l) {
      l.className = l.querySelector('input').checked ? 'on' : '';
    });
  } catch (e) { st.textContent = 'error saving'; }
  setTimeout(function(){ st.textContent = ''; }, 6000);
}
loadTools();

document.getElementById('model').onchange = async function() {
  const name = this.value;
  const st = document.getElementById('mstatus');
  st.textContent = 'switching… reloading model (~10-30s)';
  try {
    const r = await fetch('/api/model', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: name})
    });
    const d = await r.json();
    st.textContent = d.ok ? ('\u2713 now running ' + name) : ('error: ' + (d.error || 'failed'));
  } catch (e) { st.textContent = 'error switching'; }
  setTimeout(function(){ st.textContent = ''; }, 15000);
};
loadModels();

async function postJSON(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body||{})});
  return r.json();
}
document.getElementById('dstart').onclick = function(){ postJSON('/api/daemon',{action:'start'}).then(refreshExperiment); };
document.getElementById('dstop').onclick = function(){ postJSON('/api/daemon',{action:'stop'}).then(refreshExperiment); };
document.getElementById('drestart').onclick = function(){ postJSON('/api/daemon',{action:'restart'}).then(refreshExperiment); };
// --- create-experiment form: label + meta + model + tools + prompt in one place ---
function efToolLabels() { return document.querySelectorAll('#ef-tools label'); }
function efSetAll(on) {
  efToolLabels().forEach(function(l){
    l.querySelector('input').checked = on;
    l.className = on ? 'on' : '';
  });
}
async function openExpForm() {
  const form = document.getElementById('expform');
  form.style.display = 'block';
  // Prefill from the live state: models, current tool selection, current prompt.
  try {
    const r = await fetch('/api/models'); const d = await r.json();
    document.getElementById('ef-model').innerHTML = (d.models || []).map(function(m){
      return '<option value="' + m + '"' + (m === d.current ? ' selected' : '') + '>' + m + '</option>';
    }).join('');
  } catch (e) {}
  try {
    const r = await fetch('/api/tools'); const d = await r.json();
    const enabled = d.enabled || [];
    document.getElementById('ef-tools').innerHTML = (d.tools || []).map(function(t){
      const on = enabled.indexOf(t) >= 0;
      return '<label class="' + (on ? 'on' : '') + '" title="' + esc(d.descriptions[t] || '') + '">'
           + '<input type="checkbox" value="' + t + '"' + (on ? ' checked' : '') + '>' + t + '</label>';
    }).join('');
    efToolLabels().forEach(function(l){
      l.querySelector('input').onchange = function(){
        l.className = this.checked ? 'on' : '';
        document.getElementById('ef-tools-hint').textContent =
          document.querySelectorAll('#ef-tools input:checked').length
            ? '' : 'no tools — pure reflection';
      };
    });
  } catch (e) {}
  try {
    const r = await fetch('/api/prompt'); const d = await r.json();
    document.getElementById('ef-prompt').value = oneSentencePerLine(d.content || "");
  } catch (e) {}
  document.getElementById('ef-label').focus();
}
document.getElementById('newexpbtn').onclick = openExpForm;
document.getElementById('ef-cancel').onclick = function(){
  document.getElementById('expform').style.display = 'none';
};
document.getElementById('ef-tools-all').onclick = function(){ efSetAll(true); };
document.getElementById('ef-tools-none').onclick = function(){ efSetAll(false); };
document.getElementById('ef-start').onclick = async function(){
  const label = document.getElementById('ef-label').value.trim();
  const st = document.getElementById('ef-status');
  if (!label) { st.textContent = 'a label is required'; return; }
  const tools = Array.from(document.querySelectorAll('#ef-tools input:checked'))
                     .map(function(cb){ return cb.value; });
  const payload = {
    label: label,
    description: document.getElementById('ef-desc').value.trim(),
    goal: document.getElementById('ef-goal').value.trim(),
    hypothesis: document.getElementById('ef-hyp').value.trim(),
    model: document.getElementById('ef-model').value,
    tools: tools,
    prompt: document.getElementById('ef-prompt').value,
    fresh: document.getElementById('ef-fresh').checked
  };
  this.disabled = true;
  st.textContent = 'starting… (switching models can take ~30s)';
  try {
    const d = await postJSON('/api/experiment/start', payload);
    if (d.ok) {
      st.textContent = '\u2713 experiment started';
      document.getElementById('expform').style.display = 'none';
      document.getElementById('ef-label').value = '';
      loadTools(); loadPrompt(); loadModels();
    } else { st.textContent = 'error: ' + (d.error || 'failed'); }
  } catch (e) { st.textContent = 'error starting experiment'; }
  this.disabled = false;
  refreshExperiment();
  setTimeout(function(){ st.textContent = ''; }, 8000);
};
document.getElementById('expstop').onclick = async function(){
  const summarize = document.getElementById('expsum').checked;
  this.disabled = true;
  const d = await postJSON('/api/experiment/stop', {summarize:summarize});
  if (!d.ok) { alert('Could not stop: ' + (d.error||'failed')); this.disabled = false; }
  refreshExperiment();
};
async function refreshExperiment() {
  try {
    const r = await fetch('/api/experiment'); const d = await r.json();
    const dot = document.getElementById('ddot');
    dot.className = 'dot ' + (d.daemon_active ? 'awake' : 'offline');
    document.getElementById('dstate').textContent = d.daemon_active ? 'running' : 'stopped';
    const col = d.collecting || {};
    const clog = document.getElementById('collectlog');
    if (col.running || (col.done && col.log && col.log.length)) {
      clog.style.display = 'block';
      clog.textContent = (col.log||[]).join('\n') + (col.error ? ('\nERROR: '+col.error) : '');
    } else { clog.style.display = 'none'; }
    const active = d.active;
    const newexp = document.getElementById('newexp');
    const activeexp = document.getElementById('activeexp');
    if (active) {
      newexp.style.display = 'none'; activeexp.style.display = 'flex';
      document.getElementById('expform').style.display = 'none';
      var toolsDesc = !active.tools_enabled ? 'off'
        : ((active.tools_available && active.tools_available.length)
            ? active.tools_available.join(', ') : 'none');
      document.getElementById('expinfo').textContent =
        active.label + '  ·  ' + active.elapsed + '  ·  ' + active.entries_so_far + ' entries'
        + '  ·  ' + active.model + '  ·  tools: ' + toolsDesc
        + (active.prompt_changed ? '  ·  (prompt changed since start)' : '')
        + (active.tools_changed ? '  ·  (tools changed since start)' : '');
      document.getElementById('expstop').disabled = !!col.running;
    } else {
      newexp.style.display = 'flex'; activeexp.style.display = 'none';
    }
    // Reload the runs list ONLY on the edge when a collect finishes - never every
    // tick (that was rebuilding the list and closing whatever you had expanded).
    if (lastCollecting && !col.running) loadRuns();
    lastCollecting = !!col.running;
  } catch (e) {}
}
var lastCollecting = false;
async function loadOverview() {
  try {
    const r = await fetch('/api/overview'); const d = await r.json();
    if (d.markdown) document.getElementById('overview').textContent = d.markdown;
  } catch (e) {}
}
function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
async function loadRuns() {
  try {
    const r = await fetch('/api/runs'); const runs = await r.json();
    const el = document.getElementById('runs');
    loadOverview();
    if (!runs.length) { el.innerHTML = '<div class="empty">no experiments collected yet</div>'; return; }
    el.innerHTML = runs.map(function(m){
      var meta = (m.started_at||'') + ' \u00b7 ' + (m.duration||'') + ' \u00b7 ' + (m.model||'')
               + ' \u00b7 tools ' + (m.tools_enabled?'on':'off') + ' \u00b7 ' + (m.entries||0) + ' entries';
      return '<details class="exp-item" data-folder="' + esc(m.folder) + '" data-label="' + esc(m.label) + '">'
           + '<summary>' + esc(m.label) + '</summary>'
           + '<div class="exp-meta">' + esc(meta) + '</div>'
           + '<div class="exp-body">loading…</div></details>';
    }).join('');
    el.querySelectorAll('details.exp-item').forEach(function(d){
      d.addEventListener('toggle', function(){
        if (d.open && !d.dataset.loaded) { d.dataset.loaded = '1'; showRun(d); }
      });
    });
  } catch (e) {}
}
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
  return new Promise(function(res, rej){
    try {
      var ta = document.createElement('textarea'); ta.value = text;
      ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta);
      ta.focus(); ta.select(); var ok = document.execCommand('copy'); document.body.removeChild(ta);
      ok ? res() : rej();
    } catch (e) { rej(e); }
  });
}
function renderNotes(body, notes) {
  const list = body.querySelector('.notes-list');
  list.innerHTML = '';
  if (!notes || !notes.length) { list.innerHTML = '<div class="empty">no custom summaries yet</div>'; return; }
  notes.forEach(function(n){
    const det = document.createElement('details'); det.className = 'sub note';
    const sm = document.createElement('summary'); sm.textContent = n.label + '  ·  ' + (n.added_at || '');
    const bd = document.createElement('div'); bd.className = 'note-body'; bd.textContent = n.text;
    det.appendChild(sm); det.appendChild(bd); list.appendChild(det);
  });
}
async function copyRaw(folder, kind, body) {
  const st = body.querySelector('.cpStatus'); st.textContent = 'fetching…';
  try {
    const r = await fetch('/api/run/raw?folder=' + encodeURIComponent(folder) + '&kind=' + kind);
    const d = await r.json(); const text = d.text || '';
    try {
      await copyText(text);
      st.textContent = '\u2713 copied ' + text.length + ' chars';
    } catch (e) {
      // Clipboard blocked (e.g. plain-http LAN): show a textarea to copy manually.
      st.textContent = 'select & copy below:';
      let ta = body.querySelector('.rawfallback');
      if (!ta) { ta = document.createElement('textarea'); ta.className = 'rawfallback'; body.querySelector('.raw-ctl').after(ta); }
      ta.value = text; ta.style.display = 'block'; ta.focus(); ta.select();
    }
  } catch (e) { st.textContent = 'error fetching log'; }
  setTimeout(function(){ st.textContent = ''; }, 8000);
}
async function saveNote(folder, body) {
  const label = body.querySelector('.noteLabel').value.trim() || 'note';
  const text = body.querySelector('.noteText').value.trim();
  const st = body.querySelector('.noteStatus');
  if (!text) { st.textContent = 'enter some text'; return; }
  st.textContent = 'saving…';
  try {
    const r = await fetch('/api/run/note', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder, label: label, text: text})});
    const d = await r.json();
    if (d.ok) {
      st.textContent = '\u2713 saved';
      body.querySelector('.noteText').value = ''; body.querySelector('.noteLabel').value = '';
      renderNotes(body, d.notes || []);
    } else { st.textContent = 'error: ' + (d.error || 'failed'); }
  } catch (e) { st.textContent = 'error saving'; }
  setTimeout(function(){ st.textContent = ''; }, 6000);
}
async function showRun(d) {
  const folder = d.getAttribute('data-folder');
  const body = d.querySelector('.exp-body');
  body.innerHTML =
      '<details class="sub" open><summary>AI summary</summary><div class="sub-body ai"></div></details>'
    + '<details class="sub"><summary>Raw log (copy for ChatGPT)</summary>'
    +   '<div class="raw-ctl"><button class="cpEntries">Copy entries</button>'
    +   '<button class="cpJournal ghost">Copy full journal</button>'
    +   '<span class="cpStatus mono"></span></div></details>'
    + '<details class="sub"><summary>Custom summaries</summary>'
    +   '<div class="notes-list"></div>'
    +   '<div class="note-form"><input class="noteLabel" placeholder="label, e.g. ChatGPT">'
    +   '<textarea class="noteText" placeholder="paste a custom summary…"></textarea>'
    +   '<button class="noteSave">Add summary</button><span class="noteStatus mono"></span></div>'
    + '</details>'
    + '<div class="exp-actions"><button class="delRun danger">Delete run</button>'
    +   '<button class="delExp danger">Delete experiment (all runs)</button>'
    +   '<span class="delStatus mono"></span></div>';
  try {
    const r = await fetch('/api/run?folder=' + encodeURIComponent(folder));
    const data = await r.json();
    body.querySelector('.ai').textContent = data.summary || '(no AI summary was generated)';
    renderNotes(body, data.notes || []);
  } catch (e) { body.querySelector('.ai').textContent = '(error loading experiment)'; }
  body.querySelector('.cpEntries').onclick = function(){ copyRaw(folder, 'entries', body); };
  body.querySelector('.cpJournal').onclick = function(){ copyRaw(folder, 'journal', body); };
  body.querySelector('.noteSave').onclick = function(){ saveNote(folder, body); };
  body.querySelector('.delRun').onclick = function(){ deleteRun(folder, d, body); };
  body.querySelector('.delExp').onclick = function(){
    deleteExperiment(d.getAttribute('data-label') || folder, d, body);
  };
}
async function deleteRun(folder, det, body) {
  if (!confirm('Delete run "' + folder + '"? This permanently removes its folder.')) return;
  const st = body.querySelector('.delStatus'); st.textContent = 'deleting…';
  try {
    const r = await fetch('/api/run/delete', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({folder: folder})});
    const d = await r.json();
    if (d.ok) { det.remove(); loadRuns(); }
    else { st.textContent = 'error: ' + (d.error || 'failed'); }
  } catch (e) { st.textContent = 'error deleting'; }
}
async function deleteExperiment(name, det, body) {
  const typed = prompt('Delete experiment "' + name + '" — ALL of its runs and its '
    + 'public-lab entry will be permanently removed.\n\nType the experiment name to confirm:');
  if (typed === null) return;
  const st = body.querySelector('.delStatus');
  if (typed.trim() !== name) { st.textContent = 'name did not match — not deleted'; return; }
  st.textContent = 'deleting experiment…';
  try {
    const d = await postJSON('/api/experiment/delete', {name: name});
    if (d.ok) {
      st.textContent = '\u2713 deleted ' + (d.deleted_runs || []).length + ' run(s)';
      loadRuns();
    } else { st.textContent = 'error: ' + (d.error || 'failed'); }
  } catch (e) { st.textContent = 'error deleting experiment'; }
}
document.getElementById('expbox').addEventListener('toggle', function(){ if (this.open) loadRuns(); });

function tick() { refreshNow(); refreshFeed(); refreshExperiment(); }
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    cfg = None  # set on the server before serving

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802 (http.server API)
        route = urlparse(self.path)
        if route.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route.path == "/api/now":
            self._json(_get_now(self.cfg))
        elif route.path == "/api/entries":
            qs = parse_qs(route.query)
            try:
                limit = int(qs.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            limit = max(1, min(limit, self.cfg["web"]["max_entries"]))
            entries = hannah.load_recent_entries(limit)
            entries.reverse()  # newest first for the feed
            self._json(entries)
        elif route.path == "/api/prompt":
            self._json({
                "content": hannah.load_system_prompt(),  # what's actually in effect
                "default": hannah.DEFAULT_SYSTEM_PROMPT,
            })
        elif route.path == "/api/models":
            self._json({
                "models": list(hannah.list_models(self.cfg).keys()),
                "current": hannah.selected_model_name(self.cfg),
            })
        elif route.path == "/api/tools":
            self._json({
                "tools": list(hannah.TOOLS),
                "enabled": hannah.enabled_tool_names(self.cfg),
                "descriptions": {name: spec["description"]
                                 for name, spec in hannah.TOOLS.items()},
            })
        elif route.path == "/api/experiment":
            self._json({
                "active": hr.active_run(self.cfg),
                "daemon_active": hr.daemon_active(),
                "collecting": {
                    "running": _collect["running"],
                    "done": _collect["done"],
                    "error": _collect["error"],
                    "result": _collect["result"],
                    "log": _collect["log"][-12:],
                },
            })
        elif route.path == "/api/runs":
            self._json(hr.list_runs())
        elif route.path == "/api/run":
            folder = parse_qs(route.query).get("folder", [""])[0]
            self._json(hr.run_detail(folder) if folder else
                       {"manifest": {}, "summary": "", "notes": []})
        elif route.path == "/api/run/raw":
            qs = parse_qs(route.query)
            folder = qs.get("folder", [""])[0]
            kind = qs.get("kind", ["journal"])[0]
            self._json({"text": hr.run_raw(folder, kind) if folder else ""})
        elif route.path == "/api/overview":
            self._json({"markdown": hr.read_overview()})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _read_json_body(self, max_len: int = 20000):
        """Read and parse a JSON request body, or return None on any problem."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > max_len:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return None

    def do_POST(self):  # noqa: N802 (http.server API)
        route = urlparse(self.path)
        if route.path == "/api/prompt":
            self._save_prompt()
        elif route.path == "/api/model":
            self._switch_model()
        elif route.path == "/api/tools":
            self._set_tools()
        elif route.path == "/api/daemon":
            self._daemon_control()
        elif route.path == "/api/experiment/start":
            self._experiment_start()
        elif route.path == "/api/experiment/stop":
            self._experiment_stop()
        elif route.path == "/api/run/note":
            self._add_note()
        elif route.path == "/api/run/delete":
            folder = (self._read_json_body(max_len=500) or {}).get("folder", "").strip()
            if not folder or not hr.delete_run(folder):
                self._json({"ok": False, "error": "could not delete run"})
            else:
                self._json({"ok": True})
        elif route.path == "/api/experiment/delete":
            name = (self._read_json_body(max_len=500) or {}).get("name", "").strip()
            try:
                result = hr.delete_experiment(name)
            except RuntimeError as exc:
                self._json({"ok": False, "error": str(exc)})
                return
            self._json({"ok": True, **result})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _add_note(self) -> None:
        data = self._read_json_body(max_len=100000) or {}
        folder = (data.get("folder") or "").strip()
        text = (data.get("text") or "").strip()
        label = (data.get("label") or "note").strip()
        if not folder or not text:
            self._json({"ok": False, "error": "folder and text are required"})
            return
        try:
            notes = hr.add_note(folder, label, text)
        except (RuntimeError, OSError) as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True, "notes": notes})

    def _daemon_control(self) -> None:
        action = (self._read_json_body(max_len=200) or {}).get("action")
        if action not in ("start", "stop", "restart"):
            self._json({"ok": False, "error": "action must be start/stop/restart"})
            return
        hr.daemon_action(action)
        self._json({"ok": True, "active": hr.daemon_active()})

    def _experiment_start(self) -> None:
        """Create + start an experiment: label, public-lab metadata, model,
        tool selection, and system prompt applied in one step."""
        data = self._read_json_body(max_len=50000) or {}
        label = (data.get("label") or "").strip()
        if not label:
            self._json({"ok": False, "error": "a label is required"})
            return
        tools = data.get("tools")
        if tools is not None and not isinstance(tools, list):
            tools = None
        model = (data.get("model") or "").strip() or None
        prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else None
        meta = {k: (data.get(k) or "").strip()
                for k in ("description", "goal", "hypothesis")}
        meta = {k: v for k, v in meta.items() if v}
        if meta:
            meta["status"] = "active"
        note = (data.get("description") or data.get("note") or "").strip()
        try:
            run = hr.start_run(label, note, bool(data.get("fresh")), self.cfg,
                               tools=tools, model=model, system_prompt=prompt,
                               meta=meta or None)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True, "run": {"label": run["label"]}})

    def _experiment_stop(self) -> None:
        if _collect["running"]:
            self._json({"ok": False, "error": "a collection is already in progress"})
            return
        if not hr.CURRENT.exists():
            self._json({"ok": False, "error": "no active experiment to stop"})
            return
        data = self._read_json_body(max_len=500) or {}
        summarize = data.get("summarize", True)
        local = bool(data.get("local"))
        # Collect can take a while (summary generation) - run it in the background.
        threading.Thread(
            target=_collect_worker, args=(self.cfg, summarize, local), daemon=True
        ).start()
        self._json({"ok": True, "started": True})

    def _save_prompt(self) -> None:
        data = self._read_json_body()
        content = (data or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            self._json({"ok": False, "error": "prompt is empty"})
            return
        try:
            hannah.SYSTEM_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
            hannah.SYSTEM_PROMPT_FILE.write_text(content, encoding="utf-8")
        except OSError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        hannah.ensure_prompt_archived()  # snapshot this prompt version for provenance
        self._json({"ok": True})

    def _set_tools(self) -> None:
        """Select which tools Hannah is offered. Takes effect on her next
        entry (the daemon re-reads the selection every cycle)."""
        data = self._read_json_body(max_len=2000)
        names = (data or {}).get("tools")
        if not isinstance(names, list) or not hannah.set_enabled_tools(names, self.cfg):
            self._json({"ok": False, "error": "tools must be a list of known tool names"})
            return
        self._json({"ok": True, "enabled": hannah.enabled_tool_names(self.cfg)})

    def _switch_model(self) -> None:
        data = self._read_json_body(max_len=500)
        name = (data or {}).get("model")
        if not isinstance(name, str) or not hannah.set_selected_model(name, self.cfg):
            self._json({"ok": False, "error": "unknown model"})
            return
        # Restart the llama-server unit so it reloads with the newly selected model.
        try:
            subprocess.run(
                ["systemctl", "--user", "restart", "hannah-llama.service"],
                check=True, timeout=30, capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            self._json({"ok": False, "error": f"selected, but restart failed: {exc}"})
            return
        self._json({"ok": True, "model": name})

    def log_message(self, *args):  # keep the console quiet
        pass


def main() -> None:
    cfg = hannah.load_config()
    Handler.cfg = cfg
    host, port = cfg["web"]["host"], cfg["web"]["port"]
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Hannah web viewer on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
