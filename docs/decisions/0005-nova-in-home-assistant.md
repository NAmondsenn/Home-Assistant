# 5. Nova as a device in Home Assistant, which decides its room and does the controlling

21 October 2026 · Phase 5 · Agreed with the user, not yet built

**Context.** Phase 5 starts with the lights. This morning "lights on" got "Sorry, I couldn't understand that" from Home Assistant, while "turn on my bedroom lights" worked. A look at Home Assistant (2026.10.2) found two lights, Lamp and Bedroom Light, both TP-Link, both full colour and warm-to-cool white (2500-6500K), in an area called Bedroom. Assist, Home Assistant's own understanding of commands, already gets "lamp off" and "main light on" ("main light" is Bedroom Light's other name). "Lights on" fails only because Assist isn't told where it was said. Told it came from a device in the Bedroom, Assist chose exactly those two lights. It doesn't understand "dim the lights" or "turn everything off" at all.

The user wants Nova to know which room it's in, the way everything in Home Assistant has an area, so "lights on" means that room's lights. There's one Nova, in the bedroom, for now, and maybe more one day. They want Nova "pretty integrated" with Home Assistant, which is "a big part of everything": a device of its own there, with useful controls like everything else, and Home Assistant doing the controlling.

**Decision.**

1. Nova joins Home Assistant as a device, through MQTT discovery. The Windows PC already does it this way, through HASS.Agent and the Mosquitto add-on. Nova's area is set in Home Assistant like any device's, and that's the only place its room is kept.
2. Every request Nova passes to Assist says which device it came from, so Home Assistant's areas decide what "lights on" means. Moving Nova to another area in Home Assistant moves it, and a second Nova is a second device.
3. Home Assistant does the controlling. Assist goes first, as now. What Assist doesn't understand goes to Claude, with a tool which works the lights through Home Assistant's own services: on, off, brightness, colour and warmth, for one light or a whole area. Claude is told which lights are in Nova's area, so "the lights" means that room's for Claude too.
4. The settings page's Devices section gains colour and warmth for each light that can do them.

Nova's device in Home Assistant has:

| Control | Kind | What it does |
| --- | --- | --- |
| Status | sensor | Idle, listening, thinking or speaking |
| Volume, Alarm volume, Music volume | number, as a slider | Nova's three volumes, as on the settings page |
| Next alarm | sensor, a time | When the next alarm rings, if one's set |
| Timers | sensor | How many are running, with each one's name and end time |
| Stop | button | What saying "Hey Nova, stop" does: silences anything ringing, stops Nova talking |
| Restart | button | Restarts Nova, as the settings page does |
| Speak | notify | Says the message out loud, for automations - "the washing's done" |
| Backend | binary sensor, connectivity | Whether Nova is reaching the backend on PVE2 |
| Microphone muted | switch | Stops Nova listening for "Hey Nova". Alarms and timers still ring |

The whole device shows as unavailable while Nova isn't running. Nothing anyone says to Nova, and none of its answers, goes to Home Assistant.

**How it's put together.** These are Claude's calls, not the user's, and the builder can change them if something better turns up:

- **The MQTT library.** paho-mqtt, pinned in `requirements.lock` - the one new dependency.
- **Logging in to the broker.** Nova logs in to the Mosquitto add-on as a Home Assistant user of its own, not an admin, with its password as `MQTT_PASSWORD` in `~/.config/nova/.env`. A new `mqtt:` section in `config.yaml` holds the broker's address, port and username. Left empty, MQTT is off and Nova works as it does today.
- **Nova's device id.** Nova looks up its own device id in Home Assistant once the device exists, and sends it with every request to Assist. Until it has one, requests go as they do now.
- **The broker's address.** Home Assistant's own address on the home network, on MQTT's usual port, 1883, as HASS.Agent uses. It isn't reached through Home Assistant's web address, which only passes on web requests.
- **When Assist hasn't understood.** That's Assist answering with an error that it matched nothing, or found nothing to act on. That request then goes to Claude, as if nothing had matched it on the Pi. Assist being unreachable isn't the same thing: that's still said as it is now.
- **Claude's light tool.** It calls `light.turn_on` and `light.turn_off` with a light or an area, and a brightness, a colour by name or by hue, or a warmth in kelvin. It works lights and nothing else. It takes the place of Claude's `control_home`, which only passed words on to Assist. The everyday phrases still go to Assist first, without Claude. The lights - each one's area, other names, and whether it's on, how bright and what colour - go with each request, beside the timers already sent there, so the instructions stay cached. Claude can then answer "is the lamp on?" itself.
- **Nova without an area.** Until Nova has an area, requests go to Assist as they do now, and Claude is told Nova's room isn't known, so it asks which room rather than switching on every light.
- **Stop and Speak.** These run the way the settings page's own buttons do. Speak waits for a conversation to finish, as announcements already do. It isn't held back at night as ntfy's alerts are: an automation decides when it speaks.
- **Finding its own device.** Nova finds its device id through Home Assistant's REST API, by its MQTT identifiers rather than an entity's name, which the user can change. It looks again now and then, so moving Nova to another area takes effect without a restart.
- **Mute.** A muted microphone stays muted across a restart, and the settings page shows and changes it too.
- **The Devices section.** Lights are grouped by area, with Nova's own first. Each light that has colour gets a choice of colour and a warm-to-cool slider, built without outside libraries, since the page's security policy blocks them.
- **More than one Nova.** Each Nova's MQTT ids come from a name in its config, `Nova` unless changed, plus something unique to the machine. So a second Nova becomes a second device rather than taking over the first.

**What the user does once.** Make a Home Assistant user for Nova (or let Claude make one), and put the new Nova device in the Bedroom area.

**Not part of this.** Making Nova an Assist satellite (Wyoming or ESPHome) - [0004](0004-pve2-backend.md) explains why not. Nova's music as a media player in Home Assistant. What's said, in Home Assistant. Phase 5's other items - the PC on and off, battery levels, and fewer false wakes - are planned separately.

**Must never happen.** "Lights on" switching on lights in another room. The lights by voice breaking when MQTT or the broker is down - Assist works without them. Locks, garage doors and doors worked by voice - still refused. Passwords, or the user's addresses, in the repository.

**Done when.** Nova shows in Home Assistant's devices, in the Bedroom, with every control above working from Home Assistant. "Lights on", "lamp off", "main light on" and "dim the lights" do the right thing by voice. The settings page sets colour and warmth. Tests cover each part, and it's been checked on the Pi.
