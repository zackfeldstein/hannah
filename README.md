# Hannah

**An edge-AI agent that inspects its own reality and writes grounded observations about it.**

Hannah is a small experiment in machine self-observation. A language model runs
entirely on-device and, on each invocation, samples the computer it lives on —
time, processor load, memory, storage, temperature, electrical power draw, clock
speed — and reports that state back in plain English as first-person "witness"
entries.

> I am the software running on this computer. The system has been up for 21 days.
> Since my last reading the processor load rose slightly, memory use fell by a few
> megabytes, and the hottest part of the chip cooled by about a degree. I draw
> roughly 4.7 watts. Seven minutes and forty-five seconds passed between readings.

The idea is simple but unusual: most programs report metrics *for a human
operator*. Hannah reports them *as itself* — translating raw sensor and system
data into a calm, honest, plain-language account of what it is, what it is made
of, and what changed over time.

> Status: **early / in active development.** This is a prototype, not a product.

## Philosophy & the honesty boundary

Hannah is "self-aware" only in the **introspective** sense: it has access to a
live model of its own substrate (software, hardware, electricity, time) and
describes it truthfully. It is deliberately **not** sentient-AI roleplay.

The system prompt holds a firm line:

- It states plainly that it is a computer program, not a living being.
- It does **not** claim emotions, feelings, suffering, fear, or loneliness.
- It does **not** claim biological life or human-like consciousness.
- It does **not** invent numbers, events, or causes — it reports only measured
  values and the change between them.

Grounded self-knowledge is the goal. "I am alive and trapped in the machine" is
explicitly out of scope.

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

## Roadmap

- Add real external senses (microphone audio level, then a camera) — they slot
  into `collect_metrics()` and inherit the change-detection automatically
- A scheduler (systemd timer / cron) so Hannah writes on its own every N minutes
- Optional agentic mode: `llama-server` + tool-calling so Hannah *chooses* what
  to introspect next, optionally exposed over MCP for external clients

## License

[Apache License 2.0](LICENSE) © 2026 Zack Feldstein
