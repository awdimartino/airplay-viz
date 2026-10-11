"""Music-reactive control for iStrip+ Bluetooth LE lamps (e.g. "SSL-xxxxxx"
sunset lamps).

Protocol (reverse-engineered in https://habr.com/en/articles/722412): 16-byte
packets, AES-128-ECB with a fixed key, written without response to
characteristic ac52:

    54 52 00 57 | cmd | group | mode | R G B | brightness 0-100 | speed | 0 0 0 0

cmd 2 = set colour. The lamp takes a single BLE connection. By default we hold
it permanently; set release > 0 to disconnect that many seconds after playback
ends so the iStrip+ app can use the lamp in between.
"""
import asyncio
import colorsys
import threading
import time

from bleak import BleakClient
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = bytes.fromhex("34522A5B7A6E492C08090A9D8D2A23F8")
CHAR = "0000ac52-1212-efde-1523-785fedbeda25"
RATE = 20             # updates per second while playing
GATE = 0.2            # fraction of the song's recent peak below which the lamp sits at FLOOR
CURVE = 1.2           # >1 = more contrast between quiet and loud
FLOOR = 0.10          # brightness while music plays (0-1); silence is off
SILENCE = 0.005       # mean level across ALL bars (0-1) below which it is silence
PEAK_DECAY = 0.995    # per update; recent-peak memory of roughly 7 s at 20 Hz
ATTACK = 0.6          # per update, fraction of the way to a brighter target (~80 ms)
RELEASE = 0.12        # per update, fraction of the way to a dimmer target (~400 ms)
SILENCE_HOLD = 1.5    # seconds of silence before the floor fades out


GAMMA = (1.5, 2.4, 2.4)   # per R,G,B: LED channels are linear; screens are not
GAIN = (1.0, 1.0, 0.82)   # per R,G,B white balance: this lamp's blue LED is strong
SATURATE = 1.4        # >1 strips more of the "white" (smallest channel)


def lamp_color(rgb):
    """Map a screen colour to what looks like the same colour on the lamp.

    Cheap RGB LEDs drive each channel linearly, so mid values (e.g. the green
    in orange) look nearly full and colours wash out towards white/pink.
    Boost saturation, apply gamma, then normalise so the brightest channel is
    255 (overall brightness is set separately by the brightness byte).
    """
    r, g, b = (c / 255 for c in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.08:                         # neutral/grey art: plain white
        return (255, 255, 255)
    r, g, b = colorsys.hsv_to_rgb(h, min(1.0, s * SATURATE), 1.0)
    r, g, b = (c ** gm * gn for c, gm, gn in zip((r, g, b), GAMMA, GAIN))
    m = max(r, g, b)
    return tuple(round(255 * c / m) for c in (r, g, b))


def packet(r, g, b, brightness):
    p = bytes([0x54, 0x52, 0x00, 0x57, 0x02, 0x01, 0x00,
               r, g, b, max(0, min(100, brightness)), 100, 0, 0, 0, 0])
    enc = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    return enc.update(p) + enc.finalize()


class Lamp:
    def __init__(self, address, idle="off", release=0):
        self.address = address
        self.idle = idle          # "off", or "dim": stay on at 30% in the album colour
        self.release = release    # seconds idle before disconnecting; 0 = stay connected
        self._lock = threading.Lock()
        self._active = False
        self._color = (255, 255, 255)
        self._bass = 0.0
        self._sound = 0.0
        self._last_active = 0.0

    def update(self, active, color, bars):
        """Called every frame from the render loop."""
        # energy from bass (lowest 1/8 of bars) and low-mids (up to half), so
        # songs without a big kick still drive the lamp
        n, h = max(1, len(bars) // 8), max(2, len(bars) // 2)
        bass = sum(bars[:n]) / n / 1000
        lowmid = sum(bars[n:h]) / (h - n) / 1000
        bass = min(1.0, 0.5 * bass + 0.5 * lowmid)
        # silence is judged on every frequency, so quiet or bass-light
        # passages still count as music
        sound = sum(bars) / max(1, len(bars)) / 1000
        with self._lock:
            self._active, self._color, self._bass = active, lamp_color(color), bass
            self._sound = sound
            if active:
                self._last_active = time.monotonic()

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

    def _snapshot(self):
        with self._lock:
            return self._active, self._color, self._bass, self._sound, self._last_active

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
        floor, last_sound = 0.0, 0.0
        while client.is_connected:
            now = time.monotonic()
            active, color, bass, sound, last_active = self._snapshot()
            if active:
                # scale to this song's recent peak so quieter mixes still use the
                # full range; true silence stays dark
                peak = max(bass, peak * PEAK_DECAY, 0.15)
                if sound > SILENCE:
                    last_sound = now
                rel = bass / peak
                target = (max(0.0, rel - GATE) / (1 - GATE)) ** CURVE
                # ease towards the target: quick rise, slower fall, no hard jumps
                level += (target - level) * (ATTACK if target > level else RELEASE)
                # the floor only fades out after sustained silence, so quiet
                # passages don't flicker between off and on
                want_floor = FLOOR if now - last_sound < SILENCE_HOLD else 0.0
                floor += (want_floor - floor) * RELEASE
                bright = round((floor + (1 - floor) * level) * 100)
                state = (*color, bright) if bright >= 2 else (0, 0, 0, 0)
            else:
                state = (*color, 30) if self.idle == "dim" else (0, 0, 0, 0)
                if self.release and now - last_active >= self.release:
                    await client.write_gatt_char(CHAR, packet(*state), response=False)
                    return
            if state != last or now - last_sent > 2:
                await client.write_gatt_char(CHAR, packet(*state), response=False)
                last, last_sent = state, now
            await asyncio.sleep(1 / RATE)
