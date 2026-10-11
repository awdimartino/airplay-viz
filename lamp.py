"""Music-reactive control for iStrip+ Bluetooth LE lamps (e.g. "SSL-xxxxxx"
sunset lamps).

Protocol (reverse-engineered in https://habr.com/en/articles/722412): 16-byte
packets, AES-128-ECB with a fixed key, written without response to
characteristic ac52:

    54 52 00 57 | cmd | group | mode | R G B | brightness 0-100 | speed | 0 0 0 0

cmd 2 = set colour. The lamp takes a single BLE connection. By default we hold
it permanently; set release > 0 to disconnect that many seconds after playback
ends so the iStrip+ app can use the lamp in between.

Behaviour and colour settings are live-adjustable from the settings page
(section "Lamp"; see settings.py).
"""
import asyncio
import colorsys
import math
import threading
import time

from bleak import BleakClient
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import settings
from settings import Setting

KEY = bytes.fromhex("34522A5B7A6E492C08090A9D8D2A23F8")
CHAR = "0000ac52-1212-efde-1523-785fedbeda25"
RATE = 20             # updates per second while playing (BLE interval ~49 ms)
SILENCE = 0.005       # mean level across ALL bars (0-1) below which it is silence
HIT_AVERAGE_S = 0.25  # "recent average" a hit must jump above (time constant)

settings.register("lamp", "Lamp", [
    Setting("mode", "React to", "level", choices=["level", "hits"],
            hint="level: how loud the frequency range is. hits: sudden jumps in it, e.g. kick "
                 "drums, ignoring held notes at the same frequencies."),
    Setting("low_hz", "Lowest frequency", 50, min=20, max=16000, step=5, unit="Hz",
            hint="Bottom of the frequency range the lamp listens to. For kick drums, about 40."),
    Setting("high_hz", "Highest frequency", 700, min=30, max=20000, step=5, unit="Hz",
            hint="Top of the range. For kick drums, about 120. Bass lines reach ~250 Hz, "
                 "snares and vocals go higher."),
    Setting("rise_ms", "Rise time", 160, min=0, max=2000, step=10, unit="ms",
            hint="How quickly it brightens on a beat. 0 = instant."),
    Setting("settle_ms", "Settle time", 1150, min=0, max=5000, step=10, unit="ms",
            hint="How quickly it dims again after a beat."),
    Setting("floor", "Floor brightness", 10, min=0, max=100, step=1, unit="%",
            hint="Brightness between beats while music plays. Silence is always off."),
    Setting("ceiling", "Ceiling brightness", 100, min=1, max=100, step=1, unit="%",
            hint="Brightness on the strongest beats."),
    Setting("gate", "Beat threshold", 20, min=0, max=90, step=1, unit="%",
            hint="How loud, relative to the song's recent peaks, music must be to rise "
                 "above the floor. Higher = only the biggest hits."),
    Setting("curve", "Punch", 1.2, min=0.3, max=4, step=0.1,
            hint="Contrast between soft and loud beats. 1 = linear, higher = punchier."),
    Setting("adapt_s", "Adapt time", 10, min=1, max=60, step=1, unit="s",
            hint="How quickly it rescales to a quieter song or passage."),
    Setting("silence_hold_s", "Silence hold", 1.5, min=0, max=10, step=0.1, unit="s",
            hint="How long a silence must last before the lamp turns off."),
    Setting("idle", "When idle", "off", choices=["off", "dim"], env="AIRPLAY_VIZ_LAMP_IDLE",
            hint="Nothing playing: off, or dim in the last album colour."),
    Setting("idle_level", "Idle dim level", 30, min=1, max=100, step=1, unit="%",
            hint="Brightness when idle is set to dim."),
    Setting("saturation", "Colour saturation", 1.4, min=0.5, max=3, step=0.05,
            hint="Above 1 strips the washed-out white from album colours."),
    Setting("gamma_r", "Red gamma", 1.5, min=0.5, max=4, step=0.05,
            hint="Higher dims in-between red values (purples, pinks, oranges)."),
    Setting("gamma_g", "Green gamma", 2.4, min=0.5, max=4, step=0.05,
            hint="Higher dims in-between green values (oranges, teals)."),
    Setting("gamma_b", "Blue gamma", 2.4, min=0.5, max=4, step=0.05,
            hint="Higher dims in-between blue values (purples, teals)."),
    Setting("gain_r", "Red gain", 1.0, min=0, max=1, step=0.01,
            hint="White balance: turn a channel down if its LED looks too strong."),
    Setting("gain_g", "Green gain", 1.0, min=0, max=1, step=0.01, hint=""),
    Setting("gain_b", "Blue gain", 0.82, min=0, max=1, step=0.01,
            hint="This lamp's blue LED is strong, hence below 1."),
])


def lamp_color(rgb):
    """Map a screen colour to what looks like the same colour on the lamp.

    Cheap RGB LEDs drive each channel linearly, so mid values (e.g. the green
    in orange) look nearly full and colours wash out towards white/pink.
    Boost saturation, apply gamma, then normalise so the brightest channel is
    255 (overall brightness is set separately by the brightness byte).
    """
    st = settings.values("lamp")
    gamma = (st["gamma_r"], st["gamma_g"], st["gamma_b"])
    gain = (st["gain_r"], st["gain_g"], st["gain_b"])
    r, g, b = (c / 255 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.08:                         # neutral/grey art: plain white
        return (255, 255, 255)
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * st["saturation"]), 1.0)
    r, g, b = (c ** gm * gn for c, gm, gn in zip((r, g, b), gamma, gain))
    m = max(r, g, b)
    if m <= 0:                           # every gain at 0
        return (0, 0, 0)
    return tuple(round(255 * c / m) for c in (r, g, b))


def packet(r, g, b, brightness):
    p = bytes([0x54, 0x52, 0x00, 0x57, 0x02, 0x01, 0x00,
               r, g, b, max(0, min(100, brightness)), 100, 0, 0, 0, 0])
    enc = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    return enc.update(p) + enc.finalize()


class Lamp:
    def __init__(self, address, release=0):
        self.address = address
        self.release = release    # seconds idle before disconnecting; 0 = stay connected
        self._lock = threading.Lock()
        self._active = False
        self._color = (255, 255, 255)
        self._energy = 0.0        # held peak since the lamp loop last read it
        self._sound = 0.0
        self._last_active = 0.0
        self._avg = 0.0           # recent average of the band, for hits
        self._prev = time.monotonic()

    @staticmethod
    def _band(n, lo_hz, hi_hz):
        """Indices of the cava bars whose frequency band overlaps lo..hi Hz."""
        edges = settings.bar_edges()
        if len(edges) != n + 1:   # bar count changed and cava not restarted yet
            low, high = settings.bar_range()
            edges = [low * (high / low) ** (i / n) for i in range(n + 1)]
        idx = [i for i in range(n) if edges[i + 1] > lo_hz and edges[i] < hi_hz]
        if not idx:   # range outside the bars: use the closest bar
            mid = math.log(math.sqrt(lo_hz * hi_hz))
            idx = [min(range(n), key=lambda i: abs(
                math.log(math.sqrt(edges[i] * edges[i + 1])) - mid))]
        return idx

    def update(self, active, color, bars):
        """Called every frame from the render loop."""
        if not bars:
            return
        st = settings.values("lamp")
        now = time.monotonic()
        dt, self._prev = now - self._prev, now
        band = self._band(len(bars), st["low_hz"], st["high_hz"])
        level = min(1.0, sum(bars[i] for i in band) / len(band) / 1000)
        # hits: how far the band jumps above its own recent average. A kick is
        # a sudden jump; a held bass note at the same frequencies is not.
        hit = max(0.0, level - self._avg)
        self._avg += (level - self._avg) * (1 - math.exp(-dt / HIT_AVERAGE_S))
        energy = hit if st["mode"] == "hits" else level
        # silence is judged on every frequency, so quiet or bass-light
        # passages still count as music
        sound = sum(bars) / len(bars) / 1000
        with self._lock:
            self._active, self._color = active, lamp_color(color)
            # hold the peak: frames run at 60/s, the lamp at 20/s, and a kick
            # spike can fall between lamp updates
            self._energy = max(self._energy, energy)
            self._sound = sound
            if active:
                self._last_active = now

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._client = None
        threading.Thread(target=self._loop.run_until_complete, args=(self._run(),),
                         daemon=True).start()

    def stop(self, timeout=3):
        """Turn the lamp off and disconnect (call on shutdown), so the next
        start can find it again instead of BlueZ holding a stale link."""
        async def _off():
            c = self._client
            if c is not None and c.is_connected:
                try:
                    await c.write_gatt_char(CHAR, packet(0, 0, 0, 0), response=False)
                finally:
                    await c.disconnect()
        try:
            asyncio.run_coroutine_threadsafe(_off(), self._loop).result(timeout)
        except Exception:
            pass

    def _snapshot(self, consume=False):
        with self._lock:
            snap = self._active, self._color, self._energy, self._sound, self._last_active
            if consume:
                self._energy = 0.0
            return snap

    def _wanted(self):
        active, *_, last = self._snapshot()
        return not self.release or active or time.monotonic() - last < self.release

    async def _run(self):
        while True:
            if not self._wanted():
                await asyncio.sleep(1)
                continue
            try:
                # by address rather than scan: also picks up a link BlueZ
                # still holds (a connected lamp stops advertising)
                async with BleakClient(self.address, timeout=20) as client:
                    self._client = client
                    print("lamp: connected", flush=True)
                    await self._drive(client)
                self._client = None
                print("lamp: disconnected", flush=True)
            except Exception as e:   # BLE errors: lamp off, out of range, busy
                print(f"lamp: {e!r}", flush=True)
                await asyncio.sleep(5)

    async def _drive(self, client):
        level, last, last_sent, peak = 0.0, None, 0.0, 0.2
        floor, last_sound, prev = 0.0, 0.0, time.monotonic()

        def ease(ms, dt):   # fraction of the way to go this step; ~95% done after `ms`
            return 1.0 if ms <= 0 else 1 - math.exp(-dt * 3000 / ms)

        while client.is_connected:
            now = time.monotonic()
            dt, prev = now - prev, now
            st = settings.values("lamp")
            active, color, energy, sound, last_active = self._snapshot(consume=True)
            if active:
                # scale to this song's recent peak so quieter mixes still use the
                # full range; true silence stays dark. Hits are smaller numbers
                # (a jump, not a level), hence the lower minimum.
                min_peak = 0.05 if st["mode"] == "hits" else 0.15
                peak = max(energy, peak * math.exp(-dt / st["adapt_s"]), min_peak)
                if sound > SILENCE:
                    last_sound = now
                gate = st["gate"] / 100
                target = (max(0.0, energy / peak - gate) / (1 - gate)) ** st["curve"]
                # ease towards the target: quick rise, slower fall, no hard jumps
                level += (target - level) * ease(
                    st["rise_ms"] if target > level else st["settle_ms"], dt)
                # the floor only fades out after sustained silence, so quiet
                # passages don't flicker between off and on
                want_floor = st["floor"] / 100 if now - last_sound < st["silence_hold_s"] else 0.0
                floor += (want_floor - floor) * ease(st["settle_ms"], dt)
                ceiling = max(st["ceiling"] / 100, floor)
                bright = round((floor + (ceiling - floor) * level) * 100)
                state = (*color, bright) if bright >= 2 else (0, 0, 0, 0)
            else:
                state = (*color, st["idle_level"]) if st["idle"] == "dim" else (0, 0, 0, 0)
                if self.release and now - last_active >= self.release:
                    await client.write_gatt_char(CHAR, packet(*state), response=False)
                    return
            if state != last or now - last_sent > 2:
                await client.write_gatt_char(CHAR, packet(*state), response=False)
                last, last_sent = state, now
            await asyncio.sleep(1 / RATE)
