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
    "update_mode": "soft",       # "soft" (GL16 regional, no flash), "hard" (flash inner + GL16), "du" (DU 1-bit, no flash, no ghosting — for b/w mode)
    "refresh_border_mm": 5,     # partial-refresh area expansion in mm (0 = none; old content kept in the border zone)
    "fullscreen_on_dim": False, # when True, force a full-screen clean refresh when the event set changes
    "full_refresh_deploy": 3,   # number of GC16 clean refresh passes on startup/deploy (1-5)
    "full_refresh_day_change": 2, # number of GC16 clean refresh passes on day change (1-5)
    "full_refresh_interval": 1, # number of GC16 clean refresh passes on interval elapsed (1-5)
    "full_refresh_event_end": 1, # number of GC16 clean refresh passes on event finish / fullscreen_on_dim (1-5)
    "full_refresh_manual": 1,  # number of GC16 clean refresh passes on manual Save & Render (1-5)
    "regional_hard_flashes": 1, # number of flash+draw cycles for regional hard mode updates (1-5)
    "show_time_line": True,    # show the current-time line indicator on week/7days views
    "time_line_style": "dotted", # "solid" (thick line), "dotted" (default), "wavy"
    "bw_mode": False,          # b/w 1-bit mode: renders everything in black/white, thresholds the image so DU mode can be used (no ghosting/darkening)
    "dim_style": "normal",     # "normal" (white fill + black border for dimmed) or "checkerboard" (1px B/W checkerboard pattern)
    "brightness": 1.4,            # gamma boost for e-ink
    "timezone": "",               # IANA timezone, empty = system default
    "time_format": "24h",         # "24h" or "12h"
    "date_format": "",            # strftime format: "" | "%B %Y" | "%B %d, %Y" | "%Y.%m.%d %a" | "%d %B %Y"
    "dim_past_events": False,     # dim past events on the display
    "crossed_event_dim": False,   # dim events when time line crosses them
    "show_descriptions": True,    # show event descriptions under the title/time on cards
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
            # Migrate legacy hard_refresh_count → full_refresh_manual/interval/event_end
            if "hard_refresh_count" in merged:
                hc = merged.pop("hard_refresh_count", 1)
                if "full_refresh_manual" not in data:
                    merged["full_refresh_manual"] = hc
                if "full_refresh_interval" not in data:
                    merged["full_refresh_interval"] = hc
                if "full_refresh_event_end" not in data:
                    merged["full_refresh_event_end"] = hc
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