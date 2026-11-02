# 0011: The voice check

**Context.** Nova woke about 12 times a day with nobody talking to it, mostly from films and videos on the PC. The wake word's own confidence can't tell those from the user: in the week to 30 Oct, false wakes scored a median of 0.67 and real ones 0.75, overlapping all the way. Only the user uses Nova, so a check that it's their voice is enough. On 31 Oct the user recorded 22 "Hey Nova", a minute of talking and 5 minutes of Family Guy, and the satellite then kept 3 seconds around each wake until 2 Nov: 84 clips, labelled by what the brain did after each, as 28 of the user's requests, 47 false wakes and 9 unclear ones left out.

A trial on the Pi trained openWakeWord's verifier (a logistic regression on the wake model's own features) 20 times, each time holding back a quarter of the real clips. Looking only at moments the wake model scored 0.5 or more, it kept 140 of 140 held-back requests at 0.2 and stopped 91% of false wakes; at 0.5, 95% and 94%.

**Decision.**

1. **Both have to pass.** A wake needs the wake model's score at or above the wake setting, as now (0.5, or 0.75 while something plays), and the voice check's at or above its own setting, on the same moment. openWakeWord's built-in verifier replaces the wake score with its own, which would have rejected 6 of the user's 22 recordings at 0.75, so it isn't used: the satellite runs the check itself.
2. **The check is plain numbers, not a pickle.** Training saves the scaler's means and scales and the regression's weights and bias as JSON, and the satellite works it out with numpy. Nothing loads code from a file.
3. **It's the user's voice, so it stays on the Pi.** The trained check is kept in the satellite's data folder (`~/.local/share/nova/voice_check.json`, 0600), never in the repo, which is public. Without the file, Nova wakes on the wake word alone, as before.
4. **A setting on the page.** "How sure Nova must be it's your voice" under Listening, `thresholds.voice_check`, 0.2, used straight away; 0 switches the check off. The brain sends it in the satellite's state message, as it does the wake setting, and the satellite keeps its copy, so a 0 lasts through a restart while the brain is away.
5. **Logged, not spoken.** A wake the check stops is logged on the satellite at INFO with both scores, and nothing is played or sent.
6. **Training is a script, run once on the Pi.** `scripts/train_voice_check.py` takes the recordings, the clips and a labels file, trains, says how it does on held-back clips, and writes the JSON, keeping the last one as `voice_check.json.previous`. A check which keeps under 90% of the held-back requests at 0.2 isn't saved unless `--force` is given. After training, the recordings and the clips are deleted and `wake_clips.keep` goes back off.
7. **Not over Nova's own sound.** While the satellite is playing anything of its own - an alarm, a timer, a reminder, a reply, music - or did in the last 2.5 seconds, the wake word alone counts. The user's voice then comes mixed with the speaker's, which the check never learnt, and the wake word is how they stop it: with the brain away, the only way to dismiss an alarm. A film can't make Nova ring, so a false wake then only stops what was already sounding.

**New voices.** It learns the user only. Anyone else saying "Hey Nova" is likely to be stopped; setting it to 0 lets them through. Retraining means recording again with `scripts/record_wake_samples.py`.

**Hardware.** None. The check adds a few multiplications per 80 ms on the Pi.
