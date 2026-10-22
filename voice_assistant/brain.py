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

import logging
import os
import queue
import socket
import socketserver
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from voice_assistant import paths, protocol, storage, watchdog
from voice_assistant.clock import Clock
from voice_assistant.memory import Memory
from voice_assistant.ntfy import Ntfy
from voice_assistant.protocol import ProtocolError, Refused, admit
from voice_assistant.remote import RemoteSatellite
from voice_assistant.routines import Routines
from voice_assistant.satellite_link import SOCKET_TIMEOUT, Stream
from voice_assistant.satellite_passwords import SatellitePasswords
from voice_assistant.session import Session
from voice_assistant.settings_page import SettingsPage
from voice_assistant.text_to_speech import TextToSpeech
from voice_assistant.weather import Weather

logger = logging.getLogger(__name__)

# Left while the brain runs, and taken away when it stops properly - so finding it when
# starting means the last run didn't: it crashed, the watchdog restarted it, or the
# power went. See _note_start.
RUNNING_MARK = os.path.join(paths.DATA_DIR, "running")

# The least time between telling the user's phone that the brain, or one satellite,
# had to restart, so a crash which keeps happening doesn't flood it.
RESTART_NOTICE_GAP = 3600

# How often it's checked whether one of the user's routines is due, in seconds - a few
# times within each minute.
ROUTINE_CHECK = 20

# How often each satellite is sent its copy of the timers and alarms even when nothing
# has changed, in seconds. The copy holds a week, so a repeating alarm's next week only
# comes into it a day at a time.
ALARMS_RESENT = 3600

# How long a satellite which has just connected is given to say what rang and what was
# dismissed on it while it couldn't reach the brain, in seconds. It says so straight
# away, so this is only ever waited out by one which says nothing.
AWAY_WAIT = 2.0

# How often listening for satellites checks whether it's been stopped, in seconds - and
# so how often it tells the watchdog the brain is still working.
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


class _Server(socketserver.ThreadingTCPServer):
    """Listens for satellites, each on a thread of its own."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, brain: "Brain"):
        self.brain = brain
        super().__init__(("127.0.0.1", port), _Connection)

    def service_actions(self):
        # Run between connections, every STOP_CHECK: the brain's main loop is still
        # going - see watchdog.py.
        watchdog.beat()


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
        # The satellites it lets in, in the order they were added - see default_satellite.
        self.passwords = SatellitePasswords()

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
        self._server: Optional[_Server] = None
        self._stopped = threading.Event()

        # Whether run() is running - the routines are checked while it is.
        self.running = False
        # Set once a stop signal has arrived - see shutdown.
        self._stopping = False

        logger.info("Loading the brain...")
        # The assistant's own sound effects, which each satellite keeps and plays by name.
        self.sounds_folder = Path(paths.SOUNDS_DIR)
        # What the assistant knows about the user, learnt as they talk - see memory.py.
        self.memory = Memory()
        # The user's own routines - see routines.py.
        self.routines = Routines()
        self.weather = Weather(config=config)

        # The user's own ntfy server, both ways: alerts said out loud as they arrive,
        # and things sent to their phone. None if it isn't set up - see ntfy.py.
        self.ntfy = Ntfy.from_config(config, os.getenv("NTFY_TOKEN"), on_alert=self._alert)

        # Every satellite's timers and alarms, rung on the satellite each was set from.
        # Started in run(), once what rings on no satellite has gone to the default.
        self.clock = Clock(on_timer_finished=self._announce, on_unanswered=self._unanswered,
                           on_change=self._send_alarms, can_hear=lambda satellite: satellite in self._caught_up,
                           config=config, start=False)

        # One Piper, and one Nemotron, which every session shares.
        self.tts = TextToSpeech(config)
        self.stt = _speech_to_text(config)
        self.phrase = self._make_phrase()

        # The page for looking after Nova from a phone or a laptop, or None if it's
        # switched off - see settings_page.py. It looks each satellite's session up at
        # every request, so it never holds one which has gone.
        self.settings_page = SettingsPage.from_config(config, self)

    # The satellites

    def satellites(self) -> List[str]:
        """
        Returns:
            Every satellite which may connect, in the order they were added - connected or not.
        """
        return self.passwords.names()

    def default_satellite(self) -> Optional[str]:
        """
        The satellite for whatever belongs to none - ntfy's alerts, a routine which
        doesn't say, and what rang before there were satellites.

        Returns:
            The first satellite added, or None if there are none.
        """
        names = self.passwords.names()
        return names[0] if names else None

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
            self._server = server
        self.port = server.server_address[1]
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
        hello = self._admit(stream, address)
        if hello is None:
            return
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
            session = self._start_session(satellite)
            if session is not None:
                self._catch_up(session)
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

    def _admit(self, stream: Stream, address: str) -> Optional[Dict]:
        """
        The brain's side of the handshake - see protocol.admit.

        Returns:
            The satellite's hello, or None if it wasn't let in.
        """
        named: List[str] = []

        def password_for(name: str) -> Optional[str]:
            named.append(name)
            return self.passwords.get(name)

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
            self._turned_away(named[0], f"The {named[0]} satellite hung up part way through the handshake, "
                                        f"so its password isn't the one the brain has for it")
            return None
        except (ProtocolError, OSError) as e:
            logger.warning(f"Dropped {address}, which didn't finish the handshake: {e}")
            return None
        with self._lock:
            self._why_turned_away.pop(hello["name"], None)
        return hello

    def _turned_away(self, name: str, why: str):
        """Logs why a satellite was turned away - once, however often it tries again, until it's let in."""
        with self._lock:
            if self._why_turned_away.get(name) == why:
                return
            self._why_turned_away[name] = why
        logger.warning(why)

    def _start_session(self, satellite: RemoteSatellite) -> Optional[Session]:
        """
        Makes a satellite's session, in place of any it had - from a connection which
        went without the brain noticing yet, say.

        Returns:
            The session, or None if the brain is stopping.
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
                old.close()
                old.satellite.close()
            self._give_out_unowned()
            session = Session(self, satellite)
            with self._lock:
                stopped = self._stopped.is_set()
                if not stopped:
                    self.sessions[name] = session
        if stopped:
            session.close()
            return None
        return session

    def _catch_up(self, session: Session):
        """
        Brings a satellite which has just connected up to date: what the brain owns,
        for it to mute by and to ring by while it can't reach the brain, and the phrase
        to say then - and what happened on it while it couldn't.
        """
        satellite = session.satellite
        # Sent whatever its last state was, or a satellite muted from Home Assistant
        # while it was away would come back unmuted.
        session.send_state()
        self._send_alarms_to(satellite)
        if self.phrase is not None:
            satellite.send_phrase(*self.phrase)
        # What rang and what was dismissed on it while it couldn't reach the brain, so
        # nothing dismissed there rings again here, nor is said to have been missed.
        try:
            away = satellite.away_reports.get(timeout=AWAY_WAIT)
        except queue.Empty:
            away = None
        if away is not None:
            self.clock.apply_away(satellite.name, away["rang"], away["dismissed"])
        with self._lock:
            if self.sessions.get(satellite.name) is session:
                self._caught_up.add(satellite.name)

    def _forget(self, session: Session):
        """Takes a session which has ended off the brain - unless another has already taken its place."""
        name = session.satellite.name
        with self._lock:
            if self.sessions.get(name) is not session:
                return
            del self.sessions[name]
            self._caught_up.discard(name)

    def _give_out_unowned(self):
        """
        Gives everything which rings on no satellite - set before there were any, when
        the Pi was all of Nova - to the default satellite, once there is one. Only
        once, as nothing set since rings on none.
        """
        default = self.default_satellite()
        with self._lock:
            if self._unowned_given or default is None:
                return
            self._unowned_given = True
        self.clock.assign_unowned(default)

    def _send_alarms_to(self, satellite: RemoteSatellite):
        """Sends a satellite its copy of the timers and alarms which ring on it."""
        with self._sending_alarms:
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
            message, sound, boost, still_wanted, playback: As Session.announce has them.
        """
        session = self.session_for(satellite)
        if session is not None:
            session.announce(message, sound, boost, still_wanted=still_wanted, **playback)

    def _alert(self, alert):
        """
        Says an alert from ntfy on the default satellite, as it arrives - see
        Session.alert.

        Args:
            alert: The ntfy Alert.
        """
        session = self.session_for(None)
        if session is None:
            logger.warning(f"An alert from {alert.topic} wasn't said, as the default satellite isn't connected")
            return
        session.alert(alert)

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
        if self.ntfy and self.ntfy.reminders and kind == "clock" and timer.get("label"):
            label = timer["label"]
            self.ntfy.send_soon(self.ntfy.reminders, label[:1].upper() + label[1:], title="Reminder nobody answered")

    def run_routine(self, routine):
        """
        Runs one of the user's routines by itself - at its time, or from the settings
        page - on its satellite, as Session.run_routine.

        Args:
            routine: The routine, as Routines has it - with the satellite it runs on,
                or None for the default.
        """
        session = self.session_for(routine.get("satellite"))
        if session is None:
            logger.warning(f"The {routine['name']} routine didn't run, as its satellite isn't connected")
            return
        session.run_routine(routine)

    def _run_routines_when_due(self):
        """Runs each routine at its time, for as long as the brain runs - see Routines.due."""
        while self.running:
            # One check going wrong mustn't stop the rest for good.
            try:
                for routine in self.routines.due():
                    threading.Thread(target=self.run_routine, args=(routine,), daemon=True, name="routine").start()
            except Exception as e:
                logger.error(f"Couldn't check the routines: {e}")
            time.sleep(ROUTINE_CHECK)

    # Starting and stopping

    def _tell_restarted(self, satellite: str):
        """Tells the user's phone a satellite didn't stop properly last time - once in a while, at most."""
        if not (self.ntfy and self.ntfy.restarts):
            return
        with self._lock:
            if time.time() - self._restarts_told.get(satellite, 0) < RESTART_NOTICE_GAP:
                return
            self._restarts_told[satellite] = time.time()
        logger.warning(f"The {satellite} satellite didn't stop properly, so telling the user's phone")
        self.ntfy.send_soon(self.ntfy.restarts,
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
        if (last is not None and self.ntfy and self.ntfy.restarts
                and time.time() - told_at >= RESTART_NOTICE_GAP):
            logger.warning("The last run didn't stop properly, so telling the user's phone")
            self.ntfy.send_soon(self.ntfy.restarts, f"{self.name} stopped unexpectedly, and has started again.",
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
        self._give_out_unowned()
        self.clock.start()
        # Alerts are only said from now on, once there's everything to say them with.
        if self.ntfy:
            self.ntfy.start()
        if self.settings_page:
            self.settings_page.start()
        threading.Thread(target=self._run_routines_when_due, daemon=True, name="routines").start()
        threading.Thread(target=self._send_alarms_hourly, daemon=True, name="alarm-copies").start()
        logger.info(f"{self.name}'s brain is ready")

        # However listening ends - a stop signal, stop() or something unexpected -
        # everything is closed down here, once, on the main thread.
        try:
            self.serve()
        finally:
            self._clean_up()

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
        then ntfy and the settings page, then every satellite's session, so its device
        in Home Assistant and its music stop - and then the Piper process kept waiting
        for the next thing to say.
        """
        self.running = False
        self.clock.stop()
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
