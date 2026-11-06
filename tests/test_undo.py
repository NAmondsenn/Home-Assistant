"""
Undo Test
Checks what was removed on the settings page can be taken back once, and only for a
minute - with a clock of the test's own, so nothing here waits.
"""

import threading
import time

from voice_assistant.undo import MOST_KEPT, UNDO_KEPT, UndoShelf


def _put_back(kind, item):
    """An undo which works, answering with what it was given to put back."""
    return {"success": True, "message": "Put back.", "kind": kind, "item": item}


def test_an_undo_works_once_and_not_after_a_minute():
    now = [1000.0]
    shelf = UndoShelf(clock=lambda: now[0])
    alarm = {"id": 3, "hour": 7, "minute": 0}
    token = shelf.put("alarm", alarm)

    assert shelf.use(token, _put_back) == _put_back("alarm", alarm)
    assert shelf.use(token, _put_back) is None

    # Not once it's been kept a minute.
    late = shelf.put("timer", {"id": 1})
    now[0] += UNDO_KEPT + 1
    assert shelf.use(late, _put_back) is None

    # Nor by a brain restarted since, which has a shelf of its own.
    kept = shelf.put("timer", {"id": 2})
    assert UndoShelf(clock=lambda: now[0]).use(kept, _put_back) is None
    # Nor by anything which isn't a token.
    assert shelf.use("", _put_back) is None and shelf.use(None, _put_back) is None
    assert shelf.use("é", _put_back) is None
    assert shelf.use(kept, _put_back) == _put_back("timer", {"id": 2})


def test_the_old_ones_go_as_new_ones_are_put():
    now = [1000.0]
    shelf = UndoShelf(clock=lambda: now[0])
    shelf.put("timer", {"id": 1})
    now[0] += UNDO_KEPT + 1
    shelf.put("timer", {"id": 2})
    assert len(shelf._kept) == 1

    # However many come at once, only the latest MOST_KEPT are kept.
    tokens = []
    for number in range(MOST_KEPT + 10):
        now[0] += 0.01
        tokens.append(shelf.put("timer", {"id": number}))
    assert len(shelf._kept) == MOST_KEPT
    assert shelf.use(tokens[9], _put_back) is None
    assert shelf.use(tokens[10], _put_back) == _put_back("timer", {"id": 10})
    # Each token is new, and long enough not to be guessed.
    assert len({shelf.put("timer", {"id": 3}) for _ in range(20)}) == 20
    assert len(shelf.put("timer", {"id": 4})) >= 20


def test_the_old_ones_go_whenever_the_shelf_is_used_not_only_when_one_is_put():
    now = [1000.0]
    shelf = UndoShelf(clock=lambda: now[0])
    shelf.put("alarm", {"id": 1})
    shelf.put("timer", {"id": 2})
    now[0] += UNDO_KEPT + 1
    # Asked for something else altogether, or for nothing it has.
    assert shelf.use("not-a-token", _put_back) is None
    assert shelf._kept == {}

    # One still in time stays.
    old = shelf.put("alarm", {"id": 3})
    now[0] += UNDO_KEPT / 2
    young = shelf.put("alarm", {"id": 4})
    now[0] += UNDO_KEPT / 2 + 1
    assert shelf.use("not-a-token", _put_back) is None
    assert list(shelf._kept) == [young]
    assert shelf.use(old, _put_back) is None
    assert shelf.use(young, _put_back) == _put_back("alarm", {"id": 4})


def test_an_undo_refused_can_be_tried_again_until_its_minute_is_up():
    now = [1000.0]
    shelf = UndoShelf(clock=lambda: now[0])
    token = shelf.put("alarm", {"id": 3})
    clash = {"success": False, "message": "You already have a 7:00 AM alarm."}
    done = {"success": True, "message": "Put back the 7:00 AM alarm."}

    assert shelf.use(token, lambda kind, item: clash) == clash
    assert shelf.use(token, lambda kind, item: done) == done
    # Only once it's worked is it used up.
    assert shelf.use(token, lambda kind, item: done) is None

    late = shelf.put("alarm", {"id": 4})
    now[0] += UNDO_KEPT - 1
    assert shelf.use(late, lambda kind, item: clash) == clash
    now[0] += 2
    assert shelf.use(late, lambda kind, item: done) is None


def test_two_undos_at_once_put_back_once():
    shelf = UndoShelf()
    token = shelf.put("alarm", {"id": 3})
    both = threading.Barrier(2)
    put_back, answers = [], []

    def slowly(kind, item):
        put_back.append(item)
        # Long enough for the other tap to come in meanwhile.
        time.sleep(0.2)
        return {"success": True, "message": "Put back."}

    def tap():
        both.wait()
        answers.append(shelf.use(token, slowly))

    taps = [threading.Thread(target=tap) for _ in range(2)]
    for each in taps:
        each.start()
    for each in taps:
        each.join()
    assert put_back == [{"id": 3}]
    assert sorted(answers, key=lambda answer: answer is None) == [{"success": True, "message": "Put back."}, None]
