"""Tests for the battery rules - what kind a device is, whether it charges, and its name."""

import json
import logging
import threading
import time

import pytest
import requests

from voice_assistant import storage

from voice_assistant.config import MARKS, Config, ConfigError
from voice_assistant.batteries import (
    Battery,
    BatteryChoices,
    BatteryWatcher,
    charging_from,
    find,
    is_phone,
    kind_of,
    said,
    spoken_name,
)

HOUR = 3600.0
NOW = 1_000_000_000.0


def _found(name, level, device, maker, model, charging, changed=NOW, app=True):
    return {
        "id": f"sensor.{name}_battery_level",
        "name": name,
        "level": level,
        "changed": changed,
        "device": device,
        "maker": maker,
        "model": model,
        "charging": charging,
        "app": app,
    }


def _todays_three():
    return [
        Battery.from_home(_found("macbook-pro", 72.0, "d1", "Apple", "Mac16,8", "Not Charging")),
        Battery.from_home(_found("s25-ultra", 40.0, "d2", "samsung", "SM-S938B", "discharging")),
        Battery.from_home(_found("tab-s11-ultra", 88.0, "d3", "samsung", "SM-X930", "Not Charging")),
    ]


def test_todays_devices_are_sorted_into_kinds():
    assert kind_of("samsung", "SM-S938B", True) == "mobile" and is_phone("samsung", "SM-S938B")
    assert kind_of("samsung", "SM-X930", True) == "mobile" and not is_phone("samsung", "SM-X930")
    assert kind_of("Apple", "Mac16,8", True) == "other"
    assert kind_of(None, None, False) == "other"
    assert kind_of("Apple", "iPhone16,2", True) == "mobile" and is_phone("Apple", "iPhone16,2")
    assert kind_of("Apple", "iPad14,1", True) == "mobile" and not is_phone("Apple", "iPad14,1")


def test_an_imac_is_a_mac_too():
    # Apple's iMac models start with "iMac", not "Mac" - an iMac on the app reporting a UPS battery is a computer.
    assert kind_of("Apple", "iMac21,1", True) == "other"
    imac = Battery.from_home(_found("imac", 50.0, "d7", "Apple", "iMac21,1", None))
    assert (imac.kind, imac.phone, imac.computer) == ("other", False, True)
    assert find([imac], "computer") == [imac]
    # Another maker's imac is not Apple's.
    assert not Battery.from_home(_found("imac", 50.0, "d7", "Acme", "iMac21,1", None)).computer


def test_only_a_model_that_shows_a_phone_is_a_phone():
    # A device Nova can't place counts as a tablet, so a wrong guess costs a duplicate warning, never a lost one.
    assert kind_of("samsung", "SM-P610", True) == "mobile" and not is_phone("samsung", "SM-P610")
    assert not is_phone("samsung", "SM-R960")
    assert kind_of("Google", "Pixel 8", True) == "mobile" and not is_phone("Google", "Pixel 8")
    for model in ("SM-S938B", "SM-G991B", "SM-A546B", "SM-F946B", "SM-N986B", "SM-M536B", "SM-E256B"):
        assert is_phone("samsung", model), model


def test_what_is_on_the_app_is_mobile_and_the_rest_is_other():
    assert kind_of("Xiaomi", "LYWSD03MMC", False) == "other"
    assert kind_of("Lenovo", "TB320FC", True) == "mobile" and not is_phone("Lenovo", "TB320FC")
    lenovo = Battery.from_home(_found("lenovo-tab", 50.0, "d4", "Lenovo", "TB320FC", "on"))
    assert (lenovo.kind, lenovo.phone, lenovo.computer) == ("mobile", False, False)
    xiaomi = Battery.from_home(_found("sensor", 50.0, "d5", "Xiaomi", "LYWSD03MMC", None, app=False))
    assert (xiaomi.kind, xiaomi.phone, xiaomi.computer) == ("other", False, False)


def test_charging_is_read_from_each_apps_words():
    for raw in ("charging", "Charging", "full", "on"):
        assert charging_from(raw) is True, raw
    for raw in ("discharging", "not_charging", "Not Charging", "off"):
        assert charging_from(raw) is False, raw
    for raw in (None, "unknown", "unavailable"):
        assert charging_from(raw) is None, raw


def test_names_are_said_as_words():
    assert spoken_name("macbook-pro") == "MacBook Pro"
    assert spoken_name("s25-ultra") == "S25 Ultra"
    assert spoken_name("tab-s11-ultra") == "Tab S11 Ultra"


def test_a_name_the_user_typed_keeps_its_own_capitals():
    assert spoken_name("TV remote") == "TV Remote"
    assert spoken_name("AirPods") == "AirPods"
    assert spoken_name("Nathaniel's Kindle") == "Nathaniel's Kindle"


def test_asking_finds_the_device_meant():
    three = _todays_three()
    mac, phone, tablet = three

    for asked in ("mac", "macbook", "laptop", "computer", "my mac"):
        assert find(three, asked) == [mac], asked
    assert find(three, "phone") == [phone]
    assert find(three, "tablet") == [tablet]
    assert find(three, "tab") == [tablet]
    assert find(three, "") == three
    assert find(three, "all") == three
    assert find(three, "toaster") == []


def test_a_possessive_or_a_plural_finds_the_device_meant():
    three = _todays_three()
    mac, phone, tablet = three
    assert find(three, "mac's") == [mac]
    assert find(three, "macbook's") == [mac]
    assert find(three, "my phone's") == [phone]
    assert find(three, "phones") == [phone]
    assert find(three, "tablets") == [tablet]
    assert find(three, "laptops") == [mac]
    assert find(three, "macs") == [mac]
    # Nothing is left of a bare "'s", and an empty word would match every name.
    assert find(three, "'s") == []


def test_a_full_name_finds_only_that_device():
    mac, phone, tablet = _todays_three()
    assert find([mac, phone, tablet], "s25 ultra") == [phone]
    assert find([mac, phone, tablet], "tab s11 ultra") == [tablet]


def test_a_remote_is_not_a_laptop():
    remote = Battery.from_home(_found("remote", 61.0, "d6", "Acme", "R1", "on", app=False))
    mac = _todays_three()[0]
    for asked in ("mac", "laptop", "computer"):
        assert find([mac, remote], asked) == [mac], asked


def test_a_fresh_level_is_said_with_whether_its_charging():
    mac = _todays_three()[0]
    assert said(mac, NOW + 60, HOUR) == "Your MacBook Pro's on 72%, and not charging."
    charging = Battery.from_home(_found("macbook-pro", 72.0, "d1", "Apple", "Mac16,8", "Charging"))
    assert said(charging, NOW, HOUR) == "Your MacBook Pro's on 72%, and charging."
    unknown = Battery.from_home(_found("macbook-pro", 72.0, "d1", "Apple", "Mac16,8", None))
    assert said(unknown, NOW, HOUR) == "Your MacBook Pro's on 72%."


def test_an_old_level_says_how_old():
    tablet = _todays_three()[2]
    assert said(tablet, NOW + 3 * 86400, HOUR) == "Your Tab S11 Ultra was on 88%, 3 days ago."


def test_a_battery_with_no_device_is_something_else_and_never_charging():
    # Whatever else is claimed for it - on the app, a Mac, charging - with no device there is nothing to read a
    # kind or a charging sensor from.
    found = _found("remote", 61.0, None, "Apple", "Mac16,8", "charging", app=True)
    battery = Battery.from_home(found)
    assert battery.kind == "other"
    assert battery.phone is False
    assert battery.computer is False
    assert battery.charging is None
    assert battery.device is None



def test_asking_with_no_batteries_finds_none():
    assert find([], "phone") == []
    assert find([], "") == []


def _choices(tmp_path):
    return BatteryChoices(str(tmp_path / "batteries.json"), mobile_marks=[5], other_marks=[5, 15])


def _mac(level, charging=False):
    return Battery.from_home(_found("macbook-pro", level, "d1", "Apple", "Mac16,8", "Charging" if charging else "Not Charging"))


def _phone(level, charging=False):
    return Battery.from_home(_found("s25-ultra", level, "d2", "samsung", "SM-S938B", "Charging" if charging else "discharging"))


def _tablet(level):
    return Battery.from_home(_found("tab-s11-ultra", level, "d3", "samsung", "SM-X930", "Not Charging"))


def _bare(level):
    return Battery.from_home(_found("remote", level, None, None, None, None, app=False))


def _marks(choices, *batteries):
    return [(w.battery.name, w.mark) for w in choices.due(list(batteries))]


def test_falling_past_a_mark_warns_once(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _mac(16)) == []
    # A mark warns when the level reaches it - Home Assistant's levels are whole numbers, so "down to 15%".
    assert _marks(choices, _mac(15)) == [("MacBook Pro", 15)]
    assert _marks(choices, _mac(15)) == []
    assert _marks(choices, _mac(14.9)) == []


def test_a_phone_warns_on_its_mark_itself(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _phone(6)) == []
    assert _marks(choices, _phone(5)) == [("S25 Ultra", 5)]


def test_a_charging_device_below_a_mark_gives_nothing(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _mac(10, charging=True)) == []
    # Nor after it fell, and charging has started the marks over rather than warned.
    assert _marks(choices, _mac(40)) == []
    assert _marks(choices, _mac(8, charging=True)) == []
    assert _marks(choices, _mac(8)) == [("MacBook Pro", 15)]


def test_a_wobble_round_a_mark_never_warns_twice(tmp_path):
    choices = _choices(tmp_path)
    given = [_marks(choices, _mac(level)) for level in (15, 16, 14, 16, 14)]
    assert sum(len(g) for g in given) == 1


def test_charging_starts_the_marks_over(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]
    assert _marks(choices, _mac(40, charging=True)) == []
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]


def test_first_seen_below_a_mark_warns_once_for_the_lowest(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _mac(10)) == [("MacBook Pro", 15)]
    assert _marks(choices, _phone(4)) == [("S25 Ultra", 5)]
    assert _marks(choices, _mac(4)) == [("MacBook Pro", 5)]
    # The 15 was counted as warned with the 5, so a climb back to 14 isn't a second warning.
    assert _marks(choices, _mac(14)) == []


def test_a_device_with_no_charging_state_starts_over_once_its_battery_is_changed(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _bare(4)) == [("Remote", 5)]
    assert _marks(choices, _bare(20)) == []
    assert _marks(choices, _bare(4)) == []
    assert _marks(choices, _bare(25)) == []
    assert _marks(choices, _bare(4)) == [("Remote", 5)]


def test_the_mac_starts_over_once_charged_though_it_still_says_not_charging(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]
    assert _marks(choices, _mac(80)) == []
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]
    assert _marks(choices, _mac(20)) == []
    assert _marks(choices, _mac(14)) == []


def test_a_device_switched_off_gives_no_warnings(tmp_path):
    choices = _choices(tmp_path)
    mac = _mac(10)
    choices.set(mac, enabled=False)
    assert not choices.enabled(mac.id)
    assert _marks(choices, mac) == []
    assert find([mac], "mac") == [mac]
    choices.set(mac, enabled=True)
    assert _marks(choices, mac) == [("MacBook Pro", 15)]


def test_a_devices_own_marks_replace_its_kinds(tmp_path):
    choices = _choices(tmp_path)
    mac = _mac(30)
    assert choices.marks_for(mac) == [15, 5]
    assert choices.marks_for(_phone(30)) == [5]
    choices.set(mac, marks=[20, 40])
    assert choices.marks_for(mac) == [40, 20]
    assert _marks(choices, _mac(35)) == [("MacBook Pro", 40)]
    assert choices.marks_for(_tablet(50)) == [5]


def test_the_phone_isnt_sent_its_own_warning_by_default_and_a_tablet_is(tmp_path):
    choices = _choices(tmp_path)
    assert not choices.send_to_phone(_phone(50))
    assert choices.send_to_phone(_tablet(50))
    assert choices.send_to_phone(_mac(50))
    choices.set(_phone(50), send_to_phone=True)
    choices.set(_tablet(50), send_to_phone=False)
    assert choices.send_to_phone(_phone(50))
    assert not choices.send_to_phone(_tablet(50))


def test_also_send_when_said_is_on_until_switched_off_and_is_kept(tmp_path):
    choices = _choices(tmp_path)
    assert choices.also_send_when_said
    choices.set_also_send_when_said(False)
    assert not choices.also_send_when_said
    assert not _choices(tmp_path).also_send_when_said


def test_choices_and_warned_marks_survive_a_restart(tmp_path):
    choices = _choices(tmp_path)
    mac = _mac(10)
    choices.set(_tablet(50), enabled=False, marks=[30])
    assert _marks(choices, mac) == [("MacBook Pro", 15)]

    again = _choices(tmp_path)
    assert _marks(again, mac) == []
    assert not again.enabled(_tablet(50).id)
    assert again.marks_for(_tablet(50)) == [30]


def test_a_new_device_never_takes_another_devices_warned_marks(tmp_path):
    choices = _choices(tmp_path)
    assert _marks(choices, _mac(10)) == [("MacBook Pro", 15)]
    other = Battery.from_home(_found("old-macbook", 10.0, "d9", "Apple", "Mac14,2", "Not Charging"))
    assert _marks(choices, other) == [("Old MacBook", 15)]


def test_a_device_gone_from_home_assistant_keeps_its_choices(tmp_path):
    choices = _choices(tmp_path)
    tablet = _tablet(50)
    choices.set(tablet, marks=[30], send_to_phone=False)
    assert _marks(choices, _tablet(20)) == [("Tab S11 Ultra", 30)]
    assert _marks(choices, _mac(50)) == []
    assert _marks(choices, _tablet(20)) == []
    assert choices.marks_for(tablet) == [30]
    assert not choices.send_to_phone(tablet)


def test_marks_which_dont_make_sense_are_refused(tmp_path):
    choices = _choices(tmp_path)
    mac, phone = _mac(50), _phone(50)
    for marks in ([150], [0, 10], [5, 5], [10], [1, 2, 3], [5.5, 10], [True, 10], ["5", 10]):
        with pytest.raises(ValueError):
            choices.set(mac, marks=marks)
    for marks in ([5, 10], [], [100]):
        with pytest.raises(ValueError):
            choices.set(phone, marks=marks)
    assert choices.marks_for(mac) == [15, 5]
    assert choices.marks_for(phone) == [5]


@pytest.mark.parametrize("marks", [[5], [15, 5], [20, 10, 5], [10, 10], [], [0], [100], [5.5], [True], ["5"], [99, 1]])
def test_config_and_the_page_agree_on_which_marks_make_sense(tmp_path, marks):
    # config.yaml's defaults and a device's own marks follow one rule, so the page is never offered a
    # device it can't then save. Each is asked at its own door - an install's settings file, with the counts
    # config has for each setting, and the page's set() with the counts it has for each kind of device.
    kinds = {"mobile_marks": _phone, "other_marks": _mac}
    assert {setting for _, setting in MARKS} == set(kinds)
    for (section, setting), count in MARKS.items():
        (tmp_path / "config.yaml").write_text(f"{section}:\n  {setting}: {json.dumps(marks)}\n")
        try:
            Config(path=str(tmp_path / "config.yaml"), quiet=True)
            refused_by_config = False
        except ConfigError:
            refused_by_config = True
        battery = kinds[setting](50)
        try:
            _choices(tmp_path).set(battery, marks=marks)
            refused_by_page = False
        except ValueError:
            refused_by_page = True
        assert refused_by_config == refused_by_page, (setting, count, marks)


def test_a_refused_change_changes_nothing(tmp_path):
    choices = _choices(tmp_path)
    mac = _mac(50)
    with pytest.raises(ValueError):
        choices.set(mac, enabled=False, marks=[150])
    assert choices.enabled(mac.id)
    assert not (tmp_path / "batteries.json").exists()


def test_changed_says_whether_any_choice_differs_from_the_defaults(tmp_path):
    choices = _choices(tmp_path)
    for battery in (_mac(50), _phone(50), _tablet(50)):
        assert not choices.changed(battery)
        choices.set(battery, enabled=False)
        assert choices.changed(battery)
        choices.reset(battery.id)
        assert not choices.changed(battery)
        choices.set(battery, marks=[40] if battery.kind == "mobile" else [40, 20])
        assert choices.changed(battery)
        choices.reset(battery.id)
        choices.set(battery, send_to_phone=not choices.send_to_phone(battery))
        assert choices.changed(battery)
    # Choosing what the defaults already are is no change.
    choices.reset(_mac(50).id)
    choices.set(_mac(50), enabled=True, marks=[15, 5], send_to_phone=True)
    assert not choices.changed(_mac(50))


def test_the_defaults_are_the_kinds_highest_first(tmp_path):
    choices = _choices(tmp_path)
    assert choices.default_marks(_mac(50)) == [15, 5]
    assert choices.default_marks(_phone(50)) == [5]
    choices.set(_mac(50), marks=[20, 40])
    assert choices.default_marks(_mac(50)) == [15, 5]


def test_resetting_brings_back_the_kinds_defaults_but_a_warned_mark_stays_warned(tmp_path):
    choices = _choices(tmp_path)
    tablet = _tablet(50)
    choices.set(tablet, enabled=False, marks=[30], send_to_phone=False)
    choices.set(tablet, enabled=True)
    assert _marks(choices, _tablet(12)) == [("Tab S11 Ultra", 30)]

    choices.reset(tablet.id)
    assert choices.enabled(tablet.id)
    assert choices.marks_for(tablet) == [5]
    assert choices.send_to_phone(tablet)
    assert _marks(choices, _tablet(12)) == []
    assert _choices(tmp_path).marks_for(tablet) == [5]
    # The 30 was warned before the reset, so a wobble back across it doesn't warn again.
    choices.set(tablet, marks=[30])
    assert _marks(choices, _tablet(12)) == []


def test_resetting_a_device_never_changed_is_harmless(tmp_path):
    choices = _choices(tmp_path)
    choices.reset("sensor.nothing_battery_level")
    assert choices.enabled("sensor.nothing_battery_level")


def test_a_watcher_and_the_page_at_once_lose_neither_a_choice_nor_a_warned_mark(tmp_path):
    choices = _choices(tmp_path)
    devices = [
        Battery.from_home(_found(f"mac-{n}", 10.0, f"d{n}", "Apple", "Mac16,8", "Not Charging"))
        for n in range(40)
    ]
    given = []

    def watcher():
        for battery in devices:
            given.extend(choices.due([battery]))

    def page():
        for battery in devices:
            choices.set(battery, send_to_phone=False)

    threads = [threading.Thread(target=watcher), threading.Thread(target=page)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(given) == 40
    again = _choices(tmp_path)
    assert all(not again.send_to_phone(b) for b in devices)
    assert all(again.due([b]) == [] for b in devices)


def test_a_failed_save_of_the_warned_marks_still_warns_only_once(tmp_path, monkeypatch):
    choices = _choices(tmp_path)

    def failing(path, data, indent=None):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_json", failing)
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]
    assert _marks(choices, _mac(14)) == []


def test_a_failed_save_of_the_warned_marks_leaves_the_state_a_reader_holds_as_it_was(tmp_path, monkeypatch):
    # The store never changes its state in place, so a reader holding the lock can look at it without a copy.
    choices = _choices(tmp_path)
    before = choices._state
    _failing_saves(monkeypatch)
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]
    assert before["warned"] == {}
    assert choices._state["warned"] == {_mac(14).id: [15]}


def test_a_switched_off_device_still_starts_over_when_charged(tmp_path):
    choices = _choices(tmp_path)
    mac = _mac(14)
    assert _marks(choices, mac) == [("MacBook Pro", 15)]
    choices.set(mac, enabled=False)
    assert _marks(choices, _mac(40, charging=True)) == []
    choices.set(mac, enabled=True)
    assert _marks(choices, _mac(14)) == [("MacBook Pro", 15)]


def test_a_mark_near_the_top_still_starts_over_at_full(tmp_path):
    choices = _choices(tmp_path)

    def tablet(level):
        return Battery.from_home(_found("tab-s11-ultra", level, "d3", "samsung", "SM-X930", None))

    choices.set(tablet(90), marks=[95])
    assert _marks(choices, tablet(90)) == [("Tab S11 Ultra", 95)]
    # The start-over level is capped at exactly 100: not below it, or 99% would be a charge...
    assert _marks(choices, tablet(99)) == []
    assert _marks(choices, tablet(90)) == []
    # ...and not above it, where a device with no charging state could never reach it.
    assert _marks(choices, tablet(100)) == []
    assert _marks(choices, tablet(90)) == [("Tab S11 Ultra", 95)]


class FakeHome:
    """Answers batteries() and people() as Home Assistant does, from what a test sets."""

    def __init__(self, found, people):
        self.found = found
        self.people_now = people
        self.fails = None

    def batteries(self):
        if self.fails:
            raise self.fails
        return list(self.found)

    def people(self):
        return self.people_now


def _as_found(battery, charging=None):
    """The dict HomeAssistant.batteries() would give for a Battery made by the helpers above."""
    return {
        "id": battery.id, "name": battery.name, "level": battery.level, "changed": battery.changed,
        "device": battery.device, "maker": "Apple" if battery.computer else "samsung",
        "model": "Mac16,8" if battery.computer else "SM-S938B" if battery.phone else "SM-X930",
        "charging": charging or ("Charging" if battery.charging else "Not Charging"), "app": True,
    }


class Rig:
    """A watcher with recording fakes for saying, sending and quiet hours."""

    def __init__(self, tmp_path, battery, state="home", sends=True, ntfy=True, can_say=True, quiet=False):
        self.said, self.sent = [], []
        self.quiet = quiet
        self.can_say, self.sends = can_say, sends
        people = [] if state is None else [{"id": "person.n", "state": state, "devices": ["d1", "d2", "d3"]}]
        self.home = FakeHome([_as_found(battery)], people)
        self.choices = _choices(tmp_path)
        self.watcher = BatteryWatcher(
            self.home, self.choices, self._say, self._send if ntfy else self._no_ntfy,
            lambda: self.quiet, check_every=0.01, stale_after=HOUR,
        )

    def _say(self, message):
        if self.can_say:
            self.said.append(message)
        return self.can_say

    @staticmethod
    def _no_ntfy(message, title):
        return False

    def _send(self, message, title):
        if self.sends:
            self.sent.append((message, title))
        return self.sends

    def fall_to(self, battery):
        self.home.found = [_as_found(battery)]
        self.watcher.check(NOW)


MAC_MESSAGE = "Your MacBook Pro's battery is down to 14%."


def _fallen(tmp_path, battery=None, **kwargs):
    """A rig whose battery was fine, and has now fallen under its mark."""
    rig = Rig(tmp_path, _mac(40), **kwargs)
    rig.watcher.check(NOW)
    rig.fall_to(battery or _mac(14))
    return rig


def test_at_home_by_day_its_said_and_sent(tmp_path):
    rig = _fallen(tmp_path)
    assert rig.said == [MAC_MESSAGE]
    assert rig.sent == [(MAC_MESSAGE, "Battery low")]


def test_at_home_with_also_send_off_its_only_said(tmp_path):
    rig = Rig(tmp_path, _mac(40))
    rig.choices.set_also_send_when_said(False)
    rig.watcher.check(NOW)
    rig.fall_to(_mac(14))
    assert rig.said == [MAC_MESSAGE]
    assert rig.sent == []


def test_away_its_sent(tmp_path):
    rig = _fallen(tmp_path, state="not_home")
    assert rig.said == []
    assert rig.sent == [(MAC_MESSAGE, "Battery low")]


def test_at_night_its_sent_never_said(tmp_path):
    rig = _fallen(tmp_path, quiet=True)
    assert rig.said == []
    assert rig.sent == [(MAC_MESSAGE, "Battery low")]


def test_unsure_where_the_user_is_its_sent(tmp_path):
    rig = _fallen(tmp_path, state=None)
    assert rig.said == []
    assert rig.sent == [(MAC_MESSAGE, "Battery low")]


def test_a_failed_send_by_day_is_said(tmp_path):
    rig = _fallen(tmp_path, state="not_home", sends=False)
    assert rig.said == [MAC_MESSAGE]
    assert rig.choices.held() == {}


def test_a_failed_send_at_night_is_held_then_said_if_still_true(tmp_path):
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    assert rig.said == [] and rig.choices.held() == {_mac(14).id: 15}
    rig.watcher.check(NOW)
    assert rig.said == [], "still quiet"
    rig.quiet = False
    rig.watcher.check(NOW)
    assert rig.said == [MAC_MESSAGE]
    assert rig.choices.held() == {}
    rig.watcher.check(NOW)
    assert rig.said == [MAC_MESSAGE], "said once"


def test_a_held_warning_no_longer_true_is_dropped(tmp_path):
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    rig.quiet = False
    rig.fall_to(_mac(60, charging=True))
    assert rig.said == [] and rig.sent == []
    assert rig.choices.held() == {}


def test_a_held_warning_no_longer_true_is_let_go_with_a_line_in_the_log(tmp_path, caplog):
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    rig.quiet = False
    with caplog.at_level(logging.INFO, logger="voice_assistant.batteries"):
        rig.fall_to(_mac(60, charging=True))
    assert "MacBook Pro" in caplog.text and "charging" in caplog.text
    caplog.clear()
    (tmp_path / "second").mkdir()
    quiet = _fallen(tmp_path / "second", state="not_home", sends=False, quiet=True)
    quiet.quiet = False
    quiet.choices.set(_mac(14), enabled=False)
    with caplog.at_level(logging.INFO, logger="voice_assistant.batteries"):
        quiet.watcher.check(NOW)
    assert quiet.said == [] and quiet.choices.held() == {}
    assert "MacBook Pro" in caplog.text and "off" in caplog.text


def test_a_held_warning_and_a_new_lower_mark_straight_after_quiet_hours_are_said_once(tmp_path):
    # Held at 15 in the night; by the morning it has fallen to 4%, under the 5 which hasn't warned. Both are
    # "down to 4%", so only the new one is said.
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    assert rig.choices.held() == {_mac(14).id: 15}
    rig.quiet = False
    rig.fall_to(_mac(4))
    assert rig.said == ["Your MacBook Pro's battery is down to 4%."]
    assert rig.choices.held() == {}
    rig.watcher.check(NOW)
    assert len(rig.said) == 1, "and not again on the next pass"


def test_a_new_lower_mark_in_quiet_hours_takes_the_place_of_the_one_held(tmp_path):
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    rig.fall_to(_mac(4))
    assert rig.said == [] and rig.choices.held() == {_mac(14).id: 5}
    rig.quiet = False
    rig.watcher.check(NOW)
    assert rig.said == ["Your MacBook Pro's battery is down to 4%."]
    assert rig.choices.held() == {}


def test_a_new_warning_lets_go_only_of_its_own_devices_held_one(tmp_path):
    rig = Rig(tmp_path, _mac(40), state="not_home", sends=False, quiet=True)
    rig.home.found = [_as_found(_mac(40)), _as_found(_tablet(40))]
    rig.watcher.check(NOW)
    rig.home.found = [_as_found(_mac(14)), _as_found(_tablet(4))]
    rig.watcher.check(NOW)
    assert rig.choices.held() == {_mac(14).id: 15, _tablet(4).id: 5}
    rig.quiet = False
    rig.home.found = [_as_found(_mac(4)), _as_found(_tablet(4))]
    rig.watcher.check(NOW)
    assert sorted(rig.said) == ["Your MacBook Pro's battery is down to 4%.",
                                "Your Tab S11 Ultra's battery is down to 4%."]


def test_a_held_warning_stays_held_while_its_battery_isnt_listed(tmp_path):
    # Home Assistant leaves out a sensor that is unavailable for a moment, as it is while Home Assistant restarts.
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    rig.quiet = False
    rig.home.found = []
    rig.watcher.check(NOW)
    assert rig.said == [] and rig.choices.held() == {_mac(14).id: 15}
    rig.fall_to(_mac(14))
    assert rig.said == [MAC_MESSAGE] and rig.choices.held() == {}


def test_a_held_warning_is_still_true_at_its_mark_and_gone_just_above_it(tmp_path):
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    rig.quiet = False
    rig.fall_to(_mac(15))
    assert rig.said == ["Your MacBook Pro's battery is down to 15%."]
    (tmp_path / "second").mkdir()
    other = _fallen(tmp_path / "second", state="not_home", sends=False, quiet=True)
    other.quiet = False
    other.fall_to(_mac(16))
    assert other.said == [] and other.choices.held() == {}


def test_a_held_warning_which_couldnt_be_said_stays_held(tmp_path):
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True, can_say=False)
    rig.quiet = False
    rig.watcher.check(NOW)
    assert rig.choices.held() == {_mac(14).id: 15}
    rig.can_say = True
    rig.watcher.check(NOW)
    assert rig.said == [MAC_MESSAGE] and rig.choices.held() == {}


def test_a_held_warning_survives_a_restart(tmp_path):
    _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    assert _choices(tmp_path).held() == {_mac(14).id: 15}


def test_the_phones_own_warning_is_only_said(tmp_path):
    rig = Rig(tmp_path, _phone(40))
    rig.watcher.check(NOW)
    rig.fall_to(_phone(4))
    assert rig.said == ["Your S25 Ultra's battery is down to 4%."]
    assert rig.sent == []


def test_the_phones_own_warning_away_is_left_to_the_phone(tmp_path):
    rig = Rig(tmp_path, _phone(40), state="not_home")
    rig.watcher.check(NOW)
    rig.fall_to(_phone(4))
    assert rig.said == [] and rig.sent == []
    assert rig.choices.held() == {}


def test_with_no_satellite_its_sent(tmp_path):
    rig = _fallen(tmp_path, can_say=False)
    assert rig.sent == [(MAC_MESSAGE, "Battery low")]
    assert rig.choices.held() == {}


def test_with_no_ntfy_its_said_by_day_and_held_at_night(tmp_path):
    rig = _fallen(tmp_path, ntfy=False)
    assert rig.said == [MAC_MESSAGE]
    (tmp_path / "night").mkdir()
    night = _fallen(tmp_path / "night", ntfy=False, quiet=True)
    assert night.said == [] and night.choices.held() == {_mac(14).id: 15}


def test_the_person_with_the_low_device_decides_home(tmp_path):
    rig = Rig(tmp_path, _mac(40))
    rig.home.people_now = [
        {"id": "person.other", "state": "home", "devices": ["d9"]},
        {"id": "person.n", "state": "not_home", "devices": ["d1"]},
    ]
    assert rig.watcher.home_now(_mac(14)) is False
    rig.home.people_now[0]["devices"] = ["d1"]
    rig.home.people_now[1]["devices"] = ["d9"]
    assert rig.watcher.home_now(_mac(14)) is True
    # A device nobody owns is taken to be the first person's.
    assert rig.watcher.home_now(_bare(14)) is True


def test_the_page_is_told_whose_presence_decides_each_device_only_with_several_people(tmp_path):
    rig = Rig(tmp_path, _mac(40))
    mac, phone, bare = _mac(14), _phone(4), _bare(14)
    rig.home.people_now = [
        {"id": "person.first", "name": "Amy", "state": "home", "devices": ["d2"]},
        {"id": "person.second", "name": "Ben", "state": "not_home", "devices": ["d1"]},
    ]
    # The same rule as home_now: the owner of the device, else the first person.
    assert rig.watcher.deciding_people([mac, phone, bare]) == {mac.id: "Ben", phone.id: "Amy", bare.id: "Amy"}
    assert rig.watcher.home_now(mac) is False and rig.watcher.home_now(phone) is True
    # One person decides everything, so there is nothing to show.
    rig.home.people_now = rig.home.people_now[:1]
    assert rig.watcher.deciding_people([mac, phone]) == {}
    rig.home.people_now = []
    assert rig.watcher.deciding_people([mac]) == {}


def test_who_decides_is_left_out_when_home_assistant_cant_say(tmp_path):
    rig = Rig(tmp_path, _mac(40))
    rig.home.people = lambda: (_ for _ in ()).throw(requests.ConnectionError("down"))
    assert rig.watcher.deciding_people([_mac(14)]) == {}


def test_home_now_is_unsure_with_no_person_or_an_error(tmp_path):
    rig = Rig(tmp_path, _mac(40), state=None)
    assert rig.watcher.home_now(_mac(14)) is None
    rig.home.people = lambda: (_ for _ in ()).throw(requests.ConnectionError("down"))
    assert rig.watcher.home_now(_mac(14)) is None


@pytest.mark.parametrize("state", ["unknown", "unavailable"])
def test_home_now_is_unsure_of_a_person_home_assistant_has_no_state_for(tmp_path, state):
    # Neither is "away": Home Assistant just can't say. The warning goes to the phone all the same.
    rig = _fallen(tmp_path, state=state)
    assert rig.watcher.home_now(_mac(14)) is None
    assert rig.said == [] and rig.sent == [(MAC_MESSAGE, "Battery low")]


def test_a_check_which_fails_doesnt_stop_the_next(tmp_path, caplog):
    rig = Rig(tmp_path, _mac(14))
    # The thread checks by the real clock, so the reading must be fresh by it.
    rig.home.found[0]["changed"] = time.time()
    calls = []
    real = rig.watcher.check

    def flaky(now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")
        real(now)

    rig.watcher.check = flaky
    with caplog.at_level(logging.WARNING):
        rig.watcher.start()
        deadline = time.time() + 5
        while len(calls) < 3 and time.time() < deadline:
            time.sleep(0.01)
        rig.watcher.stop()
    assert len(calls) >= 3
    assert rig.said == [MAC_MESSAGE]
    assert "boom" in caplog.text
    assert not [t for t in threading.enumerate() if t.name == "batteries"]


def test_home_assistant_down_is_logged_once(tmp_path, caplog):
    rig = Rig(tmp_path, _mac(40))
    with caplog.at_level(logging.INFO):
        for error in (requests.ConnectionError("down"), ValueError("not json"), KeyError("x"), TypeError("y")):
            rig.home.fails = error
            rig.watcher._pass()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        rig.home.fails = None
        rig.watcher._pass()
        assert "answers again" in caplog.text
        rig.home.fails = requests.ConnectionError("down again")
        rig.watcher._pass()
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_stopping_is_prompt_and_leaves_no_thread(tmp_path):
    rig = Rig(tmp_path, _mac(40))
    rig.watcher._check_every = 3600
    rig.watcher.start()
    started = time.time()
    rig.watcher.stop()
    assert time.time() - started < 2
    assert not [t for t in threading.enumerate() if t.name == "batteries"]


def test_a_bug_after_the_read_is_logged_with_its_traceback_not_as_home_assistant_down(tmp_path, caplog):
    rig = Rig(tmp_path, _mac(14))

    def broken(batteries):
        raise KeyError("mark")

    rig.choices.due = broken
    with caplog.at_level(logging.INFO):
        rig.watcher._pass()
    assert "didn't answer" not in caplog.text
    assert [r for r in caplog.records if r.exc_info and r.exc_info[0] is KeyError]
    assert rig.watcher._down is False


def test_a_bug_working_out_the_new_warnings_still_lets_the_held_ones_be_said(tmp_path, caplog):
    # Worked out first, it raised before the held step, so a warning held all night was
    # never said while the bug lasted.
    rig = _fallen(tmp_path, state="not_home", sends=False, quiet=True)
    rig.quiet = False

    def broken(batteries):
        raise KeyError("mark")

    rig.choices.due = broken
    with caplog.at_level(logging.WARNING):
        rig.watcher.check(NOW)
    assert rig.said == [MAC_MESSAGE] and rig.choices.held() == {}
    assert [r for r in caplog.records if r.exc_info and r.exc_info[0] is KeyError]


def test_a_say_which_raises_is_a_bug_not_home_assistant_down(tmp_path, caplog):
    rig = _fallen(tmp_path)
    rig.watcher._say = lambda message: (_ for _ in ()).throw(KeyError("oops"))
    rig.home.found = [_as_found(_mac(40))]
    rig.watcher.check(NOW)
    rig.home.found = [_as_found(_mac(4))]
    with caplog.at_level(logging.INFO):
        rig.watcher._pass()
    assert "didn't answer" not in caplog.text
    assert [r for r in caplog.records if r.exc_info and r.exc_info[0] is KeyError]
    assert rig.watcher._down is False


def _failing_saves(monkeypatch):
    def fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_json", fail)


def test_a_failed_save_of_a_hold_still_holds_it(tmp_path, monkeypatch):
    choices = _choices(tmp_path)
    _failing_saves(monkeypatch)
    choices.hold("sensor.x", 15)
    assert choices.held() == {"sensor.x": 15}


def test_a_failed_save_of_a_release_still_lets_it_go(tmp_path, monkeypatch):
    choices = _choices(tmp_path)
    choices.hold("sensor.x", 15)
    _failing_saves(monkeypatch)
    choices.release("sensor.x")
    assert choices.held() == {}


def test_a_delivery_which_raises_doesnt_stop_the_next(tmp_path, caplog):
    rig = Rig(tmp_path, _mac(40))
    rig.home.found = [_as_found(_mac(40)), _as_found(_tablet(40))]
    rig.watcher.check(NOW)
    rig.home.found = [_as_found(_mac(14)), _as_found(_tablet(4))]

    def say(message):
        if "MacBook" in message:
            raise RuntimeError("boom")
        rig.said.append(message)
        return True

    rig.watcher._say = say
    with caplog.at_level(logging.WARNING):
        rig.watcher.check(NOW)
    assert rig.said == ["Your Tab S11 Ultra's battery is down to 4%."]
    assert "boom" in caplog.text


def test_a_held_step_which_raises_doesnt_stop_the_new_warnings(tmp_path):
    rig = Rig(tmp_path, _mac(40))
    rig.home.found = [_as_found(_mac(40)), _as_found(_tablet(40))]
    rig.watcher.check(NOW)
    rig.choices.hold(_mac(14).id, 15)
    rig.home.found = [_as_found(_mac(14)), _as_found(_tablet(4))]
    real = rig.watcher._say

    def say(message):
        if "MacBook" in message:
            raise RuntimeError("boom")
        return real(message)

    rig.watcher._say = say
    rig.watcher.check(NOW)
    assert rig.said == ["Your Tab S11 Ultra's battery is down to 4%."]


def test_the_phone_with_its_switch_on_away_is_sent(tmp_path):
    rig = Rig(tmp_path, _phone(40), state="not_home")
    rig.choices.set(_phone(40), send_to_phone=True)
    rig.watcher.check(NOW)
    rig.fall_to(_phone(4))
    assert rig.sent == [("Your S25 Ultra's battery is down to 4%.", "Battery low")]
    assert rig.said == []


def test_the_phone_with_its_switch_on_and_a_failed_send_by_day_is_said(tmp_path):
    rig = Rig(tmp_path, _phone(40), state="not_home", sends=False)
    rig.choices.set(_phone(40), send_to_phone=True)
    rig.watcher.check(NOW)
    rig.fall_to(_phone(4))
    assert rig.said == ["Your S25 Ultra's battery is down to 4%."]


def test_the_phone_with_its_switch_on_and_a_failed_send_at_night_is_held(tmp_path):
    rig = Rig(tmp_path, _phone(40), state="not_home", sends=False, quiet=True)
    rig.choices.set(_phone(40), send_to_phone=True)
    rig.watcher.check(NOW)
    rig.fall_to(_phone(4))
    assert rig.choices.held() == {_phone(4).id: 5}


def test_a_warning_from_an_old_reading_says_its_age(tmp_path):
    rig = Rig(tmp_path, _tablet(40))
    rig.watcher.check(NOW)
    old = _found("tab-s11-ultra", 4, "d3", "samsung", "SM-X930", "Not Charging", changed=NOW - 3 * 24 * HOUR)
    rig.home.found = [old]
    rig.watcher.check(NOW)
    assert rig.said == ["Your Tab S11 Ultra's battery was down to 4%, 3 days ago."]
