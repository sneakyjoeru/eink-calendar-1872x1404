"""Settings store — persists user configuration to settings.json."""
import json
import threading
from pathlib import Path
from typing import Optional

from . import config

_DEFAULTS = {
    "view_mode": "week",          # "month" | "35days" | "week" | "7days"
    "day_start": "07:00",         # HH:MM
    "day_end": "23:00",           # HH:MM
    "max_full_day_events": 3,     # 1-3
    "selected_calendars": [],     # list of calendar IDs (empty = all)
    "time_line_interval_min": 15, # minutes between time-line updates
    "event_poll_interval_sec": 60,# seconds between event polls
    "full_refresh_interval_hours": 6, # hours between forced full refreshes (0 = never, only day change/event change)
    "update_mode": "soft",       # "soft" (GL16 regional, no flash, old state preserved at border), "hard" (flash inner + GL16 region). Full-screen refresh is governed by force_full (day change/interval/dim-toggle/manual), not this setting.
    "refresh_border_mm": 5,     # partial-refresh area expansion in mm (0 = none; old content kept in the border zone, only the inner changed area visibly updates)
    "fullscreen_on_dim": False, # when True, force a full-screen clean refresh when the event set changes (events ending/starting) — clears dimming ghosting. Off by default.
    "brightness": 1.4,            # gamma boost for e-ink
    "timezone": "",               # IANA timezone, empty = system default
    "time_format": "24h",         # "24h" or "12h"
    "date_format": "",            # strftime format: "" | "%B %Y" | "%B %d, %Y" | "%Y.%m.%d %a" | "%d %B %Y"
    "dim_past_events": False,     # dim past events on the display
    "crossed_event_dim": False,   # dim events when time line crosses them
    "text_size_modifier": 0,      # global font size adjustment (+/- pixels)
}

_lock = threading.RLock()


def load() -> dict:
    """Load settings from disk, merged with defaults."""
    if config.SETTINGS_FILE.exists():
        try:
            data = json.loads(config.SETTINGS_FILE.read_text())
            merged = {**_DEFAULTS, **data}
            # Migrate legacy settings removed in this version:
            #  - dither_border_mm → refresh_border_mm (renamed, same meaning)
            #  - update_mode 'smooth'/'fullscreen' → 'soft' (those modes no longer exist)
            if "dither_border_mm" in merged and "refresh_border_mm" not in data:
                merged["refresh_border_mm"] = merged.pop("dither_border_mm")
            elif "dither_border_mm" in merged:
                merged.pop("dither_border_mm", None)
            if merged.get("update_mode") in ("smooth", "fullscreen"):
                merged["update_mode"] = "soft"
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_DEFAULTS)


def save(settings: dict) -> None:
    """Persist settings to disk."""
    with _lock:
        config.SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


def update(partial: dict) -> dict:
    """Merge partial into stored settings and persist. Returns the new full settings."""
    with _lock:
        current = load()
        current.update(partial)
        save(current)
        return current