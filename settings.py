"""Live-adjustable settings: one schema drives validation, persistence, the
web page and presets.

Each section (e.g. "screen", "lamp") registers its settings. Values are read
at use time with get(), so changes pushed from the web page apply immediately.
Saved to ~/.config/airplay-viz/settings.json; named presets per section in
presets.json.
"""
import json
import os
import threading

CONFIG_DIR = os.path.expanduser("~/.config/airplay-viz")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
PRESETS_FILE = os.path.join(CONFIG_DIR, "presets.json")


class Setting:
    def __init__(self, key, label, default, *, min=None, max=None, step=None,
                 unit="", hint="", choices=None, restart=None, env=None):
        self.key, self.label, self.unit, self.hint = key, label, unit, hint
        self.min, self.max, self.step = min, max, step
        self.choices = choices            # list of strings for a dropdown
        self.restart = restart            # e.g. "cava": needs that restarted
        # an environment variable (from the service file) can set the default
        if env and env in os.environ:
            default = self.coerce(os.environ[env], default)
        self.default = default

    def coerce(self, value, fallback=None):
        """Validate/clamp a value; raise ValueError if unusable."""
        if self.choices:
            value = str(value)
            if value not in self.choices:
                raise ValueError(f"{self.label}: must be one of {', '.join(self.choices)}")
            return value
        try:
            value = float(value)
        except (TypeError, ValueError):
            if fallback is not None:
                return fallback
            raise ValueError(f"{self.label}: not a number")
        if value != value:   # NaN
            raise ValueError(f"{self.label}: not a number")
        if self.min is not None:
            value = max(self.min, value)
        if self.max is not None:
            value = min(self.max, value)
        if self.step and float(self.step).is_integer():
            value = int(round(value))
        return value

    def describe(self):
        return {k: getattr(self, k) for k in
                ("key", "label", "unit", "hint", "min", "max", "step", "choices",
                 "restart", "default")}


_lock = threading.Lock()
_sections = {}      # section id -> {"title": str, "settings": [Setting]}
_values = {}        # section id -> {key: value}
_listeners = []     # callables(section, changed_keys)


def register(section, title, settings):
    saved = _load(SETTINGS_FILE).get(section, {})
    with _lock:
        _sections[section] = {"title": title, "settings": list(settings)}
        vals = {}
        for s in settings:
            try:
                vals[s.key] = s.coerce(saved[s.key]) if s.key in saved else s.default
            except ValueError:
                vals[s.key] = s.default
        _values[section] = vals


def get(section, key):
    with _lock:
        return _values[section][key]


def values(section):
    """Snapshot of all values in a section (cheap; call once per frame/tick)."""
    with _lock:
        return dict(_values[section])


def on_change(callback):
    _listeners.append(callback)


def apply(section, updates):
    """Validate and apply {key: value}; persist; notify. Returns changed keys."""
    with _lock:
        if section not in _sections:
            raise ValueError(f"unknown section {section!r}")
        schema = {s.key: s for s in _sections[section]["settings"]}
        new = {}
        for key, value in updates.items():
            if key not in schema:
                raise ValueError(f"unknown setting {key!r}")
            new[key] = schema[key].coerce(value)
        changed = [k for k, v in new.items() if _values[section][k] != v]
        _values[section].update(new)
        _save_values()
    for cb in _listeners:
        cb(section, changed)
    return changed


def reset(section):
    with _lock:
        defaults = {s.key: s.default for s in _sections[section]["settings"]}
    return apply(section, defaults)


def state():
    """Everything the web page needs."""
    presets = _load(PRESETS_FILE)
    with _lock:
        return {"sections": [
            {"id": sid, "title": sec["title"],
             "settings": [dict(s.describe(), value=_values[sid][s.key]) for s in sec["settings"]],
             "presets": sorted(presets.get(sid, {}))}
            for sid, sec in _sections.items()]}


def save_preset(section, name):
    name = name.strip()
    if not name:
        raise ValueError("preset name is empty")
    presets = _load(PRESETS_FILE)
    presets.setdefault(section, {})[name] = values(section)
    _write(PRESETS_FILE, presets)


def load_preset(section, name):
    preset = _load(PRESETS_FILE).get(section, {}).get(name)
    if preset is None:
        raise ValueError(f"no preset {name!r}")
    with _lock:
        known = {s.key for s in _sections[section]["settings"]}
    # presets saved by an older version may lack newer keys: keep current ones
    return apply(section, {k: v for k, v in preset.items() if k in known})


def delete_preset(section, name):
    presets = _load(PRESETS_FILE)
    if presets.get(section, {}).pop(name, None) is None:
        raise ValueError(f"no preset {name!r}")
    _write(PRESETS_FILE, presets)


def _save_values():
    _write(SETTINGS_FILE, _values)


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(path, data):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(path + ".tmp", path)
