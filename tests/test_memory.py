"""
Memory Test
Checks what Nova remembers about the user - kept, forgotten, read out, and given to
Claude - using a memory file of the test's own.
"""

import json
import logging

import pytest

from voice_assistant import memory as memory_module
from voice_assistant.actions import Action, ActionExecutor, ActionResult, confirm
from voice_assistant.memory import Memory


@pytest.fixture
def memory(tmp_path):
    return Memory(path=str(tmp_path / "nova" / "memory.json"))


def test_a_fact_is_kept_privately_and_survives_a_restart(memory, tmp_path):
    assert memory.remember("You work at Tesco.") == {"success": True, "message": "I'll remember that.", "note": True}
    path = tmp_path / "nova" / "memory.json"
    assert json.loads(path.read_text()) == {"facts": ["You work at Tesco."]}
    assert path.stat().st_mode & 0o777 == 0o600
    assert Memory(path=str(path)).facts() == ["You work at Tesco."]


def test_the_same_thing_isnt_kept_twice(memory):
    memory.remember("You work at Tesco.")
    assert memory.remember("you work at tesco")["message"] == "I already knew that."
    assert memory.facts() == ["You work at Tesco."]


def test_memory_has_a_limit(memory, monkeypatch):
    # Everything remembered goes to Claude with every request.
    monkeypatch.setattr(memory_module, "MOST_FACTS", 2)
    memory.remember("You work at Tesco.")
    memory.remember("Your sister is called Amy.")
    assert memory.remember("Your bin day is Thursday.")["success"] is False
    assert len(memory.facts()) == 2


def test_the_closest_fact_is_forgotten(memory):
    memory.remember("You work at Tesco.")
    memory.remember("Your sister is called Amy.")
    assert memory.forget("that I work at tesco")["message"] == "Okay, I've forgotten that you work at Tesco."
    assert memory.facts() == ["Your sister is called Amy."]
    assert memory.forget("my favourite colour")["success"] is False


def test_a_forgotten_fact_goes_back_in_its_place(memory):
    for fact in ("You work at Tesco.", "Your sister is called Amy.", "Your bin day is Thursday."):
        memory.remember(fact)
    forgotten = memory.forget("your sister amy")
    assert forgotten["forgotten"] == "Your sister is called Amy." and forgotten["at"] == 1
    assert memory.put_back(forgotten["forgotten"], forgotten["at"]) == {"success": True, "message": "Put it back."}
    assert memory.facts() == ["You work at Tesco.", "Your sister is called Amy.", "Your bin day is Thursday."]
    # A place past the end, as the others have since been forgotten, is the end.
    assert memory.put_back("Your cat is called Tom.", 50)["success"]
    assert memory.facts()[-1] == "Your cat is called Tom."


def test_a_fact_remembered_again_meanwhile_isnt_put_back_twice(memory):
    memory.remember("You work at Tesco.")
    forgotten = memory.forget("that I work at tesco")
    memory.remember("you work at tesco")
    assert memory.put_back(forgotten["forgotten"], forgotten["at"]) == {"success": True, "message": "I already knew that."}
    assert memory.facts() == ["you work at tesco"]


def test_a_full_memory_says_it_cant_put_one_back(memory, monkeypatch):
    memory.remember("You work at Tesco.")
    forgotten = memory.forget("that I work at tesco")
    monkeypatch.setattr(memory_module, "MOST_FACTS", 1)
    memory.remember("Your sister is called Amy.")
    assert memory.put_back(forgotten["forgotten"], forgotten["at"]) == {
        "success": False, "message": "My memory's full, so it can't be put back."}
    assert memory.facts() == ["Your sister is called Amy."]


def test_forgetting_by_voice_tells_claude_nothing_more(memory, caplog):
    memory.remember("You work at Tesco.")
    executor = ActionExecutor(memory=memory)
    with caplog.at_level(logging.INFO):
        reply = ActionExecutor.forget(executor, "that I work at tesco")
        memory.remember("You work at Tesco.")
        result = executor.execute(Action("forget", {"fact": "that I work at tesco"}))
    # Neither in the tool's answer, nor in the result made from it.
    assert set(reply) == {"success", "message"}
    assert result.ok and result.say == "Okay, I've forgotten that you work at Tesco."
    # The fact is only in what's said back, which the user hears anyway.
    assert [r for r in caplog.records if r.levelno >= logging.INFO and not getattr(r, "transcript", False)
            and "Tesco" in r.getMessage()] == []


def test_forgetting_everything_keeps_one_copy_in_case_it_was_misheard(memory, tmp_path):
    memory.remember("You work at Tesco.")
    assert memory.forget_everything()["success"]
    assert memory.facts() == []
    assert json.loads((tmp_path / "nova" / "memory.json.old").read_text()) == {"facts": ["You work at Tesco."]}


def test_forgetting_which_cant_be_saved_says_so_and_forgets_nothing(memory, monkeypatch):
    # A full disk raised out of forget, as it never did out of remember - and the settings
    # page's request got no answer at all.
    memory.remember("You work at Tesco.")

    def full(facts, path=None):
        raise OSError("No space left on device")

    monkeypatch.setattr(memory, "_save", full)
    assert memory.forget("that I work at tesco") == {"success": False, "message": "Sorry, I couldn't forget that."}
    assert memory.forget_everything() == {"success": False, "message": "Sorry, I couldn't forget that."}
    assert memory.facts() == ["You work at Tesco."]


def test_what_is_known_is_read_out(memory):
    assert "don't know anything about you yet" in memory.describe()["message"]
    memory.remember("You work at Tesco.")
    memory.remember("Your sister is called Amy.")
    assert memory.describe()["message"] == "Here's what I know. You work at Tesco. Your sister is called Amy."


def test_a_broken_file_starts_afresh_rather_than_crashing(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text("{not json")
    assert Memory(path=str(path)).facts() == []


# Through the actions and Claude

def test_the_answer_is_said_before_i_ll_remember_that(memory):
    # "I'm vegetarian - what should I cook tonight?": the answer, then the note,
    # rather than the note instead of the answer.
    remembered = ActionExecutor(memory=memory).execute(Action("remember", {"fact": "You're vegetarian."}))
    assert remembered.confirm == "note"
    assert confirm([remembered], fallback="How about a mushroom risotto?") == (
        [], ["How about a mushroom risotto?", "I'll remember that."])
    # Not twice, when Claude has said it already.
    assert confirm([remembered], fallback="Congratulations on the new job! I'll remember that.") == (
        [], ["Congratulations on the new job! I'll remember that."])
    # Alongside a bare "Okay." there's only the note.
    assert confirm([remembered], fallback="Okay.") == ([], ["I'll remember that."])
    # And alongside another action, after that action's own confirmation.
    timer = ActionResult(True, "Timer set for 10 minutes.")
    assert confirm([remembered, timer], fallback="Okay.") == ([], ["Timer set for 10 minutes.", "I'll remember that."])


def test_claude_is_told_what_it_remembers(memory):
    from voice_assistant.llm import LLMHandler
    memory.remember("You work at Tesco.")
    llm = LLMHandler(api_key=None, memory=memory)
    prompt = llm._system_prompt(False)
    assert "call remember" in prompt and "You work at Tesco." in prompt
    assert "What you remember" not in LLMHandler(api_key=None, memory=Memory(path=memory.path + "x"))._system_prompt(False)


def test_without_memory_the_tools_arent_offered():
    offered = {schema["name"] for schema in ActionExecutor().tool_schemas()}
    assert not offered & {"remember", "forget", "forget_everything", "list_memories"}
