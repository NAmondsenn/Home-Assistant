# The voice check - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A wake counts only if the wake model hears "Hey Nova" and a small check trained on the user's voice agrees, so films stop waking Nova.

**Architecture:** `voice_assistant/voice_check.py` holds a logistic regression as plain numbers, loaded from JSON in the satellite's data folder. `WakeWordDetector` asks it on the moment the wake model fires. `scripts/train_voice_check.py` trains it on the Pi from the recordings and the wake clips. The brain sends the check's setting in the state message, as it does the wake setting.

**Tech Stack:** Python 3.13, numpy, openWakeWord 0.6 and scikit-learn (already installed on the satellite, used by the training script only), pytest.

**Spec:** `docs/decisions/0011-the-voice-check.md`; the user agreed the voice check on 21 Oct, recorded on 31 Oct and said yes to keeping clips; trial results are in the decision.

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, email address, token, recording or trained check in it.
- The check file: `<DATA_DIR>/voice_check.json`, 0600, written atomically. JSON with `version` 1, `model` ("hey_nova"), `frames` and `features` (the feature window's shape, 16 x 96 for hey_nova), `mean`, `scale`, `weights` (each frames x features long, flattened row by row), `bias`, `trained` (ISO date) and `counts` (clips of each kind). Its score is `1 / (1 + exp(-(weights . ((x - mean) / scale) + bias)))` on the flattened features - the same number scikit-learn's `predict_proba(...)[0][1]` gives for the pipeline openWakeWord's `train_verifier_model` builds.
- A missing file means no check: Nova wakes as today. A file which won't load (bad JSON, wrong shape, a non-finite number, another model) logs one warning and means no check. Nothing loads pickles.
- A wake counts when the wake score is at or above the wake setting and, with a check loaded and its setting above 0, the check's score on the same moment's features is at or above `thresholds.voice_check`. A wake the check stops is logged at INFO ("Heard 'Hey Nova' (0.81) but not in your voice (0.04), so not waking") and listening carries on, as if nothing was heard; nothing is played or sent.
- `thresholds.voice_check`: 0.2 in config.yaml, on the page under Listening as "How sure Nova must be it's your voice (0 for no check)", used straight away, the page accepts 0 to 0.95. The brain sends it as `voice_check` in the state message; the satellite takes an int or float (not bool), finite, from 0 to 0.99, else ignores it and keeps what it has (0.2 until told).
- Training (`scripts/train_voice_check.py`): positives are the recordings in `wake/hey_nova/` and the clips a labels file marks `pos`; negatives are `wake/not_hey_nova/` and clips marked `neg`; clips marked `drop` or not listed are left out. Positive features are the frames scoring 0.5 or more; negative features are every frame. It prints held-back results (as the trial did: rounds, a quarter of the clips held back each time, kept and stopped at 0.2, 0.3 and 0.5), then trains on everything and writes the file. It deletes nothing.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`. Tests on the Pi via `.superpowers/tools/pi_test.sh`, in London and UTC; ruff.

## Review Focus

1. **The user is never shut out by a fault:** a missing or bad file, or a setting of 0, wakes on the wake word alone.
2. **No pickle, and nothing of the user's voice in the repo.**
3. **The numbers match scikit-learn's,** and the features scored are the same moment's the wake model fired on.
4. **The wake path stays as fast:** the check adds only arithmetic.

---

### Task 1: The check, and training it

**Files:** create `voice_assistant/voice_check.py`, `scripts/train_voice_check.py`, `tests/test_voice_check.py`, `tests/test_train_voice_check.py`; modify `README.md` (one line beside `record_wake_samples.py`).

- `class VoiceCheck` with `score(features: np.ndarray) -> float`; `load(path) -> Optional[VoiceCheck]` (None for a missing file, None and one warning for a bad one); `from_pipeline(pipeline, model, counts) -> dict` and `save(data, path)` for the script.
- Tests: a pipeline trained by `train_verifier_model` on random features and its JSON give the same score to 1e-9; load rejects bad JSON, a wrong shape, NaN, another model, and a missing file returns None without a warning; save writes 0600 atomically; the script with fake feature extraction trains from a labels file, leaves out `drop` and unlisted clips, prints held-back results, writes the file, and deletes nothing.
- Commit: "Train a check of the user's voice, saved as plain numbers".

### Task 2: The satellite listens with the check

**Files:** modify `voice_assistant/wake_word.py`, `satellite.py` (`_take_state`), `tests/test_wake_word.py`, `tests/test_satellite.py`.

- `WakeWordDetector` loads the check from `<DATA_DIR>/voice_check.json` at start and has `voice_check_threshold` (0.2); in `listen_once`, a score at or above `sensitivity` is checked on `model.preprocessor.get_features(model.model_inputs[key])` at that moment; stopped wakes are logged and listening goes on.
- `_take_state` applies `voice_check` with the Global Constraints' check; INFO when it changes.
- Tests: with no file, as today; a check scoring under the setting doesn't wake and logs both scores; over it wakes; setting 0 wakes without asking the check; muted still hears nothing; the features passed are that moment's; `voice_check` values valid and invalid in the state message.
- Commit: "Wake only when it's the user's voice".

### Task 3: The setting, from the page to each satellite

**Files:** modify `config.yaml`, `voice_assistant/config.py`, `voice_assistant/settings_page.py`, `voice_assistant/session.py` (`send_state`), `voice_assistant/remote.py` (`set_state`), `voice_assistant/brain.py` (`apply_settings`), `voice_assistant/protocol.py` (docstring). Tests: `tests/test_session.py`, `tests/test_remote.py`, `tests/test_config.py`, `tests/test_settings_page.py`, `tests/test_brain.py`.

- `send_state` sends `voice_check` from config at each send, under the same per-session lock as `wake_confidence`. Saving it on the page sends states to every connected satellite.
- Tests: the value sent; the page refuses -0.1 and 0.96 and takes 0; a save re-sends states.
- Commit: "Send each satellite how sure to be it's the user's voice".
