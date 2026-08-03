"""Calendar rendering — composes views to PIL images for the e-ink screen.

All rendering targets 1872x1404 (Waveshare 7.8" IT8951).
Supports month / week / 7-days views with a current-time indicator line.
"""
import datetime
import logging
import math
import random
import re
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from . import config

logger = logging.getLogger("eink.render")

# Screen dimensions
W = config.SCREEN_W
H = config.SCREEN_H

# Margins (pixels)
MARGIN = 78   # left margin for hour labels
RIGHT_PAD = 10  # right edge padding
HEADER_H = 120
FOOTER_H = 30

# Fonts — try DejaVu, fall back to default
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_SIZE_MODIFIER = 0  # global font size adjustment, set before each render


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Get a cached font instance, adjusted by global size modifier.

    Always uses the regular (non-bold) font — bold fonts produce 3-5px wide
    strokes that the IT8951 GC16 waveform doubles/splits. The regular font
    produces 2px strokes that render cleanly. Size is increased by 1 when
    bold was requested, to partially compensate for the thinner weight.
    """
    size = max(4, size + _SIZE_MODIFIER + (1 if bold else 0))
    path = _FONT_PATHS[0]  # always regular — bold strokes get doubled by GC16
    key = (path, size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _font_heavy(size: int) -> ImageFont.FreeTypeFont:
    """Real bold font. Only safe in 1-bit (b/w) mode, which renders via DU and
    the 2px-grid snap — there the extra weight keeps white-on-black text legible
    (thin regular strokes get eroded by the snap and thresholding). Do NOT use in
    grayscale/GC16 mode, where bold strokes double/split."""
    size = max(4, size + _SIZE_MODIFIER)
    path = _FONT_PATHS[1]  # DejaVuSans-Bold
    key = (path, size)
    if key not in _FONT_CACHE:
        try:
            _FONT_CACHE[key] = ImageFont.truetype(path, size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default()
    return _FONT_CACHE[key]


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _text_h(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def _clean_desc(s: str) -> str:
    """Flatten a (possibly HTML) event description to a single wrapped-able line
    of plain text. Google descriptions often contain HTML/newlines."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)          # strip tags
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def _clean_location(s: str) -> str:
    """Shorten a Google Calendar location to just the building name or street
    address — drop postal code, city, state, and country.

    Google locations are typically comma-separated, e.g.:
      "Conference Room A, 123 Main St, Springfield, IL 62704, USA"
    The first comma-separated part is usually a building/room name. If it
    looks like a street address (contains a number), keep the first two parts
    (number + street). Otherwise keep just the first part. Postal codes
    (\\d{4,6}) and country names are always stripped from the result."""
    if not s:
        return ""
    s = _clean_desc(s)
    if not s:
        return ""
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        return ""
    # If the first part looks like a street address (starts with a number),
    # keep building + street (first two parts).
    import re as _re
    if _re.match(r"^\d+\b", parts[0]) and len(parts) >= 2:
        result = parts[0] + ", " + parts[1]
    else:
        result = parts[0]
    # Strip any trailing postal code or country fragment that survived
    result = _re.sub(r"\b\d{4,6}\b", "", result).strip(" ,")
    return result


# ---- Exact (non-antialiased) drawing helpers for b/w mode ----
def _hline(draw, x0: int, y: int, x1: int, fill, width: int = 1):
    """Draw a horizontal line using rectangle fill (no PIL line AA)."""
    if width == 1:
        draw.rectangle([(x0, y), (x1, y)], fill=fill)
    else:
        half = width // 2
        draw.rectangle([(x0, y - half), (x1, y + width - half - 1)], fill=fill)


def _vline(draw, x: int, y0: int, y1: int, fill, width: int = 1):
    """Draw a vertical line using rectangle fill (no PIL line AA)."""
    if width == 1:
        draw.rectangle([(x, y0), (x, y1)], fill=fill)
    else:
        half = width // 2
        draw.rectangle([(x - half, y0), (x + width - half - 1, y1)], fill=fill)


def _hsegments(draw, x0: int, x1: int, y: int, fill,
               step: int, seg_len: int, width: int = 1):
    """Draw a dotted/dashed horizontal line with exact rectangle segments."""
    for sx in range(x0, x1 + 1, step):
        ex = min(sx + seg_len - 1, x1)
        _hline(draw, sx, y, ex, fill, width)


def _vsegments(draw, x: int, y0: int, y1: int, fill,
               step: int, seg_len: int, width: int = 1):
    """Draw a dotted/dashed vertical line with exact rectangle segments."""
    for sy in range(y0, y1 + 1, step):
        ey = min(sy + seg_len - 1, y1)
        _vline(draw, x, sy, ey, fill, width)


# ---- Color helpers (grayscale for e-ink: (0,0,0)=black, (255,255,255)=white) ----
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
GRAY_DARK = (60, 60, 60)
GRAY_MID = (120, 120, 120)
GRAY_LIGHT = (200, 200, 200)
GRAY_VLIGHT = (239, 239, 239)
GRAY_DIM = (153, 153, 153)
GRAY_HOUR_LINE = (170, 170, 170)


def render_calendar(view_mode: str, events: list[dict],
                    day_start: str, day_end: str,
                    max_full_day: int, time_format: str = "24h",
                    date_format: str = "",
                    settings_url: str = "",
                    crossed_event_dim: bool = False,
                    dim_past_events: bool = False,
                    text_size_modifier: int = 0,
                    show_time_line: bool = True,
                    time_line_style: str = "dotted",
                    bw_mode: bool = False,
                    dim_style: str = "normal",
                    show_descriptions: bool = True,
                    text_outline_width: int = 5,
                    brightness: float = 1.4,
                    now: Optional[datetime.datetime] = None) -> Image.Image:
    """Render the full calendar view to a PIL Image.

    Returns an RGB image (will be converted to grayscale by the C driver).
    bw_mode: when True, render in 1-bit (black/white only) and threshold the
             final image so DU mode can be used without ghosting accumulation.
    dim_style: "normal" (white fill + dotted border) or "checkerboard" (1px
               alternating B/W pattern fill for dimmed events).
    """
    global _SIZE_MODIFIER
    _SIZE_MODIFIER = text_size_modifier

    if now is None:
        now = datetime.datetime.now()

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    ds_h, ds_m = (int(x) for x in day_start.split(":"))
    de_h, de_m = (int(x) for x in day_end.split(":"))

    if view_mode == "month":
        _render_month(draw, events, now, max_full_day, date_format=date_format, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)
    elif view_mode == "35days":
        _render_35days(draw, events, now, max_full_day, date_format=date_format, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style)
    elif view_mode == "7days":
        week_num = now.isocalendar()[1]
        _render_7days(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format, date_format=date_format, week_num=week_num,
                      crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style,
                      show_descriptions=show_descriptions, text_outline_width=text_outline_width, brightness=brightness)
    elif view_mode == "5days":
        _render_5days(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format, date_format=date_format,
                      crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style,
                      show_descriptions=show_descriptions, text_outline_width=text_outline_width, brightness=brightness)
    else:  # week (default)
        _render_week(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style,
                     show_descriptions=show_descriptions, text_outline_width=text_outline_width, brightness=brightness)

    # Draw current-time line on week/7days/5days views
    if view_mode in ("week", "7days", "5days") and show_time_line:
        _draw_time_line(draw, now, view_mode, day_start, day_end, events, time_format,
                        style=time_line_style, bw_mode=bw_mode)

    # Settings URL (centered between title and subtitle on header line)
    if settings_url:
        url_font = _font(28)
        uw = _text_w(draw, settings_url, url_font)
        # Compute center between title right edge and subtitle left edge
        font_title = _font(64, bold=True)
        title_str = now.strftime(date_format) if date_format else (
            now.strftime("%B %Y") if view_mode in ("month", "35days") else (
            now.strftime("%B %d, %Y") if view_mode == "week" else (
            "Next 5 Days" if view_mode == "5days" else "Next 7 Days")))
        tw = _text_w(draw, title_str, font_title)
        title_right = MARGIN + tw
        font_sub = _font(36)
        sub_str = f"Week {now.isocalendar()[1]}" if view_mode in ("week", "7days", "5days") else (
            f"Week {now.isocalendar()[1]} — 35 days" if view_mode == "35days" else "")
        if sub_str:
            sw = _text_w(draw, sub_str, font_sub)
            sub_left = W - MARGIN - sw
        else:
            sub_left = W - MARGIN
        ip_center = (title_right + sub_left) // 2
        # Vertically align with the title's top edge (title_y), matching the
        # subtitle alignment in _draw_header. Compute the same title_y the view
        # renderer used so full-day-bar lifting is honoured.
        if view_mode in ("week", "7days", "5days"):
            if view_mode == "week":
                _start_date = now.date() - datetime.timedelta(days=now.date().weekday())
            else:
                _start_date = now.date()
            _title_y = _fullday_title_y(draw, title_str, font_title, events, _start_date, max_full_day)
        else:
            _title_y = 20
        draw.text((ip_center - uw // 2, _title_y), settings_url, fill=GRAY_MID, font=url_font)

    # b/w mode: threshold the entire image to pure 1-bit black/white.
    # (In grayscale mode we intentionally keep the real gray levels — see below.)
    if bw_mode:
        # Always hard-threshold to pure 1-bit — never Floyd-Steinberg. b/w
        # content is already black/white (dim events use explicit white fills /
        # checkerboard), so dithering adds nothing and actively shreds thin
        # white-on-black text: it breaks up 'W' strokes and fills in the
        # counters of '0'/'e'/'a'. Hard threshold keeps glyphs crisp.
        gray = img.convert("L")
        bw = gray.point(lambda x: 0 if x < 128 else 255, "L")
        img = bw.convert("RGB")
    # else: grayscale mode — keep the palette's real gray levels (event fills,
    # dim shading, grid/hour lines). Previously this branch hard-thresholded
    # everything at 201 to dodge GL16 gray-edge ghosting, which flattened all
    # event shading. That artifact is now handled at the driver level by the
    # 2px-grid snap (snap_to_2px_grid), so the image is passed through with its
    # 16-level grays intact and rendered natively by GC16 (full) / GL16 (region).

    return img


# ---- Header ----
def _fullday_title_y(draw, title, title_font, events, start_date, max_full_day,
                     default_y=20):
    """Lift the header title so it clears a raised full-day bar.

    In week/7-day views a column whose full-day-event count is 1 or 3 draws its
    top bar up to ~y80 (above the header line) — which the tall title would
    overlap. When that happens in the first 3 columns, center the title between
    the top edge and the bar top. (Count 2 keeps the bar at the header line, so
    no lift is needed.)"""
    counts = {}
    for ev in events:
        if not ev.get("all_day"):
            continue
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        counts[d] = counts.get(d, 0) + 1
    BAR_TOP = 80
    raised = False
    for i in range(3):
        c = min(counts.get(start_date + datetime.timedelta(days=i), 0), max_full_day)
        if c == 1 or c == 3:
            raised = True
            break
    if not raised:
        return default_y
    # Center the title's glyphs vertically in the band between the screen top
    # edge (y=0) and the full-day card top edge (BAR_TOP).
    bb = draw.textbbox((0, 0), title, font=title_font)
    return max(0, int(round(BAR_TOP / 2 - (bb[1] + bb[3]) / 2)))


def _draw_header(draw: ImageDraw.ImageDraw, title: str, subtitle: str = "",
                 title_y: int = 20):
    """Draw the page header with title (left) and subtitle (right)."""
    # Title
    font_title = _font(64, bold=True)
    draw.text((MARGIN, title_y), title, fill=BLACK, font=font_title)

    # Subtitle (e.g. week number or date range) — top-aligned with the title
    if subtitle:
        font_sub = _font(36)
        sw = _text_w(draw, subtitle, font_sub)
        draw.text((W - MARGIN - sw, title_y), subtitle, fill=GRAY_DARK, font=font_sub)

    # Header separator line
    y = HEADER_H - 10
    _hline(draw, MARGIN, y, W - MARGIN, GRAY_MID, width=2)


# ---- Month view ----
def _render_month(draw, events, now, max_full_day, date_format="", dim_past_events=False, bw_mode=False, dim_style="normal"):
    """Month grid view — weeks as rows, days as columns."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = now.strftime("%B %Y")
    _draw_header(draw, title)

    today = now.date()
    # Find first day of month and the starting grid cell
    first = today.replace(day=1)
    # Monday=0
    start_weekday = first.weekday()
    grid_start = first - datetime.timedelta(days=start_weekday)

    # Number of weeks to show (usually 5-6)
    next_month = (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    last_day = (next_month - datetime.timedelta(days=1)).day
    total_days = start_weekday + last_day
    num_weeks = math.ceil(total_days / 7)

    grid_x = 19  # moved left by 11mm total
    grid_y = HEADER_H + 10
    grid_w = W - grid_x - RIGHT_PAD
    grid_h = H - grid_y - FOOTER_H
    col_w = grid_w // 7
    row_h = grid_h // num_weeks

    # Day-of-week headers
    dow_font = _font(28, bold=True)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, dow in enumerate(dows):
        cx = grid_x + i * col_w + col_w // 2
        tw = _text_w(draw, dow, dow_font)
        draw.text((cx - tw // 2, grid_y), dow, fill=GRAY_DARK, font=dow_font)
    grid_y += 36

    # Events indexed by date
    events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            events_by_date.setdefault(d, []).append(ev)
        else:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            events_by_date.setdefault(d, []).append(ev)

    # Grid cells
    cell_font = _font(32, bold=True)
    event_font = _font(22)
    event_bold = _font(22, bold=True)
    # Outer left + top border (cells only draw right+bottom edges)
    _vline(draw, grid_x, grid_y, grid_y + num_weeks * row_h - 1, GRAY_LIGHT, width=1)
    _hline(draw, grid_x, grid_y, grid_x + 7 * col_w - 1, GRAY_LIGHT, width=1)
    day_num = grid_start
    for week in range(num_weeks):
        for col in range(7):
            x = grid_x + col * col_w
            y = grid_y + week * row_h

            # Cell border — draw only RIGHT and BOTTOM edges to avoid doubling
            # (a full rectangle per cell draws shared edges twice, 1px apart).
            # Strong (thick 3px) at month boundaries; thin otherwise.
            next_day = day_num + datetime.timedelta(days=1)
            next_week = day_num + datetime.timedelta(days=7)
            right_strong = col < 6 and next_day.month != day_num.month
            bottom_strong = week < num_weeks - 1 and next_week.month != day_num.month
            if right_strong or bottom_strong:
                if right_strong:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, BLACK, width=3)
                if bottom_strong:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, BLACK, width=3)
                # Non-strong edges as thin lines
                if not right_strong and col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if not bottom_strong and week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)
            elif bw_mode:
                # Dotted cell border in b/w mode (2px dot, 6px gap).
                # Draw ONLY the dotted segments — no solid rectangle underneath.
                # Right edge (dotted vertical)
                if col < 6 and not right_strong:
                    _vsegments(draw, x + col_w - 1, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                # Bottom edge (dotted horizontal)
                if week < num_weeks - 1 and not bottom_strong:
                    _hsegments(draw, x, x + col_w - 1, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                # Left + top edges: only draw if this is the first cell (col=0/week=0)
                if col == 0:
                    _vsegments(draw, x, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if week == 0:
                    _hsegments(draw, x, x + col_w - 1, y, BLACK, step=8, seg_len=2, width=1)
            else:
                # Non-bw, non-month-boundary: draw only RIGHT + BOTTOM edges
                if col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)

            # Day number
            in_month = day_num.month == today.month
            is_today = day_num == today
            if dim_past_events and day_num < today:
                color = GRAY_DIM
            else:
                color = BLACK if in_month else GRAY_MID
            day_str = str(day_num.day)
            if is_today:
                # Highlight today — full cell width, compact height around text
                bb = draw.textbbox((0, 0), day_str, font=cell_font)
                tw = bb[2] - bb[0]
                th = bb[3] - bb[1]
                pad = 3
                rect_top = y + 6 + bb[1] - pad
                rect_bot = y + 6 + bb[3] + pad
                draw.rectangle([x + 1, rect_top, x + col_w - 2, rect_bot], fill=BLACK)
                draw.text((x + 10, y + 6), day_str, fill=(255, 255, 255), font=cell_font)
                # Dotted cell border — skip edges that already have strong month-boundary line
                prev_day = day_num - datetime.timedelta(days=1)
                prev_week = day_num - datetime.timedelta(days=7)
                left_strong = col > 0 and prev_day.month != day_num.month
                top_strong = week > 0 and prev_week.month != day_num.month
                dot_step = 27
                dot_r = 4
                if bw_mode:
                    # Square dots (no AA) in b/w mode
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
                else:
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
            else:
                draw.text((x + 10, y + 6), day_str, fill=color, font=cell_font)

            # Events for this day (wrap by syllables, fill cell as space permits)
            day_events = events_by_date.get(day_num, [])
            cell_avail_w = col_w - 16
            visible_events = [ev for ev in day_events if ev.get("summary", "") not in ("", "(No title)")]
            ey = y + 48
            ev_fill = GRAY_DIM if (dim_past_events and day_num < today) else GRAY_DARK
            ev_idx = 0
            while ev_idx < len(visible_events) and ey + 26 <= y + row_h - 4:
                ev = visible_events[ev_idx]
                ev_time = _ev_time_str(ev, now)
                if ev_time:
                    time_w = _text_w(draw, ev_time + " ", event_bold)
                    draw.text((x + 8, ey), ev_time + " ", fill=ev_fill, font=event_bold)
                    name = ev["summary"]
                    if time_w + _text_w(draw, name, event_font) > cell_avail_w:
                        while len(name) > 2 and time_w + _text_w(draw, name + "…", event_font) > cell_avail_w:
                            name = name[:-1]
                        name += "…"
                    draw.text((x + 8 + time_w, ey), name, fill=ev_fill, font=event_font)
                else:
                    # All-day event — wrap summary to fit cell width
                    wrapped = _wrap_text_lines(draw, ev["summary"], event_font, cell_avail_w)
                    if wrapped:
                        draw.text((x + 8, ey), wrapped[0], fill=ev_fill, font=event_font)
                ey += 26
                ev_idx += 1
            if ev_idx < len(visible_events) and ey + 26 > y + row_h - 4:
                remaining = len(visible_events) - ev_idx
                draw.text((x + 8, ey), f"+{remaining}", fill=ev_fill, font=_font(18))

            day_num += datetime.timedelta(days=1)


def _render_35days(draw, events, now, max_full_day, date_format="", dim_past_events=False, bw_mode=False, dim_style="normal"):
    """35-days view — 5 weeks starting from current week's Monday.

    Shows a month-like grid with the current week as the top row.
    Includes month separator lines between months.
    """
    if date_format:
        title = now.strftime(date_format)
    else:
        title = now.strftime("%B %Y")
    week_num = now.isocalendar()[1]
    _draw_header(draw, title, f"Week {week_num} — 35 days")

    today = now.date()
    # Start from Monday of current week
    start_date = today - datetime.timedelta(days=today.weekday())
    num_weeks = 5

    grid_x = 19  # moved left by 11mm total
    grid_y = HEADER_H + 10
    grid_w = W - grid_x - RIGHT_PAD
    grid_h = H - grid_y - FOOTER_H
    col_w = grid_w // 7
    row_h = grid_h // num_weeks

    # Day-of-week headers
    dow_font = _font(28, bold=True)
    dows = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, dow in enumerate(dows):
        cx = grid_x + i * col_w + col_w // 2
        tw = _text_w(draw, dow, dow_font)
        draw.text((cx - tw // 2, grid_y), dow, fill=GRAY_DARK, font=dow_font)
    grid_y += 36

    # Events indexed by date
    events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        events_by_date.setdefault(d, []).append(ev)

    # Grid cells
    cell_font = _font(32, bold=True)
    event_font = _font(22)
    event_bold = _font(22, bold=True)
    # Outer left + top border (cells only draw right+bottom edges)
    _vline(draw, grid_x, grid_y, grid_y + num_weeks * row_h - 1, GRAY_LIGHT, width=1)
    _hline(draw, grid_x, grid_y, grid_x + 7 * col_w - 1, GRAY_LIGHT, width=1)
    day_num = start_date
    for week in range(num_weeks):
        for col in range(7):
            x = grid_x + col * col_w
            y = grid_y + week * row_h

            # Cell border — draw only RIGHT and BOTTOM edges to avoid doubling
            next_day = day_num + datetime.timedelta(days=1)
            next_week = day_num + datetime.timedelta(days=7)
            right_strong = col < 6 and next_day.month != day_num.month
            bottom_strong = week < num_weeks - 1 and next_week.month != day_num.month
            if right_strong or bottom_strong:
                if right_strong:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, BLACK, width=3)
                if bottom_strong:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, BLACK, width=3)
                if not right_strong and col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if not bottom_strong and week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)
            elif bw_mode:
                # Dotted cell border — draw ONLY dots, no solid rectangle
                if col < 6 and not right_strong:
                    _vsegments(draw, x + col_w - 1, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if week < num_weeks - 1 and not bottom_strong:
                    _hsegments(draw, x, x + col_w - 1, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if col == 0:
                    _vsegments(draw, x, y, y + row_h - 1, BLACK, step=8, seg_len=2, width=1)
                if week == 0:
                    _hsegments(draw, x, x + col_w - 1, y, BLACK, step=8, seg_len=2, width=1)
            else:
                # Non-bw, non-month-boundary: draw only RIGHT + BOTTOM edges
                if col < 6:
                    _vline(draw, x + col_w - 1, y, y + row_h - 1, GRAY_LIGHT, width=1)
                if week < num_weeks - 1:
                    _hline(draw, x, y + row_h - 1, x + col_w - 1, GRAY_LIGHT, width=1)

            # Day number
            is_today = day_num == today
            color = GRAY_DIM if (dim_past_events and day_num < today and not is_today) else BLACK
            day_str = str(day_num.day)
            if is_today:
                # Highlight today — full cell width, compact height
                bb = draw.textbbox((0, 0), day_str, font=cell_font)
                th = bb[3] - bb[1]
                pad = 3
                rect_top = y + 6 + bb[1] - pad
                rect_bot = y + 6 + bb[3] + pad
                draw.rectangle([x + 1, rect_top, x + col_w - 2, rect_bot], fill=BLACK)
                draw.text((x + 10, y + 6), day_str, fill=(255, 255, 255), font=cell_font)
                # Dotted cell border — skip edges with strong month-boundary lines
                prev_day = day_num - datetime.timedelta(days=1)
                prev_week = day_num - datetime.timedelta(days=7)
                left_strong = col > 0 and prev_day.month != day_num.month
                top_strong = week > 0 and prev_week.month != day_num.month
                dot_step = 27
                dot_r = 4
                if bw_mode:
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.rectangle([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.rectangle([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
                else:
                    if not top_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y - dot_r, x + dx + dot_r, y + dot_r], fill=BLACK)
                    if not bottom_strong:
                        for dx in range(2, col_w - 1, dot_step):
                            draw.ellipse([x + dx - dot_r, y + row_h - 1 - dot_r, x + dx + dot_r, y + row_h - 1 + dot_r], fill=BLACK)
                    if not left_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x - dot_r, y + dy - dot_r, x + dot_r, y + dy + dot_r], fill=BLACK)
                    if not right_strong:
                        for dy in range(dot_step, row_h - 2, dot_step):
                            draw.ellipse([x + col_w - 1 - dot_r, y + dy - dot_r, x + col_w - 1 + dot_r, y + dy + dot_r], fill=BLACK)
            else:
                draw.text((x + 10, y + 6), day_str, fill=color, font=cell_font)

            # Events for this day (wrap by syllables, fill cell as space permits)
            day_events = events_by_date.get(day_num, [])
            cell_avail_w = col_w - 16
            visible_events = [ev for ev in day_events if ev.get("summary", "") not in ("", "(No title)")]
            ey = y + 48
            ev_fill = GRAY_DIM if (dim_past_events and day_num < today) else GRAY_DARK
            ev_idx = 0
            while ev_idx < len(visible_events) and ey + 26 <= y + row_h - 4:
                ev = visible_events[ev_idx]
                ev_time = _ev_time_str(ev, now)
                if ev_time:
                    time_w = _text_w(draw, ev_time + " ", event_bold)
                    draw.text((x + 8, ey), ev_time + " ", fill=ev_fill, font=event_bold)
                    name = ev["summary"]
                    if time_w + _text_w(draw, name, event_font) > cell_avail_w:
                        while len(name) > 2 and time_w + _text_w(draw, name + "…", event_font) > cell_avail_w:
                            name = name[:-1]
                        name += "…"
                    draw.text((x + 8 + time_w, ey), name, fill=ev_fill, font=event_font)
                else:
                    wrapped = _wrap_text_lines(draw, ev["summary"], event_font, cell_avail_w)
                    if wrapped:
                        draw.text((x + 8, ey), wrapped[0], fill=ev_fill, font=event_font)
                ey += 26
                ev_idx += 1
            remaining = len(visible_events) - ev_idx
            if remaining > 0 and ey + 26 > y + row_h - 4:
                draw.text((x + 8, ey), f"+{remaining}", fill=ev_fill, font=_font(18))

            day_num += datetime.timedelta(days=1)


def _render_week(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", date_format="", crossed_event_dim=False, dim_past_events=False, bw_mode=False, dim_style="normal", show_descriptions=True, text_outline_width=5, brightness=1.4):
    """Week view — 7 day columns with timed events stacked vertically."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = now.strftime("%B %d, %Y")
    week_num = now.isocalendar()[1]
    week_start = now.date() - datetime.timedelta(days=now.date().weekday())
    ty = _fullday_title_y(draw, title, _font(64, bold=True), events, week_start, max_full_day)
    _draw_header(draw, title, f"Week {week_num}", title_y=ty)

    _render_day_grid(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format=time_format, days=7, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style,
                     show_descriptions=show_descriptions, text_outline_width=text_outline_width, brightness=brightness)


# ---- 7-days view (next 7 days starting today) ----
def _render_7days(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", date_format="", crossed_event_dim=False, dim_past_events=False, week_num=None, bw_mode=False, dim_style="normal", show_descriptions=True, text_outline_width=5, brightness=1.4):
    """7-days view — starting from today, 7 consecutive day columns."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = "Next 7 Days"
    ty = _fullday_title_y(draw, title, _font(64, bold=True), events, now.date(), max_full_day)
    _draw_header(draw, title, f"Week {week_num}" if week_num else now.strftime("%a %b %d"), title_y=ty)

    _render_day_grid(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format=time_format, days=7, start_today=True, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style,
                     show_descriptions=show_descriptions, text_outline_width=text_outline_width, brightness=brightness)


def _render_5days(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", date_format="", crossed_event_dim=False, dim_past_events=False, bw_mode=False, dim_style="normal", show_descriptions=True, text_outline_width=5, brightness=1.4):
    """5-days view — starting from today, 5 consecutive day columns.
    Identical layout to 7-days but with fewer, wider columns and larger fonts."""
    if date_format:
        title = now.strftime(date_format)
    else:
        title = "Next 5 Days"
    week_num = now.isocalendar()[1]
    ty = _fullday_title_y(draw, title, _font(64, bold=True), events, now.date(), max_full_day)
    _draw_header(draw, title, f"Week {week_num}", title_y=ty)

    _render_day_grid(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format=time_format, days=5, start_today=True, date_format=date_format,
                     crossed_event_dim=crossed_event_dim, dim_past_events=dim_past_events, bw_mode=bw_mode, dim_style=dim_style,
                     show_descriptions=show_descriptions, font_scale=1.25, text_outline_width=text_outline_width, brightness=brightness)


def _render_day_grid(img, draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, time_format="24h", days=7, start_today=False, date_format="", crossed_event_dim=False, dim_past_events=False, bw_mode=False, dim_style="normal", show_descriptions=True, font_scale=1.0, text_outline_width=5, brightness=1.4):
    """Shared day-grid renderer for week, 7-days, and 5-days views."""
    today = now.date()

    if start_today:
        start_date = today
    else:
        # Start from Monday of current week
        start_date = today - datetime.timedelta(days=today.weekday())

    # Hour lines + labels — measure widest label first for dynamic left margin
    hour_font = _font(int(26 * font_scale), bold=True)
    max_label_w = 0
    for h in range(ds_h, de_h + 1):
        if time_format == "12h":
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            label = f"{h12} {ampm}"
        else:
            label = f"{h:02d}"
        lw = _text_w(draw, label, hour_font)
        if lw > max_label_w:
            max_label_w = lw

    left_margin = max(60, max_label_w + 14)  # dynamic left margin for hour labels
    label_rpad = 6  # gap between label right edge and grid
    grid_x = left_margin
    grid_y = HEADER_H + 50  # Leave room for full-day event strip
    grid_w = W - left_margin - RIGHT_PAD
    grid_h = H - grid_y - FOOTER_H + 20  # +~2mm bottom expansion

    col_w = grid_w // days
    ds_min = ds_h * 60 + ds_m
    de_min = de_h * 60 + de_m
    span_min = de_min - ds_min
    if span_min <= 0:
        span_min = 16 * 60  # fallback 16h
    minute_h = grid_h / span_min  # pixels per minute

    # Full-day events — build data index early
    fd_events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            fd_events_by_date.setdefault(d, []).append(ev)

    # Grid border
    grid_border_color = BLACK if bw_mode else GRAY_LIGHT
    draw.rectangle([grid_x, grid_y, grid_x + days * col_w - 1, grid_y + grid_h - 1],
                   outline=grid_border_color, width=1)

    # Hour lines + labels
    for h in range(ds_h, de_h + 1):
        y = int(grid_y + (h * 60 - ds_min) * minute_h)
        if y > grid_y + grid_h:
            break
        if bw_mode:
            # Dotted hour lines in b/w mode (4px period: 2px black, 2px white)
            _hsegments(draw, grid_x, grid_x + days * col_w, y, BLACK, step=4, seg_len=2, width=1)
        else:
            _hline(draw, grid_x, y, grid_x + days * col_w, GRAY_HOUR_LINE, width=1)
        if time_format == "12h":
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            label = f"{h12} {ampm}"
        else:
            label = f"{h:02d}"
        draw.text((grid_x - max_label_w - label_rpad, y - 14), label, fill=GRAY_MID, font=hour_font)

    # Column separators — thicker where month changes, extending up to header line
    sep_top = HEADER_H - 10  # header separator line
    for i in range(1, days):
        x = grid_x + i * col_w
        prev_d = start_date + datetime.timedelta(days=i - 1)
        curr_d = start_date + datetime.timedelta(days=i)
        if prev_d.month != curr_d.month:
            # Thick line at month boundary (same in all modes)
            _vline(draw, x, sep_top, grid_y + grid_h, BLACK, width=3)
        elif bw_mode:
            # Dotted vertical day separator in b/w mode (4px period)
            _vsegments(draw, x, grid_y, grid_y + grid_h, BLACK, step=4, seg_len=2, width=1)
        else:
            _vline(draw, x, grid_y, grid_y + grid_h, GRAY_LIGHT, width=1)

    # Day headers — drawn AFTER full-day events so dates stay on top of bars
    dow_font = _font(int(40 * font_scale), bold=True)
    date_font = _font(int(40 * font_scale), bold=True)
    # Compute baseline to center text vertically between header line (110) and grid top (170)
    _bb = draw.textbbox((0, 0), "Mon", font=dow_font)
    _line_y = 140 - (_bb[1] + _bb[3]) // 2
    now_min_total = now.hour * 60 + now.minute
    is_before_day = now_min_total < ds_min
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        cx = x + col_w // 2

        dow = d.strftime("%a")
        date_str = str(d.day)

        dw = _text_w(draw, dow, dow_font)
        dw2 = _text_w(draw, date_str, date_font)
        gap = 6
        combined_w = dw + gap + dw2
        line_x = cx - combined_w // 2
        line_y = _line_y

        if d == today:
            bb_dow = draw.textbbox((0, 0), dow, font=dow_font)
            bb_date = draw.textbbox((0, 0), date_str, font=date_font)
            pad = 4
            rect_top = 110  # box touches header line
            rect_bot = grid_y  # box reaches grid top
            draw.rectangle([x + 1, rect_top, x + col_w - 2, rect_bot], fill=BLACK)
            draw.text((line_x, line_y), dow, fill=WHITE, font=dow_font)
            draw.text((line_x + dw + gap, line_y), date_str, fill=WHITE, font=date_font)
        else:
            draw.text((line_x, line_y), dow, fill=GRAY_DARK, font=dow_font)
            draw.text((line_x + dw + gap, line_y), date_str, fill=BLACK, font=date_font)

    # Timed events
    timed_events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            continue
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        timed_events_by_date.setdefault(d, []).append(ev)

    event_font = _font(int(24 * font_scale))
    event_bold = _font_heavy(int(24 * font_scale))   # real bold — for white-on-black text in b/w mode
    event_font_sm = _font(int(18 * font_scale))
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        day_events = sorted(timed_events_by_date.get(d, []), key=lambda e: _ev_minutes(e, now))
        if not day_events:
            continue

        # Pre-compute positions for overlap detection
        ev_infos = []
        for ev in day_events:
            ev_start_min = _ev_minutes(ev, now, start=True)
            ev_end_min = _ev_minutes(ev, now, start=False)
            ev_start_min = max(ev_start_min, ds_min)
            ev_end_min = min(ev_end_min, de_min)
            if ev_end_min <= ev_start_min:
                continue
            ey_top = grid_y + (ev_start_min - ds_min) * minute_h
            ey_bot = grid_y + (ev_end_min - ds_min) * minute_h
            eh = max(ey_bot - ey_top, 18)
            ev_infos.append((ev, ey_top, ey_bot, eh, ev_end_min - ev_start_min, ev_start_min, ev_end_min))

        # Calculate horizontal splits for all events — checks ALL overlaps, not just neighbors
        SHRINK = 6  # ~1mm at 150 DPI
        draw_infos = []  # (ev, ey_top, ey_bot, eh, duration, xl, xr, start_min, end_min)
        for idx, (ev, ey_top, ey_bot, eh, duration, s_min, e_min) in enumerate(ev_infos):
            # Find ALL overlapping events
            overlap_idxs = []
            for j, (_, j_top, j_bot, _, _, _, _) in enumerate(ev_infos):
                if j != idx and ey_top < j_bot and ey_bot > j_top:
                    overlap_idxs.append(j)

            if overlap_idxs:
                # Rank this event's duration among all overlapping events
                all_durs = sorted([ev_infos[k][4] for k in overlap_idxs] + [duration], reverse=True)
                rank = all_durs.index(duration)  # 0 = longest

                if rank == 0:
                    # Longest event: left side, original size
                    xl, xr = x + 6, x + col_w - 6 - SHRINK
                elif rank == 1 and len(all_durs) >= 3:
                    # Middle event (3-way overlap): medium shrink
                    xl, xr = x + 6 + SHRINK * 3, x + col_w - 4
                elif len(overlap_idxs) >= 2:
                    # Shortest in 3+ overlap: render on top, 2x shrink
                    xl, xr = x + 6 + SHRINK * 6, x + col_w - 4 - SHRINK * 3
                else:
                    # 1 overlap, shorter event: original shrink
                    xl, xr = x + 6 + SHRINK * 3, x + col_w - 4
            else:
                xl, xr = x + 4, x + col_w - 4
            draw_infos.append((ev, ey_top, ey_bot, eh, duration, xl, xr, s_min, e_min))

        # Draw boxes: longest first so shorter events render ON TOP
        now_min_total = now.hour * 60 + now.minute
        for info in sorted(draw_infos, key=lambda e: -e[4]):
            ev, ey_top, ey_bot, eh, duration, xl, xr, s_min, e_min = info
            is_crossed = crossed_event_dim and (s_min <= now_min_total < e_min)
            is_past = dim_past_events and (d < today or (d == today and e_min <= now_min_total))
            is_dimmed = is_crossed or is_past
            if bw_mode:
                if is_dimmed:
                    # Dimmed in b/w: white fill + black border (or checkerboard)
                    box_fill, box_outline = WHITE, BLACK
                else:
                    # Normal in b/w: solid black box with a WHITE border. When
                    # events overlap, the shorter one is drawn on top of the
                    # longer (black) box; a white border makes that boundary
                    # visible instead of the two merging into one black blob.
                    box_fill = BLACK
                    box_outline = WHITE
            elif is_crossed:
                box_fill, box_outline = WHITE, GRAY_DIM
            elif is_past:
                box_fill, box_outline = WHITE, GRAY_LIGHT
            else:
                box_fill, box_outline = GRAY_VLIGHT, BLACK
            # Even-align the box's L/R edges to the panel's 2px column grid so
            # each 2px vertical border fills a whole column pair. Otherwise the
            # driver's 2px-grid snap widens an odd-aligned 2px border to 4px on
            # some columns, making event borders look thicker on some days than
            # others. Left edge -> even (pair bx0,bx0+1); right edge -> odd
            # (pair bx1-1,bx1). Result: a uniform 2px border on all 7 days.
            bx0 = int(xl) & ~1
            bx1 = int(xr) | 1
            by0 = int(ey_top)
            by1 = int(ey_top + eh - 1)
            if bw_mode:
                # For checkerboard-dimmed events, draw a white outline outside
                # the black border first, so overlapping dimmed events are
                # visually separated (the black border alone blends into the
                # dithered background of the event beneath).
                if is_dimmed and dim_style == "checkerboard":
                    draw.rounded_rectangle([bx0 - 2, by0 - 2, bx1 + 2, by1 + 2],
                                           radius=10, fill=WHITE, outline=WHITE, width=2)
                # Clear the card footprint to white FIRST, then draw the rounded
                # card. Otherwise the 4 corner triangles outside the rounded arc
                # show whatever is beneath (an overlapping black card), filling
                # the corners so the black fill looks rectangular while only the
                # border traces the round. Clearing makes the rounded corners
                # read against white for both the fill and the border.
                draw.rounded_rectangle([bx0, by0, bx1, by1], radius=8,
                                       fill=box_fill, outline=box_outline, width=2)
            else:
                draw.rounded_rectangle([bx0, by0, bx1, by1], radius=6,
                                       fill=box_fill, outline=box_outline, width=2)
            # Checkerboard dim: random pixel dithering with density controlled
            # by the brightness setting. Higher brightness → fewer black pixels.
            # 1.0 = 50% black, 0.1 = ~95% black, 1.9 = ~5% black.
            # Pixels are placed randomly per row (no diagonal pattern artifacts).
            if bw_mode and is_dimmed and dim_style == "checkerboard":
                # Map brightness (0.1–1.9) to black density (0.95–0.05)
                black_density = 1.0 - (brightness - 0.1) / (1.9 - 0.1)
                black_density = max(0.01, min(0.99, black_density))
                fill_x_start = int(xl) + 2
                fill_x_end = int(xr) - 1
                fill_y_start = int(ey_top) + 2
                fill_y_end = int(ey_top + eh - 1)
                row_width = fill_x_end - fill_x_start
                if row_width > 0:
                    for cy in range(fill_y_start, fill_y_end):
                        # Random per-row pixel placement
                        for cx in range(fill_x_start, fill_x_end):
                            if random.random() < black_density:
                                draw.point((cx, cy), fill=BLACK)
                            else:
                                draw.point((cx, cy), fill=WHITE)

        # Draw text: line-by-line, skipping only lines fully inside overlap zones
        line_gap_bonus = int(8 * max(0, font_scale - 1))  # extra line spacing for larger fonts
        line_h = int(26 * font_scale) + line_gap_bonus
        for ev, ey_top, ey_bot, eh, duration, xl, xr, s_min, e_min in draw_infos:
            summary = ev.get("summary", "")
            time_str = _ev_time_str(ev, now, time_format)
            avail_w = xr - xl - 8
            txt_x = xl + 10

            # Build ordered list of (text, kind) lines to render. Priority:
            # title > time > description > location. Description and location are
            # lowest priority and only fill whatever room is left in the card.
            render_lines = []
            if summary and summary != "(No title)":
                for line in _wrap_text_lines(draw, summary, event_font, avail_w):
                    render_lines.append((line, "title"))
            if time_str:
                if render_lines:
                    render_lines.append(("", "spacer"))
                render_lines.append((time_str, "time"))
            if show_descriptions:
                loc = _clean_location(ev.get("location", ""))
                desc = _clean_desc(ev.get("description", ""))
                if (loc or desc) and render_lines:
                    render_lines.append(("", "spacer"))
                # Extra half-distance (2px) between time and description/location,
                # only when both description and location are present.
                if desc and loc and render_lines:
                    render_lines.append(("", "half_spacer"))
                if desc:
                    for line in _wrap_text_lines(draw, desc, event_font_sm, avail_w):
                        render_lines.append((line, "desc"))
                if loc:
                    if desc:
                        render_lines.append(("", "spacer"))  # small gap before location
                    for line in _wrap_text_lines(draw, "@ " + loc, event_font_sm, avail_w):
                        render_lines.append((line, "loc"))

            if not render_lines:
                continue

            # Collect overlap ranges from shorter events
            overlap_ranges = []
            for o_ev, o_top, o_bot, o_eh, o_dur, o_xl, o_xr, _, _ in draw_infos:
                if o_dur < duration and o_top < ey_bot and o_bot > ey_top:
                    overlap_ranges.append((o_top, o_bot))

            is_crossed = crossed_event_dim and (s_min <= now_min_total < e_min)
            is_past = dim_past_events and (d < today or (d == today and e_min <= now_min_total))
            is_dimmed = is_crossed or is_past
            if bw_mode:
                if is_dimmed:
                    # Dimmed: white/checkerboard bg → black text (with white outline
                    # for checkerboard so text is readable on both B and W pixels)
                    text_fill = BLACK
                else:
                    # Normal: black box → white text
                    text_fill = WHITE
            else:
                text_fill = GRAY_MID if is_dimmed else BLACK
            # White text on a solid black box (1-bit mode) reads poorly with a
            # regular weight — use bold so thin strokes survive on black.
            # title/time use the event weight (bold on black in b/w); the
            # lowest-priority description uses the same colour but a smaller,
            # thinner face so it reads as secondary.
            line_font = event_bold if (bw_mode and not is_dimmed) else event_font
            fonts = {"title": line_font, "time": line_font, "loc": event_font_sm, "desc": event_font_sm}
            heights = {"title": line_h, "time": line_h, "loc": int(20 * font_scale) + line_gap_bonus, "desc": int(20 * font_scale) + line_gap_bonus}

            y = ey_top + 2
            for text, kind in render_lines:
                if kind == "spacer" or not text:
                    y += 4
                    continue
                if kind == "half_spacer":
                    y += 2
                    continue
                lh = heights.get(kind, line_h)
                f = fonts.get(kind, line_font)
                while True:
                    blocked = False
                    for o_top, o_bot in overlap_ranges:
                        # Push the line below any covering event it would touch —
                        # not only ones it falls fully inside. A line straddling
                        # the top edge of an on-top event must move down too, or
                        # its text bleeds into that event (e.g. a time label
                        # leaking onto the event that starts mid-line).
                        if y + lh > o_top and y < o_bot:
                            y = o_bot + 4
                            blocked = True
                            break
                    if not blocked:
                        break
                    if y + lh > ey_bot - 4:
                        break
                if y + lh > ey_bot - 4:
                    break  # No room
                # White text outline for contrast on colored/gray event boxes.
                # Uses PIL stroke_width=text_outline_width, drawn on a temp layer
                # clipped to the card interior so the outline never extends past
                # the card or damages the 2px border. Only for events with a
                # gray/color background (grayscale normal, b/w checkerboard dimmed).
                if text_outline_width > 0:
                    if not bw_mode:
                        _box_fill = GRAY_VLIGHT if not is_dimmed else WHITE
                        _needs_outline = (_box_fill != WHITE)
                    elif is_dimmed and dim_style == "checkerboard":
                        _needs_outline = True
                    else:
                        _needs_outline = False
                else:
                    _needs_outline = False
                if _needs_outline:
                    _bx0 = int(xl) & ~1
                    _bx1 = int(xr) | 1
                    _by0 = int(ey_top)
                    _by1 = int(ey_top + eh - 1)
                    _clip_l = _bx0 + 2
                    _clip_t = _by0 + 2
                    _clip_r = _bx1 - 2
                    _clip_b = _by1 - 2
                    _cw = _clip_r - _clip_l
                    _ch = _clip_b - _clip_t
                    if _cw > 0 and _ch > 0:
                        # Transparent layer: draw white text with outline stroke,
                        # then paste using the layer as its own alpha mask so only
                        # the white pixels land on the card (not a white rectangle).
                        _layer = Image.new("RGBA", (_cw, _ch), (0, 0, 0, 0))
                        _ldraw = ImageDraw.Draw(_layer)
                        _lx = txt_x - _clip_l
                        _ly = y - _clip_t
                        _ldraw.text((_lx, _ly), text, fill=(255, 255, 255, 255),
                                    font=f, stroke_width=text_outline_width,
                                    stroke_fill=(255, 255, 255, 255))
                        img.paste(_layer, (_clip_l, _clip_t), _layer)
                draw.text((txt_x, y), text, fill=text_fill, font=f)
                y += lh

    # Full-day events — drawn LAST so they cover everything (day headers, timed events)
    fd_font = _font(int(24 * font_scale))
    fd_h = int(30 * font_scale)  # bar height (fits 2 from header line to grid_y)
    fd_step = fd_h  # no gap between stacked events
    fd_top = HEADER_H - 8  # top event's ey so its top edge touches the header separator line
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        fd_list = fd_events_by_date.get(d, [])
        fd_count = min(len(fd_list), max_full_day)
        for j, ev in enumerate(fd_list[:max_full_day]):
            label = ev.get("summary", "")
            if not label or label == "(No title)":
                continue

            # Vertical: single event goes above header line, 2 below, 3+ overshoot
            if fd_count == 1:
                ey = fd_top - fd_step  # single event, bottom touches header line
            elif j < 2:
                ey = fd_top + j * fd_step
            else:
                ey = fd_top - (j - 1) * fd_step

            # Full cell width (stack vertically, not side-by-side)
            xl, xr = x + 4, x + col_w - 4
            avail_fd_w = xr - xl - 8
            wrapped = _wrap_text_lines(draw, label, fd_font, avail_fd_w)
            display = wrapped[0] if wrapped else label[:15]
            # Even-align L/R edges to the 2px column grid (see timed-event boxes)
            # so the 2px vertical borders stay a uniform 2px on every day after
            # the driver's 2px-grid snap, instead of widening to 4px on some.
            bx0 = int(xl) & ~1
            bx1 = int(xr) | 1
            fy0 = int(ey - 2)
            fy1 = int(ey + fd_h - 2)
            if bw_mode:
                # 3px rounded, white border separates stacked full-day bars,
                # bold white text stays legible on black. Clear to white first so
                # the rounded corners read against white, not the bar beneath.
                draw.rounded_rectangle([bx0, fy0, bx1, fy1], radius=8,
                                       fill=BLACK, outline=WHITE, width=2)
                draw.text((xl + 6, ey - 3), display, fill=WHITE, font=_font_heavy(int(24 * font_scale)))
            else:
                draw.rounded_rectangle([bx0, fy0, bx1, fy1], radius=6,
                                       fill=GRAY_VLIGHT, outline=BLACK, width=2)
                # White text outline on the gray fill, same as timed events.
                if text_outline_width > 0:
                    _fd_clip_l = bx0 + 2
                    _fd_clip_t = fy0 + 2
                    _fd_clip_r = bx1 - 2
                    _fd_clip_b = fy1 - 2
                    _fd_cw = _fd_clip_r - _fd_clip_l
                    _fd_ch = _fd_clip_b - _fd_clip_t
                    if _fd_cw > 0 and _fd_ch > 0:
                        _fd_layer = Image.new("RGBA", (_fd_cw, _fd_ch), (0, 0, 0, 0))
                        _fd_ldraw = ImageDraw.Draw(_fd_layer)
                        _fd_lx = (xl + 6) - _fd_clip_l
                        _fd_ly = (ey - 3) - _fd_clip_t
                        _fd_ldraw.text((_fd_lx, _fd_ly), display,
                                       fill=(255, 255, 255, 255),
                                       font=fd_font, stroke_width=text_outline_width,
                                       stroke_fill=(255, 255, 255, 255))
                        img.paste(_fd_layer, (_fd_clip_l, _fd_clip_t), _fd_layer)
                draw.text((xl + 6, ey - 3), display, fill=BLACK, font=fd_font)


def _wrap_text_lines(draw, text, font, max_w):
    """Wrap text to fit max_w pixels, hyphenating long words.

    Returns a list of lines.
    """
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = current + (" " if current else "") + word
        if _text_w(draw, test, font) <= max_w:
            current = test
        else:
            if current:
                lines.append(current)
            # Check if word itself overflows — hyphenate
            if _text_w(draw, word, font) > max_w:
                while word:
                    for i in range(len(word), 0, -1):
                        frag = word[:i] + ("-" if i < len(word) else "")
                        if _text_w(draw, frag, font) <= max_w or i == 1:
                            lines.append(frag)
                            word = word[i:]
                            break
                current = ""
            else:
                current = word
    if current:
        lines.append(current)
    return lines


def _ev_minutes(ev: dict, now: datetime.datetime, start: bool = True) -> int:
    """Get event start/end time in minutes from midnight (local)."""
    dt = ev["start"] if start else ev["end"]
    if isinstance(dt, datetime.datetime):
        # Convert to local if needed
        if dt.tzinfo:
            dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
        return dt.hour * 60 + dt.minute
    if isinstance(dt, datetime.date):
        return 0 if start else 24 * 60
    return 0


def _ev_time_str(ev: dict, now: datetime.datetime, time_format: str = "24h") -> str:
    """Format event time range as 'HH:MM–HH:MM' (start–end).
    Falls back to just the start time if no end time is available."""
    start = ev["start"]
    end = ev.get("end")
    fmt = "%-I:%M" if time_format == "12h" else "%H:%M"

    def _fmt(dt):
        if isinstance(dt, datetime.datetime):
            if dt.tzinfo:
                dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
            return dt.strftime(fmt)
        return ""

    s = _fmt(start)
    e = _fmt(end) if end is not None else ""
    if s and e:
        return f"{s}–{e}"
    return s


def _ev_end_time_str(ev: dict, now: datetime.datetime) -> str:
    """Format event end time as HH:MM."""
    dt = ev["end"]
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo:
            dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
        return dt.strftime("%H:%M")
    return ""


# ---- Current-time line ----
def _styled_time_line(draw, x_start, x_end, y, style, bw_mode=False):
    """Draw the current-time indicator across a column in the selected style —
    'solid', 'dotted', or 'wavy'. Shared by the in-range line and the
    out-of-range placeholder so the chosen style is always honoured (the
    placeholder used to be hardcoded stripes, making 'wavy' look like 'dotted').

    In b/w (1-bit) mode, the dotted style uses alternating vertical B/W
    segments across the full line height so the line is visible on both
    black and white event backgrounds (the standard dotted style renders
    as a solid black bar on black event boxes)."""
    x_start, x_end, y = int(x_start), int(x_end), int(y)
    if style == "solid":
        _hline(draw, x_start, y, x_end, BLACK, width=5)
        _hline(draw, x_start, y - 4, x_end, WHITE, width=1)
        _hline(draw, x_start, y + 4, x_end, WHITE, width=1)
    elif style == "wavy":
        amp, period = 5, 22

        def pts(dy):
            return [(sx, int(round(y + dy + amp * math.sin(
                        (sx - x_start) / period * 2 * math.pi))))
                    for sx in range(x_start, x_end + 1)]
        draw.line(pts(-3), fill=WHITE, width=1)   # white outline above
        draw.line(pts(3), fill=WHITE, width=1)    # white outline below
        draw.line(pts(0), fill=BLACK, width=3)    # continuous black sine
    elif style == "dotted" and bw_mode:
        # 1-bit mode: alternating vertical B/W segments across full height
        # (y-4 to y+5) so the line reads on both black and white backgrounds.
        # White border rows above/below, with the center alternating.
        stripe_w = 6
        line_top = y - 4
        line_bot = y + 5
        for sx in range(x_start, x_end, stripe_w * 2):
            x2 = min(sx + stripe_w, x_end)
            # Black segment: full-height black column
            draw.rectangle([sx, line_top, x2, line_bot], fill=BLACK)
        for sx in range(x_start + stripe_w, x_end, stripe_w * 2):
            x2 = min(sx + stripe_w, x_end)
            # White segment: full-height white column
            draw.rectangle([sx, line_top, x2, line_bot], fill=WHITE)
    else:  # dotted (default, grayscale)
        stripe_w = 6
        for sx in range(x_start, x_end, stripe_w * 2):
            x2 = min(sx + stripe_w, x_end)
            draw.rectangle([sx, y - 4, x2, y - 2], fill=WHITE)
            draw.rectangle([sx, y + 3, x2, y + 5], fill=WHITE)
        for sx in range(x_start, x_end, stripe_w * 2):
            x2 = min(sx + stripe_w, x_end)
            draw.rectangle([sx, y - 2, x2, y + 2], fill=BLACK)


def _draw_time_line(draw, now, view_mode, day_start, day_end, events, time_format="24h",
                     style="dotted", bw_mode=False):
    """Draw a horizontal line at the current time position.

    style: "solid" (thick 4px line), "dotted" (striped, default), "wavy"
    """
    if view_mode == "week":
        today = now.date()
        start_date = today - datetime.timedelta(days=today.weekday())
        col_index = today.weekday()  # 0=Mon
    else:  # 7days or 5days
        start_date = now.date()
        col_index = 0

    ds_h, ds_m = (int(x) for x in day_start.split(":"))
    de_h, de_m = (int(x) for x in day_end.split(":"))
    ds_min = ds_h * 60 + ds_m
    de_min = de_h * 60 + de_m

    now_min = now.hour * 60 + now.minute

    days = 5 if view_mode == "5days" else 7

    # Replicate dynamic left margin from _render_day_grid (must match exactly)
    hour_font = _font(26, bold=True)
    max_label_w = 0
    for h in range(ds_h, de_h + 1):
        if time_format == "12h":
            ampm = "AM" if h < 12 else "PM"
            h12 = h % 12
            if h12 == 0:
                h12 = 12
            label = f"{h12} {ampm}"
        else:
            label = f"{h:02d}"
        lw = _text_w(draw, label, hour_font)
        if lw > max_label_w:
            max_label_w = lw
    grid_x = max(60, max_label_w + 14)
    grid_y = HEADER_H + 50
    grid_w = W - grid_x - RIGHT_PAD
    # MUST match _render_day_grid's grid_h exactly — the +20 bottom expansion
    # affects minute_h, which determines the time-line Y position. A mismatch
    # causes the time line to land in the wrong hour cell.
    grid_h = H - grid_y - FOOTER_H + 20  # +~2mm bottom expansion (same as grid)
    col_w = grid_w // days
    span_min = de_min - ds_min
    if span_min <= 0:
        span_min = 16 * 60
    minute_h = grid_h / span_min

    x_start = grid_x + col_index * col_w
    x_end = x_start + col_w

    if now_min < ds_min:
        # Before visible range — styled indicator at 15-min mark
        y = int(grid_y + 15 * minute_h)
        _styled_time_line(draw, x_start, x_end, y, style, bw_mode=bw_mode)
        # Time label pill — box padding scales with font size + 3px white gap
        time_str = now.strftime("%H:%M")
        label_font = _font(26, bold=True)
        lw = _text_w(draw, time_str, label_font)
        lh = _text_h(draw, time_str, label_font)
        pad_x = 4   # fixed 4px white gap between text and pill border
        pad_y = 4
        box_r = x_end
        box_l = x_end - lw - pad_x * 2
        box_t = y - lh // 2 - pad_y
        box_b = y + lh // 2 + pad_y
        draw.rectangle([box_l, box_t, box_r, box_b], fill=WHITE, outline=BLACK, width=1)
        draw.text(((box_l + box_r) // 2, (box_t + box_b) // 2), time_str, fill=BLACK, font=label_font, anchor="mm")
        return

    if now_min > de_min:
        # After visible range — styled indicator at 45-min mark
        y = int(grid_y + grid_h - 15 * minute_h)
        _styled_time_line(draw, x_start, x_end, y, style, bw_mode=bw_mode)
        # Time label pill — box padding scales with font size + 3px white gap
        time_str = now.strftime("%H:%M")
        label_font = _font(26, bold=True)
        lw = _text_w(draw, time_str, label_font)
        lh = _text_h(draw, time_str, label_font)
        pad_x = 4
        pad_y = 4
        box_r = x_end
        box_l = x_end - lw - pad_x * 2
        box_t = y - lh // 2 - pad_y
        box_b = y + lh // 2 + pad_y
        draw.rectangle([box_l, box_t, box_r, box_b], fill=WHITE, outline=BLACK, width=1)
        draw.text(((box_l + box_r) // 2, (box_t + box_b) // 2), time_str, fill=BLACK, font=label_font, anchor="mm")
        return

    y = grid_y + (now_min - ds_min) * minute_h

    # Draw the time line in the selected style
    _styled_time_line(draw, x_start, x_end, int(y), style, bw_mode=bw_mode)
    y = int(y)

    # Small time label at the right edge of the line — box padding scales
    # with font size + 3px white gap between numbers and borders.
    time_str = now.strftime("%H:%M")
    label_font = _font(26, bold=True)
    lw = _text_w(draw, time_str, label_font)
    lh = _text_h(draw, time_str, label_font)
    pad_x = 4   # fixed 4px white gap between text and pill border
    pad_y = 4
    box_r = x_end
    box_l = x_end - lw - pad_x * 2
    box_t = y - lh // 2 - pad_y
    box_b = y + lh // 2 + pad_y
    draw.rectangle([box_l, box_t, box_r, box_b], fill=WHITE, outline=BLACK, width=1)
    draw.text(((box_l + box_r) // 2, (box_t + box_b) // 2), time_str, fill=BLACK, font=label_font, anchor="mm")


# ---- QR code screen (initial setup) ----
def render_qr_setup(qr_url: str, scheme: str, lan_ip: str, port: int) -> Image.Image:
    """Render the initial-setup screen: QR code + LAN IP:port below it."""
    import qrcode

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Title
    title_font = _font(48, bold=True)
    title = "E-Ink Calendar Setup"
    tw = _text_w(draw, title, title_font)
    draw.text(((W - tw) // 2, 80), title, fill=BLACK, font=title_font,
              stroke_width=2, stroke_fill=BLACK)

    subtitle_font = _font(28)
    subtitle = "Scan QR code or visit the URL below"
    sw = _text_w(draw, subtitle, subtitle_font)
    draw.text(((W - sw) // 2, 150), subtitle, fill=GRAY_DARK, font=subtitle_font,
              stroke_width=2, stroke_fill=GRAY_DARK)

    # QR code (centered)
    qr = qrcode.QRCode(version=1, box_size=12, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(qr_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_size = 600
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    qr_x = (W - qr_size) // 2
    qr_y = 220
    img.paste(qr_img, (qr_x, qr_y))

    # LAN IP + port below QR code
    ip_font = _font(56, bold=True)
    ip_text = f"{lan_ip}:{port}"
    iw = _text_w(draw, ip_text, ip_font)
    draw.text(((W - iw) // 2, qr_y + qr_size + 40), ip_text, fill=BLACK, font=ip_font,
              stroke_width=2, stroke_fill=BLACK)

    url_font = _font(28)
    url_text = f"{scheme}://{lan_ip}:{port}/settings"
    uw = _text_w(draw, url_text, url_font)
    draw.text(((W - uw) // 2, qr_y + qr_size + 110), url_text, fill=GRAY_DARK, font=url_font,
              stroke_width=2, stroke_fill=GRAY_DARK)

    return img


def render_wifi_hotspot(ssid: str, password: str, portal_url: str,
                        wifi_qr_text: str) -> Image.Image:
    """Provisioning screen shown while the Pi hosts its own hotspot.

    Two QR codes side by side:
      1. Join the hotspot (WIFI: payload — a phone camera joins the AP)
      2. Open the setup page (portal_url) to enter home-WiFi credentials
    Plus the SSID / password / URL in text as a fallback.
    """
    import qrcode

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    title_font = _font(52, bold=True)
    title = "Connect this display to WiFi"
    tw = _text_w(draw, title, title_font)
    draw.text(((W - tw) // 2, 40), title, fill=BLACK, font=title_font,
              stroke_width=2, stroke_fill=BLACK)

    sub_font = _font(26)
    sub = "No internet — the display is hosting a temporary hotspot"
    sw = _text_w(draw, sub, sub_font)
    draw.text(((W - sw) // 2, 108), sub, fill=GRAY_DARK, font=sub_font)

    def _qr(data: str, size: int) -> Image.Image:
        qr = qrcode.QRCode(version=1, box_size=12, border=2,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        q = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return q.resize((size, size), Image.NEAREST)

    qr_size = 520
    col_l = W // 4                 # left column center
    col_r = 3 * W // 4             # right column center
    qr_y = 250
    step_font = _font(34, bold=True)
    label_font = _font(30, bold=True)
    val_font = _font(30)

    # ---- Column 1: join the hotspot ----
    s1 = "1. Scan to join the hotspot"
    s1w = _text_w(draw, s1, step_font)
    draw.text((col_l - s1w // 2, qr_y - 60), s1, fill=BLACK, font=step_font,
              stroke_width=1, stroke_fill=BLACK)
    img.paste(_qr(wifi_qr_text, qr_size), (col_l - qr_size // 2, qr_y))
    ty = qr_y + qr_size + 24
    for label, val in (("Network:", ssid), ("Password:", password)):
        line = f"{label} {val}"
        lw = _text_w(draw, line, val_font)
        draw.text((col_l - lw // 2, ty), line, fill=BLACK, font=val_font)
        ty += 44

    # ---- Column 2: open the setup page ----
    s2 = "2. Then open the setup page"
    s2w = _text_w(draw, s2, step_font)
    draw.text((col_r - s2w // 2, qr_y - 60), s2, fill=BLACK, font=step_font,
              stroke_width=1, stroke_fill=BLACK)
    img.paste(_qr(portal_url, qr_size), (col_r - qr_size // 2, qr_y))
    uw = _text_w(draw, portal_url, val_font)
    draw.text((col_r - uw // 2, qr_y + qr_size + 24), portal_url,
              fill=BLACK, font=val_font)
    hint = "(after joining the hotspot)"
    hw = _text_w(draw, hint, sub_font)
    draw.text((col_r - hw // 2, qr_y + qr_size + 68), hint, fill=GRAY_DARK, font=sub_font)

    return img


def render_status(message: str, submessage: str = "") -> Image.Image:
    """Render a simple status/error message screen."""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    font = _font(48, bold=True)
    mw = _text_w(draw, message, font)
    draw.text(((W - mw) // 2, H // 2 - 40), message, fill=BLACK, font=font,
              stroke_width=2, stroke_fill=BLACK)

    if submessage:
        sub_font = _font(28)
        sw = _text_w(draw, submessage, sub_font)
        draw.text(((W - sw) // 2, H // 2 + 30), submessage, fill=GRAY_DARK, font=sub_font,
                  stroke_width=2, stroke_fill=GRAY_DARK)

    return img


def render_setup_required(lan_ip: str, port: int, ssl: bool = False) -> Image.Image:
    """Render the 'Setup Required' screen with Google OAuth instructions.

    Uses 3x supersampling + LANCZOS downscale for smooth antialiased text,
    matching the C IT8951 driver's text rendering technique.
    """
    redirect_uri = "http://localhost:8889/auth/callback"
    scheme = "https" if ssl else "http"
    settings_url = f"{scheme}://{lan_ip}:{port}/settings"
    scale = 3
    sw, sh = W * scale, H * scale

    # Right-hand QR column reserved at final resolution; text wraps to its left.
    qr_size = 460
    qr_x = W - RIGHT_PAD - qr_size - 30       # final-res left edge of the QR
    qr_y = 250
    text_right = qr_x - 30                     # final-res right edge for text

    canvas = Image.new("RGB", (sw, sh), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    x = MARGIN * scale
    y = 30 * scale

    # Title
    title_font = _font(60 * scale, bold=True)
    draw.text((x, y), "Setup Required", fill=BLACK, font=title_font)
    y += 76 * scale
    _hline(draw, x, y, W * scale - MARGIN * scale, GRAY_MID, width=2 * scale)
    y += 26 * scale

    step_font = _font(44 * scale, bold=True)
    text_font = _font(34 * scale)
    indent = x + 22 * scale
    text_wrap_w = text_right * scale - indent   # keep steps clear of the QR

    steps = [
        ("step", "1. Create a Google app"),
        ("text", "Open console.cloud.google.com, new project"),
        ("text", "APIs & Services > Enable 'Google Calendar API'"),
        ("text", "OAuth consent screen: User type = External"),
        ("text", "Add your Google account as a Test user"),
        ("text", "Credentials > Create > OAuth client ID"),
        ("text", "Application type: Web application"),
        ("text", "Add Authorized redirect URI:"),
        ("code", redirect_uri),
        ("text", "Create, then Download JSON (client_secret.json)"),
        ("blank", ""),
        ("step", "2. Authorize this display"),
        ("text", "Scan the QR to open the settings page"),
        ("text", "Upload client_secret.json there"),
        ("text", "Tap 'Login with Google' and approve access"),
        ("text", "Copy the shown code, paste it back"),
        ("text", "Pick your calendars — done"),
    ]

    def _fit(line, font):
        s = line
        while _text_w(draw, s, font) > text_wrap_w and len(s) > 4:
            s = s[:-1]
        return s if s == line else s[:-1] + "…"

    for kind, line in steps:
        if kind == "blank":
            y += 14 * scale
        elif kind == "step":
            draw.text((x, y), line, fill=BLACK, font=step_font)
            y += 56 * scale
        elif kind == "text":
            draw.text((indent, y), "• " + _fit(line, text_font), fill=GRAY_DARK, font=text_font)
            y += 46 * scale
        elif kind == "code":
            draw.text((indent, y), _fit(line, text_font), fill=BLACK, font=text_font)
            y += 46 * scale

    # Downscale text with LANCZOS, then paste the QR at final resolution so its
    # modules stay crisp (scannable) rather than blurred by the downscale.
    img = canvas.resize((W, H), Image.LANCZOS)
    draw2 = ImageDraw.Draw(img)

    import qrcode
    qr = qrcode.QRCode(version=1, box_size=12, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(settings_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((qr_size, qr_size), Image.NEAREST)
    img.paste(qr_img, (qr_x, qr_y))

    lbl_font = _font(30, bold=True)
    lbl = "Scan to finish setup"
    lw = _text_w(draw2, lbl, lbl_font)
    draw2.text((qr_x + (qr_size - lw) // 2, qr_y - 44), lbl, fill=BLACK, font=lbl_font)
    url_font = _font(24)
    uw = _text_w(draw2, settings_url, url_font)
    draw2.text((qr_x + (qr_size - uw) // 2, qr_y + qr_size + 12), settings_url,
               fill=GRAY_DARK, font=url_font)

    return img