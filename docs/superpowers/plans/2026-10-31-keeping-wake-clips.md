# Keeping a few seconds around each wake - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect real false wakes for the voice check to train on: when switched on, a satellite keeps 3 seconds of audio around each wake word, and nothing else.

**Architecture:** a small `WakeClips` on the satellite, used by `_woken`, reads the microphone's own buffer on a thread of its own and writes one private WAV per wake. Nothing changes on the brain: clips are labelled later by matching their times against the brain's latency log.

**Tech Stack:** Python 3.13, numpy, wave, pytest. No new dependency.

**Spec:** the user, 31 Oct ("yes"), on: "for a couple of days, the Pi keeps about 3 seconds of audio around each wake, and nothing else... The clips stay in the same private folder and get deleted after training". Why: the voice check's trial passed the user 22 of 22 and film and talk 0 of 36, but 5 minutes of Family Guy never reached 0.5 on the wake model, so it was never tested on what really wakes Nova, about 12 times a day.

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, email address or token.
- Off unless switched on: `wake_clips.keep: false` in `config.yaml`, read by the satellite only. `wake_clips.most: 400`: at that many clips it stops keeping and logs one warning. Nothing is deleted by Nova.
- Each clip is 2 s before the moment the wake word was heard and 1 s after, 16 kHz, 16-bit, mono WAV, in `~/.local/share/nova/voice-samples/wake/clips/`, folder 0700, files 0600, named `YYYYMMDD-HHMMSS-<score>.wav` from the wake's local time, e.g. `20261031-120501-0.73.wav`. Never sent anywhere.
- It can't hurt the satellite: nothing in it runs on the listening loop but handing over a position, any failure is logged and dropped, and a dead microphone holds up only its own daemon thread. A wake while a clip is being kept isn't kept.
- A muted microphone hears no wake, so keeps nothing. Only wakes keep anything: no audio is kept otherwise.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`. Tests on the Pi via `.superpowers/tools/pi_test.sh`, in London and UTC; ruff.

## Review Focus

1. **Nothing but those 3 seconds is kept,** and only when switched on.
2. **The wake path can't be slowed, blocked or broken** by a full disk, a bad folder or a dead microphone.
3. **Privacy:** modes on folder and files, including a folder which already exists.

---

### Task 1: Keep a clip around each wake

**Files:** create `voice_assistant/wake_clips.py`, `tests/test_wake_clips.py`; modify `satellite.py` (make it from config, call it from the wake), `config.yaml`, `tests/test_satellite.py`.

- `class WakeClips(microphone, folder=<default>, most=400)`, with `keep(heard_at: int, score: float)`, which returns at once. A daemon thread reads from `heard_at - 2 s` for 3 s through `microphone.listen(start=...).read(...)`, which gives int16 samples and waits for audio not yet arrived.
- `satellite.py`: only made when `wake_clips.keep` is true; the score `listen_once` returned reaches `keep`.
- Tests with a fake microphone: one wake writes one 3-second file with the right name, modes and audio; off makes none; a second wake while the first is being kept makes one file; at `most` nothing more is kept and one warning is logged; the folder made or set to 0700; a failing write, a full disk and a raising reader are logged and leave the next wake working; `keep` returns while the read is still blocked; the satellite calls it with the wake's position and score, and goes on when it raises.
- Commit: "Keep a few seconds around each wake, when asked to".
