"""Tests for saying where the user's phone, tablet or Mac is, and ringing it - see devices.py."""

from voice_assistant.batteries import find
from voice_assistant.devices import AppDevice, ring_call, where

NOW = 1_000_000_000.0
TEN_MINUTES_AGO = NOW - 600


def _found(name="s25-ultra", maker="samsung", model="SM-S938B", notify="mobile_app_s25_ultra", tracker="not_home",
           reported=TEN_MINUTES_AGO, place=None):
    """One of HomeAssistant.app_devices()'s dicts - the S25's, unless a test says otherwise."""
    return {"device": f"dev_{name}", "name": name, "maker": maker, "model": model, "notify": notify,
            "tracker": None if tracker is None else {"state": tracker, "reported": reported}, "place": place}


PHONE = dict(name="s25-ultra", maker="samsung", model="SM-S938B")
TABLET = dict(name="tab-s11-ultra", maker="samsung", model="SM-X930", notify="mobile_app_tab_s11_ultra")
MAC = dict(name="macbook-pro", maker="Apple", model="Mac16,8", notify="mobile_app_macbook_pro")
IPHONE = dict(name="iphone", maker="Apple", model="iPhone17,1", notify="mobile_app_iphone")


def test_an_app_device_is_built_from_what_home_assistant_says():
    phone = AppDevice.from_home(_found(place={"state": "1 Example Road, Sometown, EX1 1AA, UK",
                                              "thoroughfare": "Example Road", "locality": "Sometown"}))
    assert phone == AppDevice(id="dev_s25-ultra", name="S25 Ultra", kind="mobile", phone=True, computer=False,
                              apple=False, notify="mobile_app_s25_ultra", tracker="not_home",
                              reported=TEN_MINUTES_AGO, place="Example Road, Sometown")
    mac = AppDevice.from_home(_found(**MAC, tracker=None))
    assert (mac.kind, mac.phone, mac.computer, mac.apple, mac.tracker, mac.reported) == \
        ("other", False, True, True, None, None)


def test_a_device_is_found_by_what_was_said_as_a_battery_is():
    devices = [AppDevice.from_home(_found(**found)) for found in (PHONE, TABLET, MAC)]
    assert [d.name for d in find(devices, "phone")] == ["S25 Ultra"]
    assert [d.name for d in find(devices, "tablet")] == ["Tab S11 Ultra"]
    assert [d.name for d in find(devices, "mac")] == ["MacBook Pro"]
    assert find(devices, "kettle") == []


def test_where_a_device_is_is_said_from_its_tracker():
    def said(**found):
        return where(AppDevice.from_home(_found(**found)), NOW)

    assert said(tracker="home") == "Your phone's at home."
    assert said(tracker="Work") == "Your phone's at Work."
    assert said(place={"state": "1 Example Road, Sometown, EX1 1AA, UK", "thoroughfare": "Example Road",
                       "locality": "Sometown"}) == "Your phone's near Example Road, Sometown, as of 10 minutes ago."
    # Without the parts, the first two of the sensor's state.
    assert said(place={"state": "1 Example Road, Sometown, EX1 1AA, UK", "thoroughfare": None, "locality": None}) == \
        "Your phone's near 1 Example Road, Sometown, as of 10 minutes ago."
    # The Android app gives a town it hasn't got as the word "null" (live, 25 Oct), so it's only the street.
    for missing in ("null", "None", "unknown", "Unavailable", " ", None):
        assert said(place={"state": "12 Example Road, null", "thoroughfare": "Example Road",
                           "locality": missing}) == "Your phone's near Example Road, as of 10 minutes ago."
    assert said(place={"state": "null", "thoroughfare": "NULL", "locality": "null"}) == \
        "Your phone's away from home, as of 10 minutes ago."
    # A part that isn't text is still said as text, without its spaces.
    assert said(place={"state": "", "thoroughfare": " Example Road ", "locality": 12}) == \
        "Your phone's near Example Road, 12, as of 10 minutes ago."
    assert said() == "Your phone's away from home, as of 10 minutes ago."
    assert said(place={"state": "unknown", "thoroughfare": None, "locality": None}) == \
        "Your phone's away from home, as of 10 minutes ago."
    assert said(tracker=None) == "I can't tell where your phone is."
    assert said(tracker="unavailable") == "I can't tell where your phone is."
    # A tablet or a Mac is said as that word.
    assert said(**TABLET, tracker="home") == "Your tablet's at home."
    assert said(**MAC, tracker="home") == "Your Mac's at home."


def test_each_kind_of_device_is_rung_its_own_way():
    assert ring_call(AppDevice.from_home(_found(**PHONE))) == {
        "message": "TTS", "data": {"tts_text": "Nova's looking for your phone.", "media_stream": "alarm_stream_max",
                                   "priority": "high", "ttl": 0}}
    assert ring_call(AppDevice.from_home(_found(**IPHONE))) == {
        "message": "Nova's looking for your iPhone.",
        "data": {"push": {"sound": {"name": "default", "critical": 1, "volume": 1.0}}}}
    assert ring_call(AppDevice.from_home(_found(**MAC))) == {
        "message": "Nova's looking for your Mac.", "data": {"push": {"sound": "default"}}}
