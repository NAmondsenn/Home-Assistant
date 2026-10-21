"""
Storage Test
The atomic JSON save shared by timers, alarms, volumes and memory.
"""

import os
import stat

import pytest

from voice_assistant import storage


def test_what_is_saved_is_read_back(tmp_path):
    path = str(tmp_path / "data" / "timers.json")
    storage.save_json(path, {"next_id": 2, "timers": [{"id": 1}]})
    assert storage.load_json(path) == {"next_id": 2, "timers": [{"id": 1}]}


def test_a_missing_file_gives_the_default(tmp_path):
    assert storage.load_json(str(tmp_path / "volumes.json"), default={}) == {}


def test_a_broken_file_is_left_to_the_caller(tmp_path):
    path = tmp_path / "alarms.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        storage.load_json(str(path))


def test_a_failed_save_keeps_the_previous_file_and_leaves_nothing_behind(tmp_path):
    path = str(tmp_path / "timers.json")
    storage.save_json(path, {"timers": [1]})
    # A set can't be written as JSON, so this fails part way through writing.
    with pytest.raises(TypeError):
        storage.save_json(path, {"timers": {1}})
    assert storage.load_json(path) == {"timers": [1]}
    assert os.listdir(tmp_path) == ["timers.json"]


@pytest.mark.skipif(os.name != "posix", reason="file permissions are a Unix idea")
def test_files_are_readable_by_this_user_only(tmp_path):
    path = str(tmp_path / "memory.json")
    storage.save_json(path, {"facts": ["You work at Tesco."]}, indent=1)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="folders can only be flushed on Unix")
def test_the_folder_is_flushed_after_the_rename(tmp_path, monkeypatch):
    # Without it, a power cut just after saving could bring the old file back on ext4.
    flushed = []
    real_fsync = os.fsync

    def fsync(descriptor):
        flushed.append("folder" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    storage.save_json(str(tmp_path / "volumes.json"), {"general": 0.5})
    assert flushed == ["file", "folder"]
