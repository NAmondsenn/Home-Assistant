# AI usage dashboard

12 November 2026. Agreed in chat with Nathaniel; mockup (option A, side by side) at https://claude.ai/artifact/9vzfn4BgpGh9otByGqHJfC.

## Why

He wants one clean, good-looking place to see what his AI use costs and how much of it there is: Claude Code (on his subscription) and Nova (on the API, Haiku 5.5, paid per token). Today the Claude Code side is four plain tiles in Home Assistant, and nothing records what Nova spends at all; the only figure is Anthropic's Console bill. He wants "the full lot so I can see everything at once", with good functionality, not just a number or two.

Success: he opens one view in Home Assistant and can see, at a glance, how close Claude Code is to its limits, how many tokens each AI used over 24 hours, 7 days and 30 days, what Nova cost, which models the usage came from, and where it went. And he hears about it if Nova's spend runs away.

## Goals

1. A new view in Home Assistant, laid out like mockup option A: a Claude Code column on the left and a Nova column on the right, in his glass theme.
2. Claude Code column, top to bottom:
   - 5-hour and weekly limit meters, each with its reset time (these exist already).
   - Tokens written over the last 24 hours, 7 days and 30 days, rolling windows, each with its "with cache" total underneath.
   - What those 30 days would have cost at API prices.
   - A chart of output tokens with a 24 h / 7 days / 30 days switch.
   - Tokens by project over the last 7 days (top few).
3. Nova column, top to bottom:
   - Spent today and this month, with a projected total for the month.
   - Tokens and cost over the last 24 hours, 7 days and 30 days.
   - A spend chart with the same 24 h / 7 days / 30 days switch, with the daily alert line drawn on it.
   - This month's spend by kind of request (chat and questions, lights and devices, routines, music, web search, and so on).
4. Bottom right, under the Nova column: a "By model" card covering both AIs: a share bar, then one row per model with which AI used it, its tokens written and its share, with the same period switch. His request: "add models and how much each model has contributed to my usage".
5. Nova records every Claude API call it makes, so the above has real numbers.
6. An ntfy alert when Nova's spend for the day passes a limit he sets, at most once a day.

## Non-goals

- Homepage tiles. He said "HA is priority"; Homepage is not part of this.
- Counting Claude Code sessions on the PC or the MacBook. About 90% of his use is on CT 109. The limit meters already cover every device, because the limits are per account.
- Pulling the real bill from Anthropic's Console or Admin API. Nova's cost is worked out from token counts and prices; the Console stays the official figure.
- Any new service or container (memory: homelab is complete; build on HA, Nova and the existing exporter).
- The phone dashboard and the rack screen. Desktop dashboard only for now.

## Anti-goals (would be a failure even if it works)

- Anything Nova was asked, or answered, reaching Home Assistant. Decision 0005 says nothing anyone says to Nova, and none of its answers, goes to HA. Counts, costs, model names and coarse kinds of request are fine; text never is.
- Hanging CT 109. It hung twice under load (2-3 Nov). The exporter must stay light: scanning 30 days of transcripts (about 530 MB of logs today) has to be incremental, not a full reread every minute.
- Making Nova slower. Recording a call must not add latency to the reply.
- A dashboard full of default HA cards that look nothing like the rest of his dashboards.
- A cache total that looks alarming and means nothing. Tokens written leads; the with-cache total is secondary text.

## Constraints

- British spelling, no em dashes, no emojis, in code comments, UI text and commits. Match each repo's existing comment style (Nova's long plain-English docstrings; the exporter's short comments).
- Anything he may want to change later goes on Nova's settings page, not in code or files (memory: settings-page-and-ha-first). For this: the daily alert limit, the exchange rate, and Nova's per-model prices.
- Nova uses HA for anything HA can do; Nova's data reaches HA over MQTT discovery on the existing broker with the existing `voice_assistant/ha_device.py` code, not a new route.
- CT 109 load limits: nice, timeout, single worker, one test suite at a time, no repeat runs (memory: ct109-load-limits).
- The brain (CT 107) has an 8 GB root disk. Whatever Nova keeps must be bounded (compare `timeline.py`, which rotates at 5 MB).
- HA dashboards are storage-mode: edit `ha-dashboard/config.yaml`, save over the websocket, read back, then commit (see `ha-dashboard/README.md` and memory rack-screen). Lab writes need him to name the exact job (memory: auto-mode-named-jobs). Ask before restarting `nova-backend` or `claude-stats`, and before any push.
- Three repos change, one commit per logical piece: `Home-Assistant` (Nova), `homelab` (`services/claude/claude-stats`), `ha-dashboard` (package and view). `Home-Assistant` has another session's unpushed song-wishlist work on branch `satellite` (5 commits ahead of `origin/main`); don't disturb it.

## Decided

- **Where:** Home Assistant, desktop dashboard (`dashboard-home`, `ha-dashboard/config.yaml`), as a new view. Layout is mockup option A plus the additions above.
- **Look:** the `glass` theme and `card_mod: class: glass`, grey monochrome like the rest; orange and red only as warnings (a limit meter past about 60% / 85%, a day over the alert limit).
- **Claude Code numbers:** CT 109 only, from the existing `claude-stats` exporter (port 9180), extended. Windows are rolling (last 24 hours, not since midnight).
- **"Tokens written"** means output tokens. "With cache" means input + output + cache reads + cache writes.
- **Model breakdown** counts tokens written, both AIs together, and shows which AI used each model.
- **Charts:** apexcharts-card, added from HACS (he said yes on 12 Nov). The exporter and Nova serve ready-made hourly and daily buckets for it, so the Claude Code charts start with the history the transcripts already hold (back to 25 Oct) rather than empty.
- **Units:** pounds. Anthropic bills in dollars, so prices are kept in dollars and converted with an exchange rate he sets.

## My calls (Claude's, not his; he can overrule)

- The view is called "AI" and sits after Devices. The existing Claude Code section on the Devices view keeps its status, last-active and Connect buttons; its Usage tiles move to the new view.
- The alert limit defaults to £0.25 a day, the placeholder in the mockup, changeable on the settings page.
- The exchange rate is a fixed number on the settings page, not fetched live.
- Nova's kinds of request are taken from what the call did (the tools Claude called, a web search, or none = chat), not from the words, which keeps decision 0005.
- Web searches are costed on top of tokens (config.yaml already says each search is billed on top).
- Nova's spend sensors go on one brain-level Nova device (same broker, same `ha_device` code), not on a satellite's device: `session.py` makes a device per satellite session, which goes unavailable when that session stops and would be duplicated by a second satellite. Entity ids are fixed from the start, since renaming them later loses chart history.
- Projects for "tokens by project" come from each transcript line's `cwd`: the repo folder under `~/work` it is in (worktrees such as `homelab-device-backups` fold into their repo), the other Remote Control folders (`~/nova`, `~/proxmox`, `~/pc`, `~/macbook`) as rows of their own, and plain `~/work` as "General". Each repo is its own row; the card shows the top five.
- The Nova work goes on a new branch from `origin/main` (the brain runs 393b487, already on main), merged into main when done; the `satellite` work is merged separately by its own session. Deploying and pushing wait for his go-ahead.
- "At API prices" can say how it compares with his plan: the plan's monthly price sits in the exporter's price file, and the line is left out while that is unset.
- "At API prices" for Claude Code uses Anthropic's list prices per model, kept in a small file beside the exporter in `homelab/services/claude/`, since the exporter is not Nova and has no settings page. Look them up with the claude-api skill when building; never from memory.

## Current state

- `homelab/services/claude/claude-stats` (Python, 245 lines, runs as `claude-stats.service` on CT 109): serves `/stats` with today's and the week's tokens, sessions, last activity, last model and the two limits from `claude -p /usage`. It caches each transcript by mtime and size. It does not split by model or project, and "today" is since midnight.
- `ha-dashboard/homeassistant/packages/claude_code.yaml`: the REST sensors for that.
- Nova: `voice_assistant/llm.py` calls Claude in a few places and `_log_usage` writes input and cache-read tokens to the journal only (no output tokens, no cache writes, no cost, nothing kept). Many requests never reach Claude (local commands and HA's Assist go first), so "requests" on the dashboard means Claude calls.
- A quick count of the last 7 days of CT 109 transcripts (by file, so slightly generous): Opus 5.5 9.6M tokens written, Sonnet 5.5 2.4M, Fable 5.2 151k, Haiku 5.5 62k.

## Left to the builder

- How Nova stores its records (one line per call in a rotated JSONL file in its data folder, a small daily roll-up, or both), as long as 30 days and the month to date survive restarts and the size stays bounded.
- Exactly which sensors Nova and the exporter add and their names, provided the dashboard can draw everything in the goals with apexcharts-card (buckets as attributes) and the existing cards.
- How the 24 h / 7 days / 30 days switches work in HA (for example an `input_select` with conditional cards, or bubble-card buttons). No HACS cards beyond apexcharts-card and the ones already installed.
- How the exporter scans 30 days without rereading everything: count message by message by timestamp, dedupe by message id, keep per-file results cached.
- Tests: Nova's suite runs on the Pi (`.superpowers/tools/pi_test.sh`, London and UTC); the cost maths and window sums deserve unit tests.
