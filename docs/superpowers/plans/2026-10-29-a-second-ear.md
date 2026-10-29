# A second speech model for what Nemotron misses - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Nemotron hears nothing after speech, ask a small offline model (Parakeet TDT 110m) first; keep the said-twice and "please" retries only for when it hears nothing too, or isn't there.

**Architecture:** `voice_assistant/parakeet.py` loads the model with sherpa-onnx's `OfflineRecognizer.from_transducer(..., model_type="nemo_transducer")`, once, at the brain's start. `NemotronSpeechToText` takes it as an optional fallback and tries it before `hear_again`'s retries. `setup.sh` (brain role) downloads and checks it as it does Nemotron.

**Tech Stack:** Python 3.13, sherpa-onnx 1.13.8 (already a brain dependency), pytest, bash.

**Spec:** the user, 29 Oct ("okay cool do it"), on `.superpowers/sdd/second-model-spike.md`: on the 54 recordings Nemotron left empty, the 110m got 52 right, 2 wrong, 0 empty, in 0.11 s (0.19 s at worst), against today's retries' 48/1/5 in 1.08 s; it invented nothing from noise; about 200 MB of memory. Already on the brain at `/home/nova/Home-Assistant/models/sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8` (sha256 f628312e9fdf8686374cb01a69425c41732529d540860311f16f37cbc32cfe9b, matched the release).

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, email address or token.
- `backend.fallback_model: "models/sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8"` in `config.yaml`, beside `backend.model`; empty switches it off. A missing or unloadable model logs one warning and Nova carries on with the retries - never a failed start.
- The fallback's answer goes through the same `HALLUCINATIONS` filter and wake-word stripping as Nemotron's. Only a raw '' after detected speech, of 3 s or less, reaches it (the rules Task 4 of the small fixes set).
- One recogniser shared across satellites' requests is safe only if each decode has its own stream; serialise calls if sherpa-onnx's offline recogniser isn't safe to share, and say which in a comment.
- Logged at INFO: "Heard nothing, so asked the second model: 'pause'". Timed into the latency record as its own step.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`. Tests on the Pi via `.superpowers/tools/pi_test.sh` (no sherpa-onnx there: fake it, as the Nemotron tests do), in London and UTC; ruff; `bash -n setup.sh update.sh`.

## Review Focus

1. **A brain without the model** (a fresh install before the download, a disk error) starts and works as today.
2. **The fallback never adds time to a request Nemotron heard.**
3. **Two satellites' empty results at once** don't corrupt each other.

---

### Task 1: The second model, tried first when Nemotron hears nothing

**Files:** create `voice_assistant/parakeet.py`; modify `voice_assistant/speech_to_text.py`, `voice_assistant/brain.py` (`_speech_to_text`), `config.yaml`, `setup.sh` (brain role: `PARAKEET_NAME`, `PARAKEET_URL`, `PARAKEET_SHA256`, a `download_parakeet` beside `download_nemotron`, called where it is), `README.md` (the brain's models), `docs/troubleshooting.md` (one line: the warning when it's missing). Tests: `tests/test_speech_to_text.py`, `tests/test_brain.py`, `tests/test_setup_script.py` or `tests/test_config.py` (wherever setup.sh's Nemotron download is tested).

- `class Parakeet(path: str)` with `transcribe(audio: np.ndarray) -> str` (16 kHz float32; '' for nothing).
- `NemotronSpeechToText(nemotron, please=None, fallback: Optional[Parakeet] = None)`. In the empty-after-speech path: the fallback first; its text, filtered and stripped, used if not ''; else `hear_again` as today. The step is named "fallback" in the latency record and the log.
- `_speech_to_text` loads it from `backend.fallback_model` (path under `paths.PROJECT_DIRECTORY`); on any failure, one warning ("The second speech model couldn't be loaded, so Nova will hear lone words again by itself: ...") and `fallback=None`.
- Tests: Nemotron '' then fallback "Pause." -> "Pause."; fallback '' -> the retries run; fallback raising -> the retries run, logged; Nemotron non-empty -> the fallback never called; fallback "Thank you." (a hallucination) -> treated as nothing; no fallback configured -> as today; `_speech_to_text` with a missing folder -> a warning and a working NemotronSpeechToText.
- setup.sh: the same download-check-unpack-remove as Nemotron, with the sha256 above, only for the brain role; `bash -n`.
- Commit: "Ask a second, small speech model when Nemotron hears nothing".
