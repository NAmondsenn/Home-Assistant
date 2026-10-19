"""
Timeline Test
Tests recording interaction timings, saving them, and summarising them.
"""

import json

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
