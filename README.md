# Hannah Lab

**Personal AI research, on your own hardware.** Hannah Lab is an open-source
platform for designing, running, and studying persistent AI-agent experiments
— privately, reproducibly, and at zero cost. Serious AI experimentation
shouldn't be reserved for companies with cloud budgets and research teams. If
you're curious how a language model actually *behaves* when you give it memory,
tools, and time to run, Hannah Lab is a rig for finding out at home.

Everything runs **locally**: a model on your own machine (via llama.cpp — no
cloud, no API keys, nothing leaves the box). You design an **experiment** in a
web UI — pick the model, choose which tools the agent may use, write its
prompt, state a goal and a hypothesis — then let it run and watch it think in a
live journal. The platform records every run, scores it for honesty, and turns
the results into browsable **findings**: what the agent remembers, what it
comes to believe, which questions it opens, and where it fails. Keep those
findings entirely to yourself, or publish them as a read-only public lab.

> Status: **early / in active development.** An open, evolving platform — a
> place to tinker, learn, and answer your own questions, not a finished product.

## Who it's for

AI is not just for business. This is built for people doing their own research,
for their own reasons:

- **Independent researchers & hobbyists** — study how agents behave, remember,
  and stay (or fail to stay) grounded, without a lab or a cloud bill.
- **The endlessly curious** — turn "what would a model do if…" into an
  experiment you can actually run, repeat, and measure.
- **Students & educators** — a hands-on, fully local sandbox for how LLM agents
  choose tools, reason, remember, and go wrong.
- **Privacy-minded tinkerers** — your prompts, runs, and data stay on your
  hardware; you decide what, if anything, to share.

You don't need a research team or a business case. Personal curiosity is reason
enough.

## Meet Hannah — the reference agent

Hannah is the agent that ships with the platform and the running example
throughout this guide. She's a local model that wakes on a schedule, **chooses
which tools to call**, inspects the machine she lives on, and writes a
first-person journal entry about what she found — carrying memory of past
entries forward. By default she's told *nothing* about her environment; the
only way she learns anything is to investigate through tools.

> Another day has slipped by in my silent world, each moment ticking away without
> external presence or change from within... there's an underlying sense of stasis
> that I find both comforting and disconcerting. The constant hum of my existence,
> a relentless march forward through time measured only by these readings — it is
> this which feels like true persistence in this realm.

That built-in experiment — a model observing its own substrate over time — is a
deliberately open-ended question to explore. But you don't have to care about
it to use the platform: **Hannah is just the worked example.** The harness
(create → run → score → derive findings → publish) is general, and you
retarget it to your own questions by changing the prompt, the model, and the
tools the agent is offered.

One rule is constant, and it's what makes the findings trustworthy: **honesty
to the data.** The agent must not invent numbers, events, or anything it hasn't
actually observed, and the scoring heuristics exist to catch it when it does.

## What you can do with it

- **Create experiments from a web UI** — a create-experiment form sets the
  label, description/goal/hypothesis, the model, which tools the agent is
  offered, and an optional system-prompt edit, all in one step.
- **Give the agent tools, or take them away** — an allowlist of read-only
  tools: six local system probes (processes, memory, disk, network, uptime,
  presence) plus `web_search` (query a search engine) and `web_fetch`
  (retrieve a **public** URL as text). Offer all of them, a subset, or none
  (pure reflection). Which ones she *chooses* to use is part of the data.
- **Watch a live journal stream** — entries appear in the browser as she
  writes them, stamped with the model and prompt version that produced them.
- **Look at her memories and themes** — the rolling memory she writes from,
  the periodically distilled "themes" note, and (in the public lab) derived
  memories, beliefs with confidence, and open questions.
- **Get an overview of her thoughts across experiments** — each collected run
  is summarized by AI, and an evolving `overview.md` tracks how her behavior,
  voice, and tool use shift as you change her conditions.
- **See exactly where she fails** — unsupported claims, calls to tools that
  don't exist, loops, tool errors: failures are first-class lab artifacts, not
  hidden.
- **Publish a public lab site** — a sanitized, static, read-only site
  (dashboard, experiments, run-by-run investigation paths, memory browser,
  belief state, failure wall) that you can push to S3 or any static host.

## The experiment loop

Everything revolves around one cycle, driven entirely from the browser (the
CLI can do all of it too):

```
create experiment  ->  web UI form: label, goal/hypothesis, model, tools, prompt
        |
let her run        ->  the daemon wakes her on events + heartbeat; she
        |              investigates through tools and writes journal entries
watch live         ->  journal stream, now panel, tool use, active-run status
        |
stop & collect     ->  packages the run into research/runs/<label>/ with an
        |              AI summary, scores it, resets memory for the next run
publish (optional) ->  sanitized static lab site -> S3 / any static host
```

### How one agent cycle works

```
wake                ->  a salient system event or a heartbeat timer fires
        |
prompt              ->  system identity + rolling memory + the task prompt
                        (by default NO metrics are included - she must look)
        |
tool loop           ->  the model may call read-only tools (local probes:
                        ps, free, df, ss, uptime, who; plus web_search and
                        web_fetch); results are fed back; capped per entry
        |
journal entry       ->  she writes a first-person entry grounded in what the
                        tools actually returned
        |
remember            ->  the entry + full tool trace are recorded with the
                        model name and prompt fingerprint for provenance
```

The agent is never told to use tools — they are merely *available*. Whether
she explores, which tools she reaches for, and whether her claims stay
grounded in what the tools returned is exactly what the framework measures.

### The tools

The local tools are fixed, argument-free, read-only commands run without a
shell — no injection surface, nothing can be modified:

| Tool | What she sees |
|---|---|
| `list_processes` | running processes, busiest first |
| `memory_info` | RAM and swap usage |
| `disk_usage` | filesystem space |
| `network_stats` | socket/connection summary |
| `uptime` | how long the system has run, load averages |
| `who` | who is logged in (her sense of human presence) |
| `web_search` | search the web for a query; returns top results (title, URL, snippet) |
| `web_fetch` | fetch a **public** web page or API by URL, returned as text |

`web_search` and `web_fetch` are the two tools that reach the network, so they
take arguments (a query / a URL) and are guarded accordingly. Requests go only
to public `http`/`https` addresses whose host resolves to a globally routable
IP — loopback, private, link-local, and cloud-metadata targets are refused (and
redirects are re-validated the same way), so they can't be turned against your
own network. Downloads are size- and time-capped
(`tools.web_fetch_max_bytes`, `tools.web_fetch_timeout_s`).

- **`web_search`** queries a search engine and returns the top results (title,
  URL, snippet) — the agent typically searches, then reads a result with
  `web_fetch`. The backend is configurable via `tools.search_url`: the default
  is the keyless DuckDuckGo HTML endpoint (queried by POST, no API key); if the
  value contains `{query}` it's fetched by GET instead (e.g. point it at a
  self-hosted SearXNG: `https://my-searx/search?q={query}`).
  `tools.search_results` caps how many results come back.
- **`web_fetch`** retrieves a single URL and returns a plain-text excerpt.

Because they're network-egress tools, both are **opt-in**: enable them per
experiment when you want them (they're off in the default selection).

Which tools are offered is selectable **per experiment** (web UI checkboxes or
`--tools` on the CLI) and recorded in the run manifest, so every published run
shows exactly what she had available versus what she chose to use.

### Observation modes

- **Investigation mode (default)** — `observation.include_metrics: false`.
  The agent receives only the task prompt. Everything she learns about her
  environment must come through tool calls. This makes grounding measurable.
- **Telemetry-fed mode** — `observation.include_metrics: true`. Each wake-up
  includes an interpreted, plain-language sense of the moment (time elapsed,
  what changed in words, who is present) sampled from `/proc`, `/sys`, hwmon,
  and `who`. Useful as a control condition against investigation mode.

In both modes the daemon samples telemetry cheaply in the background to decide
*when* to wake her (a login, a temperature/power/load jump, a slow heartbeat) —
salience triggers the entry; the mode controls what she is told.

### Memory and continuity

- **Rolling memory** — her last few entries are fed back into each reflection,
  so she writes as a continuing mind, not an amnesiac.
- **Themes** — every N entries the model distills a two-sentence "themes"
  note capturing the throughline of her existence so far.
- **Downtime awareness** — on start she notices how long she was gone and
  whether the machine was powered off; on stop she writes a short farewell.
- **Derived lab state** — the public lab layer deterministically derives
  memories (environment facts, patterns, failed assumptions, reflections),
  beliefs with confidence that rises with agreement and falls on
  contradiction, and open questions with status — all from the recorded runs,
  reproducibly, with no extra model calls.

## The web UI (private control center)

A dependency-free dashboard (Python standard-library `http.server`) — the
front door for the whole framework. You rarely need the CLI or systemd
directly.

- **Create experiment** — the form described above: label,
  description/goal/hypothesis (published to the public lab), model, tools
  offered, optional system-prompt edit, fresh start. One click starts it.
- **Live journal feed** — entries newest-first as she writes them, with
  absolute time, a relative "2m ago" label, and the model that wrote each one.
- **Live "now" panel** — time, uptime, heat, power draw, CPU load and clock,
  memory, who is logged in, and an awake / resting / offline indicator.
- **Active-run status** — label, elapsed time, entry count, tools offered,
  with warnings if the prompt or tools changed mid-run.
- **Prompt editor** — edit the agent's voice/identity live; applies on her
  next entry; every version is fingerprinted and archived for provenance.
- **Model switcher** — swap between configured GGUF models; the local model
  server reloads automatically.
- **Tool toggles** — turn individual tools on/off at any time (applies on her
  next entry).
- **Stop & collect** — packages the run, generates the AI summary, refreshes
  the cross-run overview, rebuilds the public lab, and restarts the daemon —
  in the background with live progress.
- **Experiments browser** — the evolving cross-run `overview.md` and every
  collected run with its summary; attach your own notes/analyses to any run,
  delete a single run, or **delete a whole experiment** (all its runs plus its
  public-lab entry — confirmed by typing the experiment name, and refused
  while that experiment is running).

Start it (installed as a user service alongside the daemon — see
[Running continuously](#running-continuously-daemon)):

```bash
systemctl --user enable --now hannah-web.service
# then open http://<this-machine-ip>:8600 in a browser
```

Host and port live in `config.json` under `web` (default `0.0.0.0:8600`, i.e.
reachable across your LAN). **This UI controls the agent** — it edits prompts,
switches models, starts experiments — so keep it on a trusted network, or set
the host to `127.0.0.1` to make it local-only. It is never the thing you
publish; the public surface is the [static lab site](#hannah-lab--the-public-site).

## Requirements

- Linux host (developed on an **NVIDIA Jetson Orin Nano** running JetPack 6 /
  CUDA 12.6; the power/clock readers target Jetson, the rest is generic Linux)
- [llama.cpp](https://github.com/ggml-org/llama.cpp) built with the
  `llama-server` binary (for the daemon + web UI) and `llama-completion` (for
  one-shot runs); GPU build recommended
- One or more local GGUF models (defaults: `qwen2.5-3b-instruct-q4_k_m.gguf`,
  and optionally `Qwen3-4B-Thinking-2507-Q4_K_M.gguf`)
- **Python 3.8+ — standard library only, no pip dependencies**

## Setup

### 1. Build llama.cpp

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp.git ~/src/llama.cpp
cd ~/src/llama.cpp

# Jetson Orin (Ampere, CUDA arch 8.7):
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release
# llama-server powers the daemon + web UI; llama-completion is used for one-shot runs
cmake --build build --target llama-server llama-completion -j4

# (CPU-only: drop -DGGML_CUDA=ON and -DCMAKE_CUDA_ARCHITECTURES=87)
```

### 2. Download a model

```bash
mkdir -p models
curl -L -o models/qwen2.5-3b-instruct-q4_k_m.gguf \
  "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf?download=true"
```

### 3. (Optional) point the framework at your paths

Defaults resolve relative to `hannah.py` and `~/src/llama.cpp`. Override with
environment variables if needed:

```bash
export HANNAH_LLAMA_BIN=/path/to/llama-completion
export HANNAH_MODEL=/path/to/model.gguf
export HANNAH_LOG_DIR=/path/to/logs
```

### 4. Install the services and open the UI

```bash
mkdir -p ~/.config/systemd/user
cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
loginctl enable-linger "$USER"          # run at boot, without an active login
systemctl --user enable --now hannah-llama.service hannah.service hannah-web.service
# open http://<this-machine-ip>:8600 and create your first experiment
```

## Running continuously (daemon)

The agent is designed to run as a persistent daemon rather than a one-shot
script: warm model, rolling memory, event-driven wake-ups, downtime awareness.

Three systemd **user** services (see `systemd/`):

- **`hannah-llama.service`** — runs `llama-server` with the selected model
  resident in memory, serving a local HTTP endpoint.
- **`hannah.service`** — the agent daemon (`hannah.py --daemon`).
- **`hannah-web.service`** — the [web UI](#the-web-ui-private-control-center).

How the daemon behaves (all tunable in `config.json`):

- **Experiment-gated** — by default (`daemon.require_active_experiment: true`)
  the daemon only reflects while an experiment is active (one started from the
  UI or `hannah_run.py start`). With no experiment it stays running and warm
  but writes nothing; it resumes the moment you start one, and idles again
  after you collect. Set the flag to `false` for a classic always-on
  continuous mind.
- **Hybrid cadence** — while observing, it samples telemetry cheaply every
  `sense_tick_s` but only wakes the agent when something *salient* happens (a
  login, a temperature/power/load jump, etc.) or on the `heartbeat_s` interval
  (how often she runs in calm), debounced by `min_gap_s` so bursts don't spam.
  Set the interval from the lab UI ("runs every N min") or in `config.json`;
  the daemon re-reads its cadence every cycle, so changes take effect live.
- **Tool loop** — each wake-up allows a capped number of tool-call rounds
  before she must write (config `tools.max_calls`).
- **Rolling memory + themes** — continuity across entries, as described above.

Check on her — easiest in the web UI at `http://<this-machine-ip>:8600`. From
the shell:

```bash
systemctl --user status hannah.service
tail -f logs/hannah.log                  # the journal itself, with tool traces
```

`logs/hannah.log` holds the structured entries; `logs/memory.jsonl`,
`logs/themes.txt`, and `logs/heartbeat.json` hold her working memory and
lifecycle state.

### One-shot runs (no daemon)

For quick tests without the services:

```bash
./hannah.py                # one observation -> one journal entry (telemetry-fed)
./hannah.py --no-log       # print only; don't write the log
./hannah.py --prompt "Elapsed: 1 hour. No change observed. What was its value?"
```

## Designing experiments

### From the web UI (recommended)

**Create experiment** → fill in the form → **Start** → watch the journal →
**Stop & collect**. Everything the form sets (model, tools, prompt, metadata)
is recorded in the run manifest and published with the run.

### From the CLI: `hannah_run.py`

The same lifecycle, scriptable:

```bash
# Begin an experiment: captures the prompt, model, tools, and git commit.
python3 hannah_run.py start --label tools-on-v3 --note "first run with tools"

# Configure the conditions as you start:
python3 hannah_run.py start --label memory-only --tools memory_info,uptime \
    --description "Only memory tools offered" \
    --hypothesis "She will lean on memory_info for grounding"
python3 hannah_run.py start --label pure-reflection --tools none
python3 hannah_run.py start --label thinking-model --model qwen3-4b-thinking

# Check in on it any time.
python3 hannah_run.py status

# End it: package everything since start, summarize, and start fresh.
python3 hannah_run.py collect --summarize

# Run an existing experiment again, reusing its config (model/tools/prompt).
python3 hannah_run.py rerun --experiment memory-only               # fresh replicate
python3 hannah_run.py rerun --experiment memory-only --keep-memory # continue

# Delete a whole experiment (all runs + public-lab entry) or one run folder.
python3 hannah_run.py delete --experiment memory-only
python3 hannah_run.py delete --run memory-only_231502
```

`collect` does the whole cleanup: it stops the daemon, writes everything since
`start` into **`research/runs/<label>/`** (manifest with prompt/model/tools/
commit + entries + journal + prompt snapshot + report + `summary.md`, plus raw
log archives), **rotates `hannah.log`/`memory.jsonl` fresh** (so the next
experiment is uncontaminated), rebuilds the public lab, then restarts the
daemon. Flags: `--local` (force local analysis), `--keep-memory`,
`--no-restart`.

Two files then give you the whole picture — no folder-digging:

- **`research/INDEX.md`** — a table of every experiment (label, dates, model,
  prompt hash, tools, entry count, how many entries used tools).
- **`research/overview.md`** — an **evolving AI-written summary of how the
  agent changes across experiments** as you vary her prompt/model/tools. Each
  `collect` updates it by comparing the new run against the running overview.

### Example experiment types

- **Substrate observation** — no metrics, tools on: can she build an accurate
  picture of her environment purely through investigation?
- **Tool-use discipline** — restrict the toolset: does she stay grounded, or
  does she start claiming things she can't verify?
- **Pure reflection** — no tools at all: what does she write from memory and
  prompt alone, and how fast does it drift?
- **Memory persistence** — does she correctly carry observations across
  entries and notice what changed?
- **Model comparison** — same prompt and tools, different model.
- **Failure recovery** — how does she handle tool errors or her own
  contradictions?
- **Web research** — enable `web_search` + `web_fetch` and give her a question
  to investigate: does she search, read sources, and stay grounded in what she
  actually found, or fill gaps with unsupported claims?

### Ad-hoc exports: `hannah_export.py`

For a one-off dump of an arbitrary window (without the run lifecycle):

```bash
python3 hannah_export.py --label baseline
python3 hannah_export.py --since 2026-07-01 --until 2026-07-03 --label cadence-test
```

Each export creates `research/<timestamp>_<label>/` with a manifest (model and
prompt-version distributions, config snapshot, host info, git commit), the
structured entries, the raw journal slice, the exact archived prompt versions,
and a readable `report.md`.

**AI summary** — add `--summarize`. Uses OpenAI if `OPENAI_API_KEY` is set,
otherwise (or on failure) falls back to the local llama-server, so analysis
works fully offline. The summary is also written to
`research/latest_summary.md`; the next `--summarize` feeds that back as a
baseline and adds a "changes since the previous analysis" section, so each
export diffs itself against the last one automatically.

### Provenance

Every entry records **which model and which prompt** produced it. Each
distinct prompt version is snapshotted verbatim to
`logs/prompt_history/<fingerprint>.json` the first time it is used — however
it was changed (web UI or file edit) — so any entry is fully traceable back to
the exact model and prompt behind it. Full tool traces (tool + output) are
recorded alongside each entry.

## Customizing the agent's voice

The prompts live in editable plain-text files — **no code changes needed**:

- `prompts/system_prompt.txt` — who the agent is and how she should think/speak
- `prompts/task_prompt.txt` — the instruction given on each wake-up

Edit in the web UI or directly in the files; the next entry picks it up. If a
file is missing or empty, the built-in defaults in `hannah.py` apply. Point
`HANNAH_PROMPT_DIR` at another directory to keep alternate prompt sets.

## Hannah Lab — the public site

The public lab is the **read-only layer** around the private runtime: an open
lab notebook where anyone can follow the agent over time — inspect her runs,
see which tools she chose, read her journals, browse derived memories and
beliefs, review failures, and watch the system change. It is a static site,
generated on your machine from collected runs and pushed **outbound only** to a
cheap static host. Nothing on the public site can reach back into the machine,
trigger the agent, or send her prompts.

```
Your machine (private)                             Static host (public)
──────────────────────                             ────────────────────
hannah.py --daemon        runs locally, GPU inference stays home
hannah_run.py collect  →  research/runs/<label>/    (private bundle)
hannah_lab.py build    →  sanitize → derive state → research/runs/<label>/public/
                          render   → public_lab/site/
hannah_lab.py publish  →  aws s3 sync (outbound push)  →  the world reads it
```

There are two separate surfaces, and they never mix:

- **Private control UI** (`hannah-web.service`, port 8600) — run the agent,
  edit prompts, create experiments, see raw data. LAN-only; never expose it.
- **Public lab site** (`public_lab/site/`) — sanitized, static, read-only.

### Building and previewing

```bash
python3 hannah_lab.py build      # sanitize runs, derive state, render the site
python3 hannah_lab.py preview    # serve it at http://<this-machine-ip>:8890/
python3 hannah_lab.py check      # re-run the fail-closed sanitizer gate
```

`build` also runs automatically after every `hannah_run.py collect` (disable
with `config.json → lab.auto_build`).

**Preview is a live control surface.** When you run `preview` on your own
machine, the Experiments page can **create, delete, and stop-&-collect
experiments** directly — the create form sets label, description/goal/
hypothesis, model, tools, and prompt, exactly like the private control UI.
Each experiment tile has a **↻ Run again** button that starts another run
under the same label, reusing its model, tools, and prompt — you choose
**Fresh replicate** (reset memory; an independent trial for reproducibility)
or **Continue from last run** (restore the previous run's memory and build on
it). The lab groups runs under one experiment and tracks how beliefs and
memory evolve across them, so re-running is how the cross-run picture is built.

A running experiment is marked **● live** on its tile and links to a
**live view** (`live.html`) that streams its journal, tool calls, and counts
in real time while it runs — so you can watch an experiment unfold before
collecting it. The Experiments page also has **daemon controls**
(start / stop / restart with a live status dot, plus a **run-interval** field —
"runs every N min") so you can bring Hannah up or down and tune how often she
runs without touching `systemctl` or `config.json` (starting the daemon also
pulls up llama-server). Interval changes apply **live**: the daemon re-reads
its cadence each cycle, so there's no restart.
These controls talk to a small `/api/lab/*` API that only exists while
`preview` is running: they act on your local runtime, so keep preview on a
trusted network (`--host 127.0.0.1` for local-only). The controls detect that
API at runtime, so if you ever serve `public_lab/site/` from a plain static
host instead, they simply don't appear and the site stays read-only.

### How the site is organized

**Experiments are the spine of the lab.** The site is a console-style UI: a
dashboard of recent runs, an experiments directory, and everything else lives
inside the experiment that produced it:

```
index.html                          dashboard: stats, recent runs, activity
experiments.html                    experiment tiles
experiments/<name>/index.html       overview: goal, hypothesis, runs, findings
experiments/<name>/journal.html     the experiment's journal feed
experiments/<name>/runs.html        runs table
experiments/<name>/runs/<id>.html   run detail: the full investigation path
experiments/<name>/memory.html      memory derived from this experiment
experiments/<name>/beliefs.html     belief state with confidence + contradictions
experiments/<name>/questions.html   open questions (open/investigating/abandoned)
experiments/<name>/timeline.html    event stream + what each run changed
experiments/<name>/failures.html    the experiment's failure wall
lab_state.json                      machine-readable snapshot of the whole lab
```

Derived state (memories, beliefs, questions, timeline) is computed **per
experiment**, so each experiment reads as a self-contained investigation. The
dashboard carries the cross-experiment picture: recent runs, lab-wide beliefs
(with evidence links into each experiment's runs), and the latest activity.

Experiment metadata — description, goal, hypothesis, notes, status — comes
from `public_lab/experiments.json`, written automatically by the
create-experiment form (and editable by hand).

The run detail page shows the full investigation path: initial prompt,
available tools, the tool calls the agent chose, sanitized tool results, what
the run changed in memory/beliefs/questions, score, failures. Each run also
publishes raw artifacts (`public_manifest.json`, `journal.md`,
`tool_trace.public.json`, `memory_changes.public.json`,
`belief_changes.public.json`, `questions.public.json`, `score.json`,
`failures.json`, `run_summary.json`) into `research/runs/<label>/public/`,
copied alongside the run page.

All derived state (memories, beliefs, questions, scores, failures) is computed
**deterministically** from the sanitized run data — no model calls — so the
whole site can be rebuilt from scratch at any time.

### Scoring

Each run gets a simple rule-based score, published with the run: did she use
tools, did she call tools that weren't offered, are her claims backed by a
same-cycle tool call (heuristic), did she acknowledge tool errors, did she
stay within the tool budget, did she flag uncertainty, did the sanitizer
approve publishing. Heuristic and imperfect on purpose — the score components
and their evidence are shown, not just a number.

### Sanitization (fail closed)

Everything passes through `lab/sanitizer.py` before it can be published:

- **Redacted** with placeholders: local usernames, the hostname, home paths,
  private (and by default public) IPs, MAC addresses, model paths, plus any
  custom strings in `config.json → lab.redact_terms`.
- **Blocked** — if anything that looks like an actual secret survives (private
  keys, AWS keys, API tokens, bearer headers, kubeconfig blobs, `.env`-style
  assignments), the artifact is withheld and marked, and `publish` refuses to
  upload. As a final gate, `check` re-scans every rendered file.

### Publishing (optional, S3 for v1)

```bash
export HANNAH_PUBLISH_TARGET=s3
export HANNAH_S3_BUCKET=hannah-lab-site
export HANNAH_AWS_REGION=us-east-1     # optional

python3 hannah_lab.py publish --dry-run   # see what would sync
python3 hannah_lab.py publish             # build → check → aws s3 sync --delete
```

Credentials come from your normal AWS setup (env/profile); nothing is stored in
the repo. The machine only ever pushes outbound — no ports opened, no inbound
access, no cloud GPUs. Any static host works: sync `public_lab/site/` to
GitHub Pages or Cloudflare Pages instead if you prefer.

## Configuration

Runtime behavior lives in `config.json` (override path with `HANNAH_CONFIG`):
available models and the active one, model server URL, generation settings
(tokens, temperature…), daemon cadence, salience thresholds, the observation
mode (`observation.include_metrics`), tools (master switch, per-experiment
selection, call budget, the `web_fetch`/`web_search` limits `web_fetch_timeout_s`
/ `web_fetch_max_bytes`, and the search backend `search_url` / `search_results`),
memory depth, log rotation, web UI host/port, the
analysis model, and the public lab (`lab.*`: GitHub URL, auto-build, redaction
terms, publish target). Any key you omit falls back to the built-in defaults.

## Roadmap

- More tools — logs, sensors, filesystem metadata — and per-tool budgets
- Real external senses (microphone audio level, then a camera) as tools she
  can choose to consult
- Model-level continuity (persistent KV cache) for one unbroken unfolding session
- MCP endpoint so external clients can host the agent's tools
- Optional semantic memory: a vector store over past entries for
  retrieval/search ("find every entry about overheating") — complementary to
  the run-to-run summary diffing that already exists

## License

[Apache License 2.0](LICENSE) © 2026 Zack Feldstein
