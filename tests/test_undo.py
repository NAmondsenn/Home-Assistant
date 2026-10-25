"""
Undo Test
Checks what was removed on the settings page can be taken back once, and only for a
minute - with a clock of the test's own, so nothing here waits.
"""

from voice_assistant.undo import UNDO_KEPT, UndoShelf


def test_an_undo_works_once_and_not_after_a_minute():
    now = [1000.0]
    shelf = UndoShelf(clock=lambda: now[0])
    alarm = {"id": 3, "hour": 7, "minute": 0}
    token = shelf.put("alarm", alarm)

    assert shelf.take(token) == ("alarm", alarm)
    assert shelf.take(token) is None

    # Not once it's been kept a minute.
    late = shelf.put("timer", {"id": 1})
    now[0] += UNDO_KEPT + 1
    assert shelf.take(late) is None

    # Nor by a brain restarted since, which has a shelf of its own.
    kept = shelf.put("timer", {"id": 2})
    assert UndoShelf(clock=lambda: now[0]).take(kept) is None
    # Nor by anything which isn't a token.
    assert shelf.take("") is None and shelf.take(None) is None and shelf.take("é") is None
    assert shelf.take(kept) == ("timer", {"id": 2})


def test_the_old_ones_go_as_new_ones_are_put():
    now = [1000.0]
    shelf = UndoShelf(clock=lambda: now[0])
    shelf.put("timer", {"id": 1})
    now[0] += UNDO_KEPT + 1
    shelf.put("timer", {"id": 2})
    assert len(shelf._kept) == 1
    # Each token is new, and long enough not to be guessed.
    assert len({shelf.put("timer", {"id": 3}) for _ in range(20)}) == 20
    assert len(shelf.put("timer", {"id": 4})) >= 20
