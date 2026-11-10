// The clock display - see voice_assistant/display_server.py and docs/decisions/0013.
//
// The kiosk app opens the address the settings page gave, "/display#key=...". The key
// is kept in localStorage and taken off the address at once, so it's never shown or sent
// anywhere as part of one - only ever in the Authorization header. Without a key the
// page says so and asks Nova nothing, as every request without one would count against
// the clock as a wrong try.
//
// Everything shown comes from a long poll of /display/api/state, which Nova answers as
// soon as anything on the clock would change - so Nova's blob covers the page the moment
// Nova hears the wake word. While Nova can't be reached the time carries on from
// the clock's own clock, set right by the difference from Nova's when it was last known,
// with a small mark that Nova is offline; touches and buttons do nothing until it's back.
//
// Anything from Nova - an event's title, a song's - is put on the page as text, never
// as HTML, and styles are only ever set through the CSSOM, since the page's
// Content-Security-Policy allows nothing inline. The clock has 1 GB of RAM and a slow
// chip, so there's no framework and nothing redrawn that hasn't changed.

"use strict";

const API = "/display/api/";

// Where the key is kept between loads - the kiosk app may reload the page at any time,
// and the address it reloads no longer has the key on it.
const KEY_STORE = "nova-display-key";

// How long to wait before asking again after each failure in a row, in milliseconds -
// then the last, for as long as Nova stays away. Soon at first, as a brain restarting
// is back in seconds.
const BACKOFF = [2000, 4000, 8000, 10000];

// How long to wait after Nova turns the key down, or turns the clock away for too many
// wrong ones, in milliseconds. Each refusal is a wrong try from the clock's address, and
// five in a minute lock it out, so asking any sooner would only keep it locked out.
const REFUSED_WAIT = 60000;

// How long a request may take before it's given up on, in milliseconds. Nova holds a long
// poll for up to 25 seconds, so it's given longer; anything else answers at once. Without
// these a connection the Wi-Fi dropped without a word would leave the page waiting for good.
const POLL_WITHIN = 40000;
const SEND_WITHIN = 10000;

// The soonest a long poll is asked again after one answered with nothing new, in
// milliseconds - see follow.
const QUICKEST = 1000;

// How long the volume shows after a button's pressed, in milliseconds.
const VOLUME_SHOWN = 1500;

// Presses the same way closer together than this, in milliseconds, are one press. The
// kiosk app may hand a button to the page twice - through fully.bind() and as a key -
// and both routes are kept, in case the page's policy turns the first down.
const SAME_PRESS = 250;

// How long the blob takes to fade and shrink away once Nova's done, in milliseconds - the
// longest of its transitions in display.css - before it's hidden altogether.
const NOVA_GOES = 700;

// How many events Home's Later tile is given, after the Next tile's - as many as fit on
// its two lines are shown.
const LATER = 3;

// How long Nova's state says to wait before going back to Home once the music stops,
// for a state without one, in seconds - the settings page's own default.
const HOME_AFTER = 60;

// What goes between two things on a line, e.g. "Artist · Album" - en spaces, as
// Outfit's own spaces are narrow enough to run the words into the dot.
const BETWEEN = "\u2002·\u2002";

const DAY_MS = 86400000;
const PAGES = ["home", "coming-up", "now-playing"];
const HOME = 0;
const NOW_PLAYING = 2;
const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];

let key = null;

// The last state Nova sent, or null before the first.
let state = null;

// Whether Nova answered the last time it was asked - null until it's first asked, so
// "Offline" isn't flashed up while the page is still connecting.
let online = null;

// Nova's clock less this one's, in milliseconds, and Nova's distance from UTC in minutes,
// e.g. 60 in British Summer Time - null until Nova has said.
let clockAhead = null;
let utcOffset = null;

let page = HOME;
let goingHome = null;
let volumeHiding = null;

// The last press counted, as {direction, at} in performance.now() milliseconds.
let lastPress = null;

// The cover being shown or fetched, as coverOf names it, and its blob: address.
let coverWanted = null;
let coverUrl = null;

// The background picture being shown or fetched, as the state's token names it, and its
// blob: address.
let backgroundWanted = null;
let backgroundUrl = null;

// What Nova's doing as the page shows it - "idle" once the blob has gone - and the timer
// hiding the blob once it's faded out.
let novaShown = "idle";
let novaHiding = null;

// The brightness last given to the kiosk app, 0 to 100.
let brightness = null;

// The finger on the screen, if any: where it's been, whether it's become a hold, and
// whether a second finger made it nothing at all.
let touch = null;

// Whether Nova was told a hold started and hasn't yet been told it ended, and the chain
// both are sent along - so an "end" never overtakes its "start" on the way.
let holdSent = false;
let holding = Promise.resolve();

const $ = (id) => document.getElementById(id);

// Writes text into an element only when it differs, so nothing is laid out again for
// the same words every second.
function setText(element, text) {
  if (element.textContent !== text) element.textContent = text;
}

// The key

// The key from "#key=..." if the address has one, kept for next time and taken off the
// address; else the one kept last time, or null - null too for one which can't be read,
// e.g. "#key=%E0", so the page says it has no key rather than stopping dead.
function readKey() {
  const given = /(?:^#|&)key=([^&]+)/.exec(location.hash);
  if (given) history.replaceState(null, "", location.pathname + location.search);
  let found = null;
  if (given) {
    try {
      found = decodeURIComponent(given[1]);
    } catch (error) {
      return null;
    }
  }
  try {
    if (found) localStorage.setItem(KEY_STORE, found);
    else found = localStorage.getItem(KEY_STORE);
  } catch (error) {
    // Storage switched off in the kiosk app - the key lasts until the page reloads.
  }
  return found;
}

// Asking Nova

// Asks Nova for something with the clock's key, giving up after a while.
//
// Args:
//   path: Where, e.g. "/display/api/state?since=7" - only ever somewhere under API, so
//     the key is never sent anywhere else.
//   options: {body} to POST as JSON, {within} milliseconds before giving up, {blob} for a
//     picture rather than JSON, {keepalive} for one sent as the page goes.
//
// Returns:
//   A promise of {status, data}, data being what was sent back for a 2xx and else null.
//   It's rejected if Nova couldn't be reached or took too long.
async function ask(path, { body = null, within = SEND_WITHIN, blob = false, keepalive = false } = {}) {
  if (!key || !path.startsWith(API)) throw new Error(`Not asking for ${path}`);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), within);
  const options = { headers: { Authorization: `Bearer ${key}` }, signal: controller.signal, cache: "no-store" };
  if (body !== null) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
    options.keepalive = keepalive;
  }
  try {
    const response = await fetch(path, options);
    let data = null;
    if (response.ok) data = blob ? await response.blob() : await response.json();
    return { status: response.status, data };
  } finally {
    clearTimeout(timer);
  }
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Follows Nova's state for as long as the page is open: each answer is shown and the
// next asked for with its version, which Nova holds until something changes. Only Nova
// being out of reach makes the page offline - a state the page fails to draw is logged
// and the next asked for, so a mistake here never passes for Nova being away.
async function follow() {
  let version = null;
  let failures = 0;
  for (;;) {
    const asked = Date.now();
    let answer;
    try {
      answer = await ask(API + (version === null ? "state" : `state?since=${version}`), { within: POLL_WITHIN });
    } catch (error) {
      version = null;
      failures += 1;
      setOnline(false);
      await pause(BACKOFF[Math.min(failures, BACKOFF.length) - 1]);
      continue;
    }
    if (answer.status !== 200) {
      // Asked afresh next time, without a version, so it's answered at once.
      version = null;
      let wait = REFUSED_WAIT;
      if (answer.status === 401) {
        showMessage("Nova didn't accept this clock's key", "Trying again every minute.");
      } else if (answer.status !== 429) {
        failures += 1;
        wait = BACKOFF[Math.min(failures, BACKOFF.length) - 1];
      }
      setOnline(false);
      await pause(wait);
      continue;
    }
    failures = 0;
    let same = false;
    try {
      same = answer.data.version === version;
      version = answer.data.version;
      showMessage(null);
      show(answer.data);
    } catch (error) {
      console.error("Couldn't draw Nova's state:", error);
      same = true;
    }
    // Nova answers the same version early only as it stops - asked again at once, it
    // would answer at once again, over and over until it's gone. A state which can't be
    // drawn waits as long, so a mistake can't set the page asking as fast as it can.
    if (same && Date.now() - asked < QUICKEST) await pause(QUICKEST);
  }
}

// A plain message over everything - the key missing or refused - or none for null.
function showMessage(text, more = "") {
  setText($("message-text"), text || "");
  setText($("message-more"), more);
  $("message").hidden = !text;
}

function setOnline(now) {
  if (online === now) return;
  online = now;
  drawOverlays();
  drawMarks();
}

// Showing a state

function show(next) {
  const last = state;
  state = next;
  clockAhead = next.now * 1000 - Date.now();
  utcOffset = next.utc_offset;
  online = true;
  drawBackground(next.background);
  drawTime();
  drawWeather();
  drawEvents();
  drawMusic();
  drawOverlays();
  drawMarks();
  followMusic(last, next);
  setBrightness(next.brightness);
}

// Nova's time now, in milliseconds since 1970 - the clock's own, set right by Nova's
// when it's known.
function novaNow() {
  return Date.now() + (clockAhead || 0);
}

// A Date whose UTC fields are Nova's wall clock at ms, e.g. getUTCHours() 21 at 21:47
// in London - so the time shown is Nova's whatever timezone the clock thinks it's in.
// Before Nova has said, the clock's own.
function wallClock(ms) {
  if (utcOffset === null) return new Date(ms - new Date(ms).getTimezoneOffset() * 60000);
  return new Date(ms + utcOffset * 60000);
}

const twoDigits = (number) => String(number).padStart(2, "0");

// The date as the clock shows it, e.g. "Sunday 8 November".
function longDate(wall) {
  return `${DAYS[wall.getUTCDay()]} ${wall.getUTCDate()} ${MONTHS[wall.getUTCMonth()]}`;
}

// The whole days from today to an event's start, e.g. 1 for tomorrow - below 0 for one
// begun before today and still on.
function daysTo(event, today) {
  const [year, month, day] = event.start.slice(0, 10).split("-").map(Number);
  return Math.round((Date.UTC(year, month - 1, day) - today) / DAY_MS);
}

// What day an event comes, as Coming up shows it: "Mon 10:00", "Wed" for one all day,
// both within the week, then just the date, e.g. "2 Dec". One begun before today and
// still on is today's.
//
// Args:
//   event: {"start": "2026-11-09" or "2026-11-09T10:00", "all_day": bool}, in Nova's
//     timezone.
//   today: Midnight today on Nova's wall clock, as wallClock gives it, in milliseconds.
function eventWhen(event, today) {
  const [, month, day] = event.start.slice(0, 10).split("-").map(Number);
  const days = daysTo(event, today);
  if (days < 0) return DAYS[new Date(today).getUTCDay()].slice(0, 3);
  if (days >= 7) return `${day} ${MONTHS[month - 1].slice(0, 3)}`;
  const name = DAYS[new Date(today + days * DAY_MS).getUTCDay()].slice(0, 3);
  return event.all_day ? name : `${name} ${event.start.slice(11, 16)}`;
}

// What day an event comes, as Home's Next tile shows it: "Today 11:30", "Tomorrow 09:00",
// "Today" or "Tomorrow" for one all day or begun before today, then as Coming up has it,
// e.g. "Wed 15:30" or "2 Dec".
function nextWhen(event, today) {
  const days = daysTo(event, today);
  if (days > 1) return eventWhen(event, today);
  const name = days === 1 ? "Tomorrow" : "Today";
  return event.all_day || days < 0 ? name : `${name} ${event.start.slice(11, 16)}`;
}

// The same for the Later tile, but shorter, as the prototype has it: just "14:30" for one
// later today - the Next tile already says it's today, as the next event can be no later -
// and from tomorrow as Coming up has it, e.g. "Wed 15:30", where "Tomorrow" would leave
// a long title less room.
function laterWhen(event, today) {
  if (daysTo(event, today) > 0) return eventWhen(event, today);
  return event.all_day || daysTo(event, today) < 0 ? "Today" : event.start.slice(11, 16);
}

// Seconds as "1:32", or "1:02:05" past an hour, as a film on Jellyfin might be.
function minutes(seconds) {
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3600);
  const rest = `${twoDigits(Math.floor(whole / 60) % 60)}:${twoDigits(whole % 60)}`;
  return hours ? `${hours}:${rest}` : `${Math.floor(whole / 60)}:${twoDigits(whole % 60)}`;
}

// The day the events were last drawn for, so they're drawn again at midnight, when
// "Mon" becomes today.
let eventsDrawn = null;

// Once a second, on the second: the time, and the music's progress, which Nova only
// sends as it changes - its position at a moment, which the page moves on from itself.
// The next second is set going first, and each drawn on its own, so a mistake drawing
// one - a state Nova sent that the page can't read, say - never stops the clock.
function tick() {
  setTimeout(tick, 1000 - (novaNow() % 1000) + 5);
  try {
    drawTime();
  } catch (error) {
    console.error("Couldn't draw the time:", error);
  }
  try {
    drawProgress();
  } catch (error) {
    console.error("Couldn't draw the music's progress:", error);
  }
}

function drawTime() {
  const wall = wallClock(novaNow());
  const time = `${twoDigits(wall.getUTCHours())}:${twoDigits(wall.getUTCMinutes())}`;
  setText($("time"), time);
  setText($("playing-time"), time);
  setText($("date"), longDate(wall));
  if (state && eventsDrawn !== $("date").textContent) drawEvents();
}

function drawWeather() {
  const weather = state.weather;
  $("weather").hidden = !weather;
  $("no-weather").hidden = Boolean(weather);
  if (!weather) return;
  setText($("temperature"), `${Math.round(weather.temperature)}°`);
  // Nova's own words for it, e.g. "light rain" - capitalised to start the line.
  const condition = weather.condition || "";
  setText($("condition"), condition.charAt(0).toUpperCase() + condition.slice(1));
  $("condition").hidden = !condition;
  // "7° / 1°", high then low, or just the one Nova knows, e.g. "High 7°".
  const known = (value) => value !== null && value !== undefined;
  const high = known(weather.high) ? `${Math.round(weather.high)}°` : null;
  const low = known(weather.low) ? `${Math.round(weather.low)}°` : null;
  let bounds = "";
  if (high && low) bounds = `${high} / ${low}`;
  else if (high) bounds = `High ${high}`;
  else if (low) bounds = `Low ${low}`;
  setText($("high-low"), bounds);
  $("high-low").hidden = !bounds;
}

// Coming up's list and Home's Next and Later tiles, drawn again only when they change or
// the day does.
let eventsShown = null;

function drawEvents() {
  const wall = wallClock(novaNow());
  const today = Date.UTC(wall.getUTCFullYear(), wall.getUTCMonth(), wall.getUTCDate());
  const events = state.events || [];
  const drawn = JSON.stringify(events);
  eventsDrawn = longDate(wall);
  if (eventsShown === `${eventsDrawn}\n${drawn}`) return;
  eventsShown = `${eventsDrawn}\n${drawn}`;
  $("events").replaceChildren(...events.map((event) => {
    const row = document.createElement("li");
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = eventWhen(event, today);
    const what = document.createElement("span");
    what.className = "what";
    what.textContent = event.title;
    row.append(when, what);
    return row;
  }));
  $("no-events").hidden = events.length > 0;
  const next = events[0];
  $("next").hidden = !next;
  $("no-next").hidden = Boolean(next);
  setText($("next-what"), next ? next.title : "");
  setText($("next-when"), next ? nextWhen(next, today) : "");
  const later = events.slice(1, 1 + LATER);
  $("later").replaceChildren(...later.map((event) => {
    const item = document.createElement("span");
    item.className = "later-event";
    const what = document.createElement("span");
    what.className = "later-what";
    what.textContent = event.title;
    const when = document.createElement("span");
    when.className = "later-when dim";
    when.textContent = laterWhen(event, today);
    item.append(what, when);
    return item;
  }));
  $("later").hidden = !later.length;
  $("no-later").hidden = later.length > 0;
}

function drawMusic() {
  const music = state.music;
  $("playing").hidden = !music;
  $("nothing-playing").hidden = Boolean(music);
  drawCover(music);
  if (!music) return;
  setText($("source"), [music.source, music.room].filter(Boolean).join(BETWEEN));
  setText($("title"), music.title);
  setText($("artist"), [music.artist, music.album].filter(Boolean).join(BETWEEN));
  $("artist").hidden = !music.artist && !music.album;
  drawProgress();
}

// Where the music is by now: its position when Nova read it, moved on by the time since
// while it plays, and never past its end.
function drawProgress() {
  const music = state && state.music;
  if (!music) return;
  let position = music.position;
  if (music.playing) position += novaNow() / 1000 - music.at;
  const known = music.duration !== null && music.duration > 0;
  if (known) position = Math.min(position, music.duration);
  $("progress").hidden = !known;
  if (known) $("progress-done").style.transform = `scaleX(${Math.max(0, position / music.duration)})`;
  setText($("times"), known ? `${minutes(position)} / ${minutes(music.duration)}` : minutes(position));
}

// What names a cover: its address, and for Jellyfin the item too, as a Jellyfin player's
// cover is asked for by the player - the same address for the next episode.
function coverOf(music) {
  if (!music || !music.cover) return null;
  return music.source === "Jellyfin" ? `${music.cover}\n${music.title}\n${music.album}` : music.cover;
}

// Shows the music's cover, fetched with the key and shown from a blob: address, as an
// <img> can't send the key itself. The one before is let go of first, so the clock's
// 1 GB doesn't fill with old covers.
function drawCover(music) {
  const wanted = coverOf(music);
  if (wanted === coverWanted) return;
  coverWanted = wanted;
  const image = $("cover");
  image.hidden = true;
  image.removeAttribute("src");
  if (coverUrl) URL.revokeObjectURL(coverUrl);
  coverUrl = null;
  if (!wanted) return;
  ask(music.cover, { blob: true }).then((answer) => {
    // A cover asked for before the music changed again is no longer wanted.
    if (coverWanted !== wanted) return;
    if (answer.status !== 200) {
      // Asked again with the next state - the gradient shows meanwhile.
      coverWanted = null;
      return;
    }
    coverUrl = URL.createObjectURL(answer.data);
    image.src = coverUrl;
    image.hidden = false;
  }).catch(() => {
    if (coverWanted === wanted) coverWanted = null;
  });
}

// Shows his dashboard's picture behind every page, fetched with the key as a cover is,
// only when the state's token for it changes. The one before stays until the next has
// come, so the page never flashes dark between them, and is let go of then. Without one,
// or Nova answering that there's none, the plain dark background shows; failing to reach
// Nova keeps what's shown, and it's asked for again with the next state.
//
// Args:
//   token: The state's "background", a short token that changes with the picture, or
//     null for none.
function drawBackground(token) {
  const wanted = token || null;
  if (wanted === backgroundWanted) return;
  backgroundWanted = wanted;
  if (!wanted) {
    showBackground(null);
    return;
  }
  ask(API + "background", { blob: true }).then((answer) => {
    if (backgroundWanted !== wanted) return;
    if (answer.status !== 200) {
      backgroundWanted = null;
      showBackground(null);
      return;
    }
    showBackground(URL.createObjectURL(answer.data));
  }).catch(() => {
    if (backgroundWanted === wanted) backgroundWanted = null;
  });
}

function showBackground(url) {
  $("background").style.backgroundImage = url ? `url("${url}")` : "";
  if (backgroundUrl) URL.revokeObjectURL(backgroundUrl);
  backgroundUrl = url;
}

function playing(music) {
  return Boolean(music && music.playing);
}

// Music starting shows Now playing; music stopping goes back to Home a while later, if
// Now playing is still open then.
function followMusic(last, next) {
  const was = playing(last && last.music);
  const is = playing(next.music);
  if (is && !was) {
    clearTimeout(goingHome);
    goingHome = null;
    showPage(NOW_PLAYING);
  } else if (was && !is) {
    clearTimeout(goingHome);
    const after = typeof next.home_after === "number" ? next.home_after : HOME_AFTER;
    goingHome = setTimeout(() => {
      goingHome = null;
      if (page === NOW_PLAYING && !playing(state.music)) showPage(HOME);
    }, after * 1000);
  }
}

// The blob over the page while Nova listens, thinks or speaks - none while Nova's offline,
// as what it was doing when it went is no longer known. It's shown by taking "hidden" off
// and only then adding its classes, so it fades and grows in rather than appearing, and
// hidden again once it's faded out, so its animations don't go on unseen.
function drawOverlays() {
  let nova = online && state ? state.nova : "idle";
  if (nova !== "listening" && nova !== "thinking" && nova !== "speaking") nova = "idle";
  const heard = online && state ? state.heard : "";
  // Kept as it was through the fade out, rather than gone as the blob starts to go.
  if (nova !== "idle") setText($("heard"), heard ? `“${heard}”` : "");
  if (nova === novaShown) return;
  const blob = $("nova");
  clearTimeout(novaHiding);
  novaHiding = null;
  if (nova === "idle") {
    blob.className = "nova";
    novaHiding = setTimeout(() => {
      novaHiding = null;
      blob.hidden = true;
      setText($("heard"), "");
    }, NOVA_GOES);
  } else {
    if (blob.hidden) {
      blob.hidden = false;
      // Laid out once without "on", so the change to it is a transition.
      void blob.offsetWidth;
    }
    blob.className = `nova on ${nova}`;
  }
  novaShown = nova;
}

function drawMarks() {
  $("offline").hidden = online !== false;
  $("muted").hidden = !(online && state && state.muted) || novaShown !== "idle";
}

function setBrightness(level) {
  if (typeof level !== "number" || level === brightness || !window.fully) return;
  brightness = level;
  // The kiosk app's brightness goes from 0 to 255, the settings page's from 0 to 100.
  window.fully.setScreenBrightness(Math.round(level * 255 / 100));
}

// Pages

function showPage(index) {
  page = (index + PAGES.length) % PAGES.length;
  PAGES.forEach((id, at) => {
    $(id).hidden = at !== page;
  });
  document.querySelectorAll(".dots span").forEach((dot, at) => dot.classList.toggle("open", at === page));
}

// Touch

function pointOf(finger) {
  return { x: finger.clientX, y: finger.clientY, t: performance.now() };
}

function trackedIn(list) {
  return touch ? Array.from(list).find((finger) => finger.identifier === touch.id) : undefined;
}

// Checks whether the finger down has become a hold - from when it's been down long
// enough, and again later if a timer came early.
function checkHold() {
  if (!touch || touch.held || touch.abandoned) return;
  const now = performance.now();
  const down = now - touch.points[0].t;
  if (NovaGestures.isHold(touch.points, now)) {
    touch.held = true;
    startHold();
  } else if (down < NovaGestures.HOLD_MS) {
    touch.timer = setTimeout(checkHold, NovaGestures.HOLD_MS - down);
  }
}

// Only a decided hold makes Nova listen, never a tap or a swipe, and only while Nova's
// there to ask - a hold while it's offline does nothing, as decision 10 says.
function startHold() {
  if (online !== true) return;
  holdSent = true;
  holding = holding.then(() => ask(API + "hold", { body: { action: "start" } })).catch(() => {});
}

// Ends a hold Nova was told of, with keepalive, so it's sent even if the page is hidden
// or going meanwhile - and as the page goes, straight away rather than after its start,
// which may never come back to a page that's gone.
function endHold(going = false) {
  if (!holdSent) return;
  holdSent = false;
  const send = () => ask(API + "hold", { body: { action: "end" }, keepalive: true });
  if (going) send().catch(() => {});
  else holding = holding.then(send).catch(() => {});
}

function dropTouch(going = false) {
  if (!touch) return;
  clearTimeout(touch.timer);
  if (touch.held) endHold(going);
  touch = null;
}

document.addEventListener("touchstart", (event) => {
  event.preventDefault();
  // One finger on the screen, yet one still followed: its touchend was lost, which
  // would otherwise leave every later touch taken for a second finger. It's let go of,
  // a hold ended, and this touch begun afresh.
  if (touch && event.touches.length === 1) dropTouch();
  if (touch) {
    // A second finger: whatever the first was doing isn't a tap or a swipe now - but a
    // hold already begun carries on until the first finger lifts.
    clearTimeout(touch.timer);
    touch.abandoned = true;
    return;
  }
  const finger = event.changedTouches[0];
  touch = { id: finger.identifier, points: [pointOf(finger)], held: false, abandoned: false, timer: null };
  touch.timer = setTimeout(checkHold, NovaGestures.HOLD_MS);
}, { passive: false });

document.addEventListener("touchmove", (event) => {
  event.preventDefault();
  const finger = trackedIn(event.changedTouches);
  if (finger) touch.points.push(pointOf(finger));
}, { passive: false });

document.addEventListener("touchend", (event) => {
  event.preventDefault();
  const finger = trackedIn(event.changedTouches);
  if (!finger) return;
  touch.points.push(pointOf(finger));
  const { points, held, abandoned } = touch;
  dropTouch();
  if (held || abandoned) return;
  const gesture = NovaGestures.classify(points);
  if (gesture === "tap" || gesture === "swipe-left") showPage(page + 1);
  else if (gesture === "swipe-right") showPage(page - 1);
}, { passive: false });

document.addEventListener("touchcancel", (event) => {
  if (trackedIn(event.changedTouches)) dropTouch();
});

// A held finger the page never hears lift - the kiosk app going to the background,
// say - mustn't leave Nova recording.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) dropTouch();
});
window.addEventListener("pagehide", () => dropTouch(true));
document.addEventListener("contextmenu", (event) => event.preventDefault());

// The volume buttons

// Steps Nova's volume on the clock's satellite - the music's while Nova's music plays
// there, else its voice; Nova decides which - and shows where it is now. Global, as the
// kiosk app calls it by name.
function novaVolume(direction) {
  if (online !== true) return;
  const now = performance.now();
  // Measured from the last press counted, not the last seen, so a button held down and
  // repeating still steps four times a second rather than once.
  if (lastPress && lastPress.direction === direction && now - lastPress.at < SAME_PRESS) return;
  lastPress = { direction, at: now };
  ask(API + "volume", { body: { direction } }).then((answer) => {
    if (answer.status === 200) showVolume(answer.data);
  }).catch(() => {});
}
window.novaVolume = novaVolume;

function showVolume(volume) {
  setText($("volume-what"), volume.category === "music" ? "Music" : "Voice");
  $("volume-level").style.transform = `scaleX(${volume.percent / 100})`;
  setText($("volume-percent"), `${volume.percent}%`);
  $("volume").hidden = false;
  clearTimeout(volumeHiding);
  volumeHiding = setTimeout(() => {
    $("volume").hidden = true;
  }, VOLUME_SHOWN);
}

// The arrow keys stand in for the buttons in a desktop browser, for trying the page out,
// and the volume keys count where a browser passes them to the page, e.g. "AudioVolumeUp".
document.addEventListener("keydown", (event) => {
  if (event.key === "ArrowUp" || event.key === "AudioVolumeUp") novaVolume("up");
  else if (event.key === "ArrowDown" || event.key === "AudioVolumeDown") novaVolume("down");
});

// Anything the Content-Security-Policy stops, said in the console - Fully Kiosk's
// fully.bind() runs its handler as a string, which the policy may turn down on the clock.
document.addEventListener("securitypolicyviolation", (event) => {
  console.error(`Blocked by the page's policy (${event.violatedDirective}): ${event.blockedURI || "inline code"}`);
});

function start() {
  key = readKey();
  // Fully Kiosk's JavaScript interface, on the clock: its volume buttons, which reach
  // the kiosk app as key presses the page itself never sees.
  if (window.fully) {
    window.fully.bind("volumeUp", "novaVolume('up')");
    window.fully.bind("volumeDown", "novaVolume('down')");
  }
  showPage(HOME);
  tick();
  if (!key) {
    showMessage("This clock has no key - add it on Nova's settings page",
      "Then open the address it gives in the clock's kiosk app.");
    return;
  }
  follow();
}

start();
