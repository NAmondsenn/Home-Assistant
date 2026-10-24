"""Tests for the battery rules - what kind a device is, whether it charges, and its name."""

from voice_assistant.batteries import (
    Battery,
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
