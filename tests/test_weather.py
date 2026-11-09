"""
Weather Test
Checks Home Assistant's forecast, from Met, is put into words, using a fake in place of
Home Assistant, so nothing goes online.

Every time here is written in the machine's own zone and sent as Home Assistant does, in
UTC, so the days and hours come out the same in London and in UTC. Met's daily entries
are at noon UTC, the first of them today, and its hourly ones are 48 from the next hour.
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests

from voice_assistant.choices import ChoiceStore
from voice_assistant.home_assistant import CANT_REACH
from voice_assistant.weather import Weather

# A Wednesday in October, away from the clocks changing, at 1 PM local time.
NOW = datetime(2026, 10, 14, 13, 0).astimezone()

TODAY = ("It's 12 degrees and partly cloudy at the moment. Today, a high of 14 and a low of 8, "
         "with a 40 percent chance of rain.")


def _at(days: int, hour: int) -> str:
    """A local time this many days from NOW, as Home Assistant sends it."""
    local = datetime(NOW.year, NOW.month, NOW.day, hour).astimezone() + timedelta(days=days)
    return local.astimezone(timezone.utc).isoformat()


def _entity(entity_id="weather.forecast_home", state="partlycloudy", temperature=12.3, hours_old=0.5):
    """A weather entity as HomeAssistant.weather_entities gives it."""
    attributes = {"temperature": temperature, "humidity": 80, "friendly_name": "Forecast Home"}
    if temperature is None:
        del attributes["temperature"]
    return {"id": entity_id, "name": "Forecast Home", "state": state, "attributes": attributes,
            "reported": NOW.timestamp() - hours_old * 3600}


def _day(days, condition="cloudy", high=14.4, low=7.9, chance=None, now=None):
    """
    A daily entry as Met gives it through Home Assistant, at noon UTC on the local day this
    many days from now (NOW unless given) - with no chance of rain, unless one's given.
    """
    day = (now or NOW).date() + timedelta(days=days)
    entry = {"condition": condition, "datetime": f"{day.isoformat()}T12:00:00+00:00", "humidity": 80,
             "precipitation": 1.2,
             "temperature": high, "templow": low, "uv_index": 1.0, "wind_bearing": 200, "wind_speed": 20.5}
    if chance is not None:
        entry["precipitation_probability"] = chance
    return entry


def _hourly(when: str, precipitation=0.0, condition="cloudy"):
    """An hourly entry as Met gives it through Home Assistant, for a time sent as it sends it."""
    return {"condition": condition, "datetime": when, "humidity": 85, "precipitation": precipitation,
            "temperature": 12.0, "uv_index": 0.5, "wind_bearing": 210, "wind_speed": 18.0, "cloud_coverage": 90}


def _hour(days, hour, precipitation=0.0, condition="cloudy"):
    return _hourly(_at(days, hour), precipitation, condition)


def _met_hours(now, rainy=lambda local: False):
    """Met's 48 hourly entries from the hour after now, in UTC, raining in the local hours rainy() says."""
    first = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    hours = [first + timedelta(hours=count) for count in range(48)]
    return [_hourly(when.isoformat(), 0.6 if rainy(when.astimezone()) else 0.0) for when in hours]


def _dry_hours(days=0, first=14):
    return [_hour(days, hour) for hour in range(first, 24)]


@pytest.fixture(params=["Europe/London", "UTC"])
def zone(request, monkeypatch):
    """Runs a test on London time and on UTC, whatever the machine's own zone."""
    monkeypatch.setenv("TZ", request.param)
    time.tzset()
    yield request.param
    monkeypatch.undo()
    time.tzset()


DAYS = [_day(0, chance=40), _day(1), _day(2)]


class FakeHome:
    """Stands in for HomeAssistant, with its weather entities and their forecasts."""

    def __init__(self, entities=None, daily=None, hourly=(), down=False):
        self.entities = [_entity()] if entities is None else entities
        self.daily = DAYS if daily is None else daily
        self.hourly = list(hourly)
        self.down = down
        self.asked = []

    def weather_entities(self):
        if self.down:
            raise requests.ConnectionError("down")
        return [dict(entity) for entity in self.entities]

    def forecasts(self, entity_id, kind):
        self.asked.append((entity_id, kind))
        return list(self.daily if kind == "daily" else self.hourly)


@pytest.fixture
def choices(tmp_path):
    return ChoiceStore(str(tmp_path / "weather.json"), {"entity": None})


def _said(home, choices, day="today", now=NOW):
    return Weather(home, choices).forecast(day, now=now)["message"]


def test_todays_weather_is_said_as_before(choices):
    home = FakeHome()
    assert Weather(home, choices).forecast(now=NOW) == {"success": True, "message": TODAY}
    # With a chance of rain for the day, the hours aren't needed.
    assert home.asked == [("weather.forecast_home", "daily")]


def test_without_a_chance_of_rain_the_hours_say_when(choices):
    # Met gives no chance of rain, so the hours still to come today say when it's likely.
    hours = [_hour(0, 11, 2.0)] + [_hour(0, hour, 0.4 if 15 <= hour <= 18 else 0.0) for hour in range(13, 24)]
    home = FakeHome(daily=[_day(0)], hourly=hours + [_hour(1, 9, 3.0)])
    assert _said(home, choices) == ("It's 12 degrees and partly cloudy at the moment. Today, a high of 14 and a "
                                    "low of 8, with rain likely from about 3 PM until 6 PM.")

    # One rainy hour is said as one, whether Met gives it any rain in millimetres or not.
    hours = [_hour(0, hour, condition="rainy" if hour == 15 else "cloudy") for hour in range(13, 24)]
    assert _said(FakeHome(daily=[_day(0)], hourly=hours), choices).endswith(", with rain likely around 3 PM.")


def test_tomorrows_weather_is_said_as_before(choices):
    home = FakeHome(daily=[_day(0), _day(1)], hourly=_dry_hours() + _dry_hours(days=1, first=0))
    assert _said(home, choices, "tomorrow") == "Tomorrow, cloudy, a high of 14 and a low of 8, and no rain expected."

    # "An 80", "an 11" and "an 18" are said with a vowel sound.
    home = FakeHome(daily=[_day(0), _day(1, condition="rainy", high=12.2, low=6.1, chance=85)])
    assert _said(home, choices, "tomorrow") == \
        "Tomorrow, raining, a high of 12 and a low of 6, with an 85 percent chance of rain."


def test_no_rain_says_so(choices):
    expected = "Today, a high of 14 and a low of 8, and no rain expected."
    assert _said(FakeHome(daily=[_day(0, chance=0)]), choices).endswith(expected)
    assert _said(FakeHome(daily=[_day(0)], hourly=_dry_hours()), choices).endswith(expected)


def test_an_old_forecast_says_how_old(choices):
    # With the internet down, Home Assistant still has Met's last forecast.
    old = _said(FakeHome(entities=[_entity(hours_old=5)]), choices)
    assert old == TODAY + " That's Home Assistant's forecast from 5 hours ago."
    assert _said(FakeHome(entities=[_entity(hours_old=2)]), choices) == TODAY


def test_a_forecast_with_less_in_it_still_says_what_it_has(choices):
    day = _day(0)
    del day["templow"]
    home = FakeHome(daily=[day])
    assert _said(home, choices) == "It's 12 degrees and partly cloudy at the moment. Today, a high of 14."

    # A condition Nova has no words for, or no temperature, is left out too.
    home = FakeHome(entities=[_entity(state="exceptional")], daily=[day])
    assert _said(home, choices).startswith("It's 12 degrees at the moment.")
    home = FakeHome(entities=[_entity(temperature=None)], daily=[day])
    assert _said(home, choices).startswith("It's partly cloudy at the moment.")


@pytest.mark.parametrize("odd", ["mild", "", [], {"high": 14}, True, float("nan"), float("inf")])
def test_a_high_or_low_that_isnt_a_number_is_left_out_not_an_error(choices, odd):
    # Met gives numbers, but a sentence is never worth an error: round("mild") raised TypeError.
    rain = "with a 40 percent chance of rain."
    assert _said(FakeHome(daily=[_day(0, high=odd, chance=40)]), choices) == (
        f"It's 12 degrees and partly cloudy at the moment. Today, a low of 8, {rain}")
    assert _said(FakeHome(daily=[_day(0, low=odd, chance=40)]), choices) == (
        f"It's 12 degrees and partly cloudy at the moment. Today, a high of 14, {rain}")
    assert _said(FakeHome(daily=[_day(0, high=odd, low=odd, chance=40)]), choices) == (
        "It's 12 degrees and partly cloudy at the moment. Today, a 40 percent chance of rain.")
    assert _said(FakeHome(daily=[_day(1, high=odd, low=odd)]), choices, day="tomorrow") == (
        "Tomorrow, cloudy.")


def test_a_temperature_or_chance_of_rain_that_isnt_a_number_is_left_out_too(choices):
    nan = float("nan")
    home = FakeHome(entities=[_entity(temperature=nan)], daily=[_day(0, chance=nan)], hourly=_dry_hours())
    assert _said(home, choices) == ("It's partly cloudy at the moment. Today, a high of 14 and a low of 8, "
                                    "and no rain expected.")
    home = FakeHome(entities=[_entity(temperature="mild")], daily=[_day(0, chance="lots")], hourly=_dry_hours())
    assert _said(home, choices).startswith("It's partly cloudy at the moment.")


def test_with_several_weather_entities_the_chosen_one_is_used(choices):
    home = FakeHome(entities=[_entity("weather.other_place"), _entity("weather.home")])
    weather = Weather(home, choices)

    # The first by entity id, until one's chosen.
    weather.forecast(now=NOW)
    choices.change(lambda state: state.update(entity="weather.other_place"))
    weather.forecast(now=NOW)
    # Or once the chosen one has gone.
    home.entities = [_entity("weather.home"), _entity("weather.second_home")]
    weather.forecast(now=NOW)
    assert [entity for entity, _ in home.asked] == ["weather.home", "weather.other_place", "weather.home"]


def test_without_a_weather_entity_or_with_it_unavailable_it_says_so(choices):
    assert Weather(FakeHome(entities=[]), choices).forecast(now=NOW) == {
        "success": False, "message": "Home Assistant hasn't got a weather forecast."}
    home = FakeHome(entities=[_entity(state="unavailable")])
    assert Weather(home, choices).forecast(now=NOW) == {
        "success": False, "message": "Home Assistant's forecast isn't available right now."}
    assert home.asked == []


def test_with_home_assistant_down_it_says_so(choices):
    assert Weather(FakeHome(down=True), choices).forecast(now=NOW) == {"success": False, "message": CANT_REACH}

    class GoesDown(FakeHome):
        def forecasts(self, entity_id, kind):
            raise requests.Timeout("timed out")

    assert Weather(GoesDown(), choices).forecast(now=NOW) == {"success": False, "message": CANT_REACH}


def test_a_token_home_assistant_wont_take_is_said_as_everywhere_else(choices):
    class Refuses(FakeHome):
        def weather_entities(self):
            raise requests.HTTPError("401", response=SimpleNamespace(status_code=401))

    assert Weather(Refuses(), choices).forecast(now=NOW) == {
        "success": False, "message": "Home Assistant didn't accept my token. HA_TOKEN in the .env file needs checking."}


def test_an_entry_without_a_time_or_a_chance_that_isnt_a_number_is_passed_over(choices):
    undated = _day(0, high=30)
    del undated["datetime"]
    home = FakeHome(daily=[undated, {**_day(0), "datetime": "soon"}, _day(0, chance="lots")],
                    hourly=[{"condition": "rainy", "precipitation": 3.0}] + _dry_hours())
    assert _said(home, choices).endswith("Today, a high of 14 and a low of 8, and no rain expected.")


def test_late_at_night_today_is_still_today_and_tomorrow_is_the_next_day(choices, zone):
    late = datetime(2026, 10, 14, 23, 30).astimezone()
    hours = [_hour(0, 23, condition="rainy"), _hour(1, 0, 0.8)]
    daily = [_day(0, now=late), _day(1, high=9.2, low=3.1, now=late)]
    entities = [{**_entity(), "reported": late.timestamp()}]
    # The 23:00 hour is still to come, and tomorrow's first isn't today's.
    assert _said(FakeHome(entities, daily, hours), choices, now=late).endswith(
        "a high of 14 and a low of 8, with rain likely around 11 PM.")

    just_before_midnight = datetime(2026, 10, 14, 23, 59).astimezone()
    assert _said(FakeHome(entities, daily, hours), choices, "tomorrow", now=just_before_midnight) == \
        "Tomorrow, cloudy, a high of 9 and a low of 3, with rain likely around midnight."


def test_the_hours_are_right_across_the_clocks_going_back(choices, zone):
    # 25 October 2026: in London, 1 AM comes twice, and the day has 25 hours. Only the first
    # 1 AM - on summer time, as now is - has rain, so losing it, or keeping the second's dry
    # hour in its place, says "around 3 AM", and putting 3 AM first says "until 1 AM".
    now = datetime(2026, 10, 25, 0, 30).astimezone()
    home = FakeHome([{**_entity(), "reported": now.timestamp()}], [_day(0, now=now), _day(1, now=now)],
                    _met_hours(now, lambda local: local.date() == now.date() and (
                        (local.hour == 1 and local.utcoffset() == now.utcoffset()) or local.hour == 3)))
    assert _said(home, choices, now=now).endswith(
        "Today, a high of 14 and a low of 8, with rain likely from about 1 AM until 3 AM.")
    assert _said(home, choices, "tomorrow", now=now) == \
        "Tomorrow, cloudy, a high of 14 and a low of 8, and no rain expected."


def test_the_weather_choice_is_only_offered_with_more_than_one(choices):
    home = FakeHome(entities=[_entity("weather.home")])
    weather = Weather(home, choices)
    assert weather.entities() == [{"id": "weather.home", "name": "Forecast Home", "chosen": True}]

    home.entities = [_entity("weather.other_place"), _entity("weather.home")]
    # The one the forecast would use - the first by entity id - until one is chosen.
    assert [(entity["id"], entity["chosen"]) for entity in weather.entities()] == [
        ("weather.other_place", False), ("weather.home", True)]
    weather.choose("weather.other_place")
    assert choices.get("entity") == "weather.other_place"
    assert [entity["chosen"] for entity in weather.entities()] == [True, False]
    # And it is used at the next forecast.
    weather.forecast(now=NOW)
    assert home.asked[0][0] == "weather.other_place"

    # Only a weather entity Home Assistant has.
    for made_up in ("weather.made_up", "light.kitchen", ""):
        with pytest.raises(ValueError, match="Home Assistant isn't showing that weather just now."):
            weather.choose(made_up)
    assert choices.get("entity") == "weather.other_place"


# Today as numbers and words, for the display's weather page

def test_today_gives_numbers_and_words(choices):
    home = FakeHome(entities=[_entity(state="rainy", temperature=9.4)], daily=[_day(0, high=11, low=5), _day(1)])
    assert Weather(home, choices).today(now=NOW) == {"temperature": 9.4, "condition": "raining", "high": 11, "low": 5}
    # The words are the ones Nova says, so "partlycloudy" is not shown as one.
    home = FakeHome(entities=[_entity(state="partlycloudy")])
    assert Weather(home, choices).today(now=NOW)["condition"] == "partly cloudy"
    # Only the daily forecast is asked for, and for the chosen entity.
    assert home.asked == [("weather.forecast_home", "daily")]


def test_today_has_no_high_or_low_without_a_forecast_for_today(choices):
    home = FakeHome(entities=[_entity(temperature=9.4)], daily=[_day(1), _day(2)])
    assert Weather(home, choices).today(now=NOW) == {"temperature": 9.4, "condition": "partly cloudy",
                                                     "high": None, "low": None}
    # A high or low which is not a number is left out, as it is when Nova says it.
    home = FakeHome(daily=[_day(0, high="mild", low=None)])
    today = Weather(home, choices).today(now=NOW)
    assert (today["high"], today["low"]) == (None, None)


def test_today_is_none_without_weather_to_show(choices):
    assert Weather(FakeHome(entities=[]), choices).today(now=NOW) is None
    home = FakeHome(entities=[_entity(state="unavailable")])
    assert Weather(home, choices).today(now=NOW) is None
    assert home.asked == []
    # A temperature which is missing leaves nothing to show.
    assert Weather(FakeHome(entities=[_entity(temperature=None)]), choices).today(now=NOW) is None


def test_today_leaves_a_condition_nova_does_not_say_empty(choices):
    home = FakeHome(entities=[_entity(state="exceptional")])
    assert Weather(home, choices).today(now=NOW)["condition"] == ""


def test_today_uses_the_chosen_entity(choices):
    home = FakeHome(entities=[_entity("weather.home", temperature=1.0), _entity("weather.other", temperature=2.0)])
    choices.change(lambda state: state.update(entity="weather.other"))
    assert Weather(home, choices).today(now=NOW)["temperature"] == 2.0
    assert home.asked == [("weather.other", "daily")]


def test_today_raises_when_home_assistant_cant_say(choices):
    # The display keeps its last good weather, so the failure is for its poll to catch.
    with pytest.raises(requests.ConnectionError):
        Weather(FakeHome(down=True), choices).today(now=NOW)

    class GoesDown(FakeHome):
        def forecasts(self, entity_id, kind):
            raise requests.Timeout("timed out")

    with pytest.raises(requests.Timeout):
        Weather(GoesDown(), choices).today(now=NOW)
