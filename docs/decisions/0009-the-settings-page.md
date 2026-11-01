# 9. The settings page: everything the user would change

24 October 2026 · Phase 5 · Agreed with the user, and built

**The user's answer (24 October)**

1. **"Longest request" goes on the page now, with its default going from 10 seconds to 15: yes** (decision 11). The user's decision 7 puts the listening settings - wake sensitivity and the speech timeouts - with the false-wake work, but this one is different. It's the most one request can last, and today it's fixed at 10 seconds on the satellite, whatever the brain's setting says. 0008's lists make long requests likelier ("eggs, milk, bread, butter and ..."). It changes nothing about how Nova hears, only how long Nova keeps listening to someone who's still talking.

**Context.** The review of 23 October (`audit-settings.md`) found that the page covers the everyday controls well, and that its security is sound. But:
- every saved setting restarts the brain. Nova is deaf for about half a minute, and Claude's conversation is lost. That's so even for a setting that's tuned by trying, such as how long a pause ends what's being said;
- there's no Stop. Quieting a ringing alarm from the page means deleting it, and a repeating alarm loses tomorrow's too;
- settings are checked only for their kind. "11pm" is saved as the start of the night, and quietly replaced with 23:00 at the next start;
- one tap runs any script, the Mac's shutdown among them. One tap forgets a memory or deletes an alarm, with no way back;
- adding a satellite, or signing in to Spotify, needs a shell on the brain. The Spotify speaker is one name for every satellite. A routine can't be given a satellite on the page, and saving one there drops the one it had;
- some settings this user would change are still only in files, and the page is awkward on a phone, which is where it's mostly used.

0007 (batteries) adds a Batteries section whose choices work straight away, with no restart. It was planned so that later sections could work the same way, and this design is the first to.

0008 (Home Assistant first) adds lists, calendar events, activities from calendars, the weather from Met, sending to the Mac and tablet, and the PC on and off. Its decision 15 hands this design the choices those make:
- the main list;
- the calendar new events go to;
- which calendars count as activities;
- the weather entity, when there's more than one;
- which Wake-on-LAN button wakes which PC, when there's more than one of either.

`weather.location` leaves the page.

The user decided these on 23 October, and they're theirs here:
- scripts by voice are only those exposed to Assist in Home Assistant, and the settings page asks before running any script (their 3);
- satellites are added and removed on the page, which shows a new satellite's password once (their 8);
- the voice's speed is on the page now, and choosing the voice waits for the AI machine (their 9);
- the listening settings come with the false-wake work, which isn't in this design (their 7).

The user's standing rule 2 is this design's reason: "everything like this that i would need to add to or change in the future i want to be able to do in settings page rather than messing about with a bunch of code and whatever." Rule 1 holds too: anything the page offers from Home Assistant is found as Nova runs. So does rule 4's line: nothing on the page can loosen a restriction.

Fixes batch 1 comes first. It makes every page request get an answer, and stops two quick saves losing one (S14). It also puts the Speech to text tile right (S6), and corrects what `config.yaml` says about the token (S15). The listening settings (S2) wait for the false-wake work, and activities (S4) are 0008's.

**Decision.**

1. **One way for a change to work straight away (S1).** A change made on the page is saved first. Then it's used from the next moment it's needed, read from where it's kept at that moment, rather than copied once at start. That's how 0007's battery choices already work, and everything here works the same way. Things are kept in one of two places:
   - **Settings,** whose defaults are in the repository's `config.yaml`, are kept in the brain's running settings, and saved in the install's own file. Once a save passes its checks (2), the new values go into the running settings, which every session reads. The few parts that copy a setting when they start are changed to read it when they use it. Where that can't be done, the brain tells the part once, after saving. That's the transcripts switch, and Piper's speed (12).
   - **Choices about things Home Assistant or Google has** are kept on the brain, in the data folder, the way 0007 keeps its battery choices. That's 0007's batteries, and 13's calendars, lists, weather and PCs. One small shared helper serves all of them. Nothing in `config.yaml` names an entity.

   What the brain keeps already works straight away: satellites, routines, memory, alarms, volumes and mute.

   A setting that makes a connection or a player waits for a restart, because a connection swapped half way is where things go wrong. That's the music player, the Navidrome, Home Assistant and ntfy addresses, ntfy's alert topics, and MQTT. They're the ones set once for each service. Every setting on the page says which kind it is, "Used straight away" or "Used once Nova restarts", and so does the message after saving. The restart notice only shows for a change that waits, and the save bar then offers "Restart now". If using a change straight away fails, it's still saved, it's used at the next restart, and the page says so. The table at the end lists every setting.

2. **Values are checked for whether they make sense (S5).** Today a setting is checked for its kind, and a few for being between 0 and 1. From now on:
   - Each number has a range, given in the table at the end. A time is a time of day, from 00:00 to 23:59. An address starts with `http://` or `https://`. An ntfy topic has only the letters, digits, hyphens and underscores ntfy accepts.
   - Quiet hours whose start and end are the same mean there are no quiet hours, and the page says so beside them.
   - The same checks run as Nova starts, so a hand edit is caught too. A value out of range stops Nova starting, with a message naming the setting, as a wrong kind does today. Before this is deployed, the brain's own settings file is checked against the new limits, so the update itself can't stop it.
   - A setting that has left `config.yaml` is let through with a warning saying where it's set now, as retired sections already are. That's `weather.location` and `home_assistant.activities` (0008), and `home_assistant.calendars` (13). Today any of them would stop Nova starting on an install whose own file still sets it.
   - On saving, settings are checked against each other too. Navidrome as the player needs Navidrome's address, and ntfy's topics need its server.
   - The page is sent each limit, so the field and a phone's keyboard both know what's allowed. An error names the setting the way the page does: "Seconds of quiet which end what you're saying should be from 0.2 to 3."
   - 0007's marks are checked the same way, from 1 to 99.

3. **A Stop button (S3).** While anything rings, or Nova is speaking, on any satellite, the page's masthead shows Stop. It does what Home Assistant's Stop does today, and through the same code. It silences whatever rings and stops what Nova is saying, on every satellite where something is. A ringing alarm is stopped, not deleted, so a repeating one still rings tomorrow. The music carries on, as it does for Home Assistant's Stop. The page's quick check, every second or two, now says whether anything is ringing, so the button comes and goes by itself.

4. **Undo (S18).** Forgetting a memory, or removing an alarm, timer or reminder, still happens at the first tap. The message that says so then has an Undo button for 10 seconds. Undo puts back exactly what went:
   - the same fact, in the same place;
   - or the same alarm, timer or reminder, with its time, repeat, label and satellite. The satellite is sent its copy again.

   A timer or one-off alarm whose time has passed in the meantime isn't put back, because it would ring straight away. The page says its time has passed. What can be undone is kept on the brain for a minute, and not across a restart. Deleting a routine, and restarting, keep asking first, as now.

5. **Scripts ask first (S7), with the user's decision 3.** Every script asks before it runs - "Run MacBook shutdown?" - and only a yes runs it. Scenes stay one tap.
   - A script or scene that 0008's check refuses, because it works a lock, a valve, an alarm panel or a door, isn't offered on the page at all. So the page is never a way round the rule.
   - Each script that's exposed to Assist is marked "By voice", from 0008's read of the registry, so the user can see what Nova will run when asked. Exposing one is done in Home Assistant, where it's one switch for all of Assist.
   - Every script is shown, as now. There's no switch to hide one.

6. **Satellites are added and removed on the page (S8), with the user's decision 8.** The Satellites list in Status gains four things.
   - **Add a satellite.** It takes the satellite's name. That's its machine's name, which setup.sh uses unless told otherwise, so it's lower-case letters, digits and hyphens. The brain makes its password, and the page shows it once, in a panel with a Copy button and "Type this into setup.sh on the Pi". The panel stays until the user closes it. The password is never logged, never in the page's state, and never shown again. The brain lets the satellite in at its first try, with no restart, as it does for one added on the command line today.
   - **New password,** for a satellite whose password has been lost. It's shown once, the same way. A Pi that's already set up takes it with one command, which the panel shows: `bash setup.sh --new-password`, which asks for the password (not shown as it's typed), saves it as `NOVA_BACKEND_TOKEN` and restarts the satellite. The secret has to reach the Pi somehow, and this is the least there can be. A Pi added again after being removed takes its new password the same way. The satellite keeps its alarms, routines and volumes. If it's connected, it's let go, and it's refused until its setup has the new password.
   - **Remove.** It asks first, and the question says what will happen, by name:
     - it's let go at once if it's connected, and it can't connect again until it's added again;
     - its timers and alarms move to the default satellite, so none is lost. If it's connected, it's first sent an empty copy, so that it doesn't ring them as well. If it isn't connected - a Pi being retired is usually switched off - the question says so plainly: "It still has a copy of its alarms for the next week. If it's switched on before then, it'll ring them too, so wipe it or connect it first." The satellite doesn't clear its copy when the brain refuses its name, since that refusal comes before either side has proved anything, so anything on the tailnet could fake one and silence a satellite's alarms;
     - routines which ran on it run on the default instead;
     - if it was the default, the next one is.

     Its volumes and mute stay in the data folder, in case it's added again. With no satellite left, its timers and alarms go to the next one added, as those from before there were satellites did.
   - **Make default.** The default satellite is the first in the list. It's where ntfy's alerts, 0007's spoken warnings and routines that don't name a satellite are said. Today it's whichever was added first, and it can only be changed by editing a file.

   The command line's `add-satellite` and `remove-satellite` stay, and the brain handles a removal the same way, whichever way it's made. Renaming stays off the page. A satellite's name is its key in the brain's files, its alarms and its routines, and on the Pi.

7. **Each satellite's own Spotify speaker (S9).** A satellite says, as it connects, what its Spotify Connect speaker is called. That's the name its setup gave raspotify, from the satellite's own settings. Each session then plays Spotify on its own satellite's speaker. The brain's `spotify.device_name` is only used for a satellite that doesn't say, such as one not yet updated. The Satellites list shows each satellite's speaker name. It's changed on the Pi, with its setup, because raspotify is there. So a second Pi plays on its own speaker by itself, and two files on two machines no longer have to agree by hand.

8. **Signing in to Spotify, and to Google, on the page (S10).** Google's sign-in, for 0008's calendar, works the same way as Spotify's below, and is offered in Calendars and lists (13). It replaces 0008's command on the brain. With Spotify as the player, and its client ID and secret set under Keys, the Music section says whether Nova is signed in to Spotify. It offers "Sign in to Spotify", or "Sign in again":
   - Spotify's own page opens in a new tab, to approve Nova;
   - Spotify then sends the browser to the redirect address the user's Spotify app was set up with. That page doesn't load, as today, and the user pastes its address back into the settings page;
   - the brain checks that the address carries the code for the sign-in it started, and not another. Then it gets Nova's token, and keeps it where it does now, readable by Nova's user only.

   Sessions use the new sign-in from their next Spotify command, with no restart. When Spotify needs signing in again, Nova says it's done "on the settings page", not "on the server".

   When there's no music player, the Music section says why, rather than "Nova has no music player set up" when one is chosen. For example, Spotify's keys aren't set, it isn't signed in, or Navidrome's password isn't set. Keys are still used once Nova restarts, as now.

9. **The safe settings join the page (S11).** These come off the files and onto the page:
   - the three sounds: the blip when Nova's listening, the chime for "done", and the reminder sound;
   - web search, since each search costs extra;
   - how long Nova keeps the thread of a conversation (`conversation.history_max_age`);
   - whether Spotify pauses while Nova talks;
   - MQTT's host, port and user, so that Nova in Home Assistant can be set up from the page. Its password is already under Keys.

   All but MQTT's are used straight away. Which calendars are read moves to 13. The README's steps for each service send the user to the page's Keys and Settings, with the files as the fallback.

10. **A routine's satellite (S12).** The routine editor gains "Runs on". It lists the default first, named - "The default (bedroom)" - then each satellite. It's only shown once there's more than one satellite. It's saved with the routine, so saving on the page no longer drops it. A satellite the brain doesn't let in is refused on saving, as it already is for an alarm. Run, on the page, runs the routine on its own satellite, where it would run at its time.

11. **The longest request (S16).** Today the satellite stops sending after 10 seconds, whatever `thresholds.recording_timeout` says on the brain.
    - The brain now sends each satellite its limit with its volumes and mute, as it connects and whenever the limit changes.
    - The satellite uses it, up to a most of its own of 30 seconds. So a brain that never says the user has stopped still can't keep the microphone open.
    - Until the brain has sent it, the satellite uses 10 seconds, as now.
    - The setting goes on the page, under Listening, as "Longest request, in seconds", from 5 to 30, used straight away. Its default goes from 10 to 15 seconds, for 0008's lists. That was the user's answer 1.

12. **The voice's speed (S17), with the user's decision 9.** Settings gain "Speaking speed", a slider from 0.75 to 1.5 times as fast. It's kept in a new `voice` section, as `speed: 1.0`.
    - It's used from the next reply. Piper's waiting process, and the replies Nova keeps ready, are let go, and the next process starts at the new speed.
    - The phrase each satellite says when it can't reach the brain is in Nova's voice, so it's made again at the new speed, and sent.
    - A "Hear it" button beside the slider says one sentence on the satellite being looked after, the way the volume sliders beep.

    Choosing the voice waits for the AI machine, and the `voice` section is where that choice will go. That wait is shaped by hardware (rule 6). Each voice is about 60MB on PVE2's nearly full local-lvm, and the better voices are too slow without a GPU. The AI machine lifts both limits.

13. **What 0008 adds (its decision 15).** There's one new section, Calendars and lists, next to 0007's Batteries. Each part lists what Home Assistant has, or Google for the calendars, found as Nova runs, so a calendar or list added later turns up by itself. Each choice is kept as 1 says, and works straight away. A thing that goes drops off the page, but its choices are kept, in case it comes back. Renaming is done in Home Assistant, or in Google for a calendar.
    - **Calendars.** Each calendar has:
      - "In what's on": whether "what have I got on Thursday?" reads it. All are, to begin with, as now. This replaces `home_assistant.calendars`;
      - "An activity": whether it counts for "when's work tomorrow?" (0008's decision 11). All but Google's primary calendar do, to begin with. An activity's calendar is still read for that activity when it's out of "what's on".

      The section also says which calendar new events go to, chosen from those Google says Nova may add to. To begin with, it's Google's primary calendar, "Nathaniel" today. It also says whether Nova is signed in to Google, with "Sign in to Google" or "Sign in again" (8).
    - **Lists.** Each list, with how many things are on it. One is the main list, which "add milk to my list" goes to. To begin with, it's todo, found by its name.
    - **Sending to.** Each device Nova can send to, and how it's reached: the phone through ntfy, to the topic under Notifications, and the Mac and tablet through Home Assistant. There's nothing to choose (0008's decision 9).
    - **Weather.** Which weather entity to use, shown only when there's more than one.
    - **PCs.** Which Wake-on-LAN button wakes which PC, shown only when there's more than one of either.

    Each list also shows its other names in Home Assistant, which are the words that reach it besides its name.

14. **On a phone (S13).**
    - With more than one satellite, the masthead names the one being looked after ("Nova · bedroom"). So does the heading of each section that's a satellite's own: Sound, Timers and alarms, and Devices.
    - After a save that needs a restart, the save bar offers "Restart now", where the user's thumb already is, rather than eight sections up.
    - Recent problems shows the latest three, each cut to two lines, with "Show all".
    - Everything new here works at 390 wide, and its buttons are big enough for a thumb: Undo, Stop, and a password's Copy.

**Every setting on the page.** New ones are marked. Keys are still used once Nova restarts.

| Setting | On the page as | Allowed | Used |
| --- | --- | --- | --- |
| `music.player` | What plays the music | navidrome or spotify | Once Nova restarts |
| `navidrome.url`, `home_assistant.url`, `ntfy.url` | Each one's address | An http or https address | Once Nova restarts |
| `navidrome.username` | Navidrome's user for Nova | Text | Once Nova restarts |
| `ntfy.alerts` | Topics read out as they arrive | ntfy topics | Once Nova restarts |
| `ntfy.phone`, `reminders`, `restarts` | As now | An ntfy topic, or nothing | Straight away |
| `ntfy.quiet_from`, `quiet_until` | Night starts and ends at | A time, 00:00 to 23:59 | Straight away |
| `thresholds.silence_duration` | Seconds of quiet which end what you're saying | 0.2 to 3 | Straight away |
| `thresholds.recording_timeout` (new, answer 1) | Longest request, in seconds | 5 to 30 | Straight away |
| `logging.log_transcripts` | Keep what's said in the logs | Yes or no | Straight away |
| `sounds.listening_enabled`, `chime_enabled`, `reminder_enabled` (new) | The three sounds | Yes or no | Straight away |
| `web_search.enabled` (new) | Search the web when asked to | Yes or no | Straight away |
| `conversation.history_max_age` (new) | Seconds a conversation's thread is kept | 0 to 3600 | Straight away |
| `spotify.pause_while_speaking` (new) | Pause Spotify while Nova talks | Yes or no | Straight away |
| `mqtt.host` (new) | Home Assistant's MQTT host | A host name or address, without `http://` | Once Nova restarts |
| `mqtt.port` (new) | Its port | 1 to 65535 | Once Nova restarts |
| `mqtt.username` (new) | Nova's MQTT user | Text | Once Nova restarts |
| `voice.speed` (new) | Speaking speed | 0.75 to 1.5 | Straight away |

**Goals.**
- Everything the user would change is on the page, and most of it works at once, with no restart and no conversation lost.
- Nothing on the page can break Nova, lose an alarm, or get round a restriction.
- A new satellite, calendar, list or device is set up on the page, or by itself - never in a file or a shell.
- The page works one-handed, on a phone.

**Not goals.**
- The listening settings, wake sensitivity and false wakes (the user's decision 7).
- Choosing the voice (the user's decision 9).
- Controls on the page for what 0008 adds by voice: the PC's power, switches, media players, or the things on a list. Home Assistant's dashboard has them (rule 3).
- Keys working without a restart.
- Renaming a satellite, or changing a satellite's own settings from the page.
- What the review said should stay off the page: who's let in and the page's port, the brain's port and model, `llm.*`, `mqtt.name`, the timers' cadences, `SENSITIVE` and the allowlist, and the setup and update scripts.
- Making each session again in place of a restart, for the settings that wait for one. It would be a second way to restart, for settings set once per service.

**What would make it a failure, even working.**
- "Used straight away" said of a change that isn't, or that is on some satellites and not others.
- A change made on the page that stops Nova starting, or an update that does because the install's file has a setting that's gone.
- Anything the page runs, sets or shows getting round a restriction, such as a script that works a lock.
- A satellite's password shown twice, logged, or left in the page's state.
- An alarm lost by removing a satellite, or rung both on the removed Pi and on the default.
- Stop deleting an alarm, or a repeating alarm missing tomorrow.
- Undo putting back something different, or a timer that rings the moment it's back.
- A Spotify or Google sign-in finished from an address the page didn't start, or either token readable by anyone but Nova.
- Replies slower to start after a speed change, other than the first.
- On a phone: not knowing which satellite a change is for, or having to scroll back up to restart.

**Constraints.**
- It runs on the brain (CT 107), apart from three changes on the satellite:
  - its Spotify speaker name, as it connects (7);
  - its recording limit, from the brain (11);
  - `setup.sh --new-password` (6).

  The first two are new optional fields, so an updated brain and a satellite not yet updated still work together, either way round.
- It's built after 0007 and 0008, on `satellite`, like the rest. 0007's Batteries section is the first part built the way decision 1 describes, and decision 13 needs 0008 built.
- The page's security stays as it is: Tailscale's login, the page's own header on every change, nothing cached, and the strict content policy. The only secret it ever shows is a new satellite's password, once. The page's process can already read the passwords' file.
- A restart still costs about half a minute of Nova not hearing, and Claude's conversation. That's why only the settings set once for each service wait for one.
- Batch 1's lock round every save covers the change to the running settings too.
- The repo is public. No password, token, address, or entity id carrying the user's email or the PC's hardware address goes in code, tests, docs or commits.
- Only the voice choice is shaped by hardware (12). Nothing else here is.
- The house style, and the repo's habits: tests first, plain-English commits, nothing outward-facing without the user's OK. The look at 390 and 1440 wide is the user's to check, on their phone and a laptop, once it's deployed.

**Left to the builder.**
- For each setting used straight away, whether its part reads it as it's used or is told after saving. Each needs a test that it takes effect at its next use, with no restart.
- The shared helper for choices kept on the brain, taken from 0007's battery choices, and the files it keeps them in, and their format.
- The exact rule for a satellite's name, checked against what setup.sh and a machine's name allow.
- How the brain sends a removed satellite its empty copy, and moves its timers and alarms. Widening what gives out those from before there were satellites is the likely way. It's for a satellite removed on the page or on the command line.
- How a session's Spotify picks up a sign-in made after the session started, and how the sign-in's state is made and checked with the pinned spotipy.
- Whether each side already ignores a message field it doesn't know, with a test each way.
- How "Hear it" plays, the way the volume beep does.
- The exact words: errors, confirms, and what Nova says, in its usual style.
- How the new parts look, in the page's existing style, at 390 and 1440 wide.

**My calls, not the user's** (for them to check):
- Things are kept in two places: settings in the running settings, and choices about Home Assistant's things in the data folder, like 0007's. Nothing that names an entity goes in `config.yaml`.
- Settings that make a connection or a player wait for a restart. Everything else is used straight away.
- The ranges in the table. Out-of-range values are refused at start, as a wrong kind is, and settings that have left `config.yaml` only warn.
- Stop works on every satellite where something rings or speaks, and leaves the music playing.
- Undo lasts 10 seconds on the page and a minute on the brain. It covers timers and reminders too, and doesn't survive a restart.
- Scripts: a confirm for every one, and no switch to hide any. Scenes stay one tap.
- Satellites:
  - a removed one's timers and alarms move to the default, and its volumes and mute are kept;
  - a lost password gets a new one, rather than removing the satellite and adding it again;
  - renaming stays off the page;
  - one that isn't connected can still be removed, with a plain warning that its copy may ring for a week if it's switched on;
  - a Pi takes a new password with `bash setup.sh --new-password`.
- "Runs on" is only shown with more than one satellite, and Run uses the routine's own satellite.
- The satellite's own most for one recording is 30 seconds.
- Speaking speed goes from 0.75 to 1.5 times as fast, and comes with "Hear it".
- Calendars get "In what's on", as well as "An activity".
- The PC's power, and the things on lists, aren't worked from the page.
