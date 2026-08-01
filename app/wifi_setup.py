"""WiFi hotspot fallback for E-Ink Calendar (NetworkManager / nmcli based).

The Orange Pi runs NetworkManager, so all WiFi operations go through `nmcli`
(driving hostapd/dnsmasq/wpa_supplicant directly would fight NM). When internet
connectivity is lost and can't be restored, the Pi raises a WiFi hotspot (AP
mode on wlan0) with a random password. The e-ink shows a QR code to join the
hotspot plus the URL of the WiFi-setup page, where the user enters their home
WiFi credentials. When connectivity is restored the hotspot is torn down and a
normal render is triggered.

Monitor design is deliberately conservative: the hotspot is only raised after
connectivity has been lost for several consecutive checks ("can't be restored"),
so a transient blip never flaps the AP up and down.
"""
import logging
import random
import string
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("eink.wifi")

WIFI_IFACE = "wlan0"
HOTSPOT_SSID = "EInk-Calendar-Setup"
HOTSPOT_CON = "eink-hotspot"      # nmcli connection name we create/manage
HOTSPOT_IP = "10.42.0.1"          # NetworkManager "shared" mode gateway/default

_hotspot_active = False
_hotspot_password = ""
_monitor_thread: Optional[threading.Thread] = None
_on_hotspot: Optional[Callable[[str, str, str], None]] = None   # (ssid, pw, ip)
_on_restore: Optional[Callable[[], None]] = None


# ---- helpers ---------------------------------------------------------------

def _run(args, timeout=20) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _random_password(length=10) -> str:
    # No ambiguous chars (0/O/1/l/I) — easier to read off a QR/type by hand.
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(random.choice(chars) for _ in range(length))


def _split_nmcli(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped ':' and unescape '\\' sequences."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
            i += 1
            continue
        cur += c
        i += 1
    out.append(cur)
    return out


# ---- state accessors -------------------------------------------------------

def is_hotspot_active() -> bool:
    return _hotspot_active


def hotspot_ssid() -> str:
    return HOTSPOT_SSID


def hotspot_password() -> str:
    return _hotspot_password


def hotspot_ip() -> str:
    return HOTSPOT_IP


def get_hotspot_qr_text() -> str:
    """WIFI-join QR payload — a phone camera scan joins the hotspot AP."""
    return f"WIFI:S:{HOTSPOT_SSID};T:WPA;P:{_hotspot_password};;"


# ---- connectivity ----------------------------------------------------------

def is_online(timeout: int = 5) -> bool:
    """True only if the Pi has real internet. Ping is authoritative; NM's
    connectivity check is used as a fast positive signal."""
    try:
        r = _run(["nmcli", "-t", "networking", "connectivity", "check"],
                 timeout=timeout + 2)
        if (r.stdout or "").strip() == "full":
            return True
    except Exception:
        pass
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            r = _run(["ping", "-c", "1", "-W", str(timeout), host],
                     timeout=timeout + 2)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


def wifi_connected() -> bool:
    """True if wlan0 has an active connection that is NOT our hotspot."""
    try:
        r = _run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"])
        for line in r.stdout.splitlines():
            parts = _split_nmcli(line)
            if len(parts) >= 2 and parts[1] == WIFI_IFACE and parts[0] != HOTSPOT_CON:
                return True
    except Exception:
        pass
    return False


def scan_networks() -> list[dict]:
    """Return [{ssid, signal, encrypted}] via nmcli. May be stale/empty while
    the radio is in AP (hotspot) mode — the setup page also allows manual SSID
    entry for that case."""
    nets: list[dict] = []
    try:
        r = _run(["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY",
                  "device", "wifi", "list"], timeout=20)
        seen = set()
        for line in r.stdout.splitlines():
            parts = _split_nmcli(line)
            if len(parts) < 3:
                continue
            ssid, signal, security = parts[0], parts[1], parts[2]
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            nets.append({
                "ssid": ssid,
                "signal": signal,
                "encrypted": bool(security.strip()),
            })
        nets.sort(key=lambda n: int(n.get("signal") or 0), reverse=True)
    except Exception as e:
        logger.error("scan failed: %s", e)
    return nets


# ---- hotspot ---------------------------------------------------------------

def start_hotspot() -> tuple[bool, str]:
    """Raise the WiFi hotspot via nmcli. Returns (ok, password_or_error)."""
    global _hotspot_active, _hotspot_password
    if _hotspot_active:
        return True, _hotspot_password
    pw = _random_password()
    try:
        _run(["nmcli", "connection", "delete", HOTSPOT_CON])  # clear any stale one
        r = _run(["nmcli", "device", "wifi", "hotspot",
                  "ifname", WIFI_IFACE, "con-name", HOTSPOT_CON,
                  "ssid", HOTSPOT_SSID, "password", pw], timeout=30)
        if r.returncode != 0:
            logger.error("hotspot start failed: %s", (r.stderr or "").strip())
            return False, (r.stderr or "").strip()
        _hotspot_active = True
        _hotspot_password = pw
        logger.info("Hotspot up: SSID=%s ip=%s", HOTSPOT_SSID, HOTSPOT_IP)
        return True, pw
    except Exception as e:
        logger.error("hotspot start exception: %s", e)
        return False, str(e)


def stop_hotspot() -> None:
    """Tear down the hotspot and let NetworkManager reconnect to known networks."""
    global _hotspot_active
    try:
        _run(["nmcli", "connection", "down", HOTSPOT_CON])
        _run(["nmcli", "connection", "delete", HOTSPOT_CON])
        _run(["nmcli", "device", "connect", WIFI_IFACE], timeout=30)
    except Exception as e:
        logger.error("hotspot stop error: %s", e)
    _hotspot_active = False
    logger.info("Hotspot stopped")


def connect_wifi(ssid: str, password: str) -> tuple[bool, str]:
    """Join the given network via nmcli, tearing down the hotspot first.
    On failure, the hotspot is brought back so the user isn't stranded.
    Returns (ok, error_message)."""
    was_hotspot = _hotspot_active
    if was_hotspot:
        stop_hotspot()
        time.sleep(2)
    try:
        args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", WIFI_IFACE]
        if password:
            args += ["password", password]
        r = _run(args, timeout=45)
        if r.returncode == 0:
            logger.info("Connected to %s", ssid)
            return True, ""
        err = (r.stderr or r.stdout or "connect failed").strip()
        logger.error("connect to %s failed: %s", ssid, err)
        if was_hotspot:
            start_hotspot()
        return False, err
    except Exception as e:
        logger.error("connect exception: %s", e)
        if was_hotspot:
            start_hotspot()
        return False, str(e)


# ---- monitor ---------------------------------------------------------------

def monitor_connectivity(interval: int = 60, grace_checks: int = 3) -> None:
    """Raise the hotspot only after connectivity has been lost for
    `grace_checks` consecutive checks; render the QR via the on_hotspot
    callback. While the hotspot is up, exit is user-driven (connect_wifi from
    the setup page), which calls on_restore. Conservative by design so a
    transient blip never flaps the AP."""
    fails = 0
    # Give NetworkManager time to auto-connect known networks on boot, then
    # provision promptly if this looks like a fresh/offline device.
    time.sleep(15)
    if not _hotspot_active and not is_online():
        for _ in range(4):
            time.sleep(8)
            if is_online():
                break
        if not is_online():
            ok, pw = start_hotspot()
            if ok and _on_hotspot:
                try:
                    _on_hotspot(HOTSPOT_SSID, pw, HOTSPOT_IP)
                except Exception as e:
                    logger.error("on_hotspot callback: %s", e)

    while True:
        try:
            if _hotspot_active:
                # Exit is driven by the user connecting via the setup page.
                time.sleep(interval)
                continue
            if is_online():
                fails = 0
            else:
                fails += 1
                logger.warning("connectivity lost (%d/%d)", fails, grace_checks)
                if fails >= grace_checks:
                    ok, pw = start_hotspot()
                    fails = 0
                    if ok and _on_hotspot:
                        try:
                            _on_hotspot(HOTSPOT_SSID, pw, HOTSPOT_IP)
                        except Exception as e:
                            logger.error("on_hotspot callback: %s", e)
            time.sleep(interval)
        except Exception as e:
            logger.error("monitor error: %s", e)
            time.sleep(interval)


def notify_restored() -> None:
    """Called after a successful user-driven reconnect to trigger a normal
    render (calendar / settings)."""
    if _on_restore:
        try:
            _on_restore()
        except Exception as e:
            logger.error("on_restore callback: %s", e)


def start_monitor(interval: int = 60,
                  on_hotspot: Optional[Callable[[str, str, str], None]] = None,
                  on_restore: Optional[Callable[[], None]] = None) -> None:
    """Start the connectivity monitor. `on_hotspot(ssid, pw, ip)` is called when
    the hotspot is raised (render the QR); `on_restore()` when connectivity is
    restored (render the calendar/settings)."""
    global _monitor_thread, _on_hotspot, _on_restore
    _on_hotspot = on_hotspot
    _on_restore = on_restore
    if _monitor_thread and _monitor_thread.is_alive():
        return
    _monitor_thread = threading.Thread(
        target=monitor_connectivity, args=(interval,), daemon=True
    )
    _monitor_thread.start()
