# Release checklist

Run through this whenever a branch is put on Nova, before relying on it. It takes about 15 minutes. Jot down roughly what time anything odd happens, so it can be matched against the log.

## Putting a branch on Nova

Almost everything below runs on the brain - the hearing, Claude, the voice, music, timers and Home Assistant - and a satellite only hears the wake word and plays what it's sent. So the branch goes on the brain first, then on each satellite.

On the brain, as your own account (`nova`'s home is closed to your account, so every path is a full one):

```bash
sudo -u nova -H git -C /home/nova/Home-Assistant fetch
sudo -u nova -H git -C /home/nova/Home-Assistant switch <branch>
sudo bash /home/nova/Home-Assistant/update.sh
```

Then on each satellite, from its clone:

```bash
git fetch && git switch <branch> && bash update.sh
```

`update.sh` brings the branch up to date, installs the packages (`requirements-backend.txt` on the brain, `requirements.lock` on a satellite), fetches any new models, rewrites the service file and restarts Nova, then shows the end of the log. The brain's should show `Nova's brain is ready` and `Listening for satellites on port 10400`, and a satellite's `Satellite '<name>' ready, for its brain at ...` and `Listening for 'Hey Nova'...`, with no errors above. To go back, switch to the previous branch or tag and run `bash update.sh --no-pull`, which uses the code as it is - a tag can't be pulled.

## Hearing you

1. In one breath: "Hey Nova what time is it". The whole question is answered, with the time said like "8:15 PM".
2. "Hey Nova", wait for the blip, then "what's the date?"
3. Straight after the blip, quickly: "set a timer for two minutes".
4. With a pause mid-sentence: "Hey Nova, remind me in ten minutes... to check the oven". It shouldn't cut you off at the pause.
5. Quietly, from across the room.
6. With a fan, a tap or instrumental music on: it stops listening about a second after you finish.
7. "Hey Nova", then say nothing. After a few seconds there's a falling blip, and it's done.
7a. Ask something with a two-sentence answer ("how far away is the moon?"): it starts speaking noticeably sooner than it used to, and the second sentence follows without a gap.
7b. The moment a reply finishes, "Hey Nova" again: it's heard first time.
7c. Part way through a long answer, "Hey Nova, stop": it stops talking almost at once - in well under a quarter of a second - and says nothing more. "Hey Nova, what's the date?" part way through another one: it stops, and answers that instead.
8. Talk normally nearby for a few minutes without saying "Hey Nova". Note any false wake-ups.

## Music

With Navidrome (`music.player: navidrome`), and again with Spotify if you still use it:

8a. "Hey Nova, play Fleetwood Mac" - "Playing Fleetwood Mac" is said first, then the music starts. "Play the Rumours album", "play my liked songs", "play some jazz".
9. "Hey Nova, pause the music" - chime, and it pauses.
10. While it plays: "Hey Nova, skip this song".
11. While it plays: "Hey Nova, what time is it?" - the music pauses, the answer, the music comes back. Said at your normal volume from where you'd usually be, "Hey Nova" is heard over the music (echo cancelling, Navidrome only).
12. While it plays: "Hey Nova, turn it down" - the music, not Nova, gets quieter.
13. While it plays, let a reminder go off - the music pauses for it.

## Timers, reminders and alarms

14. A one-minute timer; when it rings, "Hey Nova, stop". It stops straight away, not at the end of its sound - and the log says "Dismissed by voice without asking the model", even if it was heard as something like "bus stop" or "soap".
15. "Hey Nova, remind me in two minutes to drink water" - later, "This is a reminder: drink water". Between its rounds, ask something else: the answer ends "Shall I turn it off?" - "Yes, I don't need it any more" turns it off ("not yet" would leave it).
16. "Hey Nova, remind me at" a time a few minutes ahead - it goes off at that minute.
17. An alarm a couple of minutes ahead - it rings in bursts, getting louder. "Hey Nova, stop" in a gap stops it straight away.
18. "Hey Nova, set a reminder", then answer its question without saying "Hey Nova".

## Everything else

19. "Hey Nova, what's the weather?"
19a. "Hey Nova, turn off the kitchen lights" (whatever yours are called in Home Assistant) - chime, and they go off. "Set the living room lights to 30 percent." "Unlock the front door" - it refuses.
19b. "Hey Nova, what have I got on Thursday?" and "am I busy this weekend?" - read from your Google Calendar, with the times. "What have I got on this week?" - an overview without the times, in about 15 seconds. "When's my next shift?" and "when's my MOT?" (or anything else on it). Anything at midnight is said as "midnight", not "12 AM".
19c. "Hey Nova, remember that my favourite colour is green" - "I'll remember that". "What do you know about me?" includes it; "forget my favourite colour" - and it's gone.
19d. With something playing in the satellite's room - a video on the PC, with HASS.Agent's media player on, say - the brain's log says `Something's playing in the <name> satellite's room (...), so its wake word needs 0.75`, and `Nothing's playing in the <name> satellite's room now, so its wake word needs 0.5` once it stops (unless those settings were changed). While it plays, "Hey Nova, what time is it?" is still answered, and "Hey Nova, stop" still stops. If a line from the video wakes it, it's ignored without a word, and the log says `Taken as speech from what's playing, so ignored`.
20. Unplug the microphone for 5 seconds, plug it back in, wait 10, then "Hey Nova" - it works without a restart.
21. Set a one minute timer, then unplug the Pi's network cable and wait 10 seconds. "Hey Nova" - it says "Sorry, I can't reach my server right now", and nothing new can be set, as it understands nothing without the brain. When the timer is due it rings from the Pi's own copy, and "Hey Nova" stops it. Plug the cable back in, and once it has reconnected "Hey Nova, what time is it?" is answered again.
21a. The internet down, but not the network: take the router's connection to the internet out (its WAN cable), leaving the home network up, and wait 10 seconds. "Hey Nova, set a one minute timer" - it's set straight away. "Hey Nova, how far away is the moon?" - straight away, it says it can't reach the internet. The lights ("turn on the lamp") and Navidrome still work, as they're on the home network. Plug the connection back in.
22. `systemctl --user restart voice-assistant` - it stops within a second or two, rather than hanging for 20.
23. `kill -STOP $(systemctl --user show -p MainPID --value voice-assistant)` - within about a minute, systemd restarts it (`journalctl --user-unit voice-assistant` says "Watchdog timeout").

## The manual scripts

`scripts/manual/` has four checks which need real hardware or accounts, so nothing runs them for you. None is needed for every release; run one when what it checks has changed. Each is run from the clone with `venv/bin/python`, and the brain's two as the brain's user (with `runuser -u nova -- ` in front, as the README says).

On a satellite:

- `scripts/manual/check_audio.py` - lists the sound devices, records until you stop talking, saves it as `test_audio.wav` and plays it back, through the same microphone and speaker Nova uses. For a new microphone or speaker, or a new `/etc/asound.conf`.
- `scripts/manual/check_wake_word.py` - counts the wake word's detections until Ctrl+C, and `--sensitivity 0.6`, say, tries another confidence for the run without changing any setting. For tuning `thresholds.wake_word_confidence`.

On the brain:

- `scripts/manual/check_actions.py` - runs sample actions through the ActionExecutor without Claude, and says whether each came back as an ActionResult. Music plays for real if Spotify is signed in.
- `scripts/manual/check_integration.py` - runs a few sample queries through Claude, the actions they ask for and Piper, and says PASS or FAIL for each. It needs the Claude key in `~/.config/nova/.env`, Spotify signed in for the music query, and Piper's models for the speech, which it writes as `test_integration_N.wav` in the folder it's run from. For a change to Claude's tools or the actions.

## Leaving it running

For a soak test, leave it running as normal for a few days, with one monitor on each machine, and use Nova as you would anyway. Neither machine has `tmux`, so each monitor is started detached from the terminal, and goes on after you log out.

Do the brain's own checks first, before the monitors start, so they don't show as restarts or a changed process in the readings. On the brain, as your own account:

- Restart it, and check it comes back as active:

  ```bash
  sudo systemctl restart nova-backend
  sudo systemctl status nova-backend
  ```

- Check the satellite rings from its copy while the brain is stopped. Set a one-minute timer, and about 20 seconds before it's due, stop the brain's process. It runs as `nova`, so it's signalled as `nova`:

  ```bash
  pid=$(systemctl show -p MainPID --value nova-backend)
  sudo -u nova kill -STOP "$pid"
  ```

  The Pi rings it from its copy (its log says "Lost the brain" after 15 seconds of silence). 30 seconds after the stop, let it carry on, in the same shell:

  ```bash
  sudo -u nova kill -CONT "$pid"
  ```

  Never leave it stopped for longer: `nova-backend.service` has `WatchdogSec=60`, so a minute's stop is a restart. Once it carries on, the brain finds the timer due and rings it again, through the Pi - nobody had answered it. One "Hey Nova, stop" ends it everywhere.

Then start the monitors. Each one writes `soak.csv` afresh, so copy an older one away first if you want to keep it.

On the Pi, from the repo:

```bash
nohup venv/bin/python scripts/soak_monitor.py --hours 72 > ~/soak-pi.out 2>&1 &
```

On the brain, as `nova` with its own home and its venv's Python. `nova`'s home is closed to your account, so every path is a full one, and `setsid -f` detaches it from the terminal:

```bash
sudo -u nova -H setsid -f /home/nova/Home-Assistant/venv/bin/python /home/nova/Home-Assistant/scripts/soak_monitor.py --hours 72 --brain < /dev/null > ~/soak-brain.out 2>&1
```

The brain's monitor refuses to start under any other home, since it would find an empty logs folder and report no errors. Neither counts errors logged before it started.

Before leaving them, log out and back in, and check each is still going - `pgrep -af soak_monitor` on the Pi, and on the brain:

```bash
pgrep -u nova -af soak_monitor
sudo -u nova tail -2 /home/nova/.local/share/nova/logs/soak.csv
```

The last line of `soak.csv` is the latest reading, and a new one comes every 10 minutes.

Each monitor samples its service's memory, CPU, open files, restarts, errors and log size every 10 minutes into `soak.csv` in its logs folder (`~/.local/share/nova/logs` on the Pi, `/home/nova/.local/share/nova/logs` on the brain), and says at the end whether anything grew that shouldn't have - in `~/soak-pi.out` or `~/soak-brain.out`, in your own home. Afterwards, the speaker shouldn't have run dry, nor the microphone overflowed - this should find nothing:

```bash
journalctl --user-unit voice-assistant | grep -E "ran dry|overflowed"
```
