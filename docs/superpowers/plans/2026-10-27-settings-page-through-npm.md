# The settings page through Nginx Proxy Manager - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The settings page also opens through Nginx Proxy Manager, on a second listener that answers only NPM's address and lets nobody in without the page's own password; the tailnet address is unchanged.

**Architecture:** A new module, `voice_assistant/page_password.py`, holds the password's hash, the logins (hashed tokens in a file, 30 days each) and the wrong-password counts. `SettingsPage` starts a second `ThreadingHTTPServer` when `settings_page.proxy_port` is set, with the same handler class; the server says which route it is, and `_let_in` checks Tailscale on one and the login cookie on the other. The page's script shows a login form on the NPM route, and Log out and Log out everywhere on both.

**Tech Stack:** Python 3.13 standard library only (`hashlib.scrypt`, `secrets`, `hmac`, `http.server`, `http.cookies`), pytest, the page's plain JavaScript (checked with Node).

**Spec:** `docs/superpowers/specs/2026-10-27-settings-page-through-npm-design.md`

## Global Constraints

- No new dependency.
- The tailnet listener's behaviour, tests and `SAFETY_HEADERS` are unchanged; on it, `Tailscale-User-Login` against `settings_page.allow` as today.
- On the NPM listener, `Tailscale-User-Login` is never read, whatever it says.
- A request to the NPM listener from an address not in `settings_page.proxy_from` gets a bare 403 and a warning, before anything else is read.
- The password: at least 12 characters; stored only as a salted `hashlib.scrypt` hash with its parameters, mode 600, never in `config.yaml`, the repo or a log; checked with `hmac.compare_digest`; changing it ends every login.
- A login: `secrets.token_urlsafe(32)`, as a cookie `HttpOnly; Secure; SameSite=Strict; Path=/`, 30 days from when it's made (use doesn't extend it); the brain keeps only its SHA-256 and end time, in Nova's data folder, mode 600.
- Wrong passwords: per device by `X-Real-IP` (trusted only because every request on this listener is from a `proxy_from` address); 5 wrong in a row from one device refuses its tries for a minute after the fifth, with "Too many wrong passwords - try again in a minute."; a right one resets it; 30 wrong in a minute from all together refuses every try for a minute; in memory only. The tailnet route is never slowed.
- A login through NPM can do everything the tailnet route can. Replacing the page password through NPM also needs the current one.
- Before a login, only `FILES` (the page's own static files) and the login and status calls are served on the NPM listener; every other API needs the login.
- Every change still needs `X-Nova: page` and a same-origin `Sec-Fetch-Site`, on both listeners - the login post included.
- The log names an NPM login `password`.
- Both new settings, `settings_page.proxy_port` (default 0, off) and `settings_page.proxy_from` (default `[]`), are used once Nova restarts.
- The repo is public: no hostname, IP address, email address or token in code, tests, docs or commits. Docs say "NPM's address", "the brain's address" and `nova.<your domain>`.
- House style: `.superpowers/sdd/2026-10-23-battery-warnings/common.md`.
- Tests: `bash .superpowers/tools/pi_test.sh [args]`, again with `TZ_OVERRIDE=UTC`; ruff; `~/tools/node/bin/node --check voice_assistant/web/settings.js`.

## Review Focus

1. **A forged `Tailscale-User-Login` on the NPM listener** is ignored - no cookie, no way in. (Task 2, `test_a_tailscale_header_through_npm_lets_nobody_in`.)
2. **The brain's port reached directly by any other device** gets 403 whatever it sends, a right cookie included. (Task 2, `test_only_npms_address_is_answered`.)
3. **An expired or ended login, or one from before a password change,** is refused at once, and after a restart. (Task 1, `test_a_login_ends_after_thirty_days_and_when_ended`; Task 2, `test_a_new_password_logs_everyone_out`.)
4. **A logins file that's corrupt, or a password file that's unreadable,** fails shut - nobody in through NPM, the tailnet route unaffected, no crash. (Task 1, `test_a_corrupt_logins_file_lets_nobody_in`; `test_an_unreadable_password_file_is_not_set`.)
5. **Wrong guesses from many addresses** can't grow memory without limit or lock out the device that has the right one for more than a minute. (Task 1, `test_wrong_tries_are_per_device_with_a_backstop`.)

---

### Task 1: The password, the logins and the wrong tries

**Files:**
- Create: `voice_assistant/page_password.py`
- Modify: `voice_assistant/paths.py` (two paths)
- Test: `tests/test_page_password.py` (new)

**Interfaces:**
- Produces:
  - `paths.PAGE_PASSWORD_FILE = os.path.join(CONFIG_DIR, "page_password")`, `paths.PAGE_LOGINS_FILE = os.path.join(DATA_DIR, "page_logins.json")`.
  - `MIN_LENGTH = 12`, `LOGIN_DAYS = 30`, `TRIES_EACH = 5`, `TRIES_ALL = 30`, `WAIT = 60` (seconds).
  - `class PagePassword(path: str = paths.PAGE_PASSWORD_FILE)`: `is_set() -> bool`; `check(password: str) -> bool` (False when not set or unreadable); `set(password: str) -> Optional[str]` - None when saved, else what to tell the user ("The page password needs at least 12 characters."). Stored as one line `scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>` with `n=2**14, r=8, p=1`, a 16-byte salt and a 32-byte hash, written with `storage.save_text` and mode 600.
  - `class Logins(path: str = paths.PAGE_LOGINS_FILE, now: Callable[[], float] = time.time)`: `make() -> str` (the token, its SHA-256 and end saved); `check(token: Optional[str]) -> bool`; `end(token: str) -> None`; `end_all() -> None`. Thread-safe (a lock), each change saved to the file; ended ones dropped on every read and write.
  - `class WrongTries(now: Callable[[], float] = time.monotonic)`: `waiting(device: str) -> bool`; `wrong(device: str) -> None`; `right(device: str) -> None`. Forgets a device a minute after its last wrong try.

- [ ] **Step 1: Write the failing tests** in `tests/test_page_password.py`, each with scratch paths:
  - `test_a_password_is_kept_only_as_a_hash` - after `set("correct horse battery")`, the file's text doesn't contain it, starts `scrypt$`, its mode is 0o600, and `check` is True for it and False for `"correct horse batterz"`.
  - `test_a_short_password_is_refused` - `set("eleven char")` returns the message and leaves `is_set()` False.
  - `test_an_unreadable_password_file_is_not_set` - garbage in the file, or a missing one: `is_set()` False, `check(...)` False, nothing raised.
  - `test_a_login_ends_after_thirty_days_and_when_ended` - with a fake `now`: `check(make())` True; at +30 days +1 s False; `end` and `end_all` make it False; a fresh `Logins` over the same file agrees (survives a restart); the file holds no token, only hashes.
  - `test_a_corrupt_logins_file_lets_nobody_in` - garbage in the file: `check("anything")` False, `make()` still works.
  - `test_wrong_tries_are_per_device_with_a_backstop` - 5 `wrong("a")`: `waiting("a")` True, `waiting("b")` False; 60 s later False; `right("a")` resets; 30 wrong from 30 addresses: every `waiting` True for a minute; after a minute with no tries, nothing is held for those addresses.
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_page_password.py` - expected: fail (no module).
- [ ] **Step 3: Implement** `voice_assistant/page_password.py` and the two paths as the Interfaces say.
- [ ] **Step 4: Run them again** - expected: pass. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Keep a page password as a hash, logins as hashed tokens for 30 days, and count wrong tries".

### Task 2: The second listener, and who it lets in

**Files:**
- Modify: `voice_assistant/settings_page.py` (`SettingsPage.__init__`, `from_config`, `start`, `stop`, `_Handler.do_GET`, `do_POST`, `_let_in`, `ACTIONS`, the module docstring); `config.yaml` (`settings_page.proxy_port: 0`, `proxy_from: []`, with comments naming neither address); `voice_assistant/config.py` (`LIMITS` for `proxy_port`: number 0-65535).
- Test: `tests/test_settings_page.py`.

**Interfaces:**
- Consumes: Task 1's `PagePassword`, `Logins`, `WrongTries`.
- Produces:
  - `SettingsPage(..., proxy_port: int = 0, proxy_from: Optional[List[str]] = None, password: Optional[PagePassword] = None, logins: Optional[Logins] = None)`; `self.proxy_port` (the bound port once started).
  - `start()` also starts the NPM server on `("0.0.0.0", proxy_port)` when it's set and `proxy_from` isn't empty (a warning, and no server, if `proxy_from` is empty); each server has `.route` - `"tailnet"` or `"npm"`.
  - On the NPM route: `GET` of a `FILES` path served without a login; `GET /api/login-state` -> `{"route": "npm", "password_set": bool, "logged_in": bool}` without a login; `POST /api/login` `{"password": str}` -> success sets the cookie `nova_login=<token>; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000`, else `{"success": False, "message": ...}` ("That's not the password." / the wait message / "Set a page password at the page's Tailscale address first."); everything else without a good login: GET of `/` serves the page (its script shows the form), an API answers 401 `{"success": False, "message": "Log in first."}`.
  - On both routes: `GET /api/login-state` (the tailnet one says `{"route": "tailnet", "password_set": ..., "logged_in": true}`); `POST /api/logout` (ends this cookie's login, clears the cookie; on the tailnet route, nothing to end); `POST /api/logout-everywhere` (`Logins.end_all()`); `POST /api/page-password` `{"password": str, "current"?: str}` - on the NPM route `current` must check, then `PagePassword.set`, then `end_all()`, and on the NPM route a fresh login cookie for this device so the user isn't thrown out mid-change.
  - `self.login` is `"password"` for an NPM login.
- Device for `WrongTries`: the `X-Real-IP` header, or the connection's address if it's missing.

- [ ] **Step 1: Write the failing tests**, starting a page with `port=0, proxy_port=0`-style free ports (as the existing tests do with `port=0`), `proxy_from=["127.0.0.1"]` so the test client counts as NPM, and scratch password and logins files:
  - `test_only_npms_address_is_answered` - with `proxy_from=["192.0.2.1"]`, every GET and POST to the NPM port gets 403, even with a good cookie.
  - `test_a_tailscale_header_through_npm_lets_nobody_in` - `Tailscale-User-Login` of an allowed login on the NPM port: `/api/state` is 401.
  - `test_with_no_password_npm_lets_nobody_in` - `/api/login-state` says `password_set` False; `/api/login` with anything fails with the "Set a page password" message; `/api/state` is 401.
  - `test_the_right_password_logs_in_for_thirty_days` - after `/api/login`, the `Set-Cookie` has `HttpOnly`, `Secure`, `SameSite=Strict`, `Max-Age=2592000`; with it, `/api/state` is 200 and a change logs "password".
  - `test_the_page_files_are_served_before_a_login` - `/settings.js`, `/settings.css` 200 with no cookie; `/api/music` 401.
  - `test_a_login_post_must_come_from_the_page` - no `X-Nova`, or `Sec-Fetch-Site: cross-site`: 403, no cookie.
  - `test_five_wrong_passwords_make_that_device_wait` - 5 wrong with `X-Real-IP: 192.0.2.7`; the right one from it is refused with the wait message; the right one with `X-Real-IP: 192.0.2.8` works.
  - `test_a_new_password_logs_everyone_out` - two logins; `/api/page-password` through NPM with the right `current`: the other login is 401, this one carries on with its new cookie; with a wrong `current`: refused, nothing changed.
  - `test_log_out_everywhere_ends_every_login` - from the tailnet route: both NPM logins 401.
  - `test_the_tailnet_route_is_unchanged` - with a password set and no cookie, an allowed `Tailscale-User-Login` on the tailnet port gets `/api/state` 200, and `/api/login-state` says `route` "tailnet".
- [ ] **Step 2: Run** `bash .superpowers/tools/pi_test.sh tests/test_settings_page.py -k "npm or password or login or log_out or tailnet_route"` - expected: fail.
- [ ] **Step 3: Implement** as the Interfaces say. Parse the cookie with `http.cookies.SimpleCookie`, tolerating a bad header as no cookie.
- [ ] **Step 4: Run them again** - expected: pass, and every existing settings-page test unchanged. Then the full suite in both time zones, and ruff.
- [ ] **Step 5: Commit** - "Serve the settings page to NPM's address too, behind the page's own password".

### Task 3: The page, and the docs

**Files:**
- Modify: `voice_assistant/web/settings.html`, `settings.js`, `settings.css`; `README.md` ("The settings page"); `docs/troubleshooting.md`.
- Test: `tests/test_settings_page.py` (the HTML has the elements); `node --check`.

**Interfaces:**
- Consumes: Task 2's `/api/login-state`, `/api/login`, `/api/logout`, `/api/logout-everywhere`, `/api/page-password`, and the 401 `{"message": "Log in first."}`.
- Produces, on the page:
  - At load, `/api/login-state` first: on the NPM route and not logged in, only a login form (password field, "Log in" button, the message from a refusal) is shown, the rest hidden and nothing else fetched; with no password set, the "Set a page password at the page's Tailscale address first." text instead of the form.
  - Any 401 from an API, later, shows the login form again (a login that ended meanwhile).
  - "Log out" (shown on the NPM route) and "Log out everywhere" (both routes), near the Keys - the builder places them.
  - Under Keys, "Page password (for NPM)": set or not, with Set or Replace; on the NPM route Replace asks for the current password too. Never shown or kept in the page once sent.
  - The login posts through the page's script with `X-Nova: page`; no inline script or style.
- Docs: README's "The settings page" gains how to set it up (`settings_page.proxy_port`, say 8766, and `settings_page.proxy_from` with NPM's address, then a restart; in NPM a proxy host `nova.<your domain>`, scheme http, the brain's address and that port, the wildcard certificate, Force SSL, the "LAN + Tailscale" access list; set the page password at the tailnet address first), and what a login can do; troubleshooting gains "Too many wrong passwords", "Set a page password ... first", and a 403 from the brain's port (NPM's address not in `proxy_from`).

- [ ] **Step 1: Write the failing test** `test_the_page_has_a_login_form_and_log_out_buttons` - the HTML has the login form, its password field (`type="password"`, `autocomplete="current-password"`), "Log out" and "Log out everywhere".
- [ ] **Step 2: Run it** - expected: fail.
- [ ] **Step 3: Implement** the page and the docs as the Interfaces say.
- [ ] **Step 4: Run** the test, `~/tools/node/bin/node --check voice_assistant/web/settings.js`, the full suite in both time zones, and ruff - expected: all pass. Read the script's login path once by hand against Task 2's answers, and say in the report what a phone should be checked for.
- [ ] **Step 5: Commit** - "Log in on the page through NPM, and log out from either address".
