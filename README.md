# E-Ink Calendar — Orange Pi Zero 2W + IT8951 (1872×1404)

E-ink calendar app for the **Waveshare 7.8" E-Ink HAT** (1872×1404, IT8951) on
an **Orange Pi Zero 2W**. Syncs with **Google Calendar** via OAuth, exposes a
settings web UI on the LAN, and renders month / week / 7-day views with a
live current-time indicator line.

## Features

- 📅 **Google Calendar sync** — log in with a Google account, select which calendars to display
- 🖥️ **Three view modes** — Month, Week, 7-days
- ⏰ **Configurable day span** — set start/end-of-day times
- 📋 **Full-day events** — display up to 3 full-day events per day
- 📍 **Current-time line** — 2px black line with 1px white outline, auto-updates every 15 minutes
- ⚡ **ASAP event updates** — screen refreshes when events are added or removed
- 📐 **Screen-aware rendering** — optimized for 1872×1404 at 4bpp via the C IT8951 driver
- 📱 **LAN setup via QR code** — the e-ink shows a QR code linking to the app's settings page, with LAN IP + port printed below
- ⚙️ **FastAPI settings server** — configure everything from a browser on your phone/laptop

## Architecture

```
┌─────────────────────────────────────────────┐
│           Orange Pi Zero 2W                  │
│  ┌─────────────┐    ┌─────────────────────┐  │
│  │  C IT8951    │◄───│  FastAPI app         │  │
│  │  driver      │    │  (Python)            │  │
│  │  (binary)    │    │  - Google OAuth      │  │
│  └─────────────┘    │  - Calendar polling  │  │
│       │ SPI         │  - PIL rendering      │  │
│       ▼             │  - Settings API       │  │
│  ┌─────────────┐    └────────┬────────────┘  │
│  │  E-Ink HAT  │             │ HTTP :8889    │
│  │  1872×1404  │             ▼               │
│  └─────────────┘    ┌─────────────────────┐  │
│                      │  LAN (QR + IP:port) │  │
│                      └─────────────────────┘  │
└─────────────────────────────────────────────┘
```

The app calls the C IT8951 binary (`it8951 --image <png>`) to render each frame.
Python (PIL) composes the calendar layout to a PNG, then the C driver displays it
with the optimized overlapped-A2-clear + 4bpp pipeline (~4s per full refresh).

## Setup

### Prerequisites

The IT8951 C driver must be installed (see
[it8951-epaper-c-orangepi-zero-2w](https://github.com/sneakyjoeru/it8951-epaper-c-orangepi-zero-2w)):

```bash
sudo ./it8951 --setup   # configures overlay + packages
sudo reboot             # if prompted
```

### Google OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → APIs & Services → Credentials
2. Create an OAuth 2.0 Client ID (type: Web application)
3. Add `http://<pi-lan-ip>:8889/auth/callback` to Authorized redirect URIs
4. Enable the **Google Calendar API**
5. Download `client_secret.json` and place it at `config/client_secret.json` on the Pi

### Install & run

```bash
git clone https://github.com/sneakyjoeru/eink-calendar-1872x1404.git
cd eink-calendar-1872x1404
pip3 install -r requirements.txt

# Set the path to the C driver binary
export IT8951_BINARY=/home/orangepi/it8951-epaper-c/it8951

# Run (needs sudo for SPI/GPIO access via the C driver)
sudo python3 app/main.py
```

On first launch, the e-ink displays a QR code linking to
`http://<lan-ip>:8889/settings`. Open it on your phone, log in with Google,
select calendars, and choose a view mode.

## Configuration

All settings are stored in `config/settings.json` and editable via the web UI:

| Setting | Options | Default |
|---------|---------|---------|
| View mode | `month`, `week`, `7days` | `week` |
| Day start | `HH:MM` | `07:00` |
| Day end | `HH:MM` | `23:00` |
| Max full-day events | 1–3 | 3 |
| Selected calendars | from Google account | all |
| Update interval (time line) | minutes | 15 |
| Event poll interval | seconds | 60 |

## License

MIT