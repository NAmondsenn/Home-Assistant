"""
Conversation Module.

Decides how to answer what the user said: whether it answers a question the
assistant asked, stops something ringing, is an everyday command handled locally or
goes to Claude, which actions to run, and how they're confirmed - a sound, or words,
and whether to listen for an answer afterwards.

This used to be tangled up with the recording and playback in main.py, with its
ordering rules held in comments and instance flags, so none of it could be tested
without a microphone - or reused for anything which hears the user another way, like
a desktop push-to-talk or a satellite. Now each satellite's session (see session.py)
turns speech into text and the reply back into sound, and everything in between is
here, in plain Python: text in, Reply out.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from voice_assistant.actions import Action, ActionExecutor, ActionResult, confirm
from voice_assistant.devices import told_claude
from voice_assistant.logging_setup import TRANSCRIPT
from voice_assistant.timeline import Timeline

# Logger setup
logger = logging.getLogger(__name__)

# What was going off at the moment the wake word fired, when nothing was.
NOTHING_RINGING = {"insistent": False, "insistent_entries": [], "reminder": None, "reminder_sounding": False}

# Seconds to wait for Home Assistant's lights before asking Claude without them. Every
# request to Claude includes the lights, so a Home Assistant which is slow or down must
# cost roughly this much at most, rather than the five seconds anything else waits -
# "what's the weather" shouldn't take longer because the lights couldn't be read.
LIGHTS_TIMEOUT = 1.5

# The whole of what may be said back to "Shut down the PC?" to do it, and to leave it. Not
# Clock.answer_to_offer, which takes "stop", "cancel" or "thanks" for a yes - harmless for a
# reminder, but here it'd shut a PC down - and "yes" with a request after it ("yes, play
# music") for one too. Anything else is a fresh request, and nothing is pressed.
AGREED = {"yes", "yeah", "yep", "yup", "yes please", "do it", "go ahead", "go on", "sure", "ok do it",
          "okay do it"}
DECLINED = {"no", "nope", "nah", "no thanks", "no thank you", "don't", "dont", "don't do it", "no don't",
            "stop", "cancel", "cancel that", "cancel it", "leave it", "no leave it", "never mind", "nevermind",
            "not now", "no not now"}


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

    def __init__(self, clock, intents, llm, actions, music, chime_enabled: bool = True, max_follow_ups: int = 3,
                 routines=None, satellite: Optional[str] = None):
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
            routines: The user's Routines, or None, for the words which start one.
            satellite: The satellite the user is talking to, whose ringing alone they
                can hear, and so stop - or None for anything ringing anywhere.
        """
        self.clock = clock
        self.intents = intents
        self.llm = llm
        self.actions = actions
        self.music = music
        self.chime_enabled = chime_enabled
        self.max_follow_ups = max_follow_ups
        self.routines = routines
        self.satellite = satellite
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
        # What a yes to the question just asked would do - shutting the PC down, after
        # "Shut down the PC?" - or None. Only ever the very next thing said in the same
        # conversation answers it, so a new wake word drops it.
        self.asked_first: Optional[Action] = None
        # Earlier conversations are only remembered for a few minutes, so this
        # morning's "play that one" doesn't colour tonight's "yes".
        self.llm.forget_stale_history()

    def handle(self, text: str, follow_up: bool = False, timeline: Optional[Timeline] = None,
               in_routine: bool = False) -> Reply:
        """
        Works out the answer to one thing the user said.

        Args:
            text: What they said, as transcribed, without the wake word.
            follow_up: True when it answers a question the assistant just asked.
            timeline: The Timeline to mark each stage on, if it's being timed.
            in_routine: True for a step of a routine, said for the user - see run_routine.

        Returns:
            The Reply.
        """
        timeline = timeline or Timeline()

        # Answering "Shut down the PC?" - read before anything else, the follow-up window's
        # going straight to Claude included, and only once: whatever's said next drops it.
        # Only a yes runs it, and as Nova's own action, never the model's.
        asked_first, self.asked_first = self.asked_first, None
        if asked_first is not None:
            answer = agreed(text) if follow_up else None
            if answer is not None:
                timeline.note(outcome="asked_first_answer")
                said = (self._run([Action(asked_first.tool, asked_first.params)])[0].say if answer
                        else "Okay, I'll leave it.")
                logger.info(f"{asked_first.tool} {'run' if answer else 'not run'} on the user's answer")
                self.llm.remember(text, said)
                return Reply(say=said)
            # Anything else is a fresh request, and nothing is done.

        # Answering the assistant's own "shall I turn that reminder off?". Checked
        # before anything else bar that, since "no" here means leave it alone, while "no"
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
                self.clock.dismiss(satellite=self.satellite)
                logger.info("Reminder dismissed after being offered")
                return self._chime("Okay.")
            # Anything else is a fresh request, so it falls through and is handled
            # normally - the reminder simply carries on.

        # Anything short said while a timer is going off means "make it stop", so it's
        # handled here rather than going to the model, making it quicker and easier
        # to silence.
        if self.clock.is_ringing(satellite=self.satellite) and self.clock.looks_like_dismissal(text):
            result = self.clock.dismiss(satellite=self.satellite)
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

        # One of the user's routines, said by its own words or asked for by name - see
        # routines.py. Not as one of a routine's own steps, which would start it again.
        routine = None if follow_up or in_routine or self.routines is None else self.routines.called(text)
        if routine:
            timeline.note(outcome="routine")
            return self.run_routine(routine, timeline=timeline)

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
        # Bar one whose tool's answer is the user's alone - "where's my phone?" - which can't be
        # the answer to anything Nova asks, and which Claude, once asked, would hear.
        result = self.intents.match(text, only=ActionExecutor.private_tools()) if follow_up else self.intents.match(text)
        local = result is not None
        if local:
            timeline.note(local_intent=True)
        else:
            result = self._ask_claude(text)
        timeline.mark("llm_done")
        actions = result.get("actions", [])

        # Every action asked for is run, in order - "pause the music and set a ten
        # minute timer" is two of them.
        action_results = self._run(actions)

        # Home Assistant's Assist only knows so many sentences. "Dim the lights" isn't
        # one of them, though Claude can set a brightness - so what Assist matched
        # nothing in is asked of Claude, as if no local command had matched. Only
        # remembered once that's known, or the request would be in the model's history
        # twice: here, and again from its own answer.
        if local and all(action_result.understood for action_result in action_results):
            stand_ins = _stand_ins(actions, action_results)
            self.llm.remember(text, next(iter(stand_ins.values()), result["response"]))
        elif local:
            logger.info("Home Assistant didn't understand that, so asking Claude")
            timeline.note(local_intent=False)
            result = self._ask_claude(text)
            timeline.mark("llm_done")
            actions = result.get("actions", [])
            action_results = self._run(actions)
        response_text = result["response"]
        if actions:
            # Marked here rather than as each run finishes, so Assist's failed attempt
            # can't leave a time from before Claude was asked if Claude's answer has no
            # actions of its own.
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
        # Nor in a routine, whose next step would be taken for the answer.
        reminder = self.ringing.get("reminder")
        if (reminder and not in_routine and not self.ringing.get("reminder_sounding") and not self.reminder_offered
                and self.clock.is_ringing(satellite=self.satellite) and not response_text.rstrip().endswith("?")):
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
        # Where the phone is - a private answer - is the user's alone, whoever asked for it:
        # Claude's history goes to Anthropic with every later request, so it only has that it
        # was said, in place of the words. Claude needs no place to answer anything it's asked.
        if result.get("success"):
            stand_ins = _stand_ins(actions, action_results)
            recorded = response_text
            for index, stand_in in stand_ins.items():
                if action_results[index].say:
                    recorded = recorded.replace(action_results[index].say, stand_in)
            self.llm.record_reply(text, recorded, [stand_ins.get(index, action_result.say)
                                                   for index, action_result in enumerate(action_results)])

        # If the assistant asked something, it listens straight away for the answer -
        # but only so many times in a row.
        listen_again = response_text.rstrip().endswith("?") and self.follow_ups < self.max_follow_ups
        if listen_again:
            self.follow_ups += 1
            # A question one of the actions asks first, answered by what's said next. Not one
            # nobody's listening for an answer to, nor one asked in a routine, whose next step
            # would be taken for the answer.
            if not in_routine:
                self.asked_first = _only_question(response_text, action_results)
        return Reply(say=response_text, sounds=sounds, listen_again=listen_again)

    def _ask_claude(self, text: str) -> Dict:
        """
        Asks Claude to answer what the user said, along with what's going on around it.

        Args:
            text: What they said.

        Returns:
            What LLMHandler.process_query gives.
        """
        # Sent along with what's currently on the clock, so a mangled label like
        # "walk the frogs" can still be matched to the real "walk the dogs" reminder.
        timers = "; ".join(self.clock.describe_all()) or None
        # And the lights, with the room Nova is in, so "dim the lights" means that
        # room's. Left out if Home Assistant isn't there, or is too slow to wait for.
        home = self.actions.home
        lights = (home.describe_lights(timeout=LIGHTS_TIMEOUT) if home else "") or None
        # What Claude reads, such as home_status, goes back to it to answer from.
        return self.llm.process_query(text, context=timers, lights=lights,
                                      read=lambda action: self.actions.execute(action).say)

    def _run(self, actions: List[Action]) -> List[ActionResult]:
        """
        Runs the actions asked for, in order.

        Args:
            actions: The Actions, if any.

        Returns:
            The ActionResult of each.
        """
        if not actions:
            return []
        # Music and volume actions talk to the music player too, so the pause has
        # to have landed first.
        self.music.wait()
        return [self.actions.execute(action) for action in actions]

    def run_routine(self, routine: Dict, timeline: Optional[Timeline] = None) -> Reply:
        """
        Runs a routine's steps in turn, each as if the user had said it, and says what
        they say, together - after any sounds, each played once. A question a step
        asks isn't listened for an answer to, as nobody asked it.

        Args:
            routine: The routine, as Routines has it.
            timeline: The Timeline to mark each stage on, if it's being timed.

        Returns:
            The Reply.
        """
        logger.info(f"Running the {routine['name']} routine", extra=TRANSCRIPT)
        said, shown, sounds = [], [], []
        for step in routine["steps"]:
            reply = self.handle(step, timeline=timeline, in_routine=True)
            said.append(reply.say)
            shown.append(reply.shown)
            sounds.extend(sound for sound in reply.sounds if sound not in sounds)
        return Reply(say=" ".join(part for part in said if part), sounds=sounds,
                     shown=" ".join(part for part in shown if part))

    def _chime(self, message: str) -> Reply:
        """A success confirmed by the chime - or, with it switched off, only shown."""
        if self.chime_enabled:
            return Reply(sounds=[("success_chime.wav", "general")])
        return Reply(shown=message)


def agreed(text: str) -> Optional[bool]:
    """
    Whether what was said back to a question that asks first - "Shut down the PC?" - agreed.

    Args:
        text: What the user said, e.g. "Nova, yes please."

    Returns:
        True for the whole of it a plain yes (AGREED), False for a plain no (DECLINED), else
        None - after its punctuation, a "nova" at the start and a "please" at the end are
        taken off.
    """
    words = re.sub(r"[^a-z' ]", " ", text.lower().replace("\u2019", "'")).split()
    if words[:1] == ["nova"]:
        words = words[1:]
    if words[-1:] == ["please"]:
        words = words[:-1]
    said = " ".join(words)
    if said in AGREED:
        return True
    if said in DECLINED:
        return False
    return None


def _only_question(reply: str, results: List[ActionResult]) -> Optional[Action]:
    """
    What a yes to the reply would do - only when exactly one result asks first and its
    question is the whole of what's asked: the reply's last sentence, with no other ending
    "?". "Shut down the PC? Which light?" asks two things, so a yes does neither.

    Args:
        reply: The whole reply, as it's said.
        results: What each of the turn's actions said.

    Returns:
        The Action to run on a yes, or None.
    """
    asking = [result for result in results if result.ok and result.ask_first]
    reply = reply.rstrip()
    if len(asking) != 1 or not asking[0].say or not reply.endswith(asking[0].say):
        return None
    return None if "?" in reply[:-len(asking[0].say)] else asking[0].ask_first


def _stand_ins(actions: List[Action], results: List[ActionResult]) -> Dict[int, str]:
    """
    What Claude's history has in place of each private answer, by its action's place in the
    turn - e.g. {0: "I said where your phone is."}.

    Args:
        actions: The actions the turn ran.
        results: What each said, in the same order.
    """
    return {index: told_claude(str(action.params.get("device") or "phone"))
            for index, (action, result) in enumerate(zip(actions, results)) if result.private}
