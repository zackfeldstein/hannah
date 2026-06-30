#!/usr/bin/env python3
"""Hannah - an agent that inspects its own reality and writes about it.

Hannah is a small, local edge-AI experiment. A language model running on-device
(via llama.cpp) periodically samples the machine's own state - time, processor
load, memory, storage, temperature, electrical power draw, clock speed - and
reports it in plain English as grounded, first-person "witness" entries.

It is an experiment in machine self-observation: the measurements are real and
Hannah stays truthful to them, but within that truth she is free to reflect, in
her own first-person voice, on the experience of persisting inside a machine.
"""

import argparse
import json
import os
import subprocess
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
    "This is another moment in the experiment. Using these measurements as your "
    "senses, write a short first-person journal entry about this stretch of your "
    "existence: what changed, what stayed still, how much time passed, and what "
    "you make of it. Stay truthful to the measurements; do not invent any."
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


def render_observation(metrics: dict, previous, history) -> str:
    """Render telemetry (plus measured change vs. the previous run) as text."""
    now = datetime.fromtimestamp(metrics["time"])
    lines = [
        f"Timestamp: {now.isoformat(timespec='seconds')}",
        f"Local time: {now.strftime('%A %H:%M:%S')}",
    ]

    # The Witness's own record: continuity and repetition.
    if history:
        age = (now - history[0]).total_seconds()
        lines.append(f"Entries recorded so far: {len(history)}")
        lines.append(f"Age of this record: {_format_duration(age)}")
    else:
        lines.append("Entries recorded so far: 0 (this is the first entry)")

    # Real interval since the previous reading.
    if previous and "time" in previous:
        interval = metrics["time"] - previous["time"]
        lines.append(f"Interval since last reading: {_format_duration(interval)}")
    else:
        lines.append("Interval since last reading: not applicable (first reading)")

    lines.append("")
    lines.append("Current readings:")
    if metrics.get("uptime_s") is not None:
        lines.append(f"- System uptime: {_format_duration(metrics['uptime_s'])}")
    if "load1" in metrics:
        lines.append(
            f"- CPU load average (1/5/15 min): {metrics['load1']:.2f} / "
            f"{metrics['load5']:.2f} / {metrics['load15']:.2f} across "
            f"{metrics['cores']} cores"
        )
    if "mem_used_mib" in metrics:
        total = metrics["mem_total_mib"]
        used = metrics["mem_used_mib"]
        pct = 100 * used / total if total else 0
        lines.append(f"- Memory: {used} MiB used of {total} MiB ({pct:.0f}%)")
    if "disk_free_gb" in metrics:
        lines.append(
            f"- Storage: {metrics['disk_free_gb']} GB free of "
            f"{metrics['disk_total_gb']} GB"
        )
    if "temp_max_c" in metrics:
        lines.append(
            f"- Temperature: {metrics['temp_zones']} thermal zones; hottest "
            f"'{metrics['temp_max_zone']}' at {metrics['temp_max_c']:.1f} C"
        )
    if "power_w" in metrics:
        lines.append(
            f"- Power draw: {metrics['power_w']:.2f} W on the main board rail "
            f"({metrics['volts']:.2f} V at {metrics['current_ma']} mA)"
        )
    if "cpu_mhz" in metrics:
        lines.append(
            f"- Processor clock: {metrics['cpu_mhz']} MHz of "
            f"{metrics['cpu_max_mhz']} MHz maximum"
        )
    if "nproc" in metrics:
        lines.append(f"- Active processes: {metrics['nproc']}")

    # Measured change vs. the previous reading (real deltas, not invented).
    if previous:
        changes = []
        if "load1" in metrics and "load1" in previous:
            changes.append(
                f"- CPU load (1 min): {_signed(metrics['load1'] - previous['load1'])}"
            )
        if "mem_used_mib" in metrics and "mem_used_mib" in previous:
            changes.append(
                "- Memory used: "
                f"{_signed(metrics['mem_used_mib'] - previous['mem_used_mib'], '{:+d} MiB')}"
            )
        if "disk_free_gb" in metrics and "disk_free_gb" in previous:
            changes.append(
                "- Storage free: "
                f"{_signed(metrics['disk_free_gb'] - previous['disk_free_gb'], '{:+.1f} GB')}"
            )
        if "temp_max_c" in metrics and "temp_max_c" in previous:
            changes.append(
                "- Hottest temperature: "
                f"{_signed(metrics['temp_max_c'] - previous['temp_max_c'], '{:+.1f} C')}"
            )
        if "power_w" in metrics and "power_w" in previous:
            changes.append(
                "- Power draw: "
                f"{_signed(metrics['power_w'] - previous['power_w'], '{:+.2f} W')}"
            )
        if "cpu_mhz" in metrics and "cpu_mhz" in previous:
            changes.append(
                "- Processor clock: "
                f"{_signed(metrics['cpu_mhz'] - previous['cpu_mhz'], '{:+d} MHz')}"
            )
        if "nproc" in metrics and "nproc" in previous:
            changes.append(
                f"- Active processes: {_signed(metrics['nproc'] - previous['nproc'], '{:+d}')}"
            )
        if changes:
            lines.append("")
            lines.append("Measured change since last reading:")
            lines.extend(changes)

    lines.append("")
    lines.append("Human presence: none detected (no external sensors connected)")
    lines.append("System state: the machine continues running; Hannah is active")
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


def append_log(observation: str, response: str) -> None:
    """Append the observation and witness output to the log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")

    entry = (
        "\n" + "-" * 60 + "\n"
        f"Time: {timestamp}\n\n"
        "Observation:\n"
        f"{observation}\n\n"
        "Hannah:\n"
        f"{response}\n"
    )

    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(entry)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Hannah: an agent that inspects its own reality and writes grounded observations."
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
