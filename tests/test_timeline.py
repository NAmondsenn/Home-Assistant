"""
Timeline Test
Tests recording interaction timings, saving them, and summarising them.
"""

import importlib.util
import json
import os
import sys

from voice_assistant import timeline as timeline_module
from voice_assistant.timeline import Timeline, load, summarise


def test_marks_are_milliseconds_since_the_start():
    timeline = Timeline()
    timeline.mark("wake", at=timeline.started)
    timeline.mark("stt_done", at=timeline.started + 1.25)
    assert timeline.marks == {"wake": 0, "stt_done": 1250}


def test_save_appends_one_line_per_interaction(tmp_path):
    path = tmp_path / "latency.jsonl"
    for chars in (10, 20):
        timeline = Timeline(follow_up=False)
        timeline.mark("wake")
        timeline.note(text_chars=chars)
        timeline.save(str(path))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["text_chars"] for line in lines] == [10, 20]
    assert lines[0]["follow_up"] is False and "wake" in lines[0]["marks_ms"]


def test_an_unwritable_log_doesnt_break_anything(tmp_path):
    timeline = Timeline()
    timeline.mark("wake")
    # A folder rather than a file, so opening it for writing fails.
    timeline.save(str(tmp_path))


def test_summarise_works_out_each_stage(tmp_path):
    path = tmp_path / "latency.jsonl"
    path.write_text("\n".join(json.dumps({"marks_ms": marks}) for marks in [
        {"wake": 0, "recording_started": 100, "speech_ended": 2000, "recording_stopped": 2800,
         "stt_done": 3400, "llm_done": 4400, "tts_done": 5000},
        {"wake": 0, "recording_started": 300, "speech_ended": 1500, "recording_stopped": 2300,
         "stt_done": 2900, "llm_done": 3900, "action_done": 4000, "tts_done": 4500},
    ]) + "\nnot json\n")

    rows = {row["stage"]: row for row in summarise(load(str(path)))}
    assert rows["Wake word to listening"]["p50"] == 200
    assert rows["Wake word to listening"]["max"] == 300
    # Starts from the action where there was one, otherwise straight from Claude.
    assert rows["Text-to-speech"]["count"] == 2 and rows["Text-to-speech"]["max"] == 600
    assert rows["End of speech to reply ready"]["p50"] == 3000
    # Stages nothing reached aren't reported at all.
    assert "Playback" not in rows


def _saved(path, count, chars=1):
    for number in range(count):
        timeline = Timeline(number=number)
        timeline.mark("wake")
        timeline.note(padding="x" * chars)
        timeline.save(str(path))


def test_the_log_is_started_afresh_once_its_big_keeping_one_old_one(tmp_path, monkeypatch):
    # It grew for ever, on both machines - the Pi's SD card, and the brain's small disk.
    monkeypatch.setattr(timeline_module, "MAX_BYTES", 1000)
    path = tmp_path / "latency.jsonl"
    _saved(path, 30, chars=50)

    assert path.stat().st_size <= 1000 + 200
    old = tmp_path / "latency.jsonl.1"
    assert old.exists() and old.stat().st_size >= 1000
    assert sorted(found.name for found in tmp_path.iterdir()) == ["latency.jsonl", "latency.jsonl.1"]
    # Nothing's lost from the newest, nor out of order across the two.
    numbers = [line["number"] for line in load(str(old)) + load(str(path))]
    assert numbers == sorted(numbers) and numbers[-1] == 29


def test_only_the_last_few_need_be_read(tmp_path, monkeypatch):
    # The settings page wants the last 200, every 20 seconds, and read the whole file each time.
    monkeypatch.setattr(timeline_module, "TAIL_BLOCK", 64)
    path = tmp_path / "latency.jsonl"
    _saved(path, 50, chars=20)
    assert [line["number"] for line in load(str(path), most=3)] == [47, 48, 49]
    assert [line["number"] for line in load(str(path), most=500)] == list(range(50))
    (tmp_path / "empty.jsonl").touch()
    assert load(str(tmp_path / "empty.jsonl"), most=3) == []


def test_the_report_reads_the_old_file_too(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "latency_report", os.path.join(os.path.dirname(__file__), "..", "scripts", "latency_report.py"))
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    monkeypatch.setattr(timeline_module, "MAX_BYTES", 1000)
    path = tmp_path / "latency.jsonl"
    _saved(path, 30, chars=50)

    both = len(load(str(path) + ".1")) + len(load(str(path)))
    assert both > len(load(str(path)))

    monkeypatch.setattr(sys, "argv", ["latency_report.py", str(path)])
    report.main()
    assert f"{both} interactions" in capsys.readouterr().out
