# Small fixes after the NPM login - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make giving a satellite a new password hard to do by mistake, reload a settings page left open across a restart, and close the small items parked by the last reviews.

**Architecture:** Page-side changes in `voice_assistant/web/settings.js` (and `.html`), one server-side field in `/api/state`, and small fixes in `settings_page.py`, `scripts/soak_monitor.py`, a test and a plan document. A fix for the empty "pause" transcript is added as its own task once the investigation (`.superpowers/sdd/pause-investigation.md`) names the cause.

**Tech Stack:** Python 3.13 standard library, pytest, the page's plain JavaScript (checked with Node).

**Spec:** the user, 27 Oct: "go fix everyhitng pls", after (1) pressing a satellite's "New password" while meaning the page password, which shut the Pi out for an hour - its confirm, "Give home-assistant a new password? It stops working until it has the new one.", didn't stop them; (2) that tab being a copy of the page from before a restart; (3) the parked items in `.superpowers/sdd/2026-10-27-settings-page-through-npm/progress.md` and `.superpowers/sdd/2026-10-25-clock-split-and-soak-prep/progress.md`. No design document; rulings against this are provisional.

## Global Constraints

- No new dependency. The repo is public: no hostname, IP address, email address or token in code, tests, docs or commits.
- The tailnet and NPM routes' security is unchanged: every change still needs `X-Nova` and a same-origin request; nothing new is served before a login.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`.
- Tests: `bash .superpowers/tools/pi_test.sh [args]`, again with `TZ_OVERRIDE=UTC`; ruff; `~/tools/node/bin/node --check voice_assistant/web/settings.js`, and the fake-DOM scenarios in the controller's scratchpad where the page changes.

## Review Focus

1. **The new-password question can't be clicked through by reflex**: it names the satellite, says Nova stops answering there, and needs the satellite's name typed. (Task 1.)
2. **A page reload never loses something half-typed** - a key, a setting, a routine being edited. (Task 2.)
3. **A brain that restarts every few seconds** (a crash loop) doesn't make the page reload in a loop. (Task 2.)

---

### Task 1: A new password for a satellite is asked for in earnest

**Files:** `voice_assistant/web/settings.js` (`newPassword`), `settings.html`/`settings.css` if a dialog of the page's own is needed; `tests/test_settings_page.py`.

- Replaces `confirm(...)` in `newPassword(name)` with the page's own question (a dialog or an inline panel, the builder's call), saying: "Give {name} a new password? Nova stops answering on {name} until you run `bash setup.sh --new-password` on it and paste the password shown next." and a field where the satellite's name must be typed before the button "Give it a new password" is enabled. Cancel is the default.
- Once given, the password is shown with those same instructions beside it, and a Copy button, as now.
- Tests: the Node scenarios - the button stays disabled until the name is typed exactly; Cancel sends nothing; `/api/satellites/password` is called once only after it. A string check in `test_settings_page.py` for the wording.
- Commit: "Ask for a satellite's name before giving it a new password, and say what it stops".

### Task 2: A page from before a restart reloads itself

**Files:** `voice_assistant/settings_page.py` (`_status`, or wherever `/api/state` gets `started`), `voice_assistant/web/settings.js`; tests.

- `/api/state` (and `/api/login-state`) gains `"page": <version>`, a short hash of the page's own files (`settings.html`, `settings.js`, `settings.css`) worked out once as the brain starts.
- The page keeps the version it loaded with. When a later answer has a different one, it reloads - at once if nothing is being typed or edited (no focused input, no open editor or dialog, no unsaved key), otherwise it shows "Nova has been updated - reload when you're ready" with a Reload button, and reloads when the editing ends.
- It reloads at most once a minute, so a brain in a crash loop can't make it loop.
- A restart with the same files doesn't reload.
- Tests: Python - `page` is in both answers and is the same across two `SettingsPage`s over the same files, different when a file differs; Node scenarios - reloads when it changes and nothing is focused; waits while a field is focused; at most one reload a minute; no reload for the same version.
- Commit: "Reload a settings page left open across an update".

### Task 3: The parked small items

**Files:** `voice_assistant/settings_page.py` (the refusal log's device key), `tests/test_settings_page.py` (`test_only_so_many_are_turned_away_at_once`), `scripts/soak_monitor.py` (`sample`), `docs/superpowers/plans/2026-10-25-clock-split-and-soak-prep.md` (its soak table).

- The device key from `X-Real-IP` is cut to 64 characters before it's used anywhere (the wrong-tries count and the once-a-minute log).
- `test_only_so_many_are_turned_away_at_once` waits on events (the turn-aways actually in flight), not on time, so a busy machine can't make it fail.
- `soak_monitor.sample` reads the log's size in a `try`, so a rotation between `exists` and `getsize` records 0 rather than ending the monitor.
- The plan document's soak table writes its greps with `grep -e ... -e ...`, not `\|`, so a line copied from the raw file works.
- Tests: the 64-character cut (a 10,000-character `X-Real-IP` holds no more than 64 characters); the monitor surviving a log that vanishes between the two calls (a fake `os.path.getsize` raising `FileNotFoundError`).
- Commit, one per item or as one: "Close the small items the last reviews parked".
