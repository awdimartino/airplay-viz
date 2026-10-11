#!/usr/bin/env python3
"""AirPlay now-playing screen: album art + track info on the left, equalizer
bars on the right, drawn full-screen on the HDMI display via SDL/KMSDRM.

- Audio analysis: cava (raw output) reading the ALSA loopback that
  shairport-sync's output is duplicated to.
- Track info + cover art: shairport-sync's metadata pipe.
- Bar colour: picked from the album art for each track.

`kill -USR1 <pid>` saves the current frame to /tmp/airplay-viz.png.
"""
import base64
import collections
import io
import json
import math
import os
import re
import signal
import subprocess
import threading
import time

os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter  # noqa: E402

PIPE = "/tmp/shairport-sync-metadata"
CAVA_CONF = os.path.expanduser("~/.config/cava/airplay-viz.conf")
SCREENSHOT = "/tmp/airplay-viz.png"
CACHE_DIR = os.path.expanduser("~/.cache/airplay-viz")
STATE_FILE = os.path.join(CACHE_DIR, "state.json")
ART_FILE = os.path.join(CACHE_DIR, "art")
IDLE_NAME = os.environ.get("AIRPLAY_VIZ_NAME", "AirPlay")
LAMP_ADDRESS = os.environ.get("AIRPLAY_VIZ_LAMP", "")          # iStrip+ lamp MAC; empty = off
LAMP_IDLE = os.environ.get("AIRPLAY_VIZ_LAMP_IDLE", "off")     # "off" or "dim" when nothing is playing
LAMP_RELEASE = float(os.environ.get("AIRPLAY_VIZ_LAMP_RELEASE", "0"))  # 0 = stay connected
# Seconds to hold back the on-screen bars. With a larger shairport output
# buffer the analysed audio arrives early; delaying only the screen keeps it in
# sync with what you hear while the lamp (which lags over Bluetooth) gets the
# head start.
SCREEN_DELAY = float(os.environ.get("AIRPLAY_VIZ_SCREEN_DELAY", "0"))
# Put the screen to sleep after this many seconds without music (0 = never)
SCREEN_TIMEOUT = float(os.environ.get("AIRPLAY_VIZ_SCREEN_TIMEOUT", "300"))
# How to sleep: "black" keeps the HDMI signal and shows pure black (for
# screens whose "no signal" page is bright); "off" switches HDMI off.
SCREEN_SLEEP = os.environ.get("AIRPLAY_VIZ_SCREEN_SLEEP", "black")
BARS = 40
FPS = 60
RATE = 44100          # AirPlay RTP frames per second
FADE = 0.6            # seconds for track-change crossfade

FONT_DIR = "/usr/share/fonts/opentype/inter/"
FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FALLBACK_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

NEUTRAL = (225, 225, 232)

ITEM = re.compile(
    rb"<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code>"
    rb"<length>(\d+)</length>(?:\s*<data encoding=\"base64\">\s*(.*?)</data>)?\s*</item>",
    re.S,
)


# --------------------------------------------------------------------------
# Shared state (metadata thread -> render loop)

lock = threading.Lock()
meta = {
    "title": "", "artist": "", "album": "", "art": None,
    "active": False, "paused": False, "paused_at": 0.0,
    "pos0": 0.0, "t0": 0.0, "total": 0.0,
    "changed": 0.0,   # time of last title/art change (debounced rebuild)
}
bars_latest = [0] * BARS
shutting_down = threading.Event()


def position(m):
    pos = m["pos0"] + (0 if m["paused"] else time.monotonic() - m["t0"])
    return max(0.0, min(pos, m["total"])) if m["total"] else max(0.0, pos)


def handle(kind, code, data):
    now = time.monotonic()
    with lock:
        m = meta
        if kind == "core" and code in ("minm", "asar", "asal"):
            key = {"minm": "title", "asar": "artist", "asal": "album"}[code]
            text = data.decode("utf-8", "replace")
            if m[key] != text:
                m[key] = text
                m["changed"] = now
        elif kind == "ssnc":
            if code == "PICT":
                m["art"] = data or None
                m["changed"] = now
            elif code == "pbeg":
                m["active"], m["paused"] = True, False
                m["changed"] = now
            elif code == "pend":
                m.update(active=False, paused=False, title="", artist="",
                         album="", art=None, total=0.0, pos0=0.0, changed=now)
            elif code == "pfls":
                m["pos0"] = position(m)
                m["paused"], m["paused_at"] = True, now
            elif code == "prsm":
                m["paused"], m["t0"], m["active"] = False, now, True
            elif code == "prgr":
                try:
                    start, cur, end = (int(x) for x in data.decode().split("/"))
                except ValueError:
                    return
                m["pos0"] = max(0, cur - start) / RATE
                m["total"] = max(0, end - start) / RATE
                m["t0"], m["paused"], m["active"] = now, False, True


def metadata_reader():
    while True:
        try:
            with open(PIPE, "rb", buffering=0) as f:
                buf = bytearray()
                scan = 0   # where to resume looking for "</item>"
                while chunk := f.read(65536):
                    buf += chunk
                    # only parse once a complete item has arrived (cover art
                    # items can be several MB)
                    while (end := buf.find(b"</item>", scan)) != -1:
                        end += len(b"</item>")
                        mt = ITEM.search(bytes(buf[:end]))
                        del buf[:end]
                        scan = 0
                        if not mt:
                            continue
                        kind = bytes.fromhex(mt[1].decode()).decode("latin-1")
                        code = bytes.fromhex(mt[2].decode()).decode("latin-1")
                        try:
                            data = base64.b64decode(mt[4]) if mt[4] else b""
                        except ValueError:
                            data = b""
                        handle(kind, code, data)
                    scan = max(0, len(buf) - len(b"</item>"))
                    if len(buf) > 64 * 1024 * 1024:   # garbage; resync
                        buf.clear()
                        scan = 0
        except OSError:
            time.sleep(2)


def session_connected():
    """True if a phone has an AirPlay (RTSP) connection to shairport open."""
    try:
        out = subprocess.run(["ss", "-Htn", "state", "established", "( sport = :5000 )"],
                             capture_output=True, text=True, timeout=3).stdout
        return bool(out.strip())
    except Exception:
        return False


def save_state(m, with_art=True):
    """Persist now-playing so a restart mid-song can restore it (shairport
    only sends track info at track start)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        if not m["active"]:
            for f in (STATE_FILE, ART_FILE):
                if os.path.exists(f):
                    os.remove(f)
            return
        state = {k: m[k] for k in ("title", "artist", "album", "total", "paused")}
        state["pos"] = position(m)
        state["saved_at"] = time.time()
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(state, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
        if not with_art:
            return
        if m["art"]:
            with open(ART_FILE + ".tmp", "wb") as f:
                f.write(m["art"])
            os.replace(ART_FILE + ".tmp", ART_FILE)
        elif os.path.exists(ART_FILE):
            os.remove(ART_FILE)
    except OSError:
        pass


def restore_state():
    if not os.path.exists(STATE_FILE) or not session_connected():
        return
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        art = open(ART_FILE, "rb").read() if os.path.exists(ART_FILE) else None
    except (OSError, ValueError):
        return
    pos = s["pos"] + (0 if s["paused"] else time.time() - s["saved_at"])
    now = time.monotonic()
    with lock:
        meta.update(title=s["title"], artist=s["artist"], album=s["album"], art=art,
                    total=s["total"], paused=s["paused"], paused_at=now - 10,
                    pos0=pos, t0=now, active=True, changed=now)


def write_cava_conf():
    os.makedirs(os.path.dirname(CAVA_CONF), exist_ok=True)
    with open(CAVA_CONF, "w") as f:
        f.write(f"""[general]
framerate = {FPS}
autosens = 1
bars = {BARS}

[input]
method = alsa
source = plughw:Loopback,1

[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 1000
bar_delimiter = 59
frame_delimiter = 10
channels = mono
mono_option = average

[smoothing]
noise_reduction = 35
""")


def cava_reader(proc):
    global bars_latest
    for line in proc.stdout:
        try:
            vals = [int(v) for v in line.split(b";") if v.strip()]
        except ValueError:
            continue
        if len(vals) >= BARS:
            bars_latest = vals[:BARS]
    if not shutting_down.is_set():
        os._exit(1)   # cava died unexpectedly: let systemd restart us


# --------------------------------------------------------------------------
# Image helpers (PIL) and colour

def pil_to_surface(img):
    img = img.convert("RGBA")
    return pygame.image.frombytes(img.tobytes(), img.size, "RGBA").convert_alpha()


def _to_oklab(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _from_oklab(L, a, b):
    """OKLab -> sRGB 0-255, or None if out of gamut."""
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    if any(c < -1e-4 or c > 1 + 1e-4 for c in lin):
        return None
    out = []
    for c in lin:
        c = min(1.0, max(0.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
        out.append(round(c * 255))
    return tuple(out)


def accent_from(img):
    """Pick the album's signature colour, the way music players do.

    Quantize the cover to its main colours, measure them in OKLab (so
    "colourful" and "bright" match perception), merge shades of the same hue,
    and score each hue by how much of the cover it covers and how colourful it
    is. Near-black/white/grey is ignored. The winner keeps its hue and is only
    nudged in lightness/chroma to read well on the dark background.
    """
    small = img.convert("RGB")
    small.thumbnail((96, 96))
    quant = small.quantize(colors=32, method=Image.Quantize.FASTOCTREE)
    pal = quant.getpalette()
    counts = quant.getcolors() or []
    total = sum(n for n, _ in counts) or 1

    clusters = []   # (share, L, C, hue_deg): real colours, can be picked
    tints = []      # faintly tinted areas (pale sky, sepia): only add weight
    for n, idx in counts:
        L, a, b = _to_oklab(pal[idx * 3: idx * 3 + 3])
        C = math.hypot(a, b)
        if L < 0.22 or L > 0.96 or C < 0.02:   # near-black, near-white, grey
            continue
        entry = (n / total, L, C, math.degrees(math.atan2(b, a)) % 360)
        (clusters if C >= 0.05 else tints).append(entry)
    if not clusters:
        return NEUTRAL

    def hue_gap(h1, h2):
        d = abs(h1 - h2) % 360
        return min(d, 360 - d)

    # chain shades into hue families: walk round the hue circle and start a
    # new family wherever there is a gap of more than LINK degrees, so a
    # gradient like teal -> blue or orange -> brown stays one family
    LINK = 20
    by_hue = sorted(clusters, key=lambda c: c[3])
    groups = [[[], 0.0]]     # [[clusters], tint_weight]
    for prev, c in zip([None] + by_hue, by_hue):
        if prev is not None and c[3] - prev[3] > LINK:
            groups.append([[], 0.0])
        groups[-1][0].append(c)
    if len(groups) > 1 and hue_gap(by_hue[0][3], by_hue[-1][3]) <= LINK:
        groups[0][0] += groups.pop()[0]   # family wrapping past 360 degrees
    for t in tints:   # tinted areas back up the family they belong to
        near = [(min(hue_gap(t[3], c[3]) for c in g[0]), g) for g in groups]
        gap, g = min(near, key=lambda x: x[0])
        if gap <= LINK:
            g[1] += t[0] * t[2] / 0.05

    def score(g):
        share = sum(c[0] for c in g[0])
        chroma = sum(c[0] * c[2] for c in g[0]) / share
        return (share + g[1]) ** 0.75 * min(chroma / 0.2, 1.3) ** 0.6

    # small accents only count when vivid (e.g. a coloured logo on dark art)
    groups = [g for g in groups
              if sum(c[0] for c in g[0]) >= (0.008 if max(c[2] for c in g[0]) > 0.12 else 0.03)]
    if not groups:
        return NEUTRAL
    best = max(groups, key=score)
    _, L, C, hue = max(best[0], key=lambda c: c[0] * c[2])

    # fit for a dark background without changing the hue
    L = min(max(L, 0.72), 0.86)
    C = min(max(C * 1.4, 0.13), 0.28)
    h = math.radians(hue)
    while C > 0:
        rgb = _from_oklab(L, C * math.cos(h), C * math.sin(h))
        if rgb:
            return rgb
        C -= 0.005
    return NEUTRAL


def rounded(img, size, radius):
    img = img.convert("RGB").resize((size, size), Image.LANCZOS)
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size * scale - 1, size * scale - 1), radius * scale, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


def shadow(size, radius, blur):
    pad = blur * 3
    sh = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        (pad, pad, pad + size, pad + size), radius, fill=(0, 0, 0, 170))
    return sh.filter(ImageFilter.GaussianBlur(blur)), pad


def blurred_background(img, w, h):
    img = img.convert("RGB")
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((int(iw * scale) + 1, int(ih * scale) + 1))
    left, top = (img.width - w) // 2, (img.height - h) // 2
    img = img.crop((left, top, left + w, top + h))
    small = img.resize((w // 4, h // 4), Image.BILINEAR).filter(ImageFilter.GaussianBlur(4))
    img = small.resize((w, h), Image.BICUBIC)
    img = ImageEnhance.Color(img).enhance(1.15)
    img = ImageEnhance.Brightness(img).enhance(0.5)
    # darken the lower part so the full-width text stays readable
    fade = Image.linear_gradient("L").resize((w, h))   # 0 at top -> 255 at bottom
    fade = fade.point(lambda p: max(0, p - 110) * 150 // 145)
    black = Image.new("RGB", (w, h), (0, 0, 0))
    return Image.composite(black, img, fade)


def placeholder_art(size):
    img = Image.new("RGB", (size, size), (38, 38, 46))
    d = ImageDraw.Draw(img)
    for y in range(size):   # soft vertical gradient
        c = int(46 - 18 * y / size)
        d.line((0, y, size, y), fill=(c, c, c + 8))
    return img


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


# --------------------------------------------------------------------------
# Rendering

class Viz:
    def __init__(self, screen):
        self.screen = screen
        self.W, self.H = screen.get_size()
        W, H = self.W, self.H
        self.margin = int(H * 0.093)                      # 56 at 600px
        self.art_size = min(int(H * 0.5), W // 3)          # 300 at 600px
        self.art_pos = (self.margin, self.margin)
        # bars sit level with the art; text + progress span the full width below
        self.text_x = self.margin
        self.text_w = W - 2 * self.margin
        self.bars_x0 = self.margin + self.art_size + int(W * 0.05)
        self.bars_x1 = W - self.margin
        self.bars_top = self.margin
        self.bars_bottom = self.margin + self.art_size

        def font(name, px, fb):
            return (pygame.font.Font(FONT_DIR + name, px), pygame.font.Font(fb, px))
        self.f_title = font("Inter-SemiBold.otf", int(H * 0.05), FALLBACK_BOLD)
        self.f_artist = font("Inter-Medium.otf", int(H * 0.035), FALLBACK)
        self.f_album = font("Inter-Regular.otf", int(H * 0.029), FALLBACK)
        self.f_time = font("Inter-Medium.otf", int(H * 0.023), FALLBACK)
        self.f_idle = font("Inter-SemiBold.otf", int(H * 0.08), FALLBACK_BOLD)
        self.f_idle2 = font("Inter-Regular.otf", int(H * 0.035), FALLBACK)

        self.layer = None        # static background + art + text
        self.prev_layer = None
        self.fade_t0 = 0.0
        self.accent = NEUTRAL
        self.prev_accent = NEUTRAL
        self.built_key = None
        self.track = None
        self.progress_y = H - self.margin - int(H * 0.05)

    # -- text helpers
    def render(self, fonts, text, color, maxw=None):
        f = fonts[0] if all(m is not None for m in fonts[0].metrics(text) or [None]) else fonts[1]
        if maxw and f.size(text)[0] > maxw:
            while text and f.size(text + "…")[0] > maxw:
                text = text[:-1]
            text = text.rstrip() + "…"
        return f.render(text, True, color)

    # -- static layer, rebuilt when track/art changes
    def build(self, m):
        W, H = self.W, self.H
        layer = pygame.Surface((W, H)).convert()
        if not m["active"]:
            # idle screen
            for y in range(H):
                c = int(22 - 12 * y / H)
                pygame.draw.line(layer, (c, c, c + 6), (0, y), (W, y))
            t = self.render(self.f_idle, IDLE_NAME, (240, 240, 245))
            s = self.render(self.f_idle2, "Ready for AirPlay", (150, 150, 160))
            gap = int(H * 0.02)
            y = (H - t.get_height() - s.get_height() - gap) // 2
            layer.blit(t, ((W - t.get_width()) // 2, y))
            layer.blit(s, ((W - s.get_width()) // 2, y + t.get_height() + gap))
            return layer, NEUTRAL

        art = None
        if m["art"]:
            try:
                art = Image.open(io.BytesIO(m["art"]))
                art.draft("RGB", (W, W))   # fast JPEG downscale for huge covers
                art.load()
            except Exception:
                art = None
        src = art if art is not None else placeholder_art(self.art_size)
        accent = accent_from(art) if art is not None else NEUTRAL

        layer.blit(pil_to_surface(blurred_background(src, W, H)), (0, 0))

        radius = max(8, self.art_size // 20)
        sh, pad = shadow(self.art_size, radius, 18)
        layer.blit(pil_to_surface(sh), (self.art_pos[0] - pad, self.art_pos[1] - pad + 8))
        layer.blit(pil_to_surface(rounded(src, self.art_size, radius)), self.art_pos)
        if art is None:
            note = self.render(self.f_idle, "♪", (110, 110, 120))
            layer.blit(note, (self.art_pos[0] + (self.art_size - note.get_width()) // 2,
                              self.art_pos[1] + (self.art_size - note.get_height()) // 2))

        y = self.art_pos[1] + self.art_size + int(H * 0.04)
        for fonts, text, color, gap in (
            (self.f_title, m["title"] or "Unknown title", (250, 250, 252), 0.010),
            (self.f_artist, m["artist"], (215, 215, 222), 0.006),
            (self.f_album, m["album"], (160, 160, 170), 0.0),
        ):
            if not text:
                continue
            surf = self.render(fonts, text, color, self.text_w)
            layer.blit(surf, (self.text_x, y))
            y += surf.get_height() + int(H * gap)
        self.progress_y = max(y + int(H * 0.035), self.H - self.margin - int(H * 0.05))
        return layer, accent

    def maybe_rebuild(self, m, now):
        key = (m["active"], m["title"], m["artist"], m["album"], id(m["art"]))
        if key == self.built_key or (now - m["changed"] < 0.25 and self.layer is not None):
            return False
        self.built_key = key
        layer, accent = self.build(m)
        cur_accent = self.current_accent(now)
        self.prev_layer, self.prev_accent = self.layer, cur_accent
        self.layer, self.accent = layer, accent
        self.fade_t0 = now
        return True

    def current_accent(self, now):
        t = min(1.0, (now - self.fade_t0) / FADE)
        return mix(self.prev_accent, self.accent, t)

    # -- per frame
    def draw(self, m, bars, now):
        scr = self.screen
        t = min(1.0, (now - self.fade_t0) / FADE)
        if self.prev_layer is not None and t < 1.0:
            scr.blit(self.prev_layer, (0, 0))
            self.layer.set_alpha(int(255 * t))
            scr.blit(self.layer, (0, 0))
            self.layer.set_alpha(None)
        else:
            scr.blit(self.layer, (0, 0))
        accent = self.current_accent(now)

        if m["active"]:
            self.draw_bars(bars, accent)
            self.draw_progress(m, accent)

    def draw_bars(self, bars, accent):
        n = len(bars)
        span = self.bars_x1 - self.bars_x0
        pitch = span / n
        bw = max(2, int(pitch * 0.68))
        height = self.bars_bottom - self.bars_top
        low, high = mix((0, 0, 0), accent, 0.75), mix(accent, (255, 255, 255), 0.35)
        for i, v in enumerate(bars):
            h = max(4, int(height * min(v, 1000) / 1000))
            x = int(self.bars_x0 + i * pitch + (pitch - bw) / 2)
            color = mix(low, high, h / height)
            pygame.draw.rect(self.screen, color, (x, self.bars_bottom - h, bw, h),
                             border_radius=min(bw // 2, h // 2))

    def draw_progress(self, m, accent):
        total = m["total"]
        pos = position(m)
        y = self.progress_y
        x0, w = self.text_x, self.text_w
        if self.track is None or self.track.get_width() != w:
            self.track = pygame.Surface((w, 4), pygame.SRCALPHA)
            pygame.draw.rect(self.track, (255, 255, 255, 55), (0, 0, w, 4), border_radius=2)
        self.screen.blit(self.track, (x0, y))
        if total:
            done = int(w * pos / total)
            if done > 0:
                pygame.draw.rect(self.screen, accent, (x0, y, done, 4), border_radius=2)
        paused = m["paused"] and time.monotonic() - m["paused_at"] > 1.5
        left = "Paused" if paused else fmt_time(pos)
        right = fmt_time(total) if total else ""
        ls = self.render(self.f_time, left, (190, 190, 200))
        self.screen.blit(ls, (x0, y + 10))
        if right:
            rs = self.render(self.f_time, right, (150, 150, 160))
            self.screen.blit(rs, (x0 + w - rs.get_width(), y + 10))


def open_display():
    pygame.display.init()
    screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)
    return screen, Viz(screen)


def fade_out(screen, seconds=2.0):
    frame = screen.copy()
    black = pygame.Surface(screen.get_size())
    t0 = time.monotonic()
    while (t := time.monotonic() - t0) < seconds:
        screen.blit(frame, (0, 0))
        black.set_alpha(int(255 * t / seconds))
        screen.blit(black, (0, 0))
        pygame.display.flip()
        time.sleep(1 / 30)


def console_blank(blank):
    """Blank/unblank the Linux console on our tty (tty1). With KMS this
    switches the HDMI output off (DPMS), not just to black."""
    try:
        with open("/dev/tty1", "w") as tty:
            subprocess.run(["setterm", "--blank", "force" if blank else "poke"],
                           stdin=tty, stdout=tty, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"console blank failed: {e}", flush=True)


def main():
    write_cava_conf()
    cava = subprocess.Popen(["cava", "-p", CAVA_CONF], stdout=subprocess.PIPE)
    threading.Thread(target=cava_reader, args=(cava,), daemon=True).start()
    threading.Thread(target=metadata_reader, daemon=True).start()

    pygame.font.init()
    screen, viz = open_display()
    restore_state()
    last_save = time.monotonic()
    history = collections.deque()   # (time, bars) for SCREEN_DELAY

    lamp = None
    if LAMP_ADDRESS:
        try:
            from lamp import Lamp
            lamp = Lamp(LAMP_ADDRESS, LAMP_IDLE, LAMP_RELEASE)
            lamp.start()
        except ImportError as e:   # bleak / cryptography missing
            print(f"lamp disabled: {e}", flush=True)
    last_playing = time.monotonic()
    asleep = False

    flags = {"shot": False, "run": True}
    signal.signal(signal.SIGUSR1, lambda *_: flags.__setitem__("shot", True))
    signal.signal(signal.SIGTERM, lambda *_: flags.__setitem__("run", False))

    clock = pygame.time.Clock()
    try:
        while flags["run"]:
            now = time.monotonic()
            with lock:
                m = dict(meta)
            if m["active"] and not m["paused"]:
                last_playing = now

            if asleep:
                if now - last_playing > 1:   # still nothing playing
                    time.sleep(0.25)
                    continue
                if SCREEN_SLEEP == "off":       # music is back: wake up
                    console_blank(False)
                    screen, viz = open_display()
                else:                           # fade in from the black frame
                    viz.prev_layer = pygame.Surface(screen.get_size()).convert()
                    viz.fade_t0 = now
                asleep = False
            elif SCREEN_TIMEOUT and now - last_playing > SCREEN_TIMEOUT:
                fade_out(screen)
                if SCREEN_SLEEP == "off":
                    pygame.display.quit()   # hand the display to the console...
                    console_blank(True)     # ...which switches HDMI off
                else:
                    screen.fill((0, 0, 0))  # keep the signal, show pure black
                    pygame.display.flip()
                asleep = True
                continue

            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    flags["run"] = False
            if viz.maybe_rebuild(m, now):
                save_state(m)
                last_save = now
            elif m["active"] and now - last_save > 5:
                save_state(m, with_art=False)
                last_save = now
            screen_bars = bars_latest
            if SCREEN_DELAY > 0:
                history.append((now, bars_latest))
                while len(history) > 1 and history[1][0] <= now - SCREEN_DELAY:
                    history.popleft()
                screen_bars = history[0][1]
            viz.draw(m, screen_bars, now)
            if lamp:
                lamp.update(m["active"], viz.current_accent(now), bars_latest)
            pygame.display.flip()
            if flags["shot"]:
                flags["shot"] = False
                pygame.image.save(screen, SCREENSHOT)
            clock.tick(FPS)
    finally:
        shutting_down.set()
        if lamp:
            lamp.stop()
        cava.terminate()
        pygame.quit()


if __name__ == "__main__":
    main()
