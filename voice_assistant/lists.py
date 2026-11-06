"""
Lists Module
Home Assistant's own to-do lists, found by the user's words, and added to, read and ticked
off - decision 12 of docs/decisions/0008.

Every todo.* list is found afresh each time, by its name in Home Assistant or one of its
other names, so a list added later works by itself. The words are matched forgivingly: case
doesn't matter, nor does the word "list", spaces, hyphens and underscores are all alike,
"to do", "to-do" and "todo" are one word, and plurals count. So "notes" is notepad, "uni
work" is uni-work and "shopping" is Shopping list. A word a name starts with counts too, if
only one list's name does. If two lists fit, Nova asks which, and with no list named, it's
the main one - todo, unless the user has chosen another.

Reading says up to ten things still to do, then how many more. A thing is ticked off, never
deleted, so it stays in Home Assistant where it can be put back; if two things fit, Nova
asks which.
"""

import logging
import re
from typing import Dict, FrozenSet, List, Optional, Tuple

import requests

from voice_assistant.choices import ChoiceStore
from voice_assistant.home_assistant import HomeAssistant, NotAllowed

# Logger setup
logger = logging.getLogger(__name__)

# The most things read from a list, before saying how many more there are - ten is about
# as many as can be taken in by ear.
MOST_ITEMS = 10

NO_SUCH_LIST = "I haven't got a list called that."

# Said of a change Home Assistant was sent but didn't answer, so may have made.
NO_ANSWER = "Home Assistant didn't answer in time"

# The words which only say whose or which thing it is - "tick off the milk" is milk.
ITEM_LEADS = ("the", "some", "my")

# The fewest letters a word needs to fit by being the start of a list's name, or one word
# of a thing's - "a" would otherwise fit "a pear", and "no" notepad.
LOOSE_LETTERS = 3

# The endings whose plural takes "es" - "glasses" is glass and "boxes" is box.
ES_PLURALS = ("sses", "xes", "ches", "shes", "zes")


def _words(text: str) -> List[str]:
    """Text in lower case, as words without punctuation, e.g. "Uni-work's" as ["uni", "works"]."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("'", "")).split()


def _singular(word: str) -> str:
    """A word with a last "s" taken off, so "notes" and "note" are the same."""
    return word[:-1] if len(word) > 1 and word.endswith("s") else word


def list_words(name: str) -> Tuple[str, ...]:
    """
    The words of a list's name, or of what was said for one, as they're matched.

    Args:
        name: E.g. "My To-Do List" or "uni_work".

    Returns:
        The words, e.g. ("todo",) or ("uni", "work") - none for "my list".
    """
    words = _words(name)
    if words and words[0] in ("my", "the"):
        words = words[1:]
    # Singular first, so "to-dos" is todo too.
    joined = []
    for word in map(_singular, words):
        if word == "do" and joined and joined[-1] == "to":
            joined[-1] = "todo"
        else:
            joined.append(word)
    return tuple(word for word in joined if word != "list")


def _begins(said: Tuple[str, ...], name: Tuple[str, ...]) -> bool:
    """Whether a name's words begin with those said, the last of them perhaps cut short - "note" for "notepad"."""
    if not said or len(said) > len(name) or len(said[-1]) < LOOSE_LETTERS:
        return False
    return said[:-1] == name[:len(said) - 1] and name[len(said) - 1].startswith(said[-1])


def match_list(said: str, lists: List[Dict]) -> List[Dict]:
    """
    The lists what was said could mean.

    Args:
        said: E.g. "my shopping list".
        lists: The lists, as HomeAssistant.todo_lists gives them.

    Returns:
        Those whose name, or one of whose other names, has the same words - so an exact fit
        wins, and "uni" is uni even with uni-work there too. If none, the one whose name
        begins with them - but only if just one list's does. Empty if none fit. Nova asks
        only when two or more fit exactly.
    """
    words = list_words(said)
    if not words:
        return []
    same = [found for found in lists
            if any(list_words(name) == words for name in [found["name"], *found.get("aliases", ())])]
    if same:
        return same
    begun = [found for found in lists if _begins(words, list_words(found["name"]))]
    return begun if len(begun) == 1 else []


def _item_singulars(word: str) -> FrozenSet[str]:
    """
    A thing's word without its plural - "glasses" and "glass" are both glass, and "eggs" egg.

    Args:
        word: One word of a thing, in lower case.

    Returns:
        The word it could be a plural of: two for an ending which takes "es", as "peaches"
        is peach but "quiches" is quiche, and the word alone can't say which.
    """
    if word.endswith(ES_PLURALS):
        return frozenset((word[:-2], word[:-1]))
    return frozenset((word if word.endswith("ss") else _singular(word),))


def _item_words(text: str) -> Tuple[FrozenSet[str], ...]:
    """A thing's words as they're matched, each as _item_singulars has it - "Some Eggs!" is ({"egg"},)."""
    words = _words(text)
    if words and words[0] in ITEM_LEADS:
        words = words[1:]
    return tuple(_item_singulars(word) for word in words)


def _same_words(one: Tuple[FrozenSet[str], ...], other: Tuple[FrozenSet[str], ...]) -> bool:
    """Whether two things have the same words, each word fitting if any of its singulars does."""
    return len(one) == len(other) and all(here & there for here, there in zip(one, other))


def _has_words(words: Tuple[FrozenSet[str], ...], thing: Tuple[FrozenSet[str], ...]) -> bool:
    """Whether a thing has every one of these words, among its own."""
    return all(any(word & each for each in thing) for word in words)


def match_item(said: str, items: List[Dict]) -> List[Dict]:
    """
    The things on a list what was said could mean.

    Args:
        said: E.g. "the milk".
        items: The things, each with "summary".

    Returns:
        Those with the same words, ignoring case, punctuation, plurals and a leading "the",
        "some" or "my" - so an exact fit wins, and "milk" is milk even with oat milk there
        too. If none, those with every word said, so "milk" is oat milk - unless all the
        words said are shorter than LOOSE_LETTERS. Nova asks only when nothing fits exactly
        and two or more fit more loosely.
    """
    words = _item_words(said)
    if not words:
        return []
    same = [item for item in items if _same_words(_item_words(item["summary"]), words)]
    if same or all(max(map(len, word)) < LOOSE_LETTERS for word in words):
        return same
    return [item for item in items if _has_words(words, _item_words(item["summary"]))]


def _joined(things: List[str], last: str = "and") -> str:
    """Things as said in a sentence, e.g. "eggs, milk and bread"."""
    return things[0] if len(things) == 1 else f"{', '.join(things[:-1])} {last} {things[-1]}"


def _list_said(name: str) -> str:
    """A list as it's said after "your", e.g. "shopping list" or "notepad list"."""
    name = name.lower()
    return name if name.endswith("list") else f"{name} list"


def say_items(name: str, items: List[Dict]) -> str:
    """
    A list's things still to do, as a sentence to say.

    Args:
        name: The list's name, e.g. "Shopping list".
        items: Its things, each with "summary".

    Returns:
        E.g. "Your shopping list has milk, eggs and bread." - the first MOST_ITEMS, then how
        many more - or "Your shopping list is empty.".
    """
    if not items:
        return f"Your {_list_said(name)} is empty."
    said = [item["summary"] for item in items[:MOST_ITEMS]]
    if len(items) > MOST_ITEMS:
        return f"Your {_list_said(name)} has {', '.join(said)}, and {len(items) - MOST_ITEMS} more."
    return f"Your {_list_said(name)} has {_joined(said)}."


def names_list(said: Optional[str]) -> bool:
    """Whether what was said for a list has the word "list" in it - "my calendar" hasn't."""
    return bool(said) and bool(re.search(r"\blists?\b", said.lower()))


class Lists:
    """Home Assistant's to-do lists, as the user names them."""

    def __init__(self, home: HomeAssistant, choices: ChoiceStore):
        """
        Args:
            home: The Home Assistant to ask.
            choices: The main list's entity id, under "main", for when no list is named -
                None until one is chosen.
        """
        self.home = home
        self.choices = choices

    def main(self) -> Optional[Dict]:
        """
        The list meant when none is named.

        Returns:
            The chosen main list if it's still there, else todo, else the only list there
            is - or None.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If its states can't be read.
        """
        return self._main(self.home.todo_lists())

    def _main(self, lists: List[Dict]) -> Optional[Dict]:
        """The main list among these, as main says."""
        chosen = self.choices.get("main")
        for found in lists:
            if found["id"] == chosen:
                return found
        for found in lists:
            if list_words(found["name"]) == ("todo",):
                return found
        return lists[0] if len(lists) == 1 else None

    def overview(self) -> List[Dict]:
        """
        Every list Home Assistant has, for the settings page.

        Returns:
            Each of todo_lists, with "main": whether it's the one meant when none is named.
            A chosen list which has gone is still kept, but todo or the only list is main
            meanwhile - as _main says - and the chosen one is again when it's back.

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If its states can't be read.
        """
        found = self.home.todo_lists()
        main = self._main(found)
        return [{**each, "main": main is not None and each["id"] == main["id"]} for each in found]

    def choose_main(self, list_id: str, seen: Optional[List[Dict]] = None):
        """
        Chooses the list meant when none is named, from the next request.

        Args:
            list_id: The list's entity id, which Home Assistant has to be showing.
            seen: What overview gave just before, if the caller read it already - so it can
                read without holding a lock the save needs.

        Raises:
            ValueError: If Home Assistant isn't showing that list.
            OSError: If the choice can't be saved.
            requests.RequestException: If Home Assistant couldn't be asked.
        """
        if seen is None:
            seen = self.overview()
        if not any(each["id"] == list_id for each in seen):
            raise ValueError("Home Assistant isn't showing that list just now.")
        self.choices.change(lambda state: state.update(main=list_id))

    def find(self, said: Optional[str]) -> Tuple[Optional[Dict], Optional[str]]:
        """
        The list meant by what was said.

        Args:
            said: The list as it was said, e.g. "my shopping list", or None if none was.

        Returns:
            The list and None - or None and a sentence to say instead, e.g. "Which list,
            uni or uni-work?".

        Raises:
            requests.RequestException: If Home Assistant couldn't be asked.
            ValueError: If its states can't be read.
        """
        lists = self.home.todo_lists()
        if not said or not list_words(said):
            found = self._main(lists)
            if found:
                return found, None
            if not lists:
                return None, "Home Assistant hasn't got any lists."
            return None, f"Which list, {_joined([each['name'] for each in lists], 'or')}?"
        fits = match_list(said, lists)
        if len(fits) == 1:
            return fits[0], None
        if fits:
            return None, f"Which list, {_joined([found['name'] for found in fits], 'or')}?"
        return None, NO_SUCH_LIST

    def _not_found(self, said: Optional[str], sentence: str) -> Dict:
        """What's said when no one list fits - and when none does, for words naming no list, it goes to Claude."""
        result = {"success": False, "message": sentence}
        if sentence == NO_SUCH_LIST and not names_list(said):
            result["not_understood"] = True
        return result

    def add(self, items: List[str], said: Optional[str]) -> Dict:
        """
        Adds things to a list, saying each added only once Home Assistant has taken it.

        Args:
            items: The things, e.g. ["eggs", "milk", "bread"].
            said: The list as it was said, or None for the main one.

        Returns:
            Dict with 'success' and a spoken 'message', and 'not_understood' for words
            which name no list.
        """
        try:
            found, sentence = self.find(said)
        except (requests.RequestException, ValueError) as e:
            return _failed("look at the lists", e)
        if not found:
            return self._not_found(said, sentence)
        where = f"your {_list_said(found['name'])}"
        if not items:
            return {"success": False, "message": f"I couldn't add that to {where}."}
        added = []
        for item in items:
            try:
                self.home.add_item(found["id"], item)
            except (requests.RequestException, NotAllowed) as e:
                # A failure stops there - the next would likely fail too, and what wasn't
                # added is all said, so the user knows what to add again.
                logger.warning(f"Could not add a thing to {found['id']}: {e}")
                return {"success": False, "message": _not_added(added, items[len(added):], where, e)}
            added.append(item)
        return {"success": True, "message": f"Added {_joined(added)} to {where}."}

    def read(self, said: Optional[str]) -> Dict:
        """
        Says what's still to do on a list.

        Args:
            said: The list as it was said, or None for the main one.

        Returns:
            Dict with 'success' and a spoken 'message', and 'not_understood' for words
            which name no list.
        """
        try:
            found, sentence = self.find(said)
            if not found:
                return self._not_found(said, sentence)
            items = self.home.list_items(found["id"])
        except (requests.RequestException, ValueError) as e:
            return _failed("read a list", e)
        return {"success": True, "message": say_items(found["name"], items)}

    def tick_off(self, item: str, said: Optional[str], exact: bool = False) -> Dict:
        """
        Ticks a thing off a list - marking it done, never deleting it.

        Args:
            item: The thing as it was said, e.g. "the milk".
            said: The list as it was said, or None to look through every list.
            exact: Whether only a thing with the same words will do - for "I've got the
                flu", a sentence about the world, which mustn't tick off "Flu jab".

        Returns:
            Dict with 'success' and a spoken 'message', and 'not_understood' for words
            which name no list - or, without the word "list" in them, fit nothing on it.
        """
        named = bool(said and list_words(said))
        try:
            if named:
                found, sentence = self.find(said)
                if not found:
                    return self._not_found(said, sentence)
                lists = [found]
            else:
                lists = self.home.todo_lists()
            things = [{**thing, "list": found} for found in lists for thing in self.home.list_items(found["id"])]
            fits = match_item(item, things)
            if exact:
                fits = [thing for thing in fits if _same_words(_item_words(thing["summary"]), _item_words(item))]
            if not fits:
                where = f"your {_list_said(lists[0]['name'])}" if named else "your lists"
                result = {"success": False, "message": f"I can't see {item} on {where}."}
                # "I've got work tomorrow" sounds like ticking work off, but fits nothing - and
                # "take Friday off work", with a Work list, isn't about the list.
                if not names_list(said):
                    result["not_understood"] = True
                return result
            # By their words, so "milk" and "Milk" are one thing, wherever it's on.
            summaries = list({_item_words(thing["summary"]): thing["summary"] for thing in fits}.values())
            on = list({thing["list"]["id"]: thing["list"]["name"] for thing in fits}.values())
            if len(summaries) > 1:
                return {"success": False, "message": f"Which one, {_joined(summaries, 'or')}?"}
            if len(on) > 1:
                return {"success": False, "message": f"Which list, {_joined(on, 'or')}?"}
            # The same thing twice on one list is still the one thing, so the first is ticked off.
            thing = fits[0]
            try:
                self.home.tick_off(thing["list"]["id"], thing["uid"])
            except requests.Timeout as e:
                # Sent, but unanswered - so it may well have been ticked off.
                logger.warning(f"Home Assistant didn't answer in time ticking something off: {e}")
                return {"success": False, "message": f"{NO_ANSWER}, so I can't tell if {thing['summary']} was "
                                                     f"ticked off your {_list_said(thing['list']['name'])}."}
        except (requests.RequestException, ValueError) as e:
            return _failed("tick something off", e)
        return {"success": True,
                "message": f"Ticked off {thing['summary']} on your {_list_said(thing['list']['name'])}."}


def _not_added(added: List[str], left: List[str], where: str, error: Exception) -> str:
    """
    What's said when adding a thing failed, e.g. "Added eggs to your shopping list, but I
    couldn't add milk or bread.".

    Args:
        added: The things Home Assistant took before it.
        left: The one which failed, then those not tried.
        where: The list as it's said, e.g. "your shopping list".
        error: Why it failed.
    """
    if isinstance(error, requests.Timeout):
        # Sent, but unanswered - so it may well have been added.
        unsure = f"{NO_ANSWER}, so I can't tell if {left[0]} was added"
        if not added:
            return f"{unsure} to {where}."
        rest = f", and I didn't add {_joined(left[1:])}" if left[1:] else ""
        return f"Added {_joined(added)} to {where}, but {unsure}{rest}."
    if not added:
        if getattr(getattr(error, "response", None), "status_code", None) == 401:
            return HomeAssistant._failure_message(error)
        return f"I couldn't add that to {where}."
    return f"Added {_joined(added)} to {where}, but I couldn't add {_joined(left, 'or')}."


def _failed(doing: str, error: Exception) -> Dict:
    """What's said when Home Assistant couldn't be asked - CANT_REACH while it's down."""
    logger.warning(f"Could not {doing} in Home Assistant: {error}")
    return {"success": False, "message": HomeAssistant._failure_message(error)}
