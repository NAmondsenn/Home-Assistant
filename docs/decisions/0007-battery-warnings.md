# 7. Battery levels and warnings, for every device Home Assistant knows has a battery

23 October 2026 · Phase 5 · Agreed with the user, and built

**Context.** Phase 5's next item is battery levels. Asked when they'd last wanted one, the user said: "maybe if im away from my mac and its running something, id wanna see battery". They agreed the real want is to be warned before the Mac dies mid-job, rather than having to remember to ask. Asking should still work, for every device.

Home Assistant (2026.10.2) already has the levels, which is how the user's dashboard shows them. Today there are three battery sensors, all from the Home Assistant app (the `mobile_app` integration):

| Sensor | Device | Maker, model | Also there |
| --- | --- | --- | --- |
| `sensor.s25_ultra_battery_level` | s25-ultra | samsung SM-S938B | `_battery_state` (charging, discharging...), `_charger_type` |
| `sensor.tab_s11_ultra_battery_level` | tab-s11-ultra | samsung SM-X930 | the same |
| `sensor.macbook_pro_internal_battery_level` | macbook-pro | Apple Mac16,8 | `_internal_battery_state` ("Not Charging" when looked at) |

The tablet's level was last changed three days before, so a reading can be old. `person.nathaniel` says whether the user is home, from those three devices. Nova's ntfy already keeps quiet hours, 23:00 to 07:00 (`ntfy.quiet_from` and `ntfy.quiet_until`).

The user set three rules for this and for everything after it:
- it must work by itself for devices they add later: "i want everything to automatically work with stuff i add in the future";
- what they'd want to change later belongs on the settings page, "rather than messing about with a bunch of code";
- if Nova can get something or do something through Home Assistant, it should, unless that's much slower.

**Decision.**

1. **Home Assistant is the only source.** The brain finds every sensor with the `battery` device class, as it runs, and reads its level from there. Where Home Assistant has it, it also reads whether the device is charging, from the same device's charging state. There's no list of devices in Nova's settings. A device added to Home Assistant later is picked up by itself, at the next check.
2. **Each device is a phone or tablet, or something else,** worked out from what its device in Home Assistant says about itself. Android phones and tablets, iPhones and iPads are mobile. Everything else - a Mac or another laptop now, a remote or a sensor later - is "something else". A device whose kind can't be worked out is "something else".
3. **The warnings.** Mobile devices are warned about once, at 5%. Everything else is warned about at 15% and again at 5%. A warning is given once as the level falls to each mark - at the mark itself, so a mark of 15 warns at 15% - while the device isn't charging, and never repeated for that mark. The marks start over only once the device has been charging, or once its level is 10 or more points above its highest mark, whatever its charging state says - a level wobbling between 15% and 16% on battery never warns twice. The second rule is there because the Mac's charging state can't be relied on: Home Assistant's history for 13-23 October shows it saying "Not Charging" throughout, through falls and through a charge to about 70%. It covers a remote or sensor with no charging state too, when it gets a new battery. A device already below a mark when it's first seen, or unplugged below one, is warned about once, for the lowest mark it's under. Which marks have warned is kept on the brain, so a restart neither repeats a warning nor loses one.
4. **Where a warning goes.** Every warning goes to the user's phone, through ntfy, to `ntfy.phone` - the one place it always reaches them, even in another room from the satellite. As well, with the user home and outside quiet hours, Nova says it out loud on the default satellite, through the brain's announcements - e.g. "Your MacBook Pro's battery is down to 15%." Home's from Home Assistant's person, found as it runs like everything else, never named in Nova's settings: today there's one, `person.nathaniel`. With more than one person, it's the one whose trackers include the device that's low, else the first; the page shows which. The quiet hours are ntfy's. If Home Assistant can't say whether the user's home, or the default satellite isn't connected, it's only sent. The exception is the phone's own warning: there's no point sending that to the phone that's running out, which warns the user itself. So it's only said out loud, home and outside quiet hours, and otherwise left to the phone. If ntfy isn't set up, or a send to the phone fails, the warning is said out loud - but never in quiet hours: then it's held until they end, and said then if it's still true (the device still under that mark and not charging). Apart from the phone's own, a warning is never dropped silently.
5. **Asking.** "How's my Mac's battery?", "what's my phone on?", "battery levels" and the like get the level and whether it's charging, for one device or all of them, straight from Home Assistant. A reading more than an hour old says how old it is - "88%, as of three days ago". The usual phrasings are local commands, answered without Claude. Anything else reaches Claude, through a tool which lists the batteries. A device whose warnings are off can still be asked about.
6. **The settings page gets a Batteries section.** It lists every device Home Assistant has a battery for, found by itself, each with its name, level, whether it's charging, and how old the reading is if it's stale. Each has a switch for its warnings, a switch for whether its warnings are sent to the phone, and its marks: one for a mobile device, two for anything else, each changeable. Sending to the phone is on by default, except for the phone itself, worked out from what Home Assistant says about the device, so a new phone or tablet sorts itself out; a wrong guess is put right with the switch. One more switch, for the whole section, is "Also send to my phone when Nova says it", on by default, for if the double-up gets annoying. A device never changed follows the defaults for its kind. A change is kept for that device on the brain, across restarts, and works straight away. A device which disappears from Home Assistant drops off the list, but its settings are kept, in case it comes back.
7. **Defaults** go in the repository's `config.yaml`, in a new `batteries` section: the marks for each kind (`[5]` for mobile, `[15, 5]` for the rest), how often to check (a minute), and when a reading counts as old (an hour). The page holds the per-device choices, which aren't kept in `config.yaml` at all.

**Goals.**
- The user hears in time to plug the Mac in, wherever they are, without asking.
- Every battery device in Home Assistant works, now and later, with nothing to set up.
- Everything changeable is changed on the settings page.

**Not goals.**
- Saying anything when a device goes offline or dies.
- Battery history, charts or forecasts. Home Assistant has the history.
- Warnings for devices Home Assistant doesn't know about.

**What would make it a failure, even working.**
- Nagging: the same warning twice for one fall, or warnings for a device that's charging.
- Waking the user at night with a spoken warning.
- A warning lost silently - neither said nor sent - other than the phone's own, which the phone gives itself.
- Having to edit a file, or ask for a change, when a new device arrives.

**Constraints.**
- It all runs on the brain (CT 107), which talks to Home Assistant for every satellite. Nothing is added to the satellite.
- Nova's Home Assistant token is an administrator's, so templates work. Finding devices and their makers needs the template API or the WebSocket API. A check costs one request a minute on the local network.
- One user, one Nova satellite for now. Warnings go to the default satellite, the first one added.
- Today the settings page restarts the brain for every saved setting, which leaves Nova deaf for about half a minute and loses Claude's conversation. The Batteries section's choices must work straight away, with no restart. Build that in a way later sections can reuse: the settings review (23 Oct) found several settings which should apply live too.
- The house style, and the repo's habits: tests first, plain-English commits, nothing outward-facing without the user's OK.

**Left to the builder.**
- How a device's kind is worked out from Home Assistant's device information - its maker and model, the operating system the Home Assistant app reports, or both - so the rule in 2 holds for Apple, Samsung and other Android makers. It needs a test for each of today's three devices.
- How the phone itself is told apart from a tablet, for the default of 4's exception - for example, iPhone against iPad from the model, and Samsung's SM-S/SM-G/SM-A/SM-F phones against its SM-T/SM-X/SM-P tablets. With no way to tell, a mobile device counts as a tablet, so its warnings are sent; the page's switch puts a wrong guess right.
- How charging is read for each kind of device: the Android app's `_battery_state`, and the Mac's `_internal_battery_state`. Check Home Assistant's history for what "Not Charging" means - plugged in but holding, or on battery. Then pair the charging sensor with its battery sensor through the device, not through the entity's name.
- Where per-device choices are kept in the data folder, and their format.
- The words Nova says, in its usual style.
- How the Batteries section looks, in the page's existing style, at 390 and 1440 wide.

**My calls, not the user's** (for them to check):
- Something whose kind can't be worked out gets "something else"'s marks.
- A reading counts as old after an hour.
- With Home Assistant unsure where the user is, or the satellite away, the warning goes to the phone.
- With no ntfy, or a failed send, it's said out loud - held until quiet hours end, and said then only if it's still true.
- Marks start over only after charging, so a wobble never warns twice; a device first seen below a mark gets one warning, for the lowest. Which marks have warned survives restarts.
- The check runs every minute.
- Per-device choices live in the brain's data folder, not in `config.yaml`.
- It's built on `satellite`, which is what's live, after the fixes found by the review of 23 October - not on a branch of its own.
