"""Configuration — loads from environment + .env file."""
import os
import subprocess
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
SSL_DIR = CONFIG_DIR / "ssl"

# IT8951 C driver binary (bundled in bin/)
IT8951_BINARY = os.environ.get("IT8951_BINARY", "/opt/eink-calendar/bin/it8951")

# App server
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8889"))

# SSL / HTTPS
SSL_ENABLED = os.environ.get("SSL_ENABLED", "1") == "1"
SSL_CERT = os.environ.get("SSL_CERT", str(SSL_DIR / "cert.pem"))
SSL_KEY = os.environ.get("SSL_KEY", str(SSL_DIR / "key.pem"))

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
SSL_DIR.mkdir(parents=True, exist_ok=True)


def ensure_ssl_cert():
    """Generate a self-signed SSL cert if one doesn't exist.

    Uses openssl to create a cert valid for the LAN IP. The browser will
    show a security warning — this is expected for a local network device.
    """
    cert_path = Path(SSL_CERT)
    key_path = Path(SSL_KEY)
    if cert_path.exists() and key_path.exists():
        return True
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:4096",
             "-keyout", str(key_path), "-out", str(cert_path),
             "-days", "3650", "-nodes",
             "-subj", "/CN=E-Ink Calendar",
             "-addext", "subjectAltName=DNS:localhost,IP:192.168.0.199,IP:127.0.0.1"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        cert_path.chmod(0o644)
        key_path.chmod(0o600)
        return True
    except Exception as e:
        import logging
        logging.getLogger("eink.config").warning("SSL cert generation failed: %s", e)
        return False
TMP_DIR.mkdir(parents=True, exist_ok=True)