#!/usr/bin/env python3
"""Experiment lifecycle for Hannah - reusable core + a thin CLI.

Think in named runs, not date ranges. The same functions power both this CLI and
the web UI:

    python3 hannah_run.py start --label tools-on-v3
    python3 hannah_run.py status
    python3 hannah_run.py collect --summarize

- start   : begin an experiment, capturing the prompt, model, tools setting, and
            code version in effect (and make sure the daemon is running).
- status  : show the active run and how many entries it has gathered.
- collect : stop the daemon, package everything since start into
            research/runs/<label>/, refresh research/INDEX.md + overview.md,
            rotate the logs fresh (resetting rolling memory), then restart.
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
# Public-lab experiment registry (description / goal / hypothesis / status),
# shown on the public site. Creating an experiment can write its metadata here.
PUBLIC_REGISTRY = hannah.BASE_DIR / "public_lab" / "experiments.json"


# --- daemon + git helpers -----------------------------------------------------

def daemon_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hannah.service"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def daemon_action(action: str, log=print) -> None:
    try:
        subprocess.run(["systemctl", "--user", action, "hannah.service"],
                       capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"(could not {action} daemon: {exc})")


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


def restart_llama(log=print) -> None:
    """Reload llama-server so a newly selected model takes effect."""
    try:
        subprocess.run(["systemctl", "--user", "restart", "hannah-llama.service"],
                       capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"(could not restart llama-server: {exc})")


def update_experiment_registry(name: str, meta: dict, log=print) -> None:
    """Merge experiment metadata into the public lab registry.

    Only known, non-empty fields are written; existing values for other
    fields are preserved so hand edits survive.
    """
    allowed = {k: str(meta[k]).strip() for k in
               ("description", "goal", "hypothesis", "notes", "status")
               if meta.get(k) and str(meta[k]).strip()}
    if not allowed:
        return
    try:
        registry = json.loads(PUBLIC_REGISTRY.read_text())
    except (OSError, ValueError):
        registry = {}
    entry = registry.get(name, {})
    entry.update(allowed)
    registry[name] = entry
    try:
        PUBLIC_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        PUBLIC_REGISTRY.write_text(json.dumps(registry, indent=2) + "\n",
                                   encoding="utf-8")
        log(f"Recorded experiment metadata for the public lab ({', '.join(allowed)}).")
    except OSError as exc:
        log(f"(could not write experiment registry: {exc})")


def _tool_stats(entries) -> dict:
    used, counts = 0, {}
    for e in entries:
        ts = e.get("tools") or []
        if ts:
            used += 1
        for t in ts:
            counts[t] = counts.get(t, 0) + 1
    return {"entries_using_tools": used,
            "tool_call_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}


# --- core API (used by both the CLI and the web UI) ---------------------------

def start_run(label: str, note: str = "", fresh: bool = False, cfg=None,
              tools=None, model=None, system_prompt=None, meta=None,
              log=print) -> dict:
    """Begin an experiment, applying its whole configuration in one step.

    Ensures the daemon is running; optionally resets memory. All parameters
    beyond label/note/fresh are optional and None means "keep as-is":

    tools:         list of tool names to offer Hannah for this run (a subset
                   of hannah.TOOLS; [] = no tools at all)
    model:         configured model name to switch to (reloads llama-server)
    system_prompt: replacement system prompt text (archived for provenance)
    meta:          public-lab registry metadata (description / goal /
                   hypothesis / notes / status), shown on the public site
    """
    cfg = cfg or hannah.load_config()
    RESEARCH.mkdir(parents=True, exist_ok=True)
    if CURRENT.exists():
        cur = json.loads(CURRENT.read_text())
        raise RuntimeError(f"A run is already active: '{cur['label']}'. Collect it first.")

    if model:
        if not hannah.set_selected_model(model, cfg):
            raise RuntimeError(f"Unknown model: {model!r}. Configured: "
                               f"{', '.join(hannah.list_models(cfg))}")
        log(f"Model for this run: {model} (reloading llama-server, ~10-30s)…")
        restart_llama(log)

    if isinstance(system_prompt, str) and system_prompt.strip():
        if system_prompt.strip() != hannah.load_system_prompt().strip():
            hannah.SYSTEM_PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
            hannah.SYSTEM_PROMPT_FILE.write_text(system_prompt, encoding="utf-8")
            hannah.ensure_prompt_archived()
            log("Updated the system prompt for this run.")

    if tools is not None:
        if not hannah.set_enabled_tools(tools, cfg):
            unknown = [t for t in tools if t not in hannah.TOOLS]
            raise RuntimeError(f"Unknown tool(s): {', '.join(unknown)}. "
                               f"Available: {', '.join(hannah.TOOLS)}")
        log(f"Tools for this run: {', '.join(tools) if tools else '(none)'}.")

    if meta:
        update_experiment_registry(label, meta, log)

    if not daemon_active():
        log("Starting daemon…")
        daemon_action("start", log)
    if fresh:
        _reset_logs(reset_memory=True)
        log("Reset logs and rolling memory for a clean start.")
    commit, dirty = _git_info()
    now = datetime.now()
    available = hannah.enabled_tool_names(cfg)
    run = {
        "label": label,
        "note": note,
        "started_at": now.isoformat(timespec="seconds"),
        "started_ts": now.timestamp(),
        "model": hannah.selected_model_name(cfg),
        "tools_enabled": bool(cfg.get("tools", {}).get("enabled", False)
                              and available),
        "tools_available": available,
        "prompt_fingerprint": hannah.prompt_fingerprint(),
        "system_prompt": hannah.load_system_prompt(),
        "task_prompt": hannah.load_task_prompt(),
        "git_commit": commit,
        "git_dirty": dirty,
    }
    CURRENT.write_text(json.dumps(run, indent=2))
    log(f"Started run '{label}' (model={run['model']}, tools="
        f"{'on' if run['tools_enabled'] else 'off'} "
        f"[{', '.join(run['tools_available']) or 'none'}], "
        f"prompt={run['prompt_fingerprint']}).")
    return run


def active_run(cfg=None) -> dict:
    """Return a light status dict for the active run, or None."""
    if not CURRENT.exists():
        return None
    run = json.loads(CURRENT.read_text())
    since = datetime.fromtimestamp(run["started_ts"])
    entries = hx.load_entries(since, None)
    return {
        "label": run["label"],
        "note": run.get("note", ""),
        "started_at": run["started_at"],
        "elapsed": hannah._format_duration((datetime.now() - since).total_seconds()),
        "model": run["model"],
        "tools_enabled": run["tools_enabled"],
        "tools_available": run.get("tools_available",
                                   hannah.enabled_tool_names(cfg)),
        "prompt_fingerprint": run["prompt_fingerprint"],
        "entries_so_far": len(entries),
        "entries_using_tools": _tool_stats(entries)["entries_using_tools"],
        "prompt_changed": hannah.prompt_fingerprint() != run["prompt_fingerprint"],
        "tools_changed": (run.get("tools_available") is not None
                          and hannah.enabled_tool_names(cfg)
                          != run["tools_available"]),
    }


def collect_run(summarize: bool = True, local: bool = False, openai_model=None,
                keep_memory: bool = False, restart: bool = True, cfg=None, log=print) -> dict:
    """End the active run: package it, summarize, refresh index/overview, rotate logs."""
    if not CURRENT.exists():
        raise RuntimeError("No active run to collect.")
    run = json.loads(CURRENT.read_text())
    cfg = cfg or hannah.load_config()
    since = datetime.fromtimestamp(run["started_ts"])
    now = datetime.now()

    was_active = daemon_active()
    if was_active:
        log("Stopping daemon…")
        daemon_action("stop", log)

    entries = hx.load_entries(since, now)
    if not entries:
        log("Warning: no entries in this run's window.")

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
        "tools_available": run.get("tools_available"),
        "prompt_fingerprint": run["prompt_fingerprint"],
        "git_commit_start": run["git_commit"],
        "git_commit_end": commit_end,
        "git_dirty": bool(run.get("git_dirty") or dirty_end),
        "tool_use": _tool_stats(entries),
    }
    manifest.update(base_manifest)
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))

    with (folder / "entries.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    (folder / "journal.txt").write_text(
        ("\n" + "-" * 60 + "\n").join(hx._log_blocks(since, now)), encoding="utf-8")
    if hannah.LOG_FILE.exists():
        shutil.copy2(hannah.LOG_FILE, folder / "hannah.log")
    if hannah.MEMORY_FILE.exists():
        shutil.copy2(hannah.MEMORY_FILE, folder / "memory.jsonl")
    (folder / "prompts" / "system_prompt.txt").write_text(run["system_prompt"], encoding="utf-8")
    (folder / "prompts" / "task_prompt.txt").write_text(run["task_prompt"], encoding="utf-8")

    hx.write_report(folder / "report.md", manifest, entries)

    summary_model = None
    summary_text = None
    if summarize and entries:
        log("Generating run summary…")
        summary_model, summary_text = hx.generate_summary(
            entries, manifest, cfg, None, openai_model, local)
        (folder / "summary.md").write_text(
            f"# {run['label']} — summary (model: {summary_model})\n\n{summary_text}\n",
            encoding="utf-8")

    _reset_logs(reset_memory=not keep_memory)
    _rebuild_index()
    if summary_text is not None:
        _update_overview(manifest, summary_text, cfg, local, openai_model, log)

    # If the registry marked this experiment active, flip it to complete so the
    # public lab's status stays truthful.
    try:
        registry = json.loads(PUBLIC_REGISTRY.read_text())
        if registry.get(run["label"], {}).get("status") == "active":
            registry[run["label"]]["status"] = "complete"
            PUBLIC_REGISTRY.write_text(json.dumps(registry, indent=2) + "\n",
                                       encoding="utf-8")
    except (OSError, ValueError):
        pass

    # Refresh the public lab (sanitized artifacts + static site). Build-only;
    # publishing to a remote host stays an explicit, separate step.
    if cfg.get("lab", {}).get("auto_build", True):
        try:
            import hannah_lab
            log("Rebuilding the public lab site…")
            hannah_lab.build(cfg, log=log)
        except Exception as exc:  # the lab must never break a collect
            log(f"(public lab build failed: {exc})")

    CURRENT.unlink()
    log(f"Collected '{run['label']}' → {folder.name}. Logs rotated fresh"
        f"{'' if keep_memory else ', memory reset'}.")

    if was_active and restart:
        log("Restarting daemon…")
        daemon_action("start", log)

    return {
        "label": run["label"],
        "folder": folder.name,
        "entries": len(entries),
        "summary_model": summary_model,
    }


def list_runs() -> list:
    """Summaries of all collected runs, newest first."""
    runs = []
    for mf in sorted(RUNS_DIR.glob("*/manifest.json")):
        try:
            m = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        runs.append({
            "folder": mf.parent.name,
            "label": m.get("label"),
            "started_at": m.get("started_at"),
            "ended_at": m.get("ended_at"),
            "duration": m.get("duration"),
            "model": m.get("model_at_start"),
            "prompt": m.get("prompt_fingerprint"),
            "tools_enabled": m.get("tools_enabled"),
            "entries": m.get("entry_count"),
            "tool_entries": m.get("tool_use", {}).get("entries_using_tools", 0),
        })
    runs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return runs


def run_detail(folder: str) -> dict:
    """Manifest + AI summary + any custom notes for one run folder."""
    safe = Path(folder).name
    d = RUNS_DIR / safe
    manifest = {}
    if (d / "manifest.json").exists():
        try:
            manifest = json.loads((d / "manifest.json").read_text())
        except ValueError:
            pass
    summary = (d / "summary.md").read_text() if (d / "summary.md").exists() else ""
    return {"manifest": manifest, "summary": summary, "notes": _read_notes(d)}


def run_raw(folder: str, kind: str = "journal") -> str:
    """Raw text for an experiment, for copy/paste. kind = 'journal' or 'entries'."""
    d = RUNS_DIR / Path(folder).name
    if kind == "entries":
        ef = d / "entries.jsonl"
        if not ef.exists():
            return ""
        # A compact, paste-ready block: a small context header + each entry.
        header = ""
        mf = d / "manifest.json"
        if mf.exists():
            try:
                m = json.loads(mf.read_text())
                header = (f"Experiment: {m.get('label')} | model: {m.get('model_at_start')} "
                          f"| prompt: {m.get('prompt_fingerprint')} | tools: "
                          f"{'on' if m.get('tools_enabled') else 'off'} | "
                          f"{m.get('entry_count')} entries\n\n")
            except ValueError:
                pass
        out = []
        for line in ef.read_text(errors="ignore").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            out.append(f"[{r.get('time')}] {r.get('entry', '').strip()}")
        return header + "\n\n".join(out)
    jf = d / "journal.txt"
    return jf.read_text(errors="ignore") if jf.exists() else ""


def _read_notes(d: Path) -> list:
    nf = d / "notes.json"
    if nf.exists():
        try:
            return json.loads(nf.read_text())
        except ValueError:
            return []
    return []


def _rebuild_lab(cfg=None, log=print) -> None:
    """Refresh the public lab site so it reflects run/experiment changes."""
    cfg = cfg or hannah.load_config()
    if not cfg.get("lab", {}).get("auto_build", True):
        return
    try:
        import hannah_lab
        hannah_lab.build(cfg, log=lambda *a, **k: None)
        log("Rebuilt the public lab site.")
    except Exception as exc:  # lab problems must never break a delete
        log(f"(public lab rebuild failed: {exc})")


def delete_run(folder: str) -> bool:
    """Permanently remove one run folder (guarded to stay inside runs/)."""
    d = RUNS_DIR / Path(folder).name
    try:
        resolved = d.resolve()
        if resolved.parent != RUNS_DIR.resolve() or not resolved.is_dir():
            return False
        shutil.rmtree(resolved)
    except OSError:
        return False
    _rebuild_index()
    _rebuild_lab()
    return True


def experiment_run_folders(name: str) -> list:
    """Folder names of every collected run belonging to an experiment label."""
    folders = []
    for mf in sorted(RUNS_DIR.glob("*/manifest.json")):
        try:
            if json.loads(mf.read_text()).get("label") == name:
                folders.append(mf.parent.name)
        except (OSError, ValueError):
            continue
    return folders


def _latest_run_folder(name: str):
    """Folder name of the most recent collected run of an experiment, or None."""
    best, best_key = None, ""
    for folder in experiment_run_folders(name):
        try:
            m = json.loads((RUNS_DIR / folder / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        key = m.get("ended_at") or m.get("started_at") or folder
        if key >= best_key:
            best, best_key = folder, key
    return best


def rerun_experiment(name: str, keep_memory: bool = False, cfg=None,
                     log=print) -> dict:
    """Start another run of an existing experiment, reusing its configuration.

    Reuses the model, tools, system prompt, and public-lab metadata captured in
    the experiment's most recent run, and starts the next run under the same
    label (the lab groups them together and tracks how beliefs/memory evolve
    across them).

    keep_memory=False (default): an independent replicate under the same
    conditions, with rolling memory reset. keep_memory=True: continue from the
    previous run's rolling memory (restored from its bundle).
    """
    name = (name or "").strip()
    if not name:
        raise RuntimeError("an experiment name is required")
    cfg = cfg or hannah.load_config()
    folder = _latest_run_folder(name)
    if not folder:
        raise RuntimeError(f"no collected runs found for experiment {name!r} "
                           "to reuse — start it fresh instead")
    d = RUNS_DIR / folder
    try:
        manifest = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        manifest = {}
    model = manifest.get("model_at_start")
    tools = manifest.get("tools_available")  # None for older runs → keep current
    prompt_file = d / "prompts" / "system_prompt.txt"
    system_prompt = (prompt_file.read_text(encoding="utf-8")
                     if prompt_file.exists() else None)
    try:
        registry = json.loads(PUBLIC_REGISTRY.read_text())
    except (OSError, ValueError):
        registry = {}
    meta = dict(registry.get(name, {}))
    meta["status"] = "active"

    if keep_memory:
        # Restore the previous run's rolling memory so she continues from it
        # (collect resets logs/memory.jsonl, so the chain lives in the bundle).
        src = d / "memory.jsonl"
        if src.exists():
            try:
                hannah.LOG_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, hannah.MEMORY_FILE)
                log("Restored rolling memory from the previous run.")
            except OSError as exc:
                log(f"(could not restore memory: {exc})")
        else:
            log("(no saved memory in the previous run bundle; starting clean)")

    start_run(name, meta.get("description", ""), fresh=not keep_memory, cfg=cfg,
              tools=tools, model=model, system_prompt=system_prompt, meta=meta,
              log=log)
    mode = "continuing memory" if keep_memory else "fresh replicate"
    log(f"Re-running '{name}' ({mode}), reusing config from {folder}.")
    return {"label": name, "keep_memory": keep_memory, "reused_from": folder}


def delete_experiment(name: str, log=print) -> dict:
    """Delete an entire experiment: all its run folders + its registry entry.

    Refuses while the experiment is actively running (collect it first).
    Returns {"deleted_runs": [...], "registry_removed": bool}.
    """
    name = (name or "").strip()
    if not name:
        raise RuntimeError("an experiment name is required")
    if CURRENT.exists():
        try:
            cur = json.loads(CURRENT.read_text())
        except (OSError, ValueError):
            cur = {}
        if cur.get("label") == name:
            raise RuntimeError(f"'{name}' is the active experiment - stop & "
                               "collect (or discard) it before deleting")

    folders = experiment_run_folders(name)
    deleted = []
    for folder in folders:
        d = RUNS_DIR / Path(folder).name
        try:
            resolved = d.resolve()
            if resolved.parent == RUNS_DIR.resolve() and resolved.is_dir():
                shutil.rmtree(resolved)
                deleted.append(folder)
        except OSError as exc:
            log(f"(could not delete run {folder}: {exc})")

    registry_removed = False
    try:
        registry = json.loads(PUBLIC_REGISTRY.read_text())
        if name in registry:
            del registry[name]
            PUBLIC_REGISTRY.write_text(json.dumps(registry, indent=2) + "\n",
                                       encoding="utf-8")
            registry_removed = True
    except (OSError, ValueError):
        pass

    if not deleted and not registry_removed:
        raise RuntimeError(f"unknown experiment: {name!r}")

    _rebuild_index()
    _rebuild_lab(log=log)
    log(f"Deleted experiment '{name}': {len(deleted)} run(s)"
        f"{', registry entry removed' if registry_removed else ''}.")
    return {"deleted_runs": deleted, "registry_removed": registry_removed}


def add_note(folder: str, label: str, text: str) -> list:
    """Attach a custom summary/note (e.g. a ChatGPT analysis) to an experiment."""
    d = RUNS_DIR / Path(folder).name
    if not d.exists():
        raise RuntimeError("unknown experiment")
    notes = _read_notes(d)
    notes.append({
        "label": label or "note",
        "text": text,
        "added_at": datetime.now().isoformat(timespec="seconds"),
    })
    (d / "notes.json").write_text(json.dumps(notes, indent=2), encoding="utf-8")
    return notes


def read_overview() -> str:
    return OVERVIEW.read_text(encoding="utf-8") if OVERVIEW.exists() else ""


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
    rows = sorted((list_runs()), key=lambda r: r.get("started_at") or "")
    lines = [
        "# Hannah — Experiment Index\n",
        "| Run | Started | Duration | Model | Prompt | Tools | Entries | Used tools |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in rows:
        lines.append(
            f"| {m['label']} | {m['started_at']} | {m['duration']} | {m['model']} | "
            f"`{m['prompt']}` | {'on' if m['tools_enabled'] else 'off'} | "
            f"{m['entries']} | {m['tool_entries']} |"
        )
    INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _update_overview(manifest, run_summary, cfg, local, openai_model, log=print) -> None:
    prev = read_overview()
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
        model, text = hx.run_analysis(messages, cfg, local, openai_model)
    except SystemExit as exc:
        log(f"Overview update skipped: {exc}")
        return
    OVERVIEW.write_text(
        f"# Hannah — Evolving Overview\n\n"
        f"_Last updated {datetime.now().isoformat(timespec='seconds')} (model: {model})_\n\n"
        f"{text}\n",
        encoding="utf-8",
    )
    log("Updated overview.")


# --- CLI ----------------------------------------------------------------------

def _parse_tools(value):
    """--tools 'a,b,c' -> list; 'all' -> every tool; 'none' -> []; None -> None."""
    if value is None:
        return None
    value = value.strip().lower()
    if value == "all":
        return list(hannah.TOOLS)
    if value in ("none", ""):
        return []
    return [t.strip() for t in value.split(",") if t.strip()]


def _cli_start(args):
    meta = {"description": args.description, "goal": args.goal,
            "hypothesis": args.hypothesis, "status": "active"}
    start_run(args.label, args.note or args.description, args.fresh,
              tools=_parse_tools(args.tools), model=args.model,
              meta=meta if any(v for k, v in meta.items() if k != "status")
              else None)
    print("Let Hannah run, then:  python3 hannah_run.py collect --summarize")


def _cli_status(args):
    run = active_run()
    if not run:
        print("No active run. Start one with:  python3 hannah_run.py start --label <name>")
        return
    print(f"Active run: {run['label']}  ({run['elapsed']} ago)")
    print(f"  model={run['model']}  tools={'on' if run['tools_enabled'] else 'off'}"
          f" [{', '.join(run.get('tools_available', [])) or 'none'}]"
          f"  prompt={run['prompt_fingerprint']}")
    print(f"  entries: {run['entries_so_far']}  (using tools: {run['entries_using_tools']})")
    if run["prompt_changed"]:
        print("  NOTE: the prompt has changed since this run started.")


def _cli_collect(args):
    collect_run(summarize=args.summarize, local=args.local, openai_model=args.openai_model,
                keep_memory=args.keep_memory, restart=not args.no_restart)


def _cli_rerun(args):
    rerun_experiment(args.experiment, keep_memory=args.keep_memory)
    print("Let it run, then:  python3 hannah_run.py collect --summarize")


def _cli_delete(args):
    if args.run:
        if delete_run(args.run):
            print(f"Deleted run folder '{args.run}'.")
        else:
            raise SystemExit(f"Could not delete run folder: {args.run!r}")
        return
    folders = experiment_run_folders(args.experiment)
    if folders and not args.yes:
        print(f"This will permanently delete experiment '{args.experiment}' "
              f"and its {len(folders)} run(s): {', '.join(folders)}")
        if input("Type the experiment name to confirm: ").strip() != args.experiment:
            raise SystemExit("Aborted.")
    delete_experiment(args.experiment)


def main() -> None:
    p = argparse.ArgumentParser(description="Hannah experiment runner (start/status/collect).")
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("start", help="Begin a named experiment.")
    ps.add_argument("--label", required=True)
    ps.add_argument("--note", default="")
    ps.add_argument("--fresh", action="store_true", help="Reset logs/memory for a clean start.")
    ps.add_argument("--tools", default=None, metavar="LIST",
                    help="Tools to offer Hannah for this run: a comma-separated "
                    f"subset of {{{','.join(hannah.TOOLS)}}}, 'all', or 'none'. "
                    "Default: keep the current selection.")
    ps.add_argument("--model", default=None,
                    help="Switch to this configured model (reloads llama-server).")
    ps.add_argument("--description", default="",
                    help="Experiment description for the public lab registry.")
    ps.add_argument("--goal", default="",
                    help="Experiment goal for the public lab registry.")
    ps.add_argument("--hypothesis", default="",
                    help="Experiment hypothesis for the public lab registry.")
    sub.add_parser("status", help="Show the active run.")
    pc = sub.add_parser("collect", help="End the run, package it, refresh index/overview.")
    pc.add_argument("--summarize", action="store_true")
    pc.add_argument("--local", action="store_true")
    pc.add_argument("--openai-model")
    pc.add_argument("--keep-memory", action="store_true")
    pc.add_argument("--no-restart", action="store_true")
    pr = sub.add_parser("rerun", help="Start another run of an existing experiment, reusing its config.")
    pr.add_argument("--experiment", required=True)
    pr.add_argument("--keep-memory", action="store_true",
                    help="Continue from the previous run's rolling memory "
                    "(default: fresh replicate).")
    pd = sub.add_parser("delete", help="Delete an experiment (all runs + registry) or one run folder.")
    group = pd.add_mutually_exclusive_group(required=True)
    group.add_argument("--experiment", help="Experiment name: delete ALL its runs + registry entry.")
    group.add_argument("--run", help="Delete a single run folder under research/runs/.")
    pd.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = p.parse_args()
    {"start": _cli_start, "status": _cli_status, "collect": _cli_collect,
     "rerun": _cli_rerun, "delete": _cli_delete}[args.cmd](args)


if __name__ == "__main__":
    main()
