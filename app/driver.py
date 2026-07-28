"""Bridge to the C IT8951 driver binary.

Renders a PIL image to PNG, then calls the C driver to display it.
Supports regional differential updates (--soft/--hard) that compare
against the last displayed image and only refresh changed regions.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from . import config

logger = logging.getLogger("eink.driver")

_last_render_time = 0.0
_use_diff = True  # Use regional diff update by default
_last_full_refresh = 0.0  # timestamp of last forced full refresh (set on first render)

# Screen: 1872×1404 px, 158×118.5 mm → ~11.85 px/mm
PX_PER_MM = 11.85


def render_to_screen(pil_image, brightness: float = 1.4, force_full: bool = False,
                      smooth: bool = False, update_mode: str = "soft",
                      dither_border_mm: float = 5) -> bool:
    """Display a PIL image on the e-ink screen via the C driver.

    update_mode: "hard" (full screen, no diff) or "soft" (regional diff GC16)
    dither_border_mm: dithering border in mm (converted to px at ~11.85 px/mm)
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

    # Full refresh (fullscreen mode, day change, interval, or Save & Render)
    if force_full:
        import os
        try:
            os.remove("/tmp/it8951_last.png")
        except OSError:
            pass
        cmd = [binary, "--image", str(tmp_path), "--brightness", str(brightness)]
        logger.info("Full screen refresh (forced)")
    elif smooth:
        # Smooth minute update: respect update_mode
        # Hard mode: smaller border (GL16 handles transitions cleanly)
        # Smooth mode: full dithering border
        if update_mode == "hard":
            smooth_border = min(border_px, 24)  # cap at ~2mm for hard flash
        else:
            smooth_border = border_px
        if update_mode == "hard":
            # Hard regional: GL16 with white flash in changed area (small border)
            cmd = [binary, "--image", str(tmp_path),
                   "--brightness", str(brightness),
                   "--hard", "--border-smooth", str(smooth_border)]
        elif update_mode == "fullscreen":
            # Fullscreen: GC16 full screen (delete diff cache)
            import os
            try:
                os.remove("/tmp/it8951_last.png")
            except OSError:
                pass
            cmd = [binary, "--image", str(tmp_path), "--brightness", str(brightness)]
            logger.info("Fullscreen refresh (smooth+fullscreen mode)")
        else:
            # Smooth regional: A2, no blink
            cmd = [binary, "--image", str(tmp_path),
                   "--brightness", str(brightness),
                   "--soft", "--border-smooth", str(border_px)]
    elif _use_diff and update_mode == "hard":
        # Hard regional: GL16 with white flash in changed area only
        cmd = [binary, "--image", str(tmp_path),
               "--brightness", str(brightness),
               "--hard", "--border-smooth", str(border_px)]
    elif _use_diff:
        # Smooth regional: A2, no blink
        cmd = [binary, "--image", str(tmp_path),
               "--brightness", str(brightness),
               "--soft", "--border-smooth", str(border_px)]
    else:
        cmd = [binary, "--image", str(tmp_path), "--brightness", str(brightness)]
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