import os
import re
import logging
from openwakeword.model import Model
from voice_assistant import paths, storage
from voice_assistant.microphone import RATE, Microphone

logger = logging.getLogger(__name__)

# Whether the microphone's muted - from Home Assistant or the settings page - kept in
# the data folder (see paths.py), so it stays muted across a restart.
MICROPHONE_FILE = os.path.join(paths.DATA_DIR, "microphone.json")

# openWakeWord works through audio 80ms at a time - this many samples at 16kHz.
CHUNK_SAMPLES = 1280

# Seconds of the audio just gone which the model works through before listening for
# real. It judges each moment on the second or so before it, so started from nothing
# it couldn't hear "Hey Nova" for about a second - just when someone answering a
# reply is most likely to say it.
CATCH_UP_SECONDS = 2.0

# Fallback wake word phrase if no config is passed in
DEFAULT_WAKE_WORD = "Hey Nova"

# One of openWakeWord's own models, listened for until the configured wake word's
# model has been copied into models/. setup.sh downloads it along with the rest.
STAND_IN_WAKE_WORD = "Hey Jarvis"
STAND_IN_MODEL = "hey_jarvis"

def _wake_word_filename(phrase):
    """
    Turns a wake word phrase into the model filename convention
    its .onnx model uses.
    """
    return re.sub(r'\s+', '_', phrase.strip().lower())

def strip_wake_word(text, phrase=DEFAULT_WAKE_WORD):
    """
    Removes the wake word from the start of a transcription.

    A recording starts a moment before the wake word was detected (see
    Microphone.record_until_silence), so its end is sometimes transcribed too -
    "Nova. Stop." - which would stop a short reply like that being recognised as
    dismissing a timer, since every word of one has to be a dismissal.

    Args:
        text: What speech-to-text heard.
        phrase: The wake word, e.g. "Hey Nova".

    Returns:
        The text without the wake word in front, e.g. "Stop."
    """
    words = phrase.split()
    if not words:
        return text

    # Matches the whole phrase or only the end of it ("Hey Nova" or just "Nova"),
    # along with whatever punctuation speech-to-text put around it - but only as whole
    # words, so "Novak Djokovic" is left as it is.
    pattern = ""
    for word in words[:-1]:
        pattern = rf"(?:{pattern}{re.escape(word)}\W+)?"
    pattern = rf"^\W*{pattern}{re.escape(words[-1])}\b\W*"

    rest = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE)

    # Speech-to-text often mishears the end of the wake word, too, and puts it in front of
    # the request as a little clause of its own - "In Nova, what's the weather?",
    # "Turn over, set a one minute timer". Up to two words ending in the wake word,
    # or in what "Nova" most often turns into, are taken off as well - but only with
    # punctuation straight after, so "Over the weekend, remind me..." is left alone.
    if rest == text:
        endings = [re.escape(words[-1])] + (["over"] if words[-1].lower() == "nova" else [])
        misheard = rf"^\W*(?:\w+\W+)?(?:{'|'.join(endings)})[.,!?]+\s*"
        rest = re.sub(misheard, "", text, count=1, flags=re.IGNORECASE)

    return rest[:1].upper() + rest[1:]

class WakeWordDetector:
    """
    A simple wake word detector using the openWakeWord library. The wake word phrase,
    whose .onnx model gets loaded, comes from config.yaml rather than being hardcoded.
    The sensitivity can be adjusted to make detection more or less strict, with a value
    between 0 and 1. Muted, from Home Assistant or the settings page, it never hears the
    wake word - but alarms and timers still ring, and Home Assistant's messages are
    still said.
    """

    def __init__(self, config=None, sensitivity=None, microphone=None):
        """
        Args:
            config: Configs loaded from config.yaml.
            sensitivity: Overrides the configured wake_word_confidence.
            microphone: The shared Microphone to listen through. Left out, one is
                opened on first use - for running the detector on its own, as
                scripts/manual/check_wake_word.py does.
        """
        # Pulls sensitivity from config.yaml's "thresholds" section, the wake word
        # phrase from "assistant", and the mic sample rate from "audio".
        thresholds_config = config.section("thresholds") if config else {}
        assistant_config = config.section("assistant") if config else {}
        audio_config = config.section("audio") if config else {}

        # Mic settings come from the same audio section the rest of the app uses.
        self.mic_rate = audio_config.get("sample_rate", 48000)
        self.microphone = microphone
        # The microphone position at which the wake word was last heard - see listen_once.
        self.heard_at = None

        self.sensitivity = sensitivity if sensitivity is not None else thresholds_config.get("wake_word_confidence",
                                                                                             0.5)
        self.wake_word_phrase = assistant_config.get("wake_word", DEFAULT_WAKE_WORD)
        self.wake_word_key = _wake_word_filename(self.wake_word_phrase)
        model_path = os.path.join(paths.MODELS_DIR, f"{self.wake_word_key}.onnx")

        # A custom wake word's model is trained separately and copied in by hand (see
        # the README), so a fresh install doesn't have one - and it used to fail to
        # start at all. Until it's there, openWakeWord's own "Hey Jarvis" stands in.
        if not os.path.exists(model_path):
            logger.warning(f"No wake word model at {model_path}, so listening for "
                           f"'{STAND_IN_WAKE_WORD}' until it's copied in")
            self.wake_word_phrase = STAND_IN_WAKE_WORD
            self.wake_word_key = model_path = STAND_IN_MODEL

        logger.info(f"Loading openWakeWord model for '{self.wake_word_phrase}'...")
        # inference_framework is set to "onnx" rather than the default "tflite".
        # This is because tflite-runtime has no wheels for Python 3.12+ and the wake word
        # models are .onnx files anyway.
        self.model = Model(wakeword_models=[model_path], inference_framework="onnx")
        logger.info(f"openWakeWord initialised with '{self.wake_word_key}' model")

        self._muted = self._load_muted()
        if self._muted:
            logger.info(f"The microphone's muted, so '{self.wake_word_phrase}' won't be heard until it's unmuted")

    @property
    def muted(self) -> bool:
        """Whether the microphone's muted, so the wake word goes unheard."""
        return self._muted

    def set_muted(self, muted: bool):
        """
        Mutes or unmutes the microphone, straight away - even part way through
        listen_once - and for every restart after.

        Args:
            muted: True to stop the wake word being heard.
        """
        self._muted = bool(muted)
        try:
            storage.save_json(MICROPHONE_FILE, {"muted": self._muted})
        except OSError as e:
            # It's still muted or unmuted until Nova restarts, just not after.
            logger.error(f"Couldn't save whether the microphone's muted: {e}")

    @staticmethod
    def _load_muted() -> bool:
        """Whether the last run left the microphone muted - not, if that can't be told."""
        try:
            saved = storage.load_json(MICROPHONE_FILE, default={})
        except (OSError, ValueError) as e:
            logger.error(f"Couldn't read whether the microphone's muted, so it isn't: {e}")
            return False
        return isinstance(saved, dict) and saved.get("muted") is True

    def listen_once(self, until=None):
        """
        Listens for the wake word, reading the shared microphone stream 80ms at a
        time from the moment this is called. Loops until the wake word is heard.

        Args:
            until: Called between each 80ms - listening stops once it returns True.
                For hearing the wake word over a reply, until the reply has
                finished. None listens until the wake word is heard.

        Returns:
            The confidence score as a float, or None if `until` stopped it first.
            heard_at is set to the microphone position the wake word was heard
            at, so a recording can carry on from there.
        """
        if self.microphone is None:
            self.microphone = Microphone(sample_rate=self.mic_rate)

        if until is None:
            logger.info(f"Listening for '{self.wake_word_phrase}'...")
        self.model.reset()

        # The model catches up on the last couple of seconds first - but never from
        # before the last wake word, which would only be heard again - and only what
        # comes after now can wake it.
        now = self.microphone.position
        reader = self.microphone.listen(start=max(0, now - int(CATCH_UP_SECONDS * RATE), self.heard_at or 0))
        while True:
            if until is not None and until():
                return None
            # Feeds audio into the openWakeWord model and gets the prediction score for the configured wake word
            prediction = self.model.predict(reader.read(CHUNK_SAMPLES))
            if reader.position <= now:
                continue
            score = prediction.get(self.wake_word_key, 0)

            # Returns the confidence score and logs it if the wake word is detected.
            # Muted, the wake word is ignored here, rather than the audio going
            # unread, so the model keeps up with the microphone and hears it again
            # the moment it's unmuted. The same goes for the wake word said over a
            # reply - see _say in main.py.
            if score >= self.sensitivity and not self._muted:
                self.heard_at = reader.position
                logger.info(f"Wake word detected! (confidence: {score:.2f})")
                return float(score)
