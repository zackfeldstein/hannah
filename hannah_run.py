#!/usr/bin/env python3
"""Experiment-oriented research workflow for Hannah.

Think in named runs, not date ranges:

    python3 hannah_run.py start --label tools-on-v3
    python3 hannah_run.py status
    python3 hannah_run.py collect --summarize

- start   : mark the beginning of an experiment, capturing the prompt, model,
            tools setting, and code version in effect.
- status  : show the active run and how many entries it has gathered.
- collect : stop the daemon, package everything since `start` into
            research/runs/<label>/ (metadata + entries + journal + prompts +
            report + summary), refresh research/INDEX.md and research/overview.md,
            rotate the logs fresh (resetting rolling memory), then restart the daemon.

Two files give you the whole picture without digging through folders:
    research/INDEX.md      - a table of every run
    research/overview.md   - an evolving summary of how Hannah changes across runs
"""

import argparse
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import hannah
import hannah_export as hx

RESEARCH = hannah.BASE_DIR / "research"
RUNS_DIR = RESEARCH / "runs"
CURRENT = RESEARCH / "current_run.json"
INDEX = RESEARCH / "INDEX.md"
OVERVIEW = RESEARCH / "overview.md"


# --- helpers ------------------------------------------------------------------

def _git_info():
    commit = hx._git_commit()
    dirty = None
    try:
        r = subprocess.run(["git", "status", "--porcelain"], cwd=str(hannah.BASE_DIR),
                           capture_output=True, text=True, timeout=5)
        dirty = bool(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return commit, dirty


def _daemon_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hannah.service"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def _daemon(action: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", action, "hannah.service"],
                       capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  (could not {action} daemon: {exc})", flush=True)


def _tool_stats(entries) -> dict:
    used, counts = 0, {}
    for e in entries:
        ts = e.get("tools") or []
        if ts:
            used += 1
        for t in ts:
            counts[t] = counts.get(t, 0) + 1
    return {
        "entries_using_tools": used,
        "tool_call_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }


# --- commands -----------------------------------------------------------------

def cmd_start(args):
    RESEARCH.mkdir(parents=True, exist_ok=True)
    if CURRENT.exists():
        cur = json.loads(CURRENT.read_text())
        raise SystemExit(
            f"A run is already active: '{cur['label']}' (started {cur['started_at']}).\n"
            "Collect it first:  python3 hannah_run.py collect --summarize"
        )
    cfg = hannah.load_config()
    commit, dirty = _git_info()
    now = datetime.now()
    run = {
        "label": args.label,
        "note": args.note,
        "started_at": now.isoformat(timespec="seconds"),
        "started_ts": now.timestamp(),
        "model": hannah.selected_model_name(cfg),
        "tools_enabled": cfg.get("tools", {}).get("enabled", False),
        "prompt_fingerprint": hannah.prompt_fingerprint(),
        "system_prompt": hannah.load_system_prompt(),
        "task_prompt": hannah.load_task_prompt(),
        "git_commit": commit,
        "git_dirty": dirty,
    }
    CURRENT.write_text(json.dumps(run, indent=2))
    print(f"Started run '{run['label']}' at {run['started_at']}")
    print(f"  model={run['model']}  tools={'on' if run['tools_enabled'] else 'off'}"
          f"  prompt={run['prompt_fingerprint']}  commit={commit}{'*' if dirty else ''}")
    print("Let Hannah run, then:  python3 hannah_run.py collect --summarize")


def cmd_status(args):
    if not CURRENT.exists():
        print("No active run. Start one with:  python3 hannah_run.py start --label <name>")
        return
    run = json.loads(CURRENT.read_text())
    since = datetime.fromtimestamp(run["started_ts"])
    entries = hx.load_entries(since, None)
    elapsed = (datetime.now() - since).total_seconds()
    print(f"Active run: {run['label']}")
    print(f"  started : {run['started_at']}  ({hannah._format_duration(elapsed)} ago)")
    print(f"  config  : model={run['model']}  tools={'on' if run['tools_enabled'] else 'off'}"
          f"  prompt={run['prompt_fingerprint']}")
    print(f"  entries : {len(entries)}  (using tools: {_tool_stats(entries)['entries_using_tools']})")
    if hannah.prompt_fingerprint() != run["prompt_fingerprint"]:
        print("  NOTE: the prompt has changed since this run started.")


def cmd_collect(args):
    if not CURRENT.exists():
        raise SystemExit("No active run to collect. Start one first.")
    run = json.loads(CURRENT.read_text())
    cfg = hannah.load_config()
    since = datetime.fromtimestamp(run["started_ts"])
    now = datetime.now()

    was_active = _daemon_active()
    if was_active:
        print("Stopping daemon…", flush=True)
        _daemon("stop")

    entries = hx.load_entries(since, now)
    if not entries:
        print("Warning: no entries in this run's window.", flush=True)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in run["label"])
    folder = RUNS_DIR / safe
    if folder.exists():
        folder = RUNS_DIR / f"{safe}_{now.strftime('%H%M%S')}"
    (folder / "prompts").mkdir(parents=True, exist_ok=True)

    base_manifest = hx.build_manifest(entries, since, now, cfg)
    commit_end, dirty_end = _git_info()
    manifest = {
        "label": run["label"],
        "note": run["note"],
        "started_at": run["started_at"],
        "ended_at": now.isoformat(timespec="seconds"),
        "duration": hannah._format_duration((now - since).total_seconds()),
        "model_at_start": run["model"],
        "tools_enabled": run["tools_enabled"],
        "prompt_fingerprint": run["prompt_fingerprint"],
        "git_commit_start": run["git_commit"],
        "git_commit_end": commit_end,
        "git_dirty": bool(run.get("git_dirty") or dirty_end),
        "tool_use": _tool_stats(entries),
    }
    manifest.update(base_manifest)  # entry_count, models_used, ranges, host, etc.
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with (folder / "entries.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    (folder / "journal.txt").write_text(
        ("\n" + "-" * 60 + "\n").join(hx._log_blocks(since, now)), encoding="utf-8")
    # Raw archives of the full logs as they stand at collection time.
    if hannah.LOG_FILE.exists():
        shutil.copy2(hannah.LOG_FILE, folder / "hannah.log")
    if hannah.MEMORY_FILE.exists():
        shutil.copy2(hannah.MEMORY_FILE, folder / "memory.jsonl")
    # The prompt this run actually used (captured at start).
    (folder / "prompts" / "system_prompt.txt").write_text(run["system_prompt"], encoding="utf-8")
    (folder / "prompts" / "task_prompt.txt").write_text(run["task_prompt"], encoding="utf-8")

    hx.write_report(folder / "report.md", manifest, entries)

    summary_text = None
    if args.summarize and entries:
        print("Generating run summary…", flush=True)
        model, summary_text = hx.generate_summary(
            entries, manifest, cfg, None, args.openai_model, args.local)
        (folder / "summary.md").write_text(
            f"# {run['label']} — summary (model: {model})\n\n{summary_text}\n", encoding="utf-8")

    # Rotate logs fresh (archives already saved into the run folder above).
    _reset_logs(reset_memory=not args.keep_memory)

    _rebuild_index()
    if summary_text is not None:
        _update_overview(manifest, summary_text, cfg, args)

    CURRENT.unlink()
    print(f"Collected run '{run['label']}' → {folder}")
    print("Logs rotated fresh." + ("" if args.keep_memory else " Rolling memory reset."))

    if was_active and not args.no_restart:
        print("Restarting daemon…", flush=True)
        _daemon("start")


# --- log rotation, index, overview -------------------------------------------

def _reset_logs(reset_memory: bool = True) -> None:
    try:
        if hannah.LOG_FILE.exists():
            hannah.LOG_FILE.write_text("")
    except OSError:
        pass
    if reset_memory:
        try:
            if hannah.MEMORY_FILE.exists():
                hannah.MEMORY_FILE.write_text("")
        except OSError:
            pass
        for f in (hannah.THEMES_FILE, hannah.STATE_FILE):
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass


def _rebuild_index() -> None:
    rows = []
    for mf in sorted(RUNS_DIR.glob("*/manifest.json")):
        try:
            rows.append(json.loads(mf.read_text()))
        except (OSError, ValueError):
            continue
    rows.sort(key=lambda m: m.get("started_at", ""))
    lines = [
        "# Hannah — Experiment Index\n",
        "| Run | Started | Duration | Model | Prompt | Tools | Entries | Used tools |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in rows:
        tu = m.get("tool_use", {})
        lines.append(
            f"| {m.get('label')} | {m.get('started_at')} | {m.get('duration')} | "
            f"{m.get('model_at_start')} | `{m.get('prompt_fingerprint')}` | "
            f"{'on' if m.get('tools_enabled') else 'off'} | {m.get('entry_count')} | "
            f"{tu.get('entries_using_tools', 0)} |"
        )
    INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Updated research/INDEX.md")


def _update_overview(manifest, run_summary, cfg, args) -> None:
    prev = OVERVIEW.read_text(encoding="utf-8") if OVERVIEW.exists() else ""
    keys = ["label", "started_at", "ended_at", "duration", "model_at_start",
            "tools_enabled", "prompt_fingerprint", "entry_count", "tool_use", "models_used"]
    meta = {k: manifest[k] for k in keys if k in manifest}
    system = (
        "You maintain a running research overview of how an edge-AI experiment "
        "named Hannah changes ACROSS successive experiments as its prompt, model, "
        "and tools are changed. Focus on trends between runs, not one run in isolation."
    )
    user = (
        (f"Current overview so far:\n{prev}\n\n" if prev else "This is the first experiment.\n\n")
        + f"A new experiment just completed.\nMetadata:\n{json.dumps(meta, indent=2)}\n\n"
        f"Its summary:\n{run_summary[:4000]}\n\n"
        "Rewrite the overall overview in Markdown: list the experiments "
        "chronologically and describe how Hannah's behavior, voice, and tool "
        "exploration shift across them, and what each prompt/model/tools change "
        "appears to cause. Keep it concise and trend-focused."
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        model, text = hx.run_analysis(messages, cfg, args.local, args.openai_model)
    except SystemExit as exc:
        print(f"Overview update skipped: {exc}")
        return
    OVERVIEW.write_text(
        f"# Hannah — Evolving Overview\n\n"
        f"_Last updated {datetime.now().isoformat(timespec='seconds')} (model: {model})_\n\n"
        f"{text}\n",
        encoding="utf-8",
    )
    print("Updated research/overview.md")


def main() -> None:
    p = argparse.ArgumentParser(description="Hannah experiment runner (start/status/collect).")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("start", help="Begin a named experiment.")
    ps.add_argument("--label", required=True, help="Short name for the experiment.")
    ps.add_argument("--note", default="", help="Optional description of the experiment.")

    sub.add_parser("status", help="Show the active run.")

    pc = sub.add_parser("collect", help="End the run, package it, refresh index/overview.")
    pc.add_argument("--summarize", action="store_true", help="Generate a run summary + update overview.")
    pc.add_argument("--local", action="store_true", help="Force the local model for analysis.")
    pc.add_argument("--openai-model", help="Override the OpenAI analysis model.")
    pc.add_argument("--keep-memory", action="store_true", help="Do not reset rolling memory.")
    pc.add_argument("--no-restart", action="store_true", help="Do not restart the daemon afterward.")

    args = p.parse_args()
    {"start": cmd_start, "status": cmd_status, "collect": cmd_collect}[args.cmd](args)


if __name__ == "__main__":
    main()
