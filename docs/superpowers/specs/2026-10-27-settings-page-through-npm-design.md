# The settings page through Nginx Proxy Manager, behind a password - design

**Status:** agreed with the user, 26-27 October 2026.

## Why

The user wants the settings page at an address of their own through NPM (`https://nova.<their domain>`) as well as its tailnet address, so it opens at home from any of their devices without Tailscale on. Their words: "its only me ever on my network anyway and i can just add password protection if needs be". Asked what should happen before anything can be changed there, they said "password first".

Today the page listens on `127.0.0.1:8765` behind `tailscale serve`, and lets in only the Tailscale logins in `settings_page.allow`, read from the `Tailscale-User-Login` header Tailscale adds. Through Nginx Proxy Manager (NPM, its own container on the LAN) there is no such header, and every request comes from NPM's address. The page holds every key Nova has (Claude, Home Assistant's admin token, Spotify, Google, ntfy, Navidrome), so with no check of its own, anything on the network - a smart plug, a guest's phone - could read the page and change them.

## Goals

- The page works at the NPM address, after a password.
- It's as safe there as the tailnet address is today, for a home network where the user is the only person but not the only device.
- The tailnet address is unchanged: no password, the same Tailscale check.

## Non-goals

- Reaching the page from outside the home network and the tailnet. NPM's own "LAN + Tailscale" access list stays on the proxy host.
- Accounts or more than one password.
- Changing NPM from here - this CT can't reach it. The user adds the proxy host; the docs say exactly how.
- Password recovery: the tailnet address can always set a new one.

## Anti-goals (a failure even if it works)

- Anything on the network reaching the page, or any of its API, without the password - directly to the brain's new port, or through NPM.
- A request through the new route being taken as a Tailscale login because it carries a `Tailscale-User-Login` header. That header is only trusted on the tailnet listener; on the new one it's ignored whatever it says.
- The password, or a login's token, stored in plain text, written to a log, or sent back to the browser.
- The tailnet address starting to ask for a password, or behaving differently in any way.

## Decided with the user

- **A login through NPM can do everything** the tailnet address can.

- **Password first** on the new address.
- **The password is Nova's own**, set on the page - not NPM's basic auth, so NPM needs nothing but a proxy host.
- **It remembers the device for 30 days** after a login, and the page has **"Log out everywhere"**.
- **Wrong passwords slow down:** after 5 wrong in a row, no try is taken for a minute.
- **Only NPM's machine reaches the new port.**

## Design

### A second listener

- The page gains a second listener for NPM, on the brain's own network, next to today's `127.0.0.1` one. It's off unless set up, by two settings under `settings_page`:
  - `proxy_port` - the port it listens on, on every address (default: none, so it's off);
  - `proxy_from` - the addresses it answers, NPM's (default: empty, so it answers nothing).
  A request from any other address gets a bare 403 and a warning in the log, before anything else is read. The repo is public, so neither has a value in the repo's `config.yaml`; the user's go in the brain's own settings.
- Both listeners serve the same page, from the same `SettingsPage` and handlers. What differs is only how a request is let in (below), and what the log names the login.
- Both settings are used once Nova restarts. On the page they're under Settings, with the other page settings, if those are shown there; if not, `config.yaml` only - the builder's call.

### Who's let in

- **The tailnet listener:** as today, the `Tailscale-User-Login` header against `settings_page.allow`. Unchanged.
- **The NPM listener:**
  - Never reads `Tailscale-User-Login`.
  - With no password set, every page and API answers with a short page saying to set a password on the page at its tailnet address first, and nothing else.
  - With one set, every page and API needs a login cookie naming a login that's still good. Without one, the page is a small login form; an API call gets 401, which the page's script turns into showing the login form.
  - The log names such a login `password` ("Settings page: password set GOOGLE_CLIENT_ID"), so the two routes can be told apart.

### The password

- Set, or changed, under Keys on the page, as "Page password (for NPM)", like the other keys: set or not, never shown.
- From the tailnet listener it's set or replaced like any key. From the NPM listener, replacing it also asks for the current one, so a login left open on a device can't lock the user out. (My call, within "it can still do everything".)
- At least 12 characters. Stored as a salted `hashlib.scrypt` hash, with its parameters, in the brain's `~/.config/nova/.env`-side secrets as the other keys are, or a file of its own in the config folder with mode 600 - the builder's call, as long as it's never in `config.yaml`, never in the repo, and never logged.
- Changing it logs out every device.
- Checked in constant time (`hmac.compare_digest`) against the hash.

### Logging in

- The form posts the password to the NPM listener's login address. It's taken only from the page itself: `Sec-Fetch-Site` same-origin, as the page's other changes are, though without `X-Nova`, as a plain form can't send it - the builder may make the form's post go through the page's script instead, with `X-Nova`, if that's simpler; the page's Content-Security-Policy stays as strict as it is.
- **Wrong passwords:** counted per device, by the `X-Real-IP` header NPM sets - trusted only on requests from a `proxy_from` address, which every request on this listener is. After 5 wrong in a row from one device, its tries within a minute of the fifth are refused unchecked, with "Too many wrong passwords - try again in a minute." A right one resets that device's count. As a backstop against many addresses, after 30 wrong in a minute from all devices together, every try is refused for a minute. (Per device is my call: a shared count would let any device on the network keep the user locked out.) The tailnet address is never slowed. The counts are kept in memory only, and stay small: one per address seen in the last minute.
- A right one gives a login: a random token (`secrets.token_urlsafe(32)`), sent back as a cookie that's `HttpOnly`, `Secure` (the browser reaches it over NPM's HTTPS), `SameSite=Strict`, `Path=/`, and lasts 30 days. The brain keeps only a SHA-256 of each token, with when it ends, in a file in Nova's data folder, mode 600, so they last through a restart. Ended ones are dropped as the file is read or written.
- Each login lasts 30 days from when it was made; using it doesn't extend it.
- **Log out** (this device) and **Log out everywhere** (every login, whichever device) are on the page, on both routes. Log out everywhere from the tailnet address is how a lost phone is shut out.

### The page itself

- **What a login can do:** everything the tailnet address can - the user's decision ("just make it password protected, it can still do everything"), having heard that a stolen login could then change keys and the servers Nova sends them to. Log out everywhere is the remedy for a lost device.
- **Before a login,** the page's own static files - `settings.js`, `settings.css`, the icon and the font, all public in the repo - are served, so the login form is the page's own, styled and posted by its script under the same Content-Security-Policy. Nothing else is served: every API and the page's data need the login.
- The page's script and styles are as now. It shows Log out and Log out everywhere where the page's other account things are (the builder's call where), and on the NPM route only, a login form when it isn't logged in.
- The paste-back sign-ins (Spotify, Google) work the same on both routes.

### NPM's part (the user's)

The docs (README's "The settings page", and troubleshooting) say:

- In the brain's settings: `settings_page.proxy_port` (say 8766) and `settings_page.proxy_from` with NPM's address; restart Nova.
- In NPM: a proxy host `nova.<your domain>`, scheme `http`, forward to the brain's address and that port, the wildcard certificate, Force SSL, and the "LAN + Tailscale" access list. No websockets are needed.
- Set the page password on the page at its tailnet address first.

No address or name of the user's own is written in the repo: the docs say "NPM's address" and "the brain's address", and the address is written only as `nova.<your domain>`.

## Constraints

- No new dependency: `hashlib.scrypt`, `secrets`, `hmac` and `http.server` are all in Python's standard library.
- The tailnet listener's behaviour, tests and security headers are unchanged.
- Every existing safety check on changes (`X-Nova`, `Sec-Fetch-Site`) applies on both listeners.
- The repo is public: no hostname, IP address, email address or token in code, tests, docs or commits.
- House style: as `.superpowers/sdd/2026-10-23-battery-warnings/common.md` - full-sentence comments saying why, Google docstrings, type hints, British English, spaced " - " dashes.
- Tests on the Pi with `.superpowers/tools/pi_test.sh`, in London time and in UTC, and ruff. The page's script is now checked with Node (`~/tools/node/bin/node`), at least `node --check`.

## Left to the builder

- Where the password hash and the logins file live (within the rules above), and their formats.
- Whether the two new settings are on the page.
- How the login form posts (plain form or the page's script), within the CSP.
- Where Log out and Log out everywhere sit on the page.
- How the two listeners share one handler class.
