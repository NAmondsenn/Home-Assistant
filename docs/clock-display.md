# Setting up the clock display

A small touchscreen clock that sits with a satellite and shows Nova's face: the time and weather, what's coming up in your calendar, and what's playing. Hold a finger on it to talk to Nova, swipe or tap to change page, and use its volume buttons to change Nova's volume. The satellite still does all the listening and speaking; the clock only shows pages and sends touches and button presses to the brain. The reasons for each choice are in [decisions/0013](decisions/0013-the-clock-display.md).

The brain's side is built and tested, and the page checked in a desktop browser at the clock's size. What's left is the clock itself: it has to be flashed, and then tried by hand (the last section).

## What you need

- A Xiaomi Mi Smart Clock (X04G), flashed - see "Flashing" in [decisions/0013](decisions/0013-the-clock-display.md). Do that first, by hand, with the backup made. This guide starts once the clock boots into the flashed system, on your Wi-Fi.
- [Fully Kiosk Browser](https://www.fully-kiosk.com/) with its PLUS licence (8.90 EUR once, for each device). The page reads the volume buttons and sets the brightness through Fully's JavaScript Interface, which only PLUS has. The free version can keep the screen on and load the page, but not the buttons or the brightness.
- `adb` on a computer on the same network, to install Fully without a Google account on the clock.

## Steps

1. **Switch the clock server on, on the brain.** On the settings page, in the Settings section, find the "Clock displays" group and set "Port the clock displays connect on" to a free port (`<port>`). Press "Save settings", then "Restart now", which shows once the change is saved: the port is only used once Nova restarts. The same setting in `config.yaml` is `display.port`. At `0`, the default, nothing listens.
   - The port listens on every address of the brain, because the clock is on your Wi-Fi rather than your tailnet. Everything but the page's own files needs the clock's key, but the page is plain HTTP, so the key travels unencrypted over your home Wi-Fi. That's why the port is for the clock's network only: if the brain has a firewall, allow `<port>` from the clock's network alone, and never forward it from your router.
   - If the brain's log says it couldn't start the server, another program has the port. Choose another.
2. **Add the clock, on the settings page.** Under Clock displays, type a name (e.g. Desk), choose the satellite it "Hears through" (the default is fine for one satellite), and press Add a clock. The page shows the clock's key once, with the address to open. Keep it until step 5: it isn't shown again, but "New key" on the clock's row makes another, and the old one stops working at once.
3. **Install Fully on the clock.** Turn on Developer options on the clock (tap the build number in About several times), then USB debugging and "ADB over network", which listens on the fixed port 5555. Not Android 11's "Wireless debugging", which pairs with a code and uses another port. From your computer:

   ```bash
   adb connect <clock>:5555
   adb install <the Fully Kiosk APK>
   ```

   Open Fully on the clock and enter the PLUS licence. If Fully asks for permission to draw over other apps, and only then, give it that from the computer:

   ```bash
   adb shell pm grant de.ozerov.fully android.permission.SYSTEM_ALERT_WINDOW
   ```

   (On Android Go devices Fully's Kiosk Mode and overlays may not work. The page needs neither.)
4. **Set Fully up.** In Fully's settings:
   - Web Content Settings: Start URL `http://<brain's address>:<port>/display#key=<key>`, with the brain's address on your Wi-Fi and the key from step 2.
   - Advanced Web Settings: turn on "Enable JavaScript Interface", and set the URL whitelist to the pattern `http://<brain's address>:<port>/*` (the brain's page alone, not the start URL with its `#key=`), so no other page Fully ever loads can use the interface. (Fully's menus move between versions - its settings search finds both.)
   - Device or Display settings: turn on "Keep Screen On". The page sets the brightness itself, from the slider on the settings page.
5. **Open it.** Load the start URL. The page keeps the key for next time and takes it off the address, so a reload from Fully's own start page still works. You should see the time within a second or two.
6. **Check the settings page.** Under Clock displays, the clock's row has Brightness (the clock follows the slider within a second), "Back to Home after, in seconds" (how long after music stops the clock returns to the first page), "Hears through", New key and Remove.

## When the clock shows a message

- "This clock has no key - add it on Nova's settings page": the address had no `#key=...` and none was kept. Open the start URL from step 4 again.
- "Nova didn't accept this clock's key": the key is wrong, or the clock was removed or given a new key on the settings page. The page tries again every minute; put the new address in Fully's Start URL. After 5 wrong keys in a row, that address is refused for a minute, even with the right one.
- A small "Offline" mark: the brain can't be reached. The clock keeps showing the time from its own clock and reconnects by itself. The pages can still be swiped and tapped through meanwhile, but a hold and the volume buttons do nothing until Nova is back.

## What to check by hand on the clock

Only the clock itself can show these, so they're left for when it's flashed and set up:

- Each page - Home, Coming up, Now playing - fits the screen, at 800 wide, with nothing cut off.
- A swipe left or right changes page, and a tap moves to the next. Neither makes Nova listen.
- Quick taps, one after another and at the very edge of the screen too, never show the listening orb.
- Holding a finger makes Nova listen through the satellite's microphone (the orb and "Listening…" show), and lifting it ends the recording. Also while Nova is muted: the page then shows a "Muted" mark when it isn't being held.
- The volume buttons change Nova's volume, a step at a time: the music's while Nova's own music plays on that satellite, otherwise its voice, and never the alarms'. If they do nothing, look in the page's console (Fully's web content debugging lets Chrome on a computer show it) for "Blocked by the page's policy" (a "securitypolicyviolation"): Fully runs the buttons' handlers as text, which the page's Content-Security-Policy may turn down on this build. Whether a press also changes the clock's own volume doesn't matter, as its speaker isn't used.
- Brightness follows the slider on the settings page.
- Music started on the satellite takes the clock to Now playing, and, once it stops, back to Home after the delay set.
- The sound wave shows while Nova speaks.
- Stop Nova on the brain: the "Offline" mark appears, the time carries on, the pages still swipe while a hold and the volume buttons wait, and the page comes back by itself when Nova does.
