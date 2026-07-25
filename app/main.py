"""Main app — FastAPI server + background scheduler.

Runs the e-ink calendar: periodically fetches Google Calendar events,
renders the current view, and displays it via the C IT8951 driver.
Exposes a settings web UI on the LAN for configuration.
"""
import asyncio
import datetime
import logging
import socket
import threading
import time

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from . import config, settings_store, driver, calendar_client, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eink.main")

app = FastAPI(title="E-Ink Calendar")

# ---- State ----
_last_events: list[dict] = []
_last_events_hash: str = ""
_last_time_line_render: float = 0.0
_render_lock = threading.Lock()


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


def _events_hash(events: list[dict]) -> str:
    """Stable hash of event list to detect changes."""
    parts = []
    for ev in events:
        parts.append(f"{ev['id']}|{ev['summary']}|{ev['start']}|{ev['end']}|{ev['all_day']}")
    return "|".join(sorted(parts))


# ---- Rendering pipeline ----
def do_render(force: bool = False) -> bool:
    """Fetch events + render + display. Thread-safe."""
    with _render_lock:
        global _last_events, _last_events_hash
        settings = settings_store.load()
        now = datetime.datetime.now()

        # Fetch events for the visible range
        if settings["view_mode"] == "month":
            start = datetime.datetime(now.year, now.month, 1)
            end = start + datetime.timedelta(days=42)
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

        if not force and not events_changed:
            logger.debug("No event changes, skipping render")
            return False

        img = render.render_calendar(
            view_mode=settings["view_mode"],
            events=events,
            day_start=settings["day_start"],
            day_end=settings["day_end"],
            max_full_day=settings["max_full_day_events"],
            now=now,
        )
        ok = driver.render_to_screen(img, brightness=settings.get("brightness", 1.4))
        if ok:
            logger.info("Screen updated (events_changed=%s, %d events)", events_changed, len(events))
        return ok


def render_setup_screen() -> bool:
    """Show QR code + LAN IP on the e-ink for initial setup."""
    lan_ip = _get_lan_ip()
    port = config.APP_PORT
    url = f"http://{lan_ip}:{port}/settings"
    img = render.render_qr_setup(url, lan_ip, port)
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
                    img = render.render_setup_required(lan_ip, config.APP_PORT)
                    driver.render_to_screen(img, brightness=1.0)
                # Show setup screen every 5 min
                time.sleep(300)
                continue

            # Event poll
            poll_interval = s.get("event_poll_interval_sec", 60)
            do_render()

            # Time-line update (every 15 min by default)
            tl_interval = s.get("time_line_interval_min", 15) * 60
            now_ts = time.time()
            if now_ts - _last_time_line_render >= tl_interval:
                do_render(force=True)
                _last_time_line_render = now_ts

            time.sleep(poll_interval)
        except Exception as e:
            logger.error("Background loop error: %s", e)
            time.sleep(60)

    logger.info("Background scheduler stopped")


# ---- FastAPI routes ----

@app.on_event("startup")
async def startup():
    """Start background scheduler + initial render."""
    lan_ip = _get_lan_ip()
    logger.info("E-Ink Calendar starting on %s:%d (LAN: %s:%d)",
                config.APP_HOST, config.APP_PORT, lan_ip, config.APP_PORT)
    logger.info("IT8951 binary: %s", config.IT8951_BINARY)

    # Initial screen
    if not calendar_client.is_configured():
        img = render.render_setup_required(lan_ip, config.APP_PORT)
        driver.render_to_screen(img, brightness=1.0)
    elif not calendar_client.is_authenticated():
        render_setup_screen()
    else:
        do_render(force=True)

    # Start scheduler
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()


@app.get("/", response_class=HTMLResponse)
async def index():
    """Redirect to settings page."""
    return RedirectResponse(url="/settings")


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    """Settings web UI."""
    s = settings_store.load()
    authenticated = calendar_client.is_authenticated()
    configured = calendar_client.is_configured()
    lan_ip = _get_lan_ip()

    cals = []
    if authenticated:
        cals = calendar_client.list_calendars()

    cal_checkboxes = ""
    for cal in cals:
        checked = "checked" if cal["id"] in s.get("selected_calendars", []) else ""
        cal_checkboxes += f"""
        <label class="cal-item">
            <input type="checkbox" name="selected_calendars" value="{cal['id']}" {checked}>
            <span class="cal-dot" style="background:{cal['color']}"></span>
            <span>{cal['summary']}</span>
        </label>"""

    auth_section = ""
    if not configured:
        auth_section = '<div class="alert">⚠️ Google OAuth not configured. Place <code>client_secret.json</code> in <code>config/</code> and restart.</div>'
    elif not authenticated:
        auth_section = f'<a class="btn btn-auth" href="/auth/start">🔐 Login with Google</a>'
    else:
        auth_section = '<span class="badge badge-ok">✓ Google connected</span> <a class="btn btn-small" href="/auth/logout">Logout</a>'

    return _SETTINGS_HTML.format(
        auth_section=auth_section,
        view_mode=s["view_mode"],
        day_start=s["day_start"],
        day_end=s["day_end"],
        max_fd=s["max_full_day_events"],
        tl_interval=s["time_line_interval_min"],
        poll_interval=s["event_poll_interval_sec"],
        brightness=s.get("brightness", 1.4),
        cal_checkboxes=cal_checkboxes,
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


@app.post("/api/settings")
async def update_settings(upd: SettingsUpdate):
    """Update settings and trigger a re-render."""
    data = upd.model_dump(exclude_none=True)
    settings_store.update(data)
    # Trigger immediate re-render
    threading.Thread(target=lambda: do_render(force=True), daemon=True).start()
    return {"ok": True}


@app.post("/api/render")
async def trigger_render():
    """Manually trigger a screen render."""
    threading.Thread(target=lambda: do_render(force=True), daemon=True).start()
    return {"ok": True}


@app.get("/api/status")
async def status():
    return {
        "authenticated": calendar_client.is_authenticated(),
        "configured": calendar_client.is_configured(),
        "lan_ip": _get_lan_ip(),
        "port": config.APP_PORT,
        "settings": settings_store.load(),
    }


# ---- Google OAuth routes ----

@app.get("/auth/start")
async def auth_start(request: Request):
    """Start Google OAuth flow."""
    lan_ip = _get_lan_ip()
    redirect_uri = f"http://{lan_ip}:{config.APP_PORT}/auth/callback"
    try:
        auth_url = calendar_client.start_auth(redirect_uri)
        return RedirectResponse(url=auth_url)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/auth/callback")
async def auth_callback(code: str):
    """Handle OAuth callback from Google."""
    ok = calendar_client.complete_auth(code)
    if ok:
        do_render(force=True)
        return RedirectResponse(url="/settings?auth=success")
    return JSONResponse({"error": "OAuth failed"}, status_code=500)


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
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #1a1a2e; color: #eee; padding: 20px; max-width: 600px; margin: 0 auto; }}
h1 {{ font-size: 1.5em; margin-bottom: 20px; text-align: center; }}
.card {{ background: #16213e; border-radius: 12px; padding: 20px; margin-bottom: 16px; }}
.card h2 {{ font-size: 1.1em; margin-bottom: 12px; color: #e94560; }}
label {{ display: block; margin-bottom: 8px; font-size: 0.9em; }}
select, input[type="time"], input[type="number"], input[type="range"] {{
  width: 100%; padding: 8px; border-radius: 6px; border: 1px solid #333;
  background: #0f3460; color: #eee; font-size: 0.9em; }}
.btn {{ display: inline-block; padding: 10px 20px; border-radius: 8px;
  border: none; cursor: pointer; font-size: 0.9em; text-decoration: none; text-align: center; }}
.btn-primary {{ background: #e94560; color: white; width: 100%; margin-top: 12px; }}
.btn-auth {{ background: #4285F4; color: white; }}
.btn-small {{ background: #333; color: #eee; font-size: 0.8em; padding: 6px 12px; }}
.alert {{ background: #3d2010; border: 1px solid #e94560; border-radius: 8px;
  padding: 12px; margin-bottom: 12px; font-size: 0.85em; }}
.alert code {{ background: #2a1a0a; padding: 2px 6px; border-radius: 4px; }}
.badge {{ display: inline-block; padding: 4px 10px; border-radius: 12px;
  font-size: 0.8em; margin-right: 8px; }}
.badge-ok {{ background: #1b4332; color: #95d5b2; }}
.cal-item {{ display: flex; align-items: center; gap: 8px; padding: 6px 0; cursor: pointer; }}
.cal-item input {{ width: auto; }}
.cal-dot {{ width: 14px; height: 14px; border-radius: 50%; display: inline-block; }}
.footer {{ text-align: center; font-size: 0.75em; color: #555; margin-top: 20px; }}
.row {{ display: flex; gap: 12px; }}
.row > div {{ flex: 1; }}
</style>
</head>
<body>
<h1>📅 E-Ink Calendar</h1>

<div class="card">
  <h2>Google Account</h2>
  {auth_section}
</div>

<div class="card">
  <h2>View Settings</h2>
  <form id="settingsForm">
    <label>View Mode
      <select name="view_mode">
        <option value="month" {'selected' if view_mode=='month' else ''}>Month</option>
        <option value="week" {'selected' if view_mode=='week' else ''}>Week</option>
        <option value="7days" {'selected' if view_mode=='7days' else ''}>7 Days (from today)</option>
      </select>
    </label>
    <div class="row">
      <div><label>Day Start <input type="time" name="day_start" value="{day_start}"></label></div>
      <div><label>Day End <input type="time" name="day_end" value="{day_end}"></label></div>
    </div>
    <label>Max Full-Day Events (per day): {max_fd}
      <input type="range" name="max_full_day_events" min="1" max="3" value="{max_fd}">
    </label>
    <label>Time Line Update (minutes)
      <input type="number" name="time_line_interval_min" value="{tl_interval}" min="1" max="60">
    </label>
    <label>Event Poll Interval (seconds)
      <input type="number" name="event_poll_interval_sec" value="{poll_interval}" min="10" max="600">
    </label>
    <label>Brightness: {brightness}
      <input type="range" name="brightness" min="1.0" max="2.0" step="0.1" value="{brightness}">
    </label>
  </form>
</div>

<div class="card">
  <h2>Calendars</h2>
  {cal_checkboxes}
  <p style="font-size:0.75em;color:#666;margin-top:8px;">Empty selection = all calendars</p>
</div>

<button class="btn btn-primary" onclick="saveSettings()">💾 Save &amp; Render</button>
<button class="btn btn-primary" style="background:#0f3460;margin-top:8px;" onclick="renderNow()">🔄 Render Now</button>

<div class="footer">
  E-Ink Calendar · {lan_ip}:{port} · 1872×1404 IT8951
</div>

<script>
async function saveSettings() {{
  const form = document.getElementById('settingsForm');
  const fd = new FormData(form);
  // Collect checked calendars
  const cals = document.querySelectorAll('input[name="selected_calendars"]:checked');
  const calIds = Array.from(cals).map(c => c.value);

  const data = {{
    view_mode: fd.get('view_mode'),
    day_start: fd.get('day_start'),
    day_end: fd.get('day_end'),
    max_full_day_events: parseInt(fd.get('max_full_day_events')),
    time_line_interval_min: parseInt(fd.get('time_line_interval_min')),
    event_poll_interval_sec: parseInt(fd.get('event_poll_interval_sec')),
    brightness: parseFloat(fd.get('brightness')),
    selected_calendars: calIds,
  }};
  const r = await fetch('/api/settings', {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(data)
  }});
  if (r.ok) alert('Settings saved! Screen updating...');
  else alert('Error saving settings');
}}
async function renderNow() {{
  const r = await fetch('/api/render', {{method: 'POST'}});
  if (r.ok) alert('Rendering...');
}}
</script>
</body>
</html>
"""


def main():
    """Entry point — run the FastAPI app with uvicorn."""
    import uvicorn
    uvicorn.run(app, host=config.APP_HOST, port=config.APP_PORT, log_level="info")


if __name__ == "__main__":
    main()