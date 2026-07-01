#!/usr/bin/env python3
"""A tiny, dependency-free web window into Hannah.

Serves a single calm page that shows a live "now" panel (time, uptime, heat,
power, presence, and whether Hannah is awake) plus her journal feed. Everything
is read-only and built on the Python standard library.

Run directly (uses config.json for host/port):
    python3 hannah_web.py
"""

import json
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import hannah

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
  .themes { margin-top: 12px; padding-top: 12px; border-top: 1px solid #1c1e24; font-size: 13px; color: #8b909b; font-style: italic; }
  .feed h2 { font-weight: 400; font-size: 13px; letter-spacing: .1em; text-transform: uppercase; color: #6b6f7a; margin: 0 0 16px; }
  .entry { border-left: 2px solid #23262e; padding: 0 0 22px 16px; position: relative; }
  .entry .ts { font-size: 12px; color: #6b6f7a; margin-bottom: 6px; }
  .entry .body { font-family: Georgia, "Times New Roman", serif; font-size: 16px; color: #d9dbe1; white-space: pre-wrap; }
  .empty { color: #6b6f7a; font-style: italic; }
  footer { margin-top: 40px; color: #43464f; font-size: 12px; text-align: center; }
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>HANNAH</h1>
      <div class="sub">a mind observing itself inside a machine</div>
    </header>

    <section class="now" id="now">
      <div class="line1">
        <span class="dot" id="dot"></span>
        <span class="status" id="status">connecting…</span>
        <span class="clock" id="clock"></span>
      </div>
      <div class="metrics" id="metrics"></div>
      <div class="presence" id="presence"></div>
      <div class="themes" id="themes" style="display:none"></div>
    </section>

    <section class="feed">
      <h2>Journal</h2>
      <div id="feed"><div class="empty">loading…</div></div>
    </section>

    <footer>read-only · refreshes every 5s</footer>
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
    if (n.themes) { th.style.display = 'block'; th.textContent = n.themes; }
    else { th.style.display = 'none'; }
  } catch (e) { document.getElementById('status').textContent = 'offline'; }
}
async function refreshFeed() {
  try {
    const r = await fetch('/api/entries?limit=100'); const items = await r.json();
    const feed = document.getElementById('feed');
    if (!items.length) { feed.innerHTML = '<div class="empty">no entries yet</div>'; return; }
    feed.innerHTML = items.map(function(it) {
      return '<div class="entry"><div class="ts">' + ago(it.time) +
             '</div><div class="body"></div></div>';
    }).join('');
    // set text via textContent to avoid HTML injection
    const bodies = feed.querySelectorAll('.body');
    items.forEach(function(it, i) { bodies[i].textContent = it.entry; });
  } catch (e) {}
}
function tick() { refreshNow(); refreshFeed(); }
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
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

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
