"""
Weather Test
Checks Home Assistant's forecast, from Met, is put into words, using a fake in place of
Home Assistant, so nothing goes online.

Every time here is written in the machine's own zone and sent as Home Assistant does, in
UTC, so the days and hours come out the same in London and in UTC.
"""

from datetime import datetime, timedelta, timezone

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


def _day(days, condition="cloudy", high=14.4, low=7.9, chance=None):
    """A daily entry as Met gives it through Home Assistant - with no chance of rain, unless one's given."""
    entry = {"condition": condition, "datetime": _at(days, 12), "humidity": 80, "precipitation": 1.2,
             "temperature": high, "templow": low, "uv_index": 1.0, "wind_bearing": 200, "wind_speed": 20.5}
    if chance is not None:
        entry["precipitation_probability"] = chance
    return entry


def _hour(days, hour, precipitation=0.0, condition="cloudy"):
    """An hourly entry as Met gives it through Home Assistant."""
    return {"condition": condition, "datetime": _at(days, hour), "humidity": 85, "precipitation": precipitation,
            "temperature": 12.0, "uv_index": 0.5, "wind_bearing": 210, "wind_speed": 18.0, "cloud_coverage": 90}


def _dry_hours(days=0, first=13):
    return [_hour(days, hour) for hour in range(first, 24)]


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
