#!/usr/bin/env python3
"""
Soak Monitor
Keeps an eye on the assistant while it runs for days on end - its memory, CPU,
threads, restarts and log size - so anything which slowly grows, or a crash and
restart in the night, shows up rather than going unnoticed.

Run on the Pi alongside the assistant's service, and use the assistant as normal:
    venv/bin/python scripts/soak_monitor.py --hours 72
It samples every 10 minutes into logs/soak.csv, and says at the end (or on Ctrl+C)
whether anything grew that shouldn't have.
"""

import argparse
import csv
import os
import subprocess
import time
from datetime import datetime
from typing import Dict, List, Optional

# Add the project root, to find the logs folder no matter where it's run from.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIRECTORY = os.path.join(PROJECT_DIRECTORY, "logs")

SERVICE = "voice-assistant"
FIELDS = ["time", "pid", "rss_mb", "cpu_percent", "threads", "open_files", "restarts",
          "log_mb", "interactions", "temperature_c"]

# Growth over the whole run worth pointing out. Memory settling in over the first
# hour or so is normal; steady growth after that is a leak.
RSS_GROWTH_MB = 50
THREAD_GROWTH = 10


def _systemctl(prop: str) -> Optional[str]:
    """One property of the assistant's user service, e.g. MainPID."""
    try:
        result = subprocess.run(["systemctl", "--user", "show", "-p", prop, "--value", SERVICE],
                                capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def parse_status(text: str) -> Dict[str, float]:
    """
    The memory and thread count from a /proc/<pid>/status file.

    Returns:
        Dict with 'rss_mb' and 'threads'.
    """
    values = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key == "VmRSS":
            values["rss_mb"] = round(int(rest.split()[0]) / 1024, 1)
        elif key == "Threads":
            values["threads"] = int(rest.strip())
    return values


def _cpu_seconds(pid: int) -> float:
    """CPU time the process has used so far, from /proc/<pid>/stat."""
    with open(f"/proc/{pid}/stat") as f:
        fields = f.read().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _count_lines(path: str) -> int:
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def sample(previous: Optional[Dict]) -> Dict:
    """
    One reading of how the assistant is doing.

    Args:
        previous: The last reading, to work out CPU use since then.
    """
    row = {"time": datetime.now().isoformat(timespec="seconds")}
    pid = int(_systemctl("MainPID") or 0)
    row["pid"] = pid or ""
    row["restarts"] = _systemctl("NRestarts") or ""

    if pid:
        with open(f"/proc/{pid}/status") as f:
            row.update(parse_status(f.read()))
        row["open_files"] = len(os.listdir(f"/proc/{pid}/fd"))
        cpu = _cpu_seconds(pid)
        if previous and previous.get("pid") == pid and "_cpu" in previous:
            elapsed = time.monotonic() - previous["_at"]
            row["cpu_percent"] = round(100 * (cpu - previous["_cpu"]) / elapsed, 1)
        row["_cpu"], row["_at"] = cpu, time.monotonic()

    log = os.path.join(LOGS_DIRECTORY, "smart_assistant.log")
    row["log_mb"] = round(os.path.getsize(log) / 1_000_000, 2) if os.path.exists(log) else 0
    row["interactions"] = _count_lines(os.path.join(LOGS_DIRECTORY, "latency.jsonl"))
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            row["temperature_c"] = round(int(f.read()) / 1000, 1)
    except OSError:
        pass
    return row


def summarise(rows: List[Dict]) -> List[str]:
    """
    What's worth knowing about a run - the lines to print at the end.

    Args:
        rows: The readings, oldest first.
    """
    running = [row for row in rows if row.get("pid")]
    if not running:
        return ["The assistant wasn't running for any of the readings."]

    first, last = running[0], running[-1]
    hours = (datetime.fromisoformat(last["time"]) - datetime.fromisoformat(first["time"])).total_seconds() / 3600
    lines = [f"{len(rows)} readings over {hours:.1f} hours, "
             f"{int(last['interactions']) - int(first['interactions'])} interactions."]
    warnings = []

    rss = [float(row["rss_mb"]) for row in running if row.get("rss_mb") not in (None, "")]
    if rss:
        lines.append(f"Memory: {rss[0]:.0f} MB at the start, {rss[-1]:.0f} MB at the end, {max(rss):.0f} MB at most.")
        if rss[-1] - rss[0] > RSS_GROWTH_MB:
            warnings.append(f"Memory grew by {rss[-1] - rss[0]:.0f} MB - worth looking for a leak.")

    cpu = [float(row["cpu_percent"]) for row in running if row.get("cpu_percent") not in (None, "")]
    if cpu:
        lines.append(f"CPU: {sum(cpu) / len(cpu):.0f}% of one core on average, {max(cpu):.0f}% at most.")
    lines.append(f"Log file: {float(last['log_mb']):.1f} MB.")

    threads = [int(row["threads"]) for row in running if row.get("threads") not in (None, "")]
    if threads and threads[-1] - threads[0] > THREAD_GROWTH:
        warnings.append(f"Threads went from {threads[0]} to {threads[-1]} - something isn't finishing.")

    restarts = [int(row["restarts"]) for row in rows if str(row.get("restarts", "")).isdigit()]
    if restarts and restarts[-1] > restarts[0]:
        warnings.append(f"The service restarted {restarts[-1] - restarts[0]} time(s) - check the journal.")
    elif len({row["pid"] for row in running}) > 1:
        warnings.append("The assistant's process changed during the run, so it stopped at least once.")

    if not running == rows:
        warnings.append(f"It wasn't running for {len(rows) - len(running)} of the readings.")

    return lines + [f"! {warning}" for warning in warnings] + ([] if warnings else ["Nothing grew that shouldn't have."])


def main():
    parser = argparse.ArgumentParser(description="Keeps an eye on the assistant while it runs for days.")
    parser.add_argument("--hours", type=float, default=72, help="how long to watch for (default 72)")
    parser.add_argument("--minutes", type=float, default=10, help="time between readings (default 10)")
    parser.add_argument("--out", default=os.path.join(LOGS_DIRECTORY, "soak.csv"), help="where to write the readings")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    rows, previous = [], None
    end = time.monotonic() + args.hours * 3600
    print(f"Watching {SERVICE} for {args.hours:g} hours, every {args.minutes:g} minutes, into {args.out}")

    try:
        with open(args.out, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            writer.writeheader()
            while True:
                previous = sample(previous)
                rows.append(previous)
                writer.writerow(previous)
                f.flush()
                if time.monotonic() >= end:
                    break
                time.sleep(args.minutes * 60)
    except KeyboardInterrupt:
        print("\nStopped early.")

    print()
    for line in summarise(rows):
        print(line)


if __name__ == "__main__":
    main()
