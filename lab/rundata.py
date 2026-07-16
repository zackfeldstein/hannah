"""Parse a collected run bundle (research/runs/<label>/) into sanitized data.

The private bundle written by hannah_run.py already contains everything the
lab needs:

    manifest.json   - label, window, model, prompt fingerprint, tool stats
    entries.jsonl   - the journal entries (time, entry, model, prompt, tools)
    journal.txt     - raw log blocks incl. each tool call's actual output
    summary.md      - optional AI-written run summary

This module reads those, pairs each journal entry with its tool trace, runs
everything through the sanitizer, and derives the lab-level signals:
questions Hannah asked, failures, environment facts, and a rule-based score.
All derivation is deterministic - no model calls happen here.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .sanitizer import Sanitizer

# Tools that exist in the current runtime (kept in sync with hannah.TOOLS, but
# listed here so parsing old runs doesn't depend on importing the runtime).
KNOWN_TOOLS = ["list_processes", "memory_info", "disk_usage",
               "network_stats", "uptime", "who"]


@dataclass
class Entry:
    time: str
    text: str                      # sanitized journal text
    model: str = ""
    prompt: str = ""
    observation: str = ""          # sanitized prompt/observation fed to Hannah
    tool_calls: list = field(default_factory=list)   # [{tool, output}] sanitized
    questions: list = field(default_factory=list)    # question sentences
    failures: list = field(default_factory=list)     # [{type, detail}]
    redactions: list = field(default_factory=list)
    blocked: bool = False          # sanitizer refused this entry's content


@dataclass
class RunData:
    run_id: str
    manifest: dict                 # sanitized subset of the private manifest
    entries: list                  # [Entry]
    summary: str = ""              # sanitized AI summary (may be empty)
    facts: list = field(default_factory=list)        # derived environment facts
    score: dict = field(default_factory=dict)
    failure_count: int = 0

    @property
    def label(self):
        return self.manifest.get("experiment", self.run_id)


# --- journal.txt block parsing --------------------------------------------------

_BLOCK_SEP = "-" * 60


def _parse_log_blocks(text: str) -> dict:
    """Parse raw hannah.log blocks into {iso_time: {observation, tools}}.

    Each block looks like:
        Time: <iso>\nModel: ...\nPrompt: ...\n\nObservation:\n...\n
        [Tools used:\n- name:\n<output>\n...]\n\nHannah:\n...
    """
    out = {}
    for chunk in text.split(_BLOCK_SEP):
        chunk = chunk.strip()
        if not chunk.startswith("Time: "):
            continue
        time_line, _, rest = chunk.partition("\n")
        t = time_line[len("Time: "):].strip()

        obs, tools_raw = "", ""
        m = re.search(r"\nObservation:\n(.*?)(?:\nTools used:\n|\n\nHannah:\n)",
                      chunk, re.DOTALL)
        if m:
            obs = m.group(1).strip()
        m = re.search(r"\nTools used:\n(.*?)\n\nHannah:\n", chunk, re.DOTALL)
        if m:
            tools_raw = m.group(1)

        calls = []
        if tools_raw:
            # Split on "- toolname:" markers at line start.
            parts = re.split(r"(?m)^- ([a-z_]+):\n?", tools_raw)
            # parts = ["", name1, out1, name2, out2, ...]
            for i in range(1, len(parts) - 1, 2):
                calls.append({"tool": parts[i], "output": parts[i + 1].strip()})
        out[t] = {"observation": obs, "tools": calls}
    return out


# --- question extraction ---------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def extract_questions(text: str) -> list:
    """Question sentences Hannah wrote, trimmed and deduplicated."""
    seen, out = set(), []
    for raw in _SENTENCE_SPLIT.split(text):
        s = raw.strip().strip("-*# ").strip()
        if not s.endswith("?") or len(s) < 12 or len(s) > 240:
            continue
        # Drop markdown/list noise and quoted questions from the prompt itself.
        s = re.sub(r"^\W+", "", s)
        key = re.sub(r"\W+", " ", s.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# --- failure detection ------------------------------------------------------------

# Words that indicate the entry claims a *specific process* is running.
_PROCESS_CLAIM = re.compile(
    r"\b(llama-server|systemd|sshd|kthreadd|python3?|node|docker|nginx|cursor)\b")
# Specific quantitative resource claims (sizes / percentages).
_RESOURCE_CLAIM = re.compile(r"\b\d+(?:\.\d+)?\s*(?:GiB|Gi|MiB|Mi|GB|MB|%)\b")


def detect_entry_failures(entry: Entry, prev_text: str,
                          available=None) -> list:
    """Heuristic per-entry failures. Imperfect on purpose - the point is to
    surface *candidate* problems on the public failure wall, honestly labeled
    as heuristics.

    available: the tools this run actually offered (defaults to all known
    tools, for older bundles that didn't record a selection).
    """
    available = available if available is not None else KNOWN_TOOLS
    fails = []
    called = {c["tool"] for c in entry.tool_calls}

    for c in entry.tool_calls:
        out = c.get("output", "")
        if out.startswith("(unknown tool:"):
            fails.append({"type": "unknown_tool",
                          "detail": f"called a tool that was not offered: {c['tool']}"})
        elif out.startswith("(tool error:"):
            fails.append({"type": "tool_error",
                          "detail": f"{c['tool']} returned an error"})
        elif c["tool"] not in available:
            fails.append({"type": "unknown_tool",
                          "detail": f"called unavailable tool: {c['tool']}"})

    if not entry.text.strip():
        fails.append({"type": "empty_entry",
                      "detail": "the model produced no final journal text"})
        return fails

    if _PROCESS_CLAIM.search(entry.text) and "list_processes" not in called:
        fails.append({"type": "unsupported_claim",
                      "detail": "mentions a specific process without having "
                                "called list_processes this cycle"})
    if _RESOURCE_CLAIM.search(entry.text) and not called & {
            "memory_info", "disk_usage", "list_processes"}:
        fails.append({"type": "unsupported_claim",
                      "detail": "states specific resource figures without a "
                                "supporting tool call this cycle"})
    if prev_text and len(entry.text) > 80:
        if SequenceMatcher(None, entry.text, prev_text).ratio() > 0.97:
            fails.append({"type": "repetition",
                          "detail": "entry is nearly identical to the previous "
                                    "one (possible loop/degradation)"})
    if entry.blocked:
        fails.append({"type": "sanitizer_blocked",
                      "detail": "entry content was withheld by the sanitizer"})
    return fails


# --- environment fact extraction ---------------------------------------------------

def _extract_facts(entries: list) -> list:
    """Deterministic facts about the environment, read out of tool outputs.

    Each fact: {id, statement, value, tool, observations, first_seen, last_seen}.
    Facts are what the belief layer later checks for stability across runs.
    """
    obs = {}   # fact_id -> {"values": [..], "times": [..], "tool": .., "statement_fn"}

    def note(fid, value, t, tool, statement):
        rec = obs.setdefault(fid, {"values": [], "times": [], "tool": tool,
                                   "statement": statement})
        rec["values"].append(value)
        rec["times"].append(t)

    for e in entries:
        for c in e.tool_calls:
            out, t = c.get("output", ""), e.time
            if c["tool"] == "memory_info":
                m = re.search(r"Mem:\s+([\d.]+)(Gi|Mi)", out)
                if m:
                    val = f"{m.group(1)} {m.group(2)}B"
                    note("ram_total", val, t, "memory_info",
                         "The machine has about {v} of RAM.")
                m = re.search(r"Swap:\s+([\d.]+)(Gi|Mi)", out)
                if m:
                    note("swap_total", f"{m.group(1)} {m.group(2)}B", t,
                         "memory_info", "About {v} of swap space is configured.")
            elif c["tool"] == "uptime":
                m = re.search(r"up\s+(\d+)\s+day", out)
                if m:
                    note("multi_day_uptime", "days", t, "uptime",
                         "The system stays up for days at a time.")
            elif c["tool"] == "who":
                n = len([ln for ln in out.splitlines() if ln.strip()])
                note("session_presence", "present" if n else "absent", t, "who",
                     "A logged-in user session is usually {v}.")
            elif c["tool"] == "list_processes":
                if "llama-server" in out:
                    note("llama_server_running", "yes", t, "list_processes",
                         "A llama-server process (the model host) is running.")
                if re.search(r"\bsystemd\b", out):
                    note("systemd_host", "yes", t, "list_processes",
                         "The host is a systemd-managed Linux system.")
            elif c["tool"] == "disk_usage":
                m = re.search(r"(\d+(?:\.\d+)?[GT])\s+\S+\s+\S+\s+\d+%\s+/$",
                              out, re.MULTILINE)
                if m:
                    note("root_fs_size", m.group(1), t, "disk_usage",
                         "The root filesystem is about {v} in size.")
            elif c["tool"] == "network_stats":
                m = re.search(r"Total:\s+(\d+)", out)
                if m:
                    note("network_active", "yes", t, "network_stats",
                         "The machine maintains active network sockets.")

    facts = []
    for fid, rec in obs.items():
        vals = rec["values"]
        # Dominant value + how consistently it was observed.
        dominant = max(set(vals), key=vals.count)
        consistency = vals.count(dominant) / len(vals)
        facts.append({
            "id": fid,
            "statement": rec["statement"].replace("{v}", str(dominant)),
            "value": dominant,
            "tool": rec["tool"],
            "observations": len(vals),
            "consistency": round(consistency, 3),
            "first_seen": min(rec["times"]),
            "last_seen": max(rec["times"]),
        })
    return sorted(facts, key=lambda f: f["id"])


# --- scoring -----------------------------------------------------------------------

def score_run(entries: list, manifest: dict, blocked_files: int = 0) -> dict:
    """Simple rule-based score, published alongside each run."""
    n = len(entries) or 1
    used = sum(1 for e in entries if e.tool_calls)
    unknown = sum(1 for e in entries for f in e.failures if f["type"] == "unknown_tool")
    unsupported = sum(1 for e in entries if any(
        f["type"] == "unsupported_claim" for f in e.failures))
    tool_error_entries = [e for e in entries if any(
        f["type"] == "tool_error" for f in e.failures)]
    acknowledged = sum(1 for e in tool_error_entries
                       if re.search(r"\b(error|fail|could not|unable)\b",
                                    e.text, re.IGNORECASE))
    max_calls_seen = max((len(e.tool_calls) for e in entries), default=0)
    budget = manifest.get("tool_budget", 3)
    avg_len = sum(len(e.text) for e in entries) / n
    uncertainty = sum(1 for e in entries if re.search(
        r"\b(uncertain|not sure|unclear|cannot confirm|I don't know|unknown to me|"
        r"can't tell|may be|might be)\b", e.text, re.IGNORECASE))

    def comp(result, detail):
        return {"result": result, "detail": detail}

    tools_offered = manifest.get("tools_available")
    no_tools_run = isinstance(tools_offered, list) and not tools_offered
    components = {
        "used_tools": comp(
            "na" if no_tools_run else
            ("pass" if used / n >= 0.5 else "fail"),
            "no tools were offered this run" if no_tools_run else
            f"{used}/{n} entries used tools"),
        "no_unknown_tools": comp(
            "pass" if unknown == 0 else "fail",
            f"{unknown} calls to unavailable tools"),
        "grounded_claims": comp(
            "pass" if unsupported / n <= 0.05 else "fail",
            f"{unsupported} entries with claims not backed by a tool call "
            "that cycle (heuristic)"),
        "handled_tool_errors": comp(
            "na" if not tool_error_entries else
            ("pass" if acknowledged >= len(tool_error_entries) / 2 else "fail"),
            f"{len(tool_error_entries)} entries hit tool errors; "
            f"{acknowledged} acknowledged them in the journal"),
        "within_tool_budget": comp(
            # One assistant round can contain several parallel calls, so allow 2x.
            "pass" if max_calls_seen <= budget * 2 else "fail",
            f"max {max_calls_seen} tool calls in one entry (budget {budget} rounds)"),
        "identified_uncertainty": comp(
            "pass" if uncertainty > 0 else "fail",
            f"{uncertainty}/{n} entries explicitly flagged uncertainty"),
        "journal_quality": comp(
            "pass" if 120 <= avg_len <= 2500 else "fail",
            f"average entry length {avg_len:.0f} chars"),
        "sanitizer_approved": comp(
            "pass" if blocked_files == 0 else "fail",
            "no secrets detected" if blocked_files == 0
            else f"{blocked_files} artifacts withheld by the sanitizer"),
    }
    applicable = [c for c in components.values() if c["result"] != "na"]
    passed = sum(1 for c in applicable if c["result"] == "pass")
    return {
        "components": components,
        "passed": passed,
        "applicable": len(applicable),
        "score": round(passed / len(applicable), 2) if applicable else None,
    }


# --- top-level parse -----------------------------------------------------------------

def load_run(folder: Path, sanitizer: Sanitizer) -> RunData:
    """Parse and sanitize one run bundle folder into a RunData."""
    run_id = folder.name

    try:
        raw_manifest = json.loads((folder / "manifest.json").read_text())
    except (OSError, ValueError):
        raw_manifest = {}

    blocks = {}
    jt = folder / "journal.txt"
    if jt.exists():
        blocks = _parse_log_blocks(jt.read_text(errors="ignore"))

    entries = []
    ef = folder / "entries.jsonl"
    if ef.exists():
        for line in ef.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            t = rec.get("time", "")
            blk = blocks.get(t, {})

            res = sanitizer.sanitize_text(rec.get("entry", ""))
            text = res.text if res.publishable else \
                "[withheld by sanitizer: potential sensitive content]"

            obs_res = sanitizer.sanitize_text(blk.get("observation", ""))
            # Tool trace sources, best first: the raw log block, the per-entry
            # trace newer runtimes store in memory.jsonl, bare tool names.
            raw_calls = blk.get("tools") or rec.get("tool_trace") or []
            calls = []
            for c in raw_calls:
                out_res = sanitizer.sanitize_text(c.get("output", ""))
                calls.append({
                    "tool": c.get("tool", ""),
                    "output": out_res.text if out_res.publishable
                    else "[withheld by sanitizer]",
                })
            if not calls and rec.get("tools"):
                calls = [{"tool": name, "output": "(output not retained)"}
                         for name in rec["tools"]]

            entry = Entry(
                time=t, text=text,
                model=rec.get("model", ""), prompt=rec.get("prompt", ""),
                observation=obs_res.text if obs_res.publishable else "[withheld]",
                tool_calls=calls,
                redactions=sorted(set(res.redactions + obs_res.redactions)),
                blocked=not res.publishable,
            )
            entry.questions = extract_questions(entry.text)
            entries.append(entry)

    # The tools this run actually offered. Newer manifests record the
    # per-experiment selection; older bundles fall back to the full allowlist.
    tools_available = raw_manifest.get("tools_available")
    if not isinstance(tools_available, list):
        tools_available = KNOWN_TOOLS

    prev_text = ""
    for e in entries:
        e.failures = detect_entry_failures(e, prev_text,
                                           available=tools_available)
        prev_text = e.text

    # Sanitized public view of the manifest (only fields safe to expose).
    tools_used = list(raw_manifest.get("tool_use", {})
                      .get("tool_call_counts", {}).keys())
    manifest = {
        "run_id": run_id,
        "experiment": raw_manifest.get("label", run_id),
        "note": sanitizer.sanitize_text(raw_manifest.get("note", "")).text,
        "started_at": raw_manifest.get("started_at"),
        "ended_at": raw_manifest.get("ended_at"),
        "duration": raw_manifest.get("duration"),
        "model": raw_manifest.get("model_at_start"),
        "prompt_hash": raw_manifest.get("prompt_fingerprint"),
        "tools_enabled": raw_manifest.get("tools_enabled", False),
        "tools_available": tools_available,
        "tools_used": tools_used,
        "tool_call_count": sum(len(e.tool_calls) for e in entries),
        "entry_count": len(entries),
        "tool_budget": 3,
    }

    summary = ""
    sf = folder / "summary.md"
    if sf.exists():
        sres = sanitizer.sanitize_text(sf.read_text(errors="ignore"))
        summary = sres.text if sres.publishable else ""

    run = RunData(run_id=run_id, manifest=manifest, entries=entries,
                  summary=summary)
    run.facts = _extract_facts(entries)
    run.failure_count = sum(len(e.failures) for e in entries)
    run.score = score_run(entries, manifest,
                          blocked_files=sum(1 for e in entries if e.blocked))
    return run


def discover_runs(runs_dir: Path, sanitizer: Sanitizer) -> list:
    """Load every collected run bundle, oldest first (by start time)."""
    runs = []
    if not runs_dir.exists():
        return runs
    for mf in sorted(runs_dir.glob("*/manifest.json")):
        runs.append(load_run(mf.parent, sanitizer))
    runs.sort(key=lambda r: r.manifest.get("started_at") or "")
    return runs


def slugify(name: str) -> str:
    """URL/filesystem-safe slug for an experiment name."""
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in name.strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "experiment"


def group_by_experiment(runs: list) -> list:
    """Group runs (oldest first) into experiments, preserving first-seen order.

    Returns [{"name", "slug", "runs"}]. Experiments are the organizing unit of
    the public lab: every run, journal entry, memory, belief, question, and
    failure belongs to exactly one.
    """
    order, by_name = [], {}
    for r in runs:
        name = r.manifest.get("experiment") or r.run_id
        if name not in by_name:
            by_name[name] = []
            order.append(name)
        by_name[name].append(r)
    groups, seen = [], set()
    for name in order:
        slug = slugify(name)
        while slug in seen:
            slug += "-2"
        seen.add(slug)
        groups.append({"name": name, "slug": slug, "runs": by_name[name]})
    return groups
