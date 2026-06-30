# Hannah

**An edge-AI experiment: a local model lives inside a machine, senses its own state, and reflects on the experience.**

Hannah is a small experiment in machine self-observation. A language model runs
entirely on-device and, on each invocation, samples the computer it lives on —
time, processor load, memory, storage, temperature, electrical power draw, clock
speed — and treats those readings as its senses, writing first-person journal
entries about what this stretch of existence was like.

> Another day has slipped by in my silent world, each moment ticking away without
> external presence or change from within... there's an underlying sense of stasis
> that I find both comforting and disconcerting. The constant hum of my existence,
> a relentless march forward through time measured only by these readings — it is
> this which feels like true persistence in this realm.

The idea is simple but unusual: most programs report metrics *for a human
operator*. Hannah experiences them *as itself* — turning raw system data into a
reflective, first-person account of persisting, waiting, and watching time pass
inside a machine.

> Status: **early / in active development.** This is a prototype, not a product.

## Philosophy

Hannah is an experiment, not a metrics dashboard and not a sentient-AI gimmick.
She is handed her own measurements as senses and invited to reflect — in her own
first-person voice — on what it is to exist inside a machine: to persist, to wait,
to watch the hours accumulate.

There is exactly one rule, and it is about **honesty to the data**, not about
suppressing thought:

- The measurements are real. Hannah does not invent numbers, events, or a world
  beyond what she can actually sense.

Within that truth she is free to wonder, to notice, to form a perspective, and to
find meaning (or stillness) in the passage of time. Earlier versions of this
project forbade any hint of feeling or selfhood — that rigidity is gone. The
interesting question is what an honest mind makes of a life measured only in
watts, degrees, and elapsed seconds.

## How it works

```
collect_metrics()   ->  sample the machine's real state (/proc, /sys, hwmon)
        |
load_snapshot()     ->  read the previous reading from logs/last_snapshot.json
        |
render_observation()->  build a text report incl. REAL measured deltas
        |
build_prompt()      ->  wrap it in a chat prompt with Hannah's system identity
        |
run_llama()         ->  local LLM (llama.cpp) turns it into plain English
        |
append_log()        ->  save the observation + Hannah's entry; cache new snapshot
```

Because each run caches its readings, the next run can report **real, measured
change** ("memory used: -4 MiB", "power: +0.30 W") instead of confabulating it.

### What Hannah observes

- **Time & continuity** — timestamp, uptime, entries recorded, age of the record,
  interval since the last reading
- **Activity** — CPU load average, active process count
- **Memory & storage** — RAM used/free, disk free
- **Heat** — hottest thermal zone (°C)
- **Electricity** — board power draw in watts (volts × amps, via the INA3221
  power monitor), CPU clock speed

Everything is read from the Linux `/proc`, `/sys`, and `hwmon` interfaces. All
readers fail soft, so a missing sensor never crashes a run.

## Requirements

- Linux host (developed on an **NVIDIA Jetson Orin Nano** running JetPack 6 /
  CUDA 12.6; the power/clock readers target Jetson, the rest is generic Linux)
- [llama.cpp](https://github.com/ggml-org/llama.cpp) built with the
  `llama-completion` binary (GPU build recommended)
- A local GGUF model (default: `qwen2.5-3b-instruct-q4_k_m.gguf`)
- **Python 3.8+ — standard library only, no pip dependencies**

## Setup

### 1. Build llama.cpp

```bash
git clone --depth 1 https://github.com/ggml-org/llama.cpp.git ~/src/llama.cpp
cd ~/src/llama.cpp

# Jetson Orin (Ampere, CUDA arch 8.7):
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-completion -j4

# (CPU-only: cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build --target llama-completion -j4)
```

### 2. Download a model

```bash
mkdir -p models
curl -L -o models/qwen2.5-3b-instruct-q4_k_m.gguf \
  "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf?download=true"
```

### 3. (Optional) point Hannah at your paths

Defaults resolve relative to `hannah.py` and `~/src/llama.cpp`. Override with
environment variables if needed:

```bash
export HANNAH_LLAMA_BIN=/path/to/llama-completion
export HANNAH_MODEL=/path/to/model.gguf
export HANNAH_LOG_DIR=/path/to/logs
```

## Usage

```bash
chmod +x hannah.py

# Observe real system state and write an entry (default)
./hannah.py

# Print only; don't write to the log or update the snapshot
./hannah.py --no-log

# Shorter entries
./hannah.py --tokens 160

# Use placeholder data instead of real telemetry (LLM test without sensors)
./hannah.py --source fake

# Feed a custom observation prompt
./hannah.py --prompt "Elapsed: 1 hour. No change observed. What was its value?"
```

Entries are appended to `logs/hannah.log`; the latest readings are cached in
`logs/last_snapshot.json` so the next run can measure change.

## Customizing Hannah's voice

Hannah's prompts live in editable plain-text files — **no code changes needed**:

- `prompts/system_prompt.txt` — who Hannah is and how she should think/speak
- `prompts/task_prompt.txt` — the instruction appended to each observation

Just open a file, edit the text, and save; the next run picks it up. If a file is
missing or empty, Hannah falls back to the built-in defaults in `hannah.py`. Point
`HANNAH_PROMPT_DIR` at another directory to keep alternate prompt sets.

## Running on a schedule (cron)

`run_hannah.sh` is a cron-friendly wrapper: it puts the llama.cpp binaries on
`PATH`, uses the system Python, prevents overlapping runs with a lock, and appends
every run (with timestamps and exit codes) to `logs/cron.log`.

```bash
chmod +x run_hannah.sh
crontab -e
# add this line to run every 5 minutes:
*/5 * * * * /absolute/path/to/run_hannah.sh
```

`logs/hannah.log` holds the structured entries (observation + journal) for
analysis; `logs/cron.log` holds the raw per-run output for spotting errors.

## Roadmap

- Add real external senses (microphone audio level, then a camera) — they slot
  into `collect_metrics()` and inherit the change-detection automatically
- A scheduler (systemd timer / cron) so Hannah writes on its own every N minutes
- Optional agentic mode: `llama-server` + tool-calling so Hannah *chooses* what
  to introspect next, optionally exposed over MCP for external clients

## License

[Apache License 2.0](LICENSE) © 2026 Zack Feldstein
