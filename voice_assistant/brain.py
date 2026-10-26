"""
Brain Module.

Nova's brain, on PVE2 - see docs/decisions/0006. It holds what every satellite shares
- the clock, memory, routines, ntfy, the weather, Piper and Nemotron - and gives each
satellite which connects a session of its own (see session.py), for as long as it
stays connected.

It's only reachable over the tailnet: it listens on its own machine alone, and
Tailscale passes the tailnet's connections on to it. Each satellite proves its own
password first - see protocol.admit, and add-satellite in backend.py.

This was the part of main.py's SmartAssistant which wasn't one satellite's: its
running mark and restart notice, its signals and shutdown, the watchdog, routines at
their time, and the reminder nobody answered sent to the user's phone.
"""

import hmac
import logging
import os
import queue
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

from voice_assistant import logging_setup, paths, protocol, storage, watchdog
from voice_assistant.batteries import BatteryChoices, BatteryWatcher
from voice_assistant.choices import ChoiceStore
from voice_assistant.clock import Clock, spoken_clock_time
from voice_assistant.google_calendar import READ_FAILURES, GoogleCalendar
from voice_assistant.ha_registry import REGISTRY_TIMEOUT, HomeRegistry, HomeSocket
from voice_assistant.home_assistant import HomeAssistant
from voice_assistant.lists import Lists
from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant.memory import Memory
from voice_assistant.ntfy import RETOPIC, Ntfy
from voice_assistant.pc import PCPower
from voice_assistant.protocol import ProtocolError, Refused, admit
from voice_assistant.remote import RemoteSatellite
from voice_assistant.routines import Routines
from voice_assistant.satellite_link import SOCKET_TIMEOUT, Stream
from voice_assistant.satellite_passwords import SatellitePasswords, name_problem
from voice_assistant.session import Session
from voice_assistant.settings_page import SettingsPage
from voice_assistant.text_to_speech import TextToSpeech
from voice_assistant.weather import Weather

logger = logging.getLogger(__name__)

# Left while the brain runs, and taken away when it stops properly - so finding it when
# starting means the last run didn't: it crashed, the watchdog restarted it, or the
# power went. See _note_start.
RUNNING_MARK = os.path.join(paths.DATA_DIR, "running")

# What the brain exits with once the settings page has asked it to restart. systemd
# restarts it for this status whatever else it was told - see RestartForceExitStatus in
# nova-backend.service - which the nova user couldn't do itself with systemctl, as
# the service can never gain privileges.
RESTART_EXIT_STATUS = 75

# How long a restart waits before stopping, in seconds, so the settings page's answer
# gets out first.
RESTART_DELAY = 1.0

# The least time between telling the user's phone that the brain, or one satellite,
# had to restart, so a crash which keeps happening doesn't flood it.
RESTART_NOTICE_GAP = 3600

# How often it's checked whether one of the user's routines is due, in seconds - a few
# times within each minute.
ROUTINE_CHECK = 20

# A routine which came due while its satellite was away runs once it's back if it's
# less late than this, in seconds. Later than that, it's skipped, and Nova says so
# instead - the user's call.
MISSED_ROUTINE_GRACE = 3600

# How often each satellite is sent its copy of the timers and alarms even when nothing
# has changed, in seconds. The copy holds a week, so a repeating alarm's next week only
# comes into it a day at a time.
ALARMS_RESENT = 3600

# How long a satellite which has just connected is given to say what rang and what was
# dismissed on it while it couldn't reach the brain, in seconds. It says so straight
# away, so this is only ever waited out on a connection gone bad - which is let go,
# for the satellite to connect again and say it. Counted as caught up instead, the
# brain said what it had missed, and let go of it, while the satellite might still be
# ringing it, so nothing took the ringing over when the report came again.
AWAY_WAIT = 2.0

# How often listening for satellites checks whether it's been stopped, in seconds - and
# so how often it tells the watchdog the brain is still working.
#
# That's said to be when the stillest session last moved (see Brain.last_moved), so a
# session stuck part way through a turn - its satellite still connected and pinging,
# and every wake word there getting silence - is caught by the watchdog's allowance:
# watchdog.ALLOWANCE, 150 seconds, the same as main.py had on the Pi, whose watchdog
# went quiet as soon as a turn hung, as the microphone was no longer read. That's more
# than a turn legitimately goes between steps which count as moving - a web search
# retried once (two lots of llm.search_timeout) after a recording of up to 10 seconds
# - while a session between wake words, or playing a reply of any length, moves
# several times a second. nova-backend.service's WatchdogSec=60 then restarts the brain
# within a minute of the watchdog going quiet: three and a half minutes from a session
# sticking, at most, which its satellite rides out, ringing from its copy.
STOP_CHECK = 0.5

# What a satellite says when it can't reach the brain, in the brain's own voice - see
# fallback.Phrase.
FALLBACK_PHRASE = "Sorry, I can't reach my server right now."


def _speech_to_text(config):
    """
    Nemotron, loaded, for every satellite's recordings - see nemotron.py.

    Args:
        config: Configs loaded from config.yaml, for backend.model.

    Returns:
        A NemotronSpeechToText.
    """
    # Imported here, so the rest of Nova never needs sherpa-onnx.
    from voice_assistant.nemotron import Nemotron
    from voice_assistant.speech_to_text import NemotronSpeechToText

    logger.info("Loading Nemotron...")
    return NemotronSpeechToText(Nemotron(os.path.join(paths.PROJECT_DIRECTORY, config.section("backend").get("model"))))


def _skipped_message(skipped: List[Dict]) -> str:
    """
    What's said for routines which didn't run while their satellite was away, e.g.
    "Your 7 o'clock routine didn't run - I couldn't reach the speaker."

    Args:
        skipped: Each as Routines.take_missed has it.
    """
    times = []
    for entry in skipped:
        hour, minute = (int(part) for part in entry["at"].split(":"))
        said = f"{hour % 12 or 12} o'clock" if minute == 0 else spoken_clock_time(hour, minute)
        if said not in times:
            times.append(said)
    if len(times) == 1:
        return f"Your {times[0]} routine didn't run - I couldn't reach the speaker."
    return f"Your {', '.join(times[:-1])} and {times[-1]} routines didn't run - I couldn't reach the speaker."


class _Server(socketserver.ThreadingTCPServer):
    """Listens for satellites, each on a thread of its own."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, brain: "Brain"):
        self.brain = brain
        super().__init__(("127.0.0.1", port), _Connection)

    def service_actions(self):
        # Run between connections, every STOP_CHECK. Nothing in it may stop the brain
        # listening.
        try:
            # Never waiting on a removal the page is handling - its look is enough.
            self.brain._shut_out_removed(wait=False)
        except Exception:
            logger.exception("Couldn't check for satellites which have been removed")
        # The brain's main loop is still going, and so is every session, but for one
        # which is stuck - see STOP_CHECK.
        watchdog.beat(self.brain.last_moved())


class _Connection(socketserver.BaseRequestHandler):
    """One satellite's connection, for as long as it keeps it open. It's closed once this returns."""

    def handle(self):
        self.server.brain._serve_satellite(self.request, self.client_address[0])


class Brain:
    """What every satellite shares, and the session each one connected has."""

    def __init__(self, config):
        """
        Loads everything - Piper and Nemotron take a few seconds.

        Args:
            config: Configs loaded from config.yaml.
        """
        self.config = config
        self.name = config.section("assistant").get("name", "Assistant")
        # Where it listens. 0 is any free port, which this becomes once serve() is listening.
        self.port = config.section("backend").get("port", 10400)
        # The satellites it lets in, the default first - see default_satellite.
        self.passwords = SatellitePasswords()
        # How the passwords' file looked when it was last read for who's still let in,
        # and the satellites it had then - see _shut_out_removed.
        self._passwords_seen = self._passwords_file()
        try:
            self._names_seen: List[str] = self.passwords.names(missing_ok=False)
        except FileNotFoundError:
            # A fresh brain's - the file is made as the first satellite is added.
            logger.info("No satellites have been added yet")
            self._names_seen = []
        except Exception as e:
            logger.error(f"Couldn't read the satellites' passwords: {e}")
            self._names_seen = []

        # Each satellite's session, by its name, for as long as it's connected.
        self.sessions: Dict[str, Session] = {}
        # Guards sessions and the other notes below. Only ever held for a moment, and
        # never while calling out, so anything may take it.
        self._lock = threading.Lock()
        # Held while a satellite's session is made, so two connections from the same
        # satellite can't both make one.
        self._connecting = threading.Lock()
        # Held while a satellite's copy of the timers and alarms is made and sent, so
        # an older copy can't reach it after a newer one.
        self._sending_alarms = threading.Lock()
        # Held while it's seen which satellites have been removed, and each is handled
        # - by the server's poll and by remove_satellite alike, which may look at
        # once - so one removal is never handled twice.
        self._removing = threading.Lock()
        # The same for the phrase, made again when the speaking speed changes - see
        # apply_settings - so one at the old speed can't reach a satellite after it.
        self._sending_phrase = threading.Lock()
        # The satellites connected which have said what happened while they were
        # away, so can be told what came due while the brain was off - see
        # Clock.can_hear, which reads it without the lock.
        self._caught_up: Set[str] = set()
        # Why each satellite was last turned away, so it's logged once rather than at
        # every try - see _turned_away.
        self._why_turned_away: Dict[str, str] = {}
        # When each satellite's restart was last told to the user's phone.
        self._restarts_told: Dict[str, float] = {}
        # Whether what rang on no satellite has been given to the default yet.
        self._unowned_given = False
        # Whether each satellite removed had its copy of the alarms emptied, for
        # remove_satellite to say - whichever look handled the removal.
        self._emptied: Dict[str, bool] = {}
        self._server: Optional[_Server] = None
        self._stopped = threading.Event()

        # Whether run() is running - the routines are checked while it is.
        self.running = False
        # Set once a stop signal has arrived - see shutdown.
        self._stopping = False
        # Whether it's stopping because the settings page asked for a restart - see restart.
        self.restarting = False

        logger.info("Loading the brain...")
        # The assistant's own sound effects, which each satellite keeps and plays by name.
        self.sounds_folder = Path(paths.SOUNDS_DIR)
        # What the assistant knows about the user, learnt as they talk - see memory.py.
        self.memory = Memory()
        # The user's own routines - see routines.py.
        self.routines = Routines(let_in=lambda satellite: not self._gone_from_file(satellite))

        # The user's own ntfy server, both ways: alerts said out loud as they arrive,
        # and things sent to their phone. None if it isn't set up - see ntfy.py.
        self.ntfy = Ntfy.from_config(config, os.getenv("NTFY_TOKEN"), on_alert=self._alert)
        # With no ntfy there's still a night for the battery watcher to keep quiet in, read
        # from the same settings and kept by the same function - see _make_battery_watcher.
        # Kept, so a night changed on the settings page reaches it - see apply_settings.
        ntfy_settings = config.section("ntfy")
        self._night = Ntfy("", quiet_from=ntfy_settings.get("quiet_from", "23:00"),
                           quiet_until=ntfy_settings.get("quiet_until", "07:00"))

        # Home Assistant, the brain's own client - for watching the batteries, and what
        # the satellites' sessions ask of it besides is theirs - or None until it's set
        # up. See batteries.py.
        self.home = HomeAssistant.from_config(config, os.getenv("HA_TOKEN"))
        # What Home Assistant's entities are, read every few minutes and shared with every
        # session's own HomeAssistant - see ha_registry.py.
        # Its own socket, as reading it all takes longer than the voice path's calls may.
        self.registry = (HomeRegistry(HomeSocket(self.home.url, os.getenv("HA_TOKEN"), timeout=REGISTRY_TIMEOUT))
                         if self.home else None)
        if self.home:
            self.home.registry = self.registry
        self.batteries = self._make_battery_watcher() if self.home else None
        # Home Assistant's own weather, and which of its weather entities to use, for a
        # home with several - see weather.py.
        self.weather = (Weather(self.home, ChoiceStore(os.path.join(paths.DATA_DIR, "weather.json"), {"entity": None}))
                        if self.home else None)
        # Home Assistant's to-do lists, and which is the main one, for "add milk to my list"
        # - see lists.py.
        self.lists = (Lists(self.home, ChoiceStore(os.path.join(paths.DATA_DIR, "lists.json"), {"main": None}))
                      if self.home else None)
        # The PC, through HASS.Agent and Wake-on-LAN, and which Wake-on-LAN button wakes which
        # PC, for a home with several - see pc.py.
        self.pc = (PCPower(self.home, ChoiceStore(os.path.join(paths.DATA_DIR, "pcs.json"), {"wake": {}}))
                   if self.home else None)
        # The user's calendars, read from Google itself rather than through Home Assistant,
        # and shared by every session - or None until Google's keys are set. See
        # google_calendar.py.
        self.calendar: Optional[GoogleCalendar] = GoogleCalendar.from_env(config)

        # Every satellite's timers and alarms, rung on the satellite each was set from.
        # Started in run(), once what rings on no satellite has gone to the default.
        self.clock = Clock(on_timer_finished=self._announce, on_unanswered=self._unanswered,
                           on_change=self._send_alarms, can_hear=lambda satellite: satellite in self._caught_up,
                           let_in=lambda satellite: not self._gone_from_file(satellite), config=config, start=False)

        # One Piper, and one Nemotron, which every session shares.
        self.tts = TextToSpeech(config)
        self.stt = _speech_to_text(config)
        self.phrase = self._make_phrase()

        # The page for looking after Nova from a phone or a laptop, or None if it's
        # switched off - see settings_page.py. It looks each satellite's session up at
        # every request, so it never holds one which has gone.
        self.settings_page = SettingsPage.from_config(config, self)

    def _make_battery_watcher(self) -> BatteryWatcher:
        """
        The watcher for the batteries, which warns the user's phone and says it aloud as
        well - see BatteryWatcher. It's started by run().

        Returns:
            The watcher, set up from config.yaml's batteries section, and keeping ntfy's
            quiet hours - or, with no ntfy, the brain's own night's.
        """
        settings = self.config.section("batteries")
        choices = BatteryChoices(os.path.join(paths.DATA_DIR, "batteries.json"),
                                 settings.get("mobile_marks", [5]), settings.get("other_marks", [15, 5]))

        def send(message: str, title: str) -> bool:
            # The phone's topic at each warning, so one set or cleared on the settings page
            # is used from the next - with none, it's as if there were no ntfy.
            phone = self.ntfy.phone if self.ntfy else ""
            return bool(phone) and self.ntfy.send(phone, message, title=title)

        quiet = (self.ntfy or self._night).quiet
        return BatteryWatcher(self.home, choices, say=self._say_battery, send=send, quiet=quiet,
                              check_every=settings.get("check_every", 60),
                              stale_after=settings.get("stale_after", 3600))

    def apply_settings(self, changes: Dict[Tuple[str, str], Any]) -> List[Tuple[str, str]]:
        """
        Uses settings saved on the settings page straight away, with no restart. Each is
        put in the running settings, which most parts read as they use them - a session's
        silence, say. The parts which keep a copy of one are told.

        Args:
            changes: Each setting, as (section, name), to its new value - only those used
                straight away, e.g. {("ntfy", "quiet_from"): "22:00"}.

        Returns:
            Those which a part couldn't be told of, so are only used once Nova restarts.
            Each part's failure is logged.
        """
        self.config.apply(changes)
        unused = []
        ntfy_changes = [key for key in changes if key[0] == "ntfy" and key[1] in RETOPIC]
        if ntfy_changes:
            settings = self.config.section("ntfy")
            # All five, as they are now - retopic falls back to ntfy's own night for a time which is missing.
            told = [settings.get(name) or "" for name in RETOPIC]
            for part in (self.ntfy, self._night):
                if part is None:
                    continue
                try:
                    part.retopic(*told)
                except Exception:
                    logger.exception("ntfy couldn't be told of the settings saved, so they're used once Nova restarts")
                    unused += [key for key in ntfy_changes if key not in unused]
            if self.ntfy is None:
                # Nothing is sent to a topic until Nova restarts with an ntfy server to send
                # through - one saved with its address, say. The night is used now, by the
                # battery watcher.
                unused += [key for key in ntfy_changes
                           if key[1] not in ("quiet_from", "quiet_until") and key not in unused]
        if ("logging", "log_transcripts") in changes:
            try:
                logging_setup.log_transcripts(changes[("logging", "log_transcripts")])
            except Exception:
                logger.exception("Keeping what's said in the logs couldn't be changed until Nova restarts")
                unused.append(("logging", "log_transcripts"))
        if ("voice", "speed") in changes and not self._change_speed(changes[("voice", "speed")]):
            unused.append(("voice", "speed"))
        if ("thresholds", "recording_timeout") in changes:
            self._send_states()
        return unused

    def _send_states(self):
        """Has every session connected send its satellite its state again."""
        with self._lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            try:
                session.send_state()
            except Exception:
                logger.exception(f"The {session.satellite.name} satellite couldn't be sent the longest request, "
                                 "so it keeps the old one until it reconnects")

    def _change_speed(self, speed: float) -> bool:
        """
        Has Piper speak at a new speed from the next reply, and makes the phrase each
        satellite says while it can't reach the brain again at it, sending it to each
        connected - so Nova sounds the same either way.

        Args:
            speed: How fast, e.g. 1.25.

        Returns:
            Whether Piper speaks at it now. Anything which went wrong is logged.
        """
        try:
            self.tts.set_speed(speed)
        except Exception:
            # set_speed changes the speed before it stops the Piper left waiting at the
            # old one, so it can fail with the new speed already in use.
            if self.tts.speed != speed:
                logger.exception("Piper couldn't be told of the new speaking speed, so it's used once Nova restarts")
                return False
            logger.exception("Piper speaks at the new speed, but something went wrong getting it ready")
        try:
            self._send_phrase()
        except Exception:
            logger.exception("The phrase for when the server can't be reached couldn't be made again, "
                             "so it stays at the old speaking speed")
        return True

    def _send_phrase(self):
        """Makes the phrase again at Piper's speed, and sends it to each satellite connected."""
        # Made before the lock is taken, as Piper takes a moment over it.
        phrase = self._make_phrase()
        if phrase is None:
            # The old one is better than none - for a satellite connecting later too.
            logger.warning("The phrase for when the server can't be reached stays at the old speaking speed")
            return
        # Which are connected is seen with the lock held, so one just connecting either
        # is sent it here or is caught up after, with it - see _catch_up.
        with self._sending_phrase:
            self.phrase = phrase
            with self._lock:
                satellites = [session.satellite for session in self.sessions.values()]
            for satellite in satellites:
                if satellite.connected:
                    satellite.send_phrase(*phrase)

    # The satellites

    def satellites(self) -> List[str]:
        """
        Returns:
            Every satellite which may connect, the default first - connected or not.
        """
        return self.passwords.names()

    def default_satellite(self) -> Optional[str]:
        """
        The satellite for whatever belongs to none - ntfy's alerts, a routine which
        doesn't say, and what rang before there were satellites.

        Returns:
            The first in the passwords' file - the first added, unless another has
            been made the default - or None if there are none.
        """
        names = self.passwords.names()
        return names[0] if names else None

    def add_satellite(self, name: str) -> str:
        """
        Lets a new satellite in, as the settings page asks - at its first try, as its
        password is read afresh as each satellite connects.

        Args:
            name: Its Pi's machine name, e.g. "bedroom".

        Returns:
            Its password, made up here, for the page to show once.

        Raises:
            ValueError: If the name isn't a machine name, or is already there.
        """
        problem = name_problem(name)
        if problem:
            raise ValueError(problem)
        return self.passwords.add(name)

    def renew_password(self, name: str) -> str:
        """
        Gives a satellite a new password, as the settings page asks, and lets it go if
        it's connected - so it's refused until its setup has the new one. Its alarms,
        routines and volumes are kept, as they're by its name.

        Args:
            name: The satellite.

        Returns:
            The new password, for the page to show once.

        Raises:
            ValueError: If there's no such satellite.
        """
        password = self.passwords.renew(name)
        # After it's saved, never before - see _start_session, which lets go of one
        # whose session is made after this looks.
        with self._lock:
            session = self.sessions.get(name)
        if session is not None:
            logger.info(f"The {name} satellite has a new password, so it's let go until its setup has it")
            # Saved by now, so the old one is refused: the new one is given however
            # letting go goes, or the satellite couldn't connect until given another.
            self._let_go(session)
        return password

    def remove_satellite(self, name: str) -> Optional[bool]:
        """
        Stops letting a satellite in, as the settings page asks, and handles it
        straight away rather than at the server's next look - see _on_removed.

        Args:
            name: The satellite.

        Returns:
            Whether its copy of the alarms was emptied - False if it wasn't
            connected, or couldn't be sent the empty one, so it still rings them
            if it's switched on within the week. None if there was none to remove.
        """
        # One from a removal before, by the command line, isn't this one's.
        with self._lock:
            self._emptied.pop(name, None)
        if not self.passwords.remove(name):
            return None
        # Handled by this look, or by the server's poll before it - which this
        # waits for.
        self._shut_out_removed()
        with self._lock:
            return self._emptied.pop(name, False)

    def make_default(self, name: str):
        """
        Makes a satellite the default, as the settings page asks. ntfy's alerts,
        the spoken warnings and routines without a satellite go to it from then on,
        as each asks default_satellite as it goes.

        Args:
            name: The satellite.

        Raises:
            ValueError: If there's no such satellite.
        """
        self.passwords.make_default(name)
        logger.info(f"The {name} satellite is the default now")

    def last_moved(self) -> float:
        """
        When the brain was last known to be getting on with everything, by
        time.monotonic(): now, unless a session has been still since longer ago -
        stuck in a turn, which nothing else would notice. See STOP_CHECK.

        Returns:
            The earliest of now and each session's moved_at.
        """
        with self._lock:
            sessions = list(self.sessions.values())
        return min([time.monotonic()] + [session.moved_at for session in sessions])

    def session_for(self, satellite: Optional[str]) -> Optional[Session]:
        """
        Args:
            satellite: A satellite's name, or None for the default one.

        Returns:
            Its session, or None if it isn't connected.
        """
        name = satellite or self.default_satellite()
        with self._lock:
            session = self.sessions.get(name) if name else None
        return session if session is not None and session.satellite.connected else None

    def _stoppable_sessions(self) -> List[Tuple[str, Session]]:
        """
        Returns:
            Each connected satellite with something ringing on it, or its Nova
            talking, and its session - the default first.
        """
        found = []
        for name in self.satellites():
            session = self.session_for(name)
            if session is not None and (self.clock.is_ringing(satellite=name) or session.activity == "speaking"):
                found.append((name, session))
        return found

    def stoppable(self) -> bool:
        """Whether the settings page's Stop has anything to stop, on any satellite."""
        return bool(self._stoppable_sessions())

    def stop_everywhere(self) -> List[str]:
        """
        The settings page's Stop: Home Assistant's, on every satellite with something
        to stop - see Session.stop_what_rings_and_speaks.

        Returns:
            The names of the satellites stopped.
        """
        stopped = self._stoppable_sessions()
        for _, session in stopped:
            session.stop_what_rings_and_speaks()
        return [name for name, _ in stopped]

    def serve(self):
        """
        Listens for satellites on backend.port, on this machine alone, each on a thread
        of its own - until stop(), or a stop signal.
        """
        server = _Server(self.port, self)
        with self._lock:
            if self._stopped.is_set():
                server.server_close()
                return
            # First, so whatever sees the brain listening also knows where.
            self.port = server.server_address[1]
            self._server = server
        logger.info(f"Listening for satellites on port {self.port}")
        try:
            server.serve_forever(poll_interval=STOP_CHECK)
        finally:
            with self._lock:
                self._server = None
            server.server_close()

    def stop(self):
        """Stops listening, and lets every satellite go. Safe from any thread, and more than once."""
        with self._lock:
            self._stopped.set()
            server = self._server
            sessions = list(self.sessions.values())
        if server is not None:
            server.shutdown()
        for session in sessions:
            session.close()
            session.satellite.close()

    def _serve_satellite(self, sock: socket.socket, address: str):
        """
        Lets one satellite in, and runs its session until it goes.

        Args:
            sock: Its connection, which the caller closes once this returns.
            address: Where it connected from - for the log.
        """
        # It bounds how long a send to a satellite which has stalled can block, and
        # how soon one let go - see RemoteSatellite.close - is noticed.
        sock.settimeout(SOCKET_TIMEOUT)
        stream = Stream(sock)
        # The satellite says hello as it connects - see _catch_up.
        hello_at = time.monotonic()
        admitted = self._admit(stream, address)
        if admitted is None:
            return
        hello, password = admitted
        name = hello["name"]
        logger.info(f"The {name} satellite connected")
        if hello["unclean"]:
            self._tell_restarted(name)

        satellite = RemoteSatellite(stream, hello)
        # Read from the start, so the satellite's pings are answered while its session is made.
        reading = threading.Thread(target=satellite.serve, daemon=True, name=f"satellite-{name}")
        reading.start()
        session = None
        try:
            session = self._start_session(satellite, password)
            if session is not None and self._catch_up(session, hello_at):
                session.run()
        except Exception:
            logger.exception(f"The {name} satellite's session failed")
        finally:
            satellite.close()
            reading.join()
            if session is not None:
                session.close()
                self._forget(session)
        logger.info(f"The {name} satellite went")

    def _admit(self, stream: Stream, address: str) -> Optional[Tuple[Dict, str]]:
        """
        The brain's side of the handshake - see protocol.admit.

        Returns:
            The satellite's hello and the password it proved, or None if it wasn't let in.
        """
        named: List[str] = []
        # The password as it was read, for _start_session to tell whether it has
        # changed since - only ever compared, never logged.
        read: List[Optional[str]] = []

        def password_for(name: str) -> Optional[str]:
            named.append(name)
            read.append(self.passwords.get(name))
            return read[-1]

        try:
            hello = admit(stream, password_for)
        except Refused as e:
            self._turned_away(named[0] if named else address, f"Refused a satellite: {e}")
            return None
        except ConnectionError:
            if not named:
                logger.info(f"{address} hung up before saying which satellite it was")
                return None
            # A satellite hangs up as soon as the brain can't prove it knows its
            # password - so this, rather than a refusal, is what a wrong one looks like.
            # It also hangs up on a brain which takes longer than SOCKET_TIMEOUT to
            # answer it, though, so it's only most likely.
            self._turned_away(named[0], f"The {named[0]} satellite hung up part way through the handshake, "
                                        f"so most likely its password isn't the one the brain has for it "
                                        f"- or the brain was too slow to answer it")
            return None
        except (ProtocolError, OSError) as e:
            logger.warning(f"Dropped {address}, which didn't finish the handshake: {e}")
            return None
        with self._lock:
            self._why_turned_away.pop(hello["name"], None)
        return hello, read[-1]

    def _passwords_file(self) -> Optional[Tuple[int, int, int]]:
        """
        The passwords' file, as it is now - which changes with every add-satellite and
        remove-satellite, as each saves it afresh, as a new file.

        Returns:
            Its inode, modification time and size, or None if there isn't one.
        """
        try:
            found = os.stat(self.passwords.path)
        except OSError:
            return None
        return found.st_ino, found.st_mtime_ns, found.st_size

    def _shut_out_removed(self, wait: bool = True):
        """
        Handles each satellite removed since the passwords' file was last looked at -
        on the settings page, or with remove-satellite run by itself, which the brain
        only hears of through the file. See _on_removed, the one place either is
        handled. A password is only proved as a satellite connects, so a satellite
        removed otherwise stayed for as long as it stayed connected.

        Args:
            wait: Whether to wait for a look already under way. The server's poll
                doesn't, as it must never stop the brain listening: the look under
                way sees the file as it is, and the poll looks again next time.
        """
        if not self._removing.acquire(blocking=wait):
            return
        try:
            now = self._passwords_file()
            if now == self._passwords_seen:
                return
            # Noted first, so a file which can't be read is said once, not every check.
            self._passwords_seen = now
            try:
                # A missing file is never every satellite removed - see _gone_from_file.
                names = self.passwords.names(missing_ok=False)
            except Exception as e:
                logger.error(f"Couldn't read the satellites' passwords to see which are still let in: {e}")
                return
            with self._lock:
                connected = list(self.sessions)
            # Those connected too, for one added and removed again between two looks.
            gone = [name for name in dict.fromkeys(self._names_seen + connected) if name not in names]
            self._names_seen = names
            for name in gone:
                try:
                    self._on_removed(name, names)
                except Exception:
                    logger.exception(f"Couldn't finish handling the {name} satellite's removal")
        finally:
            self._removing.release()

    def _on_removed(self, name: str, names: List[str]):
        """
        Handles a satellite which has been removed, whether it's connected or not. Its
        timers, alarms and routines go to the default, so nothing rings nowhere - or
        stay on none with no satellite left, for the next added. Its volumes and mute
        stay in the data folder, so added again it has them back.

        Args:
            name: The satellite.
            names: The satellites still let in, as the removal was read - the first
                is the default. Never read again here: a file gone missing since
                would read as none, and what was the satellite's would ring nowhere.
        """
        # Out of sessions first, so neither a change on the clock nor the hourly
        # resend can send it its real copy after the empty one - see _send_alarms_to.
        with self._lock:
            session = self.sessions.pop(name, None)
            self._caught_up.discard(name)
        emptied = False
        if session is not None:
            logger.info(f"The {name} satellite was removed, so it's let go")
            # Emptied before it's let go, so it never rings its old alarms from its
            # copy - as it would once it couldn't reach the brain, for a week.
            with self._sending_alarms:
                emptied = session.satellite.connected and session.satellite.send_alarms([])
            self._let_go(session)
        with self._lock:
            self._emptied[name] = emptied
        if not emptied:
            logger.warning(f"The {name} satellite was removed while it wasn't connected, so it still has a copy "
                           f"of its alarms for the next week, and rings them if it's switched on before then")
        default = names[0] if names else None
        if default is None:
            with self._lock:
                self._unowned_given = False
        self.clock.move_satellite(name, default)
        self.routines.forget_satellite(name, default)

    def _let_go(self, session: Session):
        """
        Ends a satellite's session and lets the satellite go - the connection even if
        ending the session fails, which is logged.
        """
        name = session.satellite.name
        try:
            session.close()
        except Exception:
            logger.exception(f"Couldn't let go of the {name} satellite's session properly")
        finally:
            session.satellite.close()

    def _turned_away(self, name: str, why: str):
        """Logs why a satellite was turned away - once, however often it tries again, until it's let in."""
        with self._lock:
            if self._why_turned_away.get(name) == why:
                return
            self._why_turned_away[name] = why
        logger.warning(why)

    def _start_session(self, satellite: RemoteSatellite, password: Optional[str] = None) -> Optional[Session]:
        """
        Makes a satellite's session, in place of any it had - from a connection which
        went without the brain noticing yet, say.

        Args:
            satellite: The satellite, just let in.
            password: The password it proved, or None not to check it's still its own.

        Returns:
            The session, or None if the brain is stopping or the satellite was removed,
            or given a new password, as it connected.
        """
        name = satellite.name
        with self._connecting:
            with self._lock:
                old = self.sessions.pop(name, None)
                self._caught_up.discard(name)
            if old is not None:
                logger.info(f"The {name} satellite connected again, so its last connection is let go")
                # Before the new session is made, since both would be the same device
                # in Home Assistant, connecting with the same MQTT client id.
                self._let_go(old)
            self._give_out_orphans()
            session = Session(self, satellite)
            with self._lock:
                stopped = self._stopped.is_set()
                if not stopped:
                    self.sessions[name] = session
        if stopped:
            self._let_go(session)
            return None

        # Checked once it's registered, never before: remove-satellite is only seen
        # through the file (see _shut_out_removed), which lets go of whatever is in
        # sessions as it reads it. A removal before this is seen here; one after it is
        # seen there, as the session is in sessions by then - so whichever way round
        # the two threads run, a satellite removed while its session was being made
        # can't stay. The same goes for a new password, which renew_password saves
        # before it looks in sessions.
        if self._removed(name, password):
            logger.info(f"The {name} satellite was removed, or given a new password, as it connected, so it's let go")
            if self._gone_from_file(name):
                # Removed rather than given a new password, and online a moment ago
                # - so the page's question said nothing of its copy - which
                # _on_removed found no session to empty. It proved itself on this
                # connection, so it's emptied here.
                with self._sending_alarms:
                    if satellite.connected:
                        satellite.send_alarms([])
            self._let_go(session)
            self._forget(session)
            return None
        return session

    def _removed(self, name: str, password: Optional[str] = None) -> bool:
        """
        Whether a satellite has had its password removed - or, given the one it proved,
        changed. One which can't be told, because the file can't be read, is taken to
        be let in, as _shut_out_removed does.
        """
        try:
            if password is not None:
                return not hmac.compare_digest((self.passwords.get(name) or "").encode(), password.encode())
            return name not in self.passwords.names()
        except Exception as e:
            logger.error(f"Couldn't read the satellites' passwords to see if the {name} satellite is still let in: {e}")
            return False

    def _gone_from_file(self, name: str) -> bool:
        """
        Whether a satellite is missing from the passwords' file. A file which is
        missing, or can't be read, says nothing either way, so never counts - that
        would be every satellite removed at once.
        """
        try:
            return name not in self.passwords.names(missing_ok=False)
        except Exception:
            return False

    def _catch_up(self, session: Session, hello_at: float) -> bool:
        """
        Brings a satellite which has just connected up to date: what the brain owns,
        for it to mute by and to ring by while it can't reach the brain, and the phrase
        to say then - and what happened on it while it couldn't.

        Args:
            session: The satellite's session, just made.
            hello_at: When it said hello, by time.monotonic().

        Returns:
            Whether it said what happened while it couldn't. One which didn't is let
            go, to connect again and say - see AWAY_WAIT.
        """
        satellite = session.satellite
        # Sent whatever its last state was, or a satellite muted from Home Assistant
        # while it was away would come back unmuted.
        session.send_state()
        self._send_alarms_to(satellite)
        with self._sending_phrase:
            if self.phrase is not None:
                satellite.send_phrase(*self.phrase)
        # What rang and what was dismissed on it while it couldn't reach the brain, so
        # nothing dismissed there rings again here, nor is said to have been missed -
        # and what it was still ringing, which rings on here.
        try:
            away = satellite.away_reports.get(timeout=AWAY_WAIT)
        except queue.Empty:
            logger.warning(f"The {satellite.name} satellite didn't say what happened while it couldn't reach "
                           f"the brain, so it's let go, to connect again and say")
            return False
        self.clock.apply_away(satellite.name, away["rang"], away["dismissed"], away["ringing"])
        # Only now it's saved does the satellite stop ringing what it was, and forget
        # what it said - lost before then, the next brain hears it all again.
        satellite.settle_away()
        # All of it, from the hello, counts against the satellite's SETTLE_WAIT - even
        # starting its session, which that leaves out - so it's logged, to be watched.
        logger.info(f"The {satellite.name} satellite was caught up {time.monotonic() - hello_at:.2f} seconds "
                    f"after its hello")
        with self._lock:
            if self.sessions.get(satellite.name) is session:
                self._caught_up.add(satellite.name)
        return True

    def _forget(self, session: Session):
        """Takes a session which has ended off the brain - unless another has already taken its place."""
        name = session.satellite.name
        with self._lock:
            if self.sessions.get(name) is not session:
                return
            del self.sessions[name]
            self._caught_up.discard(name)

    def _give_out_orphans(self):
        """
        Gives whatever rings on no satellite, or on one no longer let in, to the
        default - or leaves it on none with no satellite yet, for the first added.
        On none is what was set before there were satellites, when the Pi was all of
        Nova, and the last satellite's once it's removed. On one no longer let in is
        what was a satellite's removed while the brain was down - with
        remove-satellite run with it stopped, or across an update - which
        _on_removed never saw. That one's routines run on the default too, and those
        kept for it while it was away go there.

        Run as the brain starts, and as each satellite connects - holding _removing,
        so it never reads which satellites there are part way through a removal,
        and gives what's on none to the one being removed.
        """
        with self._removing:
            try:
                # A missing file is never every satellite removed - see _gone_from_file.
                names = self.passwords.names(missing_ok=False)
            except FileNotFoundError:
                # Only as it starts, as no satellite can connect with no file. A
                # fresh brain has none - but one with alarms on a satellite was
                # likely given the wrong config folder.
                if any(self.clock.satellites()):
                    logger.warning(f"There's no {self.passwords.path}, but there are alarms or timers for "
                                   f"satellites - is the config folder the right one?")
                else:
                    logger.info("No satellites have been added yet")
                return
            except Exception as e:
                logger.error(f"Couldn't read the satellites' passwords to see what belongs to none: {e}")
                return
            default = names[0] if names else None
            with self._lock:
                unowned = default is not None and not self._unowned_given
                if unowned:
                    self._unowned_given = True
            if unowned:
                self.clock.move_satellite(None, default)
                self.routines.forget_satellite(None, default)
            orphans = self.clock.satellites() | self.routines.missed_satellites()
            orphans |= {routine["satellite"] for routine in self.routines.all()}
            for name in sorted(orphan for orphan in orphans if orphan and orphan not in names):
                logger.warning(f"The {name} satellite was removed while the brain was down, so it still has a copy "
                               f"of its alarms for the next week, and rings them if it's switched on before then - "
                               f"what was its goes to {default or 'the next satellite added'}")
                if default is None:
                    with self._lock:
                        self._unowned_given = False
                self.clock.move_satellite(name, default)
                self.routines.forget_satellite(name, default)

    def _send_alarms_to(self, satellite: RemoteSatellite):
        """
        Sends a satellite its copy of the timers and alarms which ring on it - unless
        it's no longer the one connected by its name. A copy made just before it was
        removed would otherwise reach it after its empty one (see _on_removed), and
        it would ring that for a week.
        """
        with self._sending_alarms:
            with self._lock:
                session = self.sessions.get(satellite.name)
            if session is None or session.satellite is not satellite:
                return
            satellite.send_alarms(self.clock.copy_for(satellite.name))

    def _send_alarms(self):
        """Sends each satellite connected its copy - whenever anything is set, rings or is dismissed."""
        with self._lock:
            satellites = [session.satellite for session in self.sessions.values()]
        for satellite in satellites:
            if satellite.connected:
                self._send_alarms_to(satellite)

    def _send_alarms_hourly(self):
        """Sends each satellite its copy every ALARMS_RESENT as well, until the brain stops."""
        while not self._stopped.wait(ALARMS_RESENT):
            try:
                self._send_alarms()
            except Exception as e:
                logger.error(f"Couldn't send the satellites their copies: {e}")

    def _make_phrase(self) -> Optional[Tuple[bytes, int]]:
        """
        Says FALLBACK_PHRASE with Piper, for each satellite to keep.

        Returns:
            Its 16-bit samples and their rate - or None if Piper said nothing, when
            each satellite keeps the recording which ships with it.
        """
        pieces = list(self.tts.stream(FALLBACK_PHRASE, remember=False))
        if not pieces:
            logger.warning("Piper didn't say the phrase for when the server can't be reached")
            return None
        audio = (np.clip(np.concatenate(pieces), -1, 1) * 32767).astype("<i2").tobytes()
        if len(audio) > protocol.MAX_AUDIO:
            logger.warning("The phrase for when the server can't be reached is too long to send")
            return None
        return audio, self.tts.sample_rate

    # What goes off by itself

    def _announce(self, message, sound="announcement_sound.wav", boost=False, still_wanted=None, satellite=None,
                  **playback):
        """
        Says what the clock rings on the satellite it was set from, as Session.announce
        - or nowhere, if that satellite isn't connected, since it rings from its copy.

        Args:
            satellite: Where it rings - or None for the default satellite.
            message, sound, boost, still_wanted, playback: As Session.announce has them. Whatever the
            clock says is ringing, so its sound is never switched off.
        """
        session = self.session_for(satellite)
        if session is not None:
            session.announce(message, sound, boost, still_wanted=still_wanted, ringing=True, **playback)

    def _alert(self, alert) -> bool:
        """
        Says an alert from ntfy on the default satellite, as it arrives - see
        Session.alert.

        Args:
            alert: The ntfy Alert.

        Returns:
            Whether it was said - Stop, the wake word and an overdue sound count as
            said, as the user has heard it. One which wasn't, because saying it failed
            or the default satellite was away, is kept for "any notifications?".
        """
        session = self.session_for(None)
        if session is None:
            logger.warning(f"An alert from {alert.topic} wasn't said, as the default satellite isn't connected, "
                           f"so it's kept for \"any notifications?\"")
            return False
        if not session.alert(alert):
            logger.warning(f"An alert from {alert.topic} wasn't said, so it's kept for "
                           f"\"any notifications?\"")
            return False
        return True

    def _say_battery(self, message: str) -> bool:
        """
        Says a battery warning on the default satellite, as BatteryWatcher has it said.

        Args:
            message: What to say, e.g. "Your MacBook Pro's battery is down to 15%."

        Returns:
            Whether it was said - it wasn't with the default satellite away, or if
            saying it failed, and the watcher then sends it or holds it instead. Never
            raises.
        """
        session = self.session_for(None)
        if session is None:
            logger.warning("A battery warning wasn't said, as the default satellite isn't connected")
            return False
        try:
            said = session.announce(message)
        except Exception:
            # The satellite going away mid-sentence, most likely.
            logger.exception("A battery warning couldn't be said")
            return False
        if not said:
            logger.warning("A battery warning wasn't said")
        return bool(said)

    def _unanswered(self, timer, kind, satellite=None):
        """
        Sends a reminder nobody answered to the user's phone, once it's stopped ringing.
        Only a reminder: a plain timer or an alarm going unanswered has nothing more
        to say there.

        Args:
            timer: What stopped ringing.
            kind: "clock" for a timer or reminder, "alarm" for an alarm.
            satellite: Where it rang - the phone is the same for every satellite.
        """
        # Read once, so a topic cleared on the settings page meanwhile is never sent to as "".
        topic = self.ntfy.reminders if self.ntfy else ""
        if topic and kind == "clock" and timer.get("label"):
            label = timer["label"]
            self.ntfy.send_soon(topic, label[:1].upper() + label[1:], title="Reminder nobody answered")

    def run_routine(self, routine) -> bool:
        """
        Runs one of the user's routines by itself - at its time, or from the settings
        page - on its satellite, as Session.run_routine.

        Args:
            routine: The routine, as Routines has it - with the satellite it runs on,
                or None for the default.

        Returns:
            Whether it ran - it didn't with its satellite away.
        """
        session = self.session_for(routine.get("satellite"))
        if session is None:
            logger.warning(f"The {routine['name']} routine didn't run, as its satellite isn't connected",
                           extra=TRANSCRIPT)
            return False
        session.run_routine(routine)
        return True

    def _run_timed(self, routine, due_at: float):
        """
        Runs a routine which has come due - or, with its satellite away, keeps it until
        it's back (see _catch_up_routines), as Routines has already marked it as run
        for the day.

        Args:
            routine: The routine, as Routines has it.
            due_at: When it came due, as an epoch timestamp.
        """
        if self.run_routine(routine):
            return
        satellite = routine.get("satellite") or self.default_satellite()
        if satellite:
            self.routines.missed(routine, satellite, due_at)

    def _catch_up_routines(self):
        """
        Runs, on each satellite which is back, the routines which came due while it
        was away - or, for any more than MISSED_ROUTINE_GRACE late, says they didn't.
        """
        with self._lock:
            back = list(self._caught_up)
        now = time.time()
        for satellite in back:
            session = self.session_for(satellite)
            if session is None:
                continue
            # Only those still there - one deleted meanwhile is let go.
            missed = [(entry, self.routines.get(entry["id"])) for entry in self.routines.take_missed(satellite)]
            missed = [(entry, routine) for entry, routine in missed if routine is not None]
            skipped = [entry for entry, _ in missed if now - entry["due_at"] >= MISSED_ROUTINE_GRACE]
            for entry, routine in missed:
                if entry not in skipped:
                    logger.info(f"Running the {routine['name']} routine, which came due while the {satellite} "
                                f"satellite was away", extra=TRANSCRIPT)
                    threading.Thread(target=self._run_timed, args=(routine, entry["due_at"]), daemon=True,
                                     name="routine").start()
            if skipped:
                logger.info(f"Telling the {satellite} satellite {len(skipped)} routines didn't run while it was away")
                threading.Thread(target=session.announce, args=(_skipped_message(skipped),), daemon=True,
                                 name="routine").start()

    def _run_routines_when_due(self):
        """
        Runs each routine at its time, for as long as the brain runs - see Routines.due
        - and those which came due while their satellite was away, once it's back.
        """
        while self.running:
            # One check going wrong mustn't stop the rest for good.
            try:
                for routine in self.routines.due():
                    threading.Thread(target=self._run_timed, args=(routine, time.time()), daemon=True,
                                     name="routine").start()
                self._catch_up_routines()
            except Exception as e:
                logger.error(f"Couldn't check the routines: {e}")
            time.sleep(ROUTINE_CHECK)

    # Starting and stopping

    def _tell_restarted(self, satellite: str):
        """Tells the user's phone a satellite didn't stop properly last time - once in a while, at most."""
        topic = self.ntfy.restarts if self.ntfy else ""
        if not topic:
            return
        with self._lock:
            if time.time() - self._restarts_told.get(satellite, 0) < RESTART_NOTICE_GAP:
                return
            self._restarts_told[satellite] = time.time()
        logger.warning(f"The {satellite} satellite didn't stop properly, so telling the user's phone")
        self.ntfy.send_soon(topic,
                            f"{self.name}'s {satellite} satellite stopped unexpectedly, and has started again.",
                            title=f"{self.name}'s {satellite} satellite restarted")

    def _note_start(self):
        """
        Leaves RUNNING_MARK, which _clean_up takes away - telling the user's phone if
        the last run left one too, since then it never stopped properly.
        """
        try:
            last = storage.load_json(RUNNING_MARK)
        except (OSError, ValueError):
            # Half written as the power went, most likely - which is a restart too.
            last = {}
        told_at = (last or {}).get("told_at", 0)
        topic = self.ntfy.restarts if self.ntfy else ""
        if last is not None and topic and time.time() - told_at >= RESTART_NOTICE_GAP:
            logger.warning("The last run didn't stop properly, so telling the user's phone")
            self.ntfy.send_soon(topic, f"{self.name} stopped unexpectedly, and has started again.",
                                title=f"{self.name} restarted")
            told_at = time.time()
        try:
            storage.save_json(RUNNING_MARK, {"started_at": time.time(), "told_at": told_at})
        except OSError as e:
            logger.warning(f"Couldn't note that the brain is running: {e}")

    def run(self):
        """
        Starts everything, and listens for satellites until stopped - by a stop signal
        (see shutdown) or stop() - then closes everything down.
        """
        self.running = True
        # Tells systemd it has started, and from here on that it's still working.
        watchdog.start()
        self._note_start()
        self._give_out_orphans()
        self.clock.start()
        # Alerts are only said from now on, once there's everything to say them with.
        if self.ntfy:
            self.ntfy.start()
        if self.settings_page:
            self.settings_page.start()
        threading.Thread(target=self._run_routines_when_due, daemon=True, name="routines").start()
        threading.Thread(target=self._send_alarms_hourly, daemon=True, name="alarm-copies").start()
        if self.batteries:
            self.batteries.start()
        if self.registry:
            self.registry.start()
        if self.calendar:
            threading.Thread(target=self._read_calendars, daemon=True, name="calendars").start()
        logger.info(f"{self.name}'s brain is ready")

        # However listening ends - a stop signal, stop() or something unexpected -
        # everything is closed down here, once, on the main thread.
        try:
            self.serve()
        finally:
            self._clean_up()

    def _read_calendars(self):
        """
        Reads Google's list of the calendars once, as the brain starts, so the activities -
        their names - are heard before anything's asked. A failure is only logged: the
        list is read again when the calendar is next asked about.
        """
        try:
            self.calendar.calendars()
        except READ_FAILURES as e:
            # Only the kind of failure - a requests error names the address, with a calendar's id in it.
            logger.warning(f"Couldn't read the calendars from Google yet: {type(e).__name__}")

    def restart(self) -> str:
        """
        Stops the brain cleanly, a moment from now, for systemd to start it again - the
        exit status says it's a restart, see RESTART_EXIT_STATUS and backend.py.

        Returns:
            What to tell the user.
        """
        if not os.getenv("INVOCATION_ID"):
            return "Nova isn't running as a service here, so restart it yourself."
        self.restarting = True
        logger.info("Restarting, as the settings page asked")
        # Stopping listening ends run(), which then cleans up as for any stop.
        timer = threading.Timer(RESTART_DELAY, self.stop)
        timer.daemon = True
        timer.start()
        return "Restarting - give it half a minute."

    def shutdown(self, *args):
        """
        Called when the program receives Ctrl+C or a termination request.

        Only stops listening, by raising SystemExit in it. The cleanup happens in run()
        once that has unwound. A second signal while it's shutting down is ignored,
        so it can't cut the cleanup short.
        """
        if self._stopping:
            return
        self._stopping = True
        logger.info("Shutting down...")
        self.running = False
        raise SystemExit(0)

    def _clean_up(self):
        """
        Closes everything down in order - the clock, so nothing else starts ringing,
        then the batteries, ntfy and the settings page, then every satellite's session, so its device
        in Home Assistant and its music stop - and then the Piper process kept waiting
        for the next thing to say.
        """
        self.running = False
        self.clock.stop()
        if self.batteries:
            self.batteries.stop()
        if self.registry:
            self.registry.stop()
        if self.ntfy:
            self.ntfy.stop()
        if self.settings_page:
            self.settings_page.stop()
        self.stop()
        self.tts.close()
        # Stopped properly, so the next start isn't taken for a restart after a problem.
        try:
            os.remove(RUNNING_MARK)
        except OSError:
            pass
        logger.info("Shut down cleanly")
