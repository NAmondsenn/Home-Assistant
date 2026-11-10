# Clock Display Look Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the clock display the look the user chose on the clock: his Home Assistant dashboard background, a tiled home page, a glowing blob for Nova, and the time on Now playing.

**Architecture:** The brain finds the dashboard background in Home Assistant's Lovelace configuration, fetches it at 512 pixels, keeps it in memory, and serves it behind the clock's key; the page fetches it like a cover (with the key, as a `blob:` URL) and sets it as the background. The rest is the page's HTML and CSS, following the two prototypes.

**Tech Stack:** Python standard library and the existing Home Assistant client and websocket; plain HTML, CSS and JavaScript.

**Spec:** `docs/decisions/0013-the-clock-display.md`, its last section "Revision, 10 November 2026: the look", with the prototypes `docs/decisions/0013-look-blob.html` and `docs/decisions/0013-look-home.html` (design E). Read all three before any task.

## Global Constraints

- Work in the worktree `<this worktree>` on branch `clock-look`. Never touch the main checkout. No `git stash`.
- Tests: `nice -n 19 timeout 900 bash <the clock-look test script> <pytest args>` (it copies this worktree to the Pi and runs pytest there). Check `/proc/loadavg` first and wait if the first number is above 3; the task's test file while working; one full run with `-x` before the commit; never two runs at once; no repeat runs. Ruff on the Pi afterwards. Node 22 for `node --check` only, niced.
- The page's CSP: no inline script or style, no `style=` attributes; setting `element.style` from JavaScript is fine; images may be `blob:`.
- Animations use `transform` and `opacity` only (no `filter`, `backdrop-filter`, animated `border-radius`, `preserve-3d` or animated box shadows): the clock's chip manages about 37 frames a second with the prototype's blob and far less otherwise.
- No personal data in the repository (no hosts, keys, his photo or his calendar).
- The user's code style: full-sentence comments in British spelling with spaced " - " dashes, why not what; Google-style docstrings; one commit per task ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; tests that fail on the old code.

## Review Focus

1. Home Assistant away, no dashboard background, a background that isn't an image, or a huge one: the page keeps its dark background and nothing raises.
2. The background route never fetches a path the clock chose, and needs the key like every other `/display/api/*` route.
3. The tiled home page with long event titles, no events, one event, no weather: nothing overflows its tile.
4. The blob appears and goes with Nova's state exactly as the orb did (listening, thinking, speaking, idle; muted mark when idle).
5. Frames on the clock: the controller measures the built page on the clock (about 35 frames a second or better).

---

### Task 1: The dashboard background, found in Home Assistant and served to the clock

**Files:**
- Modify: `voice_assistant/home_assistant.py` (or `voice_assistant/ha_registry.py`, wherever the websocket reads live) - finding the background
- Modify: `voice_assistant/display_server.py` - the route; `voice_assistant/display_feed.py` only if the background is cached there
- Test: `tests/test_home_assistant.py` (or `tests/test_ha_registry.py`), `tests/test_display_server.py`

**Interfaces:**
- Produces: a function that returns the dashboard background's Home Assistant path or None - the first dashboard (the default first, then `lovelace/dashboards/list`'s order) whose `lovelace/config` has `background.image`; `media-source://image_upload/<id>` becomes `/api/image/serve/<id>/512x512`; a string starting `/local/` is used as it is; a CSS `url(...)` string or `background` given as a plain string is read for such a path; anything else gives None. And a fetch of that path with Nova's token, allowing only those two kinds of path, at most 2 MB, image types only (jpeg, png, webp).
- Produces: `GET /display/api/background` behind the clock's key, answering the image (cached in memory, looked up again at most hourly, never per request) or 404 when there is none; the state carries `"background": <a short token that changes when the picture does, or null>` so the page fetches it again only on a change.

- [ ] **Step 1: Write the failing tests**: the lookup with a fake websocket answering two dashboards (the first without a background, the second with an `image_upload` one) gives `/api/image/serve/<id>/512x512`; `/local/x.jpg` passes through; a `url(/local/y.png)` string is read; an `https://` address, missing config, or a websocket error gives None. The route: 401 without the key; 404 when there's none; 200 with the right content type when there is; a second request within the hour doesn't fetch again; a non-image answer from Home Assistant gives 404; the state's `background` token changes when the image does.
- [ ] **Step 2: Run them**, expect failures.
- [ ] **Step 3: Implement**, following how the existing websocket client asks Home Assistant (look at `ha_registry.HomeSocket` and how `HomeAssistant.picture()` fetches with the token and checks size and type).
- [ ] **Step 4: Run the files, then the full suite with `-x`, then ruff.**
- [ ] **Step 5: Commit** "Find the dashboard's background in Home Assistant and serve it to the clock".

### Task 2: The page's new look

**Files:**
- Modify: `voice_assistant/web/display.html`, `voice_assistant/web/display.css`, `voice_assistant/web/display.js`
- Test: `tests/test_display_server.py` (only if a served file's name changes), `tests/web/` Node test only if the gesture module changes

**Interfaces:**
- Consumes: Task 1's `background` token in the state and `/display/api/background`.

- [ ] **Step 1: Background.** When the state's `background` token is new, fetch `/display/api/background` with the key, show it as a `blob:` URL behind every page (cover, centred) under the prototype's dark tint, revoking the old URL; on 404 or failure keep the dark background.
- [ ] **Step 2: Home page = design E** from `0013-look-home.html`: the four tiles and their contents at the same sizes and positions. Next = the first event not over (title, then "Today 11:30" / "Tomorrow 09:00" / "Wed 15:30" / "2 Dec"); Later = the next two or three, shortened with an ellipsis to fit; empty tiles say "Nothing coming up" in the muted colour; no weather shows "No weather". The old home layout goes.
- [ ] **Step 3: Nova = the blob** from `0013-look-blob.html` (the five lobes, the white core, the two aura layers, the veil, the listening / thinking / speaking rules, what Nova heard below at the prototype's position), in place of the orb and the sound wave, driven by the same state as before; the old orb and wave CSS and markup go.
- [ ] **Step 4: Now playing:** the time in the top right as in the prototype (26 px, bold, right 58 px, top 30 px), ticking with the page's clock.
- [ ] **Step 5: Check** with headless Chromium against a fake server at 800 x 480 (home, Coming up, Now playing, listening, speaking, a long event title, no events, no weather, no background), compare with the prototypes, and save the screenshots in the task's report folder. `node --check` each JS file; the full suite with `-x`; ruff.
- [ ] **Step 6: Commit** "Give the clock display the look he chose: his background, tiles and the blob".

### Task 3: Write it down

- [ ] Update `docs/clock-display.md`: the background comes from the HA dashboard (and how to change it); the hand checks gain the blob and the tiles. Commit "Say where the clock's background comes from".

### Final

- [ ] Whole-branch review (Opus) against the spec section and Review Focus; one fix wave if needed; full suite in London and UTC; then the controller merges into `satellite`, measures frames on the clock after deploying (on his named job), and restores Fully's real settings on the clock.
