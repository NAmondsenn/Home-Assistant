"""
Voice Check Test
Checks that the check saved as plain numbers scores just as the scikit-learn pipeline
openWakeWord trains does, that a file which won't do means no check rather than a
crash, and that it's saved readable by this user only and atomically - all on made-up
features. Needs numpy (as on the Pi), and openWakeWord's trainer with scikit-learn for
the test against the pipeline, and is skipped anywhere they aren't.
"""

import datetime
import json
import logging
import math
import os
import stat
import warnings

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import voice_check
from voice_assistant.voice_check import VoiceCheck

FRAMES, FEATURES = 16, 96
SIZE = FRAMES * FEATURES


def _trained_pipeline(seed=1):
    """A pipeline as openWakeWord's trainer makes, on made-up features in float32 as its own are."""
    trainer = pytest.importorskip("openwakeword.custom_verifier_model")
    rng = np.random.default_rng(seed)
    positives = rng.normal(0.3, 1, (40, FRAMES, FEATURES)).astype(np.float32)
    negatives = rng.normal(-0.3, 1, (80, FRAMES, FEATURES)).astype(np.float32)
    return trainer.train_verifier_model(np.vstack([positives, negatives]), np.array([1] * 40 + [0] * 80))


def _numbers(seed, low, high):
    return [float(value) for value in np.random.default_rng(seed).uniform(low, high, SIZE)]


MEAN, SCALE, WEIGHTS = _numbers(1, -1, 1), _numbers(2, 0.5, 2), _numbers(3, -0.05, 0.05)


def _check(**changes):
    """A made-up check's data, for hey_nova's 16 frames of 96 features, with any of it changed."""
    data = {"version": 1, "model": "hey_nova", "frames": FRAMES, "features": FEATURES,
            "mean": MEAN, "scale": SCALE, "weights": WEIGHTS, "bias": -0.3,
            "trained": "2026-11-02", "counts": {"hey_nova": 2, "not_hey_nova": 1, "pos": 3, "neg": 4}}
    data.update(changes)
    return data


def _with(numbers, value):
    """A list of the check's numbers with one of them changed."""
    return numbers[:5] + [value] + numbers[6:]


def _write(path, data):
    path.write_text(json.dumps(data))
    return str(path)


def test_the_saved_check_scores_just_as_the_pipeline_does(tmp_path):
    pipeline = _trained_pipeline()
    counts = {"hey_nova": 22, "not_hey_nova": 36, "pos": 28, "neg": 47}
    data = voice_check.from_pipeline(pipeline, "hey_nova", counts)
    path = str(tmp_path / "voice_check.json")
    voice_check.save(data, path)
    check = voice_check.load(path)

    rng = np.random.default_rng(2)
    for spread in (0.5, 1.5, 4.0):
        for _ in range(20):
            # Shaped as the wake model's features for a moment are.
            features = rng.normal(0, spread, (1, FRAMES, FEATURES)).astype(np.float32)
            assert check.score(features) == pytest.approx(pipeline.predict_proba(features)[0][1], abs=1e-9)


def test_the_saved_check_says_what_it_is(tmp_path):
    counts = {"hey_nova": 22, "not_hey_nova": 36, "pos": 28, "neg": 47}
    data = voice_check.from_pipeline(_trained_pipeline(), "hey_nova", counts)
    path = str(tmp_path / "voice_check.json")
    voice_check.save(data, path)

    with open(path) as file:
        saved = json.load(file)
    assert (saved["version"], saved["model"], saved["frames"], saved["features"]) == (1, "hey_nova", 16, 96)
    assert [len(saved[key]) for key in ("mean", "scale", "weights")] == [16 * 96] * 3
    assert isinstance(saved["bias"], float)
    assert datetime.date.fromisoformat(saved["trained"]) == datetime.date.today()
    assert saved["counts"] == counts


def test_scores_are_worked_out_from_the_formula(tmp_path):
    data = _check()
    check = voice_check.load(_write(tmp_path / "voice_check.json", data))
    features = np.random.default_rng(4).normal(0, 1, (1, FRAMES, FEATURES))

    z = sum(w * (x - m) / s for w, x, m, s in zip(data["weights"], features.reshape(-1), data["mean"], data["scale"]))
    assert check.score(features) == pytest.approx(1 / (1 + math.exp(-(z + data["bias"]))), abs=1e-12)


def test_a_score_far_either_way_is_0_or_1_without_overflowing():
    check = VoiceCheck(mean=np.zeros(2), scale=np.ones(2), weights=np.array([1.0, 1.0]), bias=0.0)
    assert check.score(np.array([1000.0, 1000.0])) == 1.0
    assert check.score(np.array([-1000.0, -1000.0])) == 0.0


def test_a_score_which_isnt_a_number_passes_with_one_warning_for_each_run_of_them(caplog):
    # Finite numbers which still overflow: x / 1e-320 is infinite, and the weights then
    # take infinity from infinity, which isn't a number.
    check = VoiceCheck(mean=np.zeros(2), scale=np.array([1e-320, 1e-320]), weights=np.array([1.0, -1.0]),
                       bias=0.0)
    broken, fine = np.array([1.0, 1.0]), np.array([0.0, 0.0])
    with warnings.catch_warnings(), caplog.at_level(logging.DEBUG, logger="voice_assistant.voice_check"):
        # numpy's own overflow warnings are kept quiet - one would be an error here.
        warnings.simplefilter("error")
        assert [check.score(features) for features in (broken, broken, fine, broken)] == [1.0, 1.0, 0.5, 1.0]
    assert [record.levelname for record in caplog.records] == ["WARNING", "WARNING"]


def test_features_of_another_size_are_refused():
    check = VoiceCheck(mean=np.zeros(6), scale=np.ones(6), weights=np.ones(6), bias=0.0)
    with pytest.raises(ValueError):
        check.score(np.zeros((1, 2, 4)))


def test_a_missing_file_is_no_check_and_no_warning(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG, logger="voice_assistant.voice_check"):
        assert voice_check.load(str(tmp_path / "voice_check.json")) is None
    assert caplog.records == []


@pytest.mark.parametrize("text", [
    "{not json",
    "",
    "null",
    "[1, 2, 3]",
    # Nested deeper than Python's json can follow.
    pytest.param("[" * 100000 + "]" * 100000, id="nested"),
    json.dumps(_check(version=2)),
    json.dumps(_check(version=True)),
    # Another wake word's check, which would be scoring features it never learnt from.
    json.dumps(_check(model="hey_jarvis")),
    json.dumps({key: value for key, value in _check().items() if key != "bias"}),
    # Wrong shapes: too few weights, a mean of the wrong length, frames which don't
    # match the lists, and a window which isn't the wake word model's, even with as
    # many numbers in all.
    json.dumps(_check(weights=WEIGHTS[:-1])),
    json.dumps(_check(mean=MEAN + [0.0])),
    json.dumps(_check(frames=17)),
    json.dumps(_check(frames=2, features=3, mean=MEAN[:6], scale=SCALE[:6], weights=WEIGHTS[:6])),
    json.dumps(_check(frames=32, features=48)),
    json.dumps(_check(features=0)),
    json.dumps(_check(frames="16")),
    json.dumps(_check(scale="1")),
    # Numbers which aren't: NaN and infinity as Python's json writes them, a string, true,
    # and whole numbers too big for a float.
    json.dumps(_check(weights=_with(WEIGHTS, float("nan")))),
    json.dumps(_check(mean=_with(MEAN, float("inf")))),
    json.dumps(_check(bias=float("nan"))),
    json.dumps(_check(bias="0.1")),
    json.dumps(_check(bias=True)),
    json.dumps(_check(bias=10 ** 400)),
    json.dumps(_check(weights=_with(WEIGHTS, "0.2"))),
    json.dumps(_check(weights=_with(WEIGHTS, True))),
    json.dumps(_check(weights=_with(WEIGHTS, 10 ** 400))),
    json.dumps(_check(scale=_with(SCALE, 10 ** 400))),
    # A scale of 0 would divide by 0, and scikit-learn's are never under 0.
    json.dumps(_check(scale=_with(SCALE, 0.0))),
    json.dumps(_check(scale=_with(SCALE, -2.0))),
])
def test_a_file_which_wont_do_is_no_check_and_one_warning(tmp_path, caplog, text):
    path = tmp_path / "voice_check.json"
    path.write_text(text)
    with caplog.at_level(logging.DEBUG, logger="voice_assistant.voice_check"):
        assert voice_check.load(str(path)) is None
    assert [record.levelname for record in caplog.records] == ["WARNING"]
    assert str(path) in caplog.records[0].getMessage()


def test_a_check_for_the_wake_word_asked_for_is_loaded(tmp_path):
    path = _write(tmp_path / "voice_check.json", _check(model="hey_jarvis"))
    assert voice_check.load(path, model="hey_jarvis") is not None
    assert voice_check.load(path, model="hey_nova") is None


def test_a_check_for_a_model_with_another_window_is_loaded_when_asked_for(tmp_path):
    data = _check(model="other", frames=8, mean=MEAN[:768], scale=SCALE[:768], weights=WEIGHTS[:768])
    path = _write(tmp_path / "voice_check.json", data)
    assert voice_check.load(path, model="other", frames=8) is not None
    assert voice_check.load(path, model="other") is None


def test_a_folder_in_its_place_is_no_check_and_one_warning(tmp_path, caplog):
    (tmp_path / "voice_check.json").mkdir()
    with caplog.at_level(logging.DEBUG, logger="voice_assistant.voice_check"):
        assert voice_check.load(str(tmp_path / "voice_check.json")) is None
    assert [record.levelname for record in caplog.records] == ["WARNING"]


@pytest.mark.skipif(os.name != "posix", reason="file permissions are a Unix idea")
def test_it_is_saved_readable_by_this_user_only(tmp_path):
    path = str(tmp_path / "data" / "voice_check.json")
    voice_check.save(_check(), path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with open(path) as file:
        assert json.load(file) == _check()


def test_a_failed_save_keeps_the_last_check_and_leaves_nothing_behind(tmp_path, monkeypatch):
    path = str(tmp_path / "voice_check.json")
    voice_check.save(_check(), path)

    def fsync(descriptor):
        raise OSError("the disk's full")

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError):
        voice_check.save(_check(bias=0.7), path)
    monkeypatch.undo()
    assert os.listdir(tmp_path) == ["voice_check.json"]
    with open(path) as file:
        assert json.load(file)["bias"] == -0.3


def test_a_check_which_wouldnt_load_isnt_saved(tmp_path):
    path = str(tmp_path / "voice_check.json")
    with pytest.raises(ValueError):
        voice_check.save(_check(bias=float("nan")), path)
    assert os.listdir(tmp_path) == []
