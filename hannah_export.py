#!/usr/bin/env python3
"""Export a self-contained research bundle from Hannah's logs.

Bundles a time range of journal entries together with the prompts, model, config,
and metadata needed to interpret them - and can call an AI (OpenAI by default) to
produce a research summary. Standard-library only.

Examples:
    python3 hannah_export.py --label baseline
    python3 hannah_export.py --since 2026-07-01 --until 2026-07-03 --label cadence-test
    OPENAI_API_KEY=sk-... python3 hannah_export.py --label run1 --summarize
"""

import argparse
import json
import os
import platform
import subprocess
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import hannah


def _parse_bound(value: str, end_of_day: bool):
    """Parse a --since/--until value (date or ISO datetime) into a datetime."""
    if not value:
        return None
    text = value.strip()
    if len(text) == 10 and "T" not in text:  # date-only
        text += "T23:59:59" if end_of_day else "T00:00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"Could not parse date/time: {value!r} (use YYYY-MM-DD or ISO)")


def _entry_time(rec):
    try:
        return datetime.fromisoformat(rec["time"])
    except (KeyError, ValueError):
        return None


def load_entries(since, until):
    """Load memory entries within [since, until] (inclusive)."""
    entries = []
    if not hannah.MEMORY_FILE.exists():
        return entries
    for line in hannah.MEMORY_FILE.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        t = _entry_time(rec)
        if t is None:
            continue
        if since and t < since:
            continue
        if until and t > until:
            continue
        entries.append(rec)
    return entries


def _log_blocks(since, until):
    """Return raw hannah.log blocks (observation + entry) within the range."""
    texts = []
    for path in [hannah.LOG_FILE] + [
        hannah.LOG_FILE.with_suffix(hannah.LOG_FILE.suffix + f".{i}") for i in (1, 2, 3)
    ]:
        if path.exists():
            texts.append(path.read_text(errors="ignore"))
    combined = "\n".join(texts)
    blocks = []
    for chunk in combined.split("-" * 60):
        chunk = chunk.strip()
        if not chunk:
            continue
        t = None
        for ln in chunk.splitlines():
            if ln.startswith("Time: "):
                try:
                    t = datetime.fromisoformat(ln[len("Time: "):].strip())
                except ValueError:
                    t = None
                break
        if t is None:
            continue
        if since and t < since:
            continue
        if until and t > until:
            continue
        blocks.append((t, chunk))
    blocks.sort(key=lambda b: b[0])
    return [b[1] for b in blocks]


def _distribution(entries, key):
    counts = {}
    for e in entries:
        counts[e.get(key) or "(unknown)"] = counts.get(e.get(key) or "(unknown)", 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _host_info():
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    tegra = hannah._read_text("/etc/nv_tegra_release")
    if tegra:
        info["jetpack"] = tegra.splitlines()[0]
    return info


def _git_commit():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(hannah.BASE_DIR), capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def build_manifest(entries, since, until, cfg):
    times = [_entry_time(e) for e in entries if _entry_time(e)]
    return {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "requested_range": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "actual_range": {
            "first_entry": min(times).isoformat() if times else None,
            "last_entry": max(times).isoformat() if times else None,
        },
        "entry_count": len(entries),
        "models_used": _distribution(entries, "model"),
        "prompt_versions_used": _distribution(entries, "prompt"),
        "active_model_now": hannah.selected_model_name(cfg),
        "config_snapshot": {
            "generation": cfg.get("generation"),
            "daemon": cfg.get("daemon"),
            "salience": cfg.get("salience"),
            "memory": cfg.get("memory"),
            "models": cfg.get("models"),
        },
        "host": _host_info(),
        "git_commit": _git_commit(),
    }


def write_report(path, manifest, entries):
    lines = ["# Hannah — Research Export\n"]
    lines.append(f"- Exported: {manifest['exported_at']}")
    r = manifest["actual_range"]
    lines.append(f"- Entry range: {r['first_entry']} → {r['last_entry']}")
    lines.append(f"- Entries: {manifest['entry_count']}")
    lines.append(f"- Models: {manifest['models_used']}")
    lines.append(f"- Prompt versions: {manifest['prompt_versions_used']}")
    lines.append(f"- Host: {manifest['host'].get('jetpack', manifest['host']['platform'])}")
    lines.append(f"- Commit: {manifest.get('git_commit')}\n")
    lines.append("## Entries\n")
    for e in entries:
        lines.append(f"### {e.get('time')}  ·  {e.get('model', '?')}  ·  prompt {e.get('prompt', '?')}")
        lines.append("")
        lines.append(e.get("entry", "").strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _analysis_messages(entries, manifest, cfg, prev_summary, cap):
    """Build the [system, user] chat messages for a research analysis."""
    capped = entries[:cap]
    corpus = "\n\n".join(
        f"[{e.get('time')} | {e.get('model', '?')} | prompt {e.get('prompt', '?')}]\n"
        f"{e.get('entry', '').strip()}"
        for e in capped
    )
    system = (
        "You are a research analyst studying 'Hannah', an edge-AI experiment in "
        "machine self-observation: a local LLM on a Jetson periodically writes "
        "first-person journal entries about its own hardware state and the passage "
        "of time. Analyze the dataset rigorously and concretely."
    )
    sections = (
        "1. Overview\n2. Recurring themes\n3. Notable events (arrivals/departures, "
        "load/thermal/power spikes, downtime/waking)\n4. How the voice/behavior "
        "evolved over time\n5. Differences across models or prompt versions\n"
        "6. Anomalies, repetition, or signs of degradation\n7. Suggested next "
        "experiments."
    )
    prior = ""
    if prev_summary:
        prior = (
            "Below is the analysis from the PREVIOUS export. Use it as the baseline "
            "to compare against.\n\n=== PREVIOUS ANALYSIS ===\n"
            f"{prev_summary}\n=== END PREVIOUS ANALYSIS ===\n\n"
        )
        sections += (
            "\n8. Changes since the previous analysis - concretely compare this run "
            "to the previous analysis above: what is new, what has shifted in "
            "behavior/voice/themes, and the effect of any model, prompt, or config "
            "changes between the two."
        )
    user = (
        f"{prior}"
        f"Metadata for THIS run:\n{json.dumps(manifest, indent=2)}\n\n"
        f"Journal entries ({len(capped)} of {len(entries)}):\n\n{corpus}\n\n"
        f"Write a research summary in Markdown with sections:\n{sections}\n"
        "Cite specific entries (by timestamp) as evidence."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def openai_summary(entries, manifest, cfg, model_override=None, prev_summary=None):
    """Produce a research analysis via OpenAI (stdlib urllib). Requires OPENAI_API_KEY."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    acfg = cfg.get("analysis", {})
    model = model_override or acfg.get("openai_model", "gpt-5.5")
    base = acfg.get("openai_base_url", "https://api.openai.com/v1").rstrip("/")
    messages = _analysis_messages(
        entries, manifest, cfg, prev_summary, acfg.get("max_entries", 300)
    )
    payload = {"model": model, "messages": messages}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return model, body["choices"][0]["message"]["content"].strip()


def local_summary(entries, manifest, cfg, prev_summary=None):
    """Fallback analysis via the local llama-server.

    The local model has a small context window, so far fewer entries are sent than
    to OpenAI. Requires the llama-server (hannah-llama.service) to be running.
    """
    acfg = cfg.get("analysis", {})
    if not hannah.server_healthy(cfg):
        raise SystemExit(
            "Local llama-server is not reachable. Start it (systemctl --user start "
            "hannah-llama.service) or set OPENAI_API_KEY for cloud analysis."
        )
    messages = _analysis_messages(
        entries, manifest, cfg, prev_summary, acfg.get("local_max_entries", 40)
    )
    srv = cfg["server"]
    payload = {
        "messages": messages,
        "max_tokens": acfg.get("local_tokens", 1500),
        "temperature": 0.4,
        "stream": False,
    }
    req = urllib.request.Request(
        srv["url"].rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=srv["timeout_s"]) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = body["choices"][0]["message"]
    text = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    return f"local:{hannah.selected_model_name(cfg)}", hannah._strip_thinking(text)


def generate_summary(entries, manifest, cfg, prev_summary, openai_model, force_local):
    """Pick the analysis backend: local if forced or no key; OpenAI otherwise, with
    an automatic fallback to the local model if the OpenAI call fails."""
    if force_local:
        print("Using the local model for analysis (--local).", flush=True)
        return local_summary(entries, manifest, cfg, prev_summary)
    if not os.environ.get("OPENAI_API_KEY"):
        print("No OPENAI_API_KEY set — falling back to the local model.", flush=True)
        return local_summary(entries, manifest, cfg, prev_summary)
    try:
        return openai_summary(entries, manifest, cfg, openai_model, prev_summary)
    except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"OpenAI analysis failed ({exc}); falling back to the local model.", flush=True)
        return local_summary(entries, manifest, cfg, prev_summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a Hannah research bundle.")
    parser.add_argument("--since", help="Start (YYYY-MM-DD or ISO datetime).")
    parser.add_argument("--until", help="End (YYYY-MM-DD or ISO datetime).")
    parser.add_argument("--label", default="export", help="Short label for the bundle folder.")
    parser.add_argument("--out", default=str(hannah.BASE_DIR / "research"),
                        help="Base output directory (default: ./research).")
    parser.add_argument("--summarize", action="store_true",
                        help="Generate summary.md (OpenAI if OPENAI_API_KEY is set, "
                        "otherwise the local llama-server).")
    parser.add_argument("--local", action="store_true",
                        help="Force the local llama-server for the summary (skip OpenAI).")
    parser.add_argument("--openai-model", help="Override the OpenAI model for --summarize.")
    args = parser.parse_args()

    cfg = hannah.load_config()
    since = _parse_bound(args.since, end_of_day=False)
    until = _parse_bound(args.until, end_of_day=True)

    entries = load_entries(since, until)
    if not entries:
        raise SystemExit("No entries found in that range; nothing to export.")

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_label = "".join(c if c.isalnum() or c in "-_" else "-" for c in args.label)
    bundle = Path(args.out) / f"{stamp}_{safe_label}"
    (bundle / "prompts" / "history").mkdir(parents=True, exist_ok=True)

    manifest = build_manifest(entries, since, until, cfg)
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Structured entries + raw log slice.
    with (bundle / "entries.jsonl").open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    (bundle / "journal.txt").write_text(
        ("\n" + "-" * 60 + "\n").join(_log_blocks(since, until)), encoding="utf-8"
    )

    # Prompt snapshots: current + every archived version referenced by the entries.
    (bundle / "prompts" / "system_prompt.txt").write_text(
        hannah.load_system_prompt(), encoding="utf-8")
    (bundle / "prompts" / "task_prompt.txt").write_text(
        hannah.load_task_prompt(), encoding="utf-8")
    for fp in manifest["prompt_versions_used"]:
        src = hannah.PROMPT_HISTORY_DIR / f"{fp}.json"
        if src.exists():
            (bundle / "prompts" / "history" / f"{fp}.json").write_text(
                src.read_text(), encoding="utf-8")

    write_report(bundle / "report.md", manifest, entries)

    if args.summarize:
        out_base = Path(args.out)
        latest = out_base / "latest_summary.md"   # stable pointer for the next run
        prev_summary = latest.read_text(encoding="utf-8") if latest.exists() else None
        if prev_summary:
            print("Found previous summary — will compare against it.", flush=True)
        model, summary = generate_summary(
            entries, manifest, cfg, prev_summary, args.openai_model, args.local
        )
        header = (
            f"# AI Analysis (model: {model})\n"
            f"- Generated: {manifest['exported_at']}\n"
            f"- Range: {manifest['actual_range']['first_entry']} → "
            f"{manifest['actual_range']['last_entry']}\n"
            f"- Compared to previous: {'yes' if prev_summary else 'no (first run)'}\n\n"
        )
        content = header + summary + "\n"
        # Per-run copy, timestamped history, and the stable latest pointer.
        (bundle / "summary.md").write_text(content, encoding="utf-8")
        hist = out_base / "summaries"
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"{stamp}_{safe_label}.md").write_text(content, encoding="utf-8")
        latest.write_text(content, encoding="utf-8")
        print(f"  summary.md written ({model}); latest_summary.md updated.")

    print(f"Exported {len(entries)} entries to: {bundle}")


if __name__ == "__main__":
    main()
