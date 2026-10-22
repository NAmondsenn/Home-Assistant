"""
Session Module.

One satellite's Nova, on the brain - see docs/decisions/0006. A session answers each
wake word its satellite hears: it records what the user says from the audio the
satellite sends, hears it with the brain's speech-to-text, has the conversation work
out the answer (see conversation.py), and says it through the satellite's speaker. It
also says what goes off by itself there - the timers and alarms set from it, Home
Assistant's messages and the user's routines.

This was main.py's SmartAssistant, when the Pi was the whole of Nova. Now the Pi is a
satellite (see satellite.py), and each satellite connected to the brain has a session
of its own, with its own volumes, mute, music, room in Home Assistant and
conversation. What they all share - the clock, memory, routines, ntfy, the weather,
Piper and Nemotron - is the brain's.
"""

import logging
import os
import queue
import re
import threading
import time
from typing import Dict, Optional

from voice_assistant import paths, storage
from voice_assistant.actions import ActionExecutor
from voice_assistant.conversation import Conversation
from voice_assistant.ha_device import DeviceControls, NovaDevice
from voice_assistant.home_assistant import HomeAssistant
from voice_assistant.intents import LocalIntents
from voice_assistant.llm import LLMHandler
from voice_assistant.logging_setup import transcripts_logged
from voice_assistant.music import MusicHold, make_player
from voice_assistant.network import online
from voice_assistant.recording import RATE, record_until_silence
from voice_assistant.remote import RemoteSatellite
from voice_assistant.settings_page import change_volume, volume_levels
from voice_assistant.timeline import Timeline
from voice_assistant.vad import make_detector
from voice_assistant.volume import VolumeControl
from voice_assistant.wake_phrase import DEFAULT_WAKE_WORD, strip_wake_word

logger = logging.getLogger(__name__)

# How long a session waits for a wake word before checking its satellite is still
# there, in seconds - the satellite's wakes never say it has gone.
WAKE_CHECK = 0.5

# How often a reply checks for a wake word said over it, in seconds - often, since the
# user is waiting for it to stop.
TALKED_OVER_CHECK = 0.05

# How often Home Assistant is asked which device is this satellite's, and which room
# it's in, in seconds: every half a minute until it's found - it isn't until Home
# Assistant has taken in the device's MQTT announcement - then every five minutes,
# since the user can move it to another room whenever they like.
DEVICE_LOOKUP = 30
DEVICE_LOOKUP_FOUND = 300


def _kept_for(what: str, satellite: str) -> str:
    """
    Where one of a satellite's own settings is kept in the data folder, e.g.
    "volume-bedroom.json". Anything in its name which couldn't be in a file's name is
    replaced, so a name can't lead anywhere else.
    """
    return os.path.join(paths.DATA_DIR, f"{what}-{re.sub(r'[^A-Za-z0-9_.-]', '_', satellite)}.json")


class Session:
    """One satellite's Nova, for as long as the satellite stays connected."""

    def __init__(self, brain, satellite: RemoteSatellite):
        """
        Args:
            brain: What every session shares - its config, clock, memory, routines,
                ntfy, weather, tts (Piper), stt (a NemotronSpeechToText) and
                sounds_folder.
            satellite: The satellite, just connected.
        """
        self.brain = brain
        self.satellite = satellite
        self.config = config = brain.config

        assistant_config = config.section("assistant")
        self.assistant_name = assistant_config.get("name", "Assistant")
        # What wakes the satellite, which the recording sometimes catches the end of -
        # see strip_wake_word.
        self.wake_word = assistant_config.get("wake_word", DEFAULT_WAKE_WORD)

        # The assistant's own sound effects, which the satellite keeps and plays by name.
        sounds_config = config.section("sounds")
        self.chime_enabled = sounds_config.get("chime_enabled", True)
        self.reminder_sound_enabled = sounds_config.get("reminder_enabled", True)
        self.listening_sound_enabled = sounds_config.get("listening_enabled", True)

        # How long each stage of every interaction took, one line per interaction,
        # for scripts/latency_report.py to summarise.
        self.latency_file = os.path.join(paths.LOGS_DIR, "latency.jsonl")

        # Set while the assistant is idle, cleared while it's dealing with the user.
        # Timer announcements wait on this, so they don't talk over a conversation.
        self._idle = threading.Event()
        self._idle.set()
        # Held while something which went off is being announced. The clock rings
        # each timer on a thread of its own, so two going off together would
        # otherwise talk over each other.
        self._announcing = threading.Lock()
        # Held while the conversation works out a reply - to the user, or for a routine
        # running by itself, which mustn't do so at the same time. See run_routine.
        self._conversation_lock = threading.Lock()

        # What the assistant is doing - "idle", "listening", "thinking" or "speaking" -
        # for the settings page's star. Only ever read for show.
        self.activity = "idle"
        # The last volume beep the settings page played, stopped as the next starts,
        # so dragging a slider doesn't pile them up - see boop.
        self._last_boop = None
        self._boop_lock = threading.Lock()
        # Set once the session is closed, which ends run() - see close.
        self._closed = threading.Event()
        self._closing = threading.Lock()

        logger.info(f"Starting the {satellite.name} satellite's session...")

        # Everything played goes to the satellite's speaker - see remote.py.
        self.speaker = satellite.speaker
        # Decides which parts of a recording are speech - see vad.py. One for each
        # session, since it carries what it last heard from one frame to the next.
        self.speech_detector = make_detector(config)
        # Seconds from before the wake word was detected which the satellite sends
        # first, by the same setting - see _handle_interaction.
        self.pre_roll = config.section("audio").get("pre_roll", 0.3)

        # Whether the satellite's microphone is muted - from Home Assistant or the
        # settings page - kept so it stays muted however often either end restarts.
        self._muted_file = _kept_for("microphone", satellite.name)
        self._muted = self._load_muted()

        # The satellite's own volumes, saved so they survive a restart, and sent to it
        # whenever one changes, for it to ring by while it can't reach the brain.
        self.volume = VolumeControl(config=config, path=_kept_for("volume", satellite.name),
                                    on_change=self.send_state)

        # Navidrome or Spotify, whichever music.player says - or None, and the
        # assistant carries on without music. See music.py.
        self.music_player = make_player(config, self.speaker, self.volume)

        # Keeps the music paused while there's something to say - see MusicHold.
        self.music = MusicHold(self.music_player, online=online)

        # Home Assistant, for the lights and the calendar - None until it's set up. One
        # for each session, since it keeps the room its satellite is in.
        self.home = HomeAssistant.from_config(config, os.getenv("HA_TOKEN"))

        # Everything the assistant can do for the user - see actions.py. The lights and
        # the calendar are only there once Home Assistant is set up, and timers and
        # alarms ring on the satellite they were set from.
        self.actions = ActionExecutor(music=self.music_player, clock=brain.clock, volume=self.volume,
                                      weather=brain.weather, home=self.home, memory=brain.memory,
                                      ntfy=brain.ntfy, satellite=satellite.name)

        # The everyday commands handled without Claude - see intents.py.
        self.intents = LocalIntents(clock=brain.clock, ntfy=brain.ntfy)

        # The satellite as a device of its own in Home Assistant, over MQTT, or None
        # until mqtt.host is set - see ha_device.py. It's named after the satellite's
        # own machine, so the Pi keeps the device, and the room, it had. Which device it
        # is there is looked up while the session runs - see _look_up_device.
        self.device = NovaDevice.from_config(config, os.getenv("MQTT_PASSWORD"), self._device_controls(),
                                             hostname=satellite.hostname)
        self._stop_looking_up = threading.Event()

        # Claude, offered whichever of those tools can actually work here, and told
        # what's known about the user.
        self.llm = LLMHandler(config=config, tools=self.actions.tool_schemas(), memory=brain.memory)

        # How each thing the user says is answered - everything between hearing it and
        # saying the reply. See conversation.py.
        self.thresholds = config.section("thresholds")
        self.conversation = Conversation(brain.clock, self.intents, self.llm, self.actions, self.music,
                                         chime_enabled=self.chime_enabled,
                                         max_follow_ups=self.thresholds.get("max_follow_ups", 3),
                                         routines=brain.routines)

    # The satellite's mute and volumes

    @property
    def muted(self) -> bool:
        """Whether the satellite's microphone is muted, so it doesn't hear the wake word."""
        return self._muted

    def set_muted(self, muted: bool):
        """
        Mutes or unmutes the satellite's microphone, straight away - from Home
        Assistant or the settings page. Alarms and timers still ring, and Home
        Assistant's messages are still said.

        Args:
            muted: True to stop the satellite hearing the wake word.
        """
        self._muted = bool(muted)
        try:
            storage.save_json(self._muted_file, {"muted": self._muted})
        except OSError as e:
            # It's still muted or unmuted until the brain restarts, just not after.
            logger.error(f"Couldn't save whether the {self.satellite.name} satellite is muted: {e}")
        self.send_state()

    def _load_muted(self) -> bool:
        """Whether the satellite was left muted - not, if that can't be told."""
        try:
            saved = storage.load_json(self._muted_file, default={})
        except Exception as e:
            logger.error(f"Couldn't read whether the {self.satellite.name} satellite is muted, so it isn't: {e}")
            return False
        return isinstance(saved, dict) and saved.get("muted") is True

    def send_state(self) -> bool:
        """
        Sends the satellite its volumes and its mute, which the brain owns: whenever
        either changes, and as it connects. It mutes its wake word by them, and rings
        by them while it can't reach the brain.

        Returns:
            Whether it was sent.
        """
        volumes = {category: self.volume.get(category) for category in VolumeControl.CATEGORIES}
        return self.satellite.set_state(volumes, self._muted)

    # Running

    def run(self):
        """
        Answers each wake word the satellite hears, one at a time, until it goes or the
        session is closed - and then closes the session.
        """
        # Checked and started together, with what close() checks it by: a session the
        # brain closed before it ran - replaced by its satellite connecting again, say
        # - never starts its device, which would otherwise stay connected for good, with
        # the same MQTT client id as the new session's. One closed after is stopped.
        with self._closing:
            if self._closed.is_set():
                return
            if self.device:
                self.device.start()
                if self.home:
                    threading.Thread(target=self._look_up_device, daemon=True,
                                     name=f"ha-device-lookup-{self.satellite.name}").start()
        try:
            while self.satellite.connected and not self._closed.is_set():
                try:
                    wake = self.satellite.wakes.get(timeout=WAKE_CHECK)
                except queue.Empty:
                    continue
                try:
                    self._converse(wake)
                # Logged, and the next wake word answered as usual - after a moment,
                # in case it would only go wrong again straight away.
                except Exception as e:
                    logger.error(f"Error: {e}", exc_info=True)
                    time.sleep(1)
        finally:
            self.close()

    def close(self):
        """
        Lets the session go, once its satellite has gone or the brain is stopping: Home
        Assistant shows its device as unavailable, its music stops, and so does anything
        still playing on the satellite. run() ends at its next check. Safe to call more
        than once, from any thread.
        """
        with self._closing:
            if self._closed.is_set():
                return
            self._closed.set()
        self._stop_looking_up.set()
        if self.device:
            self.device.stop()
        if self.music_player:
            self.music_player.close()
        self.speaker.close()

    # Answering the user

    def _converse(self, wake: Dict):
        """
        Everything from the wake word until the assistant has nothing left to say:
        one request, or several, when the user says the wake word over a reply -
        each of which starts straight away, as if the wake word had just been heard.

        Args:
            wake: The wake word, as the satellite's wakes have it.
        """
        # Marks the assistant as busy, so a timer going off waits its turn rather than interrupting.
        self._idle.clear()
        self.activity = "listening"
        # Pauses the music for the whole interaction (speaking and playback) to avoid audio conflicts.
        # The pause goes through Spotify's servers, so it runs alongside the
        # recording rather than holding it up - see MusicHold. Held across every
        # request, so it doesn't come back for a moment when a reply is talked over.
        self.music.hold()
        try:
            # Not talked over, even if the satellite says it was - over a chime, say. Only
            # a wake word said over a reply is that - see _say.
            heard_over = self._answer(wake)
            while heard_over is not None:
                heard_over = self._answer(heard_over, talked_over=heard_over["talked_over"])
        finally:
            # The satellite silences anything going off when it hears the wake word, and
            # leaves it to the brain to let it back.
            self.speaker.allow("announcement")
            self.activity = "idle"
            self._idle.set()
            self.music.release()

    def _answer(self, wake: Dict, talked_over: bool = False) -> Optional[Dict]:
        """
        Answers one request, from the moment the wake word was heard.

        Args:
            wake: The wake word, with the feed of what the user said after it.
            talked_over: True when the wake word was said over the last reply - see
                Conversation.start.

        Returns:
            The wake word heard over this reply too, so the next request has already
            begun - or None.
        """
        # Times the interaction from this moment on - see Timeline.
        timeline = Timeline(follow_up=False)
        timeline.mark("wake")
        print("Listening...")

        # What was audibly going off at the moment the wake word fired,
        # read before the interaction quietens anything, since what
        # matters is what the user could hear when they started talking.
        ringing = self.brain.clock.ringing_state()

        # Anything going off stops the moment the wake word is heard, rather than
        # playing on over the user - and into the microphone - until its sound
        # ends. It carries on afterwards unless the conversation dealt with it.
        self.speaker.silence("announcement")
        try:
            with self._conversation_lock:
                self.conversation.start(ringing, talked_over=talked_over)
            return self._handle_interaction(wake["feed"], timeline=timeline)
        except Exception as e:
            # Said out loud, rather than only logged, so it isn't taken for the
            # assistant not having heard. Only for something which went wrong
            # during a conversation.
            logger.error(f"Interaction failed: {e}", exc_info=True)
            self._apologise()
            return None
        finally:
            timeline.save(self.latency_file)

    def _handle_interaction(self, feed, follow_up: bool = False, timeline: Optional[Timeline] = None) -> Optional[Dict]:
        """
        Handles one full interaction after the wake word: records the query from what
        the satellite sends, transcribes it, has the conversation work out the reply
        (see conversation.py), and plays it.

        Args:
            feed: The AudioFeed of what the user says, as the satellite sends it.
            follow_up: True when the assistant has just asked the user a question
                       and is listening for the answer, which allows a little
                       longer to start speaking than a fresh request does.
            timeline: The Timeline to mark each stage on. The caller saves it.

        Returns:
            The wake word heard over the reply, which the next request starts from - see
            _say. None otherwise.
        """
        # A throwaway one when none is given, so the marks below never need checking for.
        timeline = timeline or Timeline()

        speech_timeout = self.thresholds.get("speech_timeout", 3.0)
        if follow_up:
            # Answering a question takes a moment's thought, so there's more time to
            # start talking than when the user has just said the wake word.
            speech_timeout = self.thresholds.get("follow_up_timeout", speech_timeout * 2)

        self.activity = "listening"

        # A short sound to say it's listening, since on a headless Pi there's otherwise no
        # way to tell the wake word was heard, or when to start talking. The satellite
        # is already sending what its microphone hears, so nothing said while the sound
        # plays goes missing, and it's far too short to count as the user starting to speak.
        if self.listening_sound_enabled:
            self._play_sound("listening.wav")

        # Speech-to-text is given the recording as it's made, so it can work out the
        # words while the user is still saying them - see speech_to_text.py.
        listening = self.brain.stt.listen()

        # Records audio until silence / a timeout occurs. For a fresh request, the
        # satellite sent the moment from before the wake word was detected first, since
        # "Hey Nova what time is it" said in one breath has often started on "what" by
        # then. That's kept, but not listened to: it often holds the end of "Nova" too,
        # which would otherwise start the recording, and the pause for the listening
        # sound then end it. An answer comes with nothing from before.
        pre_roll = 0.0 if follow_up else self.pre_roll
        try:
            audio_data = record_until_silence(
                feed, self.speech_detector, listen_from=round(pre_roll * RATE), keep_before=pre_roll,
                silence_duration=self.thresholds.get("silence_duration", 0.6),
                speech_timeout=speech_timeout,
                timeout=self.thresholds.get("recording_timeout", 10.0),
                timeline=timeline, heard=listening.add)
        except BaseException:
            # Otherwise it would wait on for the rest of a recording which isn't coming.
            listening.cancel()
            raise
        finally:
            # The user has stopped, or isn't going to start - so the satellite stops sending.
            self.satellite.stop_listening()

        if not self.satellite.connected:
            # Gone part way through, so there's nobody to answer - and half a request
            # isn't acted on.
            listening.cancel()
            timeline.note(outcome="satellite_gone")
            return None

        if audio_data is None or len(audio_data) == 0:
            listening.cancel()
            timeline.note(outcome="no_speech")
            self._not_heard()
            return None

        # The words, once speech-to-text has caught up with the end of the recording,
        # carrying on listening if no text comes back. The audio is already at the
        # 16kHz speech-to-text expects, since the satellite sends it at that.
        self.activity = "thinking"
        transcription = listening.finish(audio_data)
        timeline.mark("stt_done")
        timeline.note(audio_seconds=round(len(audio_data) / RATE, 2))

        # Retrieves the transcribed text, without the end of the wake word which the
        # recording sometimes catches - see strip_wake_word.
        text = strip_wake_word((transcription or {}).get("text", ""), self.wake_word)
        if not text.strip():
            timeline.note(outcome="nothing_transcribed")
            self._not_heard()
            return None

        timeline.note(text_chars=len(text))
        self._show(f"You: {text}")

        # Everything between hearing the request and answering it - see conversation.py.
        with self._conversation_lock:
            reply = self.conversation.handle(text, follow_up=follow_up, timeline=timeline)

        for sound, category in reply.sounds:
            self._play_sound(sound, volume=self.volume.get(category))
        if reply.shown:
            self._show(f"{self.assistant_name}: {reply.shown}")
        if reply.say:
            heard_over = self._say(reply.say, timeline=timeline, listen=True)
            if heard_over is not None:
                return heard_over

        # The assistant asked something, so it listens straight away for the answer.
        if reply.listen_again:
            logger.info("Question asked, listening for a reply")
            print("Listening...")
            # The answer is timed on its own, from when listening starts again.
            follow_up_timeline = Timeline(follow_up=True)
            try:
                # Asked for before the listening sound, so an answer started over it
                # isn't lost - and recorded from this feed, which is the one the
                # satellite sends it to.
                return self._handle_interaction(self.satellite.listen(), follow_up=True,
                                                timeline=follow_up_timeline)
            finally:
                follow_up_timeline.save(self.latency_file)
        return None

    def _say(self, message: str, timeline: Optional[Timeline] = None, listen: bool = False) -> Optional[Dict]:
        """
        Speaks a reply to the user through the satellite, starting as soon as its
        first sentence is ready.

        Args:
            message: What to say.
            timeline: Optional Timeline to mark when the speech started and when it finished.
            listen: Stop the moment the wake word is heard over it. A reply couldn't be
                talked over at all, so the user had to sit through every word of it -
                three and a half minutes, once, for a fortnight of their calendar.

        Returns:
            With listen, the wake word heard over it - or before it started - which the
            next request starts from. None if there wasn't one.
        """
        if listen:
            # Woken again while the reply was being worked out - or over a chime - so the
            # satellite is already sending what the user says next. That's answered
            # instead, rather than this being said over them, and it isn't talking over
            # the reply, which never started.
            try:
                waiting = self.satellite.wakes.get_nowait()
                logger.info("Woken again before the reply was said, so it isn't")
                return dict(waiting, talked_over=False)
            except queue.Empty:
                pass

        self._show(f"{self.assistant_name}: {message}")
        self.activity = "speaking"
        started = None
        if timeline:
            def started():
                timeline.mark("tts_done")
                timeline.note(reply_chars=len(message))
        tts = self.brain.tts
        playback = self.speaker.play_stream(tts.stream(message, on_start=started), tts.sample_rate,
                                            volume=self.volume.get("general"))
        if not listen:
            playback.wait()
            heard_over = None
        else:
            heard_over = self._heard_over(playback)
        # Only a wake word heard while a reply was playing on the satellite stops it as
        # talking over it - one it heard just before the reply reached it still stops
        # it, so it isn't said over the user, but starts the next request afresh.
        talked_over = heard_over is not None and heard_over["talked_over"]
        if heard_over is not None:
            logger.info("Talked over, so stopped part way" if talked_over
                        else "Woken again as the reply started, so it's stopped")
            playback.stop()
        if timeline:
            timeline.mark("playback_done")
            if talked_over:
                timeline.note(talked_over=True)
        return heard_over

    def _heard_over(self, playback) -> Optional[Dict]:
        """
        Waits for a reply to finish, or for the wake word to be heard over it.

        Args:
            playback: The reply, playing on the satellite.

        Returns:
            The wake word, as the satellite's wakes have it - or None once the reply
            has finished, as it does when the satellite goes.
        """
        while not playback.done:
            try:
                return self.satellite.wakes.get(timeout=TALKED_OVER_CHECK)
            except queue.Empty:
                pass
        return None

    def _show(self, line):
        """
        Prints a line of the conversation. The console is the system journal when
        running as a service, so this is left out along with the logged transcripts
        when logging.log_transcripts is off.
        """
        if transcripts_logged():
            print(line)

    def _play_sound(self, name, volume=None):
        """
        Plays one of the assistant's sound effects on the satellite, waiting for it to finish.

        Args:
            name: Filename within the sounds folder, e.g. "success_chime.wav".
            volume: How loud, defaulting to the general volume.
        """
        if volume is None:
            volume = self.volume.get("general")
        self.speaker.play_file(str(self.brain.sounds_folder / name), volume=volume).wait()

    def _not_heard(self):
        """
        Plays the listening sound backwards - a short falling blip - when nothing was
        heard or understood. Giving up used to be silent, which was easy to take for
        the assistant still listening, or still thinking.
        """
        if self.listening_sound_enabled:
            self._play_sound("not_heard.wav")

    def _apologise(self):
        """Says something went wrong, without letting that fail in turn."""
        try:
            self._say("Sorry, something went wrong there.")
        except Exception as e:
            logger.error(f"Couldn't even say so: {e}")

    # What goes off by itself

    def announce(self, message, sound="announcement_sound.wav", boost=False, still_wanted=None,
                 start=0.0, length=None, level=1.0):
        """
        Speaks a message which the user has scheduled, e.g. a timer, on the satellite -
        what the brain's clock calls for the timers and alarms set from it, and what
        its alerts and Home Assistant's messages are said with.

        Played as an "announcement", which the wake word silences straight away -
        see _answer().

        Args:
            message: What to say. None plays just the sound, with no speech - an
                     alarm uses this for every loop after its first, so nothing
                     waits on speech synthesis while it's trying to be loud.
            sound: The sound to play first, from the sounds folder.
            boost: True to play at full volume regardless of the configured alarm
                   level. An alarm always sets this; a plain timer only once it's
                   been ringing unanswered a while - see Clock._sound_off.
            still_wanted: Called before anything plays, so a timer or alarm the
                          user dismissed while this was waiting stays quiet.
            start: Seconds into the sound to start from - an alarm's bursts carry on
                   through its sound. See Clock._ring_alarm.
            length: Seconds of the sound to play, or None for all of it.
            level: How loud the sound plays, as a share of the volume - an alarm
                   builds up from quiet. The message itself is always at full volume.
        """
        # The music is held from now, before waiting for any conversation to finish,
        # so it doesn't come back in the gap between the two.
        self.music.hold()
        try:
            with self._announcing:
                self._announce_in_turn(message, sound, boost, still_wanted, start, length, level)
        finally:
            # Let back a few seconds later rather than straight away, since a timer's
            # next round comes three seconds after this one.
            self.music.release(after=5)

    def _announce_in_turn(self, message, sound, boost, still_wanted, start, length, level):
        """The body of announce, run once any other announcement has finished."""
        # Waits for any conversation to finish before making any announcements.
        if not self._wait_for_turn(still_wanted):
            return

        # Anything which goes off by itself uses the alarm volume, so it can stay
        # loud even when the assistant's voice is turned down - and once boosted,
        # it jumps straight to full regardless of that level.
        volume = 1.0 if boost else self.volume.get("alarm")

        try:
            # An alarm's later bursts aren't printed, since there's one every few seconds.
            if message or not start:
                self._show(f"\n{self.assistant_name}: {message}" if message
                           else f"\n{self.assistant_name}: [{sound}]")

            # A sound first, so the user knows the assistant is about to speak.
            if sound and self.reminder_sound_enabled:
                self._play_announcement(self.speaker.play_file(
                    str(self.brain.sounds_folder / sound), volume=volume * level, kind="announcement",
                    start=start, length=length), still_wanted)

            if not message:
                return

            # The sound lasts a few seconds, long enough for the user to start talking
            # to the assistant - or to dismiss this - before it's finished, so the
            # message waits its turn again rather than talking over them.
            if not self._wait_for_turn(still_wanted):
                return

            # Shown as speaking only between conversations - in one, the conversation
            # itself says what the assistant's doing.
            if self._idle.is_set():
                self.activity = "speaking"
            tts = self.brain.tts
            self._play_announcement(self.speaker.play_stream(
                tts.stream(message), tts.sample_rate, volume=volume, kind="announcement"),
                still_wanted)
            if self._idle.is_set():
                self.activity = "idle"
        except Exception as e:
            logger.error(f"Announcement failed: {e}")

    def _wait_for_turn(self, still_wanted=None):
        """
        Waits for any conversation to finish, then checks the announcement waiting
        on it is still wanted.

        Args:
            still_wanted: Optional check from the Clock - False once the timer or
                          alarm has been dismissed.

        Returns:
            True if the announcement should go ahead.
        """
        if not self._idle.wait(timeout=60):
            logger.warning("Announcing over an unusually long interaction")

        # Talking to the assistant is often how something ringing gets dismissed, so
        # by the time the conversation it was waiting on has finished, it may well
        # have been dealt with - and playing it again then would be wrong.
        if still_wanted is not None and not still_wanted():
            logger.info("Dismissed while waiting its turn, so it isn't announced")
            return False
        return True

    def _play_announcement(self, playback, still_wanted=None):
        """
        Waits for one sound for something which went off by itself to finish - or
        stops it the moment it's dismissed, rather than once the whole sound has
        played. An alarm's sound is 34 seconds long.

        Args:
            playback: The sound, already started.
            still_wanted: Checked while it plays - False once it's been dismissed.
        """
        while not playback.wait(timeout=0.1):
            if still_wanted is not None and not still_wanted():
                logger.info("Dismissed part way through, so it's stopped")
                playback.stop()
                return

    def alert(self, alert):
        """
        Says an alert from ntfy out loud as it arrives - see ntfy.py. Like a timer, it
        waits for any conversation to finish, and pauses the music.

        Args:
            alert: The ntfy Alert.
        """
        # "Send that to my phone" straight afterwards sends the whole of it.
        self.actions.last_answer = (alert.title or f"From {alert.topic}", alert.details)
        self.announce(f"{alert.topic.capitalize()}: {alert.headline}")

    def run_routine(self, routine):
        """
        Runs one of the user's routines by itself - at its time, or from the settings
        page - once any conversation has finished, as an announcement waits. Its steps
        go through the conversation as if the user had said them (see routines.py),
        and what they say is said as an announcement is, so the wake word stops it.

        Args:
            routine: The routine, as Routines has it.
        """
        # The music is held from now, so a step which plays some starts it once
        # everything's been said - as when the user asks for it.
        self.music.hold()
        try:
            with self._announcing:
                if not self._wait_for_turn():
                    return
                self.activity = "thinking"
                with self._conversation_lock:
                    self.conversation.start()
                    reply = self.conversation.run_routine(routine)
                for sound, category in reply.sounds:
                    self._play_sound(sound, volume=self.volume.get(category))
                if reply.shown:
                    self._show(f"{self.assistant_name}: {reply.shown}")
                if reply.say:
                    self._show(f"{self.assistant_name}: {reply.say}")
                    self.activity = "speaking"
                    tts = self.brain.tts
                    self._play_announcement(self.speaker.play_stream(
                        tts.stream(reply.say), tts.sample_rate, volume=self.volume.get("general"),
                        kind="announcement"))
        except Exception as e:
            logger.error(f"A routine failed: {e}", exc_info=True)
        finally:
            if self._idle.is_set():
                self.activity = "idle"
            self.music.release()

    # For the settings page and Home Assistant

    def boop(self, level):
        """
        Plays the volume beep at a level, for the settings page's sliders - stopping
        the last one if it's still going, so dragging a slider doesn't pile them up.

        Args:
            level: How loud, from 0 to 1.
        """
        with self._boop_lock:
            if self._last_boop is not None:
                self._last_boop.stop()
            self._last_boop = self.speaker.play_file(str(self.brain.sounds_folder / "volume_beep.wav"), volume=level)

    def _device_controls(self) -> DeviceControls:
        """What Home Assistant sees and does of the satellite - see ha_device.py."""
        return DeviceControls(
            status=lambda: self.activity,
            volumes=lambda: volume_levels(self.volume, self.music_player),
            set_volume=lambda category, percent: change_volume(self.volume, self.music_player, category, percent),
            clock=lambda: self.brain.clock.overview(),
            # Whether the brain has its speech-to-text to hear the satellite with.
            backend=lambda: self.brain.stt.loaded,
            muted=lambda: self.muted,
            set_muted=self.set_muted,
            stop=self._stop_from_home_assistant,
            restart=self.satellite.restart,
            speak=self._speak_from_home_assistant)

    def _stop_from_home_assistant(self):
        """Home Assistant's Stop: silences anything ringing, and stops Nova talking."""
        if self.brain.clock.is_ringing():
            self.brain.clock.dismiss()
        self.speaker.stop(["reply", "announcement"])

    def _speak_from_home_assistant(self, message):
        """
        Says a message from Home Assistant - an automation's "the washing's done" -
        once any conversation has finished, as a timer is, muted or not. Handed to a
        thread of its own, since Home Assistant's commands are carried out one at a
        time, and a Stop pressed meanwhile mustn't wait until this has been said.

        Args:
            message: What to say.
        """
        threading.Thread(target=self.announce, args=(message,), daemon=True, name="ha-speak").start()

    def _look_up_device(self):
        """
        Keeps up with which device in Home Assistant is this satellite's, and the room
        it's in - which Assist goes by for "turn on the lights" - until the session
        closes. HomeAssistant.find_device logs anything that goes wrong.
        """
        while True:
            try:
                found = self.home.find_device(self.device.identifier)
            except Exception as e:
                logger.error(f"Couldn't look for the {self.satellite.name} satellite's device in Home Assistant: {e}")
                found = False
            if self._stop_looking_up.wait(DEVICE_LOOKUP_FOUND if found else DEVICE_LOOKUP):
                return
