# E-Ink Calendar — Orange Pi Zero 2W + IT8951 (1872×1404)

E-ink calendar app for the **Waveshare 7.8" E-Ink HAT** (1872×1404, IT8951) on
an **Orange Pi Zero 2W**. Syncs with **Google Calendar** via OAuth, exposes a
settings web UI on the LAN, and renders month / week / 7-day / 35-day views
with a live current-time indicator line.

## Features

- 📅 **Google Calendar sync** — log in with a Google account, select which calendars to display
- 🖥️ **Four view modes** — Month, Month (5 weeks), Week, 7-days (from today)
- ⏰ **Configurable day span** — set start/end-of-day times
- 📋 **Full-day events** — display 0–3 full-day events per day, stacked vertically
- 📍 **Current-time line** — striped line with time label, auto-updates at configurable intervals (1 min – 60 min)
- ⚡ **ASAP event updates** — screen refreshes when events are added or removed
- 🔄 **Regional differential updates** — only refreshes the changed area using the [IT8951 C driver](https://github.com/sneakyjoeru/it8951-epaper-c-orangepi-zero-2w) diff mode (`--soft`/`--hard`). Soft keeps the old pixels around the change (no flash); Hard briefly flashes the changed area. Full-screen GC16 clean refreshes only happen on day change, the configured interval, or when the optional "fullscreen on event end" toggle is on — never during small regional updates.
- 🌫️ **Dim past events** — past days and ended events are dimmed (toggleable)
- 📏 **Text size modifier** — adjust all text sizes globally (+/- pixels)
- 🔐 **HTTPS settings server** — self-signed SSL, OAuth via `http://localhost` redirect
- 📱 **LAN setup via QR code** — the e-ink shows a QR code linking to the app's settings page
- 🖼️ **Live preview** — view the current e-ink display from any browser at `/preview`
- 📷 **Image endpoint** — fetch the last rendered image at `/image`
- 📶 **WiFi hotspot** — captive portal for initial setup without an existing network
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
│  ┌─────────────┐    │  - Live preview       │  │
│  │  E-Ink HAT  │    └────────┬────────────┘  │
│  │  1872×1404  │             │ HTTPS :8889   │
│  └─────────────┘             ▼               │
│                      ┌─────────────────────┐  │
│                      │  LAN (QR + IP:port) │  │
│                      └─────────────────────┘  │
└─────────────────────────────────────────────┘
```

The app calls the C IT8951 binary to render each frame. Python (PIL) composes
the calendar layout to a PNG, then the C driver displays it. Regional
differential updates compare the new image against the last displayed image
and only refresh the changed area — small updates (e.g. time-line movement)
take ~2s instead of ~5s for a full refresh.

## Setup

### Prerequisites

The IT8951 C driver must be installed. A prebuilt aarch64 binary is bundled at
`bin/it8951`. To build it yourself on the Orange Pi (the build needs the Pi's
SPI/GPIO hardware headers and `libgpiod`/`libfreetype`), the full C source is in
`bin/c-driver/`:

```bash
cd bin/c-driver
sudo make build-pi            # builds ./it8951 (native aarch64)
sudo make install             # copies it to /opt/eink-calendar/bin/it8951
```

See [it8951-epaper-c-orangepi-zero-2w](https://github.com/sneakyjoeru/it8951-epaper-c-orangepi-zero-2w)
for the standalone driver repo. First-time OS setup:

```bash
sudo ./it8951 --setup   # configures overlay + packages
sudo reboot             # if prompted
```

The driver's regional update modes:
- `--soft` — GL16, no flash. The border keeps the old pixels so the soft update
  "considers the old state before the partial refresh" — only the inner changed
  area visibly updates (recommended).
- `--hard` — brief white flash of the inner changed area, then GL16 the region.
- `--fullscreen` — full-screen GC16 clean refresh (removes ghosting). Used for
  day change / interval / manual full refresh, not regional updates.
- `--border-smooth N` — partial-refresh area expansion in px. The changed region
  is expanded by N px on each side; the border zone keeps the old content (no
  dithering), so only the inner changed area is visually updated.

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

All settings are stored in `config/settings.json` and editable via the web UI
at `https://<pi-ip>:8889/settings`:

| Setting | Options | Default | Description |
|---------|---------|---------|-------------|
| View mode | `month`, `35days`, `week`, `7days` | `week` | Calendar layout |
| Day start | `HH:MM` | `07:00` | Start of displayed time range |
| Day end | `HH:MM` | `23:00` | End of displayed time range |
| Time format | `24h`, `12h` | `24h` | Hour label format |
| Date format | 13 options | Default | Page title date format |
| Max full-day events | 0 (hide), 1, 2, 3 | 3 | Full-day events per day |
| Smooth update interval | 1, 5, 10, 15, 30, 60 min | 15 | Time-line regional refresh interval (week & 7-day views) |
| Update mode | `soft`, `hard` | `soft` | Style for regional updates: soft (GL16, no flash, old state preserved at edges), hard (flash inner + GL16) |
| Refresh area expansion | 0, 2, 5, 10, 15, 20 mm | 5 | Partial-refresh area expansion — the changed region is expanded by this much; the border keeps the old content (no dithering) |
| Full refresh interval | Never, 30m, 1h, 1.5h, 2h, 3h, 6h, 12h, 24h | 6h | Forces a full-screen GC16 clean refresh to clear ghosting (also on day change) |
| Fullscreen on event end | on/off | off | Force a full-screen clean refresh when the event set changes (events ending/starting) — clears dimming ghosting |
| Event poll interval | seconds | 60 | How often to check for event changes |
| Brightness | 0.1 – 2.0 | 1.0 | Gamma boost for e-ink contrast |
| Text size modifier | -8 to +8 | 0 | Global font size adjustment |
| Timezone | IANA name or UTC offset | auto | Timezone for event display |
| Dim past events | on/off | off | Dim past days and ended events |
| Crossed event dim | on/off | off | Dim events when time line crosses them |
| Selected calendars | from Google account | all | Which calendars to display |

## Web Endpoints

| URL | Description |
|-----|-------------|
| `/settings` | Settings page (configure everything) |
| `/preview` | Live preview of the e-ink display (auto-refreshes every 15s) |
| `/image` | Last rendered e-ink image (PNG) |
| `/api/render` | Trigger a manual render |
| `/api/status` | System status JSON |
| `/health` | Health check |
| `/auth/start` | Start Google OAuth flow |
| `/auth/exchange` | Exchange OAuth code for tokens |
| `/auth/logout` | Disconnect Google account |

## License

MIT