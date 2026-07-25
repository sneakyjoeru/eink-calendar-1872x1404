"""Calendar rendering — composes views to PIL images for the e-ink screen.

All rendering targets 1872x1404 (Waveshare 7.8" IT8951).
Supports month / week / 7-days views with a current-time indicator line.
"""
import datetime
import logging
import math
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from . import config

logger = logging.getLogger("eink.render")

# Screen dimensions
W = config.SCREEN_W
H = config.SCREEN_H

# Margins (pixels)
MARGIN = 40
HEADER_H = 120
FOOTER_H = 30

# Fonts — try DejaVu, fall back to default
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Get a cached font instance."""
    path = _FONT_PATHS[1] if bold else _FONT_PATHS[0]
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


# ---- Color helpers (grayscale for e-ink: 0=black, 255=white) ----
WHITE = 255
BLACK = 0
GRAY_DARK = 60
GRAY_MID = 120
GRAY_LIGHT = 200
GRAY_VLIGHT = 230


def render_calendar(view_mode: str, events: list[dict],
                    day_start: str, day_end: str,
                    max_full_day: int,
                    now: Optional[datetime.datetime] = None) -> Image.Image:
    """Render the full calendar view to a PIL Image.

    Returns an RGB image (will be converted to grayscale by the C driver).
    """
    if now is None:
        now = datetime.datetime.now()

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    ds_h, ds_m = (int(x) for x in day_start.split(":"))
    de_h, de_m = (int(x) for x in day_end.split(":"))

    if view_mode == "month":
        _render_month(draw, events, now, max_full_day)
    elif view_mode == "7days":
        _render_7days(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day)
    else:  # week (default)
        _render_week(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day)

    # Draw current-time line on week/7days views
    if view_mode in ("week", "7days"):
        _draw_time_line(draw, now, view_mode, day_start, day_end, events)

    return img


# ---- Header ----
def _draw_header(draw: ImageDraw.ImageDraw, title: str, subtitle: str = ""):
    """Draw the page header with title (left) and subtitle (right)."""
    # Title
    font_title = _font(64, bold=True)
    draw.text((MARGIN, 20), title, fill=BLACK, font=font_title)

    # Subtitle (e.g. week number or date range)
    if subtitle:
        font_sub = _font(36)
        sw = _text_w(draw, subtitle, font_sub)
        draw.text((W - MARGIN - sw, 30), subtitle, fill=GRAY_DARK, font=font_sub)

    # Header separator line
    y = HEADER_H - 10
    draw.line([(MARGIN, y), (W - MARGIN, y)], fill=GRAY_MID, width=2)


# ---- Month view ----
def _render_month(draw, events, now, max_full_day):
    """Month grid view — weeks as rows, days as columns."""
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

    grid_x = MARGIN
    grid_y = HEADER_H + 10
    grid_w = W - 2 * MARGIN
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
    event_font = _font(20)
    day_num = grid_start
    for week in range(num_weeks):
        for col in range(7):
            x = grid_x + col * col_w
            y = grid_y + week * row_h

            # Cell border
            draw.rectangle([x, y, x + col_w - 1, y + row_h - 1], outline=GRAY_LIGHT)

            # Day number
            in_month = day_num.month == today.month
            is_today = day_num == today
            color = BLACK if in_month else GRAY_MID
            if is_today:
                # Highlight today with a filled circle
                cx = x + 24
                cy = y + 24
                r = 22
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)
                draw.text((cx - 14, cy - 18), str(day_num.day), fill=WHITE, font=cell_font)
            else:
                draw.text((x + 10, y + 6), str(day_num.day), fill=color, font=cell_font)

            # Events for this day (up to 3)
            day_events = events_by_date.get(day_num, [])
            ey = y + 48
            shown = 0
            for ev in day_events[:3]:
                label = ev["summary"][:18]
                # Truncate with ellipsis if too long
                if _text_w(draw, label, event_font) > col_w - 16:
                    while len(label) > 3 and _text_w(draw, label + "…", event_font) > col_w - 16:
                        label = label[:-1]
                    label += "…"
                draw.text((x + 8, ey), label, fill=GRAY_DARK, font=event_font)
                ey += 24
                shown += 1
            if len(day_events) > 3:
                draw.text((x + 8, ey), f"+{len(day_events) - 3}", fill=GRAY_MID, font=_font(18))

            day_num += datetime.timedelta(days=1)


# ---- Week view (7 columns = Mon..Sun) ----
def _render_week(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day):
    """Week view — 7 day columns with timed events stacked vertically."""
    title = now.strftime("%B %d, %Y")
    week_num = now.isocalendar()[1]
    _draw_header(draw, title, f"Week {week_num}")

    _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, days=7)


# ---- 7-days view (next 7 days starting today) ----
def _render_7days(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day):
    """7-days view — starting from today, 7 consecutive day columns."""
    title = "Next 7 Days"
    _draw_header(draw, title, now.strftime("%a %b %d"))

    _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, days=7, start_today=True)


def _render_day_grid(draw, events, now, ds_h, ds_m, de_h, de_m, max_full_day, days=7, start_today=False):
    """Shared day-grid renderer for week and 7-days views."""
    today = now.date()

    if start_today:
        start_date = today
    else:
        # Start from Monday of current week
        start_date = today - datetime.timedelta(days=today.weekday())

    grid_x = MARGIN
    grid_y = HEADER_H + 50  # Leave room for full-day event strip
    grid_w = W - 2 * MARGIN
    grid_h = H - grid_y - FOOTER_H

    col_w = grid_w // days
    ds_min = ds_h * 60 + ds_m
    de_min = de_h * 60 + de_m
    span_min = de_min - ds_min
    if span_min <= 0:
        span_min = 16 * 60  # fallback 16h
    minute_h = grid_h / span_min  # pixels per minute

    # Day headers
    dow_font = _font(30, bold=True)
    date_font = _font(24)
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        cx = x + col_w // 2

        dow = d.strftime("%a")
        dw = _text_w(draw, dow, dow_font)
        draw.text((cx - dw // 2, grid_y - 50), dow, fill=GRAY_DARK, font=dow_font)

        date_str = str(d.day)
        dw2 = _text_w(draw, date_str, date_font)
        color = BLACK
        if d == today:
            # Circle the date
            r = 18
            draw.ellipse([cx - r, grid_y - 22, cx + r, grid_y - 22 + 2 * r], fill=BLACK)
            draw.text((cx - dw2 // 2, grid_y - 20), date_str, fill=WHITE, font=date_font)
        else:
            draw.text((cx - dw2 // 2, grid_y - 22), date_str, fill=color, font=date_font)

    # Full-day events strip (above the time grid)
    fd_y = HEADER_H + 10
    fd_h = 36
    fd_events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            d = ev["start"]
            if isinstance(d, datetime.datetime):
                d = d.date()
            fd_events_by_date.setdefault(d, []).append(ev)

    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        fd_list = fd_events_by_date.get(d, [])
        for j, ev in enumerate(fd_list[:max_full_day]):
            ey = fd_y + j * 24
            label = ev["summary"][:15]
            if _text_w(draw, label + "…", _font(18)) > col_w - 8:
                while len(label) > 2 and _text_w(draw, label + "…", _font(18)) > col_w - 8:
                    label = label[:-1]
                label += "…"
            # Draw a filled bar
            draw.rectangle([x + 4, ey, x + col_w - 4, ey + 20], fill=GRAY_LIGHT)
            draw.text((x + 8, ey + 1), label, fill=BLACK, font=_font(18))

    # Grid border
    draw.rectangle([grid_x, grid_y, grid_x + days * col_w - 1, grid_y + grid_h - 1],
                   outline=GRAY_LIGHT, width=1)

    # Hour lines + labels
    hour_font = _font(22)
    for h in range(ds_h, de_h + 1):
        y = grid_y + (h * 60 - ds_min) * minute_h
        if y > grid_y + grid_h:
            break
        draw.line([(grid_x, y), (grid_x + days * col_w, y)], fill=GRAY_VLIGHT, width=1)
        label = f"{h:02d}:00"
        draw.text((grid_x - 52, y - 12), label, fill=GRAY_MID, font=hour_font)

    # Column separators
    for i in range(1, days):
        x = grid_x + i * col_w
        draw.line([(x, grid_y), (x, grid_y + grid_h)], fill=GRAY_LIGHT, width=1)

    # Timed events
    timed_events_by_date: dict[datetime.date, list[dict]] = {}
    for ev in events:
        if ev["all_day"]:
            continue
        d = ev["start"]
        if isinstance(d, datetime.datetime):
            d = d.date()
        timed_events_by_date.setdefault(d, []).append(ev)

    event_font = _font(20)
    event_font_sm = _font(16)
    for i in range(days):
        d = start_date + datetime.timedelta(days=i)
        x = grid_x + i * col_w
        day_events = sorted(timed_events_by_date.get(d, []), key=lambda e: _ev_minutes(e, now))

        for ev in day_events:
            ev_start_min = _ev_minutes(ev, now, start=True)
            ev_end_min = _ev_minutes(ev, now, start=False)
            # Clamp to visible range
            ev_start_min = max(ev_start_min, ds_min)
            ev_end_min = min(ev_end_min, de_min)
            if ev_end_min <= ev_start_min:
                continue

            ey_top = grid_y + (ev_start_min - ds_min) * minute_h
            ey_bot = grid_y + (ev_end_min - ds_min) * minute_h
            eh = max(ey_bot - ey_top, 18)

            # Event block
            draw.rectangle([x + 6, ey_top, x + col_w - 6, ey_top + eh - 1],
                           fill=BLACK)
            # Title text (white on black)
            label = ev["summary"][:20]
            if eh > 24:
                draw.text((x + 10, ey_top + 4), label, fill=WHITE, font=event_font)
                time_str = _ev_time_str(ev, now)
                draw.text((x + 10, ey_top + eh - 20), time_str, fill=WHITE, font=event_font_sm)
            else:
                # Too short for two lines — just the title
                draw.text((x + 10, ey_top + 2), label[:12], fill=WHITE, font=event_font_sm)


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


def _ev_time_str(ev: dict, now: datetime.datetime) -> str:
    """Format event time as HH:MM."""
    dt = ev["start"]
    if isinstance(dt, datetime.datetime):
        if dt.tzinfo:
            dt = dt.astimezone(now.tzinfo or datetime.timezone.utc).replace(tzinfo=None)
        return dt.strftime("%H:%M")
    return ""


# ---- Current-time line ----
def _draw_time_line(draw, now, view_mode, day_start, day_end, events):
    """Draw a horizontal line at the current time position.

    2px black line with 1px white outline. Only drawn on the current day's column.
    """
    if view_mode == "week":
        today = now.date()
        start_date = today - datetime.timedelta(days=today.weekday())
        col_index = today.weekday()  # 0=Mon
    else:  # 7days
        start_date = now.date()
        col_index = 0

    ds_h, ds_m = (int(x) for x in day_start.split(":"))
    de_h, de_m = (int(x) for x in day_end.split(":"))
    ds_min = ds_h * 60 + ds_m
    de_min = de_h * 60 + de_m

    now_min = now.hour * 60 + now.minute
    if now_min < ds_min or now_min > de_min:
        return  # Outside visible range

    days = 7
    grid_x = MARGIN
    grid_y = HEADER_H + 50
    grid_w = W - 2 * MARGIN
    grid_h = H - grid_y - FOOTER_H
    col_w = grid_w // days
    span_min = de_min - ds_min
    if span_min <= 0:
        span_min = 16 * 60
    minute_h = grid_h / span_min

    y = grid_y + (now_min - ds_min) * minute_h
    x_start = grid_x + col_index * col_w
    x_end = x_start + col_w

    # White outline (1px above and below the black line)
    draw.line([(x_start, y - 3), (x_end, y - 3)], fill=WHITE, width=1)
    draw.line([(x_start, y + 4), (x_end, y + 4)], fill=WHITE, width=1)

    # 2px black line
    draw.line([(x_start, y - 1), (x_end, y - 1)], fill=BLACK, width=2)
    draw.line([(x_start, y + 1), (x_end, y + 1)], fill=BLACK, width=2)

    # Small time label at the right edge of the line
    time_str = now.strftime("%H:%M")
    label_font = _font(18)
    lw = _text_w(draw, time_str, label_font)
    # Background pill
    draw.rectangle([x_end - lw - 8, y - 12, x_end, y + 12], fill=WHITE, outline=BLACK, width=1)
    draw.text((x_end - lw - 4, y - 10), time_str, fill=BLACK, font=label_font)


# ---- QR code screen (initial setup) ----
def render_qr_setup(qr_url: str, lan_ip: str, port: int) -> Image.Image:
    """Render the initial-setup screen: QR code + LAN IP:port below it."""
    import qrcode

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # Title
    title_font = _font(48, bold=True)
    title = "E-Ink Calendar Setup"
    tw = _text_w(draw, title, title_font)
    draw.text(((W - tw) // 2, 80), title, fill=BLACK, font=title_font)

    subtitle_font = _font(28)
    subtitle = "Scan QR code or visit the URL below"
    sw = _text_w(draw, subtitle, subtitle_font)
    draw.text(((W - sw) // 2, 150), subtitle, fill=GRAY_DARK, font=subtitle_font)

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
    draw.text(((W - iw) // 2, qr_y + qr_size + 40), ip_text, fill=BLACK, font=ip_font)

    url_font = _font(28)
    url_text = f"http://{lan_ip}:{port}/settings"
    uw = _text_w(draw, url_text, url_font)
    draw.text(((W - uw) // 2, qr_y + qr_size + 110), url_text, fill=GRAY_DARK, font=url_font)

    return img


def render_status(message: str, submessage: str = "") -> Image.Image:
    """Render a simple status/error message screen."""
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    font = _font(48, bold=True)
    mw = _text_w(draw, message, font)
    draw.text(((W - mw) // 2, H // 2 - 40), message, fill=BLACK, font=font)

    if submessage:
        sub_font = _font(28)
        sw = _text_w(draw, submessage, sub_font)
        draw.text(((W - sw) // 2, H // 2 + 30), submessage, fill=GRAY_DARK, font=sub_font)

    return img


def render_setup_required(lan_ip: str, port: int) -> Image.Image:
    """Render the 'Setup Required' screen with Google OAuth instructions.

    Shows step-by-step instructions for creating Google OAuth credentials
    and uploading client_secret.json to the Pi.
    """
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    x = MARGIN
    y = 60

    # Title
    title_font = _font(56, bold=True)
    draw.text((x, y), "Setup Required", fill=BLACK, font=title_font)
    y += 80

    # Separator
    draw.line([(x, y), (W - MARGIN, y)], fill=GRAY_MID, width=2)
    y += 30

    step_font = _font(32, bold=True)
    text_font = _font(26)
    code_font = _font(24)
    indent = x + 20

    steps = [
        ("step", "1. Create Google OAuth Credentials"),
        ("text", "Go to console.cloud.google.com"),
        ("text", "Create or select a project"),
        ("text", "Enable Google Calendar API"),
        ("text", "Credentials > Create > OAuth client ID"),
        ("text", "Type: Web application"),
        ("text", f"Redirect URI: http://{lan_ip}:{port}/auth/callback"),
        ("text", "Download client_secret.json"),
        ("blank", ""),
        ("step", "2. Upload to the Pi"),
        ("code", f"scp client_secret.json root@{lan_ip}:/opt/eink-calendar/config/"),
        ("blank", ""),
        ("step", "3. Restart the app"),
        ("code", "ssh root@192.168.0.199 'systemctl restart eink-calendar'"),
        ("blank", ""),
        ("step", "After restart:"),
        ("text", "E-ink will show a QR code"),
        ("text", f"Open http://{lan_ip}:{port}/settings"),
        ("text", "Login with Google > select calendars"),
    ]

    for kind, line in steps:
        if kind == "blank":
            y += 14
        elif kind == "step":
            draw.text((x, y), line, fill=BLACK, font=step_font)
            y += 38
        elif kind == "text":
            draw.text((indent, y), line, fill=GRAY_DARK, font=text_font)
            y += 32
        elif kind == "code":
            # Draw code in a light box
            tw = _text_w(draw, line, code_font)
            box_w = min(tw + 16, W - MARGIN - indent)
            draw.rectangle([indent - 4, y - 2, indent + box_w, y + 30], fill=GRAY_VLIGHT)
            # Truncate if too long
            display = line
            while _text_w(draw, display, code_font) > W - MARGIN - indent - 12 and len(display) > 3:
                display = display[:-1]
            if display != line:
                display = display[:-1] + "…"
            draw.text((indent + 4, y + 2), display, fill=BLACK, font=code_font)
            y += 36

    return img