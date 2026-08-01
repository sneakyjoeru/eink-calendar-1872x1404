"""Main app — FastAPI server + background scheduler.

Runs the e-ink calendar: periodically fetches Google Calendar events,
renders the current view, and displays it via the C IT8951 driver.
Exposes a settings web UI on the LAN for configuration.
"""
import asyncio
import datetime
import json
import logging
import socket
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from . import config, settings_store, driver, calendar_client, render, wifi_setup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eink.main")

app = FastAPI(title="E-Ink Calendar")

# ---- State ----
_last_events: list[dict] = []
_last_events_hash: str = ""
_last_time_line_render: float = 0.0
_last_render_date: str = ""  # track day changes for full refresh
_render_lock = threading.Lock()
_last_render_duration: float = 2.0  # measured render time (updated each render)


def _get_lan_ip() -> str:
    """Detect the LAN IP address of the Pi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "192.168.0.199"


def _scheme() -> str:
    """Return 'https' if SSL is enabled, else 'http'."""
    return "https" if config.SSL_ENABLED else "http"


def _detect_timezone() -> str:
    """Auto-detect timezone from IP via ip-api.com. Returns IANA name or empty."""
    try:
        r = requests.get("http://ip-api.com/json", timeout=3)
        data = r.json()
        if data.get("status") == "success" and data.get("timezone"):
            return data["timezone"]
    except Exception:
        pass
    return ""


def _auto_detect_timezone_async():
    """Run timezone detection in a background thread — don't block startup."""
    def _run():
        detected = _detect_timezone()
        if detected:
            s = settings_store.load()
            if not s.get("timezone"):
                settings_store.update({"timezone": detected})
                logger.info("Timezone auto-detected: %s", detected)
    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _now() -> datetime.datetime:
    """Get current datetime in the configured timezone.

    Accepts IANA names (Europe/Moscow) or UTC offsets (+3, +1.5, -4).
    """
    s = settings_store.load()
    tz_name = s.get("timezone", "").strip()
    if tz_name:
        tz = _parse_timezone(tz_name)
        if tz:
            return datetime.datetime.now(tz)
    return datetime.datetime.now()


def _parse_timezone(val: str) -> Optional[datetime.tzinfo]:
    """Parse a timezone string: IANA name or UTC offset like +3, +1.5, -4.

    Returns a tzinfo object or None if parsing fails.
    """
    val = val.strip()
    if not val:
        return None
    # Try IANA name first (e.g. Europe/Moscow)
    try:
        return ZoneInfo(val)
    except (KeyError, ValueError):
        pass
    # Try UTC offset (e.g. +3, +1.5, -4, +05:30)
    try:
        mul = 1
        s = val
        if s.startswith("+"):
            s = s[1:]
        elif s.startswith("-"):
            mul = -1
            s = s[1:]
        else:
            return None
        if ":" in s:
            parts = s.split(":")
            hours = float(parts[0]) + float(parts[1]) / 60.0
        else:
            hours = float(s)
        minutes = int(hours * 60 * mul)
        return datetime.timezone(datetime.timedelta(minutes=minutes))
    except (ValueError, IndexError):
        pass
    return None


def _events_hash(events: list[dict]) -> str:
    """Stable hash of event list to detect changes."""
    parts = []
    for ev in events:
        parts.append(f"{ev['id']}|{ev['summary']}|{ev['start']}|{ev['end']}|{ev['all_day']}")
    return "|".join(sorted(parts))


# ---- Rendering pipeline ----

def do_render(force: bool = False, force_full: bool = False,
              full_refresh_repeats: int = 0) -> bool:
    """Fetch events + render + display. Thread-safe with timeout.
    force_full: bypass diff, do full screen refresh.
    full_refresh_repeats: number of full-screen GC16 clean refresh passes.
        0 = auto-determine (2 for day change, user setting for interval/dim,
        caller-specified for startup/manual)."""
    logger.info("do_render(force=%s) starting", force)
    _render_start = time.time()
    if not _render_lock.acquire(timeout=120):
        logger.error("do_render: could not acquire render lock within 120s")
        return False
    try:
        global _last_events, _last_events_hash, _last_render_date, _last_render_duration
        settings = settings_store.load()
        now = _now()

        # Fetch events for the visible range
        if settings["view_mode"] == "month":
            start = datetime.datetime(now.year, now.month, 1)
            end = start + datetime.timedelta(days=42)
        elif settings["view_mode"] == "35days":
            start = now - datetime.timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + datetime.timedelta(days=35)
        elif settings["view_mode"] == "7days":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + datetime.timedelta(days=7)
        else:  # week
            start = now - datetime.timedelta(days=now.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + datetime.timedelta(days=7)

        events = calendar_client.fetch_events(
            start, end, settings.get("selected_calendars") or None
        )

        new_hash = _events_hash(events)
        events_changed = (new_hash != _last_events_hash)

        _last_events = events
        _last_events_hash = new_hash

        # Check for day change FIRST — before the early-return for "no event
        # changes". In week view, a day change within the same week fetches the
        # same events, so events_changed would be False and the day change would
        # never be detected if this check came after the early return. A day
        # change must force a full clean refresh even when events are identical.
        global _last_render_date
        today_str = now.strftime("%Y-%m-%d")
        day_changed = bool(_last_render_date and _last_render_date != today_str)
        if day_changed:
            force_full = True
            full_refresh_repeats = 2  # day change: 2 full clean refreshes
            logger.info("Day changed (%s → %s), forcing full refresh (2x)", _last_render_date, today_str)
        _last_render_date = today_str

        if not force and not events_changed and not day_changed:
            logger.debug("No event changes, skipping render")
            return False

        # Optional: when "fullscreen refresh when events are dimmed" is enabled,
        # force a full clean refresh whenever the event set changes (an event
        # ended/started/disappeared). Off by default — regional updates handle
        # the change without a full-screen flash. This is the opt-in replacement
        # for the removed automatic "event finish" full refresh.
        if not force_full and events_changed and settings.get("fullscreen_on_dim", False):
            force_full = True
            full_refresh_repeats = max(1, int(settings.get("hard_refresh_count", 1)))
            logger.info("Events changed + fullscreen_on_dim enabled, forcing full refresh (%dx)",
                        full_refresh_repeats)

        # Check full refresh interval setting
        if not force_full:
            full_interval = settings.get("full_refresh_interval_hours", 0)
            if full_interval and driver.needs_full_refresh(full_interval):
                force_full = True
                full_refresh_repeats = max(1, int(settings.get("hard_refresh_count", 1)))
                logger.info("Full refresh interval (%dh) elapsed, forcing full refresh (%dx)",
                            full_interval, full_refresh_repeats)

        img = render.render_calendar(
            view_mode=settings["view_mode"],
            events=events,
            day_start=settings["day_start"],
            day_end=settings["day_end"],
            max_full_day=settings["max_full_day_events"],
            time_format=settings.get("time_format", "24h"),
            date_format=settings.get("date_format", ""),
            settings_url=f"{_scheme()}://{_get_lan_ip()}:{config.APP_PORT}",
            crossed_event_dim=settings.get("crossed_event_dim", False),
            dim_past_events=settings.get("dim_past_events", False),
            text_size_modifier=settings.get("text_size_modifier", 0),
            now=now,
        )
        ok = driver.render_to_screen(img, brightness=settings.get("brightness", 1.4),
                                     force_full=force_full,
                                     update_mode=settings.get("update_mode", "soft"),
                                     refresh_border_mm=settings.get("refresh_border_mm", 5),
                                     full_refresh_repeats=full_refresh_repeats)
        if ok:
            _last_render_duration = time.time() - _render_start
            logger.info("Screen updated (events_changed=%s, %d events, %.1fs)",
                        events_changed, len(events), _last_render_duration)
        return ok
    except Exception as e:
        logger.error("do_render exception: %s", e)
        return False
    finally:
        _render_lock.release()


def render_setup_screen() -> bool:
    """Show QR code + LAN IP on the e-ink for initial setup."""
    lan_ip = _get_lan_ip()
    port = config.APP_PORT
    scheme = _scheme()
    url = f"{scheme}://{lan_ip}:{port}/settings"
    img = render.render_qr_setup(url, scheme, lan_ip, port)
    return driver.render_to_screen(img, brightness=1.0)


def render_status_screen(msg: str, sub: str = "") -> bool:
    img = render.render_status(msg, sub)
    return driver.render_to_screen(img, brightness=1.0)


# ---- Background scheduler ----
_scheduler_running = True


def background_loop():
    """Background thread: poll events + update time line."""
    global _scheduler_running, _last_time_line_render
    logger.info("Background scheduler started")

    while _scheduler_running:
        try:
            s = settings_store.load()

            if not calendar_client.is_authenticated():
                if not calendar_client.is_configured():
                    lan_ip = _get_lan_ip()
                    img = render.render_setup_required(lan_ip, config.APP_PORT, ssl=config.SSL_ENABLED)
                    driver.render_to_screen(img, brightness=1.0)
                # Show setup screen every 5 min
                time.sleep(300)
                continue

            # Event poll (checks for event changes in all views)
            poll_interval = s.get("event_poll_interval_sec", 60)
            view_mode = s.get("view_mode", "week")
            tl_interval_min = s.get("time_line_interval_min", 15)

            # For 1-min smooth updates, poll more frequently to catch the minute boundary
            if tl_interval_min == 1 and view_mode in ("week", "7days"):
                effective_poll = min(poll_interval, 5)
            else:
                effective_poll = poll_interval

            # Time-line update — only for week/7days views
            # Tied to fraction of hour: update at X:00, X:15, X:30, X:45 etc.
            tl_interval = tl_interval_min * 60

            # Determine if this poll should trigger a time-line update
            now_dt = _now()
            now_ts = time.time()
            should_update_tl = False

            if tl_interval_min == 1:
                # 1-min: trigger when we're within 8s of the next minute boundary
                # This gives enough time to prepare the image before :00
                secs_to_next = 60 - now_dt.second
                if secs_to_next <= 9 and secs_to_next > 0:
                    last_min = datetime.datetime.fromtimestamp(_last_time_line_render).strftime("%Y-%m-%d %H:%M")
                    next_min = (now_dt + datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")
                    should_update_tl = (last_min != next_min)
            else:
                # Aligned to hour: 0, 15, 30, 45 etc.
                if now_dt.minute % tl_interval_min == 0 and now_dt.second < effective_poll:
                    should_update_tl = (now_ts - _last_time_line_render >= tl_interval)

            # Skip the time-line update when the current time is outside the
            # configured day range. Outside day_start..day_end the time-line is
            # drawn as a static placeholder (fixed position), so there is nothing
            # meaningful to refresh every tick — avoid unnecessary partial (or
            # full) refreshes, especially when partial refresh is selected.
            if should_update_tl and view_mode in ("week", "7days"):
                day_start = s.get("day_start", "07:00")
                day_end = s.get("day_end", "23:00")
                ds_h, ds_m = (int(x) for x in day_start.split(":"))
                de_h, de_m = (int(x) for x in day_end.split(":"))
                ds_min = ds_h * 60 + ds_m
                de_min = de_h * 60 + de_m
                # For 1-min updates, target_now is the NEXT minute; check that too.
                check_min = now_dt.hour * 60 + now_dt.minute
                if tl_interval_min == 1:
                    check_min = check_min + 1
                if check_min < ds_min or check_min > de_min:
                    should_update_tl = False
                    logger.debug("Time-line placeholder outside day range (%s–%s), skipping update",
                                 day_start, day_end)

            if view_mode in ("week", "7days") and should_update_tl:
                # For 1-min interval: render with current minute and display immediately
                # For larger intervals: pre-render then wait for exact boundary
                if tl_interval_min == 1:
                    # Pre-render for the NEXT minute, then wait for :00 to display
                    target_now = (now_dt + datetime.timedelta(minutes=1)).replace(second=0, microsecond=0)
                    _render_start = time.time()

                    if not _render_lock.acquire(timeout=120):
                        pass
                    try:
                        settings = settings_store.load()
                        if settings["view_mode"] == "7days":
                            ev_start = target_now.replace(hour=0, minute=0, second=0, microsecond=0)
                            ev_end = ev_start + datetime.timedelta(days=7)
                        else:
                            ev_start = target_now - datetime.timedelta(days=target_now.weekday())
                            ev_start = ev_start.replace(hour=0, minute=0, second=0, microsecond=0)
                            ev_end = ev_start + datetime.timedelta(days=7)

                        events = calendar_client.fetch_events(
                            ev_start, ev_end, settings.get("selected_calendars") or None)

                        img = render.render_calendar(
                            view_mode=settings["view_mode"],
                            events=events,
                            day_start=settings["day_start"],
                            day_end=settings["day_end"],
                            max_full_day=settings["max_full_day_events"],
                            time_format=settings.get("time_format", "24h"),
                            date_format=settings.get("date_format", ""),
                            settings_url=f"{_scheme()}://{_get_lan_ip()}:{config.APP_PORT}",
                            crossed_event_dim=settings.get("crossed_event_dim", False),
                            dim_past_events=settings.get("dim_past_events", False),
                            text_size_modifier=settings.get("text_size_modifier", 0),
                            now=target_now,
                        )
                    finally:
                        _render_lock.release()

                    prepare_time = time.time() - _render_start

                    # Wait for exact minute boundary (use wall clock for sleep timing))
                    now_dt2 = datetime.datetime.now()
                    sleep_sec = 60 - now_dt2.second - now_dt2.microsecond / 1e6
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)

                    driver.render_to_screen(img, brightness=settings.get("brightness", 1.4),
                                            force_full=False,
                                            update_mode=settings.get("update_mode", "soft"),
                                            refresh_border_mm=settings.get("refresh_border_mm", 5))
                    _last_time_line_render = time.time()
                    logger.info("Time-line regional update (1-min): prepared in %.1fs, landed at :%02d.%01d",
                                prepare_time,
                                int(datetime.datetime.now().second),
                                int(datetime.datetime.now().microsecond / 100000))
                else:
                    # Larger interval: pre-render then wait for exact boundary
                    _render_start = time.time()

                    # Compute target time = start of this interval slot
                    target_now = now_dt.replace(second=0, microsecond=0)

                    # --- Phase 1: fetch events + render to image (no display) ---
                    if not _render_lock.acquire(timeout=120):
                        pass
                    try:
                        settings = settings_store.load()
                        if settings["view_mode"] == "7days":
                            ev_start = target_now.replace(hour=0, minute=0, second=0, microsecond=0)
                            ev_end = ev_start + datetime.timedelta(days=7)
                        else:
                            ev_start = target_now - datetime.timedelta(days=target_now.weekday())
                            ev_start = ev_start.replace(hour=0, minute=0, second=0, microsecond=0)
                            ev_end = ev_start + datetime.timedelta(days=7)

                        events = calendar_client.fetch_events(
                            ev_start, ev_end, settings.get("selected_calendars") or None)

                        img = render.render_calendar(
                            view_mode=settings["view_mode"],
                            events=events,
                            day_start=settings["day_start"],
                            day_end=settings["day_end"],
                            max_full_day=settings["max_full_day_events"],
                            time_format=settings.get("time_format", "24h"),
                            date_format=settings.get("date_format", ""),
                            settings_url=f"{_scheme()}://{_get_lan_ip()}:{config.APP_PORT}",
                            crossed_event_dim=settings.get("crossed_event_dim", False),
                            dim_past_events=settings.get("dim_past_events", False),
                            text_size_modifier=settings.get("text_size_modifier", 0),
                            now=target_now,
                        )
                    finally:
                        _render_lock.release()

                    prepare_time = time.time() - _render_start

                    # --- Phase 2: wait for exact minute boundary, then display ---
                    now_dt2 = datetime.datetime.now()
                    sleep_sec = 60 - now_dt2.second - now_dt2.microsecond / 1e6
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)

                    display_start = time.time()
                    driver.render_to_screen(img, brightness=settings.get("brightness", 1.4),
                                            force_full=False,
                                            update_mode=settings.get("update_mode", "soft"),
                                            refresh_border_mm=settings.get("refresh_border_mm", 5))
                    _last_time_line_render = time.time()
                    display_time = _last_time_line_render - display_start
                    logger.info("Time-line regional update: prepared in %.1fs, display %.1fs, landed at :%02d.%01d",
                                prepare_time, display_time,
                                int(datetime.datetime.now().second),
                                int(datetime.datetime.now().microsecond / 100000))
            else:
                # Regular poll for event changes (no forced refresh)
                # Skip if smooth interval is 1 min — smooth updates already fetch events
                if not (tl_interval_min == 1 and view_mode in ("week", "7days")):
                    do_render()

            time.sleep(effective_poll)
        except Exception as e:
            logger.error("Background loop error: %s", e)
            time.sleep(60)

    logger.info("Background scheduler stopped")


# ---- FastAPI routes ----

@app.on_event("startup")
async def startup():
    """Start background scheduler + initial render."""
    lan_ip = _get_lan_ip()
    scheme = _scheme()
    logger.info("E-Ink Calendar starting on %s://%s:%d (LAN: %s://%s:%d)",
                scheme, config.APP_HOST, config.APP_PORT,
                scheme, lan_ip, config.APP_PORT)
    logger.info("IT8951 binary: %s", config.IT8951_BINARY)

    # Auto-detect timezone in background (don't block startup)
    _auto_detect_timezone_async()

    # Initial screen
    if not calendar_client.is_configured():
        ssl_on = " [HTTPS]" if config.SSL_ENABLED else ""
        img = render.render_setup_required(lan_ip, config.APP_PORT, ssl=config.SSL_ENABLED)
        driver.render_to_screen(img, brightness=1.0)
    elif not calendar_client.is_authenticated():
        render_setup_screen()
    else:
        # Mandatory full-screen clean refresh on startup/deploy — clears any
        # ghosting residue and ensures the screen matches the current image.
        # 3 passes by default for a thorough clean on deploy.
        do_render(force=True, force_full=True, full_refresh_repeats=3)

    # Start scheduler
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()

    # Start WiFi connectivity monitor (hotspot fallback)
    wifi_setup.start_monitor(interval=120)


@app.get("/", response_class=HTMLResponse)
async def index():
    """Redirect to settings page."""
    return RedirectResponse(url="/settings")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings web UI."""
    s = settings_store.load()
    authenticated = calendar_client.is_authenticated()
    configured = calendar_client.is_configured()
    saved = request.query_params.get("saved") == "1"
    lan_ip = _get_lan_ip()

    cals = []
    cal_error = ""
    if authenticated:
        cals = calendar_client.list_calendars()
        # Check if the result contains an error entry
        if cals and cals[0].get("_error"):
            cal_error = cals[0]["_error"]
            cals = []

    cal_checkboxes = ""
    for cal in cals:
        checked = "checked" if cal["id"] in s.get("selected_calendars", []) else ""
        cal_checkboxes += f"""
        <label class="cal-item">
            <input type="checkbox" name="selected_calendars" value="{cal['id']}" {checked}>
            <span class="cal-dot" style="background:{cal['color']}"></span>
            <span>{cal['summary']}</span>
        </label>"""
    # Format error message for display
    if cal_error:
        # Extract helpful URL from the error message
        help_url = ""
        if "https://console.developers.google.com" in cal_error:
            start = cal_error.find("https://console.developers.google.com")
            end = cal_error.find(" ", start)
            help_url = cal_error[start:end] if end > start else cal_error[start:]
        cal_error = f'''
        <div class="alert" style="margin-top:8px">
          ⚠️ <b>Calendar API error</b><br>
          <span style="font-size:0.8em">{cal_error[:200]}</span>
          {f'<br><a href="{help_url}" target="_blank" style="color:#e94560;font-size:0.8em">Enable Calendar API →</a>' if help_url else ''}
        </div>'''

    auth_section = ""
    if not configured:
        auth_section = '''
        <div class="alert">⚠️ Place your <code>client_secret.json</code> below:</div>
        <div style="display:flex;gap:8px">
          <input type="file" id="secretFile" accept=".json" style="flex:1;padding:6px;background:#0f3460;color:#eee;border-radius:6px;border:1px solid #333;font-size:0.85em">
          <button class="btn btn-small" onclick="uploadSecret()" style="white-space:nowrap">Upload</button>
        </div>
        <p id="uploadStatus" style="font-size:0.8em;margin-top:6px"></p>
        '''
    elif not authenticated:
        auth_section = '''
        <p style="margin-bottom:10px">Click below to get the authorization link, then paste the code back.</p>
        <button class="btn btn-auth" onclick="startGoogleAuth()">🔐 Login with Google</button>
        <div id="authFlow" style="display:none;margin-top:12px">
          <p>1. Open this link in your browser:</p>
          <p><a id="authUrl" href="#" target="_blank" style="word-break:break-all;color:#4285F4"></a></p>
          <p style="margin-top:10px">2. Authorize. When the redirect fails, <b>copy the full URL</b> from the address bar and paste it below.</p>
          <p>3. Paste the redirect URL or just the code:</p>
          <input id="authCode" type="text" style="width:100%;padding:8px;margin-bottom:8px" placeholder="Paste the full redirect URL or just the code">
          <button class="btn btn-primary" onclick="exchangeCode()">✓ Exchange Code</button>
          <p id="authStatus" style="margin-top:8px;font-size:0.85em"></p>
        </div>
        '''
    else:
        auth_section = '<span class="badge badge-ok">✓ Google connected</span> <a class="btn btn-small" href="/auth/logout">Logout</a>'

    # Pre-compute selected attribute for each view mode (can't use Python
    # expressions in str.format() templates)
    sel_month = "selected" if s["view_mode"] == "month" else ""
    sel_35days = "selected" if s["view_mode"] == "35days" else ""
    sel_week = "selected" if s["view_mode"] == "week" else ""
    sel_7days = "selected" if s["view_mode"] == "7days" else ""
    sel_24h = "selected" if s.get("time_format", "24h") == "24h" else ""
    sel_12h = "selected" if s.get("time_format", "24h") == "12h" else ""
    tz = s.get("timezone", "")
    date_fmt = s.get("date_format", "")
    sel_df_empty = 'selected' if not date_fmt else ''
    sel_df_B_Y = 'selected' if date_fmt == '%B %Y' else ''
    sel_df_B_d_Y = 'selected' if date_fmt == '%B %d, %Y' else ''
    sel_df_Ymd_a = 'selected' if date_fmt == '%Y.%m.%d %a' else ''
    sel_df_d_B_Y = 'selected' if date_fmt == '%d %B %Y' else ''
    sel_a_b_d = 'selected' if date_fmt == '%a %b %d' else ''
    sel_d_m_Y = 'selected' if date_fmt == '%d.%m.%Y' else ''
    sel_m_d_Y = 'selected' if date_fmt == '%m/%d/%Y' else ''
    sel_AB_d = 'selected' if date_fmt == '%A, %B %d' else ''
    sel_Y_m_d = 'selected' if date_fmt == '%Y-%m-%d' else ''
    sel_d_b_Y = 'selected' if date_fmt == '%d %b %Y' else ''
    sel_b_d_Y = 'selected' if date_fmt == '%b %d, %Y' else ''
    sel_AB_d_Y = 'selected' if date_fmt == '%A, %B %d, %Y' else ''
    dim_past = s.get("dim_past_events", False)
    crossed_dim = s.get("crossed_event_dim", False)
    ts_mod = s.get("text_size_modifier", 0)

    return _SETTINGS_HTML.format(
        saved_html='<div class="badge badge-ok" style="display:block;text-align:center;margin-bottom:12px;padding:8px">✓ Settings saved</div>' if saved else "",
        auth_section=auth_section,
        sel_month=sel_month,
        sel_35days=sel_35days,
        sel_week=sel_week,
        sel_7days=sel_7days,
        sel_24h=sel_24h,
        sel_12h=sel_12h,
        day_start=s["day_start"],
        day_end=s["day_end"],
        max_fd=s["max_full_day_events"],
        tl_interval=s["time_line_interval_min"],
        sel_tl_1='selected' if s['time_line_interval_min']==1 else '',
        sel_tl_5='selected' if s['time_line_interval_min']==5 else '',
        sel_tl_10='selected' if s['time_line_interval_min']==10 else '',
        sel_tl_15='selected' if s['time_line_interval_min']==15 else '',
        sel_tl_30='selected' if s['time_line_interval_min']==30 else '',
        sel_tl_60='selected' if s['time_line_interval_min']==60 else '',
        fr_val=s.get('full_refresh_interval_hours', 6),
        sel_fr_0='selected' if s.get('full_refresh_interval_hours', 6)==0 else '',
        sel_fr_0_5='selected' if s.get('full_refresh_interval_hours', 6)==0.5 else '',
        sel_fr_1='selected' if s.get('full_refresh_interval_hours', 6)==1 else '',
        sel_fr_1_5='selected' if s.get('full_refresh_interval_hours', 6)==1.5 else '',
        sel_fr_2='selected' if s.get('full_refresh_interval_hours', 6)==2 else '',
        sel_fr_3='selected' if s.get('full_refresh_interval_hours', 6)==3 else '',
        sel_fr_6='selected' if s.get('full_refresh_interval_hours', 6)==6 else '',
        sel_fr_12='selected' if s.get('full_refresh_interval_hours', 6)==12 else '',
        sel_fr_24='selected' if s.get('full_refresh_interval_hours', 6)==24 else '',
        um=s.get('update_mode', 'soft'),
        sel_um_smooth='selected' if s.get('update_mode', 'soft')=='soft' else '',
        sel_um_hard='selected' if s.get('update_mode', 'soft')=='hard' else '',
        db_mm=s.get('refresh_border_mm', 5),
        sel_db_0='selected' if s.get('refresh_border_mm', 5)==0 else '',
        sel_db_2='selected' if s.get('refresh_border_mm', 5)==2 else '',
        sel_db_5='selected' if s.get('refresh_border_mm', 5)==5 else '',
        sel_db_10='selected' if s.get('refresh_border_mm', 5)==10 else '',
        sel_db_15='selected' if s.get('refresh_border_mm', 5)==15 else '',
        sel_db_20='selected' if s.get('refresh_border_mm', 5)==20 else '',
        fullscreen_on_dim='checked' if s.get('fullscreen_on_dim', False) else '',
        sel_hrc_1='selected' if s.get('hard_refresh_count', 1)==1 else '',
        sel_hrc_2='selected' if s.get('hard_refresh_count', 1)==2 else '',
        sel_hrc_3='selected' if s.get('hard_refresh_count', 1)==3 else '',
        sel_hrc_4='selected' if s.get('hard_refresh_count', 1)==4 else '',
        sel_hrc_5='selected' if s.get('hard_refresh_count', 1)==5 else '',
        sel_fd_0='selected' if s['max_full_day_events']==0 else '',
        sel_fd_1='selected' if s['max_full_day_events']==1 else '',
        sel_fd_2='selected' if s['max_full_day_events']==2 else '',
        sel_fd_3='selected' if s['max_full_day_events']==3 else '',
        poll_interval=s["event_poll_interval_sec"],
        brightness=s.get("brightness", 1.4),
        timezone=tz,
        date_fmt=date_fmt,
        sel_df_empty=sel_df_empty,
        sel_df_B_Y=sel_df_B_Y,
        sel_df_B_d_Y=sel_df_B_d_Y,
        sel_df_Ymd_a=sel_df_Ymd_a,
        sel_df_d_B_Y=sel_df_d_B_Y,
        sel_a_b_d=sel_a_b_d,
        sel_d_m_Y=sel_d_m_Y,
        sel_m_d_Y=sel_m_d_Y,
        sel_AB_d=sel_AB_d,
        sel_Y_m_d=sel_Y_m_d,
        sel_d_b_Y=sel_d_b_Y,
        sel_b_d_Y=sel_b_d_Y,
        sel_AB_d_Y=sel_AB_d_Y,
        dim_past='checked' if dim_past else '',
        crossed_dim='checked' if crossed_dim else '',
        ts_mod=ts_mod,
        cal_checkboxes=cal_checkboxes,
        cal_error=cal_error,
        lan_ip=lan_ip,
        port=config.APP_PORT,
    )


class SettingsUpdate(BaseModel):
    view_mode: Optional[str] = None
    day_start: Optional[str] = None
    day_end: Optional[str] = None
    max_full_day_events: Optional[int] = None
    selected_calendars: Optional[list[str]] = None
    time_line_interval_min: Optional[int] = None
    event_poll_interval_sec: Optional[int] = None
    brightness: Optional[float] = None
    timezone: Optional[str] = None
    date_format: Optional[str] = None


@app.post("/api/settings")
async def update_settings(request: Request):
    """Save settings from form POST, trigger render, redirect back."""
    fd = await request.form()
    data = {
        "view_mode": fd.get("view_mode", "week"),
        "day_start": fd.get("day_start", "07:00"),
        "day_end": fd.get("day_end", "23:00"),
        "max_full_day_events": int(fd.get("max_full_day_events", 3)),
        "time_line_interval_min": int(fd.get("time_line_interval_min", 15)),
        "event_poll_interval_sec": int(fd.get("event_poll_interval_sec", 60)),
        "full_refresh_interval_hours": float(fd.get("full_refresh_interval_hours", 6)),
        "update_mode": fd.get("update_mode", "soft"),
        "refresh_border_mm": float(fd.get("refresh_border_mm", 5)),
        "fullscreen_on_dim": fd.get("fullscreen_on_dim") == "1",
        "hard_refresh_count": int(fd.get("hard_refresh_count", 1)),
        "brightness": float(fd.get("brightness", 1.4)),
        "timezone": fd.get("timezone", ""),
        "time_format": fd.get("time_format", "24h"),
        "date_format": fd.get("date_format", ""),
        "crossed_event_dim": fd.get("crossed_event_dim") == "1",
        "dim_past_events": fd.get("dim_past_events") == "1",
        "text_size_modifier": int(fd.get("text_size_modifier", 0)),
        "selected_calendars": fd.getlist("selected_calendars"),
    }
    logger.info("Settings updated: %s", {k: v for k, v in data.items() if k != "selected_calendars"})
    settings_store.update(data)
    # Trigger render immediately so view mode / settings take effect right away
    threading.Thread(target=_safe_render, daemon=True).start()
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.get("/api/render")
async def trigger_render():
    """Trigger render via GET link, redirect back."""
    threading.Thread(target=_safe_render, daemon=True).start()
    return RedirectResponse(url="/settings", status_code=303)


def _safe_render():
    """Call do_render with full exception logging. Save & Render always does full hard refresh.
    The number of full refresh passes is the user-configurable hard_refresh_count setting."""
    settings = settings_store.load()
    repeats = max(1, int(settings.get("hard_refresh_count", 1)))
    logger.info("Manual render triggered (full hard refresh, %dx)", repeats)
    try:
        ok = do_render(force=True, force_full=True, full_refresh_repeats=repeats)
        logger.info("Manual render completed: %s", ok)
    except Exception as e:
        logger.error("Manual render failed: %s", e)


@app.get("/api/status")
async def status():
    return {
        "authenticated": calendar_client.is_authenticated(),
        "configured": calendar_client.is_configured(),
        "lan_ip": _get_lan_ip(),
        "port": config.APP_PORT,
        "settings": settings_store.load(),
    }


@app.get("/health")
async def health():
    """Quick health check — returns 200 if server is alive."""
    return {"ok": True}


@app.get("/image")
async def get_last_image():
    """Serve the last rendered e-ink image (from IT8951 diff cache)."""
    img_path = "/tmp/it8951_last.png"
    if not Path(img_path).exists():
        return JSONResponse({"error": "No image cached"}, status_code=404)
    return FileResponse(img_path, media_type="image/png", filename="eink-last.png")


@app.get("/preview", response_class=HTMLResponse)
async def preview_page():
    """Live preview of the e-ink display — auto-refreshes every 15 seconds."""
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E-Ink Preview</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: #1a1a2e; color: #eee; font-family: -apple-system, sans-serif; display: flex; flex-direction: column; align-items: center; padding: 20px; min-height: 100vh; }
h1 { font-size: 1.2em; margin-bottom: 12px; color: #e94560; }
img { max-width: 100%; max-height: 80vh; border-radius: 8px; box-shadow: 0 4px 24px rgba(0,0,0,0.4); image-rendering: auto; }
.status { font-size: 0.75em; color: #666; margin-top: 8px; }
</style>
</head>
<body>
<h1>📺 E-Ink Live Preview</h1>
<img id="preview" src="/image" alt="E-Ink Display" onload="document.getElementById('time').textContent='Updated: ' + new Date().toLocaleTimeString()">
<p class="status" id="time">Loading...</p>
<script>
function refresh() {
    var img = document.getElementById('preview');
    img.src = '/image?t=' + Date.now();
}
setInterval(refresh, 15000);
</script>
</body>
</html>"""


@app.post("/api/upload-secret")
async def upload_secret(file: UploadFile = File(...)):
    """Upload client_secret.json for Google OAuth."""
    if not file.filename.endswith(".json"):
        return JSONResponse({"error": "File must be a .json"}, status_code=400)
    try:
        content = await file.read()
        dest = config.BASE_DIR / "config" / "client_secret.json"
        dest.write_bytes(content)
        logger.info("client_secret.json uploaded (%d bytes)", len(content))
        return {"ok": True}
    except Exception as e:
        logger.error("Upload failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ---- WiFi setup routes ----

_WIFI_SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E-Ink Calendar — WiFi Setup</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #1a1a2e; color: #eee; padding: 20px; max-width: 600px; margin: 0 auto; }}
h1 {{ font-size: 1.3em; margin-bottom: 12px; text-align: center; }}
.card {{ background: #16213e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
.card h2 {{ font-size: 1em; margin-bottom: 10px; color: #e94560; }}
label {{ display: block; margin-bottom: 8px; font-size: 0.9em; }}
input[type="text"], input[type="password"] {{ width: 100%; padding: 8px; border-radius: 6px;
  border: 1px solid #333; background: #0f3460; color: #eee; font-size: 0.9em; }}
.btn {{ display: inline-block; padding: 10px 20px; border-radius: 8px;
  border: none; cursor: pointer; font-size: 0.9em; text-align: center; }}
.btn-primary {{ background: #e94560; color: white; width: 100%; margin-top: 12px; }}
.btn-small {{ background: #333; color: #eee; font-size: 0.8em; padding: 6px 12px; }}
.network-list {{ list-style: none; }}
.network-list li {{ padding: 8px; margin: 4px 0; background: #0f3460; border-radius: 6px;
  cursor: pointer; display: flex; justify-content: space-between; align-items: center; }}
.network-list li:hover {{ background: #1a4a7a; }}
.signal {{ color: #888; font-size: 0.8em; }}
#status {{ margin-top: 12px; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>📶 WiFi Setup</h1>
<div class="card">
  <h2>Available Networks</h2>
  <ul class="network-list" id="networkList"><li style="color:#888">Scanning...</li></ul>
  <p style="margin-top:8px;font-size:0.8em;color:#666">
    <a href="#" onclick="rescan()" style="color:#e94560">↻ Rescan</a>
  </p>
</div>
<div class="card">
  <h2>Connect</h2>
  <label>Network Name
    <input type="text" id="ssid" placeholder="Select from list above or type manually">
  </label>
  <label>Password (optional)
    <input type="password" id="password" placeholder="Leave empty for open networks">
  </label>
  <button class="btn btn-primary" onclick="connect()">Connect</button>
  <p id="status"></p>
</div>
<script>
async function scan() {{
  try {{
    const r = await fetch('/api/wifi-scan');
    const data = await r.json();
    const list = document.getElementById('networkList');
    list.innerHTML = '';
    if (data.networks && data.networks.length) {{
      data.networks.forEach(n => {{
        const li = document.createElement('li');
        const lock = n.encrypted ? '🔒' : '🔓';
        li.innerHTML = `<span>${{lock}} ${{n.ssid}}</span><span class="signal">${{n.signal}} dBm</span>`;
        li.onclick = () => document.getElementById('ssid').value = n.ssid;
        list.appendChild(li);
      }});
    }} else {{
      list.innerHTML = '<li style="color:#888">No networks found</li>';
    }}
  }} catch(e) {{
    document.getElementById('networkList').innerHTML = '<li style="color:#e94560">Scan failed</li>';
  }}
}}
async function rescan() {{
  document.getElementById('networkList').innerHTML = '<li style="color:#888">Scanning...</li>';
  scan();
}}
async function connect() {{
  const ssid = document.getElementById('ssid').value.trim();
  const password = document.getElementById('password').value;
  const status = document.getElementById('status');
  if (!ssid) {{ alert('Enter a network name'); return; }}
  status.textContent = 'Connecting...';
  try {{
    const r = await fetch('/api/wifi-connect', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ssid, password}}),
    }});
    const data = await r.json();
    if (data.ok) {{
      status.textContent = '✓ Connected! The hotspot will turn off. Page will reload in 10s...';
      setTimeout(() => location.reload(), 10000);
    }} else {{
      status.textContent = '✗ ' + (data.error || 'Connection failed');
    }}
  }} catch(e) {{
    status.textContent = 'Error: ' + e.message;
  }}
}}
scan();
</script>
</body>
</html>"""


@app.get("/wifi-setup", response_class=HTMLResponse)
async def wifi_setup_page():
    """WiFi setup page."""
    return _WIFI_SETUP_HTML


@app.get("/api/wifi-scan")
async def wifi_scan():
    """Scan available WiFi networks."""
    networks = wifi_setup.scan_networks()
    return {"networks": networks}


@app.post("/api/wifi-connect")
async def wifi_connect(request: Request):
    """Connect to a WiFi network."""
    data = await request.json()
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    if not ssid:
        return JSONResponse({"ok": False, "error": "SSID is required"}, status_code=400)
    try:
        # Stop hotspot first
        wifi_setup.stop_hotspot()
        # Write wpa_supplicant config
        import subprocess
        if password:
            subprocess.run(
                ["wpa_passphrase", ssid, password],
                capture_output=True, text=True, timeout=10, check=True
            )
        # Configure network
        conf = f'''ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country=RU

network={{
    ssid="{ssid}"
    {"psk=\"" + password + "\"" if password else "key_mgmt=NONE"}
}}
'''
        Path("/etc/wpa_supplicant/wpa_supplicant.conf").write_text(conf)
        subprocess.run(["wpa_cli", "-i", wifi_setup.HOTSPOT_IFACE, "reconfigure"],
                       capture_output=True, timeout=10)
        logger.info("WiFi configured: SSID=%s", ssid)
        return {"ok": True, "ssid": ssid}
    except Exception as e:
        logger.error("WiFi connect failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---- Google OAuth routes ----

# Google blocks IP addresses in redirect URIs. The workaround:
# use http://localhost (the only HTTP exception Google allows) and
# let the user copy the authorization code from the failed redirect manually.

_OAUTH_REDIRECT_URI = "http://localhost:8889/auth/callback"


@app.get("/auth/start")
async def auth_start(request: Request):
    """Start Google OAuth flow. Returns the authorization URL as JSON
    — the user opens it, authorizes, and pastes the resulting code."""
    try:
        auth_url = calendar_client.start_auth(_OAUTH_REDIRECT_URI)
        return {
            "auth_url": auth_url,
            "redirect_uri": _OAUTH_REDIRECT_URI,
        }
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/auth/exchange")
async def auth_exchange(code: str = Form(...)):
    """Exchange an authorization code for tokens."""
    ok, err = calendar_client.complete_auth(code)
    if ok:
        do_render(force=True, force_full=True, full_refresh_repeats=3)
        return {"ok": True}
    return JSONResponse({"error": err or "Code exchange failed"}, status_code=400)


@app.get("/auth/callback")
async def auth_callback(code: str = ""):
    """Handle OAuth callback (direct redirect — may not work for LAN IPs).
    Also renders a page the user can copy the code from."""
    if code:
        ok, _ = calendar_client.complete_auth(code)
        if ok:
            do_render(force=True, force_full=True, full_refresh_repeats=3)
            return RedirectResponse(url="/settings?auth=success")
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;padding:40px;background:#1a1a2e;color:#eee">
    <h2>Authorization Code Received</h2>
    <p>If the code below looks correct, copy it and go to the
    <a href="/settings" style="color:#e94560">settings page</a> to paste it.</p>
    <textarea rows="3" cols="60" readonly>{code}</textarea>
    </body></html>
    """.replace("{code}", code or "No code received"))


@app.get("/auth/logout")
async def auth_logout():
    calendar_client.logout()
    render_setup_screen()
    return RedirectResponse(url="/settings")


# ---- Settings HTML template ----
_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E-Ink Calendar Settings</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg: #14142b; --card: #1d1d3a; --card2: #232347; --input: #2a2a52;
  --border: #343460; --accent: #e94560; --accent2: #4285F4; --text: #ececf5;
  --muted: #8b8bb0; --ok: #4ade80; --ok-bg: #14321f; --warn: #f59e0b; --warn-bg: #3a2a10;
}}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: var(--bg); color: var(--text); padding: 20px; max-width: 960px; margin: 0 auto; line-height: 1.5; }}
header {{ text-align: center; margin-bottom: 24px; }}
header h1 {{ font-size: 1.6em; margin-bottom: 4px; }}
header p {{ color: var(--muted); font-size: 0.85em; }}
.settings-grid {{ display: grid; grid-template-columns: 1fr; gap: 16px; }}
@media (min-width: 700px) {{ .settings-grid {{ grid-template-columns: 1fr 1fr; }} }}
@media (min-width: 1000px) {{ .settings-grid {{ grid-template-columns: 1fr 1fr 1fr; }} }}
.card {{ background: var(--card); border-radius: 14px; padding: 20px; border: 1px solid var(--border); }}
.card h2 {{ font-size: 1.05em; margin-bottom: 14px; color: var(--accent); display: flex; align-items: center; gap: 8px; }}
.card h3 {{ font-size: 0.72em; margin: 18px 0 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1.2px; font-weight: 600; }}
.card h3:first-of-type {{ margin-top: 4px; }}
.field {{ margin-bottom: 14px; }}
.field:last-child {{ margin-bottom: 0; }}
label {{ display: block; font-size: 0.88em; margin-bottom: 6px; font-weight: 500; }}
select, input[type="time"], input[type="number"], input[type="text"] {{
  width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--input); color: var(--text); font-size: 0.92em; }}
select:focus, input:focus {{ outline: none; border-color: var(--accent); }}
input[type="range"] {{ width: 100%; }}
.btn {{ display: inline-block; padding: 11px 22px; border-radius: 10px;
  border: none; cursor: pointer; font-size: 0.92em; text-decoration: none; text-align: center; font-weight: 600; transition: filter .15s; }}
.btn:hover {{ filter: brightness(1.12); }}
.btn-primary {{ background: var(--accent); color: white; width: 100%; margin-top: 16px; font-size: 1em; padding: 13px; }}
.btn-auth {{ background: var(--accent2); color: white; }}
.btn-small {{ background: var(--input); color: var(--text); font-size: 0.82em; padding: 7px 14px; border: 1px solid var(--border); }}
.alert {{ background: var(--warn-bg); border: 1px solid var(--warn); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 12px; font-size: 0.85em; }}
.alert code {{ background: #2a1a0a; padding: 2px 6px; border-radius: 4px; }}
.badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 0.78em; font-weight: 600; }}
.badge-ok {{ background: var(--ok-bg); color: var(--ok); }}
.cal-item {{ display: flex; align-items: center; gap: 8px; padding: 7px 0; cursor: pointer; border-radius: 6px; }}
.cal-item:hover {{ background: var(--card2); }}
.cal-item input {{ width: auto; }}
.cal-dot {{ width: 14px; height: 14px; border-radius: 50%; display: inline-block; flex-shrink: 0; }}
.cal-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2px; }}
.footer {{ text-align: center; font-size: 0.76em; color: var(--muted); margin-top: 24px; padding: 16px; }}
.row {{ display: flex; gap: 12px; }}
.row > div {{ flex: 1; }}
.check-row {{ display: flex; align-items: center; gap: 10px; padding: 9px 12px; background: var(--input); border-radius: 8px; margin-bottom: 8px; cursor: pointer; }}
.check-row input {{ width: auto; }}
.check-row:hover {{ background: var(--card2); }}
.note {{ font-size: 0.74em; color: var(--muted); margin-top: 4px; }}
.range-val {{ display: inline-block; background: var(--input); padding: 2px 8px; border-radius: 6px; font-size: 0.8em; font-weight: 600; }}
.save-bar {{ position: sticky; bottom: 0; margin: 20px -20px 0; padding: 16px 20px; background: linear-gradient(180deg, transparent, var(--bg) 30%); }}
</style>
</head>
<body>
<header>
  <h1>📅 E-Ink Calendar</h1>
  <p>Configure your calendar display — changes apply instantly to the screen.</p>
</header>
{saved_html}

<form id="settingsForm" action="/api/settings" method="POST">
<div class="card" style="margin-bottom:16px">
  <h2>🔐 Google Account</h2>
  {auth_section}
</div>

<div class="settings-grid">
  <div class="card">
    <h2>📅 Calendar &amp; Events</h2>
    <h3>Layout</h3>
    <div class="field">
      <label>View Mode</label>
      <select name="view_mode">
        <option value="month" {sel_month}>Month (current month)</option>
        <option value="35days" {sel_35days}>Month (5 weeks, Mon-start)</option>
        <option value="week" {sel_week}>Week (Mon–Sun)</option>
        <option value="7days" {sel_7days}>7 Days (from today)</option>
      </select>
    </div>
    <div class="field">
      <label>Full-day events shown per day</label>
      <select name="max_full_day_events">
        <option value="0" {sel_fd_0}>Hide all-day events</option>
        <option value="1" {sel_fd_1}>1</option>
        <option value="2" {sel_fd_2}>2</option>
        <option value="3" {sel_fd_3}>3</option>
      </select>
    </div>
    <h3>Calendars to show</h3>
    <div class="cal-grid">
    {cal_checkboxes}
    </div>
    {cal_error}
    <p class="note">Leave all unchecked to show every calendar.</p>
    <h3>Checking for changes</h3>
    <div class="field">
      <label>Check for new events every (seconds)</label>
      <input type="number" name="event_poll_interval_sec" value="{poll_interval}" min="10" max="600">
      <div class="note">Lower = catches new/ended events sooner, more battery/CPU use.</div>
    </div>
    <h3>Look &amp; feel</h3>
    <label class="check-row">
      <input type="checkbox" name="dim_past_events" value="1" {dim_past}>
      <span>Dim past days &amp; ended events</span>
    </label>
    <label class="check-row">
      <input type="checkbox" name="crossed_event_dim" value="1" {crossed_dim}>
      <span>Dim an event once the time line passes it</span>
    </label>
  </div>

  <div class="card">
    <h2>🕐 Time &amp; Date</h2>
    <h3>Day range</h3>
    <div class="row">
      <div class="field"><label>Day starts at</label><input type="time" name="day_start" value="{day_start}"></div>
      <div class="field"><label>Day ends at</label><input type="time" name="day_end" value="{day_end}"></div>
    </div>
    <h3>Formats</h3>
    <div class="field">
      <label>Time format</label>
      <select name="time_format">
        <option value="24h" {sel_24h}>24-hour · 07:00</option>
        <option value="12h" {sel_12h}>12-hour · 7:00 AM</option>
      </select>
    </div>
    <div class="field">
      <label>Date format (title)</label>
      <select name="date_format">
        <option value="" {sel_df_empty}>Default</option>
        <option value="%Y-%m-%d" {sel_Y_m_d}>2026-07-26</option>
        <option value="%Y.%m.%d %a" {sel_df_Ymd_a}>2026.07.26 Sun</option>
        <option value="%d %b %Y" {sel_d_b_Y}>26 Jul 2026</option>
        <option value="%d %B %Y" {sel_df_d_B_Y}>26 July 2026</option>
        <option value="%d.%m.%Y" {sel_d_m_Y}>26.07.2026</option>
        <option value="%m/%d/%Y" {sel_m_d_Y}>07/26/2026</option>
        <option value="%b %d, %Y" {sel_b_d_Y}>Jul 26, 2026</option>
        <option value="%B %Y" {sel_df_B_Y}>July 2026</option>
        <option value="%B %d, %Y" {sel_df_B_d_Y}>July 26, 2026</option>
        <option value="%a %b %d" {sel_a_b_d}>Sun Jul 26</option>
        <option value="%A, %B %d" {sel_AB_d}>Sunday, July 26</option>
        <option value="%A, %B %d, %Y" {sel_AB_d_Y}>Sunday, July 26, 2026</option>
      </select>
    </div>
    <div class="field">
      <label>Timezone</label>
      <input type="text" name="timezone" value="{timezone}" placeholder="Auto-detected from your IP">
      <div class="note">City name (e.g. Europe/Moscow) or UTC offset (e.g. +3).</div>
    </div>
    <h3>Live time line</h3>
    <div class="field">
      <label>Move the time line every</label>
      <select name="time_line_interval_min">
        <option value="1" {sel_tl_1}>1 minute</option>
        <option value="5" {sel_tl_5}>5 minutes</option>
        <option value="10" {sel_tl_10}>10 minutes</option>
        <option value="15" {sel_tl_15}>15 minutes</option>
        <option value="30" {sel_tl_30}>30 minutes</option>
        <option value="60" {sel_tl_60}>1 hour</option>
      </select>
      <div class="note">Only used in Week &amp; 7-day views. Each tick is a small regional refresh.</div>
    </div>
  </div>

  <div class="card">
    <h2>🖥 Screen &amp; Refresh</h2>
    <h3>Appearance</h3>
    <div class="row">
      <div class="field"><label>Contrast / brightness</label>
        <input type="range" name="brightness" min="0.1" max="2.0" step="0.1" value="{brightness}"
               oninput="document.getElementById('brightVal').textContent=this.value">
        <div class="note" style="text-align:center"><span class="range-val" id="brightVal">{brightness}</span></div>
      </div>
      <div class="field"><label>Text size</label>
        <input type="number" name="text_size_modifier" value="{ts_mod}" step="1" min="-8" max="8" style="width:80px">
        <div class="note">+ bigger, − smaller (px)</div>
      </div>
    </div>
    <h3>Regional updates</h3>
    <div class="field">
      <label>Update style for small changes (time line, etc.)</label>
      <select name="update_mode">
        <option value="soft" {sel_um_smooth}>Soft · no flash, keeps old state at edges (recommended)</option>
        <option value="hard" {sel_um_hard}>Hard · brief flash of the changed area</option>
      </select>
      <div class="note">Both refresh only the small changed region. Soft keeps the previous pixels around the change (no flash); Hard briefly flashes the changed area. Full-screen clean refreshes happen on day change or the interval below.</div>
    </div>
    <div class="field">
      <label>Partial refresh area expansion</label>
      <select name="refresh_border_mm">
        <option value="0" {sel_db_0}>None (exact changed area)</option>
        <option value="2" {sel_db_2}>2 mm · ~24 px</option>
        <option value="5" {sel_db_5}>5 mm · ~59 px</option>
        <option value="10" {sel_db_10}>10 mm · ~119 px</option>
        <option value="15" {sel_db_15}>15 mm · ~178 px</option>
        <option value="20" {sel_db_20}>20 mm · ~237 px</option>
      </select>
      <div class="note">Expands the refreshed region by this much on each side. The border keeps the old content, so only the inner changed area visibly updates. Wider = more margin around the change.</div>
    </div>
    <div class="field">
      <label>Full-screen clean refresh</label>
      <select name="full_refresh_interval_hours">
        <option value="0" {sel_fr_0}>Never (only on day change)</option>
        <option value="0.5" {sel_fr_0_5}>Every 30 min</option>
        <option value="1" {sel_fr_1}>Every 1 hour</option>
        <option value="1.5" {sel_fr_1_5}>Every 1.5 hours</option>
        <option value="2" {sel_fr_2}>Every 2 hours</option>
        <option value="3" {sel_fr_3}>Every 3 hours</option>
        <option value="6" {sel_fr_6}>Every 6 hours</option>
        <option value="12" {sel_fr_12}>Every 12 hours</option>
        <option value="24" {sel_fr_24}>Every 24 hours</option>
      </select>
      <div class="note">Wipes the whole screen (GC16) to clear any ghosting.</div>
    </div>
    <label class="check-row">
      <input type="checkbox" name="fullscreen_on_dim" value="1" {fullscreen_on_dim}>
      <span>Full-screen refresh when an event ends (clears dimming ghosting)</span>
    </label>
    <div class="field">
      <label>Full-screen refresh passes (manual / interval / event-end)</label>
      <select name="hard_refresh_count">
        <option value="1" {sel_hrc_1}>1 pass (quick)</option>
        <option value="2" {sel_hrc_2}>2 passes</option>
        <option value="3" {sel_hrc_3}>3 passes (thorough)</option>
        <option value="4" {sel_hrc_4}>4 passes</option>
        <option value="5" {sel_hrc_5}>5 passes (deepest clean)</option>
      </select>
      <div class="note">How many GC16 clean-refresh passes for manual Save &amp; Render, the interval, and event-end triggers. Day change always uses 2; startup/deploy always uses 3.</div>
    </div>
    <a href="/preview" class="btn btn-small" style="display:block;text-align:center;margin-top:10px">🖼 Preview the screen live</a>
  </div>
</div>

<div class="save-bar">
  <button type="submit" class="btn btn-primary">💾 Save &amp; Refresh screen</button>
  <p id="saveStatus" style="text-align:center;margin-top:8px;font-size:0.85em"></p>
</div>
</form>

<div class="footer">
  E-Ink Calendar · {lan_ip}:{port} · 1872×1404 IT8951 · <a href="https://sneakyjoe.live/donate" style="color:inherit;text-decoration:underline">sneakyjoe.live</a>
</div>

<script>
async function startGoogleAuth() {{
  const status = document.getElementById('authStatus');
  status.textContent = 'Getting authorization link...';
  try {{
    const r = await fetch('/auth/start');
    const data = await r.json();
    if (data.auth_url) {{
      document.getElementById('authUrl').href = data.auth_url;
      document.getElementById('authUrl').textContent = data.auth_url;
      document.getElementById('authFlow').style.display = 'block';
      status.textContent = 'Open the link above in your browser, authorize, then paste the code.';
    }} else {{
      status.textContent = 'Error: ' + (data.error || 'Unknown');
    }}
  }} catch(e) {{
    status.textContent = 'Error: ' + e.message;
  }}
}}
async function exchangeCode() {{
  let raw = document.getElementById('authCode').value.trim();
  if (!raw) {{ alert('Paste the redirect URL or code first'); return; }}
  // Extract code= param from URL if full URL was pasted
  const match = raw.match(/[?&]code=([^&]+)/);
  const code = match ? decodeURIComponent(match[1]) : raw;
  const status = document.getElementById('authStatus');
  status.textContent = 'Exchanging code...';
  try {{
    const r = await fetch('/auth/exchange', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
      body: 'code=' + encodeURIComponent(code),
    }});
    if (r.ok) {{
      status.textContent = '✓ Connected! Refreshing...';
      setTimeout(() => location.reload(), 1000);
    }} else {{
      const err = await r.json();
      status.textContent = 'Error: ' + (err.error || 'Exchange failed');
    }}
  }} catch(e) {{
    status.textContent = 'Error: ' + e.message;
  }}
}}
async function uploadSecret() {{
  const fileInput = document.getElementById('secretFile');
  const file = fileInput.files[0];
  if (!file) {{ alert('Select client_secret.json first'); return; }}
  const status = document.getElementById('uploadStatus');
  status.textContent = 'Uploading...';
  const fd = new FormData();
  fd.append('file', file);
  try {{
    const r = await fetch('/api/upload-secret', {{method:'POST', body:fd}});
    const data = await r.json();
    if (data.ok) {{
      status.textContent = '✓ Uploaded! Refreshing...';
      setTimeout(() => location.reload(), 1000);
    }} else {{
      status.textContent = 'Error: ' + (data.error || 'Upload failed');
    }}
  }} catch(e) {{
    status.textContent = 'Error: ' + e.message;
  }}
}}
</script>
</body>
</html>
"""


def main():
    """Entry point — run the FastAPI app with uvicorn."""
    import uvicorn

    # Generate SSL cert on first run
    if config.SSL_ENABLED:
        config.ensure_ssl_cert()

    ssl_kwargs = {}
    if config.SSL_ENABLED:
        cert_path = Path(config.SSL_CERT)
        key_path = Path(config.SSL_KEY)
        if cert_path.exists() and key_path.exists():
            ssl_kwargs["ssl_certfile"] = str(cert_path)
            ssl_kwargs["ssl_keyfile"] = str(key_path)

    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT,
                log_level="info", **ssl_kwargs)


if __name__ == "__main__":
    main()