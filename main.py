import os, time, logging, signal, threading
from pathlib import Path

PROJECT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))

# voice_assistant is a proper package sitting next to this file, so its modules
# are imported through the package rather than via sys.path manipulation.
from voice_assistant import paths
from voice_assistant.config import Config
from voice_assistant.speaker import Speaker
from voice_assistant.microphone import RATE, Microphone
from voice_assistant.echo import make_echo_canceller
from voice_assistant.vad import make_detector
from voice_assistant.speech_to_text import make_speech_to_text
from voice_assistant.llm import LLMHandler
from voice_assistant.intents import LocalIntents
from voice_assistant.weather import Weather
from voice_assistant.home_assistant import HomeAssistant
from voice_assistant.memory import Memory
from voice_assistant.network import online
from voice_assistant.ntfy import Ntfy
from voice_assistant.settings_page import SettingsPage
from voice_assistant import storage
from voice_assistant.text_to_speech import TextToSpeech
from voice_assistant.wake_word import WakeWordDetector, strip_wake_word
from voice_assistant.music import MusicHold, make_player
from voice_assistant.clock import Clock
from voice_assistant.volume import VolumeControl
from voice_assistant.actions import ActionExecutor
from voice_assistant.conversation import Conversation
from voice_assistant.timeline import Timeline
from voice_assistant import watchdog
from voice_assistant.logging_setup import log_transcripts, setup_logging, transcripts_logged

# Logger for the SmartAssistant class. Where its lines go is set up when the
# assistant is run (see the bottom of this file), not on import.
logger = logging.getLogger(__name__)

# Left while the assistant runs, and taken away when it stops properly - so finding
# it when starting means the last run didn't: it crashed, the watchdog restarted it,
# or the power went. See _note_start.
RUNNING_MARK = os.path.join(paths.DATA_DIR, "running")

# The least time between telling the user's phone the assistant had to restart, so a
# crash which keeps happening doesn't flood it.
RESTART_NOTICE_GAP = 3600

class SmartAssistant:
    def __init__(self):
        # Keys and passwords from ~/.config/nova/.env, then the settings - the
        # repository's config.yaml with this install's own on top. See paths.py.
        paths.load_secrets()
        self.config = Config()
        # Whether what's said, and the replies, are kept in the logs - see logging_setup.py.
        log_transcripts(self.config.section("logging").get("log_transcripts", True))

        # Retrieves the assistant's configuration section from config.yaml
        assistant_config = self.config.section("assistant")
        self.name = assistant_config.get("name", "Assistant")
        self.wake_word = assistant_config.get("wake_word", "Hey Assistant")

        # Retrieves the audio configuration section from config.yaml.
        audio_config = self.config.section("audio")
        self.mic_rate = audio_config.get("sample_rate", 48000)

        # The assistant's own sound effects, kept with the rest of the project.
        sounds_config = self.config.section("sounds")
        self.sounds_folder = Path(paths.SOUNDS_DIR)
        self.chime_enabled = sounds_config.get("chime_enabled", True)
        self.reminder_sound_enabled = sounds_config.get("reminder_enabled", True)
        self.listening_sound_enabled = sounds_config.get("listening_enabled", True)

        # How long each stage of every interaction took, one line per interaction,
        # for scripts/latency_report.py to summarise.
        self.latency_file = os.path.join(paths.LOGS_DIR, "latency.jsonl")

        # Controls the main loop of the assistant. When set to False, the assistant will stop running.
        self.running = False
        # Set once a stop signal has arrived - see _shutdown.
        self._stopping = False

        # Set while the assistant is idle, cleared while it's dealing with the user.
        # Timer announcements wait on this, so they don't talk over a conversation.
        self._idle = threading.Event()
        self._idle.set()
        # Held while something which went off is being announced. The clock rings
        # each timer on a thread of its own, so two going off together would
        # otherwise talk over each other.
        self._announcing = threading.Lock()

        # What the assistant is doing - "idle", "listening", "thinking" or "speaking" -
        # for the settings page's star. Only ever read for show.
        self.activity = "idle"
        # The last volume beep the settings page played, stopped as the next starts,
        # so dragging a slider doesn't pile them up - see _boop.
        self._last_boop = None
        self._boop_lock = threading.Lock()

        logger.info("Loading modules...")
        
        # Takes what the assistant plays back out of what the microphone hears, so it
        # can be talked over - see echo.py. None if it's off, or can't be.
        self.echo = make_echo_canceller(self.config, self.mic_rate)

        # The one owner of the speaker, which every sound goes through - see speaker.py.
        self.speaker = Speaker(sample_rate=self.mic_rate, echo_reference=self.echo.played if self.echo else None)

        # One microphone stream for as long as the assistant runs, which the wake word
        # detector and the recorder share - see microphone.py.
        self.microphone = Microphone(sample_rate=self.mic_rate, echo=self.echo)
        # Seconds of audio from just before the wake word was detected which are kept,
        # so a request said in the same breath as it isn't cut short.
        self.pre_roll = audio_config.get("pre_roll", 0.3)
        # Decides which parts of a recording are speech - see vad.py.
        self.speech_detector = make_detector(self.config)

        # Speech-to-text - Moonshine or Whisper, whichever speech_to_text.engine says.
        self.stt = make_speech_to_text(self.config)
        
        # Sets up text-to-speech with settings from config.
        self.tts = TextToSpeech(self.config)
        
        # Sets up volume control. Levels changed by voice are saved, so they survive a restart.
        self.volume = VolumeControl(config=self.config)

        # Navidrome or Spotify, whichever music.player says - or None, and the
        # assistant carries on without music. See music.py.
        logger.info("Loading the music player...")
        self.music_player = make_player(self.config, self.speaker, self.volume)

        # Keeps the music paused while there's something to say - see MusicHold.
        self.music = MusicHold(self.music_player, online=online)

        # The user's own ntfy server, both ways: alerts said out loud as they arrive,
        # and things sent to their phone. None if it isn't set up - see ntfy.py.
        self.ntfy = Ntfy.from_config(self.config, os.getenv("NTFY_TOKEN"), on_alert=self._alert)

        # Sets up timers, which run on their own thread and call back here to speak when one goes off.
        self.clock = Clock(on_timer_finished=self._announce, on_unanswered=self._unanswered, config=self.config)

        # The everyday commands handled without Claude - see intents.py.
        self.intents = LocalIntents(clock=self.clock, ntfy=self.ntfy)

        # What the assistant knows about the user, learnt as they talk - see memory.py.
        self.memory = Memory()

        # Everything the assistant can do for the user - see actions.py. The lights and
        # the calendar are only there once Home Assistant is set up.
        self.actions = ActionExecutor(music=self.music_player, clock=self.clock, volume=self.volume,
                                      weather=Weather(config=self.config),
                                      home=HomeAssistant.from_config(self.config, os.getenv("HA_TOKEN")),
                                      memory=self.memory, ntfy=self.ntfy)

        # The page for looking after Nova from a phone or a laptop, or None if it's
        # switched off - see settings_page.py.
        self.settings_page = SettingsPage.from_config(self.config, volume=self.volume, clock=self.clock,
                                                      memory=self.memory, ntfy=self.ntfy, stt=self.stt,
                                                      music=self.music_player, boop=self._boop,
                                                      activity=lambda: self.activity,
                                                      wake_word=lambda: self.wake_word)

        # Claude, offered whichever of those tools can actually work here, and told
        # what's known about the user.
        self.llm = LLMHandler(config=self.config, tools=self.actions.tool_schemas(), memory=self.memory)

        # How each thing the user says is answered - everything between hearing it and
        # saying the reply. See conversation.py.
        self.thresholds = self.config.section("thresholds")
        self.conversation = Conversation(self.clock, self.intents, self.llm, self.actions, self.music,
                                         chime_enabled=self.chime_enabled,
                                         max_follow_ups=self.thresholds.get("max_follow_ups", 3))
        
        # Sets up the wake word detector with sensitivity settings from config.
        self.wake = WakeWordDetector(config=self.config, microphone=self.microphone)
        # What's actually listened for, which is a stand-in until the configured wake
        # word's model is copied in - see WakeWordDetector.
        self.wake_word = self.wake.wake_word_phrase

        # Register _shutdown to run on Ctrl + C (SIGINT) or a termination request (SIGTERM).
        # This ensures resources get cleaned up properly instead of the app dying abruptly .
        signal.signal(signal.SIGINT, self._shutdown) # Signal interrupt
        signal.signal(signal.SIGTERM, self._shutdown) # Signal termination
        
        logger.info(f"{self.name} ready!")

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
        Plays one of the assistant's sound effects, waiting for it to finish.

        Args:
            name: Filename within the sounds folder, e.g. "success_chime.wav".
            volume: How loud, defaulting to the general volume.

        Returns:
            True if it played.
        """
        if volume is None:
            volume = self.volume.get("general")
        playback = self.speaker.play_file(str(self.sounds_folder / name), volume=volume)
        if playback is None:
            return False
        playback.wait()
        return True

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

    def _say(self, message, timeline=None, listen=False):
        """
        Speaks a reply to the user, starting as soon as its first sentence is ready.

        Args:
            message: What to say.
            timeline: Optional Timeline to mark when the speech started and when it finished.
            listen: Listen for the wake word while it's said, and stop the moment it's
                heard. A reply couldn't be talked over at all, so the user had to sit
                through every word of it - three and a half minutes, once, for a
                fortnight of their calendar.

        Returns:
            True if the wake word was heard over it, and it was stopped part way.
        """
        self._show(f"{self.name}: {message}")
        self.activity = "speaking"
        started = None
        if timeline:
            def started():
                timeline.mark("tts_done")
                timeline.note(reply_chars=len(message))
        playback = self.speaker.play_stream(self.tts.stream(message, on_start=started), self.tts.sample_rate,
                                            volume=self.volume.get("general"))
        if not listen:
            playback.wait()
            talked_over = False
        else:
            # Waited for on a thread of its own, so the wake word can be listened for
            # here meanwhile - and so a reply which never finishes, because the
            # speaker has died part way, is still given up on as it always was.
            finished = threading.Thread(target=playback.wait, daemon=True, name="reply-wait")
            finished.start()
            talked_over = self.wake.listen_once(until=lambda: not finished.is_alive()) is not None
            if talked_over:
                logger.info("Talked over, so stopped part way")
                playback.stop()
        if timeline:
            timeline.mark("playback_done")
            if talked_over:
                timeline.note(talked_over=True)
        return talked_over

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
            playback: The sound, already started, or None if it couldn't be.
            still_wanted: Checked while it plays - False once it's been dismissed.
        """
        if playback is None:
            return
        while not playback.wait(timeout=0.1):
            if still_wanted is not None and not still_wanted():
                logger.info("Dismissed part way through, so it's stopped")
                playback.stop()
                return

    def _announce(self, message, sound="announcement_sound.wav", boost=False, still_wanted=None,
                  start=0.0, length=None, level=1.0):
        """
        Speaks a message which the user has scheduled, e.g. a timer.

        Played as an "announcement", which the wake word silences straight away -
        see _answer().

        Args:
            message: What to say. None plays just the sound, with no speech - an
                     alarm uses this for every loop after its first, so nothing
                     waits on speech synthesis while it's trying to be loud.
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
        """The body of _announce, run once any other announcement has finished."""
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
                self._show(f"\n{self.name}: {message}" if message else f"\n{self.name}: [{sound}]")

            # A sound first, so the user knows the assistant is about to speak.
            if sound and self.reminder_sound_enabled:
                self._play_announcement(self.speaker.play_file(
                    str(self.sounds_folder / sound), volume=volume * level, kind="announcement",
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
            self._play_announcement(self.speaker.play_stream(
                self.tts.stream(message), self.tts.sample_rate, volume=volume, kind="announcement"),
                still_wanted)
            if self._idle.is_set():
                self.activity = "idle"
        except Exception as e:
            logger.error(f"Announcement failed: {e}")

    def _alert(self, alert):
        """
        Says an alert from ntfy out loud as it arrives - see ntfy.py. Like a timer, it
        waits for any conversation to finish, and pauses the music.

        Args:
            alert: The ntfy Alert.
        """
        # "Send that to my phone" straight afterwards sends the whole of it.
        self.actions.last_answer = (alert.title or f"From {alert.topic}", alert.details)
        self._announce(f"{alert.topic.capitalize()}: {alert.headline}")

    def _unanswered(self, timer, kind):
        """
        Sends a reminder nobody answered to the user's phone, once it's stopped ringing.
        Only a reminder: a plain timer or an alarm going unanswered has nothing more
        to say there.

        Args:
            timer: What stopped ringing.
            kind: "clock" for a timer or reminder, "alarm" for an alarm.
        """
        if self.ntfy and self.ntfy.reminders and kind == "clock" and timer.get("label"):
            label = timer["label"]
            self.ntfy.send_soon(self.ntfy.reminders, label[:1].upper() + label[1:], title="Reminder nobody answered")

    def _boop(self, level):
        """
        Plays the volume beep at a level, for the settings page's sliders - stopping
        the last one if it's still going, so dragging a slider doesn't pile them up.

        Args:
            level: How loud, from 0 to 1.
        """
        with self._boop_lock:
            if self._last_boop is not None:
                self._last_boop.stop()
            self._last_boop = self.speaker.play_file(str(self.sounds_folder / "volume_beep.wav"), volume=level)

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
            logger.warning(f"Couldn't note that the assistant is running: {e}")

    def _shutdown(self, *args):
        """
        Called automatically when the program receives Ctrl+C or a termination request.

        Only stops the main loop, by raising SystemExit in whatever it was doing. The
        cleanup happens in run() once that has unwound, rather than in here, where
        tearing the audio down part way through something could close a stream out
        from under a recording still using it. A second signal while it's shutting
        down is ignored, so it can't cut the cleanup short.
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
        and the music, so it doesn't play on into the next song - then the speaker
        and the microphone, so neither device is left locked, and the Piper process
        kept waiting for the next reply.
        """
        self.clock.stop()
        if self.ntfy:
            self.ntfy.stop()
        if self.settings_page:
            self.settings_page.stop()
        if self.music_player:
            self.music_player.close()
        self.speaker.close()
        self.microphone.close()
        self.tts.close()
        # Stopped properly, so the next start isn't taken for a restart after a problem.
        try:
            os.remove(RUNNING_MARK)
        except OSError:
            pass
        logger.info("Shut down cleanly")

    def run(self):
        """
        Main loop of the voice assistant. This listens for the wake word, records audio until silence is detected,
        transcribes the audio to text, processes the query with the LLM, and executes any detected actions.
        """
        # Sets self.running to True to start the main loop and prints a confirmation message
        self.running = True
        print(f"\n{self.name} is ready. Say {self.wake_word} to activate.\n")
        # Tells systemd it has started, and from here on that it's still working.
        watchdog.start()
        self._note_start()
        # Alerts are only said from now on, once there's everything to say them with.
        if self.ntfy:
            self.ntfy.start()
        if self.settings_page:
            self.settings_page.start()

        # However the loop ends - a stop signal (see _shutdown) or something
        # unexpected - everything is closed down here, once, on the main thread.
        try:
            self._listen_forever()
        finally:
            self._clean_up()

    def _listen_forever(self):
        """Listens for the wake word and handles each interaction, until stopped."""
        # Main system loop that runs until self.running is set to False.
        # This loop uses try/except to catch any unexpected errors and log them rather than crashing the assistant.
        while self.running:
            try:
                self.wake.listen_once()
                self._converse()

            # Catches any unexpected errors during the main loop, logs the error
            # and pauses briefly before continuing to listen for the wake word again.
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                time.sleep(1)

    def _converse(self):
        """
        Everything from the wake word until the assistant has nothing left to say:
        one request, or several, when the user says the wake word over a reply -
        each of which starts straight away, as if the wake word had just been heard.
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
            talked_over = self._answer()
            while talked_over:
                talked_over = self._answer(talked_over=True)
        finally:
            self.speaker.allow("announcement")
            self.activity = "idle"
            self._idle.set()
            self.music.release()

    def _answer(self, talked_over=False):
        """
        Answers one request, from the moment the wake word was heard.

        Args:
            talked_over: True when the wake word was said over the last reply - see
                Conversation.start.

        Returns:
            True if the wake word was said over this reply too, so the next request
            has already begun.
        """
        # Times the interaction from this moment on - see Timeline.
        timeline = Timeline(follow_up=False)
        timeline.mark("wake")
        print("Listening...")

        # What was audibly going off at the moment the wake word fired,
        # read before the interaction quietens anything, since what
        # matters is what the user could hear when they started talking.
        ringing = self.clock.ringing_state()

        # Anything going off stops the moment the wake word is heard, rather than
        # playing on over the user - and into the microphone - until its sound
        # ends. It carries on afterwards unless the conversation dealt with it.
        self.speaker.silence("announcement")
        try:
            self.conversation.start(ringing, talked_over=talked_over)
            return self._handle_interaction(timeline=timeline)
        except Exception as e:
            # Said out loud, rather than only logged, so it isn't taken for the
            # assistant not having heard. Only for something which went wrong
            # during a conversation - one waiting for the wake word could
            # repeat every second.
            logger.error(f"Interaction failed: {e}", exc_info=True)
            self._apologise()
            return False
        finally:
            timeline.save(self.latency_file)

    def _handle_interaction(self, follow_up=False, timeline=None):
        """
        Handles one full interaction after the wake word: records the query,
        transcribes it, has the conversation work out the reply (see
        conversation.py), and plays it.

        Args:
            follow_up: True when the assistant has just asked the user a question
                       and is listening for the answer, which allows a little
                       longer to start speaking than a fresh request does.
            timeline: The Timeline to mark each stage on. The caller saves it.

        Returns:
            True if the user said the wake word over the reply - see _say.
        """
        # A throwaway one when none is given, so the marks below never need checking for.
        timeline = timeline or Timeline()

        speech_timeout = self.thresholds.get("speech_timeout", 3.0)
        if follow_up:
            # Answering a question takes a moment's thought, so there's more time to
            # start talking than when the user has just said the wake word.
            speech_timeout = self.thresholds.get("follow_up_timeout", speech_timeout * 2)

        # Listening starts from the moment the wake word was heard - or, for the answer
        # to a question, from now, before the listening sound, so an answer started
        # over the sound isn't lost. The microphone never stops, so nothing said
        # while the sound plays goes missing either way.
        listen_from = self.microphone.position if follow_up else self.wake.heard_at

        self.activity = "listening"

        # A short sound to say it's listening, since on a headless Pi there's otherwise no
        # way to tell the wake word was heard, or when to start talking. The microphone
        # hears it too, but it's far too short to count as the user starting to speak.
        if self.listening_sound_enabled:
            self._play_sound("listening.wav")

        # Speech-to-text is given the recording as it's made, so it can work out the
        # words while the user is still saying them - see speech_to_text.py.
        listening = self.stt.listen()

        # Records audio until silence / a timeout occurs. A fresh request also keeps a
        # moment from before the wake word was detected, since "Hey Nova what time is
        # it" said in one breath has often started on "what" by then.
        try:
            audio_data = self.microphone.record_until_silence(
                self.speech_detector, listen_from=listen_from,
                keep_before=0.0 if follow_up else self.pre_roll,
                silence_duration=self.thresholds.get("silence_duration", 0.6),
                speech_timeout=speech_timeout,
                timeout=self.thresholds.get("recording_timeout", 10.0),
                timeline=timeline, heard=listening.add)
        except BaseException:
            # Otherwise it would wait on for the rest of a recording which isn't coming.
            listening.cancel()
            raise

        if audio_data is None or len(audio_data) == 0:
            listening.cancel()
            timeline.note(outcome="no_speech")
            self._not_heard()
            return False

        # The words, once speech-to-text has caught up with the end of the recording,
        # carrying on listening if no text comes back. The audio is already at the
        # 16kHz speech-to-text expects, since the microphone keeps it at that.
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
            return False

        timeline.note(text_chars=len(text))
        self._show(f"You: {text}")

        # Everything between hearing the request and answering it - see conversation.py.
        reply = self.conversation.handle(text, follow_up=follow_up, timeline=timeline)

        for sound, category in reply.sounds:
            self._play_sound(sound, volume=self.volume.get(category))
        if reply.shown:
            self._show(f"{self.name}: {reply.shown}")
        if reply.say and self._say(reply.say, timeline=timeline, listen=True):
            return True

        # The assistant asked something, so it listens straight away for the answer.
        if reply.listen_again:
            logger.info("Question asked, listening for a reply")
            print("Listening...")
            # The answer is timed on its own, from when listening starts again.
            follow_up_timeline = Timeline(follow_up=True)
            try:
                return self._handle_interaction(follow_up=True, timeline=follow_up_timeline)
            finally:
                follow_up_timeline.save(self.latency_file)
        return False

# Runs the SmartAssistant if this script is executed directly. 
# This creates an instance of SmartAssistant and calls its run method to start the main loop.
if __name__ == "__main__":
    # Anything an older install kept in the repository folder - settings, keys, saved
    # timers, the Spotify sign-in, logs - is moved to where it now belongs first.
    moved = paths.migrate()
    setup_logging(os.path.join(paths.LOGS_DIR, "smart_assistant.log"))
    if moved:
        logger.info(f"Moved out of the repository folder: {', '.join(moved)} - see voice_assistant/paths.py")
    SmartAssistant().run()
