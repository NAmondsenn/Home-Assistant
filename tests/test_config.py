"""
Config Test
Checks settings are loaded from config.yaml, with config.local.yaml's on top.
"""

from voice_assistant.config import Config


def _write(folder, name, text):
    (folder / name).write_text(text)
    return str(folder / name)


def test_local_settings_override_only_what_they_name(tmp_path):
    path = _write(tmp_path, "config.yaml", "weather:\n  location: ''\n  units: metric\naudio:\n  sample_rate: 48000\n")
    _write(tmp_path, "config.local.yaml", "weather:\n  location: Newcastle upon Tyne\n")

    config = Config(path)
    assert config.section("weather") == {"location": "Newcastle upon Tyne", "units": "metric"}
    assert config.section("audio") == {"sample_rate": 48000}


def test_without_local_settings_config_yaml_is_used_as_it_is(tmp_path):
    path = _write(tmp_path, "config.yaml", "weather:\n  location: ''\n")
    assert Config(path).section("weather") == {"location": ""}
