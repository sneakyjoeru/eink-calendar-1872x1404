"""Configuration — loads from environment + .env file."""
import os
from pathlib import Path

# Load .env if present
env_file = Path(__file__).resolve().parent.parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
TMP_DIR = BASE_DIR / "tmp_render"

# IT8951 C driver binary (bundled in bin/)
IT8951_BINARY = os.environ.get("IT8951_BINARY", "/opt/eink-calendar/bin/it8951")

# App server
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8889"))

# Google OAuth
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "config/client_secret.json")
GOOGLE_TOKEN_FILE = str(CONFIG_DIR / "token.json")
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
]

# Settings
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Screen dimensions (Waveshare 7.8" IT8951)
SCREEN_W = 1872
SCREEN_H = 1404

# Render
RENDER_DPI = int(os.environ.get("RENDER_DPI", "150"))

# Ensure dirs
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
TMP_DIR.mkdir(parents=True, exist_ok=True)