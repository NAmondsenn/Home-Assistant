"""Tests for the battery rules - what kind a device is, whether it charges, and its name."""

import threading

import pytest

from voice_assistant.batteries import (
    Battery,
    BatteryChoices,
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
    found = _found("remote", 61.0, None, None, None, "charging", app=False)
    battery = Battery.from_home(found)
    assert battery.kind == "other"
    assert battery.phone is False
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
    assert _marks(choices, _mac(15)) == []
    assert _marks(choices, _mac(14.9)) == [("MacBook Pro", 15)]
    assert _marks(choices, _mac(14.9)) == []


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


def test_a_refused_change_changes_nothing(tmp_path):
    choices = _choices(tmp_path)
    mac = _mac(50)
    with pytest.raises(ValueError):
        choices.set(mac, enabled=False, marks=[150])
    assert choices.enabled(mac.id)
    assert not (tmp_path / "batteries.json").exists()


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
