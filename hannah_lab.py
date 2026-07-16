#!/usr/bin/env python3
"""Hannah Lab CLI - build and publish the public, read-only lab site.

The lab layer wraps the existing Hannah runtime without touching it. Flow:

    1. Hannah runs locally (hannah.py --daemon), runs are collected with
       hannah_run.py into research/runs/<label>/.
    2. `build` sanitizes every collected run, derives the lab state
       (memories / beliefs / questions / timeline), writes per-run public
       artifacts into research/runs/<label>/public/, and renders the static
       site into public_lab/site/.
    3. `check` runs the fail-closed sanitizer gate over the rendered site.
    4. `preview` serves the site locally as a live control surface: from the
       Experiments page you can create, delete, and collect experiments (the
       controls talk to a small local /api/lab/* API that only exists while
       preview is running).
    5. `publish` re-builds, re-checks, then pushes outbound to S3
       (aws s3 sync). Nothing ever reaches inbound into this machine.

Commands:
    python3 hannah_lab.py build
    python3 hannah_lab.py check
    python3 hannah_lab.py preview [--port 8080]
    python3 hannah_lab.py publish [--dry-run]

Publish configuration comes from env vars (no credentials in the repo):
    HANNAH_PUBLISH_TARGET=s3         # only s3 for now
    HANNAH_S3_BUCKET=hannah-lab-site
    HANNAH_AWS_REGION=us-east-1      # optional
Plus standard AWS credentials (env/profile/instance role) for the aws CLI.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import hannah
import hannah_run as hr
from lab import artifacts, rundata, site, state as labstate
from lab.sanitizer import make_sanitizer

RUNS_DIR = hannah.BASE_DIR / "research" / "runs"
PUBLIC_LAB = hannah.BASE_DIR / "public_lab"
SITE_DIR = PUBLIC_LAB / "site"
REGISTRY_FILE = PUBLIC_LAB / "experiments.json"


def _load_registry() -> dict:
    """Optional experiment metadata (description/goal/hypothesis), hand-edited."""
    try:
        return json.loads(REGISTRY_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _lab_cfg(cfg: dict) -> dict:
    return cfg.get("lab", {})


# --- build ---------------------------------------------------------------------

def build(cfg=None, log=print) -> bool:
    """Sanitize runs, derive state, write artifacts, render the site.

    Returns True when the sanitizer gate passed and the site is publishable.
    """
    cfg = cfg or hannah.load_config()
    sanitizer = make_sanitizer(cfg)
    github = _lab_cfg(cfg).get("github_url", site.DEFAULT_GITHUB)

    log(f"Loading runs from {RUNS_DIR} …")
    runs = rundata.discover_runs(RUNS_DIR, sanitizer)
    if not runs:
        log("No collected runs found; the site will be an empty shell. "
            "Collect a run first: python3 hannah_run.py collect")
    groups = rundata.group_by_experiment(runs)
    log(f"  {len(runs)} run(s) across {len(groups)} experiment(s), "
        f"{sum(len(r.entries) for r in runs)} entries total.")

    # Derived state is scoped per experiment (the organizing unit of the lab);
    # a lab-wide fold across all runs powers the home page's global picture.
    log("Deriving lab state per experiment …")
    states = {}
    for g in groups:
        st = labstate.build_state(g["runs"])
        states[g["name"]] = st
        log(f"  {g['name']}: {len(st.memories)} memories, "
            f"{len(st.beliefs)} beliefs, {len(st.questions)} questions, "
            f"{len(st.contradictions)} contradictions.")
    global_state = labstate.build_state(runs)

    log("Writing per-run public artifacts …")
    manifests = {}
    for g in groups:
        st = states[g["name"]]
        for run in g["runs"]:
            diff = st.changes_by_run.get(run.run_id, {})
            out = RUNS_DIR / run.run_id / "public"
            manifests[run.run_id] = artifacts.write_run_artifacts(
                run, diff, st, out)
            withheld = manifests[run.run_id].get("withheld_entries", 0)
            flag = f" ({withheld} entries withheld)" if withheld else ""
            log(f"  {run.run_id}: score {run.score.get('score')}, "
                f"{run.failure_count} failures{flag}")

    log(f"Rendering static site into {SITE_DIR} …")
    site.build_site(groups, states, global_state, manifests, SITE_DIR,
                    RUNS_DIR, registry=_load_registry(), github=github)

    return check(cfg, log=log)


# --- check (fail-closed gate) -----------------------------------------------------

def check(cfg=None, log=print) -> bool:
    """Scan the rendered site for secrets/identifiers. True = safe to publish."""
    cfg = cfg or hannah.load_config()
    sanitizer = make_sanitizer(cfg)
    if not SITE_DIR.exists():
        log("Site has not been built yet (public_lab/site missing).")
        return False
    log("Sanitizer gate: scanning the rendered site …")
    problems = sanitizer.check_file_tree(SITE_DIR)
    if problems:
        log("BLOCKED — sensitive content found; the site must not be published:")
        for rel, reasons in problems:
            log(f"  {rel}: {', '.join(reasons)}")
        return False
    log("  clean: no secrets or local identifiers detected.")
    return True


# --- preview (control-capable local server) ----------------------------------------
# When you preview the lab on your own machine it is a live control surface, not
# just a static file server: the experiments page can create, delete, and
# collect experiments through the small /api/lab/* API below. Those endpoints
# only exist while `preview` is running - the rendered HTML detects them at
# runtime, so if you ever serve public_lab/site/ from a plain static host the
# controls simply never appear and the site stays read-only.

# Background collect (summary generation is slow), polled by the UI.
_collect = {"running": False, "log": [], "done": False, "error": None, "result": None}


def _collect_worker(cfg):
    _collect.update(running=True, done=False, error=None, result=None, log=[])

    def log(msg):
        _collect["log"].append(str(msg))

    try:
        _collect["result"] = hr.collect_run(summarize=True, cfg=cfg, log=log)
    except Exception as exc:  # surface to the UI rather than crashing the server
        _collect["error"] = str(exc)
        log(f"ERROR: {exc}")
    finally:
        _collect.update(running=False, done=True)


class LabControlHandler(SimpleHTTPRequestHandler):
    """Serves public_lab/site/ and a small experiment-control API."""

    cfg = None  # set on the class before serving

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, max_len=100000):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if n <= 0 or n > max_len:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return None

    # -- routing --
    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/api/lab/options":
            return self._options()
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/lab/experiment/create":
            return self._create()
        if path == "/api/lab/experiment/delete":
            return self._delete()
        if path == "/api/lab/experiment/collect":
            return self._collect_start()
        self._json({"ok": False, "error": "not found"}, 404)

    # -- endpoints --
    def _options(self):
        cfg = self.cfg
        self._json({
            "control": True,
            "models": list(hannah.list_models(cfg).keys()),
            "current_model": hannah.selected_model_name(cfg),
            "tools": list(hannah.TOOLS),
            "tool_descriptions": {n: s["description"]
                                  for n, s in hannah.TOOLS.items()},
            "enabled_tools": hannah.enabled_tool_names(cfg),
            "current_prompt": hannah.load_system_prompt(),
            "daemon_active": hr.daemon_active(),
            "active": hr.active_run(cfg),
            "collecting": {"running": _collect["running"],
                           "done": _collect["done"],
                           "error": _collect["error"],
                           "log": _collect["log"][-12:]},
        })

    def _create(self):
        data = self._read_json() or {}
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
        meta["status"] = "active"
        try:
            hr.start_run(label, meta.get("description", ""),
                         bool(data.get("fresh")), self.cfg, tools=tools,
                         model=model, system_prompt=prompt, meta=meta,
                         log=lambda *a, **k: None)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        # Rebuild so the new (registry-only, run-less) experiment shows at once.
        try:
            build(self.cfg, log=lambda *a, **k: None)
        except Exception:
            pass
        self._json({"ok": True})

    def _delete(self):
        name = (self._read_json(max_len=1000) or {}).get("name", "").strip()
        try:
            result = hr.delete_experiment(name, log=lambda *a, **k: None)
        except RuntimeError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True, **result})

    def _collect_start(self):
        if _collect["running"]:
            self._json({"ok": False, "error": "a collection is already running"})
            return
        if not hr.CURRENT.exists():
            self._json({"ok": False, "error": "no active experiment to collect"})
            return
        threading.Thread(target=_collect_worker, args=(self.cfg,),
                         daemon=True).start()
        self._json({"ok": True, "started": True})

    def log_message(self, *args):  # keep the console quiet
        pass


def preview(port: int, host: str = "0.0.0.0") -> None:
    """Serve the built lab locally as a live control surface.

    Binds all interfaces by default (reachable on your LAN); pass
    --host 127.0.0.1 for local-only. The create/delete/collect controls act on
    your local runtime, so keep this on a trusted network.
    """
    if not SITE_DIR.exists():
        raise SystemExit("Build the site first: python3 hannah_lab.py build")
    LabControlHandler.cfg = hannah.load_config()
    handler = partial(LabControlHandler, directory=str(SITE_DIR))
    server = ThreadingHTTPServer((host, port), handler)
    shown = _lan_ip() if host == "0.0.0.0" else host
    print(f"Serving {SITE_DIR} at http://{shown}:{port}/ (Ctrl-C to stop)")
    print("Live control enabled: create, delete, and collect experiments from "
          "the Experiments page.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping preview.")
    finally:
        server.server_close()


def _lan_ip() -> str:
    """Best-effort LAN IP for the preview URL (no traffic is actually sent)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


# --- publish -----------------------------------------------------------------------

def publish(cfg=None, dry_run: bool = False, log=print) -> None:
    """Outbound-only push of the static site. Rebuilds and re-checks first."""
    cfg = cfg or hannah.load_config()
    target = os.environ.get("HANNAH_PUBLISH_TARGET",
                            _lab_cfg(cfg).get("publish_target", "s3"))
    if target != "s3":
        raise SystemExit(f"Unknown publish target: {target!r} (only 's3' is "
                         "supported for now)")
    bucket = os.environ.get("HANNAH_S3_BUCKET",
                            _lab_cfg(cfg).get("s3_bucket", ""))
    if not bucket:
        raise SystemExit("Set HANNAH_S3_BUCKET (or lab.s3_bucket in config.json).")

    if not build(cfg, log=log):
        raise SystemExit("Publish aborted: the sanitizer gate failed. "
                         "Nothing was uploaded.")

    cmd = ["aws", "s3", "sync", str(SITE_DIR), f"s3://{bucket}", "--delete"]
    region = os.environ.get("HANNAH_AWS_REGION",
                            _lab_cfg(cfg).get("aws_region", ""))
    if region:
        cmd += ["--region", region]
    if dry_run:
        cmd.append("--dryrun")
    log(f"Publishing: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd)
    except FileNotFoundError:
        raise SystemExit("The aws CLI is not installed. Install it, or sync "
                         f"{SITE_DIR} to your static host by other means.")
    if result.returncode != 0:
        raise SystemExit(f"aws s3 sync failed (exit {result.returncode}).")
    log("Published." if not dry_run else "Dry run complete (nothing uploaded).")


# --- CLI -----------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Hannah Lab: build/check/preview/publish the public site.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="Sanitize runs, derive state, render the site.")
    sub.add_parser("check", help="Run the fail-closed sanitizer gate on the site.")
    pv = sub.add_parser("preview", help="Serve public_lab/site locally.")
    # 8890 by default: 8080 is llama-server, 8600 is the private web UI.
    pv.add_argument("--port", type=int, default=8890)
    pv.add_argument("--host", default="0.0.0.0",
                    help="Interface to bind (default 0.0.0.0 = reachable on "
                    "your LAN; use 127.0.0.1 for local-only).")
    pub = sub.add_parser("publish", help="Build, check, then push the site to S3.")
    pub.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if args.cmd == "build":
        ok = build()
        raise SystemExit(0 if ok else 1)
    if args.cmd == "check":
        raise SystemExit(0 if check() else 1)
    if args.cmd == "preview":
        preview(args.port, args.host)
        return
    if args.cmd == "publish":
        publish(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
