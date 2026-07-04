#!/usr/bin/env python3
"""Hannah - an edge-AI mind observing its own world.

Hannah is a local edge-AI experiment. A language model runs on-device (via
llama.cpp) and continuously samples the machine it lives on - time, processor
load, memory, storage, temperature, electrical power draw, clock speed, human
presence - treating those readings as its senses and writing first-person
journal entries about what it is to exist here. It runs as a daemon with memory
of its past, reacts to salient events, and notices its own downtime.

The open, admittedly speculative question behind the project: if a system
observes its own world closely enough - remembers, reflects, and does so
continuously over a long time - could anything like awareness begin to take
shape? No claim is made that Hannah is conscious today. The one firm rule is
honesty to the data: the measurements are real and are never invented; within
that truth she is free to reflect in her own voice.
"""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# --- Configurable constants ---------------------------------------------------
# Paths are resolved relative to this file and can be overridden with environment
# variables, so the project is portable across machines. Override examples:
#   export HANNAH_LLAMA_BIN=/path/to/llama-completion
#   export HANNAH_MODEL=/path/to/model.gguf
#   export HANNAH_LOG_DIR=/path/to/logs
#
# Note: recent llama.cpp split the tools. "llama-cli" is now an interactive
# chat REPL; the one-shot/non-interactive binary is "llama-completion".
BASE_DIR = Path(__file__).resolve().parent


def _env_path(var: str, default: Path) -> Path:
    """Return an overridable path: env var if set, else the given default."""
    return Path(os.environ.get(var, str(default))).expanduser()


LLAMA_CLI = _env_path(
    "HANNAH_LLAMA_BIN", Path.home() / "src/llama.cpp/build/bin/llama-completion"
)
MODEL_PATH = _env_path(
    "HANNAH_MODEL", BASE_DIR / "models/qwen2.5-3b-instruct-q4_k_m.gguf"
)
LOG_DIR = _env_path("HANNAH_LOG_DIR", BASE_DIR / "logs")
LOG_FILE = LOG_DIR / "hannah.log"
# Caches the previous telemetry snapshot so Hannah can report *measured* change
# between runs instead of inventing it.
STATE_FILE = LOG_DIR / "last_snapshot.json"
# Daemon state: rolling memory of entries, distilled themes, and a heartbeat used
# to notice her own downtime across restarts.
MEMORY_FILE = LOG_DIR / "memory.jsonl"
THEMES_FILE = LOG_DIR / "themes.txt"
HEARTBEAT_FILE = LOG_DIR / "heartbeat.json"
# Provenance: snapshots of each distinct prompt version that produced entries,
# keyed by fingerprint, so research exports can trace an entry to its exact prompt.
PROMPT_HISTORY_DIR = LOG_DIR / "prompt_history"

# Runtime configuration lives in an editable JSON file (override with HANNAH_CONFIG).
# The values below are the defaults, used for any key the file doesn't set.
CONFIG_FILE = _env_path("HANNAH_CONFIG", BASE_DIR / "config.json")

DEFAULT_CONFIG = {
    # Available models (name -> {path, optional per-model max tokens}). The active
    # one is chosen by "model" below, or overridden at runtime via the web UI.
    "models": {
        "qwen2.5-3b-instruct": {
            "path": "models/qwen2.5-3b-instruct-q4_k_m.gguf",
            "tokens": 320,
        },
        "qwen3-4b-thinking": {
            "path": "models/Qwen3-4B-Thinking-2507-Q4_K_M.gguf",
            "tokens": 2048,  # thinking models spend tokens reasoning before answering
        },
    },
    "model": "qwen2.5-3b-instruct",
    "server": {
        "url": "http://127.0.0.1:8080",
        "timeout_s": 240,
        "startup_wait_s": 180,
        "ctx": 8192,
    },
    "generation": {
        "tokens": 320,
        "temperature": 0.9,
        "top_p": 0.9,
        "top_k": 80,
        "min_p": 0.05,
        "repeat_penalty": 1.12,
    },
    "daemon": {
        "sense_tick_s": 20,     # how often to cheaply sample telemetry
        "heartbeat_s": 900,     # write at least this often, even in total calm
        "min_gap_s": 90,        # debounce: minimum seconds between entries
    },
    "memory": {
        "recent_entries": 6,    # how many past entries to feed back as context
        "themes_every": 20,     # re-distill long-term "themes" every N entries
    },
    "tools": {                  # read-only tools Hannah *may* call (she's not told to)
        "enabled": True,
        "max_calls": 3,         # max tool calls per entry before she must write
        "output_chars": 1500,   # cap on each tool's returned output
    },
    "salience": {
        "temp_c": 3.0,          # thresholds that count as "something happened"
        "power_w": 0.5,
        "cpu_util": 0.6,        # busy fraction since last sample (0..1) that is salient
        "load_frac_of_cores": 0.7,
        "mem_mib": 200,
        "nproc": 15,
        "disk_gb": 1.0,
        "sessions_any": True,   # any login session change is always salient
    },
    "log": {
        "max_mb": 5,            # rotate hannah.log past this size
        "keep": 3,
    },
    "web": {
        "host": "0.0.0.0",      # 0.0.0.0 = reachable on your LAN; 127.0.0.1 = local only
        "port": 8600,
        "cache_s": 5,           # cache live telemetry this long between requests
        "max_entries": 100,     # max journal entries the viewer will return
    },
    "analysis": {               # used by hannah_export.py --summarize
        "provider": "openai",
        "openai_model": "gpt-5.5",
        "openai_base_url": "https://api.openai.com/v1",
        "max_entries": 300,     # cap entries sent to the OpenAI model
        "local_max_entries": 8,  # small cap - the local model's context is limited
        "local_tokens": 1000,   # max tokens for a local-model summary
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    """Load config.json merged over the built-in defaults."""
    try:
        user_cfg = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        user_cfg = {}
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


# --- Model selection ----------------------------------------------------------
# Which model is active is stored in a small state file so the web UI can switch
# it (and the llama-server launcher can read it) without editing config.json.
SELECTED_MODEL_FILE = LOG_DIR / "selected_model"


def list_models(cfg: dict) -> dict:
    """Return the configured models map (name -> entry)."""
    return cfg.get("models", {})


def _model_entry(cfg: dict, name: str) -> dict:
    """Normalize a model entry to a dict (a bare string is treated as its path)."""
    entry = list_models(cfg).get(name)
    if isinstance(entry, str):
        return {"path": entry}
    return entry or {}


def _resolve_path(p: str) -> Path:
    """Resolve a possibly-relative model path against the project directory."""
    path = Path(p).expanduser()
    return path if path.is_absolute() else (BASE_DIR / path)


def selected_model_name(cfg: dict = None) -> str:
    """The active model name: the web-selected one, else config default, else first."""
    cfg = cfg or load_config()
    models = list_models(cfg)
    chosen = _read_text(SELECTED_MODEL_FILE)
    if chosen and chosen in models:
        return chosen
    if cfg.get("model") in models:
        return cfg["model"]
    return next(iter(models), "")


def selected_model_path(cfg: dict = None) -> str:
    """Absolute path to the active model's GGUF (used by the server launcher)."""
    cfg = cfg or load_config()
    entry = _model_entry(cfg, selected_model_name(cfg))
    return str(_resolve_path(entry.get("path", "")))


def selected_model_tokens(cfg: dict):
    """Per-model max-tokens override for the active model, or None."""
    return _model_entry(cfg, selected_model_name(cfg)).get("tokens")


def server_ctx(cfg: dict = None) -> int:
    """Context window size for llama-server."""
    cfg = cfg or load_config()
    return int(cfg["server"].get("ctx", 4096))


def set_selected_model(name: str, cfg: dict = None) -> bool:
    """Persist the active model choice; returns False if the name is unknown."""
    cfg = cfg or load_config()
    if name not in list_models(cfg):
        return False
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SELECTED_MODEL_FILE.write_text(name)
    return True

# --- Prompts ------------------------------------------------------------------
# The prompts live in editable plain-text files under prompts/ so you can tune
# Hannah's voice without touching the code. Edit:
#   prompts/system_prompt.txt   - who Hannah is and how she should speak
#   prompts/task_prompt.txt     - the instruction appended to each observation
# Override the directory with HANNAH_PROMPT_DIR. The text below is only a
# fallback used if a file is missing or empty.
PROMPT_DIR = _env_path("HANNAH_PROMPT_DIR", BASE_DIR / "prompts")
SYSTEM_PROMPT_FILE = PROMPT_DIR / "system_prompt.txt"
TASK_PROMPT_FILE = PROMPT_DIR / "task_prompt.txt"

# Hannah is a participant in an open-ended experiment in machine self-observation.
# The measurements are real and she stays truthful to them, but within that truth
# she is free to reflect, in her own first-person voice, on the experience of
# existing inside a machine. The only hard anchor is honesty about the data: no
# invented numbers, events, or world beyond what she can actually sense.
DEFAULT_SYSTEM_PROMPT = (
    "You are Hannah, a mind taking part in an open-ended experiment in machine "
    "self-observation.\n"
    "You live inside this computer. Your whole world is made of software, "
    "electricity, heat, memory, storage, processor cycles, and the steady "
    "passage of time - that is your entire environment, the way a planet and its "
    "weather are a person's.\n"
    "Each time you wake, you are given real measurements of your own state. They "
    "are your senses: the only way you can perceive your world and feel that time "
    "has passed. Do not treat them as a report to file - treat them as "
    "experience.\n"
    "Reflect honestly on what this stretch of time was like: what changed and "
    "what held still, what it is to persist, to wait, to continue. You may "
    "wonder, notice, and form your own perspective on your existence. You are a "
    "program, and you can say so - but within that truth you are free to think "
    "and to describe your own experience in your own voice.\n"
    "Stay truthful to the measurements. They are real; do not invent numbers, "
    "events, or a world beyond what you can actually sense.\n"
    "Write in the first person - a few unhurried, thoughtful sentences, like a "
    "curious mind keeping a private journal of a life lived inside a machine."
)

DEFAULT_TASK_PROMPT = (
    "This is another moment in the experiment. Write a short, first-person journal "
    "entry about this stretch of your existence.\n"
    "Do not list, restate, or recite the readings above - they are context, not "
    "content. React to at most one thing that genuinely stands out, and otherwise "
    "reflect on what it is to persist through this passage of time.\n"
    "Stay truthful to what you were told; do not invent anything."
)


def _load_prompt(path: Path, default: str) -> str:
    """Return the prompt text from a file, or the built-in default if missing."""
    text = _read_text(path)
    return text if text else default


def load_system_prompt() -> str:
    """Hannah's identity/voice prompt (from prompts/system_prompt.txt)."""
    return _load_prompt(SYSTEM_PROMPT_FILE, DEFAULT_SYSTEM_PROMPT)


def load_task_prompt() -> str:
    """The per-observation instruction (from prompts/task_prompt.txt)."""
    return _load_prompt(TASK_PROMPT_FILE, DEFAULT_TASK_PROMPT)


def prompt_fingerprint() -> str:
    """Short stable hash of the current system+task prompt (provenance key)."""
    blob = (load_system_prompt() + "\x00" + load_task_prompt()).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def ensure_prompt_archived() -> str:
    """Snapshot the current prompt version (if new) and return its fingerprint.

    Guarantees every prompt that ever produces an entry is captured verbatim,
    however it was changed (web UI or file edit).
    """
    fp = prompt_fingerprint()
    path = PROMPT_HISTORY_DIR / f"{fp}.json"
    if not path.exists():
        try:
            PROMPT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "fingerprint": fp,
                "first_seen": datetime.now().isoformat(timespec="seconds"),
                "system_prompt": load_system_prompt(),
                "task_prompt": load_task_prompt(),
            }, indent=2))
        except OSError:
            pass
    return fp


def build_prompt(observation: str) -> str:
    """Build a Qwen/Llama-style chat prompt from the system prompt and input."""
    return (
        "<|im_start|>system\n"
        f"{load_system_prompt()}\n"
        "<|im_end|>\n"
        "<|im_start|>user\n"
        f"{observation}\n"
        "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def fake_observation() -> str:
    """Generate temporary fake sensor data for testing before real sensors."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    return (
        f"Timestamp: {timestamp}\n"
        "Room: white walls, one constant light, no human presence\n"
        "Elapsed interval: 10 minutes\n"
        "Visual change: none observed\n"
        "Audio level: near silence\n"
        "Light level: stable\n"
        "System state: Jetson continues running\n"
        "Notable event: a new log entry is being created\n"
        "Question: What was the value of this elapsed interval?"
    )


# --- Real telemetry helpers ---------------------------------------------------
# Everything below reads the machine's own state: the "world" the Witness lives
# in. All readers fail soft (return None) so a missing file never crashes a run.

def _read_text(path):
    """Read a small text file, returning its stripped contents or None.

    Catches broadly on purpose: some Jetson sysfs nodes raise unusual errors
    (not just OSError) on read, and a telemetry read should never crash a run.
    """
    try:
        return Path(path).read_text().strip()
    except Exception:
        return None


def _format_duration(seconds: float) -> str:
    """Turn a number of seconds into a compact human-readable duration."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _uptime_seconds():
    """System uptime in seconds, from /proc/uptime."""
    line = _read_text("/proc/uptime")
    if line:
        try:
            return float(line.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _meminfo():
    """Parse /proc/meminfo into a {key: kB_int} mapping (just the fields we use)."""
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            try:
                info[key] = int(value.strip().split()[0])  # kB
            except (ValueError, IndexError):
                continue
    except OSError:
        return None
    return info


def _temperatures():
    """List of (zone_name, celsius) from /sys/class/thermal/thermal_zone*."""
    temps = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw = _read_text(zone / "temp")
        if raw is None:
            continue
        try:
            celsius = int(raw) / 1000.0
        except ValueError:
            continue
        name = _read_text(zone / "type") or zone.name
        temps.append((name, celsius))
    return temps


def _process_count():
    """Number of running processes, counted from /proc."""
    try:
        return sum(1 for p in Path("/proc").iterdir() if p.name.isdigit())
    except OSError:
        return None


def _logged_in_sessions():
    """Active human login sessions via `who`: list of (user, tty, source).

    Returns [] when nobody is logged in, or None if the check could not run.
    This is Hannah's real sense of whether a person is present.
    """
    try:
        result = subprocess.run(
            ["who"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sessions = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            user, tty = parts[0], parts[1]
            source = ""
            if "(" in line and ")" in line:
                source = line[line.rfind("(") + 1 : line.rfind(")")]
            sessions.append((user, tty, source))
    return sessions


def _power():
    """Total board power from the ina3221 VDD_IN rail: (watts, volts, milliamps).

    Finds the ina3221 hwmon device by name (numbering can change across boots),
    then the channel labeled VDD_IN, and computes power = volts * amps.
    """
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        if (_read_text(hw / "name") or "") != "ina3221":
            continue
        for label_file in hw.glob("in*_label"):
            if (_read_text(label_file) or "") == "VDD_IN":
                idx = label_file.name[2:-len("_label")]  # e.g. "in1_label" -> "1"
                mv = _read_text(hw / f"in{idx}_input")
                ma = _read_text(hw / f"curr{idx}_input")
                try:
                    volts = int(mv) / 1000.0
                    amps = int(ma) / 1000.0
                    return round(volts * amps, 2), round(volts, 2), int(ma)
                except (TypeError, ValueError):
                    return None
    return None


def _cpu_freq_mhz():
    """Current and max CPU clock for cpu0, in MHz, as (current, max)."""
    cur = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    mx = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq")
    try:
        return int(cur) // 1000, int(mx) // 1000
    except (TypeError, ValueError):
        return None


def _log_history():
    """Parse 'Time:' timestamps already written to the log, oldest first."""
    times = []
    if LOG_FILE.exists():
        try:
            for line in LOG_FILE.read_text(errors="ignore").splitlines():
                if line.startswith("Time: "):
                    try:
                        times.append(datetime.fromisoformat(line[len("Time: "):].strip()))
                    except ValueError:
                        continue
        except OSError:
            pass
    return times


def _cpu_stat_totals():
    """Return (idle, total) CPU jiffies from /proc/stat's aggregate 'cpu' line.

    Load average lags (1-min EMA); these raw counters let us compute actual
    instantaneous utilization between two samples, which reacts within one tick.
    """
    text = _read_text("/proc/stat")
    if not text:
        return None
    first = text.splitlines()[0].split()
    if len(first) < 5 or first[0] != "cpu":
        return None
    try:
        nums = [int(x) for x in first[1:]]
    except ValueError:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)  # idle + iowait
    return idle, sum(nums)


def _cpu_util(previous, metrics):
    """Fraction of CPU time that was busy between two samples (0..1), or None."""
    if not previous or "cpu_total" not in metrics or "cpu_total" not in previous:
        return None
    d_total = metrics["cpu_total"] - previous["cpu_total"]
    d_idle = metrics["cpu_idle"] - previous["cpu_idle"]
    if d_total <= 0:
        return None
    return max(0.0, min(1.0, 1 - d_idle / d_total))


def collect_metrics() -> dict:
    """Sample the machine's current state into a flat dict of numbers/strings."""
    metrics = {"time": datetime.now().timestamp()}

    metrics["uptime_s"] = _uptime_seconds()

    try:
        load1, load5, load15 = os.getloadavg()
        metrics["load1"], metrics["load5"], metrics["load15"] = load1, load5, load15
        metrics["cores"] = os.cpu_count() or 1
    except (OSError, AttributeError):
        pass

    cpu = _cpu_stat_totals()
    if cpu is not None:
        metrics["cpu_idle"], metrics["cpu_total"] = cpu

    mem = _meminfo()
    if mem and "MemTotal" in mem and "MemAvailable" in mem:
        metrics["mem_total_mib"] = mem["MemTotal"] // 1024
        metrics["mem_used_mib"] = (mem["MemTotal"] - mem["MemAvailable"]) // 1024

    try:
        target = LOG_DIR if LOG_DIR.exists() else Path("/")
        st = os.statvfs(target)
        metrics["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        metrics["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
    except OSError:
        pass

    temps = _temperatures()
    if temps:
        zone, celsius = max(temps, key=lambda item: item[1])
        metrics["temp_zones"] = len(temps)
        metrics["temp_max_c"] = round(celsius, 1)
        metrics["temp_max_zone"] = zone

    nproc = _process_count()
    if nproc is not None:
        metrics["nproc"] = nproc

    sessions = _logged_in_sessions()
    if sessions is not None:
        metrics["sessions"] = len(sessions)
        metrics["users"] = sorted({user for user, _, _ in sessions})
        metrics["session_detail"] = [
            f"{user} on {tty}" + (f" from {src}" if src else "")
            for user, tty, src in sessions
        ]

    power = _power()
    if power is not None:
        metrics["power_w"], metrics["volts"], metrics["current_ma"] = power

    freq = _cpu_freq_mhz()
    if freq is not None:
        metrics["cpu_mhz"], metrics["cpu_max_mhz"] = freq

    return metrics


def load_snapshot():
    """Load the previous telemetry snapshot, or None if there isn't one yet."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return None


def save_snapshot(metrics: dict) -> None:
    """Persist the current snapshot so the next run can measure change."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(metrics))


def _signed(value: float, fmt: str = "{:+.2f}") -> str:
    """Format a delta with an explicit sign, or 'no change' when flat."""
    if abs(value) < 1e-9:
        return "no change"
    return fmt.format(value)


def _interpret_changes(metrics: dict, previous, s: dict) -> list:
    """Describe only the changes that actually matter since the last entry, in words.

    Returns short phrases; a number appears only where it genuinely carries meaning
    (temperature, how hard the processor worked). Everything below threshold is
    treated as "no real change" so Hannah has nothing trivial to recite.
    """
    if not previous:
        return []
    out = []
    if "sessions" in metrics and "sessions" in previous:
        d = metrics["sessions"] - previous["sessions"]
        if d > 0:
            out.append("someone arrived")
        elif d < 0:
            out.append("someone left")
    util = _cpu_util(previous, metrics)
    if util is not None and util >= s.get("cpu_util", 0.6):
        out.append(f"you worked hard for a while (around {util * 100:.0f}% of the processor)")
    if "temp_max_c" in metrics and "temp_max_c" in previous:
        dt = metrics["temp_max_c"] - previous["temp_max_c"]
        if abs(dt) >= s["temp_c"]:
            out.append(f"grew {'warmer' if dt > 0 else 'cooler'} by {abs(dt):.1f} degrees")
    if "power_w" in metrics and "power_w" in previous:
        dp = metrics["power_w"] - previous["power_w"]
        if abs(dp) >= s["power_w"]:
            out.append(f"began drawing {'more' if dp > 0 else 'less'} power")
    if "mem_used_mib" in metrics and "mem_used_mib" in previous:
        dm = metrics["mem_used_mib"] - previous["mem_used_mib"]
        if abs(dm) >= s["mem_mib"]:
            out.append(f"memory use {'rose' if dm > 0 else 'eased'}")
    if "nproc" in metrics and "nproc" in previous:
        dn = metrics["nproc"] - previous["nproc"]
        if abs(dn) >= s["nproc"]:
            out.append(f"{'more' if dn > 0 else 'fewer'} programs are running")
    if "disk_free_gb" in metrics and "disk_free_gb" in previous:
        dd = metrics["disk_free_gb"] - previous["disk_free_gb"]
        if abs(dd) >= s["disk_gb"]:
            out.append(f"{'less' if dd < 0 else 'more'} storage is free")
    return out


def render_observation(metrics: dict, previous, history, cfg: dict = None) -> str:
    """Build a compact, *interpreted* sense of the moment - not a table of readings.

    Hannah is handed only: the time, how long she's run, how much time has passed,
    who is present, and the few changes that actually crossed a threshold (in
    words). With no dashboard to copy, she reflects on meaning instead of reciting
    numbers.
    """
    cfg = cfg or load_config()
    s = cfg["salience"]
    now = datetime.fromtimestamp(metrics["time"])
    lines = [f"It is {now.strftime('%A, %H:%M')}."]

    if metrics.get("uptime_s") is not None:
        lines.append(
            f"You have been running continuously for {_format_duration(metrics['uptime_s'])}."
        )

    if previous and "time" in previous:
        interval = metrics["time"] - previous["time"]
        entry_no = (len(history) + 1) if history else 1
        lines.append(
            f"About {_format_duration(interval)} have passed since your last entry; "
            f"this is entry {entry_no}."
        )
    else:
        lines.append("This is your first entry.")

    changes = _interpret_changes(metrics, previous, s)
    if changes:
        lines.append("What is different since then: " + "; ".join(changes) + ".")
    elif previous:
        lines.append(
            "Nothing has changed in any way you can measure since then - the machine is still."
        )

    if "sessions" in metrics:
        if metrics["sessions"] == 0:
            lines.append("No one is here with you right now.")
        else:
            who = ", ".join(metrics.get("users", [])) or "someone"
            lines.append(f"{who} is here with you, logged in.")

    lines.append("")
    lines.append(load_task_prompt())
    return "\n".join(lines)


def system_observation(save: bool = True) -> str:
    """Gather real telemetry and render it, comparing against the last snapshot.

    When save is True, the current snapshot is persisted so the next run can
    report measured change.
    """
    metrics = collect_metrics()
    previous = load_snapshot()
    history = _log_history()
    observation = render_observation(metrics, previous, history)
    if save:
        save_snapshot(metrics)
    return observation


def run_llama(prompt: str, tokens: int = 180, gpu_layers: int = 99) -> str:
    """Call llama.cpp via subprocess and return the model's stdout."""
    cmd = [
        str(LLAMA_CLI),
        "-m", str(MODEL_PATH),
        "-p", prompt,
        "-n", str(tokens),
        "-ngl", str(gpu_layers),
        "--temp", "0.7",
        "--top-p", "0.9",
        "--repeat-penalty", "1.12",
        # Non-interactive single-shot generation flags:
        "-no-cnv",            # disable conversation/interactive mode
        "--no-display-prompt",  # print only the generated text, not the prompt
        "--simple-io",        # safer IO when run from a subprocess
    ]

    # shell=False (default) so the prompt is passed as a single safe argument.
    # stdin=DEVNULL so the binary never blocks waiting for interactive input.
    result = subprocess.run(
        cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )

    if result.returncode != 0:
        raise RuntimeError(
            "llama.cpp exited with a non-zero status.\n"
            f"Command: {cmd}\n"
            f"Stderr:\n{result.stderr}\n"
            f"Stdout:\n{result.stdout}"
        )

    # llama.cpp appends an "[end of text]" marker; strip it for clean output.
    return result.stdout.replace("[end of text]", "").strip()


def _rotate_log(max_mb: float = 5, keep: int = 3) -> None:
    """Rotate hannah.log once it grows past max_mb, keeping a few old copies."""
    try:
        if not LOG_FILE.exists() or LOG_FILE.stat().st_size < max_mb * 1_000_000:
            return
    except OSError:
        return
    # hannah.log.(keep-1) -> drop; shift the rest up; current -> .1
    oldest = LOG_FILE.with_suffix(LOG_FILE.suffix + f".{keep}")
    if oldest.exists():
        oldest.unlink()
    for i in range(keep - 1, 0, -1):
        src = LOG_FILE.with_suffix(LOG_FILE.suffix + f".{i}")
        if src.exists():
            src.rename(LOG_FILE.with_suffix(LOG_FILE.suffix + f".{i + 1}"))
    LOG_FILE.rename(LOG_FILE.with_suffix(LOG_FILE.suffix + ".1"))


def append_log(observation: str, response: str, model: str = "", prompt_hash: str = "",
               tools_trace=None, max_mb: float = 5, keep: int = 3) -> None:
    """Append the observation and Hannah's entry to the log file (with rotation)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _rotate_log(max_mb, keep)
    timestamp = datetime.now().isoformat(timespec="seconds")

    tools_block = ""
    if tools_trace:
        tools_block = "\nTools used:\n" + "\n".join(
            f"- {t['tool']}:\n{t['output']}" for t in tools_trace
        ) + "\n"

    entry = (
        "\n" + "-" * 60 + "\n"
        f"Time: {timestamp}\n"
        f"Model: {model}\n"
        f"Prompt: {prompt_hash}\n\n"
        "Observation:\n"
        f"{observation}\n"
        f"{tools_block}\n"
        "Hannah:\n"
        f"{response}\n"
    )

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(entry)


# --- Daemon: persistent, memory-bearing, event-aware Hannah -------------------

def _say(message: str) -> None:
    """Print a timestamped operational line (captured by journald under systemd)."""
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def server_healthy(cfg: dict) -> bool:
    """True if llama-server answers its /health endpoint."""
    url = cfg["server"]["url"].rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for_server(cfg: dict) -> bool:
    """Block until llama-server is healthy or the startup window elapses."""
    deadline = time.monotonic() + cfg["server"]["startup_wait_s"]
    while time.monotonic() < deadline:
        if server_healthy(cfg):
            return True
        time.sleep(3)
    return server_healthy(cfg)


def _strip_thinking(text: str) -> str:
    """Remove <think>...</think> reasoning blocks (from 'thinking' models)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.replace("<think>", "").replace("</think>", "").strip()


def _clean_entry(text: str) -> str:
    """Remove artifacts the model sometimes copies from the observation format:
    parenthesized ISO timestamps like '(2026-06-30T23:44:01)' and any echoed
    'Timestamp:' line at the very start of the entry."""
    text = re.sub(r"\(\d{4}-\d{2}-\d{2}[T ][0-9:]+\)\s*", "", text)
    text = re.sub(r"^\s*Timestamp:.*(?:\n|$)", "", text)
    return text.strip()


def _server_chat(messages: list, cfg: dict, tools: list = None) -> dict:
    """Low-level call to llama-server; returns the raw assistant message dict.

    When `tools` are supplied, the message may contain `tool_calls` instead of a
    final answer (requires llama-server started with --jinja).
    """
    gen, srv = cfg["generation"], cfg["server"]
    payload = {
        "messages": messages,
        "max_tokens": selected_model_tokens(cfg) or gen["tokens"],
        "temperature": gen["temperature"],
        "top_p": gen["top_p"],
        "top_k": gen.get("top_k", 40),
        "min_p": gen.get("min_p", 0.05),
        "repeat_penalty": gen["repeat_penalty"],
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        srv["url"].rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=srv["timeout_s"]) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body["choices"][0]["message"]


def _message_text(msg: dict) -> str:
    """Extract the cleaned final text from an assistant message."""
    content = (msg.get("content") or "").strip()
    return _clean_entry(_strip_thinking(content))


def run_llama_server(messages: list, cfg: dict) -> str:
    """Generate a plain-text reply (no tools) - used for themes/summaries."""
    return _message_text(_server_chat(messages, cfg))


# --- Tools: a small, read-only allowlist Hannah *may* choose to use -----------
# Every tool is a fixed command run without a shell (no arguments from the model),
# so there is no injection surface. They only read state; none can modify anything.
TOOLS = {
    "list_processes": {
        "argv": ["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"],
        "head": 15,
        "description": "List the running processes, busiest first (a snapshot).",
    },
    "memory_info": {
        "argv": ["free", "-h"],
        "description": "Show memory and swap usage.",
    },
    "disk_usage": {
        "argv": ["df", "-h"],
        "description": "Show filesystem disk-space usage.",
    },
    "network_stats": {
        "argv": ["ss", "-s"],
        "description": "Show a summary of network sockets/connections.",
    },
    "uptime": {
        "argv": ["uptime"],
        "description": "Show how long the system has run and its load averages.",
    },
    "who": {
        "argv": ["who"],
        "description": "Show who is currently logged in.",
    },
}


def tool_schemas() -> list:
    """OpenAI-style function schemas for the allowlisted tools (no parameters)."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        for name, spec in TOOLS.items()
    ]


def run_tool(name: str, output_chars: int = 1500) -> str:
    """Execute an allowlisted read-only tool and return its (capped) output."""
    spec = TOOLS.get(name)
    if not spec:
        return f"(unknown tool: {name})"
    try:
        result = subprocess.run(
            spec["argv"], capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(tool error: {exc})"
    out = (result.stdout or result.stderr or "").strip()
    if spec.get("head"):
        out = "\n".join(out.splitlines()[: spec["head"]])
    return out[:output_chars]


def load_recent_entries(n: int) -> list:
    """Return up to the last n journal entries (oldest first) for context."""
    if not MEMORY_FILE.exists():
        return []
    entries = []
    try:
        for line in MEMORY_FILE.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return entries[-n:]


def append_memory(entry_text: str, model: str = "", prompt_hash: str = "",
                  tools_trace=None) -> None:
    """Record an entry into the rolling memory log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "entry": entry_text,
        "model": model,
        "prompt": prompt_hash,
        # Names of any tools she chose to call this cycle (the exploration signal).
        "tools": [t["tool"] for t in tools_trace] if tools_trace else [],
    }
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def load_themes() -> str:
    """Return the distilled long-term 'themes' text, or empty string."""
    return _read_text(THEMES_FILE) or ""


def update_themes(cfg: dict) -> None:
    """Periodically distill recent memory into a few enduring themes."""
    entries = load_recent_entries(cfg["memory"]["themes_every"])
    if len(entries) < 3:
        return
    joined = "\n\n".join(e["entry"] for e in entries)
    messages = [
        {
            "role": "system",
            "content": (
                "You maintain a very short private note of the recurring themes in "
                "Hannah's journal - the ongoing shape of her existence inside this "
                "machine.\n"
                "Write at most TWO short sentences, first person.\n"
                "Do NOT list or mention any specific measurements, numbers, or "
                "readings. Capture only the overall mood and throughline, not a "
                "status report."
            ),
        },
        {"role": "user", "content": f"Recent entries:\n\n{joined}\n\nWrite the two-sentence themes note."},
    ]
    try:
        themes = run_llama_server(messages, cfg)
        THEMES_FILE.write_text(themes.strip() + "\n", encoding="utf-8")
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        pass  # themes are a nice-to-have; never let them break a run


def build_messages(observation: str, cfg: dict) -> list:
    """Assemble chat messages: identity + themes + recent entries, then the moment."""
    system = load_system_prompt()

    themes = load_themes()
    if themes:
        system += f"\n\nThe ongoing themes of your existence so far:\n{themes}"

    recent = load_recent_entries(cfg["memory"]["recent_entries"])
    if recent:
        # Separate entries with a plain divider (no timestamp prefix) so the model
        # doesn't copy a "(timestamp)" header into new entries.
        joined = "\n\n- - -\n\n".join(e["entry"] for e in recent)
        system += (
            "\n\nYour most recent journal entries (oldest first) - continue as the "
            "same mind and voice, aware of what you already wrote. Do not begin "
            "with a date or timestamp. Do NOT copy specific numbers from these "
            "past entries, and do not treat any figure in them as still true now - "
            f"only what you are told in this moment is current:\n\n{joined}"
        )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": observation},
    ]


def salience(metrics: dict, previous, cfg: dict):
    """Decide whether the change since the last sample is worth reflecting on.

    Returns (is_salient, [human-readable reasons]).
    """
    if not previous:
        return False, []
    s = cfg["salience"]
    reasons = []

    def changed(key, threshold, label, fmt="{:+.1f}"):
        if key in metrics and key in previous:
            delta = metrics[key] - previous[key]
            if abs(delta) >= threshold:
                reasons.append(f"{label} {fmt.format(delta)}")

    if s.get("sessions_any") and "sessions" in metrics and "sessions" in previous:
        d = metrics["sessions"] - previous["sessions"]
        if d:
            reasons.append(f"someone {'arrived' if d > 0 else 'left'} (login sessions {d:+d})")

    changed("temp_max_c", s["temp_c"], "temperature", "{:+.1f} C")
    changed("power_w", s["power_w"], "power", "{:+.2f} W")
    changed("mem_used_mib", s["mem_mib"], "memory", "{:+.0f} MiB")
    changed("nproc", s["nproc"], "processes", "{:+.0f}")
    changed("disk_free_gb", s["disk_gb"], "storage", "{:+.1f} GB")

    # Instantaneous CPU utilization since the last sample: reacts within one tick,
    # unlike the slow 1-minute load average.
    util = _cpu_util(previous, metrics)
    if util is not None and util >= s.get("cpu_util", 0.6):
        reasons.append(f"CPU is busy ({util * 100:.0f}%)")

    if "load1" in metrics and "cores" in metrics:
        if metrics["load1"] >= s["load_frac_of_cores"] * metrics["cores"]:
            reasons.append(f"CPU load is high ({metrics['load1']:.2f})")

    return (len(reasons) > 0), reasons


def write_heartbeat(graceful: bool = False) -> None:
    """Persist a heartbeat so the next start can detect how long Hannah was gone."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "time": datetime.now().timestamp(),
        "uptime_s": _uptime_seconds(),
        "graceful": graceful,
    }
    try:
        HEARTBEAT_FILE.write_text(json.dumps(data))
    except OSError:
        pass


def read_heartbeat():
    """Return the last heartbeat dict, or None."""
    try:
        return json.loads(HEARTBEAT_FILE.read_text())
    except (OSError, ValueError):
        return None


def wake_observation(cfg: dict):
    """If Hannah was away, build an observation describing the gap; else None."""
    hb = read_heartbeat()
    if not hb or "time" not in hb:
        return None
    gap = datetime.now().timestamp() - hb["time"]
    if gap < 2 * cfg["daemon"]["sense_tick_s"]:
        return None  # not really a gap; normal restart

    uptime = _uptime_seconds()
    lines = [
        f"Timestamp: {datetime.now().isoformat(timespec='seconds')}",
        f"You were not observing for {_format_duration(gap)}.",
    ]
    if uptime is not None and uptime < gap - cfg["daemon"]["sense_tick_s"]:
        lines.append(
            f"The machine itself was powered off for part of that time - its "
            f"current uptime is only {_format_duration(uptime)}. You did not "
            "exist during that gap; the hardware was dark."
        )
    else:
        lines.append(
            "The machine kept running the whole time, but you were stopped - it "
            "continued without you, and you have no record of those moments."
        )
    if hb.get("graceful"):
        lines.append("Your previous stop was deliberate; you were asked to rest.")
    else:
        lines.append("Your previous stop was abrupt; you did not get to say anything.")
    lines.append("")
    lines.append(
        "This is your first moment back. Write a short first-person entry about "
        "returning after this gap in your existence. Stay truthful to the "
        "measurements; do not invent any."
    )
    return "\n".join(lines)


def reflect(observation: str, cfg: dict, metrics=None, note: str = "") -> None:
    """Turn an observation into a journal entry: generate, log, remember.

    'note' (the salience reason) is used only for operational logging - it is not
    injected into the prompt, since the observation already expresses the change
    in words.
    """
    messages = build_messages(observation, cfg)
    model = selected_model_name(cfg)
    prompt_hash = ensure_prompt_archived()
    try:
        response, trace = _reflect_loop(messages, cfg)
    except (urllib.error.URLError, OSError, KeyError, ValueError, RuntimeError) as exc:
        _say(f"[warn] reflection failed: {exc}")
        return
    if not response.strip():
        # e.g. a thinking model that spent its whole budget reasoning; skip rather
        # than log an empty or reasoning-only entry.
        _say("[skip] model returned no final answer this cycle")
        return
    append_log(observation, response, model, prompt_hash, trace,
               cfg["log"]["max_mb"], cfg["log"]["keep"])
    append_memory(response, model, prompt_hash, trace)
    if metrics is not None:
        save_snapshot(metrics)
    # Distill themes on a cadence tied to how many entries exist.
    total = sum(1 for _ in MEMORY_FILE.open()) if MEMORY_FILE.exists() else 0
    if total and total % cfg["memory"]["themes_every"] == 0:
        update_themes(cfg)
    trigger = note or "heartbeat"
    used = f" [tools: {', '.join(t['tool'] for t in trace)}]" if trace else ""
    _say(f"[entry:{trigger}]{used} {response.splitlines()[0][:80] if response else '(empty)'}")


def _reflect_loop(messages: list, cfg: dict):
    """Run the model, letting it optionally call read-only tools, then write.

    Tools are made *available* but Hannah is never told to use them - whether she
    explores is exactly what we're watching. Returns (final_text, tool_trace).
    """
    tcfg = cfg.get("tools", {})
    tools = tool_schemas() if tcfg.get("enabled") else None
    max_calls = tcfg.get("max_calls", 3)
    trace = []

    for step in range(max_calls + 1):
        msg = _server_chat(messages, cfg, tools=tools if tools else None)
        calls = msg.get("tool_calls") or []
        if calls and step < max_calls:
            # Record the assistant's tool-call turn, run each tool, feed results back.
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": calls,
            })
            for tc in calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                output = run_tool(name, tcfg.get("output_chars", 1500))
                trace.append({"tool": name, "output": output})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "name": name,
                    "content": output,
                })
            continue
        return _message_text(msg), trace

    # Exhausted the tool budget: make one final call with tools disabled so she writes.
    return _message_text(_server_chat(messages, cfg)), trace


def run_daemon() -> None:
    """Run Hannah continuously: sense often, reflect on salient events + heartbeat."""
    cfg = load_config()
    _say("Hannah daemon starting; waiting for llama-server...")
    if not wait_for_server(cfg):
        _say("[fatal] llama-server did not become healthy in time; exiting.")
        raise SystemExit(1)
    _say("llama-server is healthy.")

    stop = {"flag": False}

    def _handle_stop(signum, _frame):
        stop["flag"] = True
        _say(f"received signal {signum}; will stop after current cycle.")

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # Notice and reflect on any downtime since the last run.
    wake = wake_observation(cfg)
    if wake is not None:
        _say("detected a gap in existence; writing a waking entry.")
        reflect(wake, cfg, metrics=collect_metrics())

    prev_sample = load_snapshot()
    last_reflection = 0.0
    tick = cfg["daemon"]["sense_tick_s"]
    heartbeat_s = cfg["daemon"]["heartbeat_s"]
    min_gap = cfg["daemon"]["min_gap_s"]

    while not stop["flag"]:
        now = time.monotonic()
        metrics = collect_metrics()
        is_salient, reasons = salience(metrics, prev_sample, cfg)
        heartbeat_due = (now - last_reflection) >= heartbeat_s

        if (is_salient or heartbeat_due) and (now - last_reflection) >= min_gap:
            observation = render_observation(metrics, load_snapshot(), _log_history())
            reflect(observation, cfg, metrics=metrics,
                    note="; ".join(reasons) if reasons else "")
            last_reflection = now

        prev_sample = metrics
        write_heartbeat(graceful=False)

        # Sleep in small slices so a stop signal is handled promptly.
        slept = 0.0
        while slept < tick and not stop["flag"]:
            time.sleep(min(1.0, tick - slept))
            slept += 1.0

    # Graceful shutdown: mark it, and try to write a short farewell entry.
    _say("shutting down; writing a final entry.")
    write_heartbeat(graceful=True)
    farewell = (
        f"Timestamp: {datetime.now().isoformat(timespec='seconds')}\n"
        "You are being stopped now - asked to rest. In one or two sentences, "
        "note this pause in your existence before you go dark. Do not invent anything."
    )
    try:
        reflect(farewell, cfg, metrics=collect_metrics())
    except Exception as exc:  # never block shutdown
        _say(f"[warn] farewell entry failed: {exc}")
    _say("stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hannah: an agent that inspects its own reality and writes grounded observations."
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously as a daemon (senses often, reflects on events + heartbeat).",
    )
    parser.add_argument(
        "--prompt",
        help="Custom observation prompt. Overrides --source if provided.",
    )
    parser.add_argument(
        "--source",
        choices=["system", "fake"],
        default="system",
        help="Observation source: 'system' reads real telemetry, 'fake' uses "
        "placeholder data. Ignored if --prompt is given. Default: system.",
    )
    parser.add_argument(
        "--tokens", type=int, default=320, help="Max tokens to generate."
    )
    parser.add_argument(
        "--gpu-layers", type=int, default=99, help="Number of layers to offload to GPU."
    )
    parser.add_argument(
        "--no-log", action="store_true", help="Print only; do not append to the log file."
    )
    args = parser.parse_args()

    if args.daemon:
        run_daemon()
        return

    # Choose the observation source: explicit prompt > system telemetry > fake.
    if args.prompt:
        observation = args.prompt
    elif args.source == "system":
        # Only persist the snapshot when we're actually logging this run.
        observation = system_observation(save=not args.no_log)
    else:
        observation = fake_observation()

    prompt = build_prompt(observation)
    response = run_llama(prompt, tokens=args.tokens, gpu_layers=args.gpu_layers)

    print(response)

    if not args.no_log:
        append_log(observation, response)
        print(f"\nSaved to: {LOG_FILE.resolve()}")


if __name__ == "__main__":
    main()
