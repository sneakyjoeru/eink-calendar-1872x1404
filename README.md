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
┌──────────────────────────────────────────────────┐
│ Orange Pi Zero 2W                                │
│                                                  │
│  ┌──────────────┐     ┌───────────────────────┐  │
│  │ IT8951 C     │     │ FastAPI app           │  │
│  │ driver       │◄────│ (Python + PIL)        │  │
│  │ (binary)     │     │ - Google OAuth        │  │
│  └──────┬───────┘     │ - Calendar sync       │  │
│         │ SPI         │ - Settings API        │  │
│         ▼             │ - Live preview        │  │
│  ┌──────────────┐     └───────────────────────┘  │
│  │ E-Ink HAT    │                 │ HTTPS :8889  │
│  │ 1872×1404    │                 ▼              │
│  └──────────────┘     ┌───────────────────────┐  │
│                       │ LAN  (QR + IP:port)   │  │
│                       └───────────────────────┘  │
│                                                  │
└──────────────────────────────────────────────────┘
```

The app calls the C IT8951 binary to render each frame. Python (PIL) composes
the calendar layout to a PNG, then the C driver displays it. Regional
differential updates compare the new image against the last displayed image
and only refresh the changed area — small updates (e.g. time-line movement)
take ~2s instead of ~5s for a full refresh.

## Setup

### 1. Hardware & driver

Orange Pi Zero 2W (Armbian/Ubuntu) with the Waveshare 7.8" IT8951 HAT wired over
SPI. The app drives the panel through the IT8951 C driver binary. A prebuilt
aarch64 binary is bundled at `bin/it8951`; you can also download `it8951-aarch64`
from this repo's **Releases** and drop it there (`chmod +x bin/it8951`), or build
it from `bin/c-driver/` (`cd bin/c-driver && sudo make build-pi`). The full driver
lives in the [standalone driver repo](https://github.com/sneakyjoeru/it8951-epaper-c-orangepi-zero-2w).

Configure the SPI overlay + packages once, then reboot if prompted:

```bash
sudo ./bin/it8951 --setup
sudo reboot
sudo ./bin/it8951 --info      # verify the panel responds (1872 x 1404)
```

### 2. Install

```bash
sudo git clone https://github.com/sneakyjoeru/eink-calendar-1872x1404.git /opt/eink-calendar
cd /opt/eink-calendar
sudo ./install.sh
```

`install.sh` installs the Python dependencies, creates `config/`, seeds `.env`
from `.env.example`, and copies the systemd unit. The app expects to live at
`/opt/eink-calendar` (the service's `WorkingDirectory`); if you cloned elsewhere,
symlink it there or edit `WorkingDirectory` in `eink-calendar.service`.

### 3. Run as a service

```bash
sudo systemctl enable --now eink-calendar     # start now + on every boot
systemctl status eink-calendar
sudo journalctl -u eink-calendar -f           # follow logs
```

The unit runs `python3 -m app.main` as **root** (needed for SPI/GPIO), auto-restarts
on failure, and starts after the network is up. To run it in the foreground
instead (for debugging): `sudo python3 -m app.main`.

### 4. First launch — all from your phone, no SSH needed

1. **No WiFi yet?** The display hosts its own hotspot (`EInk-Calendar-Setup`, with
   a random password) and shows two QR codes: scan the first to join the hotspot,
   the second to open the setup page and enter your home-WiFi credentials. It
   reconnects and re-renders automatically. If WiFi later drops and can't recover,
   the hotspot returns the same way.
2. **Connect Google.** The e-ink then shows a *Setup Required* screen — a QR to the
   settings page plus on-screen steps to create a Google OAuth app:
   - In [Google Cloud Console](https://console.cloud.google.com/): create a project
     → enable the **Google Calendar API** → OAuth consent screen (User type
     *External*, add your Google account as a **Test user**) → create an
     **OAuth client ID** (*Web application*) → add the redirect URI
     `http://localhost:8889/auth/callback` → **Download JSON** (`client_secret.json`).
   - Scan the QR to open `https://<pi-ip>:8889/settings`, upload `client_secret.json`,
     tap **Login with Google**, approve, copy the shown code and paste it back, then
     pick your calendars.
3. **Choose a look.** Pick a preset (grouped by view) or tune the settings, and
   Save. Done.

> The settings page is HTTPS with a self-signed certificate, so your browser will
> warn once — that's expected for a LAN device; proceed to the page.

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
| Time-line interval | 1, 2, 5, 10, 15, 20, 30 min | 10 | Time-line refresh cadence — whole fractions of the hour, clock-aligned (week & 7-day views) |
| Time-line style | solid, dotted, wavy | dotted | Look of the current-time indicator |
| Update mode | `soft`, `hard`, `du` | `soft` | Regional-update style: soft (GL16, no flash), hard (flash inner + GL16), du (1-bit DU, no flash/ghosting — b/w mode) |
| B/W mode | on/off | off | 1-bit black/white rendering (crisp, never darkens; pair with DU updates) |
| Show location & description | on/off | on | Show the event location (`@`) and description under the title/time when there's room |
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