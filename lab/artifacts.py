"""Write per-run public-safe artifact files.

Artifacts live inside each private run bundle, in a `public/` subfolder:

    research/runs/<label>/public/
        public_manifest.json        - index of everything below
        journal.md                  - sanitized journal entries
        tool_trace.public.json      - per-entry sanitized tool calls
        memory_changes.public.json  - memories created/updated by this run
        belief_changes.public.json  - beliefs created/changed by this run
        questions.public.json       - questions this run opened
        score.json                  - rule-based score
        failures.json               - detected failures
        run_summary.json            - compact stats for the runs table

Only content that already passed the sanitizer reaches this module. The
manifest carries `publishable`; when the sanitizer withheld pieces of the
run, that is reflected here rather than hidden.
"""

import json
from datetime import datetime
from pathlib import Path


def _write(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def journal_markdown(run) -> str:
    m = run.manifest
    lines = [
        f"# Journal — {m.get('experiment')}",
        "",
        f"Run `{run.run_id}` · model `{m.get('model')}` · "
        f"prompt `{m.get('prompt_hash')}` · {m.get('entry_count')} entries",
        "",
    ]
    for e in run.entries:
        lines.append(f"## {e.time}")
        if e.tool_calls:
            lines.append("")
            lines.append("*Tools: " + ", ".join(
                f"`{c['tool']}`" for c in e.tool_calls) + "*")
        lines.append("")
        lines.append(e.text.strip())
        lines.append("")
    return "\n".join(lines)


def write_run_artifacts(run, diff: dict, state, out_dir: Path) -> dict:
    """Write all public artifacts for one run; returns the public manifest."""
    out_dir.mkdir(parents=True, exist_ok=True)
    m = run.manifest

    (out_dir / "journal.md").write_text(journal_markdown(run), encoding="utf-8")

    _write(out_dir / "tool_trace.public.json", [
        {"time": e.time, "tools": [c["tool"] for c in e.tool_calls],
         "tool_trace": e.tool_calls}
        for e in run.entries
    ])

    mem_by_id = {x["id"]: x for x in state.memories}
    _write(out_dir / "memory_changes.public.json", {
        "new": [mem_by_id[i] for i in diff.get("new_memories", []) if i in mem_by_id],
        "updated": [mem_by_id[i] for i in diff.get("updated_memories", [])
                    if i in mem_by_id],
    })

    bel_by_id = {b["id"]: b for b in state.beliefs}
    _write(out_dir / "belief_changes.public.json", {
        "new": [bel_by_id[i] for i in diff.get("new_beliefs", []) if i in bel_by_id],
        "confidence_changed": [bel_by_id[i] for i in
                               diff.get("belief_confidence_changes", [])
                               if i in bel_by_id],
        "contradictions": diff.get("new_contradictions", []),
    })

    _write(out_dir / "questions.public.json",
           [q for q in state.questions if run.run_id in q.get("runs", [])])

    _write(out_dir / "score.json", run.score)

    failures = [{"time": e.time, **f}
                for e in run.entries for f in e.failures]
    _write(out_dir / "failures.json", failures)

    summary = {
        "run_id": run.run_id,
        "experiment": m.get("experiment"),
        "started_at": m.get("started_at"),
        "ended_at": m.get("ended_at"),
        "duration": m.get("duration"),
        "model": m.get("model"),
        "prompt_hash": m.get("prompt_hash"),
        "entry_count": m.get("entry_count"),
        "tool_call_count": m.get("tool_call_count"),
        "memory_changes": len(diff.get("new_memories", []))
        + len(diff.get("updated_memories", [])),
        "belief_changes": len(diff.get("new_beliefs", []))
        + len(diff.get("belief_confidence_changes", [])),
        "questions_opened": len(diff.get("new_questions", [])),
        "failure_count": run.failure_count,
        "score": run.score.get("score"),
    }
    _write(out_dir / "run_summary.json", summary)

    blocked_entries = sum(1 for e in run.entries if e.blocked)
    manifest = {
        **summary,
        "note": m.get("note", ""),
        "tools_enabled": m.get("tools_enabled"),
        "tools_available": m.get("tools_available"),
        "tools_used": m.get("tools_used"),
        "journal_path": "journal.md",
        "tool_trace_path": "tool_trace.public.json",
        "memory_changes_path": "memory_changes.public.json",
        "belief_changes_path": "belief_changes.public.json",
        "questions_path": "questions.public.json",
        "score_path": "score.json",
        "failures_path": "failures.json",
        "publishable": blocked_entries == 0,
        "withheld_entries": blocked_entries,
        "sanitized_summary": (run.summary[:400] + "…") if len(run.summary) > 400
        else run.summary,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write(out_dir / "public_manifest.json", manifest)
    return manifest
