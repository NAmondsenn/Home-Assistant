"""
Display Feed Module
What each clock display shows of its satellite, from decisions 2 and 6 of docs/decisions/0013.

A clock asks the brain for its satellite's state and is answered as soon as any of it
changes - a long poll - so "Listening..." covers its page the moment Nova hears the wake
word. What Nova is doing and what it heard arrive through Session.watch as they happen;
the rest is read by one thread, each on its own schedule, from what Nova already has:
Home Assistant's weather, Nova's own Google calendars, the satellite's own player and
Jellyfin's players in Home Assistant, found at run time.
"""

import functools
import logging
import threading
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import quote

from voice_assistant.batteries import HOME_FAILURES
from voice_assistant.google_calendar import READ_FAILURES
from voice_assistant.settings_page import volume_levels

logger = logging.getLogger(__name__)

# Seconds between reads of each source. Navidrome's is the brain's own and cheap to ask;
# Spotify's means asking Spotify's servers, as do Jellyfin's players (Home Assistant), the
# weather (Met's forecast changes hourly at most) and the calendars (Google).
MUSIC_EVERY = 2.0
ONLINE_MUSIC_EVERY = 5.0
JELLYFIN_EVERY = 5.0
WEATHER_EVERY = 600.0
EVENTS_EVERY = 300.0

# How far ahead the calendars are read, and how many events are shown. A month, so a quiet
# week still shows five - the page gives the date of those beyond this week.
EVENT_DAYS = 31
EVENTS_SHOWN = 5

# Where the page fetches a cover from - Nova's player's, or a Jellyfin player's through
# Home Assistant, which wants Nova's token. See the display server.
COVER = "/display/api/cover?kind={kind}&id={id}"

# What a read gives back when it failed, as None is a real answer - no weather entity, say.
_FAILED = object()


def _settled(state: Dict) -> Dict:
    """
    What a state is compared by for a new version: all of it but the time and how far
    into its music it is, which the page moves on itself - so a clock isn't woken every
    two seconds for a progress bar it already draws.
    """
    music = state.get("music")
    if music is not None:
        music = {key: value for key, value in music.items() if key not in ("position", "at")}
    return dict({key: value for key, value in state.items() if key not in ("version", "now")}, music=music)


def _is_over(event: Dict, moment: datetime, read_on: Optional[date]) -> bool:
    """
    Whether an event has ended by a moment. The calendar gives an all-day event no end,
    but Google only gives one which is on some time from the day it's read - so one which
    started before today, a weekend away from Saturday, is still on if it was read today,
    and counts as over if it was read yesterday, until a read from today says otherwise.
    """
    if event["all_day"]:
        return event["start"] < moment.date() and (read_on is None or read_on < moment.date())
    return (event["end"] or event["start"]) <= moment


def _start(when) -> str:
    """An event's start as the page reads it: "2026-11-09" for a day, "2026-11-09T09:30" for a local time."""
    return when.strftime("%Y-%m-%dT%H:%M") if isinstance(when, datetime) else when.isoformat()


def _seconds(value) -> Optional[float]:
    """A number of seconds as a float, or None for none."""
    return float(value) if value is not None else None


class DisplayFeed:
    """
    Each satellite's state for its clocks, built when a clock first asks and kept up to
    date from then on. Its version changes with anything a clock shows, so a clock can
    wait for the next one - see wait.
    """

    def __init__(self, brain, now: Callable[[], float] = time.time, every: float = 1.0):
        """
        Args:
            brain: The brain, for its sessions, weather, calendar and Home Assistant.
            now: The brain's time, in epoch seconds - the tests' own clock.
            every: Seconds between the thread's passes. Each source is read only as
                often as its own schedule says, e.g. the weather every WEATHER_EVERY.
        """
        self._brain = brain
        self._now = now
        self._every = every
        # Guards the states and the satellites told of a change - and is what a clock waits on.
        self._changed = threading.Condition()
        self._states: Dict[str, Dict] = {}
        self._told: Set[str] = set()
        # The first version, from the time in milliseconds, so a clock still holding a
        # version from before the brain restarted - 1762650000000 and on - never holds the
        # new brain's and waits on a state it hasn't seen.
        self._first_version = int(time.time() * 1000)
        # Builds one state at a time, so they're kept in the order they were read; and the
        # sessions watched, by satellite, as (session, watcher).
        self._building = threading.Lock()
        self._watching: Dict[str, tuple] = {}
        # Reads one source at a time, and holds what was read last. Each is replaced whole -
        # the music satellite by satellite - never changed in place, so a build reads them
        # without the lock.
        self._reading = threading.Lock()
        self._due: Dict[str, float] = {}
        self._weather: Optional[Dict] = None
        self._events: List[Dict] = []
        self._events_read_on: Optional[date] = None
        self._jellyfin: List[Dict] = []
        # Nova's own music on each satellite, as (session, now_playing, when it was read).
        self._music: Dict[str, tuple] = {}
        # The sources failing, so each is logged once until it works again - and the
        # kind of error the thread last logged, while it keeps happening.
        self._failing: Set[str] = set()
        self._pass_failing: Optional[type] = None
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Starts the daemon thread which keeps every satellite's state up to date."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="display")
        self._thread.start()

    def stop(self):
        """Stops the thread, waiting briefly for a pass already under way, and stops watching the sessions."""
        self._stopping.set()
        with self._changed:
            # So the clocks waiting are answered now, not at the end of their long polls.
            self._changed.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        with self._building:
            for session, watcher in self._watching.values():
                session.unwatch(watcher)
            self._watching.clear()

    def state(self, satellite: Optional[str]) -> Dict:
        """
        Args:
            satellite: The satellite's name, e.g. "pi" - or None while there's none to
                name, whose state is never connected.

        Returns:
            Its state now, as the page reads it - see the plan's contract. One never
            asked for before is read and built now; one not connected says
            "connected": False.
        """
        with self._changed:
            known, told = satellite in self._states, satellite in self._told
        if not known:
            self._read([satellite])
        if not known or told:
            self._build(satellite)
        with self._changed:
            return dict(self._states[satellite], now=self._now())

    def wait(self, satellite: Optional[str], since: int, timeout: float) -> Dict:
        """
        Waits for a satellite's state to change from the version a clock has - its long poll.

        Args:
            satellite: The satellite's name, or None - see state.
            since: The version the clock has.
            timeout: The most seconds to wait.

        Returns:
            Its state - at once if its version isn't since, else as soon as it changes,
            or after timeout as it is then.
        """
        end = time.monotonic() + timeout
        while True:
            state = self.state(satellite)
            left = end - time.monotonic()
            if state["version"] != since or left <= 0 or self._stopping.is_set():
                return state
            with self._changed:
                # Checked again with the lock held, as a change told - or stop - between
                # state() and here would otherwise wake no one.
                if (satellite not in self._told and self._states[satellite]["version"] == since
                        and not self._stopping.is_set()):
                    self._changed.wait(left)

    def poke(self, satellite: str):
        """
        Builds a satellite's state now, rather than at the next pass - after its
        volume is changed from a clock, say, so the level shown is the new one.

        Args:
            satellite: The satellite's name.
        """
        self._build(satellite)

    def check(self):
        """
        Reads whatever is due and builds every satellite's state once - a pass of the
        thread. A source which fails keeps its last good weather or events, or gives no
        music, and is logged once until it works again. Until a clock first asks for a
        satellite's state there is nobody to read for, so nothing is read at all.
        """
        with self._changed:
            satellites = list(self._states)
        if not satellites:
            return
        self._read(satellites)
        for satellite in satellites:
            # Each on its own, so one satellite's state failing to build - a bug - leaves
            # the others' up to date.
            self._attempt(f"the state of the {satellite} satellite", lambda: self._build(satellite), ())

    def _run(self):
        while not self._stopping.is_set():
            self._pass()
            self._stopping.wait(self._every)

    def _pass(self):
        """One pass of the thread, in which no error may end the thread."""
        try:
            self.check()
        except Exception as e:
            # A bug would otherwise fill the log with the same traceback every second.
            if type(e) is not self._pass_failing:
                logger.exception("Keeping the clock displays up to date failed, will try again")
            self._pass_failing = type(e)
        else:
            self._pass_failing = None

    # Watching the sessions

    def _tell(self, satellite: str):
        """
        A session's watcher: notes that a satellite's state has changed and wakes its
        clocks, which build it themselves - see wait. Only takes note, as Session.watch
        asks, since it may be called with the session's announcement lock held.
        """
        with self._changed:
            self._told.add(satellite)
            self._changed.notify_all()

    def _watch(self, satellite: str, session):
        """
        Watches a satellite's session, once - or stops watching one which has gone, or been
        replaced. Nothing is watched once the feed is stopping, so a pass still running as
        stop() returns can't leave a session telling a feed which has gone.
        """
        if self._stopping.is_set():
            session = None
        held = self._watching.get(satellite)
        if held is not None and held[0] is session:
            return
        if held is not None:
            held[0].unwatch(held[1])
            del self._watching[satellite]
        if session is not None:
            watcher = functools.partial(self._tell, satellite)
            session.watch(watcher)
            self._watching[satellite] = (session, watcher)

    # Reading

    def _is_due(self, source: str, now: float, every: float) -> bool:
        """Whether a source is due to be read at now - and if so, when it's due next."""
        if now < self._due.get(source, float("-inf")):
            return False
        self._due[source] = now + every
        return True

    def _attempt(self, source: str, read: Callable, expected: tuple):
        """
        Reads one source - or builds one satellite's state - logging a failure once until
        it works again.

        Args:
            source: What's read, as said in the log - e.g. "the weather".
            read: Called with nothing for it.
            expected: The errors it fails with when what it asks isn't there, logged
                without a traceback. Anything else is a bug, and logged with one.

        Returns:
            What read gave, or _FAILED.
        """
        try:
            found = read()
        except Exception as e:
            if source not in self._failing:
                if isinstance(e, expected):
                    # Only the kind of failure - a requests error names the address, and a
                    # calendar's address has its id, which may be an email address.
                    logger.warning(f"Couldn't read {source} for the clock displays, will try again: "
                                   f"{type(e).__name__}")
                else:
                    logger.exception(f"Reading {source} for the clock displays failed, will try again")
                self._failing.add(source)
            return _FAILED
        if source in self._failing:
            logger.info(f"{source[0].upper()}{source[1:]} is back for the clock displays.")
            self._failing.discard(source)
        return found

    def _read(self, satellites: List[str]):
        """Reads each source which is due: weather, events, Jellyfin's players and each satellite's own music."""
        with self._reading:
            now = self._now()
            moment = datetime.fromtimestamp(now).astimezone()
            if self._is_due("weather", now, WEATHER_EVERY):
                self._read_weather(moment)
            if self._is_due("events", now, EVENTS_EVERY):
                self._read_events(moment.date())
            if self._is_due("jellyfin", now, JELLYFIN_EVERY):
                home = self._brain.home
                found = self._attempt("what Jellyfin is playing", home.jellyfin_media, HOME_FAILURES) if home else []
                self._jellyfin = [] if found is _FAILED else found
            for satellite in satellites:
                self._read_music(satellite, now)

    def _read_weather(self, moment: datetime):
        weather = self._brain.weather
        if weather is None:
            self._weather = None
            return
        found = self._attempt("the weather", lambda: weather.today(moment), HOME_FAILURES)
        if found is not _FAILED:
            self._weather = found

    def _read_events(self, today: date):
        calendar = self._brain.calendar
        if calendar is None:
            self._events, self._events_read_on = [], None
            return
        found = self._attempt("the calendars' events",
                              lambda: calendar.events(today, EVENT_DAYS, leave_out=calendar.left_out()), READ_FAILURES)
        if found is not _FAILED:
            # Only what the page shows is kept - never a calendar's name or id, which may be an email address.
            self._events = [{"summary": event["summary"], "start": event["start"], "end": event.get("end"),
                             "all_day": event["all_day"]} for event in found]
            self._events_read_on = today

    def _read_music(self, satellite: str, now: float):
        """Reads what Nova is playing on a satellite, if it's due - or at once for a session not read before."""
        session = self._session(satellite)
        player = getattr(session, "music_player", None)
        if player is None:
            self._music.pop(satellite, None)
            self._due.pop(f"music {satellite}", None)
            return
        last = self._music.get(satellite)
        every = ONLINE_MUSIC_EVERY if getattr(player, "needs_internet", True) else MUSIC_EVERY
        if last is not None and last[0] is session and now < self._due.get(f"music {satellite}", float("-inf")):
            return
        self._due[f"music {satellite}"] = now + every
        found = self._attempt(f"what Nova is playing on the {satellite} satellite", player.now_playing, HOME_FAILURES)
        self._music[satellite] = (session, None if found is _FAILED else found, now)

    def _session(self, satellite: Optional[str]):
        """
        A satellite's session, or None if it isn't connected - and always for None, which
        a clock asks for only when there's no satellite to name. The brain's
        session_for(None) would give the default's instead, or raise with the
        satellites' file unreadable.
        """
        return self._brain.session_for(satellite) if satellite else None

    # Building

    def _build(self, satellite: str):
        """Builds a satellite's state from what was read last, with a new version if anything shown has changed."""
        with self._building:
            with self._changed:
                # Forgotten before reading, so a change told while this builds is built again.
                self._told.discard(satellite)
            session = self._session(satellite)
            self._watch(satellite, session)
            built = self._contents(satellite, session, self._now())
            with self._changed:
                last = self._states.get(satellite)
                if last is None:
                    version = self._first_version
                elif _settled(last) != _settled(built):
                    version = last["version"] + 1
                else:
                    version = last["version"]
                self._states[satellite] = dict(built, version=version)
                if last is None or version != last["version"]:
                    self._changed.notify_all()

    def _contents(self, satellite: str, session, now: float) -> Dict:
        """Everything in a satellite's state but its version and the time."""
        moment = datetime.fromtimestamp(now).astimezone()
        last = self._music.get(satellite)
        playing = last[1] if last is not None and session is not None and last[0] is session else None
        room = session.home.area if session is not None and session.home is not None else None
        return {"connected": session is not None,
                "nova": session.activity if session is not None else "idle",
                "heard": session.heard if session is not None else "",
                "muted": bool(session.muted) if session is not None else False,
                "utc_offset": int(moment.utcoffset().total_seconds() // 60),
                "weather": dict(self._weather) if self._weather is not None else None,
                "events": self._next_events(moment),
                "music": (self._novas_music(session, playing, room, last[2]) if playing is not None
                          else self._jellyfins_music(room, now)),
                "volume": self._volume(session, playing)}

    def _next_events(self, moment: datetime) -> List[Dict]:
        """
        The next few events not over by now, earliest first - left out here rather than as
        they're read, so a lecture ending at 10:30 is gone at 10:30, not at the next read.
        """
        upcoming = [event for event in self._events if not _is_over(event, moment, self._events_read_on)]
        return [{"title": event["summary"], "start": _start(event["start"]), "all_day": event["all_day"]}
                for event in upcoming[:EVENTS_SHOWN]]

    def _novas_music(self, session, playing: Dict, room: Optional[str], read_at: float) -> Dict:
        """Nova's own music on the satellite, shown even while paused - Jellyfin only shows when Nova plays nothing."""
        cover = playing.get("cover")
        return {"source": session.music_player.name, "room": room, "title": playing["track"],
                "artist": playing.get("artist") or None, "album": playing.get("album") or None,
                "playing": bool(playing.get("playing")), "position": float(playing.get("position") or 0),
                "duration": _seconds(playing.get("duration")), "at": read_at,
                "cover": COVER.format(kind="nova", id=quote(str(cover), safe="")) if cover else None}

    def _jellyfins_music(self, room: Optional[str], now: float) -> Optional[Dict]:
        """
        A Jellyfin player playing, or None: one in the satellite's room if there is one,
        else the first by entity id - "media_player.kitchen" before "media_player.office_tv".
        A paused one is never shown, so the page goes back to Home once it's been paused a while.
        """
        playing = [player for player in self._jellyfin if player["state"] == "playing"]
        if not playing:
            return None
        player = min(playing, key=lambda p: (not room or p["area"] != room, p["entity_id"]))
        updated = player["updated"]
        return {"source": "Jellyfin", "room": player["area"], "title": player["title"] or player["name"],
                "artist": player["artist"], "album": player["album"], "playing": True,
                "position": float(player["position"] or 0), "duration": _seconds(player["duration"]),
                "at": float(updated) if updated is not None else now,
                "cover": (COVER.format(kind="home", id=quote(player["entity_id"], safe=""))
                          if player["picture"] else None)}

    @staticmethod
    def _volume(session, playing: Optional[Dict]) -> Dict:
        """
        The volume a clock's buttons change: the music's while Nova's own music plays on
        the satellite, else Nova's voice - never the alarm's. 0 for a satellite not connected.
        """
        if session is None:
            return {"category": "general", "percent": 0}
        category = "music" if playing is not None and playing.get("playing") else "general"
        return {"category": category, "percent": int(volume_levels(session.volume, session.music_player)[category])}
