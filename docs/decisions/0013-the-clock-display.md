# 0013: The clock display

**Context.** The user has a Xiaomi Mi Smart Clock (global model X04G): a 4-inch 800 x 480 touchscreen, three buttons on top (volume up, volume down, mic mute), a speaker and two microphones, on a MediaTek MT8167 with 1 GB of RAM, 2.4 GHz Wi-Fi only. It runs Google Assistant. He wants it to sit with the Pi satellite, perhaps with the Pi mounted on its back, as Nova's face and controls.

The Pi stays the satellite: it keeps listening, the wake word, the voice check and speaking. The clock only shows pages and sends touches and button presses to the brain. Its own speaker and microphones are not used, so Nova never speaks from two places and the voice check keeps the microphone it was tuned on.

Google's software lets nothing else read the buttons, and Home Assistant Cast is not known to work on this model, so the clock is flashed to LineageOS 20 (Android 13) by the community route (github.com/octathorp/x04g_tools and the Home Assistant forum thread "Hack Mi Smart Clock to display lovelace ui"). Its stock firmware was backed up in full on 9 Nov 2026, before anything was changed. After flashing, the mute button acts as the power button and could not be remapped by others, so it is not used; the volume buttons reach apps as ordinary key presses.

What the user wants, in his words: "the display be like the date / time and weather, then a page for my upcoming events and just stuff like that, what im playing on navidrome / jellyfin", "make the volume buttons actually change novas volume", and, of the touchscreen, "swiping for pages is good. hold to talk tap for pages is fine too". When Nova is talking "there should be like the sound wave speaking animation".

**Decision.**

1. **Three pages, dark, at the clock's own size (800 x 480).** Dots along the bottom show which page is open. The mockup he approved, `0013-clock-display-mockup.html` beside this file (shown at half size, placeholder content), is the reference for layout.
   - **Home:** the time (large), the date, today's weather (temperature, condition, high and low) and the next event, one line.
   - **Coming up:** the next five or so events, each with its day and time (just the day for all-day events, the date for ones beyond this week).
   - **Now playing:** album art, the source and room ("Navidrome · Office"), title, artist and album, and a progress bar with times.
   - The time follows the brain's clock and timezone, 24-hour.

2. **Where each page's information comes from, all through what Nova already has.**
   - Weather: Home Assistant's weather, through `weather.py` (decision 0008).
   - Events: Nova's own Google calendars, through `google_calendar.py`, honouring the calendars chosen out on the settings page. A calendar named by an email address may show its events but never its name.
   - Now playing: first the music Nova itself is playing on this clock's satellite (Navidrome, or Spotify on that satellite's speaker), from Nova's player. When Nova plays nothing, whatever Jellyfin is playing on any of Home Assistant's media players, found at run time from Home Assistant, never a list kept by hand (decision 0008, and the user's rule that new devices turn up by themselves). If several Jellyfin players are playing, the builder picks a sensible one, preferring this satellite's room.
   - With nothing playing, the Now playing page says so plainly.

3. **Changing pages by touch.** Swipe left or right to move between pages. A tap moves to the next page. The middle (mute) button is left alone as the clock's power button.

4. **Hold to talk.** Holding a finger on the screen makes Nova listen straight away on this clock's satellite, as if it had heard the wake word, through the Pi's microphone. The brain already sends a satellite `listen` (`protocol.py`), so the clock asks the brain and the brain tells the satellite. Lifting the finger ends the recording, as a pushed-to-talk request should; if the user stops speaking before lifting, Nova's usual end-of-speech detection may end it first. A hold has to be long enough not to be confused with a tap or the start of a swipe.
   - A hold works while the satellite is muted, too: muting stops the wake word, not a deliberate hold (the user's answer, 9 Nov: "when nova muted, the mic should still work"). The builder makes sure the satellite records for a hold even while muted, and the clock shows that Nova is muted when it isn't being held.

5. **Volume buttons.** Up and down change one of Nova's volumes on this clock's satellite, a step at a time like "louder" and "quieter" by voice:
   - the music volume while Nova's own music is playing there,
   - otherwise Nova's voice (`general`),
   - never the alarm volume, so timers and alarms stay loud.
   A small level bar shows briefly on the screen when the volume changes (builder's call). Changes are saved as voice changes are (`volume.py`).

6. **What the screen does on its own.**
   - When music starts on this satellite (or Jellyfin starts, under 2), the clock jumps to Now playing.
   - When it stops, the clock goes back to Home after a delay, set on the settings page; 1 minute by default (builder's call, open to the user).
   - While Nova is listening, a glowing orb covers the page with "Listening..." and, as it is heard, what Nova heard.
   - While Nova is speaking, a sound-wave animation covers the page. It follows the speech: whether it moves with the real loudness of the voice or is an animation timed to the speech is the builder's call.
   - Both overlays go once Nova is done, back to the page that was open.
   - Night mode is not wanted.

7. **The settings page, not files** (decision 0009). There is one clock today, but the section is a list, one entry per clock, so a second clock is added there rather than in code (builder's call). Each entry holds its own credential (9) and:
   - which satellite the clock belongs to (only the Pi today, but chosen, not assumed; a satellite may have more than one clock),
   - brightness, a slider, applied to the clock straight away,
   - the delay before going back to Home,
   - anything the builder adds that the user might change later.

8. **Served by the brain, shown by a kiosk app.** The brain serves the display page and pushes changes to it as they happen (listening, speaking, music starting), so the overlays appear without polling delay. The clock runs a full-screen kiosk app that keeps the screen on, loads that page, sets the brightness and passes the volume keys to the brain. Fully Kiosk Browser is the first choice. The user has agreed to buy its paid licence (PLUS, per device) if the free version can't do these; only if even that can't would a minimal WebView app of the project's own do it instead, built on the Mac, since an Android toolchain is too heavy for CT 109. Finding out which is the builder's first step on the flashed clock.

   From Fully Kiosk's documentation (9 Nov): its JavaScript interface, which needs PLUS (8.90 EUR one-off per device), gives the page `fully.bind('volumeUp', ...)` and `fully.bind('volumeDown', ...)` and `fully.setScreenBrightness(level)`, 0 to 255; "keep screen on" and settings import from `fully-once-settings.json` are free. On Android Go devices from Android 10 its Kiosk Mode and overlays may not work; the page doesn't need them. Whether a volume press also changes the clock's own volume is to be seen on the clock (it doesn't matter, since the clock's speaker is unused).

   Once the clock is flashed, the builder may reach it over the network with adb from CT 109 (the user's yes, 9 Nov) to install and set up the kiosk app and test the page; changes are still made one step at a time and said to him.

9. **Only the clock can drive Nova from the display.** Hold to talk and the volume buttons control a satellite, so whatever the clock connects to needs a credential made on the settings page, like a satellite's, never an open endpoint on the network. The page itself shows the user's calendar, so it is guarded the same way.

10. **When the brain is away.** The clock keeps showing the time from its own clock, with a small mark that Nova is offline, and reconnects by itself. Hold to talk and the volume buttons do nothing until it is back; swiping and tapping between pages still work, since they need nothing from the brain (a ruling made while building, 9 Nov).

**Flashing (done by the user, by hand, before building on the clock).**
- The backup is in a folder on his Mac, copied to the NAS. It is the only way back to stock.
- Unlock with mtkclient, then flash LineageOS's `boot`, `super` and `vbmeta` images with fastboot, as the x04g_tools guide says.
- The images (researched 9 Nov): every copy traces back to one person, octathorp, who wrote x04g_tools and the XDA guide. His `LineageOS20_X04G.zip` (AndroidFileHost, mirrored byte for byte on archive.org as `lineage-os-20-x-04-g`; MD5 `b0fa9c7eccaeb657d839e76b1038eb43`) holds the stock `boot` with the permissive cmdline, a `super` made of AndyYan's LineageOS 20 GSI (`lineage-20.0-20221111-UNOFFICIAL-a64_bvS-vndklite`, official on SourceForge, MD5 `4eda8934e440f7e76a6d1d0e32eac11c`) and his own modified `vendor`, and `vbmeta`. The `vendor` changes (audio, boot and adb fixes, mute mapped to power) aren't published, and a GSI on the stock `vendor` is reported not to boot, so his zip is needed. Before unlocking, on a machine other than CT 109: check the zip's MD5, unpack his `super` and compare its system with the official GSI, compare his `vendor` with the one in our own backup's `super.bin` (expect only audio policy files and key layouts), and his `boot` with ours (expect only the cmdline). `vbmeta` can be mtkclient's own `vbmeta.img.empty` instead; some users had to write it with `mtk w vbmeta` and flash `boot` last.
- The clock's microphone is reported quiet or crashing on this build. Nova doesn't use it, which is another reason the Pi stays the ears.
- First boot on the clock's own power plug, not a computer, since these clocks bootloop on weak power.
- Wi-Fi: 2.4 GHz, plain WPA2.
- This build runs with SELinux permissive. Acceptable for a screen on the home network; noted so it isn't forgotten.

**Order of the build** (builder's call). The brain's side comes first, since it needs no clock: the display page, pushing changes to it, the credential, the settings section, hold to talk and the volume keys, all tested against a fake clock and checked in a desktop browser at 800 x 480. Trying the kiosk app on the clock (8) waits until it is flashed, and then the page is checked on it by hand.

**Not part of this.**
- Running any of Nova on the clock, or using its speaker or microphones.
- The middle button.
- Night mode or dimming by time.
- Timers, alarms, lights or other pages; they can be added later as pages.
- Controlling playback from the screen (pause, skip).
- Home Assistant dashboards on the clock.

**Must not happen.**
- Nova listening without a deliberate hold: a tap or a swipe must never start it.
- The clock's speaker or microphones used for Nova.
- An unauthenticated way to make Nova listen or change its volume.
- The alarm volume changed by the buttons.
- A list of devices or media players kept by hand.
- Anything in the repository naming the user's hosts, holding a key, or showing his calendar.

**Left to the builder.**
- Fully Kiosk free, Fully Kiosk PLUS, or the project's own small app (8), after trying Fully Kiosk on the clock.
- How the clock connects to the brain (a WebSocket from the page, the satellite link, or something else) and how its credential is made and kept.
- The exact hold length, swipe distance and animations.
- How the sound wave follows the speech.
- How Jellyfin players are recognised in Home Assistant.
- Exact wording and the look beyond the approved mockup.

**Tests** fake the clock's connection, Home Assistant, the calendar and Nova's player, as they fake Home Assistant now: page changes on music starting and stopping, hold to talk sending `listen` and only after a hold, the buttons changing the right volume and never the alarm's, and refusal without the credential. The page itself is checked by hand on the clock.

**Hardware.** Shaped by the clock: 1 GB of RAM and a slow quad-core chip, so the page stays light - no heavy framework, no video, CSS animations, small album art. On the brain it adds one small page and a connection per clock, nothing that needs the GPU pve2 doesn't have.
