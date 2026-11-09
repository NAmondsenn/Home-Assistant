"""
Displays Test
Checks the brain keeps the clock displays and their keys: a key is shown once and
kept only as its hash, and a renewed or removed one stops working.
"""

import json
import os
import stat

import pytest

from voice_assistant.displays import DEFAULT_BRIGHTNESS, DEFAULT_HOME_AFTER, MOST_DISPLAYS, Displays


def test_add_lists_the_clock_without_its_key(tmp_path):
    path = str(tmp_path / "displays.json")
    displays = Displays(path)

    entry, key = displays.add("Desk", "pi")

    listed = displays.listed()
    assert len(listed) == 1
    assert listed[0]["name"] == "Desk"
    assert listed[0]["satellite"] == "pi"
    assert listed[0]["brightness"] == DEFAULT_BRIGHTNESS == 70
    assert listed[0]["home_after"] == DEFAULT_HOME_AFTER == 60
    assert listed[0]["id"] == entry["id"]
    assert set(listed[0]) == {"id", "name", "satellite", "brightness", "home_after"}
    assert key not in json.dumps(displays.listed())
    with open(path) as file:
        assert key not in file.read()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_find_knows_only_the_current_key(tmp_path):
    displays = Displays(str(tmp_path / "displays.json"))
    entry, key = displays.add("Desk", None)

    assert displays.find(key)["id"] == entry["id"]
    assert displays.find("wrong") is None
    assert displays.find("") is None

    new_key = displays.renew(entry["id"])

    assert new_key != key
    assert displays.find(key) is None
    assert displays.find(new_key)["id"] == entry["id"]
    # What find returns is what the page is shown, so it never carries the hash.
    assert "key" not in json.dumps(displays.find(new_key))


def test_remove_stops_the_key(tmp_path):
    displays = Displays(str(tmp_path / "displays.json"))
    entry, key = displays.add("Desk", "pi")

    displays.remove(entry["id"])

    assert displays.find(key) is None
    assert displays.listed() == []
    with pytest.raises(ValueError):
        displays.remove(entry["id"])


def test_change_keeps_to_its_limits(tmp_path):
    displays = Displays(str(tmp_path / "displays.json"))
    entry, _ = displays.add("Desk", "pi")

    changed = displays.change(entry["id"], {"brightness": 40, "home_after": 120, "satellite": None})

    assert changed["brightness"] == 40 and changed["home_after"] == 120 and changed["satellite"] is None
    assert displays.listed()[0]["brightness"] == 40
    for bad in ({"brightness": 101}, {"brightness": -1}, {"brightness": True}, {"home_after": 5},
                {"home_after": 3601}, {"home_after": "60"}, {"name": "x"}):
        with pytest.raises(ValueError):
            displays.change(entry["id"], bad)
    with pytest.raises(ValueError):
        displays.change("nobody", {"brightness": 40})
    # A refused change leaves the clock as it was.
    assert displays.listed()[0]["brightness"] == 40 and displays.listed()[0]["home_after"] == 120


def test_kept_across_a_restart(tmp_path):
    path = str(tmp_path / "displays.json")
    entry, key = Displays(path).add("Desk", "pi")

    again = Displays(path)

    assert again.find(key)["id"] == entry["id"]
    assert again.listed()[0]["name"] == "Desk"


def test_names_must_be_sensible(tmp_path):
    displays = Displays(str(tmp_path / "displays.json"))

    for bad in ("", "   ", "x" * 40):
        with pytest.raises(ValueError):
            displays.add(bad, None)
    assert displays.listed() == []

    for number in range(MOST_DISPLAYS):
        displays.add(f"Clock {number}", None)
    with pytest.raises(ValueError):
        displays.add("One too many", None)


def test_clocks_with_the_same_name_get_their_own_ids(tmp_path):
    displays = Displays(str(tmp_path / "displays.json"))

    first, first_key = displays.add("Desk", None)
    second, second_key = displays.add("Desk", None)

    assert first["id"] != second["id"]
    assert displays.find(first_key)["id"] == first["id"]
    assert displays.find(second_key)["id"] == second["id"]
    # A name which is no letters or digits still makes an id.
    assert displays.add("!!!", None)[0]["id"]


def test_a_broken_file_starts_no_clocks_and_is_left_alone(tmp_path):
    path = str(tmp_path / "displays.json")
    for broken in ("{not json", '{"a": 1}'):
        with open(path, "w") as file:
            file.write(broken)

        displays = Displays(path)

        assert displays.listed() == []
        assert displays.find("anything") is None
        for change in (lambda: displays.add("Desk", None), lambda: displays.renew("desk"),
                       lambda: displays.change("desk", {"brightness": 40}), lambda: displays.remove("desk")):
            with pytest.raises(OSError):
                change()
        with open(path) as file:
            assert file.read() == broken


def test_a_hand_edited_clock_makes_the_file_unreadable(tmp_path):
    path = str(tmp_path / "displays.json")
    good = {"id": "desk", "name": "Desk", "satellite": "pi", "brightness": 70, "home_after": 60,
            "key_hash": "0" * 64}
    for bad in ({**good, "key_hash": 5}, {**good, "key_hash": "ABC"}, {**good, "key_hash": "\u00e9" * 64},
                {**good, "brightness": 200}, {**good, "home_after": "60"}, {**good, "satellite": 3},
                {**good, "name": None}, {key: value for key, value in good.items() if key != "id"},
                good, "desk"):
        entries = [bad] if bad is not good else [good, good]
        with open(path, "w") as file:
            json.dump(entries, file)

        displays = Displays(path)

        assert displays.listed() == []
        assert displays.find("anything") is None
        with pytest.raises(OSError):
            displays.add("Desk", None)

    with open(path, "w") as file:
        json.dump([good], file)
    assert Displays(path).listed()[0]["id"] == "desk"


def test_changes_must_be_a_set_of_fields(tmp_path):
    displays = Displays(str(tmp_path / "displays.json"))
    entry, _ = displays.add("Desk", None)

    for bad in (None, 40, "brightness", [("brightness", 40)]):
        with pytest.raises(ValueError):
            displays.change(entry["id"], bad)
