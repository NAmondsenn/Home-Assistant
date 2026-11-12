"""
Usage Test
Tests the log of Nova's calls to Claude: what a call costs, what kind of request it was,
and the summary worked out from the log - windows, buckets, month and projection, the
spending alert, and what's kept across a restart.

The clock is faked, and the days are local time, so the tests build their times from
local dates and pass in London and under UTC.
"""

import json
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from voice_assistant.usage import UsageLog, cost_in_dollars, kind_of

SETTINGS = {"input_price": 1.0, "output_price": 5.0, "search_price": 10.0,
            "pounds_per_dollar": 1.0, "daily_limit": 0}

HOUR = 3600
DAY = 86400


def local(year, month, day, hour=0, minute=0) -> float:
    """A local time as seconds since 1970."""
    return datetime(year, month, day, hour, minute).timestamp()


class Clock:
    """A clock which only moves when told to."""

    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def reply(output=0, input=0, cache_read=0, cache_write=0, searches=0, tools=()):
    """A response from Anthropic with just the parts the log reads."""
    usage = SimpleNamespace(
        input_tokens=input, output_tokens=output, cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write, cache_creation=None,
        server_tool_use=SimpleNamespace(web_search_requests=searches))
    content = [SimpleNamespace(type="text", text="words which must never be kept")]
    content += [SimpleNamespace(type="tool_use", name=name, input={"secret": "words"}) for name in tools]
    return SimpleNamespace(usage=usage, content=content)


def dollar_reply(dollars: float):
    """A response which cost this many dollars at the test prices: output tokens at $5 a million."""
    return reply(output=round(dollars / 5 * 1_000_000))


def make_log(tmp_path, clock, on_over_limit=None, **settings):
    return UsageLog(str(tmp_path / "usage.jsonl"), lambda: {**SETTINGS, **settings},
                    on_over_limit=on_over_limit, clock=clock)


def test_kind_of():
    assert kind_of(["set_lights"], 0) == "Lights and devices"
    assert kind_of(["play_music"], 1) == "Web search"
    assert kind_of([], 0) == "Chat and questions"
    assert kind_of(["remember"], 0) == "Other"
    assert kind_of(["set_timer", "play_music"], 0) == "Timers and alarms"
    assert kind_of(["play_playlist"], 0) == "Music"
    assert kind_of(["manage_list"], 0) == "Calendar and lists"


def test_cost_in_dollars():
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_in_dollars(usage, SETTINGS) == 6.0
    usage.cache_read_input_tokens = 1_000_000
    assert abs(cost_in_dollars(usage, SETTINGS) - 6.1) < 1e-9
    usage.cache_creation = SimpleNamespace(ephemeral_5m_input_tokens=None, ephemeral_1h_input_tokens=1_000_000)
    assert abs(cost_in_dollars(usage, SETTINGS) - 8.1) < 1e-9
    usage.server_tool_use = SimpleNamespace(web_search_requests=1000)
    assert abs(cost_in_dollars(usage, SETTINGS) - 18.1) < 1e-9
    empty = SimpleNamespace(input_tokens=None, output_tokens=None, cache_read_input_tokens=None,
                            cache_creation_input_tokens=None, cache_creation=None, server_tool_use=None)
    assert cost_in_dollars(empty, SETTINGS) == 0.0


def test_cache_writes_without_a_breakdown_are_five_minute_ones():
    usage = SimpleNamespace(cache_creation_input_tokens=1_000_000)
    assert abs(cost_in_dollars(usage, SETTINGS) - 1.25) < 1e-9
    five = SimpleNamespace(cache_creation=SimpleNamespace(ephemeral_5m_input_tokens=1_000_000,
                                                          ephemeral_1h_input_tokens=0))
    assert abs(cost_in_dollars(five, SETTINGS) - 1.25) < 1e-9


def test_a_price_change_leaves_what_was_already_recorded(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    prices = dict(SETTINGS)
    log = UsageLog(str(tmp_path / "usage.jsonl"), lambda: prices, clock=clock)
    log.record(reply(output=1_000_000), "haiku")
    prices["output_price"] = 50.0
    assert log.summary()["today_pounds"] == 5.0
    log.record(reply(output=1_000_000), "haiku")
    assert log.summary()["today_pounds"] == 55.0


def test_windows_and_models(tmp_path):
    now = local(2026, 11, 12, 12, 30)
    clock = Clock(now - 20 * DAY)
    log = make_log(tmp_path, clock)
    log.record(reply(output=100, input=10, cache_read=5, cache_write=5), "haiku")
    clock.now = now - 3 * DAY
    log.record(reply(output=200, input=10, cache_read=5, cache_write=5), "sonnet")
    clock.now = now - HOUR
    log.record(reply(output=400, input=10, cache_read=5, cache_write=5), "haiku")
    clock.now = now

    windows = log.summary()["windows"]
    assert windows["24h"] == {"output": 400, "total": 420, "pounds": windows["24h"]["pounds"], "requests": 1}
    assert (windows["7d"]["output"], windows["7d"]["requests"]) == (600, 2)
    assert (windows["30d"]["output"], windows["30d"]["requests"]) == (700, 3)
    assert windows["30d"]["total"] == 760
    assert log.summary()["requests_24h"] == 1
    models = log.summary()["models"]
    assert models["24h"] == {"haiku": 400}
    assert models["7d"] == {"haiku": 400, "sonnet": 200}
    assert models["30d"] == {"haiku": 500, "sonnet": 200}
    assert list(models["7d"]) == ["haiku", "sonnet"]


def test_cache_share(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    assert log.summary()["cache_share_24h"] is None
    log.record(reply(input=100, cache_read=300, cache_write=100), "haiku")
    assert log.summary()["cache_share_24h"] == 0.6


def test_today_month_projection(tmp_path):
    clock = Clock(local(2026, 11, 3, 9))
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(1.0), "haiku")
    clock.now = local(2026, 11, 10, 12)
    summary = log.summary()
    assert summary["today_pounds"] == 0.0
    assert summary["month_pounds"] == 1.0
    # Nine and a half days have gone, of a month of 30.
    assert summary["month_projection_pounds"] == round(1.0 / 9.5 * 30, 4)


def test_pounds_use_the_rate_and_are_rounded(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock, pounds_per_dollar=0.79, daily_limit=0.25)
    log.record(dollar_reply(1.0), "haiku")
    summary = log.summary()
    assert summary["today_pounds"] == 0.79
    assert summary["daily_limit_pounds"] == 0.25
    assert summary["kinds_month"] == {"Chat and questions": 0.79}


def test_kinds_month_largest_first(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    for dollars, tool in ((0.1, "set_lights"), (0.5, "play_music"), (0.3, None)):
        response = dollar_reply(dollars)
        if tool:
            response.content.append(SimpleNamespace(type="tool_use", name=tool))
        log.record(response, "haiku")
    assert list(log.summary()["kinds_month"].items()) == [
        ("Music", 0.5), ("Chat and questions", 0.3), ("Lights and devices", 0.1)]


def test_hourly_and_daily_buckets(tmp_path):
    now = local(2026, 11, 12, 12, 30)
    clock = Clock(now - 2 * HOUR)
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(1.0), "haiku")
    clock.now = now
    log.record(dollar_reply(2.0), "haiku")
    summary = log.summary()

    hourly, daily = summary["hourly_pounds"], summary["daily_pounds"]
    assert len(hourly) == 24 and len(daily) == 30
    this_hour = datetime(2026, 11, 12, 12).astimezone().isoformat(timespec="seconds")
    assert hourly[-1] == [this_hour, 2.0]
    assert hourly[-3][1] == 1.0
    assert sum(spent for _, spent in hourly) == 3.0
    assert hourly[0][1] == 0.0
    assert daily[-1] == ["2026-11-12", 3.0]
    assert daily[0] == ["2026-10-14", 0.0]
    assert sum(spent for _, spent in daily[:-1]) == 0.0


def test_hourly_buckets_across_clock_change(tmp_path):
    # BST ended at 02:00 on 25 Oct 2026, so the 24 hours before noon hold an hour twice
    # in London. Under UTC there's no change, and the same must hold.
    now = local(2026, 10, 25, 12)
    clock = Clock(local(2026, 10, 25, 2, 30))
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(1.0), "haiku")
    clock.now = now
    hourly = log.summary()["hourly_pounds"]
    assert len(hourly) == 24
    assert len({hour for hour, _ in hourly}) == 24
    assert sum(spent for _, spent in hourly) == 1.0
    assert hourly[-1][0] == datetime(2026, 10, 25, 12).astimezone().isoformat(timespec="seconds")


def test_survives_restart(tmp_path):
    clock = Clock(local(2026, 11, 12, 8))
    log = make_log(tmp_path, clock)
    for hours in (0, 1, 2):
        clock.now = local(2026, 11, 12, 8 + hours)
        log.record(reply(output=1000 * (hours + 1), input=50, searches=hours, tools=["set_timer"]), "haiku")
    before = log.summary()
    assert before["requests_24h"] == 3

    again = make_log(tmp_path, clock)
    assert again.summary() == before


def test_skips_broken_line(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    path = tmp_path / "usage.jsonl"
    good = {"at": clock.now - 60, "model": "haiku", "input": 1, "output": 2, "cache_read": 0,
            "cache_write": 0, "searches": 0, "kind": "Chat and questions", "dollars": 0.5}
    path.write_text("not json\n" + json.dumps(good) + "\n" + json.dumps({"at": clock.now}) + "\n"
                    + json.dumps(good) + '\n{"at": 17')
    log = make_log(tmp_path, clock)
    assert log.summary()["requests_24h"] == 2
    # The next line must not be glued on to the half one: the file is made whole first.
    log.record(dollar_reply(1.0), "haiku")
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line) for line in lines)
    assert make_log(tmp_path, clock).summary()["requests_24h"] == 3


def test_skips_lines_with_impossible_times_or_costs(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    path = tmp_path / "usage.jsonl"
    good = {"at": clock.now - 60, "model": "haiku", "input": 1, "output": 2, "cache_read": 0,
            "cache_write": 0, "searches": 0, "kind": "Chat and questions", "dollars": 0.5}
    bad = [{**good, "at": 1e20}, {**good, "at": 1.0}, {**good, "dollars": float("nan")}]
    lines = [json.dumps(line) for line in bad]
    # json.dumps writes Infinity for the time, and an integer field which is infinite too.
    lines.append(json.dumps({**good, "at": float("inf")}))
    lines.append(json.dumps({**good, "output": float("inf")}))
    path.write_text("\n".join(lines + [json.dumps(good)]) + "\n")
    log = make_log(tmp_path, clock)
    summary = log.summary()
    assert summary["requests_24h"] == 1
    assert summary["today_pounds"] == 0.5


def test_a_log_which_couldnt_be_read_is_only_appended_to(tmp_path, monkeypatch):
    clock = Clock(local(2026, 11, 12, 12))
    path = tmp_path / "usage.jsonl"
    old = {"at": clock.now - 10 * DAY, "model": "haiku", "input": 1, "output": 2, "cache_read": 0,
           "cache_write": 0, "searches": 0, "kind": "Chat and questions", "dollars": 0.5}
    path.write_text(json.dumps(old) + "\n")

    real_open = open

    def failing_open(file, *args, **kwargs):
        if str(file) == str(path) and not args and not kwargs:
            raise PermissionError("not now")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    log = make_log(tmp_path, clock)
    monkeypatch.setattr("builtins.open", real_open)
    log.record(dollar_reply(1.0), "haiku")
    clock.now += 2 * DAY
    log.record(dollar_reply(1.0), "haiku")
    # The ten-day-old line is still there: neither first call of a day rewrote the file.
    times = [json.loads(line)["at"] for line in path.read_text().splitlines()]
    assert times == [old["at"], clock.now - 2 * DAY, clock.now]


def test_a_kind_given_is_used_instead_of_the_responses_own(tmp_path):
    # A turn's answer after a read has no tool in it, and would be "Chat and questions"
    # without the kind of the turn's first call being given.
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    log.record(reply(output=10), "haiku", kind="Music")
    log.record(reply(output=10, tools=["set_timer"]), "haiku", kind="Music")
    log.record(reply(output=10), "haiku")
    assert [json.loads(line)["kind"] for line in (tmp_path / "usage.jsonl").read_text().splitlines()] == [
        "Music", "Music", "Chat and questions"]


def test_record_later_records_in_order_off_the_callers_thread(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    log.record_later(reply(output=10), "haiku", "Music")
    log.record_later(reply(output=20), "haiku")
    log.flush()
    lines = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert [(line["output"], line["kind"]) for line in lines] == [(10, "Music"), (20, "Chat and questions")]


def test_record_later_never_waits_for_the_log(tmp_path):
    # Recording can wait - on the disk, or on the lock while a summary is worked out - and the
    # caller, which is a reply, must not wait with it.
    clock = Clock(local(2026, 11, 12, 12))
    release = threading.Event()

    def blocked_settings():
        release.wait(10)
        return SETTINGS

    log = UsageLog(str(tmp_path / "usage.jsonl"), blocked_settings, clock=clock)
    try:
        log.record_later(reply(output=10), "haiku")
        assert not (tmp_path / "usage.jsonl").exists()
    finally:
        release.set()
    log.flush()
    assert log.summary()["requests_24h"] == 1


def test_record_later_uses_one_thread_for_all_calls_and_never_raises(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    log.record_later(object(), "haiku")
    log.record_later(reply(output=10), "haiku")
    first = log._worker
    log.record_later(reply(output=10), "haiku")
    log.flush()
    # A call which can't be recorded doesn't stop the thread, and there's still only the one.
    assert log._worker is first and first.is_alive()
    assert log.summary()["requests_24h"] == 2


def test_record_later_drops_calls_rather_than_grow_without_end(tmp_path, monkeypatch):
    from voice_assistant import usage as usage_module
    monkeypatch.setattr(usage_module, "QUEUE_SIZE", 2)
    clock = Clock(local(2026, 11, 12, 12))
    release = threading.Event()
    log = UsageLog(str(tmp_path / "usage.jsonl"), lambda: (release.wait(10), SETTINGS)[1], clock=clock)
    try:
        for _ in range(6):
            log.record_later(reply(output=10), "haiku")
    finally:
        release.set()
    log.flush()
    assert 2 <= log.summary()["requests_24h"] < 6


def test_a_thread_which_cant_start_isnt_kept_or_waited_on(tmp_path, monkeypatch):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)

    def fail(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", fail)
    log.record_later(reply(output=10), "haiku")
    assert log._worker is None
    log.flush()


def test_record_without_usage(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    log.record(SimpleNamespace(content=[]), "haiku")
    log.record(SimpleNamespace(usage=None, content=[]), "haiku")
    log.record(object(), "haiku")
    assert log.summary()["requests_24h"] == 0
    assert not (tmp_path / "usage.jsonl").exists()


def test_record_never_raises(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))

    def broken():
        raise RuntimeError("settings are unavailable")

    log = UsageLog(str(tmp_path / "usage.jsonl"), broken, clock=clock)
    log.record(reply(output=10), "haiku")
    # And a file which can't be written to loses the line from the file but not from memory.
    blocked = tmp_path / "file"
    blocked.write_text("a file, not a folder")
    log = UsageLog(str(blocked / "usage.jsonl"), lambda: SETTINGS, clock=clock)
    log.record(reply(output=10), "haiku")
    assert log.summary()["requests_24h"] == 1


def test_only_counts_and_kinds_are_kept(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    log.record(reply(output=10, tools=["set_lights"]), "haiku")
    text = (tmp_path / "usage.jsonl").read_text()
    assert "words" not in text and "secret" not in text
    assert set(json.loads(text)) == {"at", "model", "input", "output", "cache_read", "cache_write",
                                     "searches", "kind", "dollars"}


def test_alert_once_on_crossing(tmp_path):
    clock = Clock(local(2026, 11, 12, 9))
    alert = Mock()
    log = make_log(tmp_path, clock, on_over_limit=alert, daily_limit=0.25)
    log.record(dollar_reply(0.20), "haiku")
    alert.assert_not_called()
    log.record(dollar_reply(0.10), "haiku")
    alert.assert_called_once_with(0.30, 0.25)
    clock.now = local(2026, 11, 12, 18)
    log.record(dollar_reply(0.10), "haiku")
    alert.assert_called_once()

    # Already over the limit when the brain starts: not told again.
    restarted = Mock()
    again = make_log(tmp_path, clock, on_over_limit=restarted, daily_limit=0.25)
    again.record(dollar_reply(0.10), "haiku")
    restarted.assert_not_called()

    # The next day starts from nothing, and can cross the line again.
    clock.now = local(2026, 11, 13, 9)
    again.record(dollar_reply(0.30), "haiku")
    restarted.assert_called_once_with(0.30, 0.25)


def test_a_failing_alert_does_not_lose_the_record(tmp_path):
    clock = Clock(local(2026, 11, 12, 9))
    alert = Mock(side_effect=RuntimeError("ntfy is down"))
    log = make_log(tmp_path, clock, on_over_limit=alert, daily_limit=0.25)
    log.record(dollar_reply(0.30), "haiku")
    alert.assert_called_once()
    assert log.summary()["requests_24h"] == 1


def test_no_alert_with_no_limit(tmp_path):
    alert = Mock()
    log = make_log(tmp_path, Clock(local(2026, 11, 12, 9)), on_over_limit=alert, daily_limit=0)
    log.record(dollar_reply(1.0), "haiku")
    alert.assert_not_called()


def test_summary_is_cached_until_a_record_or_a_settings_change(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    settings = dict(SETTINGS)
    log = UsageLog(str(tmp_path / "usage.jsonl"), lambda: settings, clock=clock)
    first = log.summary()
    assert log.summary() is first
    clock.now += 30
    assert log.summary() is first
    clock.now += 31
    assert log.summary() is not first
    cached = log.summary()
    assert log.summary() is cached
    settings["daily_limit"] = 1.5
    assert log.summary()["daily_limit_pounds"] == 1.5
    cached = log.summary()
    log.record(reply(output=10), "haiku")
    assert log.summary() is not cached
    assert log.summary()["requests_24h"] == 1


def test_month_edge_london(tmp_path):
    clock = Clock(local(2026, 10, 31, 23, 30))
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(1.0), "haiku")
    assert log.summary()["month_pounds"] == 1.0
    assert log.summary()["today_pounds"] == 1.0

    clock.now = local(2026, 11, 1, 0, 30)
    log.record(dollar_reply(2.0), "haiku")
    summary = log.summary()
    assert summary["month_pounds"] == 2.0
    assert summary["today_pounds"] == 2.0
    assert summary["daily_pounds"][-2:] == [["2026-10-31", 1.0], ["2026-11-01", 2.0]]

    # Half an hour into the month counts as a whole day, so one call isn't taken as a
    # month's rate 48 times over - and exactly midnight on the 1st isn't divided by 0.
    assert summary["month_projection_pounds"] == 2.0 * 30
    clock.now = local(2026, 11, 1, 0, 0)
    assert log.summary()["month_projection_pounds"] == 2.0 * 30


def test_projection_counts_a_whole_day_until_one_has_gone(tmp_path):
    clock = Clock(local(2026, 11, 1, 0, 10))
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(0.01), "haiku")
    assert log.summary()["month_projection_pounds"] == round(0.01 * 30, 4)
    # Past the first day the days elapsed are counted as they are.
    clock.now = local(2026, 11, 2, 12)
    assert log.summary()["month_projection_pounds"] == round(0.01 / 1.5 * 30, 4)


def test_prunes_old_lines(tmp_path):
    now = local(2026, 11, 12, 12)
    path = tmp_path / "usage.jsonl"

    def line(at):
        return json.dumps({"at": at, "model": "haiku", "input": 1, "output": 1, "cache_read": 0,
                           "cache_write": 0, "searches": 0, "kind": "Chat and questions", "dollars": 0.1})

    old, recent = now - 50 * DAY, now - 10 * DAY
    path.write_text(line(old) + "\n" + line(recent) + "\n")
    clock = Clock(now)
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(0.1), "haiku")
    times = [json.loads(text)["at"] for text in path.read_text().splitlines()]
    assert times == [recent, now]

    # Within a day it only appends. The first call of the next day prunes again: a line which
    # was 39 days old yesterday is 41 now.
    recent_edge = now - 38.5 * DAY
    with open(path, "a") as file:
        file.write(line(recent_edge) + "\n")
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(0.1), "haiku")
    assert recent_edge in [json.loads(text)["at"] for text in path.read_text().splitlines()]
    clock.now = now + 2 * DAY
    log.record(dollar_reply(0.1), "haiku")
    assert recent_edge not in [json.loads(text)["at"] for text in path.read_text().splitlines()]


def test_appends_between_prunes(tmp_path):
    clock = Clock(local(2026, 11, 12, 12))
    log = make_log(tmp_path, clock)
    for _ in range(3):
        log.record(dollar_reply(0.1), "haiku")
    assert len((tmp_path / "usage.jsonl").read_text().splitlines()) == 3
    assert oct((tmp_path / "usage.jsonl").stat().st_mode & 0o777) == "0o600"


def test_days_change_at_local_midnight(tmp_path):
    clock = Clock(local(2026, 11, 12, 23, 59))
    log = make_log(tmp_path, clock)
    log.record(dollar_reply(1.0), "haiku")
    clock.now = local(2026, 11, 13, 0, 1)
    log.record(dollar_reply(2.0), "haiku")
    summary = log.summary()
    assert summary["today_pounds"] == 2.0
    assert summary["windows"]["24h"]["pounds"] == 3.0
