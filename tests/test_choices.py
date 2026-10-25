"""Tests for ChoiceStore - choices kept in the data folder, whatever feature they belong to."""

import logging
import threading

import pytest

from voice_assistant import storage
from voice_assistant.choices import ChoiceStore

DEFAULTS = {"names": {}, "on": True}


def test_a_new_store_starts_with_the_defaults(tmp_path):
    store = ChoiceStore(str(tmp_path / "choices.json"), DEFAULTS)

    assert store.get("names") == {}
    assert store.get("on") is True
    store.change(lambda state: state["names"].update(a=1))
    assert DEFAULTS == {"names": {}, "on": True}


def test_a_file_that_cant_be_read_starts_with_the_defaults_and_says_so(tmp_path, caplog):
    path = tmp_path / "choices.json"
    path.write_text("{not json")

    with caplog.at_level(logging.WARNING):
        store = ChoiceStore(str(path), DEFAULTS)

    assert store.get("on") is True
    assert f"Couldn't read {path}, starting with the defaults" in caplog.text


def test_only_the_defaults_keys_are_kept(tmp_path):
    path = tmp_path / "choices.json"
    path.write_text('{"on": false, "stray": 1}')

    store = ChoiceStore(str(path), DEFAULTS)

    assert store.get("on") is False
    assert store.get("names") == {}
    assert "stray" not in store.state


def test_a_change_is_saved_and_read_back_by_a_new_store(tmp_path):
    path = str(tmp_path / "choices.json")
    ChoiceStore(path, DEFAULTS).change(lambda state: state["names"].update(a=1))

    assert ChoiceStore(path, DEFAULTS).get("names") == {"a": 1}


def test_a_change_that_cant_be_saved_changes_nothing(tmp_path, monkeypatch):
    store = ChoiceStore(str(tmp_path / "choices.json"), DEFAULTS)

    def failing(path, data, indent=None):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_json", failing)
    with pytest.raises(OSError):
        store.change(lambda state: state["names"].update(a=1))

    assert store.get("names") == {}


def test_keep_holds_a_change_in_memory_when_the_save_fails(tmp_path, monkeypatch, caplog):
    store = ChoiceStore(str(tmp_path / "choices.json"), DEFAULTS)

    def failing(path, data, indent=None):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_json", failing)
    with caplog.at_level(logging.WARNING):
        store.keep(lambda state: state["names"].update(a=1), "name")

    assert store.get("names") == {"a": 1}
    assert "Couldn't save the name: disk full" in caplog.text


def test_two_threads_changes_are_both_kept(tmp_path):
    path = str(tmp_path / "choices.json")
    store = ChoiceStore(path, DEFAULTS)

    def add(prefix):
        for i in range(50):
            store.change(lambda state: state["names"].update({f"{prefix}{i}": i}))

    threads = [threading.Thread(target=add, args=(p,)) for p in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.get("names")) == 100
    assert len(ChoiceStore(path, DEFAULTS).get("names")) == 100
