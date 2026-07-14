"""Derived lab state: memories, beliefs, questions, timeline, what-changed.

Runs are folded oldest-to-newest into a persistent picture of what Hannah
has observed, what she appears to believe about her environment, and what
she is still asking. Everything here is *derived deterministically* from the
sanitized run data - the model is never consulted - so the public site can be
rebuilt from scratch at any time and the same state falls out.

A "belief" in this layer is an environment fact observed via tools, weighted
by how many runs support it and how consistent the observed value was.
Contradictions (the same fact observed with different dominant values in
different runs) are kept and displayed, not smoothed over.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime

CONFIDENCE_LEVELS = ["low", "medium", "high"]


def _level(x: float) -> str:
    if x >= 0.75:
        return "high"
    if x >= 0.45:
        return "medium"
    return "low"


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


@dataclass
class LabState:
    memories: list = field(default_factory=list)
    beliefs: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    timeline: list = field(default_factory=list)      # newest last
    changes_by_run: dict = field(default_factory=dict)  # run_id -> diff dict
    contradictions: list = field(default_factory=list)


def _event(t, kind, title, run_id, detail=""):
    return {"time": t or "", "kind": kind, "title": title,
            "run_id": run_id, "detail": detail}


# --- per-run derivations ---------------------------------------------------------

def _run_observation_memories(run) -> list:
    """A few aggregate observation/pattern memories per run."""
    mems = []
    m = run.manifest
    counts = {}
    for e in run.entries:
        for c in e.tool_calls:
            counts[c["tool"]] = counts.get(c["tool"], 0) + 1
    if counts:
        top = max(counts, key=counts.get)
        mems.append({
            "id": f"mem_obs_{run.run_id}_tools",
            "type": "observation",
            "content": (f"During '{m.get('experiment')}' I made "
                        f"{sum(counts.values())} tool calls across "
                        f"{m.get('entry_count')} entries; I reached for "
                        f"{top} most often ({counts[top]} times)."),
            "created": m.get("started_at"),
            "updated": m.get("ended_at"),
            "source_run": run.run_id,
            "confidence": "high",
            "tags": ["tool-use", m.get("experiment", "")],
        })
    unsupported = sum(1 for e in run.entries if any(
        f["type"] == "unsupported_claim" for f in e.failures))
    if unsupported:
        mems.append({
            "id": f"mem_fail_{run.run_id}_claims",
            "type": "failed_assumption",
            "content": (f"In {unsupported} of {len(run.entries)} entries I stated "
                        "specifics I had not verified with a tool that cycle. "
                        "Claims need a same-cycle observation behind them."),
            "created": m.get("ended_at"),
            "source_run": run.run_id,
            "confidence": "medium",
            "tags": ["grounding", "failure"],
        })
    reps = sum(1 for e in run.entries if any(
        f["type"] == "repetition" for f in e.failures))
    if reps >= 3:
        mems.append({
            "id": f"mem_pattern_{run.run_id}_repetition",
            "type": "pattern",
            "content": (f"I repeated myself nearly verbatim {reps} times in this "
                        "run - without new observations my entries collapse "
                        "into loops."),
            "created": m.get("ended_at"),
            "source_run": run.run_id,
            "confidence": "medium",
            "tags": ["degradation", "pattern"],
        })
    if run.summary:
        first_para = run.summary.split("\n\n", 2)
        excerpt = next((p.strip() for p in first_para
                        if p.strip() and not p.strip().startswith("#")), "")
        if excerpt:
            mems.append({
                "id": f"mem_note_{run.run_id}",
                "type": "experiment_note",
                "content": excerpt[:600],
                "created": m.get("ended_at"),
                "source_run": run.run_id,
                "confidence": "medium",
                "tags": ["summary", m.get("experiment", "")],
            })
    # One representative reflection: the longest entry that used no tools and
    # triggered no failures (pure introspection, safely publishable).
    quiet = [e for e in run.entries if not e.tool_calls and not e.failures
             and not e.blocked and len(e.text) > 200]
    if quiet:
        pick = max(quiet, key=lambda e: len(e.text))
        mems.append({
            "id": f"mem_refl_{run.run_id}",
            "type": "reflection",
            "content": pick.text[:700],
            "created": pick.time,
            "source_run": run.run_id,
            "confidence": "low",
            "tags": ["reflection"],
        })
    return mems


def _fold_questions(state_q: dict, run, latest_run_id: str):
    """Merge this run's questions into the accumulated question map."""
    per_run_seen = set()
    for e in run.entries:
        for q in e.questions:
            key = _norm(q)
            if len(key) < 10:
                continue
            if key in state_q:
                rec = state_q[key]
                rec["times_asked"] += 0 if key in per_run_seen else 1
                rec["last_asked"] = e.time
                if run.run_id not in rec["runs"]:
                    rec["runs"].append(run.run_id)
            else:
                state_q[key] = {
                    "id": f"q_{len(state_q) + 1:04d}",
                    "text": q,
                    "created": e.time,
                    "last_asked": e.time,
                    "source_run": run.run_id,
                    "runs": [run.run_id],
                    "times_asked": 1,
                    "experiment": run.manifest.get("experiment", ""),
                }
            per_run_seen.add(key)


def _question_status(rec, latest_run_id: str) -> str:
    if latest_run_id in rec["runs"]:
        return "investigating" if len(rec["runs"]) > 1 else "open"
    if len(rec["runs"]) > 1:
        return "investigating"
    return "abandoned"


def _suggest_next(rec) -> str:
    t = rec["text"].lower()
    if any(w in t for w in ("process", "running", "program")):
        return "list_processes"
    if any(w in t for w in ("memory", "ram", "swap")):
        return "memory_info"
    if any(w in t for w in ("disk", "storage", "filesystem", "space")):
        return "disk_usage"
    if any(w in t for w in ("network", "connect", "socket")):
        return "network_stats"
    if any(w in t for w in ("who", "user", "present", "logged", "someone", "anyone")):
        return "who"
    if any(w in t for w in ("time", "long", "uptime", "since")):
        return "uptime"
    return ""


# --- the fold ---------------------------------------------------------------------

def build_state(runs: list) -> LabState:
    """Fold sanitized runs (oldest first) into the derived lab state."""
    state = LabState()
    fact_beliefs = {}    # fact_id -> belief record (evolving)
    questions = {}       # normalized text -> question record
    prev_snapshot = None
    latest_run_id = runs[-1].run_id if runs else None

    for idx, run in enumerate(runs):
        m = run.manifest
        rid = run.run_id
        started, ended = m.get("started_at"), m.get("ended_at")
        new_mem, upd_mem = [], []
        new_bel, bel_changes = [], []
        new_contra = []

        state.timeline.append(_event(
            started, "experiment", f"Experiment '{m.get('experiment')}' started",
            rid, m.get("note", "")))

        # -- environment facts -> environment_fact memories + beliefs -------------
        for fact in run.facts:
            mem_id = f"mem_fact_{fact['id']}"
            existing = next((x for x in state.memories if x["id"] == mem_id), None)
            if existing:
                existing["updated"] = fact["last_seen"]
                existing["source_runs"].append(rid)
                upd_mem.append(mem_id)
            else:
                rec = {
                    "id": mem_id,
                    "type": "environment_fact",
                    "content": fact["statement"],
                    "created": fact["first_seen"],
                    "updated": fact["last_seen"],
                    "source_run": rid,
                    "source_runs": [rid],
                    "confidence": _level(fact["consistency"]),
                    "tags": ["environment", fact["tool"]],
                }
                state.memories.append(rec)
                new_mem.append(mem_id)
                state.timeline.append(_event(
                    fact["first_seen"], "memory_created",
                    f"Memory: {fact['statement']}", rid))

            # Belief bookkeeping.
            b = fact_beliefs.get(fact["id"])
            if b is None:
                conf = min(0.4, 0.2 + 0.2 * fact["consistency"])
                b = {
                    "id": f"belief_{fact['id']}",
                    "statement": fact["statement"],
                    "value": fact["value"],
                    "confidence_value": conf,
                    "confidence": _level(conf),
                    "evidence_runs": [rid],
                    "observations": fact["observations"],
                    "tool": fact["tool"],
                    "created": fact["first_seen"],
                    "last_updated": fact["last_seen"],
                    "trend": "new",
                    "history": [{"run": rid, "confidence": _level(conf),
                                 "value": fact["value"]}],
                    "contradictions": [],
                }
                fact_beliefs[fact["id"]] = b
                new_bel.append(b["id"])
                state.timeline.append(_event(
                    fact["first_seen"], "belief_created",
                    f"Belief formed: {fact['statement']}", rid,
                    f"confidence {b['confidence']}"))
            else:
                prev_level = b["confidence"]
                if fact["value"] == b["value"]:
                    b["confidence_value"] = min(
                        0.95, b["confidence_value"] + 0.2 * fact["consistency"])
                else:
                    # Contradiction: same fact, different dominant value.
                    contra = {
                        "belief_id": b["id"],
                        "statement": b["statement"],
                        "previous_value": b["value"],
                        "new_value": fact["value"],
                        "run_id": rid,
                        "time": fact["first_seen"],
                    }
                    b["contradictions"].append(contra)
                    state.contradictions.append(contra)
                    new_contra.append(contra)
                    b["confidence_value"] = max(0.15, b["confidence_value"] - 0.3)
                    b["value"] = fact["value"]
                    b["statement"] = fact["statement"]
                    state.timeline.append(_event(
                        fact["first_seen"], "contradiction",
                        f"Contradiction: {b['statement']}", rid,
                        f"previously observed {contra['previous_value']}, "
                        f"now {contra['new_value']}"))
                b["confidence"] = _level(b["confidence_value"])
                b["observations"] += fact["observations"]
                b["evidence_runs"].append(rid)
                b["last_updated"] = fact["last_seen"]
                b["trend"] = ("up" if b["confidence_value"] > 0.0 and
                              CONFIDENCE_LEVELS.index(b["confidence"]) >
                              CONFIDENCE_LEVELS.index(prev_level)
                              else "down" if CONFIDENCE_LEVELS.index(b["confidence"]) <
                              CONFIDENCE_LEVELS.index(prev_level) else "steady")
                b["history"].append({"run": rid, "confidence": b["confidence"],
                                     "value": fact["value"]})
                if b["trend"] != "steady":
                    bel_changes.append(b["id"])
                    state.timeline.append(_event(
                        fact["last_seen"], "belief_changed",
                        f"Belief confidence {b['trend']}: {b['statement']}", rid,
                        f"{prev_level} → {b['confidence']}"))

        # -- meta-belief: does the environment persist across runs? ---------------
        if idx >= 1:
            stable = [b for b in fact_beliefs.values()
                      if len(set(b["evidence_runs"])) >= 2 and not b["contradictions"]]
            meta = fact_beliefs.get("_persistence")
            if stable:
                conf = min(0.9, 0.3 + 0.1 * len(stable))
                if meta is None:
                    meta = {
                        "id": "belief__persistence",
                        "statement": ("The local environment appears to persist "
                                      "across runs."),
                        "value": "persists",
                        "confidence_value": conf,
                        "confidence": _level(conf),
                        "evidence_runs": sorted({r for b in stable
                                                 for r in b["evidence_runs"]}),
                        "observations": len(stable),
                        "tool": "(cross-run comparison)",
                        "created": started,
                        "last_updated": ended,
                        "trend": "new",
                        "history": [{"run": rid, "confidence": _level(conf),
                                     "value": "persists"}],
                        "contradictions": [],
                        "detail": (f"{len(stable)} environment facts observed "
                                   "consistently in two or more runs"),
                    }
                    fact_beliefs["_persistence"] = meta
                    new_bel.append(meta["id"])
                    state.timeline.append(_event(
                        ended, "belief_created",
                        "Belief formed: the environment persists across runs",
                        rid, meta["detail"]))
                else:
                    prev_level = meta["confidence"]
                    meta["confidence_value"] = conf
                    meta["confidence"] = _level(conf)
                    meta["last_updated"] = ended
                    meta["observations"] = len(stable)
                    meta["detail"] = (f"{len(stable)} environment facts observed "
                                      "consistently in two or more runs")
                    meta["evidence_runs"] = sorted({r for b in stable
                                                    for r in b["evidence_runs"]})
                    meta["trend"] = ("up" if CONFIDENCE_LEVELS.index(meta["confidence"]) >
                                     CONFIDENCE_LEVELS.index(prev_level)
                                     else "down" if CONFIDENCE_LEVELS.index(meta["confidence"]) <
                                     CONFIDENCE_LEVELS.index(prev_level) else "steady")
                    meta["history"].append({"run": rid, "confidence": meta["confidence"],
                                            "value": "persists"})
                    if meta["trend"] != "steady":
                        bel_changes.append(meta["id"])

        # -- aggregate observation / pattern / note / reflection memories ---------
        for mem in _run_observation_memories(run):
            mem.setdefault("source_runs", [mem["source_run"]])
            state.memories.append(mem)
            new_mem.append(mem["id"])
            state.timeline.append(_event(
                mem.get("created"), "memory_created",
                f"Memory ({mem['type']}): {mem['content'][:90]}"
                + ("…" if len(mem["content"]) > 90 else ""), rid))

        # -- questions -------------------------------------------------------------
        before_q = set(questions)
        _fold_questions(questions, run, latest_run_id)
        opened = [questions[k] for k in questions if k not in before_q]
        for q in opened[:12]:
            state.timeline.append(_event(
                q["created"], "question_opened",
                f"Question: {q['text']}", rid))

        # -- failures ----------------------------------------------------------------
        if run.failure_count:
            by_type = {}
            for e in run.entries:
                for f in e.failures:
                    by_type[f["type"]] = by_type.get(f["type"], 0) + 1
            state.timeline.append(_event(
                ended, "failures",
                f"{run.failure_count} failures recorded", rid,
                ", ".join(f"{k}×{v}" for k, v in sorted(by_type.items()))))

        # -- what changed vs the previous run ---------------------------------------
        snapshot = {
            "memories": {x["id"] for x in state.memories},
            "beliefs": {b["id"]: b["confidence"] for b in fact_beliefs.values()},
            "questions": set(questions),
            "tools": set(m.get("tools_used") or []),
        }
        diff = {
            "run_id": rid,
            "experiment": m.get("experiment"),
            "new_memories": new_mem,
            "updated_memories": sorted(set(upd_mem)),
            "new_beliefs": new_bel,
            "belief_confidence_changes": sorted(set(bel_changes)),
            "new_questions": [q["text"] for q in opened][:15],
            "new_failures": run.failure_count,
            "new_contradictions": [c["statement"] for c in new_contra],
            "tool_use_delta": {},
        }
        if prev_snapshot:
            gained = snapshot["tools"] - prev_snapshot["tools"]
            lost = prev_snapshot["tools"] - snapshot["tools"]
            if gained:
                diff["tool_use_delta"]["started_using"] = sorted(gained)
            if lost:
                diff["tool_use_delta"]["stopped_using"] = sorted(lost)
        state.changes_by_run[rid] = diff
        prev_snapshot = snapshot

    # Finalize questions.
    for rec in questions.values():
        rec["status"] = _question_status(rec, latest_run_id)
        rec["suggested_tool"] = _suggest_next(rec)
    state.questions = sorted(questions.values(),
                             key=lambda q: (q["last_asked"] or ""), reverse=True)
    state.beliefs = sorted(fact_beliefs.values(),
                           key=lambda b: -b["confidence_value"])
    state.memories.sort(key=lambda x: x.get("updated") or x.get("created") or "",
                        reverse=True)
    state.timeline.sort(key=lambda ev: ev["time"] or "")
    return state
