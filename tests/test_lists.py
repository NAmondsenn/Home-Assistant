"""
Lists Test
Checks Home Assistant's to-do lists are found by the user's own words, and added to,
read and ticked off, using a fake in place of Home Assistant, so nothing is sent anywhere.
"""

import pytest
import requests

from voice_assistant.choices import ChoiceStore
from voice_assistant.home_assistant import CANT_REACH
from voice_assistant.lists import Lists, list_words

# The lists as the live checks found them, each with its entity id.
TODAY = {"Shopping list": "todo.shopping_list", "todo": "todo.todo", "notepad": "todo.notepad",
         "uni-work": "todo.uni_work"}


def _list(name, entity=None, aliases=()):
    """A list as HomeAssistant.todo_lists gives it."""
    entity = entity or "todo." + name.lower().replace(" ", "_").replace("-", "_")
    return {"id": entity, "name": name, "count": 0, "aliases": list(aliases)}


def _today():
    return [_list(name, entity) for name, entity in TODAY.items()]


class FakeHome:
    """Stands in for HomeAssistant, with each list's things still to do, by its entity id."""

    def __init__(self, lists, items=None):
        self.lists = lists
        self.items = {list_id: list(things) for list_id, things in (items or {}).items()}
        # What's raised by everything, as with Home Assistant down, and by add_item for one thing.
        self.failure = None
        self.add_fails = {}
        self.tick_fails = None
        self.added = []
        self.ticked = []

    def todo_lists(self):
        if self.failure:
            raise self.failure
        return [dict(found) for found in self.lists]

    def list_items(self, list_id):
        if self.failure:
            raise self.failure
        return [{"summary": summary, "uid": f"{list_id}-{number}"}
                for number, summary in enumerate(self.items.get(list_id, []))]

    def add_item(self, list_id, item):
        if item in self.add_fails:
            raise self.add_fails[item]
        self.added.append((list_id, item))

    def tick_off(self, list_id, uid):
        if self.tick_fails:
            raise self.tick_fails
        self.ticked.append((list_id, uid))


def _server_error():
    response = requests.Response()
    response.status_code = 500
    return requests.HTTPError("500 Server Error", response=response)


@pytest.fixture
def make_lists(tmp_path):
    def factory(lists=None, items=None):
        home = FakeHome(_today() if lists is None else lists, items)
        return Lists(home, ChoiceStore(str(tmp_path / "lists.json"), {"main": None}))
    return factory


def _found(lists, said):
    found, sentence = lists.find(said)
    return found["name"] if found else sentence


# Which list

@pytest.mark.parametrize("said, name", [
    ("shopping", "Shopping list"), ("my shopping list", "Shopping list"),
    ("todo", "todo"), ("to do", "todo"), ("to-do list", "todo"), ("my to-dos", "todo"), ("to dos", "todo"),
    ("notes", "notepad"), ("my notes", "notepad"), ("notepad", "notepad"),
    ("uni work", "uni-work"), ("uni-work", "uni-work"),
])
def test_each_list_is_found_by_the_users_own_words(make_lists, said, name):
    assert _found(make_lists(), said) == name


def test_list_words_are_matched_forgivingly():
    assert list_words("notes") == ("note",)
    assert list_words("My To-Do List") == list_words("the todo lists") == list_words("to-dos") == ("todo",)
    assert list_words("uni_work") == ("uni", "work")


def test_a_word_of_one_or_two_letters_never_fits_by_its_start(make_lists):
    lists = make_lists([_list("notepad"), _list("uni-work")])
    assert _found(lists, "no") == _found(lists, "un") == "I haven't got a list called that."
    assert _found(lists, "not") == "notepad"
    # Nor by being one word of a thing's - "a" isn't "a pear".
    lists = make_lists(items={"todo.shopping_list": ["a pear", "tv stand"]})
    assert lists.tick_off("a", "shopping")["message"] == "I can't see a on your shopping list."
    assert lists.tick_off("tv", "shopping")["message"] == "I can't see tv on your shopping list."
    assert lists.tick_off("stand", "shopping")["message"] == "Ticked off tv stand on your shopping list."


def test_a_lists_other_name_finds_it(make_lists):
    lists = make_lists([_list("Shopping list", aliases=["groceries"]), _list("todo")])
    assert _found(lists, "groceries") == "Shopping list"
    assert _found(lists, "my groceries list") == "Shopping list"


def test_two_lists_that_fit_are_asked_about(make_lists):
    assert _found(make_lists([_list("Shopping"), _list("Shopping list", "todo.shopping_2")]),
                  "shopping") == "Which list, Shopping or Shopping list?"
    # A name said in full is that list, whatever else it begins...
    assert _found(make_lists([_list("uni"), _list("uni-work")]), "uni") == "uni"
    # ...and the start of a name only counts for one list.
    assert _found(make_lists([_list("notepad"), _list("notebook")]), "note") == "I haven't got a list called that."


def test_a_list_that_isnt_there_says_so(make_lists):
    lists = make_lists()
    assert lists.read("my garden list") == {"success": False, "message": "I haven't got a list called that."}
    assert lists.home.added == [] and lists.home.ticked == []


def test_with_no_list_named_its_the_main_one(make_lists):
    lists = make_lists()
    assert lists.main()["name"] == "todo"
    assert _found(lists, None) == _found(lists, "my list") == _found(lists, "list") == "todo"

    lists.choices.change(lambda state: state.update(main="todo.shopping_list"))
    assert _found(lists, None) == "Shopping list"

    # Once the chosen one has gone, it's todo again.
    lists.home.lists = [found for found in lists.home.lists if found["name"] != "Shopping list"]
    assert _found(lists, None) == "todo"

    # Without todo, it's the only list there is - and with several, there's none to go by,
    # so Nova asks which.
    assert make_lists([_list("notepad")]).main()["name"] == "notepad"
    several = make_lists([_list("notepad"), _list("uni-work")])
    assert several.main() is None
    assert _found(several, None) == "Which list, notepad or uni-work?"
    assert _found(make_lists([]), None) == "Home Assistant hasn't got any lists."


def test_added_is_only_said_once_home_assistant_took_it(make_lists):
    lists = make_lists()
    assert lists.add(["milk"], "shopping") == {"success": True, "message": "Added milk to your shopping list."}
    assert lists.add(["eggs", "milk", "bread"], "shopping")["message"] == (
        "Added eggs, milk and bread to your shopping list.")

    lists = make_lists()
    lists.home.add_fails["milk"] = _server_error()
    assert lists.add(["milk"], "shopping") == {"success": False,
                                               "message": "I couldn't add that to your shopping list."}
    # A failure stops there, so bread isn't tried.
    assert lists.add(["eggs", "milk", "bread"], "shopping") == {
        "success": False, "message": "Added eggs to your shopping list, but I couldn't add milk or bread."}
    assert lists.home.added == [("todo.shopping_list", "eggs")]


def test_a_token_turned_down_says_so(make_lists):
    lists = make_lists()
    response = requests.Response()
    response.status_code = 401
    lists.home.add_fails["milk"] = requests.HTTPError("401", response=response)
    assert "HA_TOKEN in the .env file needs checking" in lists.add(["milk"], "shopping")["message"]


def test_no_answer_in_time_says_nova_cant_tell_if_it_was_done(make_lists):
    lists = make_lists(items={"todo.shopping_list": ["milk"]})
    lists.home.add_fails["milk"] = requests.Timeout("slow")
    assert lists.add(["milk"], "shopping") == {"success": False, "message": (
        "Home Assistant didn't answer in time, so I can't tell if milk was added to your shopping list.")}
    assert lists.add(["eggs", "milk", "bread"], "shopping") == {"success": False, "message": (
        "Added eggs to your shopping list, but Home Assistant didn't answer in time, so I can't tell if milk "
        "was added, and I didn't add bread.")}
    lists.home.tick_fails = requests.Timeout("slow")
    assert lists.tick_off("milk", "shopping") == {"success": False, "message": (
        "Home Assistant didn't answer in time, so I can't tell if milk was ticked off your shopping list.")}
    # Reading the lists unanswered changed nothing, so it's said as Home Assistant being down.
    lists.home.failure = requests.Timeout("slow")
    assert lists.tick_off("milk", None) == {"success": False, "message": CANT_REACH}


def test_nothing_sent_says_it_wasnt_done(make_lists):
    lists = make_lists(items={"todo.shopping_list": ["milk"]})
    lists.home.add_fails["milk"] = requests.ConnectionError("refused")
    assert lists.add(["milk"], "shopping")["message"] == "I couldn't add that to your shopping list."
    lists.home.tick_fails = requests.ConnectionError("refused")
    assert lists.tick_off("milk", "shopping") == {"success": False, "message": CANT_REACH}
    assert lists.home.ticked == []


def test_each_says_it_cant_reach_home_assistant_while_its_down(make_lists):
    lists = make_lists()
    lists.home.failure = requests.ConnectionError("refused")
    for result in (lists.add(["milk"], "shopping"), lists.read("shopping"), lists.tick_off("milk", None)):
        assert result == {"success": False, "message": CANT_REACH}


def test_a_list_is_read_up_to_ten_things(make_lists):
    things = ["milk", "eggs", "bread", "rice", "tea", "jam", "oats", "soap", "foil", "salt", "honey", "flour"]
    lists = make_lists(items={"todo.shopping_list": things, "todo.notepad": things[:3]})
    assert lists.read("shopping") == {"success": True, "message": (
        "Your shopping list has milk, eggs, bread, rice, tea, jam, oats, soap, foil, salt, and 2 more.")}
    assert lists.read("notes")["message"] == "Your notepad list has milk, eggs and bread."
    assert lists.read(None)["message"] == "Your todo list is empty."


def test_two_things_that_fit_are_asked_about(make_lists):
    lists = make_lists(items={"todo.shopping_list": ["oat milk", "milk shake", "Eggs"]})
    assert lists.tick_off("milk", "shopping") == {"success": False, "message": "Which one, oat milk or milk shake?"}
    assert lists.home.ticked == []
    # Case, punctuation, plurals and a leading "some" don't matter.
    assert lists.tick_off("some egg!", "shopping") == {"success": True,
                                                       "message": "Ticked off Eggs on your shopping list."}

    # "es" plurals count for things too.
    lists = make_lists(items={"todo.shopping_list": ["glasses", "box"]})
    assert lists.tick_off("glass", "shopping")["message"] == "Ticked off glasses on your shopping list."
    assert lists.tick_off("boxes", "shopping")["message"] == "Ticked off box on your shopping list."

    # The exact fit wins over looser ones.
    lists = make_lists(items={"todo.shopping_list": ["oat milk", "milk shake", "milk"]})
    assert lists.tick_off("the milk", "shopping")["message"] == "Ticked off milk on your shopping list."
    assert lists.home.ticked == [("todo.shopping_list", "todo.shopping_list-2")]


def test_with_no_list_named_every_list_is_looked_through(make_lists):
    lists = make_lists(items={"todo.todo": ["ring the bank"], "todo.shopping_list": ["milk"]})
    assert lists.tick_off("milk", None)["message"] == "Ticked off milk on your shopping list."
    assert lists.home.ticked == [("todo.shopping_list", "todo.shopping_list-0")]
    assert lists.tick_off("cheese", "my shopping list") == {"success": False,
                                                            "message": "I can't see cheese on your shopping list."}
    # Without the word "list", it may not be about the list at all - "take Friday off work".
    assert lists.tick_off("cheese", "shopping")["not_understood"] is True
    # The same thing on two lists asks which list.
    lists = make_lists(items={"todo.todo": ["milk"], "todo.shopping_list": ["milk"]})
    assert lists.tick_off("milk", None)["message"] == "Which list, Shopping list or todo?"


def test_words_naming_no_list_go_to_claude(make_lists):
    lists = make_lists(items={"todo.shopping_list": ["milk"]})
    assert lists.add(["the dentist"], "my calendar") == {
        "success": False, "message": "I haven't got a list called that.", "not_understood": True}
    assert lists.home.added == []
    # Nothing on any list fitting, with no list said, goes to Claude too...
    assert lists.tick_off("work tomorrow", None) == {
        "success": False, "message": "I can't see work tomorrow on your lists.", "not_understood": True}
    assert lists.tick_off("milk", "my calendar") == {
        "success": False, "message": "I haven't got a list called that.", "not_understood": True}
    # ...but a list asked for by name is still one Nova hasn't got, and "my list" is still a list.
    assert "not_understood" not in lists.read("my garden list")
    assert lists.tick_off("cheese", "my list") == {"success": False, "message": "I can't see cheese on your lists."}
