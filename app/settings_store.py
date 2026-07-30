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
    "update_mode": "soft",       # "soft" (GL16 regional, no flash, dithering visible), "hard" (flash inner + GL16 dither), "smooth" (A2 1-bit, no flash, fastest — no dithering). Full-screen refresh is governed by force_full (day/event change/interval), not this setting.
    "dither_border_mm": 5,        # dithering border in mm (0 = no dithering, converted to px at ~11.85 px/mm)
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
            # "fullscreen" is no longer a regional update mode — normalize the
            # legacy value to "soft" so regional updates don't force a whole
            # screen refresh every tick.
            if merged.get("update_mode") == "fullscreen":
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