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
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from fastapi import FastAPI, Request, UploadFile, File, Form
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

def do_render(force: bool = False) -> bool:
    """Fetch events + render + display. Thread-safe with timeout."""
    logger.info("do_render(force=%s) starting", force)
    if not _render_lock.acquire(timeout=120):
        logger.error("do_render: could not acquire render lock within 120s")
        return False
    try:
        global _last_events, _last_events_hash
        settings = settings_store.load()
        now = _now()

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
    sel_week = "selected" if s["view_mode"] == "week" else ""
    sel_7days = "selected" if s["view_mode"] == "7days" else ""
    tz = s.get("timezone", "")

    return _SETTINGS_HTML.format(
        auth_section=auth_section,
        sel_month=sel_month,
        sel_week=sel_week,
        sel_7days=sel_7days,
        day_start=s["day_start"],
        day_end=s["day_end"],
        max_fd=s["max_full_day_events"],
        tl_interval=s["time_line_interval_min"],
        poll_interval=s["event_poll_interval_sec"],
        brightness=s.get("brightness", 1.4),
        timezone=tz,
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


@app.post("/api/settings")
async def update_settings(
    view_mode: str = Form("week"),
    day_start: str = Form("07:00"),
    day_end: str = Form("23:00"),
    max_full_day_events: int = Form(3),
    time_line_interval_min: int = Form(15),
    event_poll_interval_sec: int = Form(60),
    brightness: float = Form(1.4),
    timezone: str = Form(""),
):
    """Save settings from form POST, trigger render, redirect back."""
    data = {
        "view_mode": view_mode,
        "day_start": day_start,
        "day_end": day_end,
        "max_full_day_events": max_full_day_events,
        "time_line_interval_min": time_line_interval_min,
        "event_poll_interval_sec": event_poll_interval_sec,
        "brightness": brightness,
        "timezone": timezone,
    }
    logger.info("Settings updated: %s", {k: v for k, v in data.items() if k != "selected_calendars"})
    settings_store.update(data)
    threading.Thread(target=_safe_render, daemon=True).start()
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/api/render")
async def trigger_render():
    """Trigger render via GET link, redirect back."""
    threading.Thread(target=_safe_render, daemon=True).start()
    return RedirectResponse(url="/settings", status_code=303)


def _safe_render():
    """Call do_render with full exception logging."""
    logger.info("Manual render triggered")
    try:
        ok = do_render(force=True)
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


# ---- Google OAuth routes ----

# Google blocks IP addresses in redirect URIs. The workaround:
# use http://localhost (allowed without HTTPS) and let the user
# copy the authorization code from the failed redirect manually.

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
        do_render(force=True)
        return {"ok": True}
    return JSONResponse({"error": err or "Code exchange failed"}, status_code=400)


@app.get("/auth/callback")
async def auth_callback(code: str = ""):
    """Handle OAuth callback (direct redirect — may not work for LAN IPs).
    Also renders a page the user can copy the code from."""
    if code:
        ok, _ = calendar_client.complete_auth(code)
        if ok:
            do_render(force=True)
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
  <form id="settingsForm" action="/api/settings" method="POST">
    <label>View Mode
      <select name="view_mode">
        <option value="month" {sel_month}>Month</option>
        <option value="week" {sel_week}>Week</option>
        <option value="7days" {sel_7days}>7 Days (from today)</option>
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
    <label>Timezone
      <input type="text" name="timezone" value="{timezone}" placeholder="Auto-detected from IP" style="font-size:0.85em">
      <span style="font-size:0.75em;color:#666">IANA name (e.g. Europe/Moscow) or UTC offset (e.g. +3, +1.5, -4)</span>
    </label>
  </form>
</div>

<div class="card">
  <h2>Calendars</h2>
  {cal_checkboxes}
  {cal_error}
  <p style="font-size:0.75em;color:#666;margin-top:8px;">Empty selection = all calendars</p>
</div>

<button type="submit" class="btn btn-primary">💾 Save &amp; Render</button>
<a href="/api/render" class="btn btn-primary" style="background:#0f3460;margin-top:8px;display:block;text-align:center">🔄 Render Now</a>
<p id="saveStatus"></p>

<div class="footer">
  E-Ink Calendar · {lan_ip}:{port} · 1872×1404 IT8951
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