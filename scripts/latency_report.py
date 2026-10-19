#!/usr/bin/env python3
"""
Latency Report
Summarises logs/latency.jsonl: for each stage of an interaction, how long it
usually takes (p50), how long it takes on a bad day (p95), and the worst seen.

Run on the Pi once the assistant has been used for a while:
    venv/bin/python scripts/latency_report.py
or point it at another copy of the log:
    venv/bin/python scripts/latency_report.py path/to/latency.jsonl
"""

import os
import sys

# Add the project root to the path so the voice_assistant package imports work
# no matter where the script is run from.
PROJECT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIRECTORY)

from voice_assistant.timeline import load, summarise


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT_DIRECTORY, "logs", "latency.jsonl")
    if not os.path.exists(path):
        print(f"No latency log at {path} yet - use the assistant for a while first.")
        return

    timelines = load(path)
    rows = summarise(timelines)
    print(f"\n{len(timelines)} interactions from {path}\n")
    print(f"{'Stage':34} {'count':>6} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8}")
    print("-" * 68)
    for row in rows:
        print(f"{row['stage']:34} {row['count']:>6} {row['p50']:>8} {row['p95']:>8} {row['max']:>8}")
    print()


if __name__ == "__main__":
    main()
