"""
Wake Phrase Module.

The wake word as words - what it is when config.yaml doesn't say, and taking it off
the front of what speech-to-text heard.

Kept apart from wake_word.py, which listens for it, so the brain can use it without
the satellite's audio libraries: it has no microphone, so no PortAudio for
sounddevice to load.
"""

import re

# Fallback wake word phrase if no config is passed in
DEFAULT_WAKE_WORD = "Hey Nova"


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
