"""Photo printing via CUPS (the `lp` / `lpstat` CLI).

Works with any CUPS-configured printer — dedicated dye-sub photo printers (DNP, Canon
Selphy, Mitsubishi, …) via gutenprint, or any USB/network printer. Degrades gracefully:
if CUPS or a printer isn't configured, printing reports unavailable and the booth keeps
working. Kept CLI-based (no pycups C dependency) for portability on the Jetson.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def available() -> tuple[bool, str]:
    if not shutil.which("lp") or not shutil.which("lpstat"):
        return False, "CUPS not installed (lp/lpstat missing)"
    if not printers():
        return False, "no printer configured (add one in CUPS)"
    return True, "ready"


def printers() -> list[dict]:
    """Configured printers with their state; marks the system default."""
    if not shutil.which("lpstat"):
        return []
    out: list[dict] = []
    try:
        default = ""
        r = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=5)
        if ":" in r.stdout:
            default = r.stdout.split(":")[-1].strip()
        r = subprocess.run(["lpstat", "-p"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines():
            if line.startswith("printer "):
                name = line.split()[1]
                state = ("printing" if "printing" in line
                         else "idle" if "is idle" in line
                         else "disabled" if "disabled" in line else "unknown")
                out.append({"name": name, "default": name == default, "state": state})
    except Exception:
        pass
    return out


def print_files(files, printer: str = "", copies: int = 1, media: str = "",
                fit: bool = True) -> dict:
    """Send one or more image files to the printer. `printer=""` uses the CUPS default."""
    if not shutil.which("lp"):
        return {"ok": False, "error": "CUPS not installed"}
    files = [str(f) for f in files if Path(f).exists()]
    if not files:
        return {"ok": False, "error": "no file to print"}
    jobs: list[str] = []
    try:
        for f in files:
            cmd = ["lp"]
            if printer:
                cmd += ["-d", printer]
            if copies and copies > 1:
                cmd += ["-n", str(copies)]
            if media:
                cmd += ["-o", "media=" + media]
            if fit:
                cmd += ["-o", "fit-to-page"]
            cmd.append(f)
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return {"ok": False, "error": (r.stderr or r.stdout).strip() or "lp failed"}
            jobs.append(r.stdout.strip())
        return {"ok": True, "jobs": jobs, "count": len(files)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def queue() -> list[dict]:
    """Pending print jobs (CUPS queue)."""
    if not shutil.which("lpstat"):
        return []
    try:
        r = subprocess.run(["lpstat", "-o"], capture_output=True, text=True, timeout=5)
        return [{"job": ln.split()[0], "info": ln.strip()}
                for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        return []
