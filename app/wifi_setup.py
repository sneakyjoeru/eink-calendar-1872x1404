"""WiFi hotspot fallback for E-Ink Calendar.

When the Pi has no internet access, it starts a WiFi hotspot with a
random password so the user can connect and configure WiFi settings
through a captive portal page.

Uses hostapd + dnsmasq for the hotspot and a simple HTTP redirect
for the captive portal.
"""
import logging
import random
import string
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("eink.wifi")

HOTSPOT_IFACE = "wlan0"  # WiFi interface used for hotspot
HOTSPOT_SSID = "EInk-Calendar-Setup"
HOTSPOT_IP = "192.168.4.1"
HOSTAPD_CONF = "/etc/hostapd/hostapd.conf"
DNSMASQ_CONF = "/etc/dnsmasq.conf"

_hotspot_active = False
_hotspot_password = ""
_monitor_thread: Optional[threading.Thread] = None


def _random_password(length=10) -> str:
    """Generate a random alphanumeric password."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def _write_hostapd_conf(ssid: str, password: str):
    """Write hostapd configuration file."""
    conf = f"""interface={HOTSPOT_IFACE}
driver=nl80211
ssid={ssid}
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
"""
    Path(HOSTAPD_CONF).write_text(conf)


def _write_dnsmasq_conf():
    """Write dnsmasq configuration for captive portal."""
    conf = f"""interface={HOTSPOT_IFACE}
dhcp-range={HOTSPOT_IFACE},192.168.4.2,192.168.4.20,255.255.255.0,24h
dhcp-option=3,{HOTSPOT_IP}
dhcp-option=6,{HOTSPOT_IP}
server=8.8.8.8
log-queries
log-dhcp
listen-address={HOTSPOT_IP}
# Redirect all DNS queries to the Pi's IP (captive portal)
address=/#/{HOTSPOT_IP}
"""
    Path(DNSMASQ_CONF).write_text(conf)


def start_hotspot() -> tuple[bool, str]:
    """Start the WiFi hotspot. Returns (success, password_or_error)."""
    global _hotspot_active, _hotspot_password

    if _hotspot_active:
        return True, _hotspot_password

    password = _random_password()
    _hotspot_password = password

    try:
        # Stop any existing services that might interfere
        subprocess.run(["systemctl", "stop", "hostapd", "dnsmasq"],
                       capture_output=True, timeout=10)

        # Bring up the interface
        subprocess.run(["ip", "addr", "add", f"{HOTSPOT_IP}/24", "dev", HOTSPOT_IFACE],
                       capture_output=True, timeout=5)
        subprocess.run(["ip", "link", "set", HOTSPOT_IFACE, "up"],
                       capture_output=True, timeout=5)

        # Write configs
        _write_hostapd_conf(HOTSPOT_SSID, password)
        _write_dnsmasq_conf()

        # Start services
        subprocess.run(["systemctl", "start", "hostapd"],
                       capture_output=True, timeout=10)
        subprocess.run(["systemctl", "start", "dnsmasq"],
                       capture_output=True, timeout=10)

        _hotspot_active = True
        logger.info("WiFi hotspot started: SSID=%s, password=%s", HOTSPOT_SSID, password)
        return True, password
    except Exception as e:
        logger.error("Failed to start hotspot: %s", e)
        stop_hotspot()
        return False, str(e)


def stop_hotspot():
    """Stop the WiFi hotspot and restore network."""
    global _hotspot_active
    try:
        subprocess.run(["systemctl", "stop", "hostapd", "dnsmasq"],
                       capture_output=True, timeout=10)
        subprocess.run(["ip", "addr", "del", f"{HOTSPOT_IP}/24", "dev", HOTSPOT_IFACE],
                       capture_output=True, timeout=5)
        subprocess.run(["ip", "link", "set", HOTSPOT_IFACE, "down"],
                       capture_output=True, timeout=5)
        # Restore DHCP client
        subprocess.run(["dhclient", "-v", HOTSPOT_IFACE],
                       capture_output=True, timeout=10)
    except Exception as e:
        logger.error("Failed to stop hotspot: %s", e)
    _hotspot_active = False
    logger.info("WiFi hotspot stopped")


def is_online(timeout: int = 5) -> bool:
    """Check if the Pi has internet connectivity by pinging a reliable host."""
    try:
        subprocess.run(["ping", "-c", "1", "-W", str(timeout), "8.8.8.8"],
                       capture_output=True, timeout=timeout + 2, check=True)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def scan_networks() -> list[dict]:
    """Scan for available WiFi networks using iwlist.

    Returns a list of dicts: [{ssid, signal, encrypted}, ...]
    """
    try:
        result = subprocess.run(
            ["iwlist", HOTSPOT_IFACE, "scan"],
            capture_output=True, text=True, timeout=15
        )
        networks = []
        current = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if "ESSID:" in line:
                ssid = line.split('"')[1] if '"' in line else ""
                if ssid:
                    current["ssid"] = ssid
                    if current.get("ssid"):
                        networks.append(dict(current))
                    current = {}
            elif "Signal level=" in line:
                try:
                    level = line.split("Signal level=")[1].split(" ")[0]
                    current["signal"] = level
                except (IndexError, ValueError):
                    pass
            elif "Encryption key:on" in line:
                current["encrypted"] = True
            elif "Encryption key:off" in line:
                current["encrypted"] = False
        # Add last entry
        if current.get("ssid"):
            networks.append(dict(current))
        # Sort by signal strength (dBm, higher = stronger)
        networks.sort(key=lambda n: int(n.get("signal", "-100")), reverse=True)
        return networks
    except Exception as e:
        logger.error("Network scan failed: %s", e)
        return []


def get_hotspot_qr_text() -> str:
    """Return the QR code payload for the hotspot connection."""
    return f"WIFI:S:{HOTSPOT_SSID};T:WPA;P:{_hotspot_password};;"


def monitor_connectivity(interval: int = 60):
    """Background thread: monitor internet and toggle hotspot as needed."""
    while True:
        try:
            online = is_online()
            if online and _hotspot_active:
                logger.info("Internet restored — stopping hotspot")
                stop_hotspot()
            elif not online and not _hotspot_active:
                logger.info("No internet — starting hotspot")
                start_hotspot()
            time.sleep(interval)
        except Exception as e:
            logger.error("Connectivity monitor error: %s", e)
            time.sleep(interval)


def start_monitor(interval: int = 60):
    """Start the connectivity monitor in a background thread."""
    global _monitor_thread
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _monitor_thread = threading.Thread(
        target=monitor_connectivity, args=(interval,), daemon=True
    )
    _monitor_thread.start()
