"""
Weather Test
Checks the forecast is found and put into words, using a fake in place of
Open-Meteo, so nothing goes online.
"""

import pytest

from voice_assistant import weather as weather_module
from voice_assistant.weather import Weather

FORECAST = {
    "current": {"temperature_2m": 11.6, "weather_code": 3},
    "daily": {"weather_code": [3, 61], "temperature_2m_max": [14.6, 12.2],
              "temperature_2m_min": [7.8, 6.1], "precipitation_probability_max": [40, 85]},
}


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.fixture
def requests_made(monkeypatch):
    made = []

    def get(url, params=None, timeout=None):
        made.append(url)
        if url == weather_module.GEOCODING_URL:
            return FakeResponse({"results": [{"latitude": 54.97, "longitude": -1.61,
                                              "name": params["name"], "country": "United Kingdom"}]})
        return FakeResponse(FORECAST)

    monkeypatch.setattr(weather_module.requests, "get", get)
    return made


def _weather(make_config, location="Newcastle"):
    return Weather(config=make_config(weather={"location": location}))


def test_todays_weather_is_said_as_a_sentence(make_config, requests_made):
    # Weather used to be unanswerable - search only switched on for "search" or "look up".
    result = _weather(make_config).forecast("today")
    assert result == {"success": True, "message": "It's 12 degrees and cloudy at the moment. Today, a high of "
                                                  "15 and a low of 8, with a 40 percent chance of rain."}


def test_tomorrows_weather_is_said_as_a_sentence(make_config, requests_made):
    assert _weather(make_config).forecast("tomorrow")["message"] == \
        "Tomorrow, raining lightly, a high of 12 and a low of 6, with an 85 percent chance of rain."


def test_the_place_is_only_looked_up_once(make_config, requests_made):
    weather = _weather(make_config)
    weather.forecast()
    weather.forecast("tomorrow")
    assert requests_made.count(weather_module.GEOCODING_URL) == 1


def test_without_a_location_it_says_so(make_config, requests_made):
    assert "Set weather location" in _weather(make_config, location="").forecast()["message"]
    assert requests_made == []


def test_a_failed_request_says_so_rather_than_crashing(make_config, monkeypatch):
    def offline(*args, **kwargs):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(weather_module.requests, "get", offline)
    assert _weather(make_config).forecast() == {"success": False,
                                                "message": "Sorry, I couldn't get the weather right now."}
