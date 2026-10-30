# Claude's working-out, and recording "Hey Nova" - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop Nova reading Claude's working-out aloud, and give the user a one-command way to record what the voice check will be trained on.

**Architecture:** `LLMHandler` asks Claude with adaptive thinking at low effort, so reasoning goes into thinking blocks, which are never spoken. A new script, `scripts/record_wake_samples.py`, records through Nova's own microphone into a private folder, for a later plan to train openWakeWord's custom verifier on.

**Tech Stack:** Python 3.13, anthropic 0.120.2, numpy, pytest. No new dependency.

**Spec:** the user, 30 Oct: "some of the responses have been so long recently and still false wake words, i need to do the hey nova recordings i think", then "yes go" to fixing the working-out bug and building the script. Evidence: the brain's journal holds about 8 replies since 24 Oct beginning "The user said..." (15.8 s of audio for "Light" at 01:14 on 30 Oct). Trial through Nova's real code on the brain, 30 Oct, 36 replies per setup, 6 prompts:

| Setup | Reasoning spoken | Median first answer | Longest |
|---|---|---|---|
| thinking disabled (today) | 9 | 1.15 s | 407 chars |
| disabled + final-answer-only line | 7 | 1.2 s | 314 chars |
| disabled + that line + low effort | 5 | 1.2 s | 174 chars |
| adaptive thinking, low effort, max_tokens 1024 | 0 | 1.5 s | 134 chars |

The API's migration guide says the same: on Haiku 5.5 use effort as the lever, not `disabled`, and thinking counts toward `max_tokens`.

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, email address or token.
- Claude's request: `thinking: {"type": "adaptive"}`, `output_config: {"effort": <llm.effort>}`. `llm.effort` in `config.yaml`, default `"low"`. `llm.max_tokens` becomes 1024 and `llm.max_tokens_search` 1200, because thinking counts toward them. Thinking blocks are never spoken, logged as speech or stored in the thread, and go back to Claude unchanged within a turn's tool rounds.
- Recordings: 16 kHz, 16-bit, mono WAV, through `voice_assistant.microphone.Microphone`, so they're what the wake word model hears. In `~/.local/share/nova/voice-samples/wake/`, folder 0700, files 0600, never sent anywhere. Nothing trains or deletes here.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`. Tests on the Pi via `.superpowers/tools/pi_test.sh`, in London and UTC; ruff.

## Review Focus

1. **Nothing of a thinking block can be spoken or stored,** on any path: a plain reply, tool rounds, web search, `not_for_me`.
2. **A tool round still works with thinking on:** blocks go back unchanged, in order; earlier turns in the thread still send.
3. **An empty answer is handled:** thinking that uses up `max_tokens` leaves something sensible said, not silence.
4. **The script can't lose or leak a recording,** and stops cleanly part way.

---

### Task 1: Keep Claude's working-out out of what Nova says

**Files:** modify `voice_assistant/llm.py`, `voice_assistant/config.py` (if `llm.effort` needs a limit), `config.yaml`. Tests: `tests/test_llm.py`, `tests/test_config.py`.

- `LLMHandler` reads `llm.effort` (default "low"). `request_kwargs` sends adaptive thinking and `output_config`, in place of `{"type": "disabled"}`. Rewrite the comment above it: thinking used to be off for speed, Haiku 5.5 then wrote its working-out into the reply ("The user said 'Light', which is ambiguous..."), and a trial of 36 replies per setup found low effort leaked none for 0.4 s more. Keep the point that thinking counts toward `max_tokens`.
- `config.yaml`: `max_tokens: 1024`, `max_tokens_search: 1200`, `effort: "low"`, with comments saying why.
- A response with a thinking block and no text and no tool use (cut off by `max_tokens`) is said as the existing "nothing came back" path says it; log a warning with the stop reason. Find that path first; add one only if there's none.
- Tests: the request carries adaptive thinking and the effort; `llm.effort` is honoured; a thinking block then text speaks only the text; a thinking block never reaches `history`; a thinking block then `not_for_me` is silent; thinking + read tool: the next request's assistant content has the thinking block first and unchanged; thinking + no text + `stop_reason` "max_tokens" says something and warns.
- Commit: "Keep Claude's working-out out of what Nova says".

### Task 2: Record what the voice check needs

**Files:** create `scripts/record_wake_samples.py`, `tests/test_record_wake_samples.py`; modify `README.md` or `docs/troubleshooting.md` only if `scripts/record_samples.py` is listed there.

- Run with Nova stopped, as `record_samples.py` is. Three kinds, one per run:
  - `hey-nova --count N --folder-label close`: a blip through the speaker, then 3 s recorded, N times, about 5 s apart, no typing. Each take's best wake score is printed; a take scoring under 0.2 (nothing said, or too far) is taken again, at most twice more.
  - `film --seconds N`: records N seconds in one go, in 10-second files, for a film playing with nobody talking.
  - `talk --seconds N`: the same, for the user talking normally without the wake word.
- Files go to `wake/hey_nova/` and `wake/not_hey_nova/`, numbered on from what's there, so a run can be repeated or added to. The label goes in the file name (`close-007.wav`).
- Reuse `save_wav` from `record_samples.py`; the blip is `sounds/`' listening sound through the same output Nova uses. Ctrl-C keeps what's finished and says how many.
- Tests, with fakes for the microphone, model and sound: N takes make N files; numbering carries on; a low score is retaken at most twice more and still kept; file modes and folder mode; film splits into 10-second files; Ctrl-C part way keeps what's done.
- Commit: "Add a script to record what the voice check is trained on".
