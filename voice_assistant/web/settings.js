// Nova's settings page - see voice_assistant/settings_page.py.
//
// Everything shown comes from /api/state, and every change is sent with the X-Nova
// header, which the page only accepts changes with. Anything from Nova - a fact it
// remembers, a line from its log - is put on the page as text, never as HTML.

"use strict";

const VOLUME_NAMES = { general: "Voice", alarm: "Timers and alarms", music: "Music" };

function element(tag, text, className) {
  const made = document.createElement(tag);
  if (text !== undefined) made.textContent = text;
  if (className) made.className = className;
  return made;
}

function toast(message, ok) {
  const shown = document.getElementById("toast");
  shown.textContent = message;
  shown.className = ok === false ? "show bad" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (shown.className = ""), 4000);
}

async function change(path, body) {
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Nova": "page" },
      body: JSON.stringify(body || {}),
    });
    const result = await response.json().catch(() => ({ success: false, message: "Nova didn't answer properly." }));
    toast(result.message || (result.success ? "Done." : "That didn't work."), result.success);
    await load();
    return result;
  } catch (error) {
    toast("Couldn't reach Nova.", false);
  }
}

function since(seconds) {
  const minutes = Math.floor((Date.now() / 1000 - seconds) / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} hour${hours === 1 ? "" : "s"}`;
  return `${Math.floor(hours / 24)} days`;
}

function showStatus(status) {
  const list = document.getElementById("status");
  list.replaceChildren();
  const rows = [
    ["Running for", since(status.started)],
    ["Typical reply", status.reply_ms === null ? "Not timed yet" : `${(status.reply_ms / 1000).toFixed(1)}s after you stop, over ${status.replies} replies`],
  ];
  if (status.backend !== null) rows.push(["Backend", status.backend]);
  if (status.ntfy !== null) rows.push(["ntfy", status.ntfy]);
  for (const [name, value] of rows) list.append(element("dt", name), element("dd", value));

  const problems = document.getElementById("problems");
  problems.replaceChildren();
  if (!status.problems.length) problems.append(element("li", "None.", "quiet"));
  for (const problem of status.problems) {
    const row = element("li", undefined, problem.level);
    row.append(element("span", problem.time, "when"), element("span", problem.message));
    problems.append(row);
  }
  document.getElementById("summary").textContent =
    status.problems.some((problem) => problem.level !== "warning") ? "Something's gone wrong lately - see below." : "All well.";
}

function showVolumes(volumes) {
  const shown = document.getElementById("volumes");
  shown.replaceChildren();
  for (const [category, percent] of Object.entries(volumes)) {
    const label = element("label", undefined, "slider");
    const value = element("output", `${percent}%`);
    const slider = element("input");
    Object.assign(slider, { type: "range", min: 0, max: 100, step: 5, value: percent });
    slider.addEventListener("input", () => (value.textContent = `${slider.value}%`));
    slider.addEventListener("change", () => change("/api/volume", { category, percent: Number(slider.value) }));
    label.append(element("span", VOLUME_NAMES[category] || category), slider, value);
    shown.append(label);
  }
}

function showClock(clock) {
  const shown = document.getElementById("clock");
  shown.replaceChildren();
  const items = [...clock.timers.map((item) => ({ ...item, kind: "timer" })), ...clock.alarms.map((item) => ({ ...item, kind: "alarm" }))];
  if (!items.length) shown.append(element("li", "Nothing set.", "quiet"));
  for (const item of items) {
    const row = element("li");
    const due = new Date(item.due_at * 1000).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });
    const remove = element("button", item.kind === "alarm" ? "Remove" : "Cancel", "small");
    remove.type = "button";
    remove.addEventListener("click", () => change("/api/remove", { kind: item.kind, id: item.id }));
    row.append(element("span", item.name), element("span", due, "when"), remove);
    shown.append(row);
  }
}

function showMemories(memories) {
  const shown = document.getElementById("memories");
  shown.replaceChildren();
  if (!memories.length) shown.append(element("li", "Nothing yet.", "quiet"));
  for (const fact of memories) {
    const row = element("li");
    const forget = element("button", "Forget", "small");
    forget.type = "button";
    forget.addEventListener("click", () => change("/api/forget", { fact }));
    row.append(element("span", fact), forget);
    shown.append(row);
  }
}

function showKeys(keys) {
  const shown = document.getElementById("keys");
  shown.replaceChildren();
  for (const key of keys) {
    const form = element("form", undefined, "key");
    const input = element("input");
    Object.assign(input, { type: "password", autocomplete: "off", placeholder: key.set ? "Replace…" : "Not set", name: key.name });
    input.setAttribute("aria-label", key.label);
    const save = element("button", "Save", "small");
    form.append(element("span", key.label), element("span", key.set ? "Set" : "Not set", key.set ? "good" : "quiet"), input, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!input.value) return;
      await change("/api/keys", { name: key.name, value: input.value });
      input.value = "";
    });
    shown.append(form);
  }
}

function showSettings(settings, problem) {
  const form = document.getElementById("settings");
  const problemShown = document.getElementById("settings-problem");
  problemShown.hidden = !problem;
  problemShown.textContent = problem || "";
  form.replaceChildren();
  for (const setting of settings) {
    const label = element("label", undefined, setting.kind === "yes/no" ? "check" : "");
    const input = element("input");
    input.name = setting.key;
    input.dataset.kind = setting.kind;
    if (setting.kind === "yes/no") {
      input.type = "checkbox";
      input.checked = Boolean(setting.value);
    } else {
      input.type = setting.kind === "number" ? "number" : "text";
      if (setting.kind === "number") input.step = "any";
      input.value = Array.isArray(setting.value) ? setting.value.join(", ") : setting.value ?? "";
    }
    label.append(element("span", setting.label), input);
    form.append(label);
  }
  const save = element("button", "Save settings");
  save.type = "submit";
  form.append(save);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("settings").addEventListener("submit", (event) => {
    event.preventDefault();
    const changes = {};
    for (const input of event.target.querySelectorAll("input")) {
      changes[input.name] = input.dataset.kind === "yes/no" ? input.checked : input.value;
    }
    change("/api/settings", { changes });
  });
  document.getElementById("add-alarm").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    change("/api/alarms", { time: form.time.value, repeat: form.repeat.value });
    form.reset();
  });
  document.getElementById("restart-button").addEventListener("click", () => {
    if (confirm("Restart Nova now? It'll take half a minute.")) change("/api/restart");
  });
  load();
  setInterval(load, 30000);
});

async function load() {
  try {
    const response = await fetch("/api/state");
    if (!response.ok) throw new Error(await response.text());
    const state = await response.json();
    showStatus(state.status);
    showVolumes(state.volumes);
    showClock(state.clock);
    showMemories(state.memories);
    // Typing in these mustn't be undone by the page refreshing itself.
    if (!document.querySelector("#keys input:focus, #settings input:focus")) {
      showKeys(state.keys);
      showSettings(state.settings, state.settings_problem);
    }
    document.getElementById("restart").hidden = !state.restart_needed;
  } catch (error) {
    document.getElementById("summary").textContent = `Couldn't reach Nova: ${error.message}`;
  }
}
