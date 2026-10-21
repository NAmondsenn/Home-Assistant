"""
Conversation Test
Drives the rules for answering the user with text alone - no microphone, speaker,
Whisper or Claude - using fakes for the clock, local commands, Claude and the
actions, so each rule can be checked on its own.
"""

from voice_assistant.actions import Action, ActionResult
from voice_assistant.clock import Clock
from voice_assistant.conversation import Conversation


class FakeClock:
    """Stands in for the Clock: what's ringing, and the word rules it really uses."""

    looks_like_dismissal = staticmethod(Clock.looks_like_dismissal)
    answer_to_offer = staticmethod(Clock.answer_to_offer)

    def __init__(self, ringing=False):
        self.ringing = ringing
        self.dismissed = 0
        self.silenced = []

    def is_ringing(self):
        return self.ringing

    def dismiss(self):
        self.dismissed += 1
        self.ringing = False
        return {"success": True, "message": "Okay, stopped."}

    def silence_insistent(self, entries):
        self.silenced.append(entries)

    def describe_all(self):
        return []


class FakeIntents:
    def __init__(self, handles=None):
        self.handles = handles or {}

    def match(self, text):
        return self.handles.get(text)


class FakeLLM:
    """Stands in for Claude, answering each request with the next reply given."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.asked, self.remembered, self.recorded, self.tool_answers = [], [], [], []

    def forget_stale_history(self):
        pass

    def process_query(self, text, context=None):
        self.asked.append(text)
        return self.replies.pop(0)

    def remember(self, text, reply):
        self.remembered.append((text, reply))

    def record_reply(self, text, spoken, tool_answers=()):
        self.recorded.append((text, spoken))
        self.tool_answers.append(list(tool_answers))


class FakeActions:
    def __init__(self, results=None):
        self.results = results or {}
        self.run = []

    def execute(self, action):
        self.run.append(action.tool)
        return self.results.get(action.tool, ActionResult(True, "Done."))


class FakeMusic:
    def __init__(self):
        self.waited = 0

    def wait(self):
        self.waited += 1


def _answer(text, *actions):
    return {"response": text, "actions": list(actions), "success": True}


def _conversation(clock=None, intents=None, llm=None, actions=None, ringing=None, **settings):
    made = Conversation(clock or FakeClock(), intents or FakeIntents(), llm or FakeLLM(), actions or FakeActions(),
                        FakeMusic(), **settings)
    made.start(ringing)
    return made


# Answering a question

def test_a_plain_answer_is_said_and_remembered():
    llm = FakeLLM(_answer("Paris."))
    reply = _conversation(llm=llm).handle("what's the capital of France")
    assert (reply.say, reply.sounds, reply.listen_again) == ("Paris.", [], False)
    assert llm.recorded == [("what's the capital of France", "Paris.")]


def test_a_local_command_never_reaches_claude():
    llm = FakeLLM()
    intents = FakeIntents({"pause": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions({"control_playback": ActionResult(True, "Paused", confirm="chime")})
    reply = _conversation(intents=intents, llm=llm, actions=actions).handle("pause")
    assert llm.asked == [] and llm.remembered == [("pause", "Okay.")]
    assert reply.sounds == [("success_chime.wav", "general")] and reply.say == ""


def test_the_answer_to_a_question_isnt_taken_for_a_local_command():
    # "Stop" answering "which timer?" needs the conversation so far to make sense of.
    llm = FakeLLM(_answer("Okay."))
    intents = FakeIntents({"stop": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    _conversation(intents=intents, llm=llm).handle("stop", follow_up=True)
    assert llm.asked == ["stop"]


def test_actions_wait_for_the_music_to_have_paused_and_say_their_own_words():
    llm = FakeLLM(_answer("Okay.", Action("set_timer", {"duration_seconds": 600})))
    actions = FakeActions({"set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    conversation = _conversation(llm=llm, actions=actions)
    reply = conversation.handle("set a ten minute timer")
    assert conversation.music.waited == 1 and reply.say == "Timer set for 10 minutes."


def test_what_each_action_said_is_kept_for_the_model():
    llm = FakeLLM(_answer("Okay.", Action("get_weather", {"day": "today"}), Action("set_timer", {"duration_seconds": 600})))
    actions = FakeActions({"get_weather": ActionResult(True, "It's 10 degrees."),
                           "set_timer": ActionResult(True, "Timer set for 10 minutes.")})
    _conversation(llm=llm, actions=actions).handle("what's the weather, and set a ten minute timer")
    assert llm.tool_answers == [["It's 10 degrees.", "Timer set for 10 minutes."]]


def test_a_question_is_listened_for_but_only_so_many_times_in_a_row():
    llm = FakeLLM(*[_answer("Which one?")] * 3)
    conversation = _conversation(llm=llm, max_follow_ups=2)
    assert [conversation.handle("cancel it").listen_again for _ in range(3)] == [True, True, False]


# Something ringing

def test_stop_while_ringing_is_handled_without_claude():
    clock, llm = FakeClock(ringing=True), FakeLLM()
    reply = _conversation(clock=clock, llm=llm).handle("Stop.")
    assert clock.dismissed == 1 and llm.asked == []
    assert reply.sounds == [("success_chime.wav", "general")]


def test_stop_heard_as_so_while_ringing_is_still_a_stop():
    # Heard over a ringing timer on the Pi, "So" went to Claude, which replied "Your
    # message just says 'So'" and asked what was wanted.
    clock, llm = FakeClock(ringing=True), FakeLLM()
    _conversation(clock=clock, llm=llm).handle("So")
    assert clock.dismissed == 1 and llm.asked == []


def test_without_the_chime_a_dismissal_is_shown_not_said():
    reply = _conversation(clock=FakeClock(ringing=True), chime_enabled=False).handle("Stop.")
    assert (reply.sounds, reply.shown, reply.say) == ([], "Okay, stopped.", "")


def test_talking_over_a_timer_silences_it_after_the_request_is_dealt_with():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's sunny."))
    ringing = {"insistent": True, "insistent_entries": ["timer 1"], "reminder": None, "reminder_sounding": False}
    _conversation(clock=clock, llm=llm, ringing=ringing).handle("what's the weather like")
    assert clock.silenced == [["timer 1"]] and clock.dismissed == 0


def test_a_quiet_reminder_is_offered_once_and_the_answer_heard():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."), _answer("Paris."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)

    reply = conversation.handle("what time is it")
    assert reply.say == "It's 7:36 PM. Also, your walk the dogs reminder is still going. Shall I turn it off?"
    assert reply.listen_again

    # "No" here means leave it, where said to something ringing it means stop.
    assert conversation.handle("no", follow_up=True).say == "Okay, I'll leave it."
    assert clock.dismissed == 0
    # And it isn't offered again in the same conversation.
    assert "reminder" not in conversation.handle("what's the capital of France").say


def test_yes_to_the_reminder_offer_turns_it_off():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)
    conversation.handle("what time is it")
    assert conversation.handle("yes please", follow_up=True).sounds == [("success_chime.wav", "general")]
    assert clock.dismissed == 1


def test_a_new_conversation_forgets_an_unanswered_offer():
    clock = FakeClock(ringing=True)
    llm = FakeLLM(_answer("It's 7:36 PM."), _answer("Okay, leaving it."))
    ringing = {"insistent": False, "insistent_entries": [], "reminder": "walk the dogs", "reminder_sounding": False}
    conversation = _conversation(clock=clock, llm=llm, ringing=ringing)
    conversation.handle("what time is it")
    clock.ringing = False
    conversation.start()
    conversation.handle("no")
    assert llm.asked[-1] == "no"


# Talked over

def test_stop_said_over_a_reply_only_stops_the_talking():
    # Taken as a command, "stop" paused the music too - which was only quiet while
    # the assistant was talking.
    llm = FakeLLM()
    intents = FakeIntents({"Stop.": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions()
    conversation = _conversation(intents=intents, llm=llm, actions=actions)
    conversation.start(talked_over=True)
    for said in ("Stop.", "Okay, thanks.", "Shut up.", "Soap.", "So."):
        reply = conversation.handle(said)
        assert (reply.say, reply.sounds, reply.listen_again) == ("", [], False)
    assert actions.run == [] and llm.asked == [] and llm.remembered == []


def test_a_request_said_over_a_reply_is_answered_as_usual():
    llm = FakeLLM(_answer("Paris."))
    conversation = _conversation(llm=llm)
    conversation.start(talked_over=True)
    assert conversation.handle("what's the capital of France").say == "Paris."


def test_stop_said_after_the_reply_has_finished_is_still_a_command():
    intents = FakeIntents({"Stop.": _answer("Okay.", Action("control_playback", {"action": "pause"}))})
    actions = FakeActions()
    _conversation(intents=intents, actions=actions).handle("Stop.")
    assert actions.run == ["control_playback"]
