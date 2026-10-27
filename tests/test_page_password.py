"""
Page Password Test
Checks the settings page's own password is kept only as a hash, its logins only as
hashes of their tokens for 30 days, and wrong passwords are slowed per device with a
backstop - all failing shut when a file can't be read.
"""

import hashlib
import os
import stat

from voice_assistant.page_password import (
    LOGIN_DAYS,
    WAIT,
    Logins,
    PagePassword,
    WrongTries,
)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, start: float = 1_000_000.0):
        self.time = start

    def __call__(self) -> float:
        return self.time


def test_a_password_is_kept_only_as_a_hash(tmp_path):
    path = str(tmp_path / "page_password")
    password = PagePassword(path)

    assert password.set("correct horse battery") is None

    with open(path) as file:
        text = file.read()
    assert "correct horse battery" not in text
    assert text.startswith("scrypt$")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert password.is_set()
    assert password.check("correct horse battery")
    assert not password.check("correct horse batterz")
    # A fresh one over the same file - the brain after a restart - agrees.
    assert PagePassword(path).check("correct horse battery")


def test_a_short_password_is_refused(tmp_path):
    password = PagePassword(str(tmp_path / "page_password"))

    assert password.set("eleven char") == "The page password needs at least 12 characters."
    assert not password.is_set()
    assert not password.check("eleven char")


def test_an_unreadable_password_file_is_not_set(tmp_path, caplog):
    missing = PagePassword(str(tmp_path / "missing"))
    assert not missing.is_set()
    assert not missing.check("correct horse battery")

    for garbage in ("not a hash at all", "scrypt$16384$8$1$!!!$!!!", "scrypt$x$8$1$AAAA$AAAA",
                    "scrypt$16384$8$1$AAAA", "\x00\xff"):
        path = tmp_path / "page_password"
        path.write_text(garbage)
        password = PagePassword(str(path))
        assert not password.is_set()
        assert not password.check("correct horse battery")
    # The log says the file is wrong without quoting it.
    assert "not a hash" not in caplog.text and "!!!" not in caplog.text


def test_a_login_ends_after_thirty_days_and_when_ended(tmp_path):
    path = str(tmp_path / "page_logins.json")
    clock = Clock()
    logins = Logins(path, now=clock)

    token = logins.make()
    assert logins.check(token)
    assert not logins.check("not-a-login")
    assert not logins.check(None)
    assert not logins.check("")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path) as file:
        text = file.read()
    assert token not in text
    assert hashlib.sha256(token.encode()).hexdigest() in text

    # Survives a restart.
    assert Logins(path, now=clock).check(token)

    # Using it doesn't extend it.
    clock.time += LOGIN_DAYS * 86400 - 1
    assert logins.check(token)
    clock.time += 2
    assert not logins.check(token)
    assert not Logins(path, now=clock).check(token)
    # And the ended one is dropped from the file.
    logins.make()
    with open(path) as file:
        assert hashlib.sha256(token.encode()).hexdigest() not in file.read()

    first, second = logins.make(), logins.make()
    logins.end(first)
    assert not logins.check(first)
    assert logins.check(second)
    assert not Logins(path, now=clock).check(first)
    assert Logins(path, now=clock).check(second)

    logins.end_all()
    assert not logins.check(second)
    assert not Logins(path, now=clock).check(second)


def test_a_corrupt_logins_file_lets_nobody_in(tmp_path, caplog):
    path = tmp_path / "page_logins.json"
    for garbage in ("not json", "[1, 2]", '{"abc": "tomorrow"}', "\x00\xff"):
        path.write_text(garbage)
        logins = Logins(str(path))
        assert not logins.check("anything")
        token = logins.make()
        assert logins.check(token)
        assert token not in caplog.text
    assert "tomorrow" not in caplog.text


def test_wrong_tries_are_per_device_with_a_backstop():
    clock = Clock()
    tries = WrongTries(now=clock)

    for _ in range(4):
        tries.wrong("a")
    assert not tries.waiting("a")
    tries.wrong("a")
    assert tries.waiting("a")
    assert not tries.waiting("b")

    clock.time += WAIT - 1
    assert tries.waiting("a")
    clock.time += 1
    assert not tries.waiting("a")

    # A right one resets the device's count.
    for _ in range(4):
        tries.wrong("a")
    tries.right("a")
    tries.wrong("a")
    assert not tries.waiting("a")

    # Many devices together hold every try, even a new device's.
    clock.time += WAIT
    for number in range(30):
        tries.wrong(f"device {number}")
    assert tries.waiting("device 0")
    assert tries.waiting("somewhere else")
    clock.time += WAIT - 1
    assert tries.waiting("somewhere else")
    clock.time += 1
    assert not tries.waiting("somewhere else")
    assert not tries.waiting("device 0")
    # Nothing is held for those addresses once a minute has passed with no tries.
    assert tries._devices == {}
    assert len(tries._recent) == 0
