"""
Page Password Test
Checks the settings page's own password is kept only as a hash, its logins only as
hashes of their tokens for 30 days, and wrong passwords are slowed per device with a
backstop - all failing shut when a file can't be read.
"""

import hashlib
import json
import os
import stat
import threading
import time

from voice_assistant import page_password
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

    assert password.set("correct horse battery", Logins(str(tmp_path / "logins.json"))) is None

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
    logins = Logins(str(tmp_path / "logins.json"))
    token = logins.make()

    assert password.set("eleven char", logins) == "The page password needs at least 12 characters."
    assert not password.is_set()
    assert not password.check("eleven char")
    # Nothing changed, so nobody was logged out.
    assert logins.check(token)


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


def test_a_lone_surrogate_is_just_a_wrong_password(tmp_path, caplog):
    password = PagePassword(str(tmp_path / "page_password"))
    password.set("correct horse battery", Logins(str(tmp_path / "logins.json")))
    caplog.clear()

    assert not password.check("correct horse \ud800")
    assert caplog.text == ""


def test_a_new_password_ends_every_login_first(tmp_path):
    path = str(tmp_path / "page_password")
    password = PagePassword(path)
    logins = Logins(str(tmp_path / "logins.json"))
    password.set("correct horse battery", logins)
    token = logins.make()

    assert password.set("a different password", logins) is None
    assert not logins.check(token)
    assert password.check("a different password")
    assert not password.check("correct horse battery")


def test_a_new_password_is_not_saved_if_the_logins_cannot_be_ended(tmp_path):
    path = str(tmp_path / "page_password")
    password = PagePassword(path)
    password.set("correct horse battery", Logins(str(tmp_path / "logins.json")))

    class Stuck(Logins):
        def end_all(self):
            raise OSError("disk full")

    stuck = Stuck(str(tmp_path / "logins.json"))
    message = password.set("a different password", stuck)
    assert message == "Could not log out every device, so the page password is as it was."
    assert password.check("correct horse battery")
    assert not password.check("a different password")


def test_if_the_new_password_cannot_be_saved_the_old_one_stands_and_nobody_is_logged_in(
        tmp_path, monkeypatch):
    path = str(tmp_path / "page_password")
    password = PagePassword(path)
    logins = Logins(str(tmp_path / "logins.json"))
    password.set("correct horse battery", logins)
    token = logins.make()

    def full(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(page_password, "save_text", full)
    message = password.set("a different password", logins)
    assert message == "Could not save the page password, so it is as it was - every device is logged out."
    assert not logins.check(token)
    assert password.check("correct horse battery")


def test_a_login_made_as_the_new_password_is_saved_is_ended_too(tmp_path, monkeypatch):
    # One made with the old password between the first ending and the save would
    # otherwise outlive the change.
    password = PagePassword(str(tmp_path / "page_password"))
    logins = Logins(str(tmp_path / "logins.json"))
    password.set("correct horse battery", logins)
    made = []
    real_save = page_password.save_text

    def saving(path, text):
        made.append(logins.make())
        real_save(path, text)

    monkeypatch.setattr(page_password, "save_text", saving)
    assert password.set("a different password", logins) is None
    assert made and not logins.check(made[0])


def test_a_new_password_whose_logins_cannot_be_ended_after_says_so(tmp_path):
    password = PagePassword(str(tmp_path / "page_password"))
    password.set("correct horse battery", Logins(str(tmp_path / "logins.json")))

    class StuckSecondTime(Logins):
        ended = 0

        def end_all(self):
            self.ended += 1
            if self.ended == 2:
                raise OSError("disk full")
            super().end_all()

    message = password.set("a different password", StuckSecondTime(str(tmp_path / "logins.json")))
    assert message == ("The page password is changed, but Nova couldn't log every device out - "
                       "use Log out everywhere.")
    assert password.check("a different password")


def test_only_one_password_is_checked_at_a_time(tmp_path, monkeypatch):
    password = PagePassword(str(tmp_path / "page_password"))
    password.set("correct horse battery", Logins(str(tmp_path / "logins.json")))
    real = page_password._scrypt
    running, most = [0], [0]
    count = threading.Lock()

    def slow(*args, **kwargs):
        with count:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.02)
        try:
            return real(*args, **kwargs)
        finally:
            with count:
                running[0] -= 1

    monkeypatch.setattr(page_password, "_scrypt", slow)
    threads = [threading.Thread(target=password.check, args=("a wrong password",)) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert most[0] == 1


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

    # A logout with no cookie does nothing.
    logins.end(None)
    assert logins.check(second)

    logins.end_all()
    assert not logins.check(second)
    assert not Logins(path, now=clock).check(second)


def test_a_corrupt_logins_file_lets_nobody_in(tmp_path, caplog):
    path = tmp_path / "page_logins.json"
    for garbage in ("not json", "[1, 2]", '{"abc": "tomorrow"}', "\x00\xff",
                    json.dumps({"é" * 64: 2e9}), json.dumps({"g" * 64: 2e9})):
        path.write_text(garbage)
        logins = Logins(str(path))
        assert not logins.check("anything")
        logins.end("anything")
        token = logins.make()
        assert logins.check(token)
        assert token not in caplog.text
    assert "tomorrow" not in caplog.text


def test_a_login_that_would_never_end_is_refused(tmp_path):
    path = tmp_path / "page_logins.json"
    clock = Clock()
    for ends in ("Infinity", "NaN", str(clock.time + LOGIN_DAYS * 86400 + 1)):
        digest = hashlib.sha256(b"forever").hexdigest()
        path.write_text(f'{{"{digest}": {ends}}}')
        assert not Logins(str(path), now=clock).check("forever")
    # One that ends in time is still let in.
    path.write_text(f'{{"{digest}": {clock.time + LOGIN_DAYS * 86400}}}')
    assert Logins(str(path), now=clock).check("forever")


def test_logins_made_at_once_are_all_kept(tmp_path):
    logins = Logins(str(tmp_path / "page_logins.json"))
    tokens = []
    threads = [threading.Thread(target=lambda: tokens.append(logins.make())) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(tokens) == 20
    assert all(logins.check(token) for token in tokens)


def wrong() -> bool:
    return False


def right() -> bool:
    return True


def test_wrong_tries_are_per_device_with_a_backstop():
    clock = Clock()
    tries = WrongTries(now=clock)

    assert [tries.attempt("a", wrong) for _ in range(5)] == ["wrong"] * 5
    # Its next try isn't even checked.
    assert tries.attempt("a", right) == "waiting"
    assert tries.waiting("a")
    assert not tries.waiting("b")
    assert tries.attempt("b", right) == "right"

    clock.time += WAIT - 1
    assert tries.attempt("a", right) == "waiting"
    clock.time += 1
    assert not tries.waiting("a")

    # A right one resets the device's count.
    for _ in range(4):
        tries.attempt("a", wrong)
    assert tries.attempt("a", right) == "right"
    for _ in range(4):
        tries.attempt("a", wrong)
    assert not tries.waiting("a")

    # Many devices together hold every try, even a new device's.
    clock.time += WAIT
    for number in range(30):
        assert tries.attempt(f"device {number}", wrong) == "wrong"
    assert tries.attempt("device 0", right) == "waiting"
    assert tries.attempt("somewhere else", right) == "waiting"
    clock.time += WAIT - 1
    assert tries.waiting("somewhere else")
    clock.time += 1
    assert not tries.waiting("somewhere else")
    assert not tries.waiting("device 0")
    # Nothing is held for those addresses once a minute has passed with no tries.
    assert tries._devices == {}
    assert len(tries._recent) == 0


def test_the_backstop_counts_only_the_last_minute():
    clock = Clock()
    tries = WrongTries(now=clock)
    for number in range(29):
        tries.attempt(f"device {number}", wrong)
    clock.time += WAIT
    assert tries.attempt("another", wrong) == "wrong"
    assert not tries.waiting("somewhere else")


def test_right_ones_do_not_count_towards_the_backstop():
    tries = WrongTries(now=Clock())
    for number in range(29):
        tries.attempt(f"device {number}", wrong)
    assert tries.attempt("me", right) == "right"
    assert not tries.waiting("somewhere else")


def checks_run_at_once(tries: WrongTries, devices: list) -> int:
    """Makes a try from each device at once, each check slow, and counts the checks run."""
    run = []

    def check() -> bool:
        run.append(1)
        time.sleep(0.02)
        return False

    threads = [threading.Thread(target=tries.attempt, args=(device, check)) for device in devices]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return len(run)


def test_tries_at_once_from_one_device_get_only_five_checks():
    assert checks_run_at_once(WrongTries(now=Clock()), ["a"] * 20) == 5


def test_tries_at_once_from_many_devices_get_only_thirty_checks():
    assert checks_run_at_once(WrongTries(now=Clock()), [f"device {n}" for n in range(60)]) == 30
