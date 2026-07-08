"""Wi-Fi management via NetworkManager (nmcli).

Two jobs:
  1. **Internet (STA)** — scan / join / forget networks on the management radio.
  2. **Guest hotspot (AP)** — run a WPA2 access point on a *separate* radio so the booth
     serves guests offline while staying online for cloud sync.

Safety: the **management interface** (the Wi-Fi device carrying our default route) is
never used for the AP and is refused by hotspot_up(), so configuring the hotspot can
never cut the admin/SSH link. On the Jetson that's the M.2 radio; the USB dongle is the AP.
"""
from __future__ import annotations

import subprocess

HOTSPOT_CON = "photobooth-ap"          # NetworkManager connection name for the guest AP


def _nmcli(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)


def _split_terse(line: str) -> list[str]:
    """Split an `nmcli -t` line on unescaped ':' (nmcli escapes literal colons as '\\:')."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _wifi_devices() -> list[dict]:
    r = _nmcli("-t", "-f", "DEVICE,TYPE,STATE", "device")
    devs = []
    for line in r.stdout.splitlines():
        p = _split_terse(line)
        if len(p) >= 3 and p[1] == "wifi":
            devs.append({"device": p[0], "state": p[2]})
    return devs


def mgmt_device() -> str | None:
    """The Wi-Fi device carrying the default route — our internet + admin link."""
    r = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        toks = line.split()
        if "dev" in toks:
            dev = toks[toks.index("dev") + 1]
            # only report it if it's actually a Wi-Fi device
            if any(d["device"] == dev for d in _wifi_devices()):
                return dev
    return None


def _ap_device(mgmt: str | None) -> str | None:
    for d in _wifi_devices():
        if d["device"] != mgmt:
            return d["device"]
    return None


def _connectivity() -> str:
    try:
        return _nmcli("-t", "-f", "CONNECTIVITY", "general").stdout.strip()
    except Exception:
        return "unknown"


def _connected_ssid(device: str | None) -> str | None:
    if not device:
        return None
    r = _nmcli("-t", "-f", "GENERAL.CONNECTION", "device", "show", device)
    val = r.stdout.strip().split(":", 1)[-1].strip()
    return val or None


# ---- status ---------------------------------------------------------------
def status() -> dict:
    mgmt = mgmt_device()
    ap = _ap_device(mgmt)
    return {
        "internet": _connectivity(),                 # full / limited / none / unknown
        "mgmt_device": mgmt,
        "mgmt_ssid": _connected_ssid(mgmt),
        "ap_device": ap,                             # spare radio available for the hotspot
        "wifi_devices": _wifi_devices(),
        "hotspot": hotspot_status(),
    }


# ---- STA (internet) -------------------------------------------------------
def scan(device: str | None = None) -> list[dict]:
    device = device or mgmt_device()
    if not device:
        return []
    try:
        _nmcli("device", "wifi", "rescan", "ifname", device, timeout=20)
    except Exception:
        pass                                          # rescan can fail while an AP is up; list anyway
    r = _nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
               "ifname", device, timeout=20)
    seen, nets = set(), []
    for line in r.stdout.splitlines():
        p = _split_terse(line)
        if len(p) < 4 or not p[1] or p[1] in seen:
            continue
        seen.add(p[1])
        nets.append({"in_use": p[0] == "*", "ssid": p[1],
                     "signal": int(p[2]) if p[2].isdigit() else 0,
                     "security": p[3] or "open"})
    nets.sort(key=lambda n: n["signal"], reverse=True)
    return nets


def connect(ssid: str, password: str = "", device: str | None = None) -> dict:
    device = device or mgmt_device()
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    if device:
        args += ["ifname", device]
    try:
        r = _nmcli(*args, timeout=45)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": r.returncode == 0, "error": r.stderr.strip()}


def forget(ssid: str) -> dict:
    r = _nmcli("connection", "delete", ssid)
    return {"ok": r.returncode == 0, "error": r.stderr.strip()}


# ---- AP (guest hotspot) ---------------------------------------------------
def hotspot_status() -> dict:
    """Live guest hotspot (SSID/password/state) from NetworkManager (single source of truth)."""
    try:
        state = _nmcli("-t", "-f", "GENERAL.STATE", "connection", "show", HOTSPOT_CON).stdout
        active = "activated" in state
        out = _nmcli("-s", "-t", "-f",
                     "802-11-wireless.ssid,802-11-wireless-security.psk,802-11-wireless.hidden",
                     "connection", "show", HOTSPOT_CON).stdout
        fields = dict(_split_terse(l)[:2] for l in out.splitlines() if ":" in l)
        ssid = fields.get("802-11-wireless.ssid", "")
    except Exception:
        return {"active": False}
    if not ssid:
        return {"active": False}
    return {"active": active, "ssid": ssid,
            "password": fields.get("802-11-wireless-security.psk", ""),
            "hidden": fields.get("802-11-wireless.hidden", "no") == "yes"}


def hotspot_up(ssid: str, password: str, device: str | None = None,
               band: str = "bg", hidden: bool = False) -> dict:
    mgmt = mgmt_device()
    device = device or _ap_device(mgmt)
    if not device:
        return {"ok": False, "error": "no spare Wi-Fi radio for the hotspot "
                "(plug in the USB Wi-Fi dongle; the M.2 radio stays as the internet link)"}
    if device == mgmt:
        return {"ok": False, "error": "refusing to use the management interface for the AP"}
    if len(password) < 8:
        return {"ok": False, "error": "hotspot password must be at least 8 characters (WPA2)"}
    _nmcli("connection", "delete", HOTSPOT_CON)
    r = _nmcli("connection", "add", "type", "wifi", "ifname", device, "con-name", HOTSPOT_CON,
               "autoconnect", "yes", "ssid", ssid,
               "802-11-wireless.mode", "ap", "802-11-wireless.band", band,
               "802-11-wireless.hidden", "yes" if hidden else "no",
               "ipv4.method", "shared", "ipv6.method", "disabled",
               "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password, timeout=30)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip()}
    r2 = _nmcli("connection", "up", HOTSPOT_CON, timeout=30)
    return {"ok": r2.returncode == 0, "error": r2.stderr.strip(), "device": device}


def hotspot_down() -> dict:
    _nmcli("connection", "down", HOTSPOT_CON)
    r = _nmcli("connection", "delete", HOTSPOT_CON)
    return {"ok": r.returncode == 0, "error": r.stderr.strip()}
