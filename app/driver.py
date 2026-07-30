"""Bridge to the C IT8951 driver binary.

Renders a PIL image to PNG, then calls the C driver to display it.
Supports regional differential updates (--soft/--hard/--smooth) that
compare against the last displayed image and only refresh the changed
region plus a dithered border.

The C driver applies Floyd-Steinberg dithering in the border zone,
blending from OLD pixels (outer edge — clean, sparse dots) to NEW
pixels (inner edge — full new content). Because the outer edge keeps
the old (already-clean) pixels, no dithering noise is written to
untouched screen area, so repeated regional updates do not darken the
rest of the calendar over time. The dithering border is adjustable via
the dither_border_mm setting.

Full-screen clean refreshes are only ever triggered by force_full (day
change, full-refresh interval, event add/remove, or manual Save & Render)
— regional updates never refresh the whole screen.
"""
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("eink.driver")

_last_render_time = 0.0
_last_full_refresh = 0.0  # timestamp of last forced full refresh (set on first render)

# Screen: 1872×1404 px, 158×118.5 mm → ~11.85 px/mm
PX_PER_MM = 11.85


def render_to_screen(pil_image, brightness: float = 1.4, force_full: bool = False,
                      smooth: bool = False, update_mode: str = "soft",
                      dither_border_mm: float = 5) -> bool:
    """Display a PIL image on the e-ink screen via the C driver.

    update_mode: regional update flavour for non-full refreshes —
                 "soft"   (GL16 regional, no flash, dithering visible) [default],
                 "hard"   (white-flash inner changed area + GL16 dithered border),
                 "smooth" (A2 1-bit, no flash, fastest — dithering NOT visible).
                 "fullscreen" is accepted as an alias for forcing a full refresh
                 and is handled via force_full below (never as a regional mode).
    dither_border_mm: dithering border in mm — blends old→new content with
                      Floyd-Steinberg dithering (maximal noise near the refresh
                      region, fading to sparse dots at the outer edge).
                      Converted to px at ~11.85 px/mm.

    force_full: full-screen clean refresh. Deletes the diff cache so the C
                driver's diff path finds no previous image → does a full GL16
                refresh AND saves the cache, preserving it for the next
                regional update's dithering. This is the ONLY path that touches
                the whole screen; regional updates never call it.

    Regional update principle: only the changed bounding box (+ a dithered
    border) is rewritten. The border blends old→new, so untouched screen area
    keeps its old (clean) pixels — no dark dithering noise accumulates outside
    the refreshed region over time.
    """
    global _last_render_time, _last_full_refresh
    import time

    binary = config.IT8951_BINARY
    if not Path(binary).exists():
        logger.error("IT8951 binary not found at %s", binary)
        return False

    tmp_path = config.TMP_DIR / "render.png"
    pil_image.save(str(tmp_path), "PNG")

    # Convert mm to px for border-smooth
    border_px = int(dither_border_mm * PX_PER_MM) if dither_border_mm > 0 else 0

    # ---- Full-screen clean refresh (day change / interval / event change / manual) ----
    # Delete the diff cache: the C driver finds no previous image → full GL16
    # refresh of the whole screen AND re-saves the cache. This is the only
    # branch that refreshes the whole screen; regional updates never do this.
    # "fullscreen" update_mode is treated as an explicit full-refresh request.
    if force_full or update_mode == "fullscreen":
        try:
            os.remove("/tmp/it8951_last.png")
        except OSError:
            pass
        cmd = [binary, "--image", str(tmp_path),
               "--brightness", str(brightness),
               "--hard", "--border-smooth", "0"]
        logger.info("Full screen refresh (forced, cache-preserving)")
    else:
        # ---- Regional differential update (only changed region + dithered border) ----
        # soft:   GL16, no flash — dithering visible in the border zone.
        # hard:   white-flash the inner changed area, GL16 the dithered border.
        # smooth: A2 1-bit, no flash, fastest — dithering NOT visible.
        if update_mode == "hard":
            # Hard flash should stay small: cap the border so the flash + dither
            # zone is compact (~2 mm). Soft/smooth keep the full configured border.
            regional_border = min(border_px, 24)
            mode_flag = "--hard"
        elif update_mode == "smooth":
            regional_border = border_px
            mode_flag = "--smooth"
        else:  # "soft" (default)
            regional_border = border_px
            mode_flag = "--soft"
        cmd = [binary, "--image", str(tmp_path),
               "--brightness", str(brightness),
               mode_flag, "--border-smooth", str(regional_border)]
        logger.info("Regional %s update (border=%dpx / %.1fmm)",
                    update_mode, regional_border, dither_border_mm)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error("IT8951 driver error: %s", result.stderr[-500:])
            return False
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                if line.startswith("diff:"):
                    logger.info("Driver: %s", line)
        _last_render_time = time.time()
        if force_full or update_mode == "fullscreen" or _last_full_refresh == 0.0:
            _last_full_refresh = _last_render_time
        logger.info("Rendered to screen (%.1fs)", time.time() - (_last_render_time - 0.001))
        return True
    except subprocess.TimeoutExpired:
        logger.error("IT8951 driver timed out")
        return False
    except Exception as e:
        logger.error("IT8951 driver exception: %s", e)
        return False


def needs_full_refresh(interval_hours: float = 0) -> bool:
    """Check if a full refresh is needed based on interval (hours).
    Returns True if interval has elapsed since last full refresh.
    interval_hours=0 means never force by interval (only day change triggers it)."""
    import time
    if interval_hours <= 0:
        return False
    if _last_full_refresh == 0.0:
        return False  # haven't done any render yet, don't force on first
    return (time.time() - _last_full_refresh) >= interval_hours * 3600


def render_clear() -> bool:
    """Clear the screen to white."""
    binary = config.IT8951_BINARY
    if not Path(binary).exists():
        return False
    try:
        subprocess.run([binary, "--clear"], capture_output=True, timeout=30)
        return True
    except Exception:
        return False


def render_info() -> Optional[dict]:
    """Get device info from the C driver."""
    binary = config.IT8951_BINARY
    if not Path(binary).exists():
        return None
    try:
        result = subprocess.run(
            [binary, "--info"], capture_output=True, text=True, timeout=10
        )
        info = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                info[key.strip().lower()] = val.strip()
        return info
    except Exception:
        return None