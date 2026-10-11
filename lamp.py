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
import threading
import time

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = bytes.fromhex("34522A5B7A6E492C08090A9D8D2A23F8")
CHAR = "0000ac52-1212-efde-1523-785fedbeda25"
RATE = 20             # updates per second while playing
FLOOR = 0.15          # brightness between beats (0-1)


def packet(r, g, b, brightness):
    p = bytes([0x54, 0x52, 0x00, 0x57, 0x02, 0x01, 0x00,
               r, g, b, max(0, min(100, brightness)), 100, 0, 0, 0, 0])
    enc = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    return enc.update(p) + enc.finalize()


class Lamp:
    def __init__(self, address, idle="dim", release=0):
        self.address = address
        self.idle = idle          # "dim": stay on in the album colour, "off"
        self.release = release    # seconds idle before disconnecting; 0 = stay connected
        self._lock = threading.Lock()
        self._active = False
        self._color = (255, 255, 255)
        self._bass = 0.0
        self._last_active = 0.0

    def update(self, active, color, bars):
        """Called every frame from the render loop."""
        n = max(1, len(bars) // 8)            # lowest ~1/8 of the bars = bass
        bass = min(1.0, sum(bars[:n]) / n / 1000)
        with self._lock:
            self._active, self._color, self._bass = active, tuple(color), bass
            if active:
                self._last_active = time.monotonic()

    def start(self):
        threading.Thread(target=lambda: asyncio.run(self._run()), daemon=True).start()

    def _snapshot(self):
        with self._lock:
            return self._active, self._color, self._bass, self._last_active

    def _wanted(self):
        active, _, _, last = self._snapshot()
        return not self.release or active or time.monotonic() - last < self.release

    async def _run(self):
        while True:
            if not self._wanted():
                await asyncio.sleep(1)
                continue
            try:
                dev = await BleakScanner.find_device_by_address(self.address, timeout=15)
                if dev is None:
                    print(f"lamp: {self.address} not found", flush=True)
                    await asyncio.sleep(10)
                    continue
                async with BleakClient(dev, timeout=20) as client:
                    print("lamp: connected", flush=True)
                    await self._drive(client)
                print("lamp: disconnected", flush=True)
            except Exception as e:   # BLE errors: lamp off, out of range, busy
                print(f"lamp: {e!r}", flush=True)
                await asyncio.sleep(5)

    async def _drive(self, client):
        level, last, last_sent = 0.0, None, 0.0
        while client.is_connected:
            now = time.monotonic()
            active, color, bass, last_active = self._snapshot()
            if active:
                target = FLOOR + (1 - FLOOR) * bass ** 1.3
                # fast attack, smoother decay
                level = target if target > level else level * 0.8 + target * 0.2
                state = (*color, round(level * 100))
            else:
                state = (*color, 30) if self.idle == "dim" else (0, 0, 0, 0)
                if self.release and now - last_active >= self.release:
                    await client.write_gatt_char(CHAR, packet(*state), response=False)
                    return
            if state != last or now - last_sent > 2:
                await client.write_gatt_char(CHAR, packet(*state), response=False)
                last, last_sent = state, now
            await asyncio.sleep(1 / RATE)
