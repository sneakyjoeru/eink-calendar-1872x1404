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
_last_tl_minute: str = ""        # wall-clock minute key of the last time-line update (dedup)
_last_event_poll: float = 0.0     # ts of the last event-change poll
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
              full_refresh_repeats: int = 0, hard_clear: bool = False) -> bool:
    """Fetch events + render + display. Thread-safe with timeout.
    force_full: bypass diff, do full screen refresh.
    full_refresh_repeats: number of full-screen GC16 clean refresh passes.
        0 = auto-determine (2 for day change, user setting for interval/dim,
        caller-specified for startup/manual).
    hard_clear: force a GC16 flashing clean refresh even in b/w mode (used for
        explicit Save & Render — automatic b/w full refreshes stay flash-free DU)."""
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
        elif settings["view_mode"] == "5days":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + datetime.timedelta(days=5)
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
            full_refresh_repeats = max(1, int(settings.get("full_refresh_day_change", 2)))
            logger.info("Day changed (%s → %s), forcing full refresh (%dx)",
                        _last_render_date, today_str, full_refresh_repeats)
        _last_render_date = today_str

        if not force and not events_changed and not day_changed:
            logger.debug("No event changes, skipping render")
            return False

        # Optional: when "fullscreen refresh when events are dimmed" is enabled,
        # force a full clean refresh whenever the event set changes (an event
        # ended/started/disappeared). Off by default — regional updates handle
        # the change without a full-screen flash.
        if not force_full and events_changed and settings.get("fullscreen_on_dim", False):
            force_full = True
            full_refresh_repeats = max(1, int(settings.get("full_refresh_event_end", 1)))
            logger.info("Events changed + fullscreen_on_dim enabled, forcing full refresh (%dx)",
                        full_refresh_repeats)

        # Check full refresh interval setting
        if not force_full:
            full_interval = settings.get("full_refresh_interval_hours", 0)
            if full_interval and driver.needs_full_refresh(full_interval):
                force_full = True
                full_refresh_repeats = max(1, int(settings.get("full_refresh_interval", 1)))
                logger.info("Full refresh interval (%dh) elapsed, forcing full refresh (%dx)",
                            full_interval, full_refresh_repeats)

        # If force_full was passed directly (startup/manual) and no trigger set
        # the repeat count, use the deploy setting.
        if force_full and full_refresh_repeats == 0:
            full_refresh_repeats = max(1, int(settings.get("full_refresh_deploy", 3)))

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
            show_time_line=settings.get("show_time_line", True),
            time_line_style=settings.get("time_line_style", "dotted"),
            bw_mode=settings.get("bw_mode", False),
            dim_style=settings.get("dim_style", "normal"),
            show_descriptions=settings.get("show_descriptions", True),
            text_outline_width=int(settings.get("text_outline_width", 5)),
            brightness=settings.get("brightness", 1.4),
            now=now,
        )
        ok = driver.render_to_screen(img, brightness=settings.get("brightness", 1.4),
                                     force_full=force_full,
                                     update_mode=settings.get("update_mode", "soft"),
                                     refresh_border_mm=settings.get("refresh_border_mm", 5),
                                     full_refresh_repeats=full_refresh_repeats,
                                     regional_hard_repeats=max(1, int(settings.get("regional_hard_flashes", 1))),
                                     bw_mode=settings.get("bw_mode", False),
                                     hard_clear=hard_clear)
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


def render_hotspot_screen(ssid: str = "", pw: str = "", ip: str = "") -> bool:
    """Show the WiFi-provisioning QR screen while the Pi hosts its own hotspot:
    a QR to join the hotspot + a QR/URL for the setup page."""
    ssid = ssid or wifi_setup.hotspot_ssid()
    pw = pw or wifi_setup.hotspot_password()
    ip = ip or wifi_setup.hotspot_ip()
    portal_url = f"{_scheme()}://{ip}:{config.APP_PORT}/wifi-setup"
    img = render.render_wifi_hotspot(ssid, pw, portal_url,
                                     wifi_setup.get_hotspot_qr_text())
    return driver.render_to_screen(img, brightness=1.0, force_full=True)


def render_after_restore() -> None:
    """Re-render the appropriate screen once connectivity is restored."""
    logger.info("Connectivity restored — re-rendering")
    if not calendar_client.is_configured():
        img = render.render_setup_required(_get_lan_ip(), config.APP_PORT,
                                           ssl=config.SSL_ENABLED)
        driver.render_to_screen(img, brightness=1.0)
    elif not calendar_client.is_authenticated():
        render_setup_screen()
    else:
        do_render(force=True, force_full=True)


# ---- Background scheduler ----
_scheduler_running = True


def background_loop():
    """Background thread: poll events + update time line."""
    global _scheduler_running, _last_time_line_render, _last_tl_minute, _last_event_poll
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

            # --- Current-time line + event polling ------------------------
            poll_interval = s.get("event_poll_interval_sec", 60)
            view_mode = s.get("view_mode", "week")
            tl_interval_min = max(1, int(s.get("time_line_interval_min", 15)))
            tl_active = view_mode in ("week", "7days", "5days") and s.get("show_time_line", True)

            now_dt = _now()
            now_ts = time.time()

            # The time line updates at wall-clock minutes that are a whole
            # fraction of the hour (minute % interval == 0): e.g. a 10-min
            # interval fires at :00, :10, :20 ... This is anchored to the clock,
            # so it is independent of when the app was (re)started. We pre-render
            # a few seconds early and display exactly on the boundary.
            fired_tl = False
            if tl_active:
                next_dt = (now_dt + datetime.timedelta(minutes=1)).replace(second=0, microsecond=0)
                secs_to_next = 60 - now_dt.second - now_dt.microsecond / 1e6
                next_key = next_dt.strftime("%Y-%m-%d %H:%M")
                is_boundary = (next_dt.minute % tl_interval_min == 0)
                if is_boundary and 0 < secs_to_next <= 12 and next_key != _last_tl_minute:
                    target_now = next_dt
                    _render_start = time.time()
                    if not _render_lock.acquire(timeout=120):
                        pass
                    try:
                        settings = settings_store.load()
                        if settings["view_mode"] in ("7days", "5days"):
                            ev_start = target_now.replace(hour=0, minute=0, second=0, microsecond=0)
                        else:
                            ev_start = (target_now - datetime.timedelta(days=target_now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
                        ev_days = 5 if settings["view_mode"] == "5days" else 7
                        ev_end = ev_start + datetime.timedelta(days=ev_days)
                        events = calendar_client.fetch_events(
                            ev_start, ev_end, settings.get("selected_calendars") or None)
                        img = render.render_calendar(
                            view_mode=settings["view_mode"], events=events,
                            day_start=settings["day_start"], day_end=settings["day_end"],
                            max_full_day=settings["max_full_day_events"],
                            time_format=settings.get("time_format", "24h"),
                            date_format=settings.get("date_format", ""),
                            settings_url=f"{_scheme()}://{_get_lan_ip()}:{config.APP_PORT}",
                            crossed_event_dim=settings.get("crossed_event_dim", False),
                            dim_past_events=settings.get("dim_past_events", False),
                            text_size_modifier=settings.get("text_size_modifier", 0),
                            show_time_line=settings.get("show_time_line", True),
                            time_line_style=settings.get("time_line_style", "dotted"),
                            bw_mode=settings.get("bw_mode", False),
                            dim_style=settings.get("dim_style", "normal"),
                            show_descriptions=settings.get("show_descriptions", True),
                            text_outline_width=int(settings.get("text_outline_width", 5)),
                            brightness=settings.get("brightness", 1.4),
                            now=target_now,
                        )
                    finally:
                        _render_lock.release()
                    prepare_time = time.time() - _render_start
                    # wait for the exact minute boundary, then display
                    ndt = datetime.datetime.now()
                    sleep_sec = 60 - ndt.second - ndt.microsecond / 1e6
                    if 0 < sleep_sec <= 60:
                        time.sleep(sleep_sec)
                    # Check if a periodic full-screen refresh is due — the
                    # time-line path bypasses do_render(), so the interval check
                    # there never runs. Without this, the user's "full-screen
                    # clean refresh every N hours" setting is silently ignored
                    # during time-line updates.
                    full_interval = settings.get("full_refresh_interval_hours", 0)
                    tl_force_full = full_interval and driver.needs_full_refresh(full_interval)
                    tl_full_repeats = 0
                    if tl_force_full:
                        tl_full_repeats = max(1, int(settings.get("full_refresh_interval", 1)))
                        logger.info("Time-line update: full-refresh interval (%dh) elapsed, forcing full (%dx)",
                                    full_interval, tl_full_repeats)
                    driver.render_to_screen(img, brightness=settings.get("brightness", 1.4),
                                            force_full=tl_force_full,
                                            full_refresh_repeats=tl_full_repeats,
                                            update_mode=settings.get("update_mode", "soft"),
                                            refresh_border_mm=settings.get("refresh_border_mm", 5),
                                            bw_mode=settings.get("bw_mode", False))
                    _last_tl_minute = next_key
                    _last_time_line_render = time.time()
                    _last_event_poll = _last_time_line_render   # events were just fetched
                    fired_tl = True
                    logger.info("Time-line %s update -> %s (every %dm): prepared %.1fs, landed :%02d",
                                "full" if tl_force_full else "regional",
                                next_key[-5:], tl_interval_min, prepare_time,
                                int(datetime.datetime.now().second))

            # Event-change poll at the configured interval, independent of the
            # time-line cadence.
            if not fired_tl and (now_ts - _last_event_poll >= poll_interval):
                do_render()
                _last_event_poll = time.time()

            # Tick often enough to catch the pre-render window every minute when
            # the time line is active; otherwise sleep up to the poll interval.
            time.sleep(3 if tl_active else min(poll_interval, 30))
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

    # Initial screen. If we have no network yet, don't show a useless LAN-IP
    # QR — the connectivity monitor will raise the hotspot and render its QR.
    if not wifi_setup.is_online():
        render_status_screen("Connecting to WiFi…", "If it can't connect, a setup hotspot will start")
    elif not calendar_client.is_configured():
        img = render.render_setup_required(lan_ip, config.APP_PORT, ssl=config.SSL_ENABLED)
        driver.render_to_screen(img, brightness=1.0)
    elif not calendar_client.is_authenticated():
        render_setup_screen()
    else:
        # Mandatory full-screen clean refresh on startup/deploy — clears any
        # ghosting residue and ensures the screen matches the current image.
        # Pass count is auto-determined from the full_refresh_deploy setting.
        do_render(force=True, force_full=True)

    # Start scheduler
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()

    # Start WiFi connectivity monitor. On sustained connectivity loss it raises
    # a hotspot and renders the join QR (on_hotspot); on reconnect it re-renders
    # the calendar/settings (on_restore).
    wifi_setup.start_monitor(interval=60,
                             on_hotspot=render_hotspot_screen,
                             on_restore=render_after_restore)


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
          <button type="button" class="btn btn-small" onclick="uploadSecret()" style="white-space:nowrap">Upload</button>
        </div>
        <p id="uploadStatus" style="font-size:0.8em;margin-top:6px"></p>
        '''
    elif not authenticated:
        auth_section = '''
        <p style="margin-bottom:10px">Click below to get the authorization link, then paste the code back.</p>
        <button type="button" class="btn btn-auth" onclick="startGoogleAuth()">🔐 Login with Google</button>
        <div id="authFlow" style="display:none;margin-top:12px">
          <p>1. Open this link in your browser:</p>
          <p><a id="authUrl" href="#" target="_blank" style="word-break:break-all;color:#4285F4"></a></p>
          <p style="margin-top:10px">2. Authorize. When the redirect fails, <b>copy the full URL</b> from the address bar and paste it below.</p>
          <p>3. Paste the redirect URL or just the code:</p>
          <input id="authCode" type="text" style="width:100%;padding:8px;margin-bottom:8px" placeholder="Paste the full redirect URL or just the code">
          <button type="button" class="btn btn-primary" onclick="exchangeCode()">✓ Exchange Code</button>
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
    sel_5days = "selected" if s["view_mode"] == "5days" else ""
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
    outline_w = s.get("text_outline_width", 5)
    fdow = int(s.get("first_day_of_week", 0))
    _DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    sel_fdow = [''] * 7
    if 0 <= fdow <= 6:
        sel_fdow[fdow] = 'selected'

    return _SETTINGS_HTML.format(
        saved_html='<div class="badge badge-ok" style="display:block;text-align:center;margin-bottom:12px;padding:8px">✓ Settings saved</div>' if saved else "",
        auth_section=auth_section,
        sel_month=sel_month,
        sel_35days=sel_35days,
        sel_week=sel_week,
        sel_7days=sel_7days,
        sel_5days=sel_5days,
        sel_24h=sel_24h,
        sel_12h=sel_12h,
        day_start=s["day_start"],
        day_end=s["day_end"],
        max_fd=s["max_full_day_events"],
        tl_interval=s["time_line_interval_min"],
        sel_tl_1='selected' if s['time_line_interval_min']==1 else '',
        sel_tl_2='selected' if s['time_line_interval_min']==2 else '',
        sel_tl_5='selected' if s['time_line_interval_min']==5 else '',
        sel_tl_10='selected' if s['time_line_interval_min']==10 else '',
        sel_tl_15='selected' if s['time_line_interval_min']==15 else '',
        sel_tl_20='selected' if s['time_line_interval_min']==20 else '',
        sel_tl_30='selected' if s['time_line_interval_min']==30 else '',
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
        sel_um_du='selected' if s.get('update_mode', 'soft')=='du' else '',
        bw_mode='checked' if s.get('bw_mode', False) else '',
        sel_ds_normal='selected' if s.get('dim_style', 'normal')=='normal' else '',
        sel_ds_checker='selected' if s.get('dim_style', 'normal')=='checkerboard' else '',
        db_mm=s.get('refresh_border_mm', 5),
        sel_db_0='selected' if s.get('refresh_border_mm', 5)==0 else '',
        sel_db_2='selected' if s.get('refresh_border_mm', 5)==2 else '',
        sel_db_5='selected' if s.get('refresh_border_mm', 5)==5 else '',
        sel_db_10='selected' if s.get('refresh_border_mm', 5)==10 else '',
        sel_db_15='selected' if s.get('refresh_border_mm', 5)==15 else '',
        sel_db_20='selected' if s.get('refresh_border_mm', 5)==20 else '',
        fullscreen_on_dim='checked' if s.get('fullscreen_on_dim', False) else '',
        show_time_line='checked' if s.get('show_time_line', True) else '',
        sel_tl_style_solid='selected' if s.get('time_line_style', 'dotted')=='solid' else '',
        sel_tl_style_dotted='selected' if s.get('time_line_style', 'dotted')=='dotted' else '',
        sel_tl_style_wavy='selected' if s.get('time_line_style', 'dotted')=='wavy' else '',
        sel_frd_1='selected' if s.get('full_refresh_deploy', 3)==1 else '',
        sel_frd_2='selected' if s.get('full_refresh_deploy', 3)==2 else '',
        sel_frd_3='selected' if s.get('full_refresh_deploy', 3)==3 else '',
        sel_frd_4='selected' if s.get('full_refresh_deploy', 3)==4 else '',
        sel_frd_5='selected' if s.get('full_refresh_deploy', 3)==5 else '',
        sel_frdc_1='selected' if s.get('full_refresh_day_change', 2)==1 else '',
        sel_frdc_2='selected' if s.get('full_refresh_day_change', 2)==2 else '',
        sel_frdc_3='selected' if s.get('full_refresh_day_change', 2)==3 else '',
        sel_frdc_4='selected' if s.get('full_refresh_day_change', 2)==4 else '',
        sel_frdc_5='selected' if s.get('full_refresh_day_change', 2)==5 else '',
        sel_fri_1='selected' if s.get('full_refresh_interval', 1)==1 else '',
        sel_fri_2='selected' if s.get('full_refresh_interval', 1)==2 else '',
        sel_fri_3='selected' if s.get('full_refresh_interval', 1)==3 else '',
        sel_fri_4='selected' if s.get('full_refresh_interval', 1)==4 else '',
        sel_fri_5='selected' if s.get('full_refresh_interval', 1)==5 else '',
        sel_free_1='selected' if s.get('full_refresh_event_end', 1)==1 else '',
        sel_free_2='selected' if s.get('full_refresh_event_end', 1)==2 else '',
        sel_free_3='selected' if s.get('full_refresh_event_end', 1)==3 else '',
        sel_free_4='selected' if s.get('full_refresh_event_end', 1)==4 else '',
        sel_free_5='selected' if s.get('full_refresh_event_end', 1)==5 else '',
        sel_frm_1='selected' if s.get('full_refresh_manual', 1)==1 else '',
        sel_frm_2='selected' if s.get('full_refresh_manual', 1)==2 else '',
        sel_frm_3='selected' if s.get('full_refresh_manual', 1)==3 else '',
        sel_frm_4='selected' if s.get('full_refresh_manual', 1)==4 else '',
        sel_frm_5='selected' if s.get('full_refresh_manual', 1)==5 else '',
        sel_rhf_1='selected' if s.get('regional_hard_flashes', 1)==1 else '',
        sel_rhf_2='selected' if s.get('regional_hard_flashes', 1)==2 else '',
        sel_rhf_3='selected' if s.get('regional_hard_flashes', 1)==3 else '',
        sel_rhf_4='selected' if s.get('regional_hard_flashes', 1)==4 else '',
        sel_rhf_5='selected' if s.get('regional_hard_flashes', 1)==5 else '',
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
        show_desc='checked' if s.get('show_descriptions', True) else '',
        ts_mod=ts_mod,
        outline_w=outline_w,
        sel_fdow_0=sel_fdow[0], sel_fdow_1=sel_fdow[1], sel_fdow_2=sel_fdow[2],
        sel_fdow_3=sel_fdow[3], sel_fdow_4=sel_fdow[4], sel_fdow_5=sel_fdow[5],
        sel_fdow_6=sel_fdow[6],
        cal_checkboxes=cal_checkboxes,
        cal_error=cal_error,
        lan_ip=lan_ip,
        port=config.APP_PORT,
        preset_options=_PRESET_OPTIONS_HTML,
        preset_descs=_PRESET_DESCS_JSON,
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
        "full_refresh_deploy": int(fd.get("full_refresh_deploy", 3)),
        "full_refresh_day_change": int(fd.get("full_refresh_day_change", 2)),
        "full_refresh_interval": int(fd.get("full_refresh_interval", 1)),
        "full_refresh_event_end": int(fd.get("full_refresh_event_end", 1)),
        "full_refresh_manual": int(fd.get("full_refresh_manual", 1)),
        "regional_hard_flashes": int(fd.get("regional_hard_flashes", 1)),
        "show_time_line": fd.get("show_time_line") == "1",
        "time_line_style": fd.get("time_line_style", "dotted"),
        "bw_mode": fd.get("bw_mode") == "1",
        "dim_style": fd.get("dim_style", "normal"),
        "brightness": float(fd.get("brightness", 1.4)),
        "timezone": fd.get("timezone", ""),
        "time_format": fd.get("time_format", "24h"),
        "date_format": fd.get("date_format", ""),
        "crossed_event_dim": fd.get("crossed_event_dim") == "1",
        "dim_past_events": fd.get("dim_past_events") == "1",
        "show_descriptions": fd.get("show_descriptions") == "1",
        "text_size_modifier": int(fd.get("text_size_modifier", 0)),
        "text_outline_width": max(0, min(10, int(fd.get("text_outline_width", 5)))),
        "selected_calendars": fd.getlist("selected_calendars"),
        "first_day_of_week": int(fd.get("first_day_of_week", 0)),
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


# ---- Presets: curated setting combinations for common use cases ----
_PRESETS = {
    # ---- 7 days ----
    "gray_soft_7d_10m": {
        "group": "7 days", "default": True,
        "label": "7 days \u00b7 10m soft \u00b7 1h full-clear",
        "desc": "Grayscale with soft (no-flash) regional updates. 7-day view, time line every 10 min, full clean refresh every hour. Balanced default \u2014 your current setup.",
        "view_mode": "7days", "bw_mode": False, "update_mode": "soft", "dim_style": "normal",
        "time_line_interval_min": 10, "full_refresh_interval_hours": 1,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    "gray_soft_7d_live": {
        "group": "7 days",
        "label": "7 days \u00b7 1m soft live \u00b7 30m full-clear",
        "desc": "Grayscale soft with a 1-minute live time line. Frequent full clears (every 30 min) fight the slow darkening from live updates.",
        "view_mode": "7days", "bw_mode": False, "update_mode": "soft", "dim_style": "normal",
        "time_line_interval_min": 1, "full_refresh_interval_hours": 0.5,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    "bw_check_7d_live": {
        "group": "7 days",
        "label": "7 days \u00b7 b/w DU \u00b7 1m live (checkerboard)",
        "desc": "1-bit black/white with DU updates \u2014 zero ghosting, never darkens. 1-minute live time line; dimmed events use a checkerboard fill.",
        "view_mode": "7days", "bw_mode": True, "update_mode": "du", "dim_style": "checkerboard",
        "time_line_interval_min": 1, "full_refresh_interval_hours": 6,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    "bw_check_7d_5m": {
        "group": "7 days",
        "label": "7 days \u00b7 b/w DU \u00b7 5m (checkerboard)",
        "desc": "1-bit B/W DU, 7-day view, time line every 5 min, checkerboard dim. Crisp with almost no full clears (every 12 h).",
        "view_mode": "7days", "bw_mode": True, "update_mode": "du", "dim_style": "checkerboard",
        "time_line_interval_min": 5, "full_refresh_interval_hours": 12,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    # ---- 5 days ----
    "gray_soft_5d_10m": {
        "group": "5 days",
        "label": "5 days \u00b7 10m soft \u00b7 1h full-clear",
        "desc": "Grayscale with soft (no-flash) regional updates. 5-day view with larger fonts, time line every 10 min, full clean refresh every hour.",
        "view_mode": "5days", "bw_mode": False, "update_mode": "soft", "dim_style": "normal",
        "time_line_interval_min": 10, "full_refresh_interval_hours": 1,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    "bw_check_5d_live": {
        "group": "5 days",
        "label": "5 days \u00b7 b/w DU \u00b7 1m live (checkerboard)",
        "desc": "1-bit black/white with DU updates \u2014 zero ghosting, never darkens. 5-day view with larger fonts, 1-minute live time line, checkerboard dim.",
        "view_mode": "5days", "bw_mode": True, "update_mode": "du", "dim_style": "checkerboard",
        "time_line_interval_min": 1, "full_refresh_interval_hours": 6,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    # ---- Week ----
    "gray_hard_week_15m": {
        "group": "Week",
        "label": "Week \u00b7 15m hard flash \u00b7 2h full-clear",
        "desc": "Grayscale with a brief flash on each change (less residual darkening). Week view, time line every 15 min, full clear every 2 h.",
        "view_mode": "week", "bw_mode": False, "update_mode": "hard", "dim_style": "normal",
        "time_line_interval_min": 15, "full_refresh_interval_hours": 2,
        "refresh_border_mm": 2, "fullscreen_on_dim": True,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 2, "full_refresh_event_end": 2,
        "full_refresh_manual": 2, "regional_hard_flashes": 2,
    },
    "gray_soft_week_slow": {
        "group": "Week",
        "label": "Week \u00b7 30m soft \u00b7 6h full-clear (low wear)",
        "desc": "Grayscale soft, gentle on the panel: time line every 30 min, full clear only every 6 h. Fewest refreshes.",
        "view_mode": "week", "bw_mode": False, "update_mode": "soft", "dim_style": "normal",
        "time_line_interval_min": 30, "full_refresh_interval_hours": 6,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 2, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    "bw_solid_week_10m": {
        "group": "Week",
        "label": "Week \u00b7 b/w DU \u00b7 10m (solid dim)",
        "desc": "1-bit B/W DU, week view, time line every 10 min. Dimmed events use a solid white fill \u2014 the cleanest b/w look.",
        "view_mode": "week", "bw_mode": True, "update_mode": "du", "dim_style": "normal",
        "time_line_interval_min": 10, "full_refresh_interval_hours": 6,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    # ---- Month & 35 days ----
    "gray_35d_daily": {
        "group": "Month & 35 days",
        "label": "35 days \u00b7 daily full-clear",
        "desc": "5-week grid (35 days, Monday-start) \u2014 the closest to a 30-day overview. Grayscale soft; a full clean refresh once a day keeps it pristine.",
        "view_mode": "35days", "bw_mode": False, "update_mode": "soft", "dim_style": "normal",
        "time_line_interval_min": 30, "full_refresh_interval_hours": 24,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
    "gray_month_daily": {
        "group": "Month & 35 days",
        "label": "Month \u00b7 daily full-clear",
        "desc": "Month grid (no time line). Grayscale soft; a full clean refresh once a day keeps it pristine.",
        "view_mode": "month", "bw_mode": False, "update_mode": "soft", "dim_style": "normal",
        "time_line_interval_min": 30, "full_refresh_interval_hours": 24,
        "refresh_border_mm": 5, "fullscreen_on_dim": False,
        "full_refresh_deploy": 3, "full_refresh_day_change": 2,
        "full_refresh_interval": 1, "full_refresh_event_end": 1,
        "full_refresh_manual": 1, "regional_hard_flashes": 1,
    },
}

# Preset <optgroup>/<option> list and description map, generated from _PRESETS.
import json as _json
def _build_preset_options():
    groups, order = {}, []
    for k, p in _PRESETS.items():
        g = p.get("group", "Presets")
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append((k, p))
    out = []
    for g in order:
        out.append('<optgroup label="%s">' % g)
        for k, p in groups[g]:
            sel = " selected" if p.get("default") else ""
            out.append('<option value="%s"%s>%s</option>' % (k, sel, p["label"]))
        out.append("</optgroup>")
    return "".join(out)
_PRESET_OPTIONS_HTML = _build_preset_options()
_PRESET_DESCS_JSON = _json.dumps({k: p["desc"] for k, p in _PRESETS.items()}, ensure_ascii=False)


@app.get("/api/preset/{name}")
async def apply_preset(name: str):
    """Apply a preset configuration and trigger a render."""
    preset = _PRESETS.get(name)
    if not preset:
        return JSONResponse({"error": f"Unknown preset: {name}"}, status_code=404)
    settings_store.update({k: v for k, v in preset.items() if k not in ("label", "desc")})
    logger.info("Applied preset: %s (%s)", name, preset["label"])
    threading.Thread(target=_safe_render, daemon=True).start()
    return RedirectResponse(url="/settings?saved=1", status_code=303)


def _safe_render():
    """Call do_render with full exception logging. Save & Render always does full hard refresh.
    The number of full refresh passes is the user-configurable full_refresh_manual setting."""
    settings = settings_store.load()
    repeats = max(1, int(settings.get("full_refresh_manual", 1)))
    logger.info("Manual render triggered (full hard refresh, %dx)", repeats)
    try:
        ok = do_render(force=True, force_full=True, full_refresh_repeats=repeats,
                       hard_clear=True)
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
      status.innerHTML = '✓ Connecting to <b>' + ssid + '</b>…<br>' +
        'The setup hotspot will now turn off. Reconnect your phone to your ' +
        'home WiFi, then look at the display for the new address to open.';
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
    """Connect to a WiFi network via NetworkManager.

    The switchover is done in a background thread so this HTTP response can
    flush BEFORE the hotspot is torn down (tearing it down drops the client's
    connection). The client is told to reconnect to their home WiFi and read
    the new address off the display, which re-renders once connectivity is
    restored.
    """
    data = await request.json()
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    if not ssid:
        return JSONResponse({"ok": False, "error": "SSID is required"}, status_code=400)

    import time

    def _switch():
        time.sleep(1)  # let the JSON response flush to the client first
        ok, err = wifi_setup.connect_wifi(ssid, password)
        if ok and wifi_setup.is_online():
            wifi_setup.notify_restored()
        elif not ok:
            logger.error("WiFi connect failed: %s", err)

    threading.Thread(target=_switch, daemon=True).start()
    return {"ok": True, "ssid": ssid}


@app.get("/api/wifi-status")
async def wifi_status():
    """Return current WiFi connection status and list of saved networks."""
    import subprocess
    try:
        r = subprocess.run(["nmcli", "-t", "-f", "ACTIVE,SSID", "connection", "show", "--active"],
                           capture_output=True, text=True, timeout=10)
        ssid = ""
        for line in r.stdout.strip().splitlines():
            if line.startswith("yes:"):
                ssid = line.split(":", 1)[1]
                break
        if not ssid:
            r2 = subprocess.run(["nmcli", "-t", "-f", "CONNECTION,DEVICE", "device", "status"],
                                capture_output=True, text=True, timeout=10)
            for line in r2.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and parts[1] == "wlan0" and parts[0] != "--":
                    ssid = parts[0]
                    break
    except Exception:
        ssid = ""

    ip = ""
    try:
        r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        ips = r.stdout.strip().split()
        if ips:
            ip = ips[0]
    except Exception:
        pass

    saved = []
    try:
        r = subprocess.run(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.strip().splitlines():
            parts = line.split(":")
            if len(parts) >= 2 and "wifi" in parts[1]:
                saved.append(parts[0])
    except Exception:
        pass

    return {"connected": bool(ssid), "ssid": ssid, "ip": ip, "saved_networks": saved}


@app.post("/api/wifi-add")
async def wifi_add(request: Request):
    """Add a new WiFi network to NetworkManager (does not switch immediately)."""
    data = await request.json()
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "")
    if not ssid:
        return JSONResponse({"ok": False, "error": "SSID is required"}, status_code=400)

    import subprocess
    try:
        cmd = ["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0",
               "con-name", ssid, "ssid", ssid,
               "wifi-sec.key-mgmt", "wpa-psk",
               "wifi-sec.psk", password] if password else \
              ["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0",
               "con-name", ssid, "ssid", ssid]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return JSONResponse({"ok": False, "error": r.stderr.strip() or "Failed to add"}, status_code=500)
        logger.info("WiFi network saved via settings: %s", ssid)
        return {"ok": True, "ssid": ssid}
    except Exception as e:
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
        s = settings_store.load()
        do_render(force=True, force_full=True,
                 full_refresh_repeats=max(1, int(s.get("full_refresh_deploy", 3))))
        return {"ok": True}
    return JSONResponse({"error": err or "Code exchange failed"}, status_code=400)


@app.get("/auth/callback")
async def auth_callback(code: str = ""):
    """Handle OAuth callback (direct redirect — may not work for LAN IPs).
    Also renders a page the user can copy the code from."""
    if code:
        ok, _ = calendar_client.complete_auth(code)
        if ok:
            s = settings_store.load()
            do_render(force=True, force_full=True,
                     full_refresh_repeats=max(1, int(s.get("full_refresh_deploy", 3))))
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
/* Masonry-style flow: as many ~300px columns as fit — 1 on phones, 2–3 on
   wider screens. Cards of different heights pack without leaving grid gaps. */
.settings-grid {{ columns: 300px; column-gap: 16px; }}
.settings-grid > .card {{ break-inside: avoid; -webkit-column-break-inside: avoid;
  margin: 0 0 16px; }}
/* Google Account pinned at top, full width */
.top-row {{ margin-bottom: 16px; }}
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
/* Tabs */
.tabs {{ display: flex; gap: 4px; margin-bottom: 16px; border-bottom: 2px solid var(--border); }}
.tab-btn {{ padding: 12px 20px; border: none; background: none; color: var(--muted); cursor: pointer;
  font-size: 0.95em; font-weight: 600; border-bottom: 3px solid transparent; margin-bottom: -2px; transition: all .15s; }}
.tab-btn:hover {{ color: var(--text); }}
.tab-btn.active {{ color: var(--accent); border-bottom-color: var(--accent); }}
.tab-panel {{ display: none; }}
.tab-panel.active {{ display: block; }}
</style>
</head>
<body>
<header>
  <h1>📅 E-Ink Calendar</h1>
  <p>Configure your calendar display — changes apply instantly to the screen.</p>
</header>
{saved_html}

<form id="settingsForm" action="/api/settings" method="POST">
<div class="top-row">
  <div class="card" style="display:flex;gap:16px;align-items:stretch">
    <div style="flex:1;min-width:0">
      <h2>🔐 Google Account</h2>
      {auth_section}
      <h3 style="margin-top:16px">Syncing</h3>
      <div class="field">
        <label>Check Google for new events every (seconds)</label>
        <input type="number" name="event_poll_interval_sec" value="{poll_interval}" min="10" max="600">
        <div class="note">Lower = catches new/ended events sooner, more battery/CPU use.</div>
      </div>
    </div>
    <div style="flex-shrink:0;display:flex;align-items:center">
      <img id="inlinePreview" src="/image" alt="E-Ink Display"
           style="max-height:100%;max-width:200px;border-radius:8px;border:1px solid var(--border);cursor:pointer;image-rendering:auto"
           onclick="openPreviewPopup()"
           onerror="this.style.display='none'">
    </div>
  </div>
</div>

<div class="tabs">
  <button type="button" class="tab-btn active" onclick="switchTab(0)">📅 Calendar</button>
  <button type="button" class="tab-btn" onclick="switchTab(1)">🎨 Appearance</button>
  <button type="button" class="tab-btn" onclick="switchTab(2)">🔧 Advanced</button>
</div>

<!-- Tab 0: Calendar -->
<div class="tab-panel active" id="tab0">
<div class="settings-grid">

  <div class="card">
    <h2>🗓 Calendars</h2>
    <div class="cal-grid">
    {cal_checkboxes}
    </div>
    {cal_error}
    <p class="note">Leave all unchecked to show every calendar.</p>
  </div>

  <div class="card">
    <h2>⚡ Presets</h2>
    <div class="field">
      <label>Preset (defaults to your current setup)</label>
      <select id="presetSelect2" onchange="applyPreset2()">
        {preset_options}
      </select>
      <div class="note" id="presetDesc2" style="margin-top:6px"></div>
    </div>
  </div>

  <div class="card">
    <h2>📅 Events</h2>
    <label class="check-row">
      <input type="checkbox" name="show_descriptions" value="1" {show_desc}>
      <span>Show event location &amp; description on cards</span>
    </label>
    <p class="note">Location (prefixed “@”) and description under the title/time, when the event has them and there's room.</p>
  </div>

  <div class="card">
    <h2>🕐 Time &amp; Date</h2>
    <h3>Day range</h3>
    <div class="row">
      <div class="field"><label>Day starts at</label><input type="time" name="day_start" value="{day_start}"></div>
      <div class="field"><label>Day ends at</label><input type="time" name="day_end" value="{day_end}"></div>
    </div>
    <h3>Week</h3>
    <div class="field">
      <label>First day of week</label>
      <select name="first_day_of_week">
        <option value="0" {sel_fdow_0}>Monday</option>
        <option value="1" {sel_fdow_1}>Tuesday</option>
        <option value="2" {sel_fdow_2}>Wednesday</option>
        <option value="3" {sel_fdow_3}>Thursday</option>
        <option value="4" {sel_fdow_4}>Friday</option>
        <option value="5" {sel_fdow_5}>Saturday</option>
        <option value="6" {sel_fdow_6}>Sunday</option>
      </select>
      <div class="note">Controls which day starts the week in Week, Month and 35-day views.</div>
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
  </div>

</div>
</div>

<!-- Tab 1: Appearance -->
<div class="tab-panel" id="tab1">
<div class="settings-grid">

  <div class="card">
    <h2>📅 View Mode</h2>
    <div class="field">
      <label>View Mode</label>
      <select name="view_mode">
        <option value="month" {sel_month}>Month (current month)</option>
        <option value="35days" {sel_35days}>Month (5 weeks, Mon-start)</option>
        <option value="week" {sel_week}>Week (Mon–Sun)</option>
        <option value="7days" {sel_7days}>7 Days (from today)</option>
        <option value="5days" {sel_5days}>5 Days (from today)</option>
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
  </div>

  <div class="card">
    <h2>🎨 Appearance</h2>
    <div class="row">
      <div class="field"><label>Brightness</label>
        <input type="range" name="brightness" min="0.1" max="2.0" step="0.1" value="{brightness}"
               oninput="document.getElementById('brightVal').textContent=this.value">
        <div class="note" style="text-align:center"><span class="range-val" id="brightVal">{brightness}</span></div>
      </div>
      <div class="field"><label>Text size</label>
        <input type="number" name="text_size_modifier" value="{ts_mod}" step="1" min="-8" max="8">
        <div class="note">+ bigger, − smaller (px)</div>
      </div>
      <div class="field"><label>Text outline width</label>
        <input type="range" name="text_outline_width" min="0" max="10" step="1" value="{outline_w}"
               oninput="document.getElementById('outlineVal').textContent=this.value">
        <div class="note" style="text-align:center">White outline around text in colored event cards (<span class="range-val" id="outlineVal">{outline_w}</span> px, 0 = off)</div>
      </div>
    </div>
    <label class="check-row">
      <input type="checkbox" name="bw_mode" value="1" {bw_mode}>
      <span>B/W mode (1-bit black/white — crisp, never darkens; pairs with DU updates)</span>
    </label>
    <h3>Dimming</h3>
    <label class="check-row">
      <input type="checkbox" name="dim_past_events" value="1" {dim_past}>
      <span>Dim past days &amp; ended events</span>
    </label>
    <label class="check-row">
      <input type="checkbox" name="crossed_event_dim" value="1" {crossed_dim}>
      <span>Dim an event once the time line passes it</span>
    </label>
    <div class="field">
      <label>Dimmed event style (b/w mode)</label>
      <select name="dim_style">
        <option value="normal" {sel_ds_normal}>White fill + black border</option>
        <option value="checkerboard" {sel_ds_checker}>Checkerboard (1px B/W pattern, black text with white outline)</option>
      </select>
    </div>
  </div>

  <div class="card">
    <h2>⏱ Current-time line</h2>
    <label class="check-row">
      <input type="checkbox" name="show_time_line" value="1" {show_time_line}>
      <span>Show current-time line</span>
    </label>
    <div class="field">
      <label>Move the time line every</label>
      <select name="time_line_interval_min" id="tlInterval" onchange="updateTlWarning()">
        <option value="1" {sel_tl_1}>1 minute · every 1/60 h</option>
        <option value="2" {sel_tl_2}>2 minutes · 1/30 h</option>
        <option value="5" {sel_tl_5}>5 minutes · 1/12 h</option>
        <option value="10" {sel_tl_10}>10 minutes · 1/6 h</option>
        <option value="15" {sel_tl_15}>15 minutes · 1/4 h</option>
        <option value="20" {sel_tl_20}>20 minutes · 1/3 h</option>
        <option value="30" {sel_tl_30}>30 minutes · 1/2 h</option>
      </select>
      <div class="note">Only used in Week, 7-day &amp; 5-day views. Each tick is a small regional refresh.</div>
      <div class="note" id="tlWarning" style="display:none;color:#f59e0b;margin-top:4px">⚠️ Intervals under 30 minutes cause frequent regional updates that can slowly darken the screen over time. Use a full-screen clean refresh periodically to clear this.</div>
    </div>
    <div class="field">
      <label>Time-line style</label>
      <select name="time_line_style">
        <option value="solid" {sel_tl_style_solid}>Solid thick line</option>
        <option value="dotted" {sel_tl_style_dotted}>Dotted (default)</option>
        <option value="wavy" {sel_tl_style_wavy}>Wavy</option>
      </select>
    </div>
    <h3>Regional refresh</h3>
    <div class="field">
      <label>Update style for small changes (time line, etc.)</label>
      <select name="update_mode">
        <option value="soft" {sel_um_smooth}>Soft · no flash, GL16 (may darken over time)</option>
        <option value="hard" {sel_um_hard}>Hard · brief flash of the changed area</option>
        <option value="du" {sel_um_du}>DU · 1-bit, no flash, zero darkening (requires b/w mode)</option>
      </select>
      <div class="note">Soft uses GL16 which accumulates ghosting — use periodic full refreshes to clear. DU fully drives e-ink particles with no ghosting — enable b/w mode for best results.</div>
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
      <div class="note">Expands the refreshed region by this much on each side. Only the inner changed area visibly updates.</div>
    </div>
    <div class="field">
      <label>Regional hard flashes</label>
      <select name="regional_hard_flashes">
        <option value="1" {sel_rhf_1}>1</option><option value="2" {sel_rhf_2}>2</option>
        <option value="3" {sel_rhf_3}>3</option><option value="4" {sel_rhf_4}>4</option>
        <option value="5" {sel_rhf_5}>5</option>
      </select>
      <div class="note">Flash+draw cycles when regional hard mode is active.</div>
    </div>
  </div>

</div>
</div>

<!-- Tab 2: Advanced -->
<div class="tab-panel" id="tab2">
<div class="settings-grid">

  <div class="card">
    <h2>♻️ Full-screen refresh</h2>
    <p class="note" style="margin-top:-8px;margin-bottom:12px">Advanced — clears ghosting by wiping the whole screen.</p>
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
    <h3>Clean-refresh passes</h3>
    <div class="row">
      <div class="field"><label>Startup / deploy</label>
        <select name="full_refresh_deploy">
          <option value="1" {sel_frd_1}>1</option><option value="2" {sel_frd_2}>2</option>
          <option value="3" {sel_frd_3}>3</option><option value="4" {sel_frd_4}>4</option>
          <option value="5" {sel_frd_5}>5</option>
        </select>
      </div>
      <div class="field"><label>Day change</label>
        <select name="full_refresh_day_change">
          <option value="1" {sel_frdc_1}>1</option><option value="2" {sel_frdc_2}>2</option>
          <option value="3" {sel_frdc_3}>3</option><option value="4" {sel_frdc_4}>4</option>
          <option value="5" {sel_frdc_5}>5</option>
        </select>
      </div>
    </div>
    <div class="row">
      <div class="field"><label>Interval</label>
        <select name="full_refresh_interval">
          <option value="1" {sel_fri_1}>1</option><option value="2" {sel_fri_2}>2</option>
          <option value="3" {sel_fri_3}>3</option><option value="4" {sel_fri_4}>4</option>
          <option value="5" {sel_fri_5}>5</option>
        </select>
      </div>
      <div class="field"><label>Event end / dimming</label>
        <select name="full_refresh_event_end">
          <option value="1" {sel_free_1}>1</option><option value="2" {sel_free_2}>2</option>
          <option value="3" {sel_free_3}>3</option><option value="4" {sel_free_4}>4</option>
          <option value="5" {sel_free_5}>5</option>
        </select>
      </div>
    </div>
    <div class="field">
      <label>Save &amp; Render (manual)</label>
      <select name="full_refresh_manual">
        <option value="1" {sel_frm_1}>1</option><option value="2" {sel_frm_2}>2</option>
        <option value="3" {sel_frm_3}>3</option><option value="4" {sel_frm_4}>4</option>
        <option value="5" {sel_frm_5}>5</option>
      </select>
    </div>
  </div>

  <div class="card">
    <h2>📶 WiFi Configuration</h2>
    <p class="note" style="margin-top:-8px;margin-bottom:12px">Manage WiFi connections. If the current connection breaks, the Pi will start a setup hotspot automatically.</p>
    <div id="wifiCurrentStatus" style="margin-bottom:12px"></div>
    <h3>Available Networks</h3>
    <div id="wifiScanList" style="margin-bottom:8px"><span style="color:var(--muted);font-size:0.85em">Click scan to search…</span></div>
    <p style="margin-bottom:8px"><a href="#" onclick="event.preventDefault();scanWifiNetworks()" style="color:var(--accent);font-size:0.82em;text-decoration:none">↻ Scan networks</a></p>
    <div class="field">
      <label>Add a new WiFi network (in case the current one breaks)</label>
      <input type="text" id="wifiSsid" placeholder="Network name (SSID) or pick from scan above" style="margin-bottom:8px">
      <input type="password" id="wifiPassword" placeholder="Password (leave empty for open)">
    </div>
    <button type="button" class="btn btn-small" style="width:100%;margin-top:4px" onclick="addWifiNetwork()">💾 Save WiFi Network</button>
    <div class="field" style="margin-top:12px">
      <label>Saved networks</label>
      <div id="wifiSavedList" style="font-size:0.85em"></div>
    </div>
  </div>

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
var _presetDescs = {preset_descs};
function switchTab(n) {{
  document.querySelectorAll('.tab-btn').forEach(function(b, i) {{
    b.classList.toggle('active', i === n);
  }});
  document.querySelectorAll('.tab-panel').forEach(function(p, i) {{
    p.classList.toggle('active', i === n);
  }});
}}
function openPreviewPopup() {{
  var w = window.open('/preview', 'einkPreview', 'width=1000,height=800');
  if (w) w.focus();
}}
function refreshInlinePreview() {{
  var img = document.getElementById('inlinePreview');
  if (img) img.src = '/image?t=' + Date.now();
}}
setInterval(refreshInlinePreview, 15000);
async function loadWifiStatus() {{
  try {{
    const r = await fetch('/api/wifi-status');
    const d = await r.json();
    const el = document.getElementById('wifiCurrentStatus');
    if (el) {{
      let html = '<div style="padding:8px 12px;background:var(--input);border-radius:8px;font-size:0.85em">';
      if (d.connected) {{
        html += '<span class="badge badge-ok">✓ Connected</span> <b>' + d.ssid + '</b>';
        if (d.ip) html += ' · ' + d.ip;
      }} else {{
        html += '<span style="color:var(--warn)">⚠ Not connected</span>';
      }}
      html += '</div>';
      el.innerHTML = html;
    }}
    const list = document.getElementById('wifiSavedList');
    if (list && d.saved_networks) {{
      if (d.saved_networks.length) {{
        list.innerHTML = d.saved_networks.map(n => {{
          let s = '· ' + n;
          if (n === d.ssid) s += ' <span class="badge badge-ok" style="font-size:0.7em">active</span>';
          return s;
        }}).join('<br>');
      }} else {{
        list.innerHTML = '<span style="color:var(--muted)">No saved networks</span>';
      }}
    }}
  }} catch(e) {{}}
}}
async function addWifiNetwork() {{
  const ssid = document.getElementById('wifiSsid').value.trim();
  const password = document.getElementById('wifiPassword').value;
  if (!ssid) {{ alert('Enter a network name'); return; }}
  try {{
    const r = await fetch('/api/wifi-add', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ssid, password}}),
    }});
    const d = await r.json();
    if (d.ok) {{
      document.getElementById('wifiSsid').value = '';
      document.getElementById('wifiPassword').value = '';
      loadWifiStatus();
      alert('WiFi network saved: ' + ssid);
    }} else {{
      alert('Error: ' + (d.error || 'Failed to save'));
    }}
  }} catch(e) {{
    alert('Error: ' + e.message);
  }}
}}
async function scanWifiNetworks() {{
  const list = document.getElementById('wifiScanList');
  if (!list) return;
  list.innerHTML = '<span style="color:var(--muted);font-size:0.85em">Scanning…</span>';
  try {{
    const r = await fetch('/api/wifi-scan');
    const data = await r.json();
    if (data.networks && data.networks.length) {{
      let html = '<div style="display:flex;flex-direction:column;gap:4px">';
      data.networks.forEach(function(n) {{
        const lock = n.encrypted ? '🔒' : '🔓';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:var(--input);border-radius:6px;cursor:pointer;font-size:0.85em" '
          + 'onclick="document.getElementById(\'wifiSsid\').value=\'' + n.ssid.replace(/'/g, "\\'") + '\'">'
          + '<span>' + lock + ' ' + n.ssid + '</span>'
          + '<span style="color:var(--muted);font-size:0.85em">' + n.signal + ' dBm</span>'
          + '</div>';
      }});
      html += '</div>';
      list.innerHTML = html;
    }} else {{
      list.innerHTML = '<span style="color:var(--muted);font-size:0.85em">No networks found</span>';
    }}
  }} catch(e) {{
    list.innerHTML = '<span style="color:var(--warn);font-size:0.85em">Scan failed: ' + e.message + '</span>';
  }}
}}
loadWifiStatus();
function applyPreset() {{
  var sel = document.getElementById('presetSelect');
  var desc = document.getElementById('presetDesc');
  var val = sel.value;
  if (val && _presetDescs[val]) {{
    desc.textContent = _presetDescs[val];
    if (confirm('Apply this preset? This overwrites your refresh settings.')) {{
      location.href = '/api/preset/' + val;
    }} else {{
      sel.value = '';
      desc.textContent = '';
    }}
  }} else {{
    desc.textContent = '';
  }}
}}
function applyPreset2() {{
  var sel = document.getElementById('presetSelect2');
  var desc = document.getElementById('presetDesc2');
  var val = sel.value;
  if (val && _presetDescs[val]) {{
    desc.textContent = _presetDescs[val];
    if (confirm('Apply this preset? This overwrites your refresh settings.')) {{
      location.href = '/api/preset/' + val;
    }} else {{
      sel.value = '';
      desc.textContent = '';
    }}
  }} else {{
    desc.textContent = '';
  }}
}}
function updateTlWarning() {{
  var sel = document.getElementById('tlInterval');
  var warn = document.getElementById('tlWarning');
  if (sel && warn) {{
    warn.style.display = (parseInt(sel.value) < 30) ? 'block' : 'none';
  }}
}}
updateTlWarning();
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