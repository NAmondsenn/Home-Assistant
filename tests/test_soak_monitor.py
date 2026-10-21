"""
Soak Monitor Test
Checks the soak monitor reads /proc correctly, and points out what it should at the
end of a run.
"""

import importlib.util
import os

spec = importlib.util.spec_from_file_location(
    "soak_monitor", os.path.join(os.path.dirname(__file__), "..", "scripts", "soak_monitor.py"))
soak_monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(soak_monitor)


def test_memory_and_threads_are_read_from_proc_status():
    status = "Name:\tpython\nVmRSS:\t  412160 kB\nThreads:\t21\n"
    assert soak_monitor.parse_status(status) == {"rss_mb": 402.5, "threads": 21}


def _reading(hour, rss, threads=21, restarts=0, pid=1324, interactions=0):
    return {"time": f"2026-10-20T{hour:02d}:00:00", "pid": pid, "rss_mb": rss, "threads": threads,
            "restarts": str(restarts), "cpu_percent": 12.0, "log_mb": 1.2, "interactions": interactions}


def test_a_steady_run_says_nothing_grew():
    lines = soak_monitor.summarise([_reading(0, 400), _reading(12, 410, interactions=40)])
    assert lines[0] == "2 readings over 12.0 hours, 40 interactions."
    assert lines[-1] == "Nothing grew that shouldn't have."


def test_growth_and_restarts_are_pointed_out():
    lines = soak_monitor.summarise([_reading(0, 400), _reading(23, 520, threads=40, restarts=2, pid=2001)])
    warnings = [line for line in lines if line.startswith("!")]
    assert len(warnings) == 3
    assert "grew by 120 MB" in warnings[0] and "21 to 40" in warnings[1] and "restarted 2" in warnings[2]


def test_a_reading_while_it_was_down_is_pointed_out():
    lines = soak_monitor.summarise([_reading(0, 400), {**_reading(1, ""), "pid": ""}, _reading(2, 401)])
    assert any("wasn't running for 1" in line for line in lines)
