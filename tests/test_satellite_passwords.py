"""
Satellite Passwords Test
Checks the brain keeps one password for each satellite, in the order they were added,
where no other user can read them.
"""

import os
import stat

import pytest

from voice_assistant.satellite_passwords import SatellitePasswords


def test_passwords_are_kept_in_the_order_added_and_privately(tmp_path):
    path = str(tmp_path / "satellites.json")
    passwords = SatellitePasswords(path)

    first = passwords.add("bedroom")
    passwords.add("kitchen", "an-existing-password-here")

    assert passwords.names() == ["bedroom", "kitchen"]
    assert passwords.get("bedroom") == first and len(first) >= 32
    assert passwords.get("kitchen") == "an-existing-password-here"
    assert passwords.get("study") is None
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(ValueError):
        passwords.add("bedroom")


def test_a_short_password_is_refused(tmp_path):
    with pytest.raises(ValueError):
        SatellitePasswords(str(tmp_path / "s.json")).add("bedroom", "short")


def test_a_satellite_can_be_removed(tmp_path):
    passwords = SatellitePasswords(str(tmp_path / "s.json"))
    passwords.add("bedroom")

    assert passwords.remove("bedroom") is True
    assert passwords.remove("bedroom") is False
    assert passwords.names() == []
