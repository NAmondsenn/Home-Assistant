# AI Usage Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new "AI" view in Home Assistant showing Claude Code and Nova usage side by side: limits, tokens over 24 h / 7 days / 30 days, Nova's cost, charts, and a by-model breakdown, plus an ntfy alert when Nova's daily spend passes a limit.

**Architecture:** Nova records every Claude call in a bounded JSONL log on the brain (`usage.py`) and publishes a summary to HA through a brain-level MQTT device (`usage_device.py`). The `claude-stats` exporter on CT 109 adds rolling windows, model and project splits, API-price cost and chart buckets to its JSON. HA reads both and draws the view with bubble-card, card-mod and apexcharts-card.

**Tech Stack:** Python 3 (Nova on the brain, CT 107; exporter on CT 109), paho-mqtt, pytest on the Pi, stdlib unittest for the exporter, Home Assistant storage-mode dashboard YAML, apexcharts-card from HACS.

**Spec:** `docs/superpowers/specs/2026-11-12-ai-usage-dashboard-design.md` (read it first; this plan argues from it).

## Global Constraints

- British spelling, no em dashes, no emojis, in code, comments, UI text and commit messages.
- Match each repo's comment style: Nova's long plain-English docstrings and "why" comments; the exporter's short comments.
- Nothing anyone says to Nova, and none of its answers, may reach HA (decision 0005). Counts, costs, model names and request kinds only.
- Recording a call must never raise into, or slow, the reply path.
- Nova's changeable values live on the settings page (`EDITABLE`), read live from `brain.config.section("usage")`.
- CT 109 load limits: run tests with `nice`, `timeout`, one suite at a time, no repeat runs.
- Nova work: worktree `~/work/Home-Assistant-ai-usage`, branch `ai-usage` (from `origin/main`). Never touch `~/work/Home-Assistant` or branch `satellite`.
- Lab writes (HA dashboard saves, HACS install, copying the exporter, restarting `claude-stats` or `nova-backend`, deploying to the brain) and every `git push` wait for Nathaniel to name the exact job in a message. Stop and ask with the exact sentence.
- One commit per task; commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Prices are looked up with the claude-api skill at build time, never from memory.

## Review Focus

1. Nova restarts mid-month: today, the month and the 30-day windows come back intact from `usage.jsonl` (Task 1 test `test_survives_restart`).
2. A power cut leaves a half-written last line in `usage.jsonl`: it is skipped, everything else loads (Task 1 test `test_skips_broken_line`).
3. A response with no `usage`, or with `None` fields (mocks, errors, refusals): nothing is recorded or zeros are used, and nothing raises (Task 1 `test_record_without_usage`, Task 4 `test_usage_failure_does_not_break_reply`).
4. Day and month edges in Europe/London, including BST ending: a call at 23:30 on 31 Oct counts in October, one at 00:30 on 1 Nov in November, and hourly buckets stay 24 long across the clock change (Task 1 `test_month_edge_london`, `test_hourly_buckets_across_clock_change`; the suite runs in London and UTC).
5. Exporter: the same API response appearing in two transcript files (a resumed session or a subagent), `<synthetic>` models, and lines with no `cwd`: counted once, skipped, and counted as "General" respectively (Task 5 `test_dedupes_across_files`, `test_skips_synthetic`, `test_missing_cwd_is_general`).

---

## Test commands

- **Nova (on the Pi):** the shared script is hard-wired to the main checkout and a shared remote folder, so make a copy for this worktree once:
  `sed -e 's#^cd /home/nathaniel/work/Home-Assistant$#cd /home/nathaniel/work/Home-Assistant-ai-usage#' -e 's#ct-work#ct-work-ai-usage#g' ~/work/Home-Assistant/.superpowers/tools/pi_test.sh > ~/work/pi_test_ai.sh`
  Run: `nice timeout 900 bash ~/work/pi_test_ai.sh tests/test_usage.py` (London), and `TZ_OVERRIDE=UTC` in front for UTC. The full suite (no args) only at the end of Task 4.
- **Ruff:** `ssh -o BatchMode=yes pi 'cd /tmp/nova-check/ct-work-ai-usage && /tmp/nova-check-pytest/bin/ruff check .'`
- **Exporter (CT 109):** `cd ~/work/homelab/services/claude && nice timeout 300 python3 -m unittest -v test_claude_stats`

---

### Task 1: Nova's usage log

**Files:**
- Create: `voice_assistant/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces:
  - `KINDS: Dict[str, str]`: tool name to kind. Music: `play_music, play_playlist, download_music, control_playback, repeat, shuffle`. Lights and devices: `set_lights, run_home_script, home_status, get_batteries, find_device, pc_power, set_volume`. Timers and alarms: `set_timer, name_reminder, list_timers, dismiss_timer, check_missed, cancel_timer, set_alarm, list_alarms, next_alarm, cancel_alarm`. Calendar and lists: `get_calendar, get_activity, add_to_calendar, manage_list`. Any other tool: "Other". No tool: "Chat and questions". A web search: "Web search", which wins over tools.
  - `kind_of(tool_names: Sequence[str], searches: int) -> str`: the first tool's kind decides when there are several.
  - `cost_in_dollars(usage, settings: Dict) -> float`: `settings` has `input_price` and `output_price` (dollars per million tokens) and `search_price` (dollars per 1,000 searches). Cache reads cost 0.1x input; cache writes 1.25x input for 5-minute ones and 2x for 1-hour ones, read from `usage.cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens`; if that breakdown is missing, all of `cache_creation_input_tokens` is 5-minute. Searches from `usage.server_tool_use.web_search_requests`. Any missing or `None` field counts as 0.
  - `class UsageLog(path: str, settings: Callable[[], Dict], on_over_limit: Optional[Callable[[float, float], None]] = None, clock: Callable[[], float] = time.time)`
    - `record(response, model: str) -> None`: never raises (logs instead). Reads `response.usage` and the `tool_use` blocks' names in `response.content`; appends one JSON line `{"at", "model", "input", "output", "cache_read", "cache_write", "searches", "kind", "dollars"}` and keeps it in memory. Dollars are worked out at record time, so a later price change never rewrites history. Calls `on_over_limit(today_pounds, limit_pounds)` only when this record takes today's spend from at or under `daily_limit` to over it.
    - `summary() -> Dict`: cached for 60 seconds and recomputed at once after a `record`. Pounds = dollars x `pounds_per_dollar`, rounded to 4 places. Keys:
      `today_pounds`, `month_pounds`, `month_projection_pounds` (month so far / days elapsed including today as a fraction x days in month), `daily_limit_pounds`,
      `requests_24h`, `cache_share_24h` (cache reads / all input tokens, 0 to 1, or `None` with no calls),
      `windows`: `{"24h"|"7d"|"30d": {"output", "total", "pounds", "requests"}}`,
      `hourly_pounds`: 24 `[ISO hour start, pounds]` pairs, oldest first, ending with the current hour,
      `daily_pounds`: 30 `[YYYY-MM-DD, pounds]` pairs, oldest first, ending today,
      `kinds_month`: `{kind: pounds}`, largest first,
      `models`: `{"24h"|"7d"|"30d": {model: output tokens}}`.
      Days and months are local time (the brain's TZ, Europe/London).
    - At start it loads lines newer than 40 days, skipping any it can't parse. Once a day (on the first record of a new day) it rewrites the file without lines older than 40 days, atomically through `storage.save_text`, so the file stays bounded.

- [ ] **Step 1: Write the failing tests** in `tests/test_usage.py`, using a fake clock and `SimpleNamespace` responses:
  - `test_kind_of`: `kind_of(["set_lights"], 0) == "Lights and devices"`, `kind_of(["play_music"], 1) == "Web search"`, `kind_of([], 0) == "Chat and questions"`, `kind_of(["remember"], 0) == "Other"`.
  - `test_cost_in_dollars`: input 1,000,000 at `input_price` 1.0 and output 1,000,000 at `output_price` 5.0 is 6.0; 1,000,000 cache reads add 0.1; 1,000,000 1-hour cache writes add 2.0; 1,000 searches at `search_price` 10.0 add 10.0; all-`None` usage is 0.0.
  - `test_windows_and_models`: calls at now-1h, now-3d and now-20d land in 24h/7d/30d as expected, and `models["7d"]` sums output by model.
  - `test_today_month_projection`: on the 10th at noon with £1.00 spent this month, the projection is 1.00 / 9.5 x days in month.
  - `test_hourly_and_daily_buckets`: lengths are 24 and 30, the last pair is the current hour / today, and empty ones are 0.
  - `test_survives_restart`: record three calls, build a new `UsageLog` on the same path, `summary()` matches.
  - `test_skips_broken_line`: a file ending in `{"at": 17` loads the good lines only.
  - `test_record_without_usage`: a response with no `usage` records nothing and doesn't raise.
  - `test_alert_once_on_crossing`: with `daily_limit` 0.25, records taking today from 0.20 to 0.30 call `on_over_limit` once with (0.30, 0.25); another record later that day doesn't; a new `UsageLog` built over a file already over the limit doesn't call it on its next record.
  - `test_month_edge_london`: 23:30 on 31 Oct 2026 counts in October's total, 00:30 on 1 Nov in November's (a TZ-aware clock; skip under UTC if the test needs London).
  - `test_hourly_buckets_across_clock_change`: at 12:00 on 25 Oct 2026 (BST ended at 02:00), `hourly_pounds` still has 24 entries.
  - `test_prunes_old_lines`: a 50-day-old line is gone from the file after the first record of a new day.
- [ ] **Step 2: Run them, expect failures** (`ModuleNotFoundError: voice_assistant.usage`): `nice timeout 900 bash ~/work/pi_test_ai.sh tests/test_usage.py`
- [ ] **Step 3: Implement `voice_assistant/usage.py`** to the interfaces above. A module docstring in Nova's style explains what is kept, why only counts and kinds (decision 0005), and how big the file can get.
- [ ] **Step 4: Run them, expect PASS** in London and with `TZ_OVERRIDE=UTC`; ruff clean.
- [ ] **Step 5: Commit** `tests/test_usage.py voice_assistant/usage.py`: "Keep a log of each Claude call Nova makes, with what it cost"

### Task 2: Nova's usage device in HA

**Files:**
- Modify: `voice_assistant/ha_device.py` (`NovaDevice._on_connect`, add `_subscriptions`)
- Create: `voice_assistant/usage_device.py`
- Test: `tests/test_usage_device.py`; run `tests/test_ha_device.py` unchanged

**Interfaces:**
- Consumes: `UsageLog.summary()` from Task 1.
- Produces:
  - `NovaDevice._subscriptions(self) -> List[Tuple[str, int]]`: the list `_on_connect` subscribes to today, moved out unchanged.
  - `class UsageDevice(NovaDevice)` in `usage_device.py`, `__init__(host, port, username, password, usage: UsageLog, client_factory=None, hostname=None)`, name "Nova usage", `node_id` `nova_usage_<hostname>`, `controls=None`. `from_config(config, password, usage) -> Optional[UsageDevice]` reads `mqtt` like `NovaDevice.from_config` (None without `mqtt.host`). Overrides `discovery()` (device `mdl` "Nova brain"), `_state()` and `_subscriptions()` (only `HA_STATUS`, so it re-announces after HA restarts). `_on_message` must ignore anything but `HA_STATUS`.
  - Sensors and the entity ids HA will give them (fixed from the start; renaming later loses history):
    | control | name | state | attributes |
    | --- | --- | --- | --- |
    | `spent_today` | Spent today | `today_pounds` (`device_class: monetary`, unit `GBP`) | `daily_limit_pounds`, `requests_24h`, `cache_share_24h` |
    | `spent_month` | Spent this month | `month_pounds` (monetary, GBP) | `month_projection_pounds`, `kinds_month` |
    | `tokens_24h` / `tokens_7d` / `tokens_30d` | Tokens 24 hours / Tokens 7 days / Tokens 30 days | that window's `output` | `total`, `pounds`, `requests` |
    | `spend_history` | Spend history | `today_pounds` | `hourly_pounds`, `daily_pounds`, `daily_limit_pounds` |
    | `models` | Models | the 7-day top model, or `None` | `models` |
    Entity ids: `sensor.nova_usage_spent_today`, `sensor.nova_usage_spent_this_month`, `sensor.nova_usage_tokens_24_hours`, `sensor.nova_usage_tokens_7_days`, `sensor.nova_usage_tokens_30_days`, `sensor.nova_usage_spend_history`, `sensor.nova_usage_models`. Pin them with `default_entity_id` if this HA version takes it, otherwise `object_id`.

- [ ] **Step 1: Write the failing tests**, reusing `test_ha_device.py`'s fake client:
  - `test_discovery_lists_usage_sensors`: the seven controls, each with `unique_id` `nova_usage_<host>_<control>`, and no command topics.
  - `test_state_from_summary`: given a fake `UsageLog` whose summary has `today_pounds` 0.07, the `spent_today` topic gets `"0.07"`, and the attributes topic JSON has `daily_limit_pounds`.
  - `test_subscribes_to_ha_status_only`: after connect, the only subscription is `homeassistant/status`.
  - `test_no_words_reach_ha`: no published payload contains a key other than the summary keys above (guards decision 0005).
- [ ] **Step 2: Run, expect failures**: `nice timeout 900 bash ~/work/pi_test_ai.sh tests/test_usage_device.py tests/test_ha_device.py`
- [ ] **Step 3: Add `_subscriptions` to `NovaDevice`**, then implement `UsageDevice`.
- [ ] **Step 4: Run both files, expect PASS** (London and UTC); ruff clean.
- [ ] **Step 5: Commit** "Show what Nova's Claude calls cost in Home Assistant, as a device for the brain"

### Task 3: Brain wiring, settings and the alert

**Files:**
- Modify: `config.yaml` (new `usage:` section), `voice_assistant/config.py` (`LIMITS`), `voice_assistant/settings_page.py` (`EDITABLE`), `voice_assistant/brain.py` (`Brain.__init__`, start, stop)
- Test: `tests/test_brain.py`, `tests/test_settings_page.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: `UsageLog` (Task 1), `UsageDevice` (Task 2).
- Produces: `brain.usage: UsageLog` (always present) and `brain.usage_device: Optional[UsageDevice]`, started and stopped with the brain's other parts.

- [ ] **Step 1: Look up prices** with the claude-api skill: Haiku 5.5 input and output dollars per million tokens, and web search dollars per 1,000. Note the source in the config comment.
- [ ] **Step 2: Add to `config.yaml`**, after `web_search:`, with comments in the file's style:
  ```yaml
  usage:
    daily_limit: 0.25        # pounds; ntfy's phone topic is told once a day when passed
    pounds_per_dollar: 0.79  # Anthropic bills in dollars
    input_price: <looked up> # dollars per million tokens, for llm.model
    output_price: <looked up>
    search_price: <looked up> # dollars per 1,000 web searches
  ```
- [ ] **Step 3: Write the failing tests**:
  - `test_settings_page.py`: the five `usage.*` settings are on the page, used "now", and `usage.daily_limit` refuses -1.
  - `test_config.py`: `LIMITS` has numbers for all five (`daily_limit` 0 to 100, `pounds_per_dollar` 0.1 to 10, prices 0 to 1000).
  - `test_brain.py`: `brain.usage` reads `config.section("usage")` live (change `daily_limit` with `apply_settings`, the next `summary()` shows it); `on_over_limit` sends `ntfy.send_soon(phone, "Nova has spent £0.30 on Claude today, over your £0.25 limit.", title="Nova spending")`; with no ntfy or no phone topic, nothing is sent and nothing raises.
- [ ] **Step 4: Run, expect failures**: `nice timeout 900 bash ~/work/pi_test_ai.sh tests/test_brain.py tests/test_settings_page.py tests/test_config.py`
- [ ] **Step 5: Implement**: the `EDITABLE` rows ("Daily spending alert, in pounds", "Pounds per dollar", "Claude's price per million input tokens, in dollars", "per million output tokens", "per 1,000 web searches"), the `LIMITS`, and in `Brain.__init__` `self.usage = UsageLog(os.path.join(paths.DATA_DIR, "usage.jsonl"), lambda: self.config.section("usage"), on_over_limit=self._spending_alert)` and `self.usage_device = UsageDevice.from_config(config, os.getenv("MQTT_PASSWORD"), self.usage)`.
- [ ] **Step 6: Run, expect PASS**; ruff clean.
- [ ] **Step 7: Commit** "Put Nova's spending alert and Claude's prices on the settings page, and start the usage device"

### Task 4: Record every call

**Files:**
- Modify: `voice_assistant/llm.py` (`LLMHandler.__init__`, `_log_usage`), `voice_assistant/session.py:248`
- Test: `tests/test_llm.py`, `tests/test_session.py`

**Interfaces:**
- Consumes: `brain.usage` (Task 3), `UsageLog.record` (Task 1).
- Produces: `LLMHandler(..., usage: Optional[UsageLog] = None)`; `_log_usage(self, response)` becomes an instance method that keeps today's journal line, adds output tokens to it, and calls `self.usage.record(response, model=self.model_name)` when `usage` is set.

- [ ] **Step 1: Write the failing tests**:
  - `test_llm.py::test_each_response_is_recorded`: a handler with a fake usage log, and a query that reads a tool then answers, records two responses, with the handler's model.
  - `test_llm.py::test_not_for_me_is_recorded`: the `not_for_me` and refusal-while-playing returns record too.
  - `test_llm.py::test_usage_failure_does_not_break_reply`: a usage log whose `record` raises still gives the normal reply.
  - `test_session.py`: the session's `LLMHandler` gets `brain.usage`.
- [ ] **Step 2: Run, expect failures**: `nice timeout 900 bash ~/work/pi_test_ai.sh tests/test_llm.py tests/test_session.py`
- [ ] **Step 3: Implement**, wrapping the `record` call so nothing reaches the reply path. Check every `self._log_usage(` call site (three today), and `grep -n "messages.create" voice_assistant/` for any Claude call outside `process_query` that should also record.
- [ ] **Step 4: Run the two files, then the full suite once** in London and once in UTC (`nice timeout 1800 bash ~/work/pi_test_ai.sh`); ruff clean. Expect the existing pass count plus the new tests, no failures.
- [ ] **Step 5: Commit** "Record what each of Claude's answers cost"

### Task 5: Claude Code exporter: windows, models, projects, prices

**Files (repo `~/work/homelab`, branch `main`):**
- Modify: `services/claude/claude-stats`
- Create: `services/claude/claude-prices.json`, `services/claude/test_claude_stats.py`
- Modify: `services/claude/README.md` (the new fields, the price file, where it's installed)

**Interfaces:**
- Produces, in `/stats`, keeping every existing key:
  - `windows`: `{"24h"|"7d"|"30d": {"output", "total", "responses", "sessions"}}`, rolling.
  - `models`: `{"24h"|"7d"|"30d": {model: output}}`.
  - `projects_7d`: `[[project, output], ...]` top five, largest first.
  - `api_pounds_30d` and `api_pounds_by_model_30d: {model: pounds}`, `null` when the price file is missing or a model has no price.
  - `plan_pounds_month`: from the price file, or `null`.
  - `hourly_output`: 24 `[ISO hour start, output]` pairs ending this hour; `daily_output`: 30 `[YYYY-MM-DD, output]` pairs ending today (local time).
- `project_of(cwd: Optional[str]) -> str`: `~/work/<repo>/...` gives `<repo>`, with a worktree suffix folded (`homelab-device-backups` gives `homelab`, `Home-Assistant-ai-usage` gives `Home-Assistant`: strip a `-<suffix>` when the remainder is another folder in `~/work`); `~/nova`, `~/proxmox`, `~/pc`, `~/macbook` give `nova`, `proxmox`, `pc`, `macbook`; plain `~/work`, `None` or anything else gives "General".
- `claude-prices.json`: `{"pounds_per_dollar": 0.79, "plan_dollars_month": null, "models": {"claude-opus-5-5": {"input": .., "output": ..}, ...}}`, with cache reads at 0.1x input and cache writes 1.25x (5-minute) or 2x (1-hour), from each entry's `cache_creation` breakdown. Installed at `/usr/local/etc/claude-prices.json`.
- Entries gain `cwd`, `cache_write_5m`, `cache_write_1h`. Files whose mtime is older than 31 days are not read; entries older than 31 days are dropped; `stats()` is cached for 30 seconds.

- [ ] **Step 1: Look up prices** with the claude-api skill for every model in the last 30 days of transcripts (Opus 5.5, Sonnet 5.5, Haiku 5.5, Fable 5.2 today) and write `claude-prices.json`. Leave `plan_dollars_month` null; Nathaniel can fill it in.
- [ ] **Step 2: Write the failing tests** in `test_claude_stats.py` (load the script with `importlib.machinery.SourceFileLoader`, point `PROJECTS` at a temp folder of small JSONL files, fix the clock):
  - `test_rolling_windows`: entries at now-2h, now-2d and now-20d fall into 24h/7d/30d correctly, and an entry 40 days old is in none.
  - `test_models_split`, `test_projects_top_five`, `test_project_of` (each case above).
  - `test_api_pounds`: a known entry costs the expected pounds; no price file gives `null`.
  - `test_dedupes_across_files`, `test_skips_synthetic`, `test_missing_cwd_is_general`.
  - `test_keeps_old_keys`: `today_tokens`, `week_tokens`, `five_hour_pct` and the rest are still there.
- [ ] **Step 3: Run, expect failures**: `cd ~/work/homelab/services/claude && nice timeout 300 python3 -m unittest -v test_claude_stats`
- [ ] **Step 4: Implement** in the exporter's existing style.
- [ ] **Step 5: Run, expect PASS.** Then a read-only check against the real transcripts, run as a one-off with `nice` (not through the service): load the module, call `stats()`, print the size of the JSON and the 7-day model split, and compare the split with the spec's rough figures (Opus about 78%).
- [ ] **Step 6: Commit** "Add rolling windows, models, projects and API prices to the Claude Code stats"
- [ ] **Step 7: Deploy (needs his go-ahead naming the job):** copy `claude-stats` to `/usr/local/bin/`, `claude-prices.json` to `/usr/local/etc/`, restart `claude-stats.service`, then `curl -s http://127.0.0.1:9180/stats` shows `windows` and `models`.

### Task 6: The AI view in Home Assistant

**Files (repo `~/work/ha-dashboard`, branch `main`):**
- Modify: `homeassistant/packages/claude_code.yaml` (new sensors)
- Create: `homeassistant/packages/ai_usage.yaml` (`input_select.ai_usage_period`: "24 h", "7 days", "30 days"; initial "7 days")
- Modify: `config.yaml` (new view after Devices; usage tiles removed from the Devices view's Claude Code section; navbar route if the navbar lists views)
- Modify: `README.md` (apexcharts-card in "What they need"; the new package)

**Interfaces:**
- Consumes: the Task 2 entity ids; the Task 5 JSON keys.
- Produces: REST sensors `sensor.claude_code_tokens_24_hours`, `sensor.claude_code_tokens_7_days`, `sensor.claude_code_tokens_30_days` (state `output`, attribute `total`), `sensor.claude_code_api_price` (state `api_pounds_30d`, unit GBP, attributes `api_pounds_by_model_30d`, `plan_pounds_month`), `sensor.claude_code_usage_history` (attributes `hourly_output`, `daily_output`, `models`, `projects_7d`). The existing limit and reset sensors stay as they are.

- [ ] **Step 1: Install apexcharts-card (needs his go-ahead naming the job):** through HACS's websocket (`hacs/repository/download`) with the token on CT 107, as the rack-screen work saved dashboards, or by asking him to tap Download on its HACS page. Check it appears in Lovelace resources.
- [ ] **Step 2: Write the packages**, check the configuration in HA (Developer tools, YAML, or `ha core check` over the API), and reload REST and input_select (no restart unless HA asks). This is a lab write too: the same go-ahead, or ask.
- [ ] **Step 3: Build the view** to mockup option A (https://claude.ai/artifact/9vzfn4BgpGh9otByGqHJfC) and the spec's goals 2 to 4: bubble-card separators "Claude Code" and "Nova", glass cards, the limits with orange past 60% and red past 85%, the three-period token cards, the API-price card, apexcharts bar charts switched by `input_select.ai_usage_period` through conditional cards (hourly for 24 h, daily for 7 and 30 days), the Nova chart with the daily limit as a dashed line, the top-five projects and the Nova kinds as bar rows, and the By model card bottom right. Grey monochrome; colour only for warnings.
- [ ] **Step 4: Save over the websocket** (`lovelace/config/save` for `dashboard-home`), read it back and diff with the file.
- [ ] **Step 5: Screenshot** the view at 1920x1080 and at phone width with Playwright from `~/work/bench/node_modules` (token piped from CT 107 on stdin), and look for clipped text, empty charts and unreadable colours. Fix once, screenshot once more.
- [ ] **Step 6: Commit** "Add the AI view: Claude Code and Nova usage side by side"

### Task 7: Ship Nova

- [ ] **Step 1:** Whole-branch review of `ai-usage` (superpowers:requesting-code-review, reviewer on Opus).
- [ ] **Step 2 (needs his go-ahead):** merge `ai-usage` into `main`, push `main` and `homelab` and `ha-dashboard`.
- [ ] **Step 3 (needs his go-ahead):** deploy to the brain per `docs/release-checklist.md`, restart `nova-backend`, then confirm the seven `sensor.nova_usage_*` entities exist and, after one question to Nova, `requests_24h` is 1 or more.
- [ ] **Step 4:** Vault: dated log entry and index line in `~/work/wisdom` (pull first, stage named files only), and update memory.
