// Nova's settings page - see voice_assistant/settings_page.py.
//
// Everything shown comes from /api/state, and what Nova's doing right now from
// /api/live, each asked for again every so often while the page is in view. Every
// change is sent with the X-Nova header, which the page only accepts changes with.
// Anything from Nova - a fact it remembers, a line from its log - is put on the page
// as text, never as HTML, and styles are only ever set through the CSSOM, since the
// page's Content-Security-Policy allows nothing inline.
//
// Through NPM the page needs its own password: /api/login-state is asked first, and
// until there's a login only the login form is shown and nothing else is asked for.
// Any API refused later for want of one - a login which ended meanwhile - brings the
// form back.

"use strict";

// How often what Nova's doing, and everything else, is asked for while the page is
// in view, in milliseconds.
const LIVE_EVERY = 1500;
const STATE_EVERY = 20000;

// What's playing, and Home Assistant's lights and scripts and scenes, are asked for
// apart from the rest, as either can be slow - the music more often, as it changes
// more, in milliseconds.
const MUSIC_EVERY = 5000;
const HOME_EVERY = 15000;

// The batteries change slowly, and every look asks Home Assistant, so once a minute.
const BATTERIES_EVERY = 60000;

// The calendars' sign-in changes only when it's done here or removed in Google, and
// Nova reads Google's list of them every five minutes at most, so once a minute too.
const CALENDARS_EVERY = 60000;

// What Home Assistant has to choose from changes when something is added to it, and every
// look asks it, so once a minute too.
const HOME_CHOICES_EVERY = 60000;

// How long the page waits for Nova to begin answering, in milliseconds, before it gives
// up on a request. Without it one that hangs holds up what asks next for good, as each
// of every()'s runs waits for the one before. A change is given longer than a look, as
// some wait on Google or Home Assistant.
const LOOK_WITHIN = 30000;
const CHANGE_WITHIN = 60000;

// The least time between volume changes sent while a slider's dragged, in
// milliseconds - often enough for the beep to keep up with the finger, without
// asking for every step - and between a light's, which take Home Assistant longer.
const VOLUME_EVERY = 180;
const LIGHT_EVERY = 250;

// How long Undo is offered once something's removed, in milliseconds. Nova keeps what
// was removed a little longer - see voice_assistant/undo.py.
const UNDO_SHOWN = 10000;

// The least time between two reloads for an update, the first counted from when the page
// was loaded, in milliseconds - so a brain in a crash loop, or its version flipping
// mid-update, makes the page reload no more than once a minute.
const RELOAD_GAP = 60000;

// What can be typed in, so a reload would lose it - a tapped switch or slider keeps the
// focus in Chromium, but nothing half-done goes with it, as each sends its change at once.
const TYPED = new Set(["text", "password", "number", "time", "url", "search", "email", "tel", "textarea"]);

// The forms always on show, which empty themselves once sent - so anything in one which
// differs from how the page drew it is half done, wherever the focus has gone since: a
// time picked for an alarm, then its Repeats tapped, say.
const ALWAYS_SHOWN = ["add-alarm", "add-satellite", "play-something"];

// The colours offered as swatches on a light which can be set to one, as a hue in
// degrees and a saturation in percent.
const SWATCHES = [
  { name: "Red", hue: 0, saturation: 100 },
  { name: "Orange", hue: 28, saturation: 100 },
  { name: "Yellow", hue: 52, saturation: 100 },
  { name: "Green", hue: 120, saturation: 100 },
  { name: "Cyan", hue: 180, saturation: 100 },
  { name: "Blue", hue: 240, saturation: 100 },
  { name: "Purple", hue: 275, saturation: 100 },
  { name: "Pink", hue: 325, saturation: 70 },
];

// The warmest and coolest white a light's icon is tinted as, in red, green and blue.
const WARM_WHITE = [255, 177, 90];
const COOL_WHITE = [214, 230, 255];

// How many playlists are offered before the rest are tucked behind a button.
const MOST_PLAYLISTS = 12;

// How many of the recent problems are shown, each cut to two lines, before "Show all" -
// enough to see whether anything's wrong without scrolling past eight on a phone.
const FEWEST_PROBLEMS = 3;

// Said in the Music section when there's no player and Nova doesn't say why.
const NO_PLAYER = "Nova has no music player set up. Choose one in Settings, under Music.";

// What Nova's doing, in words. While it waits, it's waiting for its wake word, which
// /api/state says.
const DOING = { listening: "Listening", thinking: "Thinking", speaking: "Speaking", offline: "Can't reach Nova" };

const VOLUMES = {
  general: { label: "Voice", icon: "i-voice" },
  alarm: { label: "Timers and alarms", icon: "i-alarm" },
  music: { label: "Music", icon: "i-music" },
};

// The settings, grouped by what they're for. One that's under none of these goes
// under Other, so a new setting turns up on the page without it changing.
const GROUPS = [
  ["Music", ["music.", "navidrome.", "spotify."]],
  ["Voice", ["voice."]],
  ["Sounds", ["sounds."]],
  ["Claude", ["web_search.", "conversation."]],
  ["Home", ["home_assistant.", "mqtt."]],
  ["Notifications", ["ntfy."]],
  ["Listening", ["thresholds."]],
  ["Privacy", ["logging."]],
];

// The night's two settings - quiet hours, which there are none of while they're the same.
const NIGHT = ["ntfy.quiet_from", "ntfy.quiet_until"];

const DAY = 24 * 60 * 60 * 1000;

let activity = "idle";
let wakeWord = "Hey Nova";
// Whether the microphone's muted, as /api/state last said - Home Assistant can change it too.
let muted = false;

// The satellites, as /api/state last said, and the one the user has chosen to look
// after - or null for the default. The choice is only offered when there's more than one.
let satellites = [];
let chosen = null;

// The timers on the page, ticked down every second, as [where it's shown, when it's due].
let counting = [];

// What each list on the page was last drawn from, so it's only drawn again when that
// changes - rather than every time, sweeping away a button that's been tabbed to.
const drawn = {};

// Each volume's slider, and each light, kept rather than drawn again, so one being
// dragged isn't replaced from under the finger.
const sliders = {};
const lights = {};

// What /api/music last said, and when, for how far through the song is meanwhile -
// and whether every playlist is shown.
let music = null;
let musicAt = 0;
let allPlaylists = false;

// The recent problems, newest first, as /api/state last said - and whether all of them are
// shown, whole.
let recentProblems = [];
let allProblems = false;

// What /api/home last said.
let home = null;

// What /api/batteries last said.
let batteries = null;

// What /api/calendars said last.
let calendars = null;

// What /api/home-choices said last.
let homeChoices = null;

// Whether the save bar offers to restart Nova rather than to save - once a save has
// something which waits for a restart, while nothing else is waiting to be saved - and
// what that save said, which names what waits.
let offerRestart = false;
let restartSaid = "";

// Whether the page is waiting for a login, as it is through NPM without one - when
// nothing is asked for, as Nova would refuse it all - and what /api/login-state said
// last: which route the page was reached by, and whether the page password is set.
let locked = true;
let loginState = null;

// Whether login-state has been read since load() last looked - so the first load, straight
// after the look at start-up, doesn't ask again - and whether the page has looked at all
// yet, as only the first look moves the focus to the password, never one mid-use.
let loginFresh = false;
let firstLook = true;

// Each of every()'s runs, to be started again once the page is unlocked.
const runners = [];

// The version of the page's own files the brain had when the page first asked, whether
// a later answer has said another - the brain's been updated since, and the page is out
// of date - and when the page last loaded or reloaded, for RELOAD_GAP. Also the notice
// shown while it waits to reload, if it is.
let pageVersion = null;
let updated = false;
let reloadedAt = Date.now();
let updateNotice = null;

// Whether the brain's being asked if it's up, just before a reload.
let reloadChecking = false;

// Making things

function element(tag, text, className) {
  const made = document.createElement(tag);
  if (text !== undefined) made.textContent = text;
  if (className) made.className = className;
  return made;
}

// One of the icons in the page's sprite.
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${name}`);
  svg.append(use);
  return svg;
}

// A coloured dot for how something's doing: "good", "bad", "warning" or "off". What it
// means is always said in words beside it too.
function dot(mood) {
  const made = element("span", undefined, `dot ${mood}`);
  made.setAttribute("aria-hidden", "true");
  return made;
}

function emptyRow(text, mood) {
  const row = element("li", undefined, "empty");
  if (mood) row.append(dot(mood));
  row.append(element("span", text, "what"));
  return row;
}

function removeButton(label, onClick) {
  const button = element("button", undefined, "icon-button danger");
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.append(icon("i-close"));
  button.addEventListener("click", onClick);
  return button;
}

function capitalised(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function counted(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

// Times

function clockTime(date) {
  return date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
}

// Today, tomorrow, yesterday, a day this week, or a date.
function dayOf(date) {
  const midnight = (day) => new Date(day.getFullYear(), day.getMonth(), day.getDate());
  const days = Math.round((midnight(date) - midnight(new Date())) / DAY);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  if (Math.abs(days) < 7) return date.toLocaleDateString("en-GB", { weekday: "long" });
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

function since(date) {
  const day = dayOf(date);
  return day === "Today" ? `Since ${clockTime(date)}` : `Since ${day === "Yesterday" ? "yesterday" : day}`;
}

// How long something's been going, roughly.
function duration(seconds) {
  const minutes = Math.floor(seconds / 60);
  if (minutes < 1) return "Just started";
  if (minutes < 60) return counted(minutes, "minute", "minutes");
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return counted(hours, "hour", "hours");
  const days = Math.floor(hours / 24);
  const left = hours % 24;
  return left ? `${counted(days, "day", "days")} ${counted(left, "hour", "hours")}` : counted(days, "day", "days");
}

// How long a timer has left, as a kitchen timer shows it.
function countdown(dueAt) {
  const left = Math.max(0, Math.round(dueAt - Date.now() / 1000));
  if (!left) return "Now";
  const pad = (number) => String(number).padStart(2, "0");
  if (left >= 3600) return `${Math.floor(left / 3600)}:${pad(Math.floor((left % 3600) / 60))}:${pad(left % 60)}`;
  return `${Math.floor(left / 60)}:${pad(left % 60)}`;
}

// When a problem was logged, from the log's own "2026-10-20 17:02" - its time if it was
// today, and otherwise just its day.
function loggedAt(text) {
  const [date, time] = text.split(" ");
  const [year, month, day] = date.split("-").map(Number);
  const [hour, minute] = time.split(":").map(Number);
  const when = dayOf(new Date(year, month - 1, day, hour, minute));
  return when === "Today" ? time : when;
}

// The time, and the timers, every second.
function tick() {
  const now = new Date();
  document.getElementById("time").textContent = clockTime(now);
  document.getElementById("date-long").textContent =
    now.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  document.getElementById("date-short").textContent =
    now.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  for (const [shown, dueAt] of counting) shown.textContent = countdown(dueAt);
  drawProgress();
  // One that's just gone off is asked about again, to show whether it's still ringing.
  if (counting.some(([, dueAt]) => dueAt * 1000 <= now)) {
    counting = counting.filter(([, dueAt]) => dueAt * 1000 > now);
    load();
  }
  setTimeout(tick, 1000 - (Date.now() % 1000) + 20);
}

// Talking to Nova

// Says how a change went, for a few seconds.
function toast(message, ok) {
  const shown = document.getElementById("toast");
  shown.textContent = message;
  shown.classList.toggle("bad", ok === false);
  shown.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => shown.classList.remove("show"), 4000);
}

// Says something was removed, with Undo, which sends back the token Nova answered
// with. Each removal has a toast of its own, stacked above the one for everything
// else, so a second removal doesn't take away the first's Undo. It goes once it's
// worked, or after UNDO_SHOWN - one refused, as it would clash with an alarm set
// since, can be tried again meanwhile.
function undoToast(message, token) {
  const shown = element("div", message, "toast undoable");
  shown.setAttribute("role", "status");
  const button = element("button", "Undo", "button quiet");
  button.type = "button";
  let timer;
  const gone = () => {
    clearTimeout(timer);
    shown.remove();
  };
  button.addEventListener("click", async () => {
    button.disabled = true;
    const result = await change("/api/undo", { token });
    if (result.success) gone();
    else button.disabled = false;
  });
  shown.append(button);
  document.getElementById("undos").append(shown);
  // Shown a frame after it's in the page, so it can fade in as the other toast does.
  requestAnimationFrame(() => shown.classList.add("show"));
  timer = setTimeout(gone, UNDO_SHOWN);
}

// A path which asks after the chosen satellite, if there is one.
function forChosen(path) {
  if (!chosen) return path;
  return `${path}${path.includes("?") ? "&" : "?"}satellite=${encodeURIComponent(chosen)}`;
}

// fetch, giving up with an AbortError if Nova hasn't begun to answer in time - which
// leaves out the answer's body, small and sent with its start.
function fetchWithin(path, options = {}, within = LOOK_WITHIN) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), within);
  return fetch(path, { ...options, signal: controller.signal }).finally(() => clearTimeout(timer));
}

// What Nova answered - and, refused for want of a login, the login form instead of
// the page. Through NPM that's a login which ended since the page was shown: 30 days
// on, or Log out everywhere on another device.
function answered(response) {
  if (response.status === 401) lockAgain();
  return response;
}

// Sends Nova a change, answering with what it said - for the chosen satellite, which
// only the changes to its own volumes, music, lights, mute and alarms look at.
async function send(path, body) {
  let response;
  try {
    response = answered(await fetchWithin(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Nova": "page" },
      body: JSON.stringify(chosen ? { ...body, satellite: chosen } : body || {}),
    }, CHANGE_WITHIN));
  } catch (error) {
    // Said as that rather than as unreachable: Nova got it, and may have done it.
    if (error.name === "AbortError") {
      return { success: false, message: "Nova didn't answer in time, so that may or may not have worked." };
    }
    throw error;
  }
  return response.json().catch(() => ({ success: false, message: "Nova didn't answer properly." }));
}

// Sends a change, says how it went, and shows the page as it is now - or, as reload,
// the part of it which changed.
async function change(path, body, reload = load) {
  let result;
  try {
    result = await send(path, body);
  } catch (error) {
    result = { success: false, message: "Couldn't reach Nova." };
  }
  const message = result.message || (result.success ? "Done." : "That didn't work.");
  if (result.undo) undoToast(message, result.undo);
  else toast(message, result.success);
  await reload();
  return result;
}

async function load() {
  // Whether the page password is set may have changed on another device.
  if (loginFresh) loginFresh = false;
  else await checkLogin();
  if (locked) return;
  let state;
  try {
    const response = answered(await fetchWithin(forChosen("/api/state")));
    if (!response.ok) throw new Error(await response.text());
    state = await response.json();
  } catch (error) {
    // What's shown stays, and the star says Nova can't be reached.
    return;
  }
  wakeWord = state.wake_word || wakeWord;
  muted = Boolean(state.muted);
  satellites = state.satellites;
  // The one looked after was removed on another device. Nova answered with the default's,
  // so the page goes back to it too, rather than keep a name no chip is lit for. Not when
  // the file can't be read, which leaves no list to tell it by.
  if (chosen && !state.satellites_unreadable && !satellites.some((satellite) => satellite.name === chosen)) {
    choose(null);
    return;
  }
  showActivity();
  showStatus(state.status);
  showSatellites(state);
  showMuted();
  showVolumes(state.volumes, state.satellite);
  drawIfChanged("clock", state.clock, showClock);
  drawIfChanged("memories", state.memories, showMemories);
  drawIfChanged("own-routines", state.routines, showOwnRoutines);
  drawIfChanged("keys", state.keys, showKeys);
  drawIfChanged("settings", state.settings, showSettings);
  showSettingsProblem(state.settings_problem);
  document.getElementById("restart").hidden = !state.restart_needed;
  // Restarted since, or the saved settings put back as they're running.
  if (offerRestart && !state.restart_needed) {
    offerRestart = false;
    countChanges();
  }
  noticeVersion(state.page);
}

// Whether Stop's been pressed and the brain's not yet answered. The quick check can
// land in that time, while what rings still does, and would show Stop again.
let stopping = false;

async function live() {
  let now = "offline";
  let stoppable = false;
  try {
    const response = answered(await fetchWithin(forChosen("/api/live")));
    if (response.ok) {
      const said = await response.json();
      now = said.activity;
      stoppable = Boolean(said.stoppable);
    }
  } catch (error) {
    // Out of reach, then.
  }
  // Stop is there only while there's something to stop, on any satellite.
  document.getElementById("stop").hidden = !stoppable || stopping;
  // Back after a restart, or being out of reach - so anything else may have changed too.
  if (activity === "offline" && now !== "offline") {
    load();
    loadMusic();
    loadHome();
    loadBatteries();
    loadCalendars();
    loadHomeChoices();
  }
  activity = now;
  showActivity();
  // Asked for often, so a reload held back by editing happens soon after it ends.
  reloadWhenReady();
}

// Runs work now, and again every so often while the page is in view - never two at
// once, and not at all while it's hidden, so a phone left on the page isn't kept busy.
function every(milliseconds, work) {
  let timer = null;
  let running = false;
  async function run() {
    clearTimeout(timer);
    timer = null;
    if (running || document.hidden || locked) return;
    running = true;
    try {
      await work();
    } finally {
      running = false;
      if (!document.hidden) timer = setTimeout(run, milliseconds);
    }
  }
  document.addEventListener("visibilitychange", run);
  runners.push(run);
  run();
}

// Logging in, through NPM

// Asks whether the page needs a login, and shows the page or the login form as it
// says. Out of reach, what's shown stays - or, the first time, the page is shown, and
// a 401 from anything it asks for brings the form.
//
// Returns:
//   Whether Nova said.
async function checkLogin() {
  const first = firstLook;
  firstLook = false;
  try {
    const response = await fetchWithin("/api/login-state");
    if (!response.ok) throw new Error(await response.text());
    loginState = await response.json();
  } catch (error) {
    if (!loginState) unlock();
    return false;
  }
  loginFresh = true;
  document.getElementById("log-out").hidden = loginState.route !== "npm";
  drawIfChanged("page-password", { route: loginState.route, set: loginState.password_set }, showPagePassword);
  if (loginState.route === "npm" && !loginState.logged_in) lock(first);
  else unlock();
  noticeVersion(loginState.page);
  return true;
}

// Only the login form - or, with no page password yet, where to set one. Nothing's
// asked for until it's sent, so a page left locked asks Nova nothing at all.
function lock(focus) {
  locked = true;
  document.body.classList.add("locked");
  const form = document.getElementById("login");
  const opening = form.hidden;
  form.hidden = !loginState.password_set;
  document.getElementById("login-first").hidden = loginState.password_set;
  if (focus && opening && !form.hidden) document.getElementById("login-password").focus();
}

// A 401: the login has ended, so the form is shown again - once, however many of the
// page's asks were refused. Only NPM's route ever says 401, so the form is shown even
// if login-state couldn't be read at start-up, taking the page password as set - or
// failing to read it would unlock the page, whose asks would be refused again, round
// and round as fast as Nova answers.
function lockAgain() {
  if (locked) return;
  loginState = loginState || { route: "npm", password_set: true, logged_in: false };
  lock(false);
  checkLogin();
}

// The page, and everything it asks for started again.
function unlock() {
  document.getElementById("login").hidden = true;
  document.getElementById("login-first").hidden = true;
  document.getElementById("login-said").hidden = true;
  if (!locked) return;
  locked = false;
  document.body.classList.remove("locked");
  for (const run of runners) run();
}

// Sends what was typed, which is gone from the field as it's sent - the field is the
// only place it's ever kept - and shows Nova's answer if it's refused.
async function logIn(event) {
  event.preventDefault();
  const field = document.getElementById("login-password");
  const said = document.getElementById("login-said");
  const button = event.target.querySelector("button");
  if (!field.value) return;
  button.disabled = true;
  // The body is made as send is called, so the field can be emptied straight after.
  const sent = send("/api/login", { password: field.value });
  field.value = "";
  let result;
  try {
    result = await sent;
  } catch (error) {
    result = { success: false, message: "Couldn't reach Nova." };
  }
  button.disabled = false;
  if (result.success) {
    const told = await checkLogin();
    // Logged in, yet the page can't be shown: Nova out of reach as it's asked, or the
    // browser didn't send the login back.
    if (!told) result = { message: "Logged in, but Nova couldn't be reached to show the page - try again in a moment." };
    else if (!locked) return;
    else result = { message: "Logged in, but this browser didn't keep it - check it's the https:// address, with cookies allowed." };
  }
  said.textContent = result.message || "That didn't work.";
  said.hidden = false;
  field.focus();
}

// A page left open across an update

// Notes the version of the page's files Nova says it has: the first is the page's own,
// and any other means it's out of date.
function noticeVersion(version) {
  if (!version) return;
  pageVersion = pageVersion || version;
  updated = version !== pageVersion;
  reloadWhenReady();
}

// Reloads an out-of-date page - straight away unless something's being edited, which a
// reload would lose, or it reloaded less than RELOAD_GAP ago. Meanwhile it says so, with
// a button to reload when the user's ready.
async function reloadWhenReady() {
  if (updated && await readyToReload()) {
    reloadedAt = Date.now();
    location.reload();
    return;
  }
  if (!updated) {
    if (updateNotice) updateNotice.remove();
    updateNotice = null;
    return;
  }
  if (updateNotice) return;
  // As an Undo's toast, so it stays until it's used - here, until the page reloads.
  updateNotice = element("div", "Nova has been updated - reload when you're ready", "toast undoable");
  updateNotice.setAttribute("role", "status");
  const button = element("button", "Reload", "button quiet");
  button.type = "button";
  button.addEventListener("click", () => location.reload());
  updateNotice.append(button);
  document.getElementById("undos").append(updateNotice);
  const shown = updateNotice;
  requestAnimationFrame(() => shown.classList.add("show"));
}

// Whether the page may reload now: nothing's being edited, RELOAD_GAP has passed, and the
// brain answers with the new version just before - as in a crash loop it can be down
// again by then, and a reload onto a brain that's down leaves the browser's own error
// page, with nothing left to try again. Not answering, it waits for live() to hear
// from Nova again, and asks then.
async function readyToReload() {
  if (reloadChecking || activity === "offline" || editingSomething() || Date.now() - reloadedAt < RELOAD_GAP) {
    return false;
  }
  reloadChecking = true;
  let said = null;
  try {
    const response = await fetchWithin("/api/login-state");
    if (response.ok) said = await response.json();
  } catch (error) {
    // Down again, then.
  } finally {
    reloadChecking = false;
  }
  if (!said || !said.page) return false;
  // Back to the page's own files, or something begun while it was asked.
  updated = said.page !== pageVersion;
  return updated && !editingSomething();
}

// Whether a field in one of the forms always on show differs from how the page drew it -
// only what can be typed in or chosen from, as a button has no defaultValue to compare.
function changedField(field) {
  if (field.type === "select-one") {
    const options = [...field.options];
    // With none marked as chosen in the HTML, a select starts on its first option.
    const first = options.find((option) => option.defaultSelected) || options[0];
    return Boolean(first) && field.value !== first.value;
  }
  return TYPED.has(field.type) && field.value !== field.defaultValue;
}

// Whether anything's being typed or changed which a reload would lose: a field with the
// focus, anything in a form always on show, an editor or question open, a sign-in half
// done, a key or the page password half put in, settings not yet saved, the login form -
// or a satellite's password, which is shown only the once, on its way or on show.
function editingSomething() {
  if (passwordComing) return true;
  if (TYPED.has(document.activeElement && document.activeElement.type)) return true;
  if (ALWAYS_SHOWN.some((id) => [...document.getElementById(id).elements].some(changedField))) return true;
  const open = ["routine-editor", "renew-panel", "password-panel", "spotify-finish", "google-finish", "login"];
  if (open.some((id) => !document.getElementById(id).hidden)) return true;
  return Boolean(document.getElementById("keys").querySelector(".key.open")
    || document.getElementById("page-password").querySelector(".key.open")
    || document.getElementById("settings").querySelector(".changed"));
}

// Draws part of the page again if what it's drawn from has changed - unless drawing
// it says not now, by giving false, when it's tried again next time.
function drawIfChanged(part, data, draw) {
  const now = JSON.stringify(data);
  if (drawn[part] !== now && draw(data) !== false) drawn[part] = now;
}

// What it shows

function showActivity() {
  document.getElementById("star").dataset.activity = activity in DOING ? activity : "idle";
  document.getElementById("doing").textContent =
    DOING[activity] || (muted ? "Microphone muted" : `Waiting for “${wakeWord}”`);
}

function tile(about) {
  const made = element("div", undefined, "tile");
  made.append(icon(about.icon));
  if (about.mood) made.append(dot(about.mood));
  made.append(element("span", about.label, "label"), element("span", about.value, "value"));
  if (about.detail) made.append(element("p", about.detail, "detail"));
  return made;
}

// Where what's said is turned into text.
function hearing(speechToText) {
  // Nemotron, on the brain, which hears every satellite.
  const about = { icon: "i-server", label: "Speech to text", value: "Nemotron" };
  if (speechToText === "ready") return { ...about, detail: "On the brain - ready", mood: "good" };
  return { ...about, detail: "On the brain - missing", mood: "bad" };
}

function notifications(ntfy) {
  const about = { icon: "i-bell", label: "Notifications" };
  if (ntfy === null) return { ...about, value: "Off", detail: "ntfy isn't set up" };
  if (ntfy === "listening") return { ...about, value: "Listening", detail: "For alerts on ntfy", mood: "good" };
  return { ...about, value: "Not connected", detail: "Can't reach ntfy", mood: "bad" };
}

function showStatus(status) {
  // From when the user stops talking to the reply being ready.
  const reply = status.reply_ms === null
    ? { icon: "i-reply", label: "Typical reply", value: "Not timed yet", detail: "Ask Nova something" }
    : { icon: "i-reply", label: "Typical reply", value: `${(status.reply_ms / 1000).toFixed(1)}s`,
        detail: `Over ${counted(status.replies, "reply", "replies")}` };
  document.getElementById("status").replaceChildren(...[
    { icon: "i-star", label: "Running", value: duration(Date.now() / 1000 - status.started),
      detail: since(new Date(status.started * 1000)) },
    reply,
    hearing(status.speech_to_text),
    notifications(status.ntfy),
  ].map(tile));

  recentProblems = status.problems;
  showProblems();
}

// The latest few problems, each cut to two lines, or all of them whole. "Show all" is
// offered while any are left out or cut short - so one long problem can still be read.
function showProblems() {
  const shown = allProblems ? recentProblems : recentProblems.slice(0, FEWEST_PROBLEMS);
  const problems = shown.map((problem) => {
    const serious = problem.level !== "warning";
    const row = element("li");
    const what = element("span", undefined, "what");
    what.append(element("span", serious ? "Problem: " : "Warning: ", "visually-hidden"), problem.message);
    const when = element("span", loggedAt(problem.time), "when");
    when.title = problem.time;
    row.append(dot(serious ? "bad" : "warning"), what, when);
    return row;
  });
  const list = document.getElementById("problems");
  list.replaceChildren(...(problems.length ? problems : [emptyRow("No problems lately.", "good")]));
  // Measured as they'd be cut, even while all are shown, so that once nothing's left out
  // or cut short any more there's no "Show fewer" that would change nothing.
  list.classList.add("clamped");
  const cut = [...list.querySelectorAll(".what")].some((what) => what.scrollHeight > what.clientHeight);
  if (recentProblems.length <= FEWEST_PROBLEMS && !cut) allProblems = false;
  list.classList.toggle("clamped", !allProblems);
  const all = document.getElementById("problems-all");
  all.hidden = !allProblems && recentProblems.length <= FEWEST_PROBLEMS && !cut;
  all.textContent = allProblems ? "Show fewer" : "Show all";
  all.setAttribute("aria-expanded", String(allProblems));
}

// Each satellite, which is the default, and whether it's connected - with a way to
// give it a new password, and a choice of which to look after below, when there's
// more than one.
function showSatellites(state) {
  const buttons = new Map();
  const rows = satellites.map((satellite) => {
    const row = element("li");
    const what = element("span", undefined, "what");
    what.append(satellite.name);
    // What belongs to no satellite - ntfy's alerts, say - goes to this one.
    if (satellite.default) what.append(element("span", "Default", "badge"));
    what.append(element("span",
      satellite.connected ? (DOING[satellite.activity] || (satellite.muted ? "Microphone muted" : "Listening for its wake word"))
        : "Not connected", "detail"));
    if (satellite.spotify_speaker) what.append(element("span", `Spotify plays on ${satellite.spotify_speaker}`, "detail"));
    const actions = element("span", undefined, "actions");
    if (!satellite.default) {
      const makeDefault = element("button", "Make default", "button quiet");
      makeDefault.type = "button";
      makeDefault.setAttribute("aria-label", `Make ${satellite.name} the default`);
      makeDefault.addEventListener("click", () => change("/api/satellites/default", { name: satellite.name }));
      actions.append(makeDefault);
    }
    const renew = element("button", "New password", "button quiet");
    renew.type = "button";
    renew.setAttribute("aria-label", `Give ${satellite.name} a new password`);
    renew.addEventListener("click", () => newPassword(satellite.name));
    buttons.set(satellite.name, renew);
    const remove = element("button", "Remove", "button quiet danger");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${satellite.name}`);
    remove.addEventListener("click", () => removeSatellite(satellite));
    actions.append(renew, remove);
    row.append(dot(satellite.connected ? "good" : "off"), what, actions);
    return row;
  });
  // A broken satellites file is said as that, not taken for there being none.
  const none = state.satellites_unreadable
    ? "The satellites' file can't be read, so they can't be shown or changed - see the brain's log."
    : "No satellites have been added.";
  drawIfChanged("satellites", [satellites, none], () => {
    document.getElementById("satellites").replaceChildren(...(rows.length ? rows : [emptyRow(none)]));
    renewButtons = buttons;
  });
  // Its satellite removed - here or on another device - the question has nothing left to
  // ask about, and left open it would hold back an update reload for good.
  if (renewing !== null && !satellites.some((satellite) => satellite.name === renewing)) {
    const panel = document.getElementById("renew-panel");
    if (panel.contains(document.activeElement)) cancelRenew();
    else closeRenew();
  }

  // With more than one, the one looked after is named at the top, and over the sections
  // which are its own: "Nova · bedroom", "Sound · bedroom". The dot's held to the name, so
  // a line too narrow for both breaks before it, not after.
  const whose = satellites.length > 1 && state.satellite ? ` ·\u00a0${state.satellite}` : "";
  for (const shown of document.querySelectorAll(".whose")) {
    shown.textContent = whose;
    shown.hidden = !whose;
  }

  const choice = document.getElementById("satellite-choice");
  choice.hidden = satellites.length < 2;
  drawIfChanged("satellite-choice", [satellites.map((satellite) => satellite.name), state.satellite], () => {
    choice.replaceChildren(...satellites.map((satellite) => {
      const chip = element("button", satellite.name, "button quiet");
      chip.type = "button";
      chip.setAttribute("aria-pressed", String(satellite.name === state.satellite));
      chip.addEventListener("click", () => choose(satellite.name));
      return chip;
    }));
  });

  // The routine editor's "Runs on" too, while it's open, as satellites come and go -
  // keeping the choice made if it's still offered. Closed, it's drawn as it opens.
  drawIfChanged("runs-on", satellites.map((satellite) => [satellite.name, satellite.default]), () => {
    if (document.getElementById("routine-editor").hidden) return false;
    showRunsOn(document.getElementById("routine-satellite").value);
  });
}

// Asks before giving a satellite a new password, as it's let go until its setup has the
// new one - and a "Cancel / OK" was once clicked through, which shut a Pi out for an hour.
// So its name has to be typed first: the button waits for exactly that, and Cancel is
// not an answer until then.
let renewing = null;
// Each satellite's "New password" button as drawn, by its name - for the focus to go back to.
let renewButtons = new Map();
// Whether a new password's been asked for and not yet shown - the question's closed by
// then, so without this the page counts as not being edited, and an update reload could
// land between the two and lose the password for good.
let passwordComing = false;

function newPassword(name) {
  renewing = name;
  document.getElementById("renew-about").replaceChildren(
    `Give ${name} a new password? Nova stops answering on ${name} until you run `,
    element("code", "bash setup.sh --new-password"),
    " on it and paste the password shown next.");
  document.getElementById("renew-label").textContent = `Type ${name} to go ahead`;
  const field = document.getElementById("renew-name");
  field.value = "";
  document.getElementById("renew-go").disabled = true;
  closePassword();
  document.getElementById("renew-panel").hidden = false;
  field.focus();
}

function closeRenew() {
  renewing = null;
  document.getElementById("renew-name").value = "";
  document.getElementById("renew-go").disabled = true;
  document.getElementById("renew-panel").hidden = true;
}

// Closes the question with the focus back where it was asked from - its satellite's
// "New password" button, or the satellites' heading once that row's gone - so a keyboard
// isn't dropped back at the top of the page.
function cancelRenew() {
  const button = renewButtons.get(renewing);
  closeRenew();
  (button || document.getElementById("satellites-title")).focus();
}

// Sent once, and only with the name typed exactly - the button's disabled otherwise, but
// Enter in the field submits the form all the same.
async function giveNewPassword() {
  const name = renewing;
  if (name === null || document.getElementById("renew-name").value !== name) return;
  closeRenew();
  passwordComing = true;
  try {
    const result = await change("/api/satellites/password", { name });
    if (result.success && result.password) showPassword(name, result.password, false);
  } finally {
    passwordComing = false;
  }
}

// Removes a satellite, once the user's sure - asked with what happens to what was
// its, from what the page knows: where its timers and alarms go, whether the default
// moves, and whether it's away with a copy of its alarms nothing here can empty.
// Asked from Nova's state afresh, not the last look's, which can be twenty seconds
// old - long enough to have shut the Pi down since. Gone in the moment between,
// Nova's answer says it still has its copy.
async function removeSatellite(shown) {
  const name = shown.name;
  await load();
  const satellite = satellites.find((other) => other.name === name);
  if (!satellite) return;
  const others = satellites.filter((other) => other.name !== name);
  const next = satellite.default ? others[0]?.name : satellites.find((other) => other.default)?.name;
  let question = `Remove ${name}? It's let go now, and can't connect again until it's added again.`;
  question += next
    ? ` Its timers and alarms move to ${next}, and routines that ran on it run there instead.`
    : " Its timers and alarms go to the next satellite added.";
  if (satellite.default && next) question += ` ${next} becomes the default.`;
  if (!satellite.connected) {
    question += " It still has a copy of its alarms for the next week. If it's switched on before then, it'll ring"
      + " them too, so wipe it or connect it first.";
  }
  if (!confirm(question)) return;
  // Looking after it, the page goes back to the default.
  await change("/api/satellites/remove", { name }, async () => (chosen === name ? choose(null) : load()));
}

// A satellite's password, just made. It's put straight into the panel's text and
// kept nowhere else once this returns - in no variable, nor anything the browser
// keeps - and closing the panel takes it off the page.
function showPassword(name, password, added) {
  closeRenew();
  document.getElementById("password-about").textContent = added
    ? `The ${name} satellite's password, shown only this once:`
    : `The ${name} satellite's new password, shown only this once:`;
  document.getElementById("password-text").textContent = password;
  document.getElementById("password-how").replaceChildren(...(added
    ? ["Type this into setup.sh on the Pi."]
    : [`Nova stops answering on ${name} until you run `, element("code", "bash setup.sh --new-password"),
       " on it and paste this password in - or, on a Pi not yet updated, put it after ",
       element("code", "NOVA_BACKEND_TOKEN="), " in ", element("code", "~/.config/nova/.env"), " and restart the satellite."]));
  const copy = document.getElementById("password-copy");
  copy.textContent = "Copy";
  document.getElementById("password-panel").hidden = false;
  copy.focus();
}

function closePassword() {
  document.getElementById("password-text").textContent = "";
  document.getElementById("password-panel").hidden = true;
}

// Looks after another satellite, showing what's its own from the start.
function choose(name) {
  chosen = name;
  for (const part of ["satellite-choice", "clock", "lights", "routines"]) delete drawn[part];
  for (const category of Object.keys(sliders)) {
    sliders[category].remove();
    delete sliders[category];
  }
  music = null;
  home = null;
  load();
  live();
  loadMusic();
  loadHome();
}

function showMuted() {
  document.getElementById("mute").checked = muted;
  document.getElementById("microphone").classList.toggle("muted", muted);
  document.getElementById("mute-detail").textContent = muted
    ? `Not listening for “${wakeWord}”. Alarms and timers still ring.`
    : `Listening for “${wakeWord}”`;
}

// The volumes of the satellite named, which is the default's unless another is looked after.
function showVolumes(volumes, name) {
  const none = Object.keys(volumes).length === 0;
  // A satellite which isn't connected has no volumes to show. It's said by its being
  // connected, so the note never blames the connection for volumes missing for another
  // reason; with no satellite to ask after, there being no volumes is all there is to go on.
  const shown = satellites.find((satellite) => satellite.name === name);
  document.getElementById("volumes-missing").hidden = shown ? shown.connected : !none;
  document.getElementById("mute").disabled = none;
  if (none) {
    for (const category of Object.keys(sliders)) {
      sliders[category].remove();
      delete sliders[category];
    }
  }
  for (const [category, percent] of Object.entries(volumes)) {
    sliders[category] = sliders[category] || volumeSlider(category);
    sliders[category].show(percent);
  }
}

// Sends a slider's newest value as it's dragged - one at a time, so they arrive in
// order, each the latest when it's sent, and no closer together than spacing, in
// milliseconds. deliver sends one, and mustn't fail.
function steadily(spacing, deliver) {
  let latest = null;
  let sent = null;
  let sentAt = 0;
  let sending = false;
  let waiting = null;

  function push() {
    if (sending || waiting) return;
    const wait = sentAt + spacing - Date.now();
    if (wait > 0) {
      waiting = setTimeout(() => {
        waiting = null;
        push();
      }, wait);
      return;
    }
    if (latest === sent) return;
    const value = latest;
    sending = true;
    sent = value;
    sentAt = Date.now();
    deliver(value).finally(() => {
      sending = false;
      push();
    });
  }

  return {
    send(value) {
      latest = value;
      push();
    },
    // Whether a value's on its way, or waiting to be.
    get busy() {
      return sending || waiting !== null;
    },
    // What the other end has now, which needn't be sent.
    settled(value) {
      latest = sent = value;
    },
  };
}

// Draws a slider at a level, with its fill and the number beside it.
function drawLevel(slider, output, percent) {
  slider.value = percent;
  output.textContent = `${percent}%`;
  slider.style.setProperty("--level", `${percent}%`);
}

// A volume's slider. As it's dragged, each new level is sent, and Nova beeps at it.
function volumeSlider(category) {
  const about = VOLUMES[category] || { label: capitalised(category), icon: "i-sound" };
  const id = `volume-${category}`;
  const made = element("div", undefined, "tile volume");
  const label = element("label", about.label, "label");
  label.htmlFor = id;
  const output = element("output", undefined, "value");
  output.setAttribute("for", id);
  const slider = element("input");
  Object.assign(slider, { type: "range", id, min: 0, max: 100, step: 1 });
  made.append(icon(about.icon), label, output, slider);
  document.getElementById("volumes").append(made);

  let held = false;
  const sender = steadily(VOLUME_EVERY, (percent) => send("/api/volume", { category, percent })
    .then((result) => {
      if (!result.success) toast(result.message || "Couldn't change the volume.", false);
    })
    .catch(() => toast("Couldn't reach Nova.", false)));
  slider.addEventListener("input", () => {
    held = true;
    drawLevel(slider, output, Number(slider.value));
    sender.send(Number(slider.value));
  });
  slider.addEventListener("change", () => {
    held = false;
    sender.send(Number(slider.value));
  });

  return {
    remove: () => made.remove(),
    // Shows the level Nova has - unless the slider's being moved, or a change to it
    // is on its way.
    show(percent) {
      if (held || sender.busy) return;
      sender.settled(percent);
      drawLevel(slider, output, percent);
    },
  };
}

// What's playing

async function loadMusic() {
  try {
    const response = answered(await fetchWithin(forChosen("/api/music")));
    if (!response.ok) return;
    music = await response.json();
    musicAt = Date.now();
  } catch (error) {
    return;
  }
  showMusic();
}

function showMusic() {
  showSpotify();
  document.getElementById("music").hidden = !music.available;
  const missing = document.getElementById("music-missing");
  missing.hidden = music.available;
  missing.textContent = music.why || NO_PLAYER;
  if (!music.available) return;
  const now = music.now;
  document.getElementById("track").textContent = now ? now.track : "Nothing playing";
  document.getElementById("artist").textContent = now ? now.artist : "Ask Nova, or pick something below.";
  document.getElementById("album").textContent = now ? now.album : "";

  // The cover's only fetched again once it's a different one.
  const cover = document.getElementById("cover");
  const wanted = now && now.cover ? forChosen(`/api/cover?for=${encodeURIComponent(now.cover)}`) : "";
  if (cover.dataset.for !== wanted) {
    cover.dataset.for = wanted;
    cover.hidden = true;
    if (wanted) cover.src = wanted;
    else cover.removeAttribute("src");
  }

  const playing = Boolean(now && now.playing);
  const button = document.getElementById("play-pause");
  button.setAttribute("aria-label", playing ? "Pause" : "Play");
  button.querySelector("use").setAttribute("href", playing ? "#i-pause" : "#i-play");
  document.getElementById("previous").disabled = !now;
  document.getElementById("next").disabled = !now;
  drawProgress();
  drawIfChanged("playlists", music.playlists, showPlaylists);
}

// How far through the song is - carried on from what Nova last said, while it plays.
function drawProgress() {
  const now = music && music.available && music.now;
  const shown = document.getElementById("progress");
  shown.hidden = !(now && now.duration);
  if (shown.hidden) return;
  const played = Math.min(now.duration, now.position + (now.playing ? (Date.now() - musicAt) / 1000 : 0));
  document.getElementById("progress-fill").style.width = `${(played / now.duration) * 100}%`;
  document.getElementById("elapsed").textContent = minutes(played);
  document.getElementById("length").textContent = minutes(now.duration);
}

function minutes(seconds) {
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// The liked songs first, as they're always there, then the playlists by name.
function showPlaylists(playlists) {
  const names = ["Liked songs", ...playlists.filter((name) => name.toLowerCase() !== "liked songs")];
  const shown = allPlaylists ? names : names.slice(0, MOST_PLAYLISTS);
  const chips = shown.map((name) => {
    const chip = element("button", name, "button quiet");
    chip.type = "button";
    chip.addEventListener("click", () => playMusic({ action: "playlist", playlist: name }, true));
    return chip;
  });
  if (shown.length < names.length) {
    const more = element("button", `${names.length - shown.length} more`, "button quiet");
    more.type = "button";
    more.addEventListener("click", () => {
      allPlaylists = true;
      showPlaylists(playlists);
    });
    chips.push(more);
  }
  document.getElementById("playlists").replaceChildren(...chips);
}

// Signing in to Spotify

// Whether Nova's signed in to Spotify - shown with every satellite away too, as the
// sign-in is the brain's, for them all.
function showSpotify() {
  showSignIn("spotify", "Spotify", music.spotify);
}

// A sign-in's panel - "spotify" or "google" - shown only when Nova can sign in there,
// with whether it is.
function showSignIn(service, name, state) {
  document.getElementById(service).hidden = !state;
  if (!state) return;
  document.getElementById(`${service}-said`).hidden = !state.signed_in;
  document.getElementById(`${service}-start`).textContent = state.signed_in ? "Sign in again" : `Sign in to ${name}`;
}

// Opens the service's page in a tab of its own. The tab's made in the click itself, as a
// phone's Safari blocks one opened once anything's been waited for, and sent there once
// Nova says where - or, if the browser refused it, the address is a link to tap.
async function startSignIn(service) {
  const tab = window.open("", "_blank");
  // So the service's page can't reach back and send this one somewhere else.
  if (tab) tab.opener = null;
  let result;
  try {
    result = await send(`/api/${service}/start`, {});
  } catch (error) {
    result = { success: false, message: "Couldn't reach Nova." };
  }
  if (!result.success || !String(result.url).startsWith("https://")) {
    if (tab) tab.close();
    toast(result.message || "That didn't work.", false);
    return;
  }
  // The tab could have been closed while Nova was asked.
  const opened = Boolean(tab && !tab.closed);
  document.getElementById(`${service}-link`).href = result.url;
  document.getElementById(`${service}-link-note`).hidden = opened;
  if (opened) tab.location = result.url;
  document.getElementById(`${service}-finish`).hidden = false;
}

// Finishes a sign-in with the address pasted, then shows the part of the page it's in.
async function finishSignIn(service, reload) {
  const address = document.getElementById(`${service}-address`);
  if (!address.value.trim()) return;
  await change(`/api/${service}/finish`, { address: address.value }, reload);
  // Used up whatever the answer, so another try starts afresh.
  address.value = "";
  document.getElementById(`${service}-finish`).hidden = true;
  document.getElementById(`${service}-link-note`).hidden = true;
}

// Calendars and lists

async function loadCalendars() {
  try {
    const response = answered(await fetchWithin("/api/calendars"));
    if (!response.ok) return;
    calendars = await response.json();
  } catch (error) {
    return;
  }
  showCalendars();
}

// Until Google's keys are set, the section says so, and offers no sign-in. Once signed
// in, it has each calendar and where new events go - or why it hasn't: Google out of
// reach, or no longer taking the sign-in, when the sign-in's button is offered again.
function showCalendars() {
  const missing = document.getElementById("calendars-missing");
  missing.hidden = calendars.available;
  missing.textContent = calendars.available ? "" : calendars.message;
  showSignIn("google", "Google", calendars.available ? calendars : null);

  const list = document.getElementById("calendars");
  const newEvents = document.getElementById("new-events");
  const signedIn = Boolean(calendars.available && calendars.signed_in);
  list.hidden = !calendars.available || (!signedIn && !calendars.message);
  newEvents.hidden = !signedIn || Boolean(calendars.message);
  if (list.hidden) return;
  if (calendars.message) {
    drawIfChanged("calendars", calendars.message, () => list.replaceChildren(emptyRow(calendars.message)));
    return;
  }
  drawIfChanged("calendars", calendars.calendars, () => {
    list.replaceChildren(...(calendars.calendars.length ? calendars.calendars.map(calendarRow) : [emptyRow("Google has no calendars for you.")]));
  });
  const writable = calendars.calendars.filter((each) => each.writable);
  newEvents.hidden = !writable.length;
  drawIfChanged("new-events", [writable, calendars.new_events], () => {
    const select = document.getElementById("new-events-calendar");
    select.replaceChildren(...writable.map((each) => {
      const option = element("option", each.name || "Your calendar");
      option.value = each.id;
      return option;
    }));
    // With no primary in Google's list, new events still go to Google's own primary.
    if (!calendars.new_events) {
      const primary = element("option", "Your main calendar");
      primary.value = "";
      primary.disabled = true;
      select.prepend(primary);
    }
    select.value = calendars.new_events || "";
  });
}

// Sends a change to a calendar's choices. As for the batteries, the rows are drawn again
// whatever the data says, as a switch which failed to save is still showing what wasn't saved.
function changeCalendar(path, body) {
  return change(path, body, () => {
    delete drawn.calendars;
    delete drawn["new-events"];
    return loadCalendars();
  });
}

// A calendar: its name, by Google's own, and a switch each for what's on and for being
// an activity. The ids are by its place in the list, as a calendar's own id can be an
// email address.
function calendarRow(calendar, index) {
  const row = element("li");
  const top = element("div", undefined, "top");
  const what = element("span", undefined, "what");
  what.append(element("span", calendar.name || "No name", "name"));
  if (calendar.primary) what.append(element("span", "Your own", "sub"));
  else if (!calendar.writable) what.append(element("span", "Nova can read it, but not add to it", "sub"));
  if (calendar.activity === null) what.append(element("span", "Can't be an activity - it has no name, or one listed before it has the same name", "sub"));
  top.append(icon("i-calendar"), what);
  row.append(top);

  const choose = (label, key, checked) => {
    const id = `calendar-${key}-${index}`;
    const field = element("div", undefined, "choice");
    const name = element("label", label);
    name.htmlFor = id;
    const box = element("input");
    // Null for an activity it can't be, which is off, and can't be switched on.
    Object.assign(box, { type: "checkbox", id, checked: checked === true, disabled: checked === null });
    box.setAttribute("role", "switch");
    box.addEventListener("change", () => changeCalendar("/api/calendars", { id: calendar.id, [key]: box.checked }));
    field.append(name, box);
    return field;
  };
  row.append(choose("In what's on", "whats_on", calendar.whats_on));
  row.append(choose("An activity", "activity", calendar.activity));
  return row;
}

// Asks the page's other choices of Home Assistant: the main list, the weather and the PCs.
async function loadHomeChoices() {
  try {
    const response = answered(await fetchWithin("/api/home-choices"));
    if (!response.ok) return;
    homeChoices = await response.json();
  } catch (error) {
    return;
  }
  showHomeChoices();
}

// The lists and how each device is sent to, as rows; the weather and the PCs as choices,
// only when Nova's told there is one. Without Home Assistant, only why not.
function showHomeChoices() {
  const missing = document.getElementById("home-choices-missing");
  missing.hidden = homeChoices.available;
  missing.textContent = homeChoices.available ? "" : homeChoices.message;
  const lists = document.getElementById("lists");
  const sending = document.getElementById("sending");
  document.getElementById("lists-title").hidden = lists.hidden = !homeChoices.available;
  document.getElementById("sending-title").hidden = sending.hidden = !homeChoices.available || !homeChoices.sending.length;
  document.getElementById("weather-choice").hidden = !homeChoices.available || !homeChoices.weather;
  document.getElementById("pcs-choice").hidden = !homeChoices.available || !homeChoices.pcs;
  if (!homeChoices.available) return;

  drawIfChanged("lists", homeChoices.lists, () => {
    lists.replaceChildren(...(homeChoices.lists.length ? homeChoices.lists.map(listRow) : [emptyRow("Home Assistant has no to-do lists.")]));
  });
  drawIfChanged("sending", homeChoices.sending, () => {
    sending.replaceChildren(...homeChoices.sending.map(sendingRow));
  });
  drawIfChanged("weather", homeChoices.weather, () => {
    if (!homeChoices.weather) return;
    const select = document.getElementById("weather-entity");
    select.replaceChildren(...homeChoices.weather.map((each) => {
      const option = element("option", each.name);
      option.value = each.id;
      return option;
    }));
    const chosen = homeChoices.weather.find((each) => each.chosen);
    select.value = chosen ? chosen.id : "";
  });
  drawIfChanged("pcs", homeChoices.pcs, () => {
    if (!homeChoices.pcs) return;
    document.getElementById("pcs-choice").replaceChildren(...homeChoices.pcs.pcs.map(pcWakeChoice));
  });
}

// Sends a choice. As for the calendars, what's shown is drawn again whatever the data says,
// as a choice which failed to save is still showing what wasn't saved.
function changeHomeChoice(path, body) {
  return change(path, body, () => {
    for (const part of ["lists", "weather", "pcs"]) delete drawn[part];
    return loadHomeChoices();
  });
}

// A to-do list: its name, how many things are still to do on it, its other names, and
// either a mark that it's the main one or a way to make it so.
function listRow(found) {
  const row = element("li");
  const top = element("div", undefined, "top");
  const what = element("span", undefined, "what");
  what.append(element("span", found.name, "name"));
  const parts = [];
  if (found.count !== null) parts.push(`${found.count} ${found.count === 1 ? "thing" : "things"} to do`);
  if (found.aliases.length) parts.push(`Also: ${found.aliases.join(", ")}`);
  if (parts.length) what.append(element("span", parts.join(" - "), "sub"));
  top.append(icon("i-routine"), what);
  row.append(top);
  if (found.main) {
    what.append(element("span", "Main list", "sub"));
  } else {
    const make = element("button", "Make main", "button quiet");
    make.type = "button";
    // Said by a screen reader, which otherwise hears only "Make main" for every list.
    make.setAttribute("aria-label", `Make ${found.name} the main list`);
    make.addEventListener("click", () => changeHomeChoice("/api/lists/main", { id: found.id }));
    row.append(make);
  }
  return row;
}

// A device Nova can send to, and how: nothing to choose.
function sendingRow(device) {
  const row = element("li");
  const top = element("div", undefined, "top");
  const what = element("span", undefined, "what");
  what.append(element("span", device.name, "name"), element("span", device.how, "sub"));
  top.append(icon("i-bell"), what);
  row.append(top);
  return row;
}

// Which Wake-on-LAN button wakes one PC, from all Home Assistant has.
function pcWakeChoice(pc, index) {
  const field = element("div", undefined, "field");
  const id = `pc-wake-${index}`;
  const label = element("label", `Wake ${pc.name} with`);
  label.htmlFor = id;
  const wrap = element("span", undefined, "select");
  const select = element("select");
  select.id = id;
  if (!pc.wake) {
    const none = element("option", "Choose a button");
    none.value = "";
    none.disabled = true;
    select.append(none);
  }
  for (const button of homeChoices.pcs.buttons) {
    const option = element("option", button.name);
    option.value = button.id;
    select.append(option);
  }
  select.value = pc.wake || "";
  select.addEventListener("change", () => changeHomeChoice("/api/pcs/wake", { device: pc.device, button: select.value }));
  wrap.append(select, icon("i-chevron"));
  field.append(label, wrap);
  return field;
}

// Asks the music to do something, then shows what it's doing now - saying how it went
// only when the music itself doesn't, as it does for a pause or a skip.
async function playMusic(body, say) {
  let result;
  try {
    result = await send("/api/music", body);
  } catch (error) {
    result = { success: false, message: "Couldn't reach Nova." };
  }
  if (say || !result.success) toast(result.message || (result.success ? "Done." : "That didn't work."), result.success);
  await loadMusic();
  // Spotify takes a moment to catch up with itself.
  setTimeout(loadMusic, 1200);
  return result;
}

// Home Assistant

async function loadHome() {
  try {
    const response = answered(await fetchWithin(forChosen("/api/home")));
    if (!response.ok) return;
    home = await response.json();
  } catch (error) {
    return;
  }
  showHome();
}

function showHome() {
  const routines = document.getElementById("home-routines");
  // Without Home Assistant, the lights say how to connect it, and there are no scripts or scenes.
  if (!home.available || !home.success) {
    const why = home.available ? home.message
      : "Nova isn't connected to Home Assistant. Add its address in Settings, and its token in Keys.";
    drawIfChanged("lights", why, () => {
      for (const id of Object.keys(lights)) delete lights[id];
      document.getElementById("lights").replaceChildren(emptyRow(why));
    });
    routines.hidden = true;
    return;
  }
  drawIfChanged("lights", home.lights.map((light) => [light.id, light.area]), () => {
    for (const id of Object.keys(lights)) delete lights[id];
    // The lights come in order of room, Nova's own first, so each room's heading goes
    // in where its first light does.
    const rows = [];
    let area;
    for (const light of home.lights) {
      if (light.area !== area || !rows.length) {
        area = light.area;
        rows.push(element("li", area || "No room", "area"));
      }
      rows.push((lights[light.id] = lightRow(light)).row);
    }
    document.getElementById("lights").replaceChildren(...(rows.length ? rows : [emptyRow("Home Assistant has no lights.")]));
  });
  for (const light of home.lights) lights[light.id].show(light);
  routines.hidden = false;
  drawIfChanged("routines", home.routines, showRoutines);
}

// The batteries

async function loadBatteries() {
  try {
    const response = answered(await fetchWithin("/api/batteries"));
    if (!response.ok) return;
    batteries = await response.json();
  } catch (error) {
    return;
  }
  showBatteries(batteries);
}

// Sends a change to a battery's warnings. The rows are drawn again whatever the data
// says, as a switch which failed to save is still showing what wasn't saved.
function changeBattery(path, body) {
  return change(path, body, () => {
    delete drawn.batteries;
    return loadBatteries();
  });
}

function showBatteries(state) {
  const list = document.getElementById("batteries");
  const also = document.getElementById("also-send");
  if (!state.available) {
    also.hidden = true;
    drawIfChanged("batteries", state.message, () => list.replaceChildren(emptyRow(state.message)));
    return;
  }
  also.hidden = false;
  // Set at every load. A switch someone has just tapped is already as they want it, and a
  // load that lands during its own save sets it back for the round trip, then corrects it.
  document.getElementById("also-send-box").checked = state.also_send_when_said;
  drawIfChanged("batteries", state.devices, () => {
    // Not while a number is being typed, which a redraw would wipe - tried again next
    // time. A tapped switch or button keeps the focus in Chromium, so only a box counts.
    const typing = document.activeElement;
    if (typing && typing.type === "number" && list.contains(typing)) return false;
    // Nor while a switch or button is held down: the row drawn again under the pointer
    // loses the click, if it's slow enough to land just after a mark box was edited.
    if (list.querySelector(":active")) return false;
    list.replaceChildren(...(state.devices.length ? state.devices.map(batteryRow) : [emptyRow("Home Assistant has no batteries.")]));
  });
}

// A battery: how it is, a switch for its warnings and one for sending them to the phone,
// a box for each mark, and a way back to the defaults once anything's been changed.
function batteryRow(device) {
  const row = element("li");
  const top = element("div", undefined, "top");
  const what = element("span", undefined, "what");
  what.append(element("span", device.name, "name"));
  const charging = device.charging === null ? "" : device.charging ? ", charging" : ", not charging";
  const decides = device.person ? `, ${device.person} decides if you're home` : "";
  what.append(element("span", `${device.level}%${charging}${device.stale ? `, as of ${device.stale} ago` : ""}${decides}`, "sub"));
  top.append(icon("i-battery"), what);
  row.append(top);

  const choose = (label, id, checked, key) => {
    const field = element("div", undefined, "choice");
    const name = element("label", label);
    name.htmlFor = id;
    const box = element("input");
    Object.assign(box, { type: "checkbox", id, checked });
    box.setAttribute("role", "switch");
    box.addEventListener("change", () => changeBattery("/api/batteries", { id: device.id, [key]: box.checked }));
    field.append(name, box);
    return field;
  };
  row.append(choose("Warn me", `battery-on-${device.id}`, device.enabled, "enabled"));
  row.append(choose("Send to my phone", `battery-phone-${device.id}`, device.send_to_phone, "send_to_phone"));

  const marks = element("div", undefined, "marks");
  marks.append(element("span", device.marks.length === 1 ? "Warn at" : "Warn at both"));
  const boxes = device.marks.map((mark, i) => {
    const box = element("input");
    Object.assign(box, { type: "number", min: 1, max: 99, step: 1, value: mark });
    box.inputMode = "numeric";
    box.setAttribute("aria-label", `${device.name} warning mark ${i + 1}, in percent`);
    box.addEventListener("change", () => {
      // Done typing, so the rows may be drawn again with what comes back.
      box.blur();
      changeBattery("/api/batteries", { id: device.id, marks: boxes.map((each) => Number(each.value)) });
    });
    marks.append(box, element("span", "%"));
    return box;
  });
  row.append(marks);

  if (device.changed) {
    const back = element("button", "Back to the defaults", "button quiet");
    back.type = "button";
    back.addEventListener("click", () => changeBattery("/api/batteries", { id: device.id, reset: true }));
    row.append(back);
  }
  return row;
}

// How a colour looks at full brightness, for a light's icon and swatches.
function colourOf(hue, saturation) {
  return `hsl(${hue}, 100%, ${100 - saturation / 2}%)`;
}

// How a white looks, from amber at the warmest the light makes to cool white at the coolest.
function whiteOf(kelvin, light) {
  const span = light.max_kelvin - light.min_kelvin;
  const cool = span > 0 ? Math.min(1, Math.max(0, (kelvin - light.min_kelvin) / span)) : 0.5;
  return `rgb(${WARM_WHITE.map((part, i) => Math.round(part + (COOL_WHITE[i] - part) * cool)).join(", ")})`;
}

// A light: its switch, and while it's on, how bright it is - dragged as a volume is -
// and, if it can, its colour as swatches and a hue slider, or how warm its white is.
// The icon takes the colour it's set to.
function lightRow(light) {
  const id = `light-${light.id}`;
  const row = element("li");
  const name = element("label", light.name, "what");
  name.htmlFor = id;
  const toggle = element("input");
  Object.assign(toggle, { type: "checkbox", id });
  toggle.setAttribute("role", "switch");
  const unreachable = element("span", "Unavailable", "state");
  const level = element("div", undefined, "level");
  const slider = element("input");
  Object.assign(slider, { type: "range", min: 1, max: 100, step: 1 });
  slider.setAttribute("aria-label", `How bright ${light.name} is`);
  const output = element("output");
  level.append(slider, output);
  const tones = element("div", undefined, "tones");
  row.append(icon("i-bulb"), name, toggle, unreachable, level, tones);

  let switching = false;
  let current = light;
  const check = (result) => {
    if (!result.success) toast(result.message || "Couldn't change the light.", false);
  };
  const tell = (body) => send("/api/light", { id: light.id, ...body }).then(check)
    .catch(() => toast("Couldn't reach Nova.", false));

  // A slider whose newest value goes out steadily as it's dragged, as the brightness
  // does. moved draws what's being dragged to, and while it is, or a value is on its
  // way, what Home Assistant says is left alone.
  function dragged(control, body, moved) {
    const made = { sender: steadily(LIGHT_EVERY, (value) => tell(body(value))), held: false };
    control.addEventListener("input", () => {
      made.held = true;
      moved(Number(control.value));
      made.sender.send(Number(control.value));
    });
    control.addEventListener("change", () => {
      made.held = false;
      made.sender.send(Number(control.value));
      setTimeout(loadHome, 900);
    });
    return made;
  }
  const idle = (made) => !made || !(made.held || made.sender.busy);

  const brightness = dragged(slider, (percent) => ({ brightness: percent }), (percent) => drawLevel(slider, output, percent));

  let hue = null;
  let hueSlider = null;
  if (light.colour_modes.includes("hs")) {
    const swatches = element("div", undefined, "swatches");
    for (const swatch of SWATCHES) {
      const button = element("button", undefined, "swatch");
      button.type = "button";
      button.title = swatch.name;
      button.setAttribute("aria-label", `Make ${light.name} ${swatch.name.toLowerCase()}`);
      button.style.setProperty("background", colourOf(swatch.hue, swatch.saturation));
      button.addEventListener("click", () => {
        row.style.setProperty("--tint", colourOf(swatch.hue, swatch.saturation));
        tell({ hue: swatch.hue, saturation: swatch.saturation });
        setTimeout(loadHome, 900);
      });
      swatches.append(button);
    }
    hueSlider = element("input", undefined, "hue");
    Object.assign(hueSlider, { type: "range", min: 0, max: 360, step: 1 });
    hueSlider.setAttribute("aria-label", `The colour of ${light.name}`);
    // The slider keeps whatever saturation the light has, or is fully saturated if it's set to a white.
    const saturation = () => current.saturation ?? 100;
    hue = dragged(hueSlider, (degrees) => ({ hue: degrees, saturation: saturation() }),
      (degrees) => row.style.setProperty("--tint", colourOf(degrees, saturation())));
    tones.append(swatches, hueSlider);
  }

  let warmth = null;
  let warmthSlider = null;
  if (light.colour_modes.includes("color_temp") && light.min_kelvin && light.max_kelvin) {
    warmthSlider = element("input", undefined, "warmth");
    Object.assign(warmthSlider, { type: "range", min: light.min_kelvin, max: light.max_kelvin, step: 50 });
    warmthSlider.setAttribute("aria-label", `How warm the white of ${light.name} is`);
    warmth = dragged(warmthSlider, (kelvin) => ({ kelvin }),
      (kelvin) => row.style.setProperty("--tint", whiteOf(kelvin, light)));
    tones.append(warmthSlider);
  }

  toggle.addEventListener("change", async () => {
    switching = true;
    row.classList.toggle("on", toggle.checked);
    let result;
    try {
      result = await send("/api/light", { id: light.id, on: toggle.checked });
    } catch (error) {
      result = { success: false, message: "Couldn't reach Nova." };
    }
    switching = false;
    if (!result.success) {
      toast(result.message || "Couldn't switch the light.", false);
      toggle.checked = current.on;
    }
    // Home Assistant takes a moment to say it's changed.
    setTimeout(loadHome, 700);
  });

  return {
    row,
    // Shows the light as Home Assistant has it - leaving alone whatever's being changed.
    show(now) {
      current = now;
      unreachable.hidden = now.reachable;
      toggle.hidden = !now.reachable;
      row.classList.toggle("unreachable", !now.reachable);
      if (!switching) {
        toggle.checked = now.on;
        row.classList.toggle("on", now.on);
      }
      level.hidden = !(now.on && now.dimmable && now.reachable);
      tones.hidden = !(now.on && now.reachable && tones.childElementCount > 0);
      if (now.brightness !== null && idle(brightness)) {
        brightness.sender.settled(now.brightness);
        drawLevel(slider, output, now.brightness);
      }
      if (!idle(hue) || !idle(warmth)) return;
      if (now.hue !== null) {
        row.style.setProperty("--tint", colourOf(now.hue, now.saturation));
        if (hueSlider) {
          hue.sender.settled(now.hue);
          hueSlider.value = now.hue;
        }
      } else if (now.kelvin !== null) {
        row.style.setProperty("--tint", whiteOf(now.kelvin, now));
        if (warmthSlider) {
          warmth.sender.settled(now.kelvin);
          warmthSlider.value = now.kelvin;
        }
      } else {
        row.style.removeProperty("--tint");
      }
    },
  };
}

// The user's own routines

const REPEATS = { daily: "Every day", weekdays: "Weekdays", weekends: "Weekends" };

// The routine being changed in the editor, or null for a new one.
let editing = null;

function showOwnRoutines(routines) {
  const rows = routines.map((routine) => {
    const row = element("li", undefined, "routine");
    const what = element("span", undefined, "what");
    what.append(element("span", routine.name, "routine-name"), element("span", startsWhen(routine), "detail"));
    const run = element("button", "Run", "button quiet");
    run.type = "button";
    run.setAttribute("aria-label", `Run ${routine.name}`);
    run.addEventListener("click", async () => {
      run.disabled = true;
      await change("/api/routines/run", { id: routine.id });
      run.disabled = false;
    });
    const edit = element("button", undefined, "icon-button");
    edit.type = "button";
    edit.title = `Change ${routine.name}`;
    edit.setAttribute("aria-label", edit.title);
    edit.append(icon("i-edit"));
    edit.addEventListener("click", () => openEditor(routine));
    row.append(icon("i-routine"), what, run, edit);
    return row;
  });
  document.getElementById("own-routines").replaceChildren(...(rows.length ? rows : [
    emptyRow("No routines yet. One does a few things at once - when you say its words, or at a time you choose.")]));
}

// How a routine starts, in words: "Say “good morning”, or weekdays at 07:00".
function startsWhen(routine) {
  const said = routine.phrases.map((phrase) => `“${phrase}”`).join(" or ");
  const timed = routine.at ? `${REPEATS[routine.repeat]} at ${routine.at}` : "";
  if (said && timed) return `Say ${said}, or ${timed.charAt(0).toLowerCase()}${timed.slice(1)}`;
  if (said) return `Say ${said}`;
  return timed || "Run from here";
}

function openEditor(routine) {
  editing = routine || null;
  document.getElementById("editor-title").textContent = routine ? `Change ${routine.name}` : "New routine";
  document.getElementById("routine-name").value = routine ? routine.name : "";
  document.getElementById("routine-phrases").value = routine ? routine.phrases.join(", ") : "";
  document.getElementById("routine-at").value = routine ? routine.at : "";
  document.getElementById("routine-repeat").value = routine ? routine.repeat : "daily";
  document.getElementById("routine-steps").value = routine ? routine.steps.join("\n") : "";
  showRunsOn(routine ? routine.satellite || "" : "");
  document.getElementById("delete-routine").hidden = !routine;
  document.getElementById("new-routine").hidden = true;
  document.getElementById("routine-editor").hidden = false;
  document.getElementById("routine-name").focus();
}

// The editor's "Runs on", from the satellites the brain lets in - the default first, as
// "", which is what a routine keeps while it has no satellite of its own. Saving sends
// the choice, so a routine saved on the page keeps the satellite it had.
function showRunsOn(chosen) {
  const field = document.getElementById("routine-satellite-field");
  field.hidden = satellites.length < 2;
  const fallback = satellites.find((satellite) => satellite.default);
  const options = [new Option(fallback ? `The default (${fallback.name})` : "The default", ""),
                   ...satellites.map((satellite) => new Option(satellite.name, satellite.name))];
  document.getElementById("routine-satellite").replaceChildren(...options);
  // A satellite since removed is no longer offered, so it starts at the default.
  document.getElementById("routine-satellite").value = satellites.some((satellite) => satellite.name === chosen) ? chosen : "";
}

function closeEditor() {
  editing = null;
  document.getElementById("routine-editor").hidden = true;
  document.getElementById("new-routine").hidden = false;
}

// Home Assistant's scripts, to start, and scenes, to set.
function showRoutines(routines) {
  const rows = routines.map((routine) => {
    const scene = routine.kind === "scene";
    const row = element("li", undefined, "routine");
    const start = element("button", scene ? "Set" : "Start", "button quiet");
    start.type = "button";
    start.setAttribute("aria-label", `${scene ? "Set" : "Start"} ${routine.name}`);
    start.addEventListener("click", async () => {
      // A script can do a lot at once, so it's asked about first - and only a yes sends
      // confirmed, without which the brain won't run it. A scene is one tap.
      if (!scene && !confirm(`Run ${routine.name}?`)) return;
      start.disabled = true;
      await change("/api/run", scene ? { id: routine.id } : { id: routine.id, confirmed: true });
      start.disabled = false;
      // A scene changes the lights, and Home Assistant takes a moment to say so.
      setTimeout(loadHome, 900);
    });
    row.append(icon(scene ? "i-scene" : "i-routine"), element("span", routine.name, "what"));
    // Exposed to Assist, as the registry says at this draw, so Nova runs it when asked aloud.
    if (!scene && routine.by_voice) row.append(element("span", "By voice", "when"));
    row.append(start);
    return row;
  });
  document.getElementById("ha-routines").replaceChildren(
    ...(rows.length ? rows : [emptyRow("No scripts or scenes in Home Assistant yet.")]));
}

function showClock(clock) {
  const items = [...clock.timers.map((item) => ({ ...item, kind: "timer" })),
                 ...clock.alarms.map((item) => ({ ...item, kind: "alarm" }))].sort((a, b) => a.due_at - b.due_at);
  counting = [];
  const rows = items.map((item) => {
    const row = element("li");
    const when = element("span", undefined, "when");
    if (item.kind === "timer") {
      when.classList.add("counting");
      when.textContent = countdown(item.due_at);
      counting.push([when, item.due_at]);
    } else {
      // Its time's in its name already.
      when.textContent = dayOf(new Date(item.due_at * 1000));
    }
    const remove = removeButton(`${item.kind === "timer" ? "Cancel" : "Remove"} the ${item.name}`,
                                () => change("/api/remove", { kind: item.kind, id: item.id }));
    row.append(icon(item.kind === "timer" ? "i-timer" : "i-alarm"), element("span", capitalised(item.name), "what"),
               when, remove);
    return row;
  });
  document.getElementById("clock").replaceChildren(
    ...(rows.length ? rows : [emptyRow("No timers or alarms set. Ask Nova for one, or add an alarm here.")]));
}

function showMemories(memories) {
  const rows = memories.map((fact) => {
    const row = element("li");
    row.append(element("span", fact, "what"), removeButton(`Forget “${fact}”`, () => change("/api/forget", { fact })));
    return row;
  });
  document.getElementById("memories").replaceChildren(
    ...(rows.length ? rows : [emptyRow("Nothing yet. Tell Nova something about yourself and it'll remember.")]));
}

function showKeys(keys) {
  const shown = document.getElementById("keys");
  // Not while a key's being put in.
  if (shown.querySelector(".key.open")) return false;
  shown.replaceChildren(...keys.map(keyTile));
}

// A key, and whether it's set. The field for a new one only opens when it's asked
// for, as keys are seldom changed - so they take little room the rest of the time.
function keyTile(key) {
  const id = `key-${key.name}`;
  const form = element("form", undefined, "tile key");
  const label = element("label", key.label);
  label.htmlFor = id;
  const state = element("span", undefined, "state");
  state.append(dot(key.set ? "good" : "off"), key.set ? "Set" : "Not set");
  const opener = element("button", undefined, "button quiet");
  opener.type = "button";
  // Named for which key it's for, as "Add" alone wouldn't say.
  const showOpen = (open) => {
    opener.textContent = open ? "Cancel" : key.set ? "Replace" : "Add";
    opener.setAttribute("aria-label", open ? `Cancel changing the ${key.label} key` : `${opener.textContent} the ${key.label} key`);
  };
  showOpen(false);
  const input = element("input");
  Object.assign(input, { type: "password", id, name: key.name, autocomplete: "off", spellcheck: false,
                         placeholder: "Paste it here" });
  const save = element("button", "Save", "button");
  save.type = "submit";
  save.disabled = true;
  const entry = element("div", undefined, "entry");
  entry.append(input, save);

  function close() {
    form.classList.remove("open");
    input.value = "";
    save.disabled = true;
    showOpen(false);
  }
  opener.addEventListener("click", () => {
    if (form.classList.contains("open")) {
      close();
      return;
    }
    form.classList.add("open");
    showOpen(true);
    input.focus();
  });
  input.addEventListener("input", () => (save.disabled = !input.value.trim()));
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    close();
    opener.focus();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = input.value;
    if (!value.trim()) return;
    close();
    change("/api/keys", { name: key.name, value });
  });
  form.append(label, state, opener, entry);
  return form;
}

// The page's own password, for its NPM address - drawn from /api/login-state, which
// says only whether it's set.
function showPagePassword(state) {
  const shown = document.getElementById("page-password");
  // Not while it's being put in.
  if (shown.querySelector(".key.open")) return false;
  shown.replaceChildren(pagePasswordTile(state));
}

// As a key's tile, but through NPM replacing it needs the current one too - so a login
// left open on a device can't lock you out - and a password manager may offer to keep it.
function pagePasswordTile(state) {
  const form = element("form", undefined, "tile key");
  const label = element("label", "Page password (for NPM)");
  label.htmlFor = "page-password-new";
  const shownState = element("span", undefined, "state");
  shownState.append(dot(state.set ? "good" : "off"), state.set ? "Set" : "Not set");
  const opener = element("button", undefined, "button quiet");
  opener.type = "button";
  const showOpen = (open) => {
    opener.textContent = open ? "Cancel" : state.set ? "Replace" : "Set";
    opener.setAttribute("aria-label", open ? "Cancel changing the page password" : `${opener.textContent} the page password`);
  };
  showOpen(false);
  const field = (id, autocomplete, placeholder) => {
    const input = element("input");
    Object.assign(input, { type: "password", id, autocomplete, placeholder });
    return input;
  };
  const current = state.route === "npm" && state.set
    ? field("page-password-current", "current-password", "The current one") : null;
  if (current) current.setAttribute("aria-label", "The current page password");
  const input = field("page-password-new", "new-password", "At least 12 characters");
  const save = element("button", "Save", "button");
  save.type = "submit";
  save.disabled = true;
  const entries = [];
  if (current) {
    const entry = element("div", undefined, "entry");
    entry.append(current);
    entries.push(entry);
  }
  const entry = element("div", undefined, "entry");
  entry.append(input, save);
  entries.push(entry);

  const fields = current ? [current, input] : [input];
  function close() {
    form.classList.remove("open");
    for (const each of fields) each.value = "";
    save.disabled = true;
    showOpen(false);
  }
  opener.addEventListener("click", () => {
    if (form.classList.contains("open")) {
      close();
      return;
    }
    form.classList.add("open");
    showOpen(true);
    fields[0].focus();
  });
  for (const each of fields) {
    each.addEventListener("input", () => (save.disabled = fields.some((one) => !one.value)));
    each.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      close();
      opener.focus();
    });
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (fields.some((one) => !one.value)) return;
    // The body is made as change is called, so the fields can be emptied straight after.
    change("/api/page-password", current ? { password: input.value, current: current.value } : { password: input.value },
           checkLogin);
    close();
  });
  form.append(label, shownState, opener, ...entries);
  return form;
}

function showSettings(settings) {
  const form = document.getElementById("settings");
  // Not while they're being changed.
  if (form.querySelector(".changed, input:focus, select:focus")) return false;
  const groups = new Map([...GROUPS.map(([name]) => [name, []]), ["Other", []]]);
  for (const setting of settings) {
    const group = GROUPS.find(([, starts]) => starts.some((start) => setting.key.startsWith(start)));
    groups.get(group ? group[0] : "Other").push(setting);
  }
  const rows = [];
  for (const [name, members] of groups) {
    if (!members.length) continue;
    const row = element("div", undefined, "group");
    const title = element("h3", name);
    title.id = `group-${name.toLowerCase()}`;
    row.setAttribute("role", "group");
    row.setAttribute("aria-labelledby", title.id);
    const fields = element("div", undefined, "fields");
    fields.append(...members.map(settingField));
    row.append(title, fields);
    rows.push(row);
  }
  document.getElementById("setting-groups").replaceChildren(...rows);
  countChanges();
  showNight();
}

// A setting, with when it's used under its label: "Used straight away".
function settingField(setting) {
  const id = `setting-${setting.key}`;
  const field = element("div", undefined, setting.kind === "yes/no" ? "field switch" : "field");
  const about = element("span", undefined, "about");
  const label = element("label", setting.label);
  label.htmlFor = id;
  const used = element("span", setting.used, "detail");
  used.id = `${id}-used`;
  about.append(label, used);
  const input = setting.choices ? choiceInput(setting) : valueInput(setting);
  input.id = id;
  input.name = setting.key;
  // The slider moves a speed between its steps to the nearest - 1.12 typed into the
  // file becoming 1.1 - so it's what's saved that it's compared with, and the moved one
  // shows as a change rather than as what the file says.
  input.dataset.saved = input.type === "range" ? String(setting.value) : valueOf(input);
  input.setAttribute("aria-describedby", used.id);
  field.append(about, setting.choices ? selectBox(input) : input.type === "range" ? speedControl(input) : input);
  // Under the second of the night's two, so it's said once.
  if (setting.key === NIGHT[1]) {
    const none = element("span", "No quiet hours", "detail night");
    none.id = `${id}-night`;
    none.hidden = true;
    field.append(none);
  }
  return field;
}

// One of a few words, picked from a list.
function choiceInput(setting) {
  const select = element("select");
  for (const choice of setting.choices) {
    const option = element("option", capitalised(choice));
    option.value = choice;
    select.append(option);
  }
  select.value = setting.value;
  return select;
}

function valueInput(setting) {
  const input = element("input");
  if (setting.kind === "yes/no") {
    input.type = "checkbox";
    input.setAttribute("role", "switch");
    input.checked = Boolean(setting.value);
    return input;
  }
  if (setting.form === "number") {
    // Its range before its value, which would otherwise be rounded to the browser's
    // own whole steps from 0 to 100 - 1.25 becoming 1.
    Object.assign(input, { type: setting.key === "voice.speed" ? "range" : "number", min: setting.min,
                           max: setting.max, step: setting.whole ? 1 : 0.05 });
    if (input.type === "number") input.inputMode = setting.whole ? "numeric" : "decimal";
  } else if (setting.form === "time") {
    input.type = "time";
  } else if (setting.form === "address") {
    input.type = "url";
    input.inputMode = "url";
  } else {
    input.type = setting.kind === "number" ? "number" : "text";
    if (setting.kind === "number") {
      input.step = "any";
      input.inputMode = "decimal";
    }
  }
  input.value = Array.isArray(setting.value) ? setting.value.join(", ") : setting.value ?? "";
  input.autocomplete = "off";
  input.spellcheck = false;
  return input;
}

// The speaking speed's slider, with how fast it is beside it, and a button to hear it
// said at that speed before it's saved.
function speedControl(slider) {
  const made = element("div", undefined, "speed");
  const output = element("output", undefined, "value");
  output.setAttribute("for", slider.id);
  const show = () => {
    // "1.0 times" rather than "1 times", and "1.25 times" as it is.
    const speed = Number(slider.value);
    output.textContent = `${Number.isInteger(speed) ? speed.toFixed(1) : speed} times`;
    slider.style.setProperty("--level", `${((speed - slider.min) / (slider.max - slider.min)) * 100}%`);
  };
  show();
  slider.addEventListener("input", show);
  const hear = element("button", "Hear it", "button quiet");
  hear.type = "button";
  hear.setAttribute("aria-label", "Hear it at this speed");
  hear.addEventListener("click", async () => {
    // Only one at a time - Nova says it's busy to another while one's being said.
    hear.disabled = true;
    let result;
    try {
      result = await send("/api/voice/hear", { speed: Number(slider.value) });
    } catch (error) {
      result = { success: false, message: "Couldn't reach Nova." };
    }
    hear.disabled = false;
    toast(result.message || (result.success ? "Done." : "That didn't work."), result.success);
  });
  made.append(slider, output, hear);
  return made;
}

// Says under the night's second setting when there are no quiet hours, as the two are
// the same - and only then reads it out with it, as a description is read even hidden.
// The first has no note of its own, so keeps just when it's used.
function showNight() {
  const [from, until] = NIGHT.map((key) => document.getElementById(`setting-${key}`));
  if (!from || !until) return;
  const none = from.value === until.value;
  document.getElementById(`${until.id}-night`).hidden = !none;
  until.setAttribute("aria-describedby", none ? `${until.id}-used ${until.id}-night` : `${until.id}-used`);
}

// A select with the page's own arrow, the browser's being hidden so it matches the fields.
function selectBox(select) {
  const box = element("span", undefined, "select");
  box.append(select, icon("i-chevron"));
  return box;
}

function valueOf(input) {
  return input.type === "checkbox" ? String(input.checked) : input.value;
}

// Marks the settings changed since they were saved, says how many there are, and
// gives them back.
function countChanges() {
  const changed = [...document.querySelectorAll("#settings input, #settings select")].filter((input) => {
    const differs = valueOf(input) !== input.dataset.saved;
    input.closest(".field").classList.toggle("changed", differs);
    return differs;
  });
  // Anything more to save goes before the restart, which then uses it too - and once
  // it's changed back, the restart is offered again.
  const restarting = offerRestart && !changed.length;
  const unsaved = document.getElementById("unsaved");
  unsaved.classList.remove("bad");
  unsaved.textContent = changed.length ? `${counted(changed.length, "change", "changes")} not saved yet.`
    : restarting ? restartSaid : "Nothing's changed yet.";
  const save = document.getElementById("save-settings");
  save.disabled = !changed.length;
  save.hidden = restarting;
  document.getElementById("restart-now").hidden = !restarting;
  return changed;
}

function showSettingsProblem(problem) {
  const shown = document.getElementById("settings-problem");
  shown.hidden = !problem;
  shown.textContent = problem ? `Couldn't read your saved settings, so Nova's own are shown: ${problem}` : "";
}

// The night sky behind the page - the same stars every time, from the same seed,
// drawn again only when the window changes size.
function drawSky() {
  const canvas = document.getElementById("sky");
  const { width, height } = canvas.getBoundingClientRect();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width === Math.round(width * ratio) && canvas.height === Math.round(height * ratio)) return;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const sky = canvas.getContext("2d");
  sky.scale(ratio, ratio);

  const night = sky.createLinearGradient(0, 0, 0, height);
  night.addColorStop(0, "#0a1024");
  night.addColorStop(1, "#04060e");
  sky.fillStyle = night;
  sky.fillRect(0, 0, width, height);

  // A faint band of haze across it, like the Milky Way, with more stars in it.
  const band = { x: width * 0.62, y: height * 0.3, angle: -0.42, length: Math.hypot(width, height),
                 across: Math.min(width, height) * 0.22 };
  sky.save();
  sky.translate(band.x, band.y);
  sky.rotate(band.angle);
  sky.scale(1, (band.across * 2) / band.length);
  const haze = sky.createRadialGradient(0, 0, 0, 0, 0, band.length / 2);
  haze.addColorStop(0, "rgba(120, 140, 210, 0.1)");
  haze.addColorStop(0.5, "rgba(90, 100, 180, 0.05)");
  haze.addColorStop(1, "rgba(90, 100, 180, 0)");
  sky.fillStyle = haze;
  sky.fillRect(-band.length / 2, -band.length / 2, band.length, band.length);
  sky.restore();

  // And a little warmth low down, as if from a town far off.
  const warmth = sky.createRadialGradient(width * 0.1, height, 0, width * 0.1, height, Math.max(width, height) * 0.5);
  warmth.addColorStop(0, "rgba(130, 90, 140, 0.08)");
  warmth.addColorStop(1, "rgba(130, 90, 140, 0)");
  sky.fillStyle = warmth;
  sky.fillRect(0, 0, width, height);

  const random = seeded(2026);
  const stars = Math.round((width * height) / 2400);
  for (let i = 0; i < stars; i++) {
    let x = random() * width;
    let y = random() * height;
    if (i % 3 === 0) {
      // In the band: anywhere along it, and mostly near its middle across it.
      const along = (random() - 0.5) * band.length;
      const across = (random() + random() + random() - 1.5) * band.across * 0.9;
      x = band.x + along * Math.cos(band.angle) - across * Math.sin(band.angle);
      y = band.y + along * Math.sin(band.angle) + across * Math.cos(band.angle);
    }
    const size = 0.3 + Math.pow(random(), 4) * 1.4;
    const alpha = (0.2 + random() * 0.65).toFixed(2);
    const tint = random();
    sky.fillStyle = tint < 0.12 ? `rgba(255, 226, 186, ${alpha})`
      : tint > 0.88 ? `rgba(186, 206, 255, ${alpha})` : `rgba(236, 240, 255, ${alpha})`;
    sky.beginPath();
    sky.arc(x, y, size, 0, Math.PI * 2);
    sky.fill();
  }

  // A few bright ones, with a soft glow.
  for (let i = 0; i < Math.round(stars / 120); i++) {
    const x = random() * width;
    const y = random() * height;
    const glow = sky.createRadialGradient(x, y, 0, x, y, 5);
    glow.addColorStop(0, "rgba(255, 248, 235, 0.9)");
    glow.addColorStop(0.25, "rgba(243, 214, 160, 0.3)");
    glow.addColorStop(1, "rgba(243, 214, 160, 0)");
    sky.fillStyle = glow;
    sky.fillRect(x - 5, y - 5, 10, 10);
  }
}

// Random numbers, the same ones each time for the same seed - mulberry32.
function seeded(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Keeps the toasts, and a field tabbed to, clear of the save bar, as tall as it's drawn -
// a long save error, or an iPhone's home bar under it, makes it taller. Its height goes to
// --save-bar through the CSSOM, which the page's CSP lets through where a style attribute
// wouldn't be. The toasts are lifted only while the bar's on screen, and its corners are
// rounded only where it ends the form, not while it's stuck to the screen's bottom.
function followSaveBar() {
  const bar = document.querySelector(".save-bar");
  const page = document.documentElement;
  if ("ResizeObserver" in window) {
    new ResizeObserver(() => page.style.setProperty("--save-bar", `${bar.offsetHeight}px`)).observe(bar);
  }
  if (!("IntersectionObserver" in window)) {
    // Lifted all the time, as it can't be told when the bar's in view.
    page.classList.add("save-bar-shown");
    return;
  }
  // The screen less its bottom pixel, so a bar stuck to the bottom is never wholly in it,
  // while one ending the form, above the bottom, is.
  new IntersectionObserver((entries) => {
    // The newest, should several have queued up.
    const seen = entries[entries.length - 1];
    page.classList.toggle("save-bar-shown", seen.isIntersecting);
    bar.classList.toggle("stuck", seen.isIntersecting && seen.intersectionRatio < 1);
  }, { rootMargin: "0px 0px -1px 0px", threshold: [0, 1] }).observe(bar);
}

document.addEventListener("DOMContentLoaded", () => {
  drawSky();
  followSaveBar();
  let resized = null;
  window.addEventListener("resize", () => {
    clearTimeout(resized);
    resized = setTimeout(drawSky, 200);
  });
  tick();

  const settings = document.getElementById("settings");
  for (const happened of ["input", "change"]) {
    settings.addEventListener(happened, () => {
      countChanges();
      showNight();
    });
  }
  settings.addEventListener("submit", async (event) => {
    event.preventDefault();
    const changed = countChanges();
    if (!changed.length) return;
    const changes = Object.fromEntries(
      changed.map((input) => [input.name, input.type === "checkbox" ? input.checked : input.value]));
    const result = await change("/api/settings", { changes });
    if (result.success) {
      for (const input of changed) input.dataset.saved = valueOf(input);
      offerRestart = Boolean(result.restart_needed);
      restartSaid = result.message || "Saved. Some of it is used once Nova restarts.";
      countChanges();
    } else {
      // In the save bar too, where it's still in view once the toast has gone.
      const unsaved = document.getElementById("unsaved");
      unsaved.textContent = result.message || "That didn't work.";
      unsaved.classList.add("bad");
    }
  });

  document.getElementById("add-satellite").addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = document.getElementById("satellite-name");
    const name = field.value.trim();
    if (!name) return;
    const result = await change("/api/satellites/add", { name });
    if (result.success && result.password) {
      field.value = "";
      showPassword(name, result.password, true);
    }
  });
  // Copied from the panel's own text, the only place it's kept. Without the clipboard
  // - which a browser only offers over HTTPS - it's selected, to be copied from there.
  const copy = document.getElementById("password-copy");
  copy.addEventListener("click", async () => {
    const shown = document.getElementById("password-text");
    try {
      await navigator.clipboard.writeText(shown.textContent);
      copy.textContent = "Copied";
    } catch (error) {
      const range = document.createRange();
      range.selectNodeContents(shown);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      toast("Couldn't copy it, so it's selected - copy it from there.", false);
    }
  });
  document.getElementById("password-close").addEventListener("click", closePassword);
  document.getElementById("renew-name").addEventListener("input", () => {
    document.getElementById("renew-go").disabled = document.getElementById("renew-name").value !== renewing;
  });
  document.getElementById("renew-panel").addEventListener("submit", (event) => {
    event.preventDefault();
    giveNewPassword();
  });
  document.getElementById("renew-cancel").addEventListener("click", cancelRenew);
  document.getElementById("renew-panel").addEventListener("keydown", (event) => {
    if (event.key === "Escape") cancelRenew();
  });

  document.getElementById("problems-all").addEventListener("click", () => {
    allProblems = !allProblems;
    showProblems();
  });

  document.getElementById("add-alarm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const result = await change("/api/alarms", { time: form.time.value, repeat: form.repeat.value });
    if (result.success) form.reset();
  });

  const restart = () => {
    if (confirm("Restart Nova now? It takes about half a minute.")) change("/api/restart");
  };
  document.getElementById("restart-button").addEventListener("click", restart);
  document.getElementById("restart-now").addEventListener("click", restart);

  // Gone as it's pressed, so it can't be pressed twice - back at the quick check
  // once Nova's answered, if anything's still ringing.
  const stop = document.getElementById("stop");
  stop.addEventListener("click", async () => {
    stop.hidden = true;
    stopping = true;
    try {
      await change("/api/stop", {}, async () => {
        stopping = false;
        await live();
      });
    } finally {
      // Whatever happened, so Stop can't stay away for good.
      stopping = false;
    }
  });

  // Put back as Nova last had it if the change didn't work - even with Nova out of
  // reach, when the page can't be shown afresh.
  document.getElementById("mute").addEventListener("change", async (event) => {
    const result = await change("/api/mute", { muted: event.target.checked });
    if (!result.success) showMuted();
  });

  const cover = document.getElementById("cover");
  cover.addEventListener("load", () => (cover.hidden = false));
  cover.addEventListener("error", () => (cover.hidden = true));
  document.getElementById("play-pause").addEventListener("click", () =>
    playMusic({ action: music && music.now && music.now.playing ? "pause" : "play" }));
  document.getElementById("previous").addEventListener("click", () => playMusic({ action: "previous" }));
  document.getElementById("next").addEventListener("click", () => playMusic({ action: "skip" }));
  document.getElementById("spotify-start").addEventListener("click", () => startSignIn("spotify"));
  document.getElementById("spotify-finish").addEventListener("submit", (event) => {
    event.preventDefault();
    finishSignIn("spotify", loadMusic);
  });
  document.getElementById("google-start").addEventListener("click", () => startSignIn("google"));
  document.getElementById("google-finish").addEventListener("submit", (event) => {
    event.preventDefault();
    finishSignIn("google", loadCalendars);
  });
  document.getElementById("new-events-calendar").addEventListener("change", (event) =>
    changeCalendar("/api/calendars/new-events", { id: event.target.value }));
  document.getElementById("weather-entity").addEventListener("change", (event) =>
    changeHomeChoice("/api/weather", { id: event.target.value }));
  document.getElementById("play-something").addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = event.target.query;
    if (!query.value.trim()) return;
    const result = await playMusic({ action: "play", query: query.value }, true);
    if (result.success) query.value = "";
  });

  document.getElementById("new-routine").addEventListener("click", () => openEditor(null));
  document.getElementById("cancel-routine").addEventListener("click", closeEditor);
  document.getElementById("routine-editor").addEventListener("submit", async (event) => {
    event.preventDefault();
    const field = (id) => document.getElementById(id).value;
    // With the choice hidden, a routine is saved as it was - not dropped to the default.
    // That's one satellite at most, so a satellite since removed goes to the default,
    // where Nova moves its routines anyway, rather than being refused with no choice
    // shown to change it.
    const kept = editing && satellites.some((satellite) => satellite.name === editing.satellite)
      ? editing.satellite : "";
    const runsOn = document.getElementById("routine-satellite-field").hidden && editing
      ? kept : field("routine-satellite");
    const routine = { id: editing ? editing.id : undefined, name: field("routine-name"), phrases: field("routine-phrases"),
                      at: field("routine-at"), repeat: field("routine-repeat"), steps: field("routine-steps"),
                      satellite: runsOn };
    const result = await change("/api/routines", { routine });
    if (result.success) closeEditor();
  });
  document.getElementById("delete-routine").addEventListener("click", async () => {
    if (!editing || !confirm(`Delete ${editing.name}?`)) return;
    const result = await change("/api/routines/remove", { id: editing.id });
    if (result.success) closeEditor();
  });

  document.getElementById("login").addEventListener("submit", logIn);
  // Reloaded rather than only locked, so nothing the page showed stays in it - Log out
  // is for a shared or lost device, whose browser someone else might look into.
  document.getElementById("log-out").addEventListener("click", async () => {
    const result = await change("/api/logout", {}, async () => {});
    if (result.success) location.reload();
    else await checkLogin();
  });
  document.getElementById("log-out-everywhere").addEventListener("click", () => {
    if (confirm("Log out every device which logged in with the page password?")) {
      change("/api/logout-everywhere", {}, checkLogin);
    }
  });

  every(STATE_EVERY, load);
  every(LIVE_EVERY, live);
  every(MUSIC_EVERY, loadMusic);
  every(HOME_EVERY, loadHome);
  every(BATTERIES_EVERY, loadBatteries);
  every(CALENDARS_EVERY, loadCalendars);
  every(HOME_CHOICES_EVERY, loadHomeChoices);
  document.getElementById("also-send-box").addEventListener("change", (event) =>
    changeBattery("/api/batteries/also", { on: event.target.checked }));
  // Each of the above waits, locked, until this says the page may be shown.
  checkLogin();
});
