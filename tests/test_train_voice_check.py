"""
Train Voice Check Test
Checks the script which trains the voice check takes the right recordings and clips,
and only the right moments of each, holds back clips as it should to say how the check
does, writes a check which loads - and deletes nothing. The wake word model is faked and
the features are made up: no recording is ever read. Needs numpy (as on the Pi), and
openWakeWord's trainer with scikit-learn where the training is real, and is skipped
anywhere they aren't.
"""

import datetime
import importlib.util
import os
import stat
import types
import zlib

import pytest

np = pytest.importorskip("numpy")

from voice_assistant import paths, voice_check

spec = importlib.util.spec_from_file_location(
    "train_voice_check", os.path.join(os.path.dirname(__file__), "..", "scripts", "train_voice_check.py"))
train = importlib.util.module_from_spec(spec)
spec.loader.exec_module(train)

RATE = 16000
FRAMES, FEATURES = 16, 96

# How each made-up file's wake scores go, 80ms at a time: the user's reach 0.5 or more
# as "Hey Nova" is said, and the others' do too, now and then, as a film's would.
USER_SCORES = [0.1, 0.3, 0.8, 0.9, 0.6, 0.2]
OTHER_SCORES = [0.1, 0.7, 0.2, 0.55, 0.1, 0.3]


def _features(level, seed):
    return np.random.default_rng(seed).normal(level, 0.3, (FRAMES, FEATURES)).astype(np.float32)


class Extract:
    """Stands in for scoring a file with the wake model: made-up steps for each file, by its name."""

    def __init__(self, users):
        # The names of the files which are the user saying "Hey Nova".
        self.users = set(users)
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        name = os.path.basename(path)
        seed = zlib.crc32(name.encode())
        if name in self.users:
            # Only the moments the wake model fired on are the user's voice - the rest is
            # the room before and after.
            return [(score, _features(1.0 if score >= 0.5 else 0.0, seed + i)) for i, score in enumerate(USER_SCORES)]
        return [(score, _features(-1.0, seed + i)) for i, score in enumerate(OTHER_SCORES)]


def _make(folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        # Not a real recording - nothing here is ever read but by Extract, which goes by the name.
        (folder / name).write_bytes(b"made up " + name.encode())


def _wake_folder(tmp_path, pos=8, neg=12):
    """A wake folder with recordings and clips, and a labels file for the clips. Returns the
    folder, the labels file, and the names of the user's files."""
    wake = tmp_path / "wake"
    hey_nova = [f"close-{n:03d}.wav" for n in range(1, 4)]
    not_hey_nova = [f"film-{n:03d}.wav" for n in range(1, 4)]
    requests = [f"2026-11-01-10{n:02d}-0.8{n % 10}.wav" for n in range(pos)]
    false_wakes = [f"2026-11-01-20{n:02d}-0.6{n % 10}.wav" for n in range(neg)]
    _make(wake / "hey_nova", hey_nova)
    _make(wake / "not_hey_nova", not_hey_nova)
    _make(wake / "clips", requests + false_wakes + ["dropped.wav", "unlisted.wav"])
    labels = tmp_path / "labels.txt"
    labels.write_text("".join(f"{name} pos\n" for name in requests) + "\n"
                      + "".join(f"{name} neg\n" for name in false_wakes) + "dropped.wav drop\n")
    return wake, labels, hey_nova + requests


def _everything(folder):
    """Every file under a folder, and what's in it."""
    found = {}
    for root, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)
            with open(path, "rb") as file:
                found[os.path.relpath(path, folder)] = file.read()
    return found


class FakeModel:
    """
    Stands in for openWakeWord's Model, scoring each 80ms of sound as told - silence
    scores 0 - and keeping what it was given. Like the real one, a reset fills what it
    remembers with random noise, and the features of a moment are the last 16 80ms it
    heard, each marked with the first sample of its 80ms.
    """

    def __init__(self, scores):
        self.scores = list(scores)
        self.calls = []
        self.model_inputs = {"hey_nova": FRAMES}
        self.preprocessor = self

    def reset(self):
        self.calls.append("reset")
        self.heard = list(np.random.default_rng().uniform(-1000, 1000, FRAMES))

    def predict(self, chunk):
        self.calls.append(("sound" if chunk.any() else "silence", len(chunk)))
        self.heard = self.heard[1:] + [float(chunk[0])]
        return {"hey_nova": self.scores.pop(0) if chunk.any() else 0.0}

    def get_features(self, frames):
        return np.repeat(np.array(self.heard[-frames:], dtype=np.float32)[:, None], FEATURES, axis=1)[None]


def test_the_labels_are_read(tmp_path):
    labels = tmp_path / "labels.txt"
    labels.write_text("a.wav pos\n\n  b.wav   neg  \nc.wav drop\n")
    assert train.read_labels(str(labels)) == {"a.wav": "pos", "b.wav": "neg", "c.wav": "drop"}


@pytest.mark.parametrize("text, wanted", [
    ("a.wav pos\nb.wav maybe\n", "line 2"),
    ("a.wav\n", "line 1"),
    ("a.wav pos extra\n", "line 1"),
    ("a.wav pos\na.wav neg\n", "a.wav"),
])
def test_labels_which_dont_make_sense_are_refused(tmp_path, text, wanted):
    labels = tmp_path / "labels.txt"
    labels.write_text(text)
    with pytest.raises(ValueError, match=wanted):
        train.read_labels(str(labels))


def test_each_80ms_is_scored_after_a_reset_and_2_seconds_of_silence_with_that_moments_features():
    model = FakeModel([0.1, 0.7, 0.4])
    # Three whole chunks, each a different level, and a part chunk left over.
    audio = np.concatenate([np.full(1280, level, dtype=np.int16) for level in (1, 2, 3)] + [np.zeros(500, np.int16)])

    steps = train.steps(audio, model, "hey_nova")

    # Silence first, as the satellite has heard the room before anything can wake it,
    # and none of it is a step.
    assert model.calls == ["reset"] + [("silence", 1280)] * (2 * RATE // 1280) + [("sound", 1280)] * 3
    assert [score for score, _ in steps] == [0.1, 0.7, 0.4]
    assert [features.shape for _, features in steps] == [(FRAMES, FEATURES)] * 3
    assert [features[-1, 0] for _, features in steps] == [1, 2, 3]
    # The first step's moment has silence before it, not what the reset left.
    assert list(steps[0][1][:-1, 0]) == [0] * (FRAMES - 1)


def test_a_file_scored_twice_gives_the_same_features_every_time():
    audio = np.concatenate([np.full(1280, level, dtype=np.int16) for level in (1, 2, 3)])
    first = train.steps(audio, FakeModel([0.1, 0.7, 0.4]), "hey_nova")
    second = train.steps(audio, FakeModel([0.1, 0.7, 0.4]), "hey_nova")
    for (_, one), (_, other) in zip(first, second):
        assert np.array_equal(one, other)


def test_a_wav_is_read_as_16_bit_samples(tmp_path):
    from record_samples import save_wav

    path = str(tmp_path / "close-001.wav")
    save_wav(path, np.full(RATE, 0.25, dtype=np.float32))
    audio = train.read_wav(path)
    assert audio.dtype == np.int16
    assert len(audio) == RATE and audio[0] == 8192


def test_a_wav_at_another_rate_is_refused(tmp_path):
    import wave

    path = str(tmp_path / "phone.wav")
    with wave.open(path, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\0\0" * 800)
    with pytest.raises(ValueError, match="phone.wav"):
        train.read_wav(path)


def test_positives_are_only_the_moments_scoring_0_5_and_negatives_every_moment():
    def step(score, level):
        return score, np.full((FRAMES, FEATURES), level, dtype=np.float32)

    positives = [[step(0.2, 1), step(0.5, 2), step(0.9, 3)], [step(0.49, 4), step(0.7, 5)]]
    negatives = [[step(0.1, 6), step(0.8, 7)], [step(0.0, 8)]]

    features, labels = train.training_set(positives, negatives)

    assert [features[i][0, 0] for i in range(len(features))] == [2, 3, 5, 6, 7, 8]
    assert list(labels) == [1, 1, 1, 0, 0, 0]


def test_with_no_moment_of_hey_nova_at_0_5_it_says_so():
    quiet = [[(0.3, np.zeros((FRAMES, FEATURES), dtype=np.float32))]]
    with pytest.raises(ValueError, match="0.5"):
        train.training_set(quiet, quiet)


def test_it_trains_from_the_labels_says_how_held_back_clips_did_and_writes_the_check(tmp_path):
    pytest.importorskip("openwakeword.custom_verifier_model")
    wake, labels, users = _wake_folder(tmp_path)
    before = _everything(tmp_path)
    check_path = str(tmp_path / "data" / "voice_check.json")
    extract = Extract(users)
    shown = []

    data = train.train(str(wake), str(labels), check_path, "hey_nova", extract, rounds=3, show=shown.append)

    # Every recording and every labelled clip was scored, and nothing else.
    scored = sorted(os.path.relpath(path, wake) for path in extract.paths)
    assert "clips/dropped.wav" not in scored and "clips/unlisted.wav" not in scored
    assert len(scored) == 3 + 3 + 8 + 12
    assert data["counts"] == {"hey_nova": 3, "not_hey_nova": 3, "pos": 8, "neg": 12}

    # A quarter of each kind of clip held back each round - 2 requests and 3 false wakes.
    assert "Held back a quarter of the clips 3 times - 2 requests and 3 false wakes each time:" in shown
    for setting in ("0.2", "0.3", "0.5"):
        assert f"  at {setting}, kept 6 of 6 requests (100%) and stopped 9 of 9 false wakes (100%)" in shown
    assert not any("never scored" in line for line in shown)
    assert any("1 marked drop and 1 not listed" in line for line in shown)

    # The check written loads, and tells the user's voice from the rest.
    assert stat.S_IMODE(os.stat(check_path).st_mode) == 0o600
    check = voice_check.load(check_path)
    assert check.score(_features(1.0, 1)[None]) > 0.5
    assert check.score(_features(-1.0, 2)[None]) < 0.5

    # Nothing it read was deleted, or changed - the check is all that's new.
    after = _everything(tmp_path)
    after.pop(os.path.relpath(check_path, tmp_path))
    assert after == before


class FakeFit:
    """Stands in for openWakeWord's trainer, keeping what each round was trained on - its check gives every moment one score."""

    def __init__(self, score=1.0):
        self.trained = []
        self.score = score

    def __call__(self, features, labels):
        # Each made-up moment is filled with its file's number, so it can be told whose it was.
        self.trained.append({int(moment[0, 0]) for moment in features})
        return self

    def predict_proba(self, features):
        return np.array([[1 - self.score, self.score]] * len(features))

    def __getitem__(self, index):
        # Indexed as a pipeline is, for the check saved at the end: its scaler, then its regression.
        size = FRAMES * FEATURES
        return [types.SimpleNamespace(mean_=np.zeros(size), scale_=np.ones(size)),
                types.SimpleNamespace(coef_=np.zeros((1, size)), intercept_=np.zeros(1),
                                      classes_=np.array([0, 1]))][index]


def test_each_round_trains_on_every_recording_and_all_but_the_clips_held_back(tmp_path):
    wake, labels, users = _wake_folder(tmp_path)
    numbers = {}

    def extract(path):
        # Each file's moments filled with its own number, all scoring 0.9.
        number = numbers.setdefault(os.path.relpath(path, wake), len(numbers))
        return [(0.9, np.full((FRAMES, FEATURES), number, dtype=np.float32))]

    fit = FakeFit()
    train.train(str(wake), str(labels), str(tmp_path / "voice_check.json"), "hey_nova", extract, rounds=4,
                show=lambda line: None, fit=fit)

    recordings = {number for name, number in numbers.items() if not name.startswith("clips")}
    requests = {number for name, number in numbers.items() if name.startswith("clips/2026-11-01-10")}
    false_wakes = {number for name, number in numbers.items() if name.startswith("clips/2026-11-01-20")}
    # Four rounds, then everything.
    assert len(fit.trained) == 5
    for trained in fit.trained[:4]:
        assert recordings <= trained
        assert len(requests - trained) == 2 and len(false_wakes - trained) == 3
    assert fit.trained[4] == set(numbers.values())
    # Not the same clips held back each round.
    assert len({frozenset(set(numbers.values()) - trained) for trained in fit.trained[:4]}) > 1


def test_a_clip_is_kept_only_if_a_moment_at_0_5_passes_the_check():
    def step(score, level):
        return score, np.full((FRAMES, FEATURES), level, dtype=np.float32)

    class ByLevel:
        # The check's score is the moment's level.
        def predict_proba(self, features):
            return np.array([[1 - moment[0, 0], moment[0, 0]] for moment in features])

    # Its best moment at 0.5 or more is 0.25, and the 0.9 is a moment the wake model didn't fire on.
    steps = [step(0.6, 0.1), step(0.3, 0.9), step(0.8, 0.25)]
    assert train.best_check_score(ByLevel(), steps) == pytest.approx(0.25)
    assert train.best_check_score(ByLevel(), [step(0.3, 0.9)]) is None


def test_a_labelled_clip_which_isnt_there_is_refused(tmp_path):
    wake, labels, users = _wake_folder(tmp_path)
    labels.write_text(labels.read_text() + "gone.wav pos\n")
    with pytest.raises(ValueError, match="gone.wav"):
        train.train(str(wake), str(labels), str(tmp_path / "voice_check.json"), "hey_nova", Extract(users),
                    show=lambda line: None, fit=FakeFit())
    assert not os.path.exists(tmp_path / "voice_check.json")


def test_training_twice_on_the_same_files_gives_the_same_results(tmp_path, monkeypatch):
    pytest.importorskip("openwakeword.custom_verifier_model")
    # Either side of midnight, which is all that may differ.
    days = iter([datetime.date(2026, 11, 2), datetime.date(2026, 11, 3)])
    monkeypatch.setattr(voice_check, "datetime", types.SimpleNamespace(date=types.SimpleNamespace(
        today=lambda: next(days))))
    wake, labels, users = _wake_folder(tmp_path)
    runs = []
    for _ in range(2):
        shown = []
        data = train.train(str(wake), str(labels), str(tmp_path / "voice_check.json"), "hey_nova", Extract(users),
                           rounds=3, show=shown.append)
        assert data.pop("trained")
        runs.append((shown, data))
        # Each run from no check, as the first was, so neither keeps one as the last.
        os.remove(tmp_path / "voice_check.json")
    assert runs[0] == runs[1]


def test_held_back_clips_which_never_reach_0_5_again_are_counted_on_their_own(tmp_path):
    wake, labels, users = _wake_folder(tmp_path)

    def extract(path):
        # The requests reach 0.5 when scored again, and the false wakes never do.
        score = 0.9 if os.path.basename(path) in users else 0.3
        return [(score, np.zeros((FRAMES, FEATURES), dtype=np.float32))]

    shown = []
    train.train(str(wake), str(labels), str(tmp_path / "voice_check.json"), "hey_nova", extract, rounds=4,
                show=shown.append, fit=FakeFit())

    # 2 requests and 3 false wakes held back each round, for 4 rounds - and FakeFit
    # passes everything.
    assert "  at 0.2, kept 8 of 8 requests (100%) and stopped 0 of 0 false wakes" in shown
    assert "  0 requests and 12 false wakes held back never scored 0.5 or more again, so they're left out" in shown


def test_the_last_check_is_kept_beside_the_new_one(tmp_path):
    # So a new check which turns out worse can be swapped back by hand.
    wake, labels, users = _wake_folder(tmp_path)
    check_path = tmp_path / "data" / "voice_check.json"
    check_path.parent.mkdir()
    check_path.write_text('{"made": "up"}')
    os.chmod(check_path, 0o644)
    shown = []

    train.train(str(wake), str(labels), str(check_path), "hey_nova", Extract(users), rounds=2, show=shown.append,
                fit=FakeFit())

    previous = tmp_path / "data" / "voice_check.json.previous"
    assert previous.read_text() == '{"made": "up"}'
    assert stat.S_IMODE(os.stat(previous).st_mode) == 0o600
    assert voice_check.load(str(check_path)) is not None
    assert f"Saved the check as {check_path}, and kept the last one as {previous}. Restart Nova to use it." in shown


def test_with_no_check_before_there_is_nothing_kept(tmp_path):
    wake, labels, users = _wake_folder(tmp_path)
    check_path = tmp_path / "voice_check.json"
    shown = []
    train.train(str(wake), str(labels), str(check_path), "hey_nova", Extract(users), rounds=2, show=shown.append,
                fit=FakeFit())
    assert not os.path.exists(str(check_path) + ".previous")
    assert f"Saved the check as {check_path}. Restart Nova to use it." in shown


def test_a_check_which_stops_the_users_held_back_requests_isnt_saved(tmp_path):
    # A labels file with pos and neg swapped, say - installed, it would shut the user
    # out at the next restart of any kind.
    wake, labels, users = _wake_folder(tmp_path)
    check_path = tmp_path / "voice_check.json"
    check_path.write_text('{"made": "up"}')
    with pytest.raises(ValueError, match=r"kept only 0 of 8 held-back requests \(0%\) at 0.2.*--force"):
        train.train(str(wake), str(labels), str(check_path), "hey_nova", Extract(users), rounds=4,
                    show=lambda line: None, fit=FakeFit(score=0.1))
    assert check_path.read_text() == '{"made": "up"}'
    assert not os.path.exists(str(check_path) + ".previous")


def test_with_too_few_clips_to_tell_it_isnt_saved(tmp_path):
    wake, labels, users = _wake_folder(tmp_path, pos=3)
    check_path = tmp_path / "voice_check.json"
    with pytest.raises(ValueError, match="no held-back requests.*--force"):
        train.train(str(wake), str(labels), str(check_path), "hey_nova", Extract(users), rounds=4,
                    show=lambda line: None, fit=FakeFit())
    assert not check_path.exists()


def test_forced_it_is_saved_anyway_and_says_so(tmp_path):
    wake, labels, users = _wake_folder(tmp_path)
    check_path = tmp_path / "voice_check.json"
    shown = []
    train.train(str(wake), str(labels), str(check_path), "hey_nova", Extract(users), rounds=4, show=shown.append,
                fit=FakeFit(score=0.1), force=True)
    assert voice_check.load(str(check_path)) is not None
    assert ("Kept only 0 of 8 held-back requests (0%) at 0.2, under 90%, but saving the check anyway, "
            "as --force says.") in shown


def test_the_labels_file_is_beside_the_clips_unless_another_is_given():
    # Never in the repository's checkout, which is public: it says when the user talked to Nova.
    assert train.LABELS_FILE == os.path.join(paths.DATA_DIR, "voice-samples", "wake", "labels.txt")
    arguments = train.parse_arguments([])
    assert (arguments.labels, arguments.force) == (train.LABELS_FILE, False)
    arguments = train.parse_arguments(["elsewhere.txt", "--force"])
    assert (arguments.labels, arguments.force) == ("elsewhere.txt", True)
