"""
Satellite Passwords Test
Checks the brain keeps one password for each satellite, in the order they were added,
where no other user can read them.
"""

import os
import stat
import subprocess
import sys
import threading

import pytest

from voice_assistant.satellite_passwords import SatellitePasswords, name_problem


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


def test_a_satellites_name_is_a_machine_name():
    # Its Pi's own, which setup.sh gives the brain for it.
    for name in ("bedroom", "pi-2", "a", "a" * 63):
        assert name_problem(name) is None
    for name in ("Bedroom", "-pi", "pi-", "pi_2", "pi 2", "a" * 64, ""):
        assert name_problem(name) == ("A satellite's name is its Pi's machine name: lower-case letters, digits "
                                      "and hyphens, not starting or ending with a hyphen.")


def test_a_satellite_can_be_made_the_default(tmp_path):
    path = str(tmp_path / "s.json")
    passwords = SatellitePasswords(path)
    for name in ("bedroom", "kitchen", "hall"):
        passwords.add(name)
    kept = {name: passwords.get(name) for name in passwords.names()}

    passwords.make_default("hall")

    # Moved to the front, with every password as it was.
    assert passwords.names() == ["hall", "bedroom", "kitchen"]
    assert {name: passwords.get(name) for name in passwords.names()} == kept
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(ValueError):
        passwords.make_default("study")
    assert passwords.names() == ["hall", "bedroom", "kitchen"]


def test_the_command_line_and_the_page_wait_for_each_other(tmp_path):
    # backend.py's add-satellite and remove-satellite run in a process of their own,
    # reading the file, changing it and writing it back, as the page does in the
    # brain's: each read before the other wrote, one change was lost - a password the
    # page had just shown, say, put back to the old one.
    path = str(tmp_path / "s.json")
    passwords = SatellitePasswords(path)
    passwords.add("bedroom")
    holding = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, sys\n"
                               f"lock = open({path + '.lock'!r}, 'a')\n"
                               "fcntl.flock(lock, fcntl.LOCK_EX)\n"
                               "print('held', flush=True)\n"
                               "sys.stdin.readline()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holding.stdout.readline().strip() == "held"
        added = threading.Thread(target=passwords.add, args=("kitchen",), daemon=True)
        added.start()
        added.join(0.5)
        # It waits for the other to finish with the file.
        assert added.is_alive() and passwords.names() == ["bedroom"]
    finally:
        holding.stdin.write("\n")
        holding.stdin.flush()
        holding.wait(5)
    added.join(5)
    assert not added.is_alive() and passwords.names() == ["bedroom", "kitchen"]


def test_no_change_is_lost_between_two_processes(tmp_path):
    path = str(tmp_path / "s.json")
    SatellitePasswords(path).add("bedroom")
    adding = ("import sys\n"
              "from voice_assistant.satellite_passwords import SatellitePasswords\n"
              "passwords = SatellitePasswords(sys.argv[1])\n"
              "for number in range(20):\n"
              "    passwords.add(f'{sys.argv[2]}-{number}')\n")
    project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    each = [subprocess.Popen([sys.executable, "-c", adding, path, prefix], cwd=project) for prefix in ("a", "b")]
    assert [process.wait(30) for process in each] == [0, 0]

    assert len(SatellitePasswords(path).names()) == 41
