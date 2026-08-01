"""Bridge to the C IT8951 driver binary.

Renders a PIL image to PNG, then calls the C driver to display it.
Supports regional differential updates (--soft/--hard) that compare
against the last displayed image and only refresh the changed region
plus an expansion border. No dithering is applied: the border zone
keeps the OLD pixels, so a soft regional update "considers the old
state before the partial refresh" — only the inner changed area is
visually updated. The expansion width is adjustable via the
refresh_border_mm setting (partial-refresh area expansion).

Full-screen clean refreshes (--fullscreen, GC16) are only triggered by
force_full (day change, full-refresh interval, dim-events toggle, or
manual Save & Render) — never during regional updates.
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
                      refresh_border_mm: float = 5,
                      full_refresh_repeats: int = 1) -> bool:
    """Display a PIL image on the e-ink screen via the C driver.

    update_mode: regional update flavour for non-full refreshes —
                 "soft" (GL16 regional, no flash, old state preserved at the
                          border — recommended) [default],
                 "hard" (white-flash inner changed area + GL16 region).
                 Whole-screen refreshes are governed solely by force_full.
    refresh_border_mm: partial-refresh area expansion in mm — the changed
                      region is expanded by this many px on each side; the
                      border zone keeps the old content (no dithering), so
                      only the inner changed area visibly updates.
                      Converted to px at ~11.85 px/mm.
    force_full: full-screen GC16 clean refresh (removes ghosting). The C
                driver's --fullscreen mode does a full clean refresh AND
                saves the diff cache for the next regional update. This is
                the ONLY path that touches the whole screen; regional
                updates never call it.
    full_refresh_repeats: how many times to run the full-screen GC16 clean
                refresh when force_full is True. Multiple passes clear
                ghosting more thoroughly (e.g. 3 on deploy, 2 on day change).

    Regional update principle: only the changed bounding box (+ an expansion
    border that keeps old pixels) is rewritten, so untouched screen area is
    never modified and no noise accumulates outside the refreshed region.
    """
    global _last_render_time, _last_full_refresh
    import time

    binary = config.IT8951_BINARY
    if not Path(binary).exists():
        logger.error("IT8951 binary not found at %s", binary)
        return False

    tmp_path = config.TMP_DIR / "render.png"
    pil_image.save(str(tmp_path), "PNG")

    # Convert mm to px for border-smooth (partial-refresh area expansion)
    border_px = int(refresh_border_mm * PX_PER_MM) if refresh_border_mm > 0 else 0

    # ---- Full-screen clean refresh (day change / interval / dim toggle / manual) ----
    # --fullscreen makes the C driver do a full GC16 clean refresh and re-save
    # the diff cache, so the next regional update has a correct baseline.
    # Multiple passes clear ghosting more thoroughly.
    if force_full:
        repeats = max(1, full_refresh_repeats)
        cmd = [binary, "--image", str(tmp_path),
               "--brightness", str(brightness),
               "--fullscreen", "--border-smooth", "0"]
        logger.info("Full screen GC16 clean refresh (forced, %dx)", repeats)
        for i in range(repeats):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    logger.error("IT8951 driver error (pass %d/%d): %s",
                                 i + 1, repeats, result.stderr[-500:])
                    return False
                if result.stdout:
                    for line in result.stdout.strip().splitlines():
                        if line.startswith("diff:"):
                            logger.info("Driver: %s (pass %d/%d)", line, i + 1, repeats)
            except subprocess.TimeoutExpired:
                logger.error("IT8951 driver timed out (pass %d/%d)", i + 1, repeats)
                return False
            except Exception as e:
                logger.error("IT8951 driver exception (pass %d/%d): %s", i + 1, repeats, e)
                return False
        _last_render_time = time.time()
        if force_full or _last_full_refresh == 0.0:
            _last_full_refresh = _last_render_time
        logger.info("Rendered to screen (%.1fs, %d full refresh passes)",
                    time.time() - (_last_render_time - 0.001), repeats)
        return True

    # ---- Regional differential update (only changed region + expansion border) ----
    # soft:   GL16, no flash — border keeps old pixels (old state preserved).
    # hard:   white-flash the inner changed area, GL16 the region.
    if update_mode == "hard":
        # Keep the flash + region compact: cap the expansion for hard mode.
        regional_border = min(border_px, 24)
        mode_flag = "--hard"
    else:  # "soft" (default)
        regional_border = border_px
        mode_flag = "--soft"
    cmd = [binary, "--image", str(tmp_path),
           "--brightness", str(brightness),
           mode_flag, "--border-smooth", str(regional_border)]
    logger.info("Regional %s update (expansion=%dpx / %.1fmm)",
                update_mode, regional_border, refresh_border_mm)
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
        if force_full or _last_full_refresh == 0.0:
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