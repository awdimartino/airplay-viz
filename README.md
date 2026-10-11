# airplay-viz

A now-playing screen for a Raspberry Pi AirPlay receiver. It shows the album art and track info, plus equalizer bars coloured from the cover, on an HDMI display. It doesn't need a desktop or a browser. A small web page lets you tune it live from your phone.

![screenshot](docs/screenshot.png)

- Album art with title, artist, album and a progress bar
- Equalizer bars coloured to match each album cover
- A blurred copy of the cover as the background, with a crossfade between tracks
- An idle screen when nothing is playing. After a few minutes without music the screen goes black, or optionally the HDMI output switches off.
- A settings page at `http://<pi>:8091/` with presets
- It draws straight to the screen through SDL's KMSDRM driver. On a Pi 5 it uses about 18% of one CPU core.

## How it works

```
iPhone ──AirPlay──► shairport-sync ──► ALSA "airplay" pcm ─┬─► USB DAC ──► amp
                         │                                 └─► snd-aloop loopback
                         │ metadata pipe                              │
                         │ (title/artist/album/cover/progress)        │ cava (raw output, FFT)
                         ▼                                            ▼
                   airplay-viz.py  (pygame on KMSDRM, HDMI display, tty1)
                         │
                         └─► settings page (http://<pi>:8091/)
```

- **shairport-sync** is the AirPlay receiver. Its output goes to an ALSA `multi` device, which sends the audio to both the USB DAC and an `snd-aloop` loopback. The loopback runs on the DAC's clock (`timer_source`), so the two copies don't drift apart.
- **cava** reads the loopback and calculates the bar heights. It runs as a hidden subprocess in raw output mode.
- **airplay-viz.py** reads the bar heights from cava, and the track info and cover art from shairport's metadata pipe. It draws the screen with pygame and uses Pillow for the blur, the rounded corners and the colour picked from the cover.
- **settings.py / webui.py / webui.html** provide the live settings page.
- shairport only sends track info when a track starts. So the display saves the current track to `~/.cache/airplay-viz/` and restores it after a restart, as long as an AirPlay session is still connected.

## Tested setup

- Raspberry Pi 5, Debian 13 (trixie), arm64, booting to the console (no desktop)
- shairport-sync 4.3.7 (Debian package, classic AirPlay)
- USB DAC: KM-HIFI-384KHZ (ALSA card id `KMHIFI384KHZ`, mixer `PCM`)
- 1024×600 HDMI display (the layout scales to the screen size)

## Setup, step by step

These steps assume a Pi running Debian or Raspberry Pi OS, a USB DAC plugged in, and an HDMI screen attached. The commands use the user `argyle`; substitute your own. Machine-specific values you'll need to fill in are marked **(edit)**.

### 1. Packages and groups

```sh
sudo apt update
sudo apt install shairport-sync cava python3-pygame python3-pil fonts-inter alsa-utils
sudo usermod -aG video,render,audio,input argyle    # log out and back in afterwards
```

### 2. Find your DAC's ALSA name and mixer

```sh
aplay -l                     # e.g. "card 2: KMHIFI384KHZ [KM-HIFI-384KHZ], device 0: USB Audio"
amixer -c KMHIFI384KHZ scontrols   # e.g. "Simple mixer control 'PCM',0"
```

Note the card id (the word after `card N:`, here `KMHIFI384KHZ`) and the mixer name (here `PCM`). Every config below uses them.

### 3. Loopback device

The loopback is a virtual sound card that gives cava a copy of the audio.

```sh
sudo cp config/modprobe/snd-aloop.conf /etc/modprobe.d/snd-aloop.conf
sudo cp config/modprobe/modules-load-snd-aloop.conf /etc/modules-load.d/snd-aloop.conf
sudo nano /etc/modprobe.d/snd-aloop.conf     # (edit) timer_source=hw:<your card id>
sudo modprobe snd-aloop
cat /proc/asound/cards                        # a "Loopback" card should appear
```

### 4. ALSA split output

This sends shairport's audio to both the DAC and the loopback.

```sh
sudo cp config/alsa/asound.conf /etc/asound.conf
sudo nano /etc/asound.conf                    # (edit) replace KMHIFI384KHZ with your card id (2 places)
speaker-test -D airplay -c 2 -t sine -l 1     # you should hear a tone through the amp
```

### 5. shairport-sync

```sh
sudo cp /etc/shairport-sync.conf /etc/shairport-sync.conf.orig
sudo cp config/shairport-sync.conf /etc/shairport-sync.conf
sudo nano /etc/shairport-sync.conf            # (edit) name, mixer_control_name, mixer_device
sudo systemctl enable --now shairport-sync
```

What the settings in that file do:
- `name` is the speaker name your phone shows.
- `output_device = "airplay"` sends the audio through the split output from step 4.
- `mixer_control_name` / `mixer_device` let your phone's volume slider control the DAC directly.
- `disable_standby_mode = "always"` keeps the output (and so the loopback) running between songs. Without it, cava gets I/O errors when playback stops.
- The `metadata` block, with `include_cover_art = "yes"`, provides the track info and cover art.
- `audio_backend_latency_offset_in_seconds = -1.0` is optional. It makes play, pause and skip respond about 1 second faster than AirPlay's default of about 2 seconds.

### 6. PipeWire / WirePlumber (only if your system runs it)

Check with `systemctl --user status wireplumber`. If it's running, it takes over the DAC and the loopback, which blocks shairport and cava. Tell it to leave them alone:

```sh
wpctl status                                  # find the DAC and Loopback devices
wpctl inspect <id> | grep device.name         # their device.name values
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cp config/wireplumber/50-airplay-usb-dac.conf ~/.config/wireplumber/wireplumber.conf.d/
nano ~/.config/wireplumber/wireplumber.conf.d/50-airplay-usb-dac.conf   # (edit) the DAC's device.name (the Loopback one is the same on every machine)
systemctl --user restart wireplumber
wpctl status                                  # the DAC and Loopback should no longer be listed
```

### 7. The display program

```sh
mkdir -p ~/airplay-viz
cp airplay-viz.py settings.py webui.py webui.html test-meta.py ~/airplay-viz/
sudo cp systemd/airplay-viz.service /etc/systemd/system/
sudo nano /etc/systemd/system/airplay-viz.service   # (edit) User=, ExecStart path, AIRPLAY_VIZ_NAME
sudo systemctl disable --now getty@tty1             # the screen's login prompt gives way to the display
sudo systemctl daemon-reload
sudo systemctl enable --now airplay-viz
```

### 8. Check it works

```sh
systemctl status airplay-viz shairport-sync         # both "active (running)"
journalctl -u airplay-viz -n 20                     # should end with "settings page on http://0.0.0.0:8091/"
```

- The screen should show "<name> · Ready for AirPlay".
- Play something from your phone to the speaker name from step 5. Within a few seconds the art, track info and moving bars appear.
- Open `http://<pi hostname>:8091/` in a browser to see the settings page.

## Settings page

`http://<pi>:8091/` (or set `AIRPLAY_VIZ_PORT` in the service file; `0` turns it off). Change any field (changed rows are highlighted), then press **Push changes**. Most settings apply instantly. Settings marked "(restarts cava)" briefly restart the audio analysis, a blip of about a second.

| Setting | What it does |
|---|---|
| Bar rise time | How quickly bars jump up to a new level (0 = instant) |
| Bar settle time | How quickly bars fall back down (higher = slower, smoother fall) |
| Smoothing | cava's own smoothing: low = jittery and fast, high = smooth and slow |
| Sensitivity | 0 = automatic (loud passages fill the bar area); otherwise a fixed level, lower for more headroom |
| Bar height | Tallest a bar can grow, as % of the bar area |
| Number of bars, Frame rate | Layout and smoothness of the bars |
| Bar delay | Holds the bars back to line up with what you hear |
| Sleep after | Minutes without music (stopped or paused) before the screen sleeps; 0 = never |
| Sleep mode | `black` keeps the HDMI signal and shows pure black (for screens whose "no signal" page is bright); `off` switches the HDMI output off |

**Presets:** **Save as…** stores the section's current values under a name. **Load** applies one, **Delete** removes it, and **Reset to defaults** goes back to the built-in values.

Values and presets are stored in `~/.config/airplay-viz/settings.json` and `presets.json`. To carry your tuning to a new machine, copy that folder across.

The page has no login. It's meant for a home network only, so don't expose port 8091 to the internet.

### Optional: a friendly name through Traefik

If you run Traefik in Docker on the same machine, it can serve the page as, say, `http://viz.pi/`. The visualizer runs on the host rather than in a container, so Traefik needs its file provider with a route pointing at the host:

1. Create `traefik-dynamic/viz.yml` next to your `docker-compose.yml`:
   ```yaml
   http:
     routers:
       viz:
         rule: Host(`viz.pi`)
         entryPoints: [web]
         service: viz
     services:
       viz:
         loadBalancer:
           servers:
             - url: http://172.20.0.1:8091   # (edit) the gateway of Traefik's Docker network
   ```
   Find the gateway with `docker network inspect <network> --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'`. Check that Traefik can reach the page: `docker exec <traefik container> wget -qO- http://<gateway>:8091/api/state`.
2. In the Traefik service in `docker-compose.yml`, add to `command:`
   ```yaml
         - "--providers.file.directory=/etc/traefik/dynamic"
         - "--providers.file.watch=true"
   ```
   and to `volumes:`
   ```yaml
         - ./traefik-dynamic:/etc/traefik/dynamic:ro
   ```
3. `docker compose up -d traefik`. This briefly restarts Traefik, so your other Traefik-routed sites are down for a few seconds.
4. The name also has to resolve to the machine, using whatever already handles your other names. The tested setup uses a dnsmasq wildcard (`address=/pi/<pi's IP>`) that Tailscale split DNS forwards `.pi` lookups to, so `viz.pi` needed no DNS change.

## Moving to a new machine

1. Follow the setup steps above. The **(edit)** values are the only machine-specific ones: user, paths, speaker name, DAC card id and mixer, and WirePlumber device names.
2. Copy `~/.config/airplay-viz/` from the old machine to keep your settings and presets.

## Customising the code

| What | Where |
|---|---|
| Idle-screen name | `AIRPLAY_VIZ_NAME` in the service file |
| Adding a new live setting | Add a `Setting(...)` to the `settings.register(...)` call in `airplay-viz.py`, then read it with `settings.get("screen", key)`. The page picks it up automatically. |
| Background blur / brightness | `blurred_background()` |
| How the colour is picked from the cover | `accent_from()` |
| Layout (art size, margins) | `Viz.__init__()` |

## Testing without a phone

```sh
# fake track (any JPEG/PNG cover)
python3 ~/airplay-viz/test-meta.py cover.jpg "Title" "Artist" "Album"
python3 ~/airplay-viz/test-meta.py end

# screenshot of the current frame -> /tmp/airplay-viz.png
kill -USR1 $(systemctl show -p MainPID --value airplay-viz)
```

To get moving bars without a phone, stop shairport-sync and mute the DAC (`amixer -c <card> sset PCM mute`). Then play test noise through the same path with `speaker-test -D airplay -c 2 -r 44100 -t pink`. Unmute and start shairport-sync again afterwards.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| shairport can't open the DAC ("device busy") | PipeWire/WirePlumber has it: see step 6 |
| No bars, or the display keeps restarting with cava errors | `disable_standby_mode = "always"` missing from the shairport config, or the loopback module isn't loaded (`cat /proc/asound/cards`) |
| Track info or art missing | The `metadata` block is missing from the shairport config, or shairport wasn't restarted after adding it |
| Screen stays on the console / blank | `journalctl -u airplay-viz`: often a missing group (`video`, `render`) or `getty@tty1` still enabled |
| Settings page not reachable | Port in use (the log says "settings page disabled"); set `AIRPLAY_VIZ_PORT` to a free port |
| Bright "no signal" screen when asleep | Set **Sleep mode** to `black` on the settings page |

## Limitations

- Classic AirPlay (version 1) only, which is what Debian's shairport-sync package supports.
- Text uses Inter, with DejaVu Sans as a fallback. Install a CJK font and add it as another fallback to display Chinese, Japanese or Korean titles.
- The colour picker uses the cover's most prominent vivid colour. That isn't always the most eye-catching one.

## Experimental: music-reactive iStrip+ lamp (`istrip-lamp` branch)

This branch can also drive an iStrip+ Bluetooth LE lamp, such as the "SSL-xxxxxx" sunset lamps. The lamp takes the album's colour and its brightness pulses with the bass. It goes dark between beats and when nothing is playing. The code is in `lamp.py`. It uses the protocol reverse-engineered in [this Habr article](https://habr.com/en/articles/722412): AES-128-ECB packets written to characteristic `ac52`.

### Lamp setup

1. Install the extra packages and copy `lamp.py` next to the other files:
   ```sh
   sudo apt install python3-bleak python3-cryptography
   cp lamp.py ~/airplay-viz/
   ```
2. Find the lamp's MAC address. With the lamp on and the iStrip+ app closed (the lamp takes only one connection), run `bluetoothctl --timeout 15 scan on` and look for a device named `SSL-…`.
3. In the service file, set `AIRPLAY_VIZ_LAMP=<MAC>`. Optionally set `AIRPLAY_VIZ_LAMP_RELEASE` to the seconds after playback ends before disconnecting, so the iStrip+ app can use the lamp in between. `0`, the default, keeps it connected permanently. The service file on this branch also drops stale Bluetooth links when it starts and stops (`btmgmt`); keep those lines.
4. Optional, for a head start over the Bluetooth lag: in the shairport config, set `audio_backend_buffer_desired_length_in_seconds = 0.35`. Then set **Bar delay** on the settings page to 150 ms so the screen stays in sync. The lamp, which isn't delayed, gets about 150 ms of lead.
5. `sudo systemctl daemon-reload && sudo systemctl restart airplay-viz`. The log should show `lamp: connected`.

### Lamp settings

The settings page gets a **Lamp** section with its own presets:

| Setting | What it does |
|---|---|
| Rise time / Settle time | How quickly it brightens on a beat, and how quickly it dims again |
| Floor / Ceiling brightness | Brightness between beats while music plays, and on the strongest beats. Silence is always off. |
| Beat threshold | How loud, relative to the song's recent peaks, music must be to rise above the floor |
| Punch | Contrast between soft and loud beats (1 = linear) |
| Adapt time | How quickly it rescales to a quieter song or passage |
| Silence hold | How long a silence must last before the lamp turns off |
| When idle / Idle dim level | Nothing playing: off, or dim in the last album colour |
| Colour saturation, R/G/B gamma, R/G/B gain | Colour correction so the LEDs match the screen (cheap LEDs wash colours out towards white/pink). The defaults are tuned by eye for one lamp. |

The lamp needs a decent Bluetooth signal, about −80 dBm or stronger. Weaker than that, connections drop during setup.

## License

MIT. See [LICENSE](LICENSE).
