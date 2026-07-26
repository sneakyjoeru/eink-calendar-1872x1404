"""Google Calendar client — OAuth + event fetching.

Handles the OAuth flow (via the web app callback) and provides
functions to list calendars and fetch events for a date range.
"""
import datetime
import json
import logging
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from . import config

logger = logging.getLogger("eink.calendar")

_flow: Optional[Flow] = None
_creds: Optional[Credentials] = None


def _client_secret_path() -> Path:
    p = Path(config.GOOGLE_CLIENT_SECRET)
    if not p.is_absolute():
        p = config.BASE_DIR / p
    return p


def is_configured() -> bool:
    """True if Google client_secret.json exists."""
    return _client_secret_path().exists()


def is_authenticated() -> bool:
    """True if we have valid stored credentials."""
    global _creds
    if _creds and _creds.valid:
        return True
    token_path = Path(config.GOOGLE_TOKEN_FILE)
    if token_path.exists():
        _creds = Credentials.from_authorized_user_file(str(token_path), config.GOOGLE_SCOPES)
        if _creds and _creds.expired and _creds.refresh_token:
            try:
                _creds.refresh(Request())
                _save_token(_creds)
                return True
            except Exception as e:
                logger.warning("Token refresh failed: %s", e)
                _creds = None
                return False
        return _creds and _creds.valid
    return False


def _save_token(creds: Credentials) -> None:
    Path(config.GOOGLE_TOKEN_FILE).write_text(creds.to_json())


def start_auth(redirect_uri: str) -> str:
    """Start the OAuth flow, return the authorization URL."""
    global _flow
    secret = _client_secret_path()
    if not secret.exists():
        raise FileNotFoundError(f"client_secret.json not found at {secret}")
    _flow = Flow.from_client_secrets_file(
        str(secret),
        scopes=config.GOOGLE_SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, _ = _flow.authorization_url(access_type="offline", prompt="consent")
    return auth_url


def complete_auth(code: str) -> tuple[bool, str]:
    """Complete the OAuth flow with the code from the callback.

    Returns (True, "") on success, (False, error_message) on failure.
    """
    global _creds, _flow
    if not _flow:
        return False, "No active OAuth flow. Click 'Login with Google' first."
    try:
        _flow.fetch_token(code=code)
        _creds = _flow.credentials
        _save_token(_creds)
        logger.info("Google OAuth completed successfully")
        return True, ""
    except Exception as e:
        msg = str(e).split("\n")[0][:200]
        logger.error("OAuth code exchange failed: %s", msg)
        return False, msg


def logout() -> None:
    """Remove stored credentials."""
    global _creds
    _creds = None
    token_path = Path(config.GOOGLE_TOKEN_FILE)
    if token_path.exists():
        token_path.unlink()


def _get_service():
    """Build an authenticated Calendar service, refreshing if needed."""
    global _creds
    if not is_authenticated():
        return None
    if _creds.expired and _creds.refresh_token:
        _creds.refresh(Request())
        _save_token(_creds)
    return build("calendar", "v3", credentials=_creds, static_discovery=False)


def list_calendars() -> list[dict]:
    """List all calendars on the account. Returns [{id, summary, color, selected}]."""
    svc = _get_service()
    if not svc:
        return []
    try:
        resp = svc.calendarList().list().execute()
        result = []
        for cal in resp.get("items", []):
            result.append({
                "id": cal.get("id", ""),
                "summary": cal.get("summary", ""),
                "color": cal.get("backgroundColor", "#4285F4"),
                "selected": cal.get("selected", False),
                "access_role": cal.get("accessRole", "reader"),
            })
        return result
    except Exception as e:
        msg = str(e).split("\n")[0][:300]
        logger.error("list_calendars failed: %s", msg)
        # Return error info so the caller can display it
        return [{"_error": msg}]


def fetch_events(start: datetime.datetime, end: datetime.datetime,
                 calendar_ids: Optional[list[str]] = None) -> list[dict]:
    """Fetch events in [start, end) from selected (or all) calendars.

    Returns a list of event dicts:
      {id, summary, start, end, all_day, calendar_id, calendar_color}
    """
    svc = _get_service()
    if not svc:
        return []

    if not calendar_ids:
        # Use all calendars if none selected
        try:
            cal_list = svc.calendarList().list().execute()
            calendar_ids = [c["id"] for c in cal_list.get("items", [])]
        except Exception:
            return []

    all_events = []
    time_min = start.isoformat() + "Z" if start.tzinfo is None else start.isoformat()
    time_max = end.isoformat() + "Z" if end.tzinfo is None else end.isoformat()

    for cal_id in calendar_ids:
        try:
            events = svc.events().list(
                calendarId=cal_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            for ev in events.get("items", []):
                all_events.append(_normalize_event(ev, cal_id))
        except Exception as e:
            logger.warning("fetch_events for %s: %s", cal_id, e)

    # Sort by start time
    all_events.sort(key=lambda e: e["start"])
    return all_events


def _normalize_event(ev: dict, cal_id: str) -> dict:
    """Normalize a Google Calendar event into our format."""
    start_raw = ev.get("start", {})
    end_raw = ev.get("end", {})
    all_day = "date" in start_raw

    if all_day:
        start_dt = datetime.date.fromisoformat(start_raw["date"])
        end_dt = datetime.date.fromisoformat(end_raw["date"])
    else:
        start_dt = datetime.datetime.fromisoformat(
            start_raw["dateTime"].replace("Z", "+00:00")
        )
        end_dt = datetime.datetime.fromisoformat(
            end_raw["dateTime"].replace("Z", "+00:00")
        )

    return {
        "id": ev.get("id", ""),
        "summary": ev.get("summary", "(No title)"),
        "location": ev.get("location", ""),
        "start": start_dt,
        "end": end_dt,
        "all_day": all_day,
        "calendar_id": cal_id,
    }