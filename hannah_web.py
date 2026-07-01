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
      <div class="modelrow">
        <label for="model">Model</label>
        <select id="model"></select>
        <span id="mstatus" class="pstatus"></span>
      </div>
      <div class="themes" id="themes" style="display:none">
        <span class="themes-label">Themes so far</span>
        <span id="themestext"></span>
      </div>
    </section>

    <section class="editor">
      <details id="promptBox">
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
async function refreshFeed() {
  try {
    const r = await fetch('/api/entries?limit=100'); const items = await r.json();
    const feed = document.getElementById('feed');
    if (!items.length) { feed.innerHTML = '<div class="empty">no entries yet</div>'; return; }
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
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

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
        self._json({"ok": True})

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
