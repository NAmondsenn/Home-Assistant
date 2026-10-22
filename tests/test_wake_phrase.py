"""
Wake Phrase Test
Checks the wake word is taken off the front of what speech-to-text heard.
"""

import pytest

from voice_assistant.wake_phrase import strip_wake_word


@pytest.mark.parametrize("heard, meant", [
    ("Hey Nova, what time is it?", "What time is it?"),
    ("Nova. Stop.", "Stop."),
    ("nova stop", "Stop"),
    ("Hey, Nova.", ""),
    ("What time is it?", "What time is it?"),
    # Only taken off the front, and only as a whole word.
    ("What's a nova?", "What's a nova?"),
    ("Novak Djokovic's age", "Novak Djokovic's age"),
    # The end of the wake word, misheard - all heard on the Pi.
    ("In Nova, what's the weather?", "What's the weather?"),
    ("Turn over, set a one minute timer.", "Set a one minute timer."),
    ("Get over. Cancel that alarm I just set.", "Cancel that alarm I just set."),
    # Only with punctuation straight after, so a real "over" is left alone.
    ("Over the weekend, remind me to call mum.", "Over the weekend, remind me to call mum."),
    ("I'm over it.", "I'm over it."),
])
def test_the_wake_word_is_taken_off_the_front_of_a_transcription(heard, meant):
    assert strip_wake_word(heard, "Hey Nova") == meant
