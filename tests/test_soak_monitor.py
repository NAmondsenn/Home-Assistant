"""
Soak Monitor Test
Checks the soak monitor reads /proc correctly, and points out what it should at the
end of a run.
"""

import importlib.util
import os
import subprocess
import sys
import types
from datetime import datetime

import pytest

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


def _scratch_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(soak_monitor, "LOGS_DIRECTORY", str(tmp_path))
    return tmp_path


def test_the_brain_is_watched_as_a_system_service(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="4242\n", stderr="")

    monkeypatch.setattr(soak_monitor.subprocess, "run", fake_run)
    assert soak_monitor._systemctl("MainPID", soak_monitor.TARGETS["brain"]) == "4242"
    assert "nova-backend" in calls[0] and "--user" not in calls[0]

    soak_monitor._systemctl("MainPID", soak_monitor.TARGETS["satellite"])
    assert "voice-assistant" in calls[1] and "--user" in calls[1]


def test_errors_are_counted_since_the_last_reading(tmp_path, monkeypatch):
    logs = _scratch_logs(tmp_path, monkeypatch)
    (logs / "backend.log").write_text(
        "2026-10-20 09:59:59 - voice_assistant.x - ERROR - before the cut-off\n"
        "2026-10-20 10:00:00 - voice_assistant.x - ERROR - exactly at it\n"
        "2026-10-20 10:30:00 - voice_assistant.x - INFO - fine\n"
        "2026-10-20 11:00:00 - voice_assistant.x - CRITICAL - after it\n"
        "Traceback (most recent call last): - ERROR - not a line of its own\n"
        "ValueError: a message's second line - ERROR - skipped too\n")
    (logs / "backend.log.1").write_text(
        "2026-10-20 10:15:00 - voice_assistant.x - ERROR - in the rotated file\n"
        "2026-10-19 10:15:00 - voice_assistant.x - ERROR - a day earlier\n")
    end = datetime(2026, 10, 21)
    assert soak_monitor.count_errors("backend.log", datetime(2026, 10, 20, 10, 0, 0), end) == 3
    assert soak_monitor.count_errors("backend.log", datetime(2026, 10, 20, 10, 45, 0), end) == 1
    assert soak_monitor.count_errors("missing.log", datetime(2026, 10, 20), end) == 0
    # One stamped the very second a reading is taken is left to the next, which counts
    # from that second - so it's counted once, not twice or never.
    assert soak_monitor.count_errors("backend.log", datetime(2026, 10, 20, 10, 0, 0),
                                     datetime(2026, 10, 20, 11, 0, 0)) == 2
    assert soak_monitor.count_errors("backend.log", datetime(2026, 10, 20, 11, 0, 0), end) == 1


def test_a_missing_log_stops_the_monitor_before_it_starts(tmp_path, monkeypatch, capsys):
    logs = _scratch_logs(tmp_path, monkeypatch)
    out = logs / "nested" / "soak.csv"
    monkeypatch.setattr(sys, "argv", ["soak_monitor.py", "--brain", "--out", str(out)])
    with pytest.raises(SystemExit) as stopped:
        soak_monitor.main()
    assert stopped.value.code == 1
    assert "No backend.log" in capsys.readouterr().out
    assert not out.exists() and not out.parent.exists()


def test_interactions_count_the_rotated_latency_file_too(tmp_path, monkeypatch):
    logs = _scratch_logs(tmp_path, monkeypatch)
    (logs / "latency.jsonl").write_text("{}\n{}\n")
    (logs / "latency.jsonl.1").write_text("{}\n{}\n{}\n")
    monkeypatch.setattr(soak_monitor, "_systemctl", lambda prop, target: None)
    row = soak_monitor.sample(None, soak_monitor.TARGETS["satellite"], datetime(2026, 10, 20))
    assert row["interactions"] == 5


def test_a_log_which_vanishes_between_the_checks_is_recorded_as_empty(tmp_path, monkeypatch):
    logs = _scratch_logs(tmp_path, monkeypatch)
    (logs / "latency.jsonl").write_text("")
    target = soak_monitor.TARGETS["satellite"]
    (logs / target.log).write_text("x")

    def rotated_away(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(soak_monitor, "_systemctl", lambda prop, target: None)
    monkeypatch.setattr(soak_monitor.os.path, "getsize", rotated_away)
    row = soak_monitor.sample(None, target, datetime(2026, 10, 20))
    assert row["log_mb"] == 0


def test_growing_open_files_and_any_errors_are_pointed_out():
    first = {**_reading(0, 400), "open_files": 30, "errors": 0}
    last = {**_reading(12, 401), "open_files": 55, "errors": 3}
    middle = {**_reading(6, 400), "open_files": 40, "errors": 4}
    lines = soak_monitor.summarise([first, middle, last])
    assert "Errors: 7 logged, at most 4 between two readings." in lines
    assert "! Open files went from 30 to 55 - something isn't being closed." in lines
    assert "! 7 errors logged - read them in the log." in lines

    # An old soak.csv has no errors column, and says nothing about them.
    old = soak_monitor.summarise([_reading(0, 400), _reading(12, 410)])
    assert not any("rror" in line for line in old)


def test_the_brains_summary_leaves_interactions_out():
    lines = soak_monitor.summarise([_reading(0, 400), _reading(12, 410)], brain=True)
    assert lines[0] == "2 readings over 12.0 hours."


@pytest.mark.parametrize("gone_at", ["status", "fd", "stat"])
def test_a_service_gone_mid_reading_is_recorded_as_not_running(tmp_path, monkeypatch, gone_at):
    # The service exits or restarts between systemctl giving its pid and /proc being read.
    # Under nohup a crash here would end the monitor unseen, for the rest of the soak.
    _scratch_logs(tmp_path, monkeypatch)
    (tmp_path / "backend.log").write_text("")
    monkeypatch.setattr(soak_monitor, "_systemctl",
                        lambda prop, target: str(os.getpid()) if prop == "MainPID" else "3")
    real_open, real_listdir = open, os.listdir

    def gone(path):
        raise FileNotFoundError(2, "No such file or directory", path)

    if gone_at == "fd":
        monkeypatch.setattr(soak_monitor.os, "listdir",
                            lambda path: gone(path) if path.startswith("/proc/") else real_listdir(path))
    else:
        monkeypatch.setattr("builtins.open", lambda path, *args, **kwargs: gone(path)
                            if str(path).endswith(f"/{gone_at}") else real_open(path, *args, **kwargs))

    row = soak_monitor.sample(None, soak_monitor.TARGETS["brain"], datetime(2026, 10, 20))

    assert row["pid"] == "" and row["restarts"] == "3"
    assert not any(field in row for field in ("rss_mb", "threads", "open_files", "_cpu"))
    lines = soak_monitor.summarise([_reading(0, 400), row, _reading(2, 401, pid=2001)], brain=True)
    assert "! It wasn't running for 1 of the readings." in lines


@pytest.fixture
def clock(monkeypatch):
    """The monitor's datetime.now(), which returns clock.at - set and moved on by the test."""
    state = types.SimpleNamespace(at=datetime(2026, 10, 20, 10, 0, 0))

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.combine(state.at.date(), state.at.time())

    monkeypatch.setattr(soak_monitor, "datetime", Fixed)
    return state


def test_a_reading_counts_whole_seconds_up_to_its_own(tmp_path, monkeypatch, clock):
    logs = _scratch_logs(tmp_path, monkeypatch)
    monkeypatch.setattr(soak_monitor, "_systemctl", lambda prop, target: None)
    (logs / "backend.log").write_text(
        "2026-10-20 10:10:00 - voice_assistant.x - ERROR - the second of the reading\n")
    clock.at = datetime(2026, 10, 20, 10, 10, 0, 700000)

    row = soak_monitor.sample(None, soak_monitor.TARGETS["brain"], datetime(2026, 10, 20, 10, 0, 0))

    assert row["_when"] == datetime(2026, 10, 20, 10, 10, 0) and row["time"] == "2026-10-20T10:10:00"
    assert row["errors"] == 0
    clock.at = datetime(2026, 10, 20, 10, 20, 0, 100000)
    assert soak_monitor.sample(row, soak_monitor.TARGETS["brain"], row["_when"])["errors"] == 1


def test_errors_are_counted_from_the_start_then_from_each_reading(tmp_path, monkeypatch, clock):
    logs = _scratch_logs(tmp_path, monkeypatch)
    (logs / "backend.log").write_text("")
    clock.at = datetime(2026, 10, 20, 10, 0, 0, 500000)
    since = []

    def reading(previous, target, counting_from):
        since.append(counting_from)
        if len(since) == 3:
            raise KeyboardInterrupt
        return {"time": f"2026-10-20T1{len(since)}:00:00", "pid": "",
                "_when": datetime(2026, 10, 20, 10 + len(since), 0, 0)}

    monkeypatch.setattr(soak_monitor, "sample", reading)
    monkeypatch.setattr(soak_monitor.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sys, "argv", ["soak_monitor.py", "--brain", "--out", str(logs / "soak.csv")])

    soak_monitor.main()

    # The start, cut to the second as each reading's time is, and then each reading's own.
    assert since == [datetime(2026, 10, 20, 10, 0, 0), datetime(2026, 10, 20, 11, 0, 0),
                     datetime(2026, 10, 20, 12, 0, 0)]


def test_a_rotation_between_readings_counts_each_error_once(tmp_path, monkeypatch, clock):
    logs = _scratch_logs(tmp_path, monkeypatch)
    monkeypatch.setattr(soak_monitor, "_systemctl", lambda prop, target: None)
    (logs / "backend.log").write_text("2026-10-20 10:05:00 - voice_assistant.x - ERROR - first\n")
    clock.at = datetime(2026, 10, 20, 10, 10, 0)
    first = soak_monitor.sample(None, soak_monitor.TARGETS["brain"], datetime(2026, 10, 20, 10, 0, 0))

    # One more is logged, then the log's handler rotates it: the file moves to .1, with
    # that one in it, and a new one is started.
    with open(logs / "backend.log", "a") as f:
        f.write("2026-10-20 10:12:00 - voice_assistant.x - ERROR - second, before the rotation\n")
    os.rename(logs / "backend.log", logs / "backend.log.1")
    (logs / "backend.log").write_text("2026-10-20 10:15:00 - voice_assistant.x - ERROR - third, after it\n")
    clock.at = datetime(2026, 10, 20, 10, 20, 0)
    second = soak_monitor.sample(first, soak_monitor.TARGETS["brain"], first["_when"])

    assert (first["errors"], second["errors"]) == (1, 2)


def test_only_a_line_whose_own_level_is_error_or_critical_is_counted(tmp_path, monkeypatch):
    # As the log writes them, with milliseconds after the stamp. A message can quote the
    # text of another line - the settings page's last error, say - and mustn't count as one.
    logs = _scratch_logs(tmp_path, monkeypatch)
    (logs / "backend.log").write_text(
        "2026-10-20 10:05:00,123 - voice_assistant.x - INFO - Showed ' - ERROR - ' to the page\n"
        "2026-10-20 10:06:00,456 - voice_assistant.x - WARNING - Last was 'x - CRITICAL - y'\n"
        "2026-10-20 10:07:00,789 - voice_assistant.x - ERROR - A real one, quoting ' - ERROR - ' too\n"
        "2026-10-20 10:08:00,001 - voice_assistant.x - CRITICAL - Another real one\n"
        "2026-10-20 10:09:00,002 - voice_assistant.x - ERRORS - not a level either\n")

    assert soak_monitor.count_errors("backend.log", datetime(2026, 10, 20), datetime(2026, 10, 21)) == 2


def _rotate(logs):
    """What the log's handler does when the file is full: each file moves along one name, and a new one starts."""
    for number in (2, 1):
        if (logs / f"backend.log.{number}").exists():
            os.replace(logs / f"backend.log.{number}", logs / f"backend.log.{number + 1}")
    os.replace(logs / "backend.log", logs / "backend.log.1")
    (logs / "backend.log").write_text("")


@pytest.mark.parametrize("rotates_before", [".1", ".2"])
def test_a_rotation_during_one_count_reads_each_file_once(tmp_path, monkeypatch, rotates_before):
    # Counting opens the log and then its older files in turn, so one which rotates part way
    # moves the file just read to the next name, where it would be read a second time.
    logs = _scratch_logs(tmp_path, monkeypatch)
    (logs / "backend.log").write_text("2026-10-20 10:30:00,000 - voice_assistant.x - ERROR - newest\n")
    (logs / "backend.log.1").write_text(
        "2026-10-20 10:10:00,000 - voice_assistant.x - ERROR - older\n"
        "2026-10-20 10:11:00,000 - voice_assistant.x - ERROR - older still\n")
    real_open = open
    rotated = []

    def rotating_open(path, *args, **kwargs):
        if str(path).endswith(f"backend.log{rotates_before}") and not rotated:
            rotated.append(path)
            _rotate(logs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", rotating_open)

    count = soak_monitor.count_errors("backend.log", datetime(2026, 10, 20), datetime(2026, 10, 21))

    assert rotated
    # Each of the three is counted once: none twice, and none lost to the move.
    assert count == 3
