// Nova's settings page - see voice_assistant/settings_page.py.
//
// Everything shown comes from /api/state, and what Nova's doing right now from
// /api/live, each asked for again every so often while the page is in view. Every
// change is sent with the X-Nova header, which the page only accepts changes with.
// Anything from Nova - a fact it remembers, a line from its log - is put on the page
// as text, never as HTML, and styles are only ever set through the CSSOM, since the
// page's Content-Security-Policy allows nothing inline.

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

// The least time between volume changes sent while a slider's dragged, in
// milliseconds - often enough for the beep to keep up with the finger, without
// asking for every step - and between a light's, which take Home Assistant longer.
const VOLUME_EVERY = 180;
const LIGHT_EVERY = 250;

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
  ["Weather", ["weather."]],
  ["Music", ["music.", "navidrome.", "spotify."]],
  ["Home", ["home_assistant."]],
  ["Backend", ["backend."]],
  ["Notifications", ["ntfy."]],
  ["Listening", ["thresholds."]],
  ["Privacy", ["logging."]],
];

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

// What /api/home last said.
let home = null;

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

function toast(message, ok) {
  const shown = document.getElementById("toast");
  shown.textContent = message;
  shown.classList.toggle("bad", ok === false);
  shown.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => shown.classList.remove("show"), 4000);
}

// A path which asks after the chosen satellite, if there is one.
function forChosen(path) {
  if (!chosen) return path;
  return `${path}${path.includes("?") ? "&" : "?"}satellite=${encodeURIComponent(chosen)}`;
}

// Sends Nova a change, answering with what it said - for the chosen satellite, which
// only the changes to its own volumes, music, lights, mute and alarms look at.
async function send(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Nova": "page" },
    body: JSON.stringify(chosen ? { ...body, satellite: chosen } : body || {}),
  });
  return response.json().catch(() => ({ success: false, message: "Nova didn't answer properly." }));
}

// Sends a change, says how it went, and shows the page as it is now.
async function change(path, body) {
  let result;
  try {
    result = await send(path, body);
  } catch (error) {
    result = { success: false, message: "Couldn't reach Nova." };
  }
  toast(result.message || (result.success ? "Done." : "That didn't work."), result.success);
  await load();
  return result;
}

async function load() {
  let state;
  try {
    const response = await fetch(forChosen("/api/state"));
    if (!response.ok) throw new Error(await response.text());
    state = await response.json();
  } catch (error) {
    // What's shown stays, and the star says Nova can't be reached.
    return;
  }
  wakeWord = state.wake_word || wakeWord;
  muted = Boolean(state.muted);
  satellites = state.satellites;
  showActivity();
  showStatus(state.status);
  showSatellites(state);
  showMuted();
  showVolumes(state.volumes);
  drawIfChanged("clock", state.clock, showClock);
  drawIfChanged("memories", state.memories, showMemories);
  drawIfChanged("own-routines", state.routines, showOwnRoutines);
  drawIfChanged("keys", state.keys, showKeys);
  drawIfChanged("settings", state.settings, showSettings);
  showSettingsProblem(state.settings_problem);
  document.getElementById("restart").hidden = !state.restart_needed;
}

async function live() {
  let now = "offline";
  try {
    const response = await fetch(forChosen("/api/live"));
    if (response.ok) now = (await response.json()).activity;
  } catch (error) {
    // Out of reach, then.
  }
  // Back after a restart, or being out of reach - so anything else may have changed too.
  if (activity === "offline" && now !== "offline") {
    load();
    loadMusic();
    loadHome();
  }
  activity = now;
  showActivity();
}

// Runs work now, and again every so often while the page is in view - never two at
// once, and not at all while it's hidden, so a phone left on the page isn't kept busy.
function every(milliseconds, work) {
  let timer = null;
  let running = false;
  async function run() {
    clearTimeout(timer);
    timer = null;
    if (running || document.hidden) return;
    running = true;
    try {
      await work();
    } finally {
      running = false;
      if (!document.hidden) timer = setTimeout(run, milliseconds);
    }
  }
  document.addEventListener("visibilitychange", run);
  run();
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
function hearing(backend) {
  const about = { icon: "i-server", label: "Speech to text" };
  if (backend === null) return { ...about, value: "This Pi", detail: "No backend set up" };
  if (backend === "answering") return { ...about, value: "Backend", detail: "Answering", mood: "good" };
  return { ...about, value: "This Pi", detail: "The backend isn't answering", mood: "bad" };
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
    hearing(status.backend),
    notifications(status.ntfy),
  ].map(tile));

  const problems = status.problems.map((problem) => {
    const serious = problem.level !== "warning";
    const row = element("li");
    const what = element("span", undefined, "what");
    what.append(element("span", serious ? "Problem: " : "Warning: ", "visually-hidden"), problem.message);
    const when = element("span", loggedAt(problem.time), "when");
    when.title = problem.time;
    row.append(dot(serious ? "bad" : "warning"), what, when);
    return row;
  });
  document.getElementById("problems").replaceChildren(...(problems.length ? problems : [emptyRow("No problems lately.", "good")]));
}

// Each satellite, and whether it's connected - with a choice of which to look after
// below, when there's more than one.
function showSatellites(state) {
  const rows = satellites.map((satellite) => {
    const row = element("li");
    const what = element("span", undefined, "what");
    what.append(satellite.name, element("span",
      satellite.connected ? (DOING[satellite.activity] || (satellite.muted ? "Microphone muted" : "Listening for its wake word"))
        : "Not connected", "detail"));
    row.append(dot(satellite.connected ? "good" : "off"), what);
    return row;
  });
  drawIfChanged("satellites", satellites, () => document.getElementById("satellites").replaceChildren(
    ...(rows.length ? rows : [emptyRow("No satellites have been added.")])));

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

function showVolumes(volumes) {
  const none = Object.keys(volumes).length === 0;
  // A satellite which isn't connected has no volumes to show.
  document.getElementById("volumes-missing").hidden = !none;
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
    const response = await fetch(forChosen("/api/music"));
    if (!response.ok) return;
    music = await response.json();
    musicAt = Date.now();
  } catch (error) {
    return;
  }
  showMusic();
}

function showMusic() {
  document.getElementById("music").hidden = !music.available;
  document.getElementById("music-missing").hidden = music.available;
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
    const response = await fetch(forChosen("/api/home"));
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
  document.getElementById("delete-routine").hidden = !routine;
  document.getElementById("new-routine").hidden = true;
  document.getElementById("routine-editor").hidden = false;
  document.getElementById("routine-name").focus();
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
      start.disabled = true;
      await change("/api/run", { id: routine.id });
      start.disabled = false;
      // A scene changes the lights, and Home Assistant takes a moment to say so.
      setTimeout(loadHome, 900);
    });
    row.append(icon(scene ? "i-scene" : "i-routine"), element("span", routine.name, "what"), start);
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
}

function settingField(setting) {
  const id = `setting-${setting.key}`;
  const field = element("div", undefined, setting.kind === "yes/no" ? "field switch" : "field");
  const label = element("label", setting.label);
  label.htmlFor = id;
  const input = setting.choices ? choiceInput(setting) : valueInput(setting);
  input.id = id;
  input.name = setting.key;
  input.dataset.saved = valueOf(input);
  field.append(label, setting.choices ? selectBox(input) : input);
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
  input.type = setting.kind === "number" ? "number" : "text";
  if (setting.kind === "number") {
    input.step = "any";
    input.inputMode = "decimal";
  }
  input.value = Array.isArray(setting.value) ? setting.value.join(", ") : setting.value ?? "";
  input.autocomplete = "off";
  input.spellcheck = false;
  return input;
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
  document.getElementById("unsaved").textContent =
    changed.length ? `${counted(changed.length, "change", "changes")} not saved yet.` : "Nothing's changed yet.";
  document.getElementById("save-settings").disabled = !changed.length;
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

document.addEventListener("DOMContentLoaded", () => {
  drawSky();
  let resized = null;
  window.addEventListener("resize", () => {
    clearTimeout(resized);
    resized = setTimeout(drawSky, 200);
  });
  tick();

  const settings = document.getElementById("settings");
  settings.addEventListener("input", countChanges);
  settings.addEventListener("change", countChanges);
  settings.addEventListener("submit", async (event) => {
    event.preventDefault();
    const changed = countChanges();
    if (!changed.length) return;
    const changes = Object.fromEntries(
      changed.map((input) => [input.name, input.type === "checkbox" ? input.checked : input.value]));
    const result = await change("/api/settings", { changes });
    if (result.success) {
      for (const input of changed) input.dataset.saved = valueOf(input);
      countChanges();
    }
  });

  document.getElementById("add-alarm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target;
    const result = await change("/api/alarms", { time: form.time.value, repeat: form.repeat.value });
    if (result.success) form.reset();
  });

  document.getElementById("restart-button").addEventListener("click", () => {
    if (confirm("Restart Nova now? It takes about half a minute.")) change("/api/restart");
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
    const routine = { id: editing ? editing.id : undefined, name: field("routine-name"), phrases: field("routine-phrases"),
                      at: field("routine-at"), repeat: field("routine-repeat"), steps: field("routine-steps") };
    const result = await change("/api/routines", { routine });
    if (result.success) closeEditor();
  });
  document.getElementById("delete-routine").addEventListener("click", async () => {
    if (!editing || !confirm(`Delete ${editing.name}?`)) return;
    const result = await change("/api/routines/remove", { id: editing.id });
    if (result.success) closeEditor();
  });

  every(STATE_EVERY, load);
  every(LIVE_EVERY, live);
  every(MUSIC_EVERY, loadMusic);
  every(HOME_EVERY, loadHome);
});
