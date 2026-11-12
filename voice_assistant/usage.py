"""
Usage Module.

Keeps a log of every call Nova makes to Claude - how many tokens it used, which model
answered, and what it cost - so the brain can publish a summary of it to Home Assistant
for the AI dashboard, and tell the phone when a day's spending runs over the limit.

One line of JSON is appended to the log per call: when it happened, the model, the
input, output, cache-read and cache-write tokens, the number of web searches, a kind of
request, and the cost in dollars. The cost is worked out when the call is recorded and
kept as it was, so changing a price later doesn't rewrite what was already spent.

Only counts, costs, model names and kinds of request are kept - never anything said to
Nova or any of its answers (decision 0005: nothing anyone says to Nova reaches Home
Assistant). The kind is taken from what the call did - the tools Claude used, whether
it searched the web, or neither - and never from the words.

The log doesn't grow for ever: lines older than KEEP_DAYS are dropped from the file at
the first call of each day, and aren't read in at the start. At a few hundred calls a
day, at about 150 bytes a line, that's well under a megabyte.

Days and months are local time - the brain runs Europe/London - so "today" starts at
midnight there, not in UTC.
"""

import json
import logging
import os
import threading
import time
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence

from . import storage

# Logger setup
logger = logging.getLogger(__name__)

# How many days of calls are kept, in the file and in memory: a bit over the 30 days the
# dashboard shows, so the oldest day it draws is never one that's half pruned away.
KEEP_DAYS = 40

# How long a summary is reused for. The dashboard asks every so often, and working one
# out reads every call kept, so a quiet minute shouldn't repeat that.
SUMMARY_SECONDS = 60

SECONDS_PER_DAY = 86400

# The kinds of request the dashboard splits spending into.
MUSIC = "Music"
DEVICES = "Lights and devices"
TIMERS = "Timers and alarms"
CALENDAR = "Calendar and lists"
OTHER = "Other"
CHAT = "Chat and questions"
SEARCH = "Web search"

# Which kind each of Claude's tools belongs to. A tool not listed here is "Other", so a
# newly added tool still shows up, just not under a heading of its own until it's added.
KINDS: Dict[str, str] = {
    **dict.fromkeys(
        ("play_music", "play_playlist", "download_music", "control_playback", "repeat", "shuffle"), MUSIC),
    **dict.fromkeys(
        ("set_lights", "run_home_script", "home_status", "get_batteries", "find_device", "pc_power",
         "set_volume"), DEVICES),
    **dict.fromkeys(
        ("set_timer", "name_reminder", "list_timers", "dismiss_timer", "check_missed", "cancel_timer",
         "set_alarm", "list_alarms", "next_alarm", "cancel_alarm"), TIMERS),
    **dict.fromkeys(("get_calendar", "get_activity", "add_to_calendar", "manage_list"), CALENDAR),
}

# The periods the dashboard's switch offers, in seconds.
WINDOWS = {"24h": SECONDS_PER_DAY, "7d": 7 * SECONDS_PER_DAY, "30d": 30 * SECONDS_PER_DAY}


def kind_of(tool_names: Sequence[str], searches: int) -> str:
    """
    Says what kind of request a call to Claude was, from what it did.

    Args:
        tool_names: The tools Claude called in its reply, in order.
        searches: How many web searches it made.

    Returns:
        "Web search" if it searched, which wins over any tool; else the kind of the
        first tool, which decides when there are several; "Other" for a tool with no
        kind of its own; and "Chat and questions" when there was no tool at all.
    """
    if searches:
        return SEARCH
    if not tool_names:
        return CHAT
    return KINDS.get(tool_names[0], OTHER)


def _count(owner, name: str) -> float:
    """A number off a usage object, or 0 if it isn't there or is None - the API leaves fields out."""
    value = getattr(owner, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _price(settings: Dict, name: str) -> float:
    """A price out of the settings, or 0 if it's missing or not a number."""
    value = settings.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _cache_writes(usage) -> float:
    """How many tokens were written to the cache, whichever way the response counted them."""
    total = _count(usage, "cache_creation_input_tokens")
    if total:
        return total
    breakdown = getattr(usage, "cache_creation", None)
    return _count(breakdown, "ephemeral_5m_input_tokens") + _count(breakdown, "ephemeral_1h_input_tokens")


def cost_in_dollars(usage, settings: Dict) -> float:
    """
    Works out what one call cost.

    Reads from cache cost a tenth of the input price. Writes to it cost 1.25 times
    that for the 5-minute cache and twice for the 1-hour one; when the response
    doesn't say which (an older SDK), they're all counted as 5-minute ones.

    Args:
        usage: The usage object of a response from Anthropic, or anything with the
            same attributes. A missing or None field counts as 0.
        settings: The prices - 'input_price' and 'output_price' in dollars per million
            tokens, and 'search_price' in dollars per 1,000 web searches.

    Returns:
        The cost in dollars.
    """
    input_price = _price(settings, "input_price")
    output_price = _price(settings, "output_price")
    search_price = _price(settings, "search_price")

    breakdown = getattr(usage, "cache_creation", None)
    if breakdown is None:
        five_minute, one_hour = _count(usage, "cache_creation_input_tokens"), 0
    else:
        five_minute = _count(breakdown, "ephemeral_5m_input_tokens")
        one_hour = _count(breakdown, "ephemeral_1h_input_tokens")

    searches = _count(getattr(usage, "server_tool_use", None), "web_search_requests")

    per_million = (
        _count(usage, "input_tokens") * input_price
        + _count(usage, "output_tokens") * output_price
        + _count(usage, "cache_read_input_tokens") * input_price * 0.1
        + five_minute * input_price * 1.25
        + one_hour * input_price * 2.0
    )
    return per_million / 1_000_000 + searches * search_price / 1000


def _parse(line: str) -> Optional[Dict]:
    """One call read from a line of the file, or None if it isn't a complete, readable one."""
    try:
        data = json.loads(line)
        return {
            "at": float(data["at"]),
            "model": str(data["model"]),
            "input": int(data["input"]),
            "output": int(data["output"]),
            "cache_read": int(data["cache_read"]),
            "cache_write": int(data["cache_write"]),
            "searches": int(data["searches"]),
            "kind": str(data["kind"]),
            "dollars": float(data["dollars"]),
        }
    except (ValueError, TypeError, KeyError):
        return None


class UsageLog:
    """The calls made to Claude over the last KEEP_DAYS days, kept in a file and in memory."""

    def __init__(self, path: str, settings: Callable[[], Dict],
                 on_over_limit: Optional[Callable[[float, float], None]] = None,
                 clock: Callable[[], float] = time.time):
        """
        Reads the log in, skipping any line which can't be read - a power cut can leave
        the last one half written.

        Args:
            path: The .jsonl file to keep the log in, made if it isn't there.
            settings: Gives the usage settings as they are now - prices, 'daily_limit' and
                'pounds_per_dollar' - called each time they're needed, so a change on the
                settings page takes effect at once.
            on_over_limit: Called with the pounds spent today and the daily limit when a
                call takes the day's spending from at or under the limit to over it. Once
                a day, not on every call after.
            clock: Gives the time in seconds since 1970. Only given by tests.
        """
        self._path = path
        self._settings = settings
        self._on_over_limit = on_over_limit
        self._clock = clock
        self._events: List[Dict] = []
        self._cache = None
        # Held while the calls are changed or read, as sessions record from threads of their own.
        self._lock = threading.Lock()
        # The day the file was last rewritten without its old lines. Left empty at the start so the
        # first call after the brain starts rewrites it - which also drops any broken line, and
        # so a new line is never glued on to a half written one.
        self._pruned_on = None
        self._folder_made = False
        self._load()

    def _load(self):
        """Reads in the lines newer than KEEP_DAYS."""
        cutoff = self._clock() - KEEP_DAYS * SECONDS_PER_DAY
        try:
            with open(self._path) as file:
                lines = file.read().splitlines()
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"Could not read the usage log: {e}")
            return
        for line in lines:
            event = _parse(line)
            if event is not None and event["at"] >= cutoff:
                self._events.append(event)

    def record(self, response, model: str) -> None:
        """
        Adds one call to the log. Never raises, as it runs right after Nova's reply and
        losing a line of the log must never cost the user that - a failure is only logged.

        Args:
            response: What Anthropic returned. Its usage and the names of the tools it
                called are read, and nothing else. One with no usage isn't recorded.
            model: The model which answered.
        """
        try:
            self._record(response, model)
        except Exception as e:
            logger.warning(f"Could not record the usage: {e}")

    def _record(self, response, model: str):
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        settings = self._settings()
        tools = [block.name for block in (getattr(response, "content", None) or [])
                 if getattr(block, "type", None) == "tool_use"]
        searches = int(_count(getattr(usage, "server_tool_use", None), "web_search_requests"))
        now = self._clock()
        event = {
            "at": round(now, 3),
            "model": str(model),
            "input": int(_count(usage, "input_tokens")),
            "output": int(_count(usage, "output_tokens")),
            "cache_read": int(_count(usage, "cache_read_input_tokens")),
            "cache_write": int(_cache_writes(usage)),
            "searches": searches,
            "kind": kind_of(tools, searches),
            "dollars": cost_in_dollars(usage, settings),
        }

        rate = _price(settings, "pounds_per_dollar") or 1.0
        limit = _price(settings, "daily_limit")
        today = datetime.fromtimestamp(now).date()
        with self._lock:
            before = round(self._dollars_on(today) * rate, 4)
            self._events.append(event)
            self._cache = None
            self._save(event, today)
            after = round(self._dollars_on(today) * rate, 4)

        # A limit of 0 turns the warning off. Called outside the lock, as it may be slow, and
        # whatever it does is its own business: it can't be allowed to lose the record.
        if self._on_over_limit is not None and limit > 0 and before <= limit < after:
            try:
                self._on_over_limit(after, limit)
            except Exception as e:
                logger.warning(f"Could not report that the spending limit was passed: {e}")

    def _dollars_on(self, day) -> float:
        """What was spent on one day, in dollars. Reads back from the newest call, which is cheap."""
        total = 0.0
        for event in reversed(self._events):
            when = datetime.fromtimestamp(event["at"]).date()
            if when < day:
                break
            if when == day:
                total += event["dollars"]
        return total

    def _save(self, event: Dict, today):
        """
        Writes a call to the file - by appending, or on the first call of a day by writing
        the whole file afresh without the calls older than KEEP_DAYS. Called with the lock held.
        """
        try:
            if self._pruned_on != today:
                cutoff = event["at"] - KEEP_DAYS * SECONDS_PER_DAY
                self._events = [e for e in self._events if e["at"] >= cutoff]
                storage.save_text(self._path, "".join(json.dumps(e) + "\n" for e in self._events))
                self._pruned_on = today
                self._folder_made = True
                return
            if not self._folder_made:
                os.makedirs(os.path.dirname(self._path), mode=0o700, exist_ok=True)
                self._folder_made = True
            # Not flushed to the disk: losing the last line to a power cut is better than
            # making every reply wait for one.
            descriptor = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a") as file:
                file.write(json.dumps(event) + "\n")
        except OSError as e:
            # The call is still counted in memory, and a rewrite which failed is tried again
            # on the next call, which would put it back in the file.
            logger.warning(f"Could not save the usage log: {e}")

    def summary(self) -> Dict:
        """
        Sums up the log for the dashboard. Worked out afresh after a call is recorded,
        a settings change, or a minute without either, and otherwise reused.

        Returns:
            A dict of:
            today_pounds, month_pounds: spent so far today and this month.
            month_projection_pounds: this month's total at the rate so far - what's spent
                over the days elapsed (today counting as the fraction which has passed),
                times the days in the month.
            daily_limit_pounds: the limit, as set.
            requests_24h: calls in the last 24 hours.
            cache_share_24h: of the input tokens in those, the share read from the cache,
                0 to 1 - or None if there were none.
            windows: for '24h', '7d' and '30d', the 'output' tokens, the 'total' of all
                tokens (input, output and cache reads and writes), the 'pounds' and the
                'requests'.
            hourly_pounds: [start of the hour, pounds] for each of the last 24 hours,
                oldest first, ending with the current one. The start is ISO 8601 with its
                UTC offset, so the hour which repeats when the clocks go back isn't mistaken
                for the first.
            daily_pounds: [YYYY-MM-DD, pounds] for each of the last 30 days, oldest first,
                ending today.
            kinds_month: pounds this month for each kind of request, the largest first.
            models: for each of the windows, the output tokens of each model.
            Spending is in pounds, at 'pounds_per_dollar' and rounded to 4 places.
        """
        settings = self._settings()
        with self._lock:
            now = self._clock()
            if self._cache is not None:
                made, made_with, result = self._cache
                if 0 <= now - made < SUMMARY_SECONDS and made_with == settings:
                    return result
            result = self._summarise(now, settings)
            self._cache = (now, dict(settings), result)
            return result

    def _summarise(self, now: float, settings: Dict) -> Dict:
        """Works out summary() from the calls kept. Called with the lock held."""
        rate = _price(settings, "pounds_per_dollar") or 1.0

        def pounds(dollars: float) -> float:
            return round(dollars * rate, 4)

        local = datetime.fromtimestamp(now)
        today = local.date()
        month_start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days_in_month = monthrange(local.year, local.month)[1]
        # Naive local times on both sides, so it's the wall clock which is counted and a clock
        # change inside the month doesn't make the day 23 or 25 hours.
        days_elapsed = (local - month_start).total_seconds() / SECONDS_PER_DAY

        # Whole hours since 1970 throughout, which are the same hours as local ones anywhere on a
        # whole hour's offset from UTC, as London is, and which have no repeated or missing hour.
        this_hour = int(now // 3600) * 3600
        hourly = {this_hour - 3600 * i: 0.0 for i in range(23, -1, -1)}
        daily = {today - timedelta(days=i): 0.0 for i in range(29, -1, -1)}

        today_dollars = month_dollars = 0.0
        kinds: Dict[str, float] = {}
        windows = {name: {"output": 0, "total": 0, "dollars": 0.0, "requests": 0} for name in WINDOWS}
        models: Dict[str, Dict[str, int]] = {name: {} for name in WINDOWS}
        reads = inputs = 0

        for event in self._events:
            at = event["at"]
            when = datetime.fromtimestamp(at)
            day = when.date()
            dollars = event["dollars"]
            tokens = event["input"] + event["output"] + event["cache_read"] + event["cache_write"]

            if day == today:
                today_dollars += dollars
            if (when.year, when.month) == (local.year, local.month):
                month_dollars += dollars
                kinds[event["kind"]] = kinds.get(event["kind"], 0.0) + dollars
            hour = int(at // 3600) * 3600
            if hour in hourly:
                hourly[hour] += dollars
            if day in daily:
                daily[day] += dollars

            for name, span in WINDOWS.items():
                if now - span <= at <= now:
                    window = windows[name]
                    window["output"] += event["output"]
                    window["total"] += tokens
                    window["dollars"] += dollars
                    window["requests"] += 1
                    models[name][event["model"]] = models[name].get(event["model"], 0) + event["output"]
            if now - SECONDS_PER_DAY <= at <= now:
                reads += event["cache_read"]
                inputs += event["input"] + event["cache_read"] + event["cache_write"]

        requests = windows["24h"]["requests"]
        return {
            "today_pounds": pounds(today_dollars),
            "month_pounds": pounds(month_dollars),
            "month_projection_pounds": pounds(month_dollars / days_elapsed * days_in_month) if days_elapsed else 0.0,
            "daily_limit_pounds": _price(settings, "daily_limit"),
            "requests_24h": requests,
            "cache_share_24h": (reads / inputs if inputs else 0.0) if requests else None,
            "windows": {name: {"output": w["output"], "total": w["total"],
                               "pounds": pounds(w["dollars"]), "requests": w["requests"]}
                        for name, w in windows.items()},
            "hourly_pounds": [[datetime.fromtimestamp(hour).astimezone().isoformat(timespec="seconds"),
                               pounds(spent)] for hour, spent in hourly.items()],
            "daily_pounds": [[day.isoformat(), pounds(spent)] for day, spent in daily.items()],
            "kinds_month": {kind: pounds(spent) for kind, spent
                            in sorted(kinds.items(), key=lambda item: item[1], reverse=True)},
            "models": {name: dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))
                       for name, counts in models.items()},
        }
