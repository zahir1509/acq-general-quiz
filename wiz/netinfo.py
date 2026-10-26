"""
Network facts for the welcome screen: this machine's LAN address and the name
of the Wi-Fi network it is on. Everything here is best-effort -- the scorer can
always override the Wi-Fi name in SETUP, and `--ip` overrides the address.
"""

import socket
import subprocess
import sys


def detect_lan_ip():
    """Best-effort local address to print in the join URL / QR code."""
    for probe in ("10.255.255.255", "192.168.1.1", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect((probe, 1))  # UDP: picks the outgoing interface, sends nothing
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout or ""
    except Exception:
        return ""


# ------------------------------------------------------------------ parsers
def parse_netsh_ssid(text):
    """`netsh wlan show interfaces` -> SSID of the connected network, or ''."""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "SSID":  # not BSSID
            return value.strip()
    return ""


def parse_nmcli_ssid(text):
    """`nmcli -t -f active,ssid dev wifi` -> SSID of the active network."""
    for line in text.splitlines():
        if line.startswith("yes:"):
            return line[4:].replace("\\:", ":").strip()
    return ""


# ------------------------------------------------------------------- probes
def _probe_windows():
    return parse_netsh_ssid(_run(["netsh", "wlan", "show", "interfaces"]))


def _probe_linux():
    name = parse_nmcli_ssid(_run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]))
    return name or _run(["iwgetid", "-r"]).strip()


def _mac_wifi_devices():
    devs = []
    lines = _run(["networksetup", "-listallhardwareports"]).splitlines()
    for i, line in enumerate(lines):
        if "Wi-Fi" in line or "AirPort" in line:
            for nxt in lines[i + 1:i + 3]:
                if nxt.startswith("Device:"):
                    devs.append(nxt.split(":", 1)[1].strip())
                    break
    for fallback in ("en0", "en1"):
        if fallback not in devs:
            devs.append(fallback)
    return devs


def _probe_mac():
    # macOS has moved this around between releases and gates some of it behind
    # Location Services, so try every interface with every method.
    for dev in _mac_wifi_devices():
        for line in _run(["ipconfig", "getsummary", dev]).splitlines():
            if " SSID : " in line:
                name = line.split(" SSID : ", 1)[1].strip()
                if name and name != "<redacted>":
                    return name
        out = _run(["networksetup", "-getairportnetwork", dev])
        if ":" in out and "not associated" not in out.lower() \
                and "not a Wi-Fi" not in out:
            name = out.split(":", 1)[1].strip()
            if name:
                return name
    return ""


def probe_ssid():
    """Name of the Wi-Fi network this machine is connected to, or ''."""
    if sys.platform.startswith("win"):
        return _probe_windows()
    if sys.platform == "darwin":
        return _probe_mac()
    return _probe_linux()
