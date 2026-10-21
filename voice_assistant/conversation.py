"""
Conversation Module.

Decides how to answer what the user said: whether it answers a question the
assistant asked, stops something ringing, is an everyday command handled locally or
goes to Claude, which actions to run, and how they're confirmed - a sound, or words,
and whether to listen for an answer afterwards.

This used to be tangled up with the recording and playback in main.py, with its
ordering rules held in comments and instance flags, so none of it could be tested
without a microphone - or reused for anything which hears the user another way, like
a desktop push-to-talk or a satellite. Now main.py turns speech into text and the
reply back into sound, and everything in between is here, in plain Python: text in,
Reply out.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from voice_assistant.actions import confirm
from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant.timeline import Timeline

# Logger setup
logger = logging.getLogger(__name__)

# What was going off at the moment the wake word fired, when nothing was.
NOTHING_RINGING = {"insistent": False, "insistent_entries": [], "reminder": None, "reminder_sounding": False}


@dataclass
class Reply:
    """
    What the assistant does in answer to one thing the user said.

    Attributes:
        say: What to say out loud, or nothing.
        sounds: Sounds to play first, as (file name, volume category) pairs.
        shown: A line to show without saying it - what a chime stood for, when the
            chime is switched off and nothing needs saying.
        listen_again: Whether the assistant asked something, and should listen for
            the answer without the wake word.
    """
    say: str = ""
    sounds: List[Tuple[str, str]] = field(default_factory=list)
    shown: str = ""
    listen_again: bool = False


class Conversation:
    """The rules for answering the user, from one wake word to the end of any follow-ups."""

    def __init__(self, clock, intents, llm, actions, music, chime_enabled: bool = True, max_follow_ups: int = 3):
        """
        Args:
            clock: The Clock, for what's ringing and for dismissing it.
            intents: The LocalIntents, for the everyday commands handled without Claude.
            llm: The LLMHandler, for everything else.
            actions: The ActionExecutor, which runs what was asked for.
            music: The MusicHold, whose pause has to land before anything else is
                sent to the music player.
            chime_enabled: Whether a success is confirmed with the chime.
            max_follow_ups: How many times in a row the assistant may ask a question
                and listen for the answer without the wake word, so it can't hold
                the microphone indefinitely.
        """
        self.clock = clock
        self.intents = intents
        self.llm = llm
        self.actions = actions
        self.music = music
        self.chime_enabled = chime_enabled
        self.max_follow_ups = max_follow_ups
        self.start()

    def start(self, ringing: Optional[Dict] = None, talked_over: bool = False):
        """
        Starts a new conversation, as the wake word is heard.

        Args:
            ringing: What was audibly going off at that moment - Clock.ringing_state(),
                read before the conversation quietens anything, since what matters is
                what the user could hear when they started talking.
            talked_over: True when the wake word was said over the assistant's last
                reply, which was stopped there and then.
        """
        self.ringing = ringing or dict(NOTHING_RINGING)
        self.talked_over = talked_over
        # Counted per conversation, not per question, so a long back and forth still
        # ends after the cap is reached.
        self.follow_ups = 0
        # A reminder is only offered up once per conversation, however many
        # follow-up questions it runs to. The answer to that offer is only expected
        # within the same conversation, so a question left unanswered doesn't colour
        # the next one.
        self.reminder_offered = False
        self.reminder_answer_pending = False
        # Earlier conversations are only remembered for a few minutes, so this
        # morning's "play that one" doesn't colour tonight's "yes".
        self.llm.forget_stale_history()

    def handle(self, text: str, follow_up: bool = False, timeline: Optional[Timeline] = None) -> Reply:
        """
        Works out the answer to one thing the user said.

        Args:
            text: What they said, as transcribed, without the wake word.
            follow_up: True when it answers a question the assistant just asked.
            timeline: The Timeline to mark each stage on, if it's being timed.

        Returns:
            The Reply.
        """
        timeline = timeline or Timeline()

        # Answering the assistant's own "shall I turn that reminder off?". Checked
        # before anything else, since "no" here means leave it alone, while "no"
        # said to something ringing means make it stop - opposite outcomes from the
        # same word, told apart only by the question having just been asked.
        if self.reminder_answer_pending:
            self.reminder_answer_pending = False
            timeline.note(outcome="reminder_answer")
            answer = self.clock.answer_to_offer(text)
            if answer is False:
                logger.info("Reminder left running at the user's request")
                return Reply(say="Okay, I'll leave it.")
            if answer:
                self.clock.dismiss()
                logger.info("Reminder dismissed after being offered")
                return self._chime("Okay.")
            # Anything else is a fresh request, so it falls through and is handled
            # normally - the reminder simply carries on.

        # Anything short said while a timer is going off means "make it stop", so it's
        # handled here rather than going to the model, making it quicker and easier
        # to silence.
        if self.clock.is_ringing() and self.clock.looks_like_dismissal(text):
            result = self.clock.dismiss()
            timeline.note(outcome="dismissed_locally")
            logger.info("Dismissed by voice without asking the model")
            return self._chime(result["message"])

        # "Hey Nova, stop" or "Hey Nova, okay" over a reply only means stop talking,
        # which the wake word already did. Taken as a command, "stop" would pause the
        # music as well - which was only quiet while the assistant was talking.
        if self.talked_over and not follow_up and self.clock.looks_like_dismissal(text):
            timeline.note(outcome="stopped_talking")
            logger.info("Told to stop talking, so said nothing more")
            return Reply()

        # A timer or alarm going off is silenced by the user saying anything at all,
        # since talking to the assistant means they've heard it. Read from the wake
        # word rather than now, so one which came due mid-conversation - which the
        # user hasn't heard, because announcements wait for the conversation to end -
        # isn't silenced along with it.
        heard_ringing = self.ringing.get("insistent_entries", [])

        # The everyday commands which don't need a language model are handled straight
        # away without going to Claude, so they still work offline. Not the answer to
        # a question the assistant asked, though, which needs the conversation so far
        # to make sense of. See intents.py.
        result = None if follow_up else self.intents.match(text)
        if result:
            timeline.note(local_intent=True)
            self.llm.remember(text, result["response"])
        else:
            # Sent along with what's currently on the clock, so a mangled label like
            # "walk the frogs" can still be matched to the real "walk the dogs" reminder.
            clock_context = "; ".join(self.clock.describe_all()) or None
            result = self.llm.process_query(text, context=clock_context)
        timeline.mark("llm_done")
        response_text = result["response"]
        actions = result.get("actions", [])

        # Every action asked for is run, in order - "pause the music and set a ten
        # minute timer" is two of them.
        action_results = []
        if actions:
            # Music and volume actions talk to the music player too, so the pause has
            # to have landed first.
            self.music.wait()
            action_results = [self.actions.execute(action) for action in actions]
            timeline.mark("action_done")
            timeline.note(actions=", ".join(action.tool for action in actions))

        # Done after the action has run, so a request which was itself about the timer
        # ("cancel the timer") still finds it ringing and dismisses it, rather than
        # finding it already silent and deleting a pending one instead.
        if heard_ringing:
            self.clock.silence_insistent(heard_ringing)

        # An action either confirms itself - the music changing, or a sound - or has
        # its message said in place of the model's own reply, which for a tool call is
        # usually just "Okay.". See confirm().
        sounds, messages = confirm(action_results, self.chime_enabled, fallback=response_text)
        if action_results:
            response_text = " ".join(messages)

        # A reminder is left alone by all of that - it isn't insistent enough to count
        # as heard just because the user happened to speak. One caught in the quiet gap
        # between its rounds is easy to forget is even pending, so once the actual
        # request is dealt with the user is asked about it. One that was
        # mid-announcement isn't: it's loud and obvious, so it can be acknowledged
        # outright rather than needing an offer. Not tacked onto a question the model
        # is already asking, which would leave two to answer at once - and added last,
        # so it trails the answer rather than interrupting it.
        reminder = self.ringing.get("reminder")
        if (reminder and not self.ringing.get("reminder_sounding") and not self.reminder_offered
                and self.clock.is_ringing() and not response_text.rstrip().endswith("?")):
            self.reminder_offered = True
            self.reminder_answer_pending = True
            response_text = f"{response_text} Also, your {reminder} reminder is still going. Shall I turn it off?".strip()

        if not response_text:
            logger.info(f"Action confirmed itself: {[r.say for r in action_results]}", extra=TRANSCRIPT)
            return Reply(sounds=sounds)

        # Kept for "send that to my phone" - unless this was that, or the next one would
        # send "Sent.".
        if not any(action.tool == "send_to_phone" for action in actions):
            self.actions.last_answer = (text, response_text)

        # The model's history only has its own text for this turn - usually "Okay." when
        # it called a tool - so it's told what's actually being said. Otherwise a
        # question an action asked ("Which one?") would be missing when the answer
        # comes back on the next turn. Only what's spoken is recorded: an action
        # confirmed by a chime or by the music changing keeps the model's own "Okay.".
        # What each action said goes with it, as the tools' own answers.
        if result.get("success"):
            self.llm.record_reply(text, response_text, [action_result.say for action_result in action_results])

        # If the assistant asked something, it listens straight away for the answer -
        # but only so many times in a row.
        listen_again = response_text.rstrip().endswith("?") and self.follow_ups < self.max_follow_ups
        if listen_again:
            self.follow_ups += 1
        return Reply(say=response_text, sounds=sounds, listen_again=listen_again)

    def _chime(self, message: str) -> Reply:
        """A success confirmed by the chime - or, with it switched off, only shown."""
        if self.chime_enabled:
            return Reply(sounds=[("success_chime.wav", "general")])
        return Reply(shown=message)
