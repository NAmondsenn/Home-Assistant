# 8. Home Assistant first: new things by voice

24 October 2026 · Phase 5 · Agreed with the user, not yet built

**The user's answers (24 October)**

1. **One small new library on the brain, `websocket-client`: yes.** It reads Home Assistant's entity registry. Only the registry has each thing's other names ("main light" for Bedroom Light) and whether it's exposed to Assist; the REST API and templates have neither. So Claude is told the lights' other names (7), Claude can run exposed scripts when Assist misses the wording (4), lists answer to their other names in Home Assistant (12), and the guard asks Assist what a sentence would act on (14). It's pure Python, has no dependencies of its own, and runs only on the brain. It's pinned in `requirements-backend.txt` and audited weekly like the others.
2. **The calendar comes straight from Google, not through Home Assistant:** "the voice assistant should still pull callendar straight from google, rather than ha -> google". Adding events goes straight to Google too (10, 11). It's the one exception to rule 3 here, and it's the user's.

**Context.** The review of 23 October (`audit-ha.md`) found that Nova finds everything it uses in Home Assistant by itself, but can reach very little of it. Only sentences ending in "light" or "lamp" go to Assist. Claude's only Home Assistant tools are the lights and reading the calendar. So "pause the TV", "run goodnight", "is Navidrome up?", "where's my phone?", "put milk on my shopping list" and "turn off my PC" all end up with Claude. Claude can't do any of them, and offline they fail outright.

Home Assistant (2026.10.2) has, from the inventory taken that day and the user's changes since:
- four to-do lists: `todo.shopping_list` "Shopping list", `todo.todo_long` (renamed "todo" today, and the main one), `todo.notepad` "notepad" and `todo.uni_work` "uni-work";
- three Google calendars: "Nathaniel", the main one; "Uni" (`calendar.my_timetable`), the lecture timetable; and "Work" (`calendar.work`), the shifts. Nova reads them through Home Assistant today, and from Google itself once this is built (answer 2);
- Met's `weather.forecast_home`, for Home Assistant's home location;
- the Home Assistant app (`mobile_app`) on the phone, the tablet and the Mac, each with a tracker and its own notify service, `notify.mobile_app_*`;
- the Windows PC: six HASS.Agent buttons over MQTT (`button.windows_pc_shutdown`, `_restart`, `_sleep`, `_monitorsleep`, `_lock` and `_launchurl`), and a Wake-on-LAN button of its own;
- 13 scripts (goodnight, the Mac's power and remote-mode scripts, and the calendar form's helpers), and no scenes;
- switches (AdGuard's, qBittorrent's), media players (Spotify, two Jellyfin players), the sun, 24 updates and the backups;
- 216 Proxmox entities. Among them are buttons that shut down the host the brain runs on, or stop every guest on it.

Fixes batch 1 comes first. It refuses locks, valves, alarm panels, and door, garage, gate and window covers, by what Home Assistant says they are, before anything reaches Assist. It puts every service call through one allowlist (`SERVICES` in `home_assistant.py`), and it lets Claude run only the tools it's offered. Next, 0007's batteries are built, with their own `get_batteries` tool.

The user decided these on 23 October, and they're theirs here:
- blinds and curtains may open by voice; doors, garages, gates and windows never;
- "Lock my PC" is allowed;
- scripts by voice are only those exposed to Assist in Home Assistant (the settings page asks before running any script - that's 0009's);
- "ring my phone" always works, wherever the user is;
- new calendar events go to the main Google calendar by default, and that's changeable on the page;
- the calendar is read from, and added to, Google itself, not through Home Assistant (24 October);
- the phone keeps ntfy, and the Mac and tablet use Home Assistant's notify services.

On lists, in their words: "let me be able to say notepad/notes, todo, shopping list or uni work to add to the correct one." The todo list is "obvs" the main one.

The user's standing rules apply, especially two:
- Rule 3: "if the voice assistant can get any info or perform and action through ha, it should unless its significantly slower".
- Rule 4's line: the language model must never be able to get round a restriction. Nothing that locks, unlocks or opens is ever done by voice.

**Decision.**

1. **Each request goes the shortest way that works.** A request Nova recognises locally goes to Assist if Assist has a sentence for it. If Assist hasn't, it goes to Nova's own tool, which uses Home Assistant's services. A request goes to Claude only when neither can make sense of the words. The calendar is the one exception, by the user's choice: Nova reads it from, and adds to, Google itself (10). Every service Nova calls stays behind batch 1's allowlist, and every request to Assist behind batch 1's check of what Assist would act on. Decision 14 lists the only things either one gains. The table at the end gives each piece's route, and what still works with the internet down.

2. **Assist works more than the lights (HA 2).** The local rule that sends requests to Assist takes Assist's own sentence shapes, for anything Assist knows:
   - "turn on X", "switch X off";
   - "pause X", "resume X";
   - "next track on X";
   - "set X volume to 40";
   - "is X on?";
   - "activate X", "run X";
   - "open X", "close X" - for blinds, curtains and shades, which the user allows (their decision 1). A door, garage, gate or window is never opened or closed: batch 1's guard refuses it by its device class, whatever it's called.

   Nova's own commands still come first: timers, alarms, reminders, the music, Nova's volume, notifications, the time, the weather and the calendar. So "pause" is still Nova's music, and "pause the TV" goes to Assist. Words that only point back, such as "it", "this" and "that", stay with Claude, which knows what they mean. Assist reaches whatever the user exposes to it in Home Assistant. That's the allowlist for these, and it's kept where the devices are, so a fan added and exposed later works by itself. Assist answers "is X on?" in its own words. A sentence Assist matches nothing in goes to Claude, as now. Claude gets no tool of its own for switches, fans or media players. When Assist can't work something, Nova says it can't.

3. **Claude gets a read-only `home_status` tool (HA 3).** It's for questions like "when's sunset?", "is Navidrome up?", "any updates?", "is the PC on?", "when did the backup last run?" and "how warm is the bedroom?". It takes the words asked about and reads every entity's state in one request. It gives Claude the best few matches by name, entity id and area, each with its state and unit, and its age if it's old. It calls no service, so it can't get round anything.

   It leaves out two kinds of thing:
   - Battery levels and charging states. Those are 0007's.
   - Street addresses and coordinates. "Where's my phone?" is decision 8's, and it's answered without Claude.

   **0007's tool isn't folded into it.** `get_batteries` does things a general read can't:
   - it pairs each battery with its charging sensor through the device;
   - it knows a phone from a tablet from a laptop;
   - it says the age in words;
   - the local commands share it, so a question gets the same answer either way.

   Folding it in would lose those, or bring the battery rules into the general tool. So Claude is offered both, and `home_status`'s description sends battery questions to `get_batteries`.

4. **Scripts and scenes by voice (HA 4), with the user's decision 3.** By voice, Nova runs only the scripts and scenes that are exposed to Assist.
   - "Run goodnight", "turn on goodnight" and "activate movie night" go through the local rule to Assist. Assist only knows the exposed ones, so Home Assistant itself holds that line.
   - Claude also gets `run_home_script(name)`, for wordings Assist misses ("do my goodnight thing"). It matches the name against the scripts and scenes Home Assistant has, refuses one that isn't exposed to Assist - which the registry says (answer 1) - and calls `script.turn_on` or `scene.turn_on`. If the registry can't be read, it refuses every script and scene, since it can't tell which are exposed.
   - Either way, before anything runs, Nova checks what it does as well as what it's called. That check is part of batch 1's guard on what goes to Assist, and Claude's tool and the settings page use it too. A scene is refused, as a lock is, if its members include a lock, valve, alarm panel or door-type cover. So is a script if its configuration names one of those, or one of their services, or runs another script that does. The configuration comes from `/api/config/script/config/<id>`, which Nova's administrator's token can read.
   - Routines reach scripts with the same words ("run goodnight"). So `routines.py`'s promise that "a routine can do anything Nova can" now holds for scripts too.

5. **The weather comes from Met, through Home Assistant (HA 5).** Nova reads Home Assistant's weather entity:
   - how it is now, from the entity's state and attributes;
   - today and tomorrow, from `weather.get_forecasts`, daily and hourly. That's a service that only reads, and it's allowed as one.

   The hourly forecast lets "will it rain later?" be answered properly. The entity is found by itself: if there's only one, that one; if there are several, the one chosen on the page (0009). With the internet down, Home Assistant still has its last forecast, and Nova says how old it is once it's more than a few hours old. Open-Meteo goes, and so do `weather.location` and its field on the page. With Home Assistant down, there's no weather, and Nova says it can't reach Home Assistant.

6. **Dimming works without Claude (HA 6).** "Dim the lights", "brighten the lamp" and "dim the bedroom lights" go to Nova's own `set_lights`, not to Assist, which has no sentence for them (0005). Nova reads the lights once, with the template it already uses, then calls `light.turn_on` with each light's new brightness:
   - a step down or up from where each light is now;
   - never below 5%, and never off;
   - a light that's off stays off when dimmed;
   - brightening a light that's off switches it on at one step.

   With no light named, it's Nova's room. A light can be named by its name or by its other names (7), and a room by its name. A name Nova doesn't know goes to Claude, as now. "Set the lamp to 30%" stays with Assist, which understands it.

7. **The lights' other names (HA 7).** Nova reads the entity registry every few minutes, with answer 1's library. Claude's list of lights then says "Bedroom Light, also main light (light.bedroom_light, Bedroom)", as 0005 promised, and decision 6 knows those names too. The same read gives the lists' other names, and which scripts are exposed (4). If the read fails, Nova carries on with names alone, and the guard falls back as 14 says.

8. **"Where's my phone?" and "ring my phone" (HA 8), with the user's decision 5.**
   - **Which device.** The devices are the Home Assistant app's, found as Nova runs. "My phone", "my tablet" and "my Mac" are told apart the way 0007 tells them apart. That way of naming the user's devices is shared by the batteries, this decision and decision 9.
   - **Where.** From the device's tracker: home, or the zone it's in, or else the place it last reported, and how long ago that was.
   - **Ringing.** It always rings, wherever the user is (their call), through the device's `notify.mobile_app_*`. On Android, the app says it aloud on the alarm stream at full volume, so it's heard on silent. On the Mac, it's an ordinary notification with a sound. Nova says "Ringing your phone." once Home Assistant has accepted it.
   - **Phrasings.** "Where's my phone?", "find my phone", "ring my phone" and "where's my tablet?" are local commands. Other wordings reach Claude's `find_device(device, ring)`.

9. **"Send that to my Mac" or tablet (HA 9), with the user's decision 10.** Sending gains a device.
   - The phone, or no device named, goes through ntfy, as today. It works with Home Assistant down, and the homelab's alerts are already there.
   - Any other device with the Home Assistant app goes through its `notify.mobile_app_*`. That's the Mac and the tablet now. The service is found among Home Assistant's services as Nova runs, so nothing names the Mac's, and a new laptop with the app becomes a target by itself.

   "Send that to my Mac" and "send it to the tablet" join the local commands.

10. **The calendar, straight from Google (HA 10), with the user's decision 6 and answer 2.**
    - **Signing in.** Nova has its own Google sign-in, kept on the brain and readable by Nova's user only, never on a satellite. It asks Google only for what it uses: the list of calendars, and reading and adding events. Until 0009's page can do it, it's done once with a command on the brain, the way Spotify's is today. When it needs doing again, Nova says so.
    - **Reading.** "What have I got on Thursday?" reads Google's calendars, with each repeating event on its own day, as Home Assistant gives them today. That replaces Home Assistant's `/api/calendars`. It's fresher too: an event added on the phone is there at once.
    - **Which calendars.** They come from Google's own list of the user's calendars, as Nova runs, so one added later turns up by itself. Google says which is the primary one, the user's own ("Nathaniel" today), so nothing is guessed.
    - **Adding.** Claude gets `add_to_calendar(summary, start, end, all_day, calendar)`, which adds the event through Google, to a calendar Google says Nova may add to.
    - New events go to the calendar chosen on the page (0009). Until one is chosen, they go to the primary calendar. A calendar named in the request is used instead: "put my shift on the work calendar" goes to Work.
    - With no end said, an event lasts an hour. With no time said, it's all day.
    - Nova says back what it added, with the day and time ("Added Dentist, Thursday the 30th at 3 PM."), so a mishearing is heard straight away.
    - Nothing is changed or deleted by voice.
    - **Offline.** With the internet down, or Google not answering, there's no calendar, and Nova says it can't reach Google. No copy is kept for offline, to keep it simple.

11. **Activities come from the calendars (HA 11, S 4).** An activity is a calendar, called by its name in Google: "Uni" is uni and "Work" is work.
    - Every event on the Work calendar is work, whatever it's called.
    - Every calendar counts except Google's primary one, the user's own ("Nathaniel" today), which holds everything else. Google says which that is (10). The page can switch any calendar on or off as an activity (0009).
    - A calendar added later, such as "Gym", is an activity by itself, so "when's gym tomorrow?" is answered locally.
    - The words heard locally are built from the activities' names and their plurals. A few English words for the same things stay in code: working, shift and shifts for work; university, lecture and class for uni. They're words, not devices.
    - Claude's `get_activity` description names the activities found.
    - `home_assistant.activities` and its event-name words go from `config.yaml`. Nova stops telling the user to edit a file: it says "I haven't got a calendar called gym."

12. **Lists, in Home Assistant's own to-do lists.** Every `todo.*` list is found as Nova runs, by its name in Home Assistant.
    - **Which list.** The user's words are matched to lists by name, forgivingly:
      - case doesn't matter, and nor does the word "list";
      - spaces, hyphens and underscores are all the same, and "to do", "to-do" and "todo" are one word;
      - plurals count;
      - a word that a list's name starts with counts, if only one list's name does.

      So "notes" and "notepad" are notepad, "uni work" is uni-work, "todo" is todo and "shopping" is Shopping list. The lists' other names in Home Assistant count too (7). If two lists fit, Nova asks which. If none fits, it says "I haven't got a list called that." A list added later works by itself.
    - **With no list named** ("add milk to my list"), it's the main list: todo, in the user's words. The page can change it (0009).
    - **One thing added** ("put milk on my shopping list"). This goes to Assist first, which knows the lists by their names and their other names, and works offline. If Assist matches nothing - "notes" isn't one of notepad's names - Nova adds the item itself with `todo.add_item`, still without Claude.
    - **Several things at once** ("eggs, milk and bread"). These go to Claude, which splits them and calls Nova's list tool. Assist would add "eggs, milk and bread" as a single item, and Nova can't safely split "salt and vinegar crisps" by itself. Offline, Nova adds what was said as one item, rather than losing it.
    - **Reading** ("what's on my todo list?"). Nova reads the list with `todo.get_items`, a service that only reads, and says the items still to do: up to ten, then how many more there are. This is local.
    - **Ticking off** ("tick off milk", "I've got the milk", "take milk off my shopping list"). Assist goes first where it has the sentence. Otherwise Nova marks the item done with `todo.update_item`, matching it forgivingly, and asks if two items match.
    - **Nothing is deleted by voice.** A ticked-off item stays in Home Assistant, where it can be put back.
    - Claude gets one tool for adding, reading and ticking off.

13. **The PC on and off.**
    - **What can be done.**
      - "Turn on my PC" presses Home Assistant's Wake-on-LAN button, found through the `wake_on_lan` integration.
      - "Turn off", "shut down", "restart" and "put to sleep" press HASS.Agent's buttons for each.
      - So do "turn off the PC's screen" and "lock my PC". The lock is the user's call, since it only makes things safer.
    - **How the buttons are found.** HASS.Agent's buttons are found by their device, the MQTT device that has a shutdown button. They're never found by the `windows_pc_` prefix, which the user can rename.
    - **What's never pressed.** HASS.Agent's other buttons (launching a web address, custom commands), and any Proxmox button.
    - **Asking first.** Shutting down and restarting ask first - "Shut down the PC?" - and only a yes does it, the way Nova already asks about a reminder. Sleep, screen off and lock don't ask.
    - **Not through Assist.** These go through Nova's own `button.press`. The Wake-on-LAN button isn't called "PC", and Assist would press any button that's exposed.
    - **Is it on?** Whether the PC is on comes from whether HASS.Agent's entities are available. So "is my PC on?" is answered locally, and "turn on my PC" while it's on says so.
    - **Phrasings.** These are local commands. Other wordings reach Claude's `pc_power(action)`.
    - **A second PC.** A second PC's HASS.Agent device is found by itself. If there's more than one PC, or more than one Wake-on-LAN button, which button wakes which PC is chosen on the page (0009).

14. **What the allowlist and the guard gain - and nothing more.** Batch 1's `SERVICES` gains:

    | Domain | Services | Only ever given |
    | --- | --- | --- |
    | `todo` | `add_item`, `update_item`, `get_items` | to-do lists |
    | `weather` | `get_forecasts` | weather entities |
    | `notify` | `mobile_app_*` only | nothing: each device is its own service |
    | `button` | `press` | the PC's allowed HASS.Agent buttons and Wake-on-LAN's, each checked by its device or integration |

    `light`, `script` and `scene` stay as they are, plus 4's check of what a script or scene does. The services that only read, `get_items` and `get_forecasts`, are asked for their answer. Google's calendar isn't a Home Assistant service, so it's outside this list: Nova's Google client only ever lists calendars, reads events and adds them, and never changes or deletes one. These stay refused:
    - `lock`, `valve` and `alarm_control_panel`;
    - opening a cover through Nova's own services - blinds and curtains open through Assist, behind batch 1's guard (2);
    - `hassio`, `homeassistant` and `shell_command`;
    - removing a to-do item;
    - every Proxmox button.

    The guard on what goes to Assist gains four things:
    - Proxmox's entities, found through the integration as Nova runs, so "turn on PVE2 shut down" is refused even if it's been exposed;
    - scripts and scenes that work something guarded (4);
    - HASS.Agent's shutdown, restart, web-address and custom-command buttons. Through Assist they'd be pressed with no yes, so they only ever go through Nova's own press (13);
    - other names. Decision 2 sends far more to Assist, so a guarded thing must be refused under any name Assist knows it by, not only its own.
      - The guard reads every entity's other names from the registry (7). Before acting, it asks Assist what a sentence would act on - the WebSocket API's own sentence debugging, which matches without doing anything - and refuses it if any target is guarded.
      - If the registry can't be read, or the sentence debugging doesn't answer, decision 2's wider rule is off, and only today's light sentences go to Assist until both work again. It fails shut, not open.
      - Either way, a lock exposed under the name "front door" is never reached by "turn off the front door". There's a test for each.

15. **What 0009 puts on the settings page.** 0008 works on its own, before 0009's page exists: it keeps each choice below in the data folder, with its default, and the page only adds the controls. These are the choices this design adds:
    - the main list (todo to begin with);
    - the calendar new events go to (Google's primary to begin with);
    - which calendars count as activities (all but that one to begin with);
    - the weather entity, only if there's more than one;
    - which Wake-on-LAN button wakes which PC, only if there's more than one of either.

    The devices things can be sent to are found by themselves, so the page shows them but there's nothing to choose. `weather.location` leaves the page. Nothing in `config.yaml` names an entity.

**Each piece, by how it goes.** With the brain down, none of this works: the satellite only rings its own copy of the timers and alarms (0006).

| Piece | Goes through | With the internet down | Rule 3 | Rule 4's line |
| --- | --- | --- | --- | --- |
| Switches, fans, media players, "is X on?" (2) | Assist | Works | Home Assistant's own understanding, one call on the local network | Batch 1's check by kind first; only what's exposed |
| `home_status` (3) | A read of every state | No - it's Claude's | It's all Home Assistant's; Nova keeps nothing | Reads only |
| Scripts and scenes (4) | Assist; Claude's tool by a service call | Assist's way works | The user's own actions in Home Assistant | Exposed ones only, contents checked |
| Weather (5) | A read, with `weather.get_forecasts` | Works, from the last forecast, with its age | Replaces Open-Meteo, one fewer setting | Reads only |
| Dimming (6) | A read, then `light.turn_on` | Works | Home Assistant's light service; Assist has no sentence for it | Lights only, never off |
| The lights' other names (7) | A read of the registry | Dimming by them works; Claude doesn't | The names given in Home Assistant | Reads only |
| Where's my phone (8) | A read of the tracker | Works, from the last report | The app already reports it | Reads only; not sent to Claude when asked locally |
| Ring my phone (8) | `notify.mobile_app_*` | Only if the app's own connection on the home Wi-Fi is on - otherwise it needs Google's push service | The app's own ring | Notifies only |
| Sending to the Mac or tablet (9) | `notify.mobile_app_*` | As for ringing; the phone's ntfy as today | Home Assistant's, except the phone (the user's call) | Notifies only |
| What's on, and activities (10, 11) | A read of Google's calendars | No | Not through Home Assistant - the user's choice (answer 2) | Reads only |
| Adding to the calendar (10) | Claude, then Google's calendar | No | The same | Adds only; never changes or deletes |
| Lists: one thing added, ticked off (12) | Assist, then `todo.add_item` or `todo.update_item` | Works | Home Assistant's own lists | Never deletes |
| Lists: several things (12) | Claude, then `todo.add_item` | Added as one item, as said | The same | The same |
| Lists: reading (12) | `todo.get_items`, a read | Works | The same | Reads only |
| The PC (13) | `button.press` on Wake-on-LAN's or HASS.Agent's buttons | Works - it's all on the local network | Home Assistant's own buttons | Named kinds only; never Proxmox's; shutting down and restarting ask first |

**Goals.**
- The everyday things in Home Assistant work by voice, mostly without Claude, and many of them with the internet down.
- Lists, calendars, devices, scripts and PCs added later work by themselves, with nothing named in Nova's files.
- Whatever can go through Home Assistant does, and each exception says why.
- Nothing that locks, unlocks or opens is done by voice, and nothing on Proxmox, whatever's said, exposed or asked for by Claude.

**Not goals.**
- Claude working switches, fans or media players itself. Assist does that, for what's exposed.
- Changing or deleting calendar events, or deleting list items, by voice.
- Spotify, or Nova's timers and alarms, through Home Assistant. They stay as `audit-ha.md` recommends.
- The listening settings and false wakes (the user's decision 7), and splitting `clock.py` (decision 11).
- Anything on the satellite.
- New services (rule 5).

**What would make it a failure, even working.**
- Anything locked, unlocked or opened by voice, or a Proxmox button pressed, whether through Assist, a script, a scene or Claude.
- A script that isn't exposed to Assist run by voice.
- One of Nova's own commands caught by the wider Assist rule. For example, "turn off the microphone" would unmute Nova through its own switch in Home Assistant, and "turn off my alarm" would go to Assist.
- An item put on the wrong list, or "Added" said when it wasn't. Several things put on as one item while Claude could have split them.
- "Dim the lights" switching a light off.
- The PC shut down or restarted without a yes.
- "Sent" or "Ringing" said when Home Assistant didn't accept it.
- An event added on the wrong day or at the wrong time, without Nova saying when.
- Nova's Google sign-in able to change or delete events, or readable by anyone but Nova.
- A new list, calendar or device that needs a file edited before it works.
- The weather slower than it is today, or with less in it.

**Constraints.**
- It all runs on the brain (CT 107), where each satellite's session has its own Home Assistant client. Nothing is added to the satellite.
- Nova's token is an administrator's, so templates and `/api/config/script/config` work. If it weren't, a script's contents couldn't be checked, and then no script would run by voice.
- Nova's Google sign-in is a new secret, on the brain only. It needs a Google Cloud OAuth client - the user's own, or the one Home Assistant's Google integration uses - set to "In production". In "Testing", Google ends the sign-in after a week. No Google library is added: plain HTTPS does it.
- Every new service call goes through batch 1's allowlist, and every request to Assist through batch 1's guard. Neither gains anything beyond decision 14.
- Settings this design takes out of `config.yaml` (`weather.location`, `home_assistant.activities`, `home_assistant.calendars`) are still set in the brain's own `~/.config/nova/config.yaml`. Today's config check refuses to start with a setting the repository no longer has, so the brain would stop starting. A removed setting must be let through with a warning, as 0009's decision 2 has it. Whichever of 0008 and 0009 is built first carries that change, in the same task as the first removal.
- The local commands stay strict (0003). Each new shape comes with tests for what it mustn't catch.
- The satellite stops recording after 10 seconds, which cuts a long list short. That's `recording_timeout`, in 0009.
- Claude's tools are cached with its instructions, but each new tool still adds to every request's cached part. Their descriptions stay short.
- It's built after 0007, on `satellite`, like the rest (the ruling of 23 October). It reuses 0007's device kinds and naming.
- No part of it is shaped by a hardware limit (rule 6).
- The repo is public. The main calendar's id is the user's email address, and the Wake-on-LAN button's carries the PC's hardware address. So neither is ever written in code, tests, docs or commits.
- The user does these once, in Home Assistant:
  - expose to Assist what they want worked by voice, and nothing else - Home Assistant doesn't expose scripts by default, so goodnight needs exposing to be run by voice;
  - check what's exposed already, such as AdGuard's protection switch;
  - on the phone, let the Home Assistant app ring through Do Not Disturb.
- The house style, and the repo's habits: tests first, plain-English commits, nothing outward-facing without the user's OK.

**Left to the builder.**
- The exact shapes of decision 2's wider rule, and the words it leaves for Nova or Claude. There should be tests for every failure case above.
- Which list sentences Assist knows on 2026.10 - adding, ticking off and reading. Check read-only, with the user's OK. Where Assist has none, Nova's own way does it.
- How list names and items are matched forgivingly. There should be a test for each of today's four lists, using the user's own words.
- How HASS.Agent's buttons are told apart by kind within their device, and how being unavailable is read as the PC being off.
- How a script's configuration is searched, including scripts it runs, and how a scene's members are read.
- What Met gives: whether its hourly forecast has a chance of rain, or only millimetres, and when a forecast counts as old.
- The Google sign-in with plain HTTPS: the least scopes that list calendars and read and add events, how the sign-in's code comes back to a brain with no browser (as 0009 does for Spotify), and where the token's kept.
- How Google's repeating and all-day events become the shape `events()` gives today, with tests from real examples, so "what have I got on Thursday?" and the activities answer as they do now.
- How a phone is rung on Android and, if one is added, on an iPhone, and the Mac's sound.
- How big a dimming step is (about a third is a start), and whether Home Assistant's own `brightness_step_pct` can do it without ever switching a light off.
- How `home_status` ranks matches and how many it gives, and whether areas come from the same read.
- How often the registry is read.
- The new tools' names, and the words Nova says, in its usual style.

**My calls, not the user's** (for them to check):
- Claude gets no tool of its own for switches, fans or media players. Assist does those, for what's exposed.
- `home_status` isn't folded together with 0007's battery tool, and it leaves out batteries and street addresses.
- Open-Meteo and `weather.location` go. So with Home Assistant down, there's no weather.
- Dimming never goes below 5% or switches a light off, and a light that's off stays off.
- Activities: every calendar but Google's primary one, with a few English words kept in code. The event-name words in `config.yaml` go.
- Lists:
  - one thing goes to Assist first, then to Nova's own tool, all before Claude;
  - several things go to Claude, and offline they're added as one item;
  - with no list named, it's todo;
  - items are ticked off, never deleted;
  - up to ten items are read out.
- Calendar: no confirming, but Nova says back what it added. An event lasts an hour by default, and is all day if no time is given.
- The PC: a yes is needed before shutting down or restarting, but not before sleep, screen off or lock. It goes through Nova's own button press, not Assist.
- Proxmox's buttons are refused through Assist too, found by their integration.
- A script's contents are checked by Nova before it runs, even through Assist. A script that builds its targets from a template isn't caught - it's the user's own, and they exposed it. Without an administrator's token, no script runs by voice.
- "Ringing" and "Sent" are said once Home Assistant accepts the call. Nova can't know whether the device got it.
- "Where's my phone?" says the place out loud. It's only sent to Claude when Claude's own `find_device` is used.
- The guard on Assist refuses a guarded thing by any of its names, and asks Assist what a sentence would act on before acting. If the registry or the sentence debugging can't be reached, only today's light sentences go to Assist.
- HASS.Agent's shutdown, restart, web-address and custom-command buttons are refused through Assist, so a shutdown can never skip its yes.
- Google's calendar keeps no offline copy: with the internet down, Nova says it can't reach Google.
- Until 0009's page can do it, Nova's Google sign-in is a command on the brain, as Spotify's is today.
