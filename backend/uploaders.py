"""Upload finished photos to Google Drive (rclone) and/or FTP. Returns share links."""
from __future__ import annotations

import shutil
import subprocess
from ftplib import FTP, FTP_TLS
from pathlib import Path

from .models import FTPDestination, GDriveDestination, Settings


def gdrive_upload(files: list[Path], g: GDriveDestination) -> dict:
    if not shutil.which("rclone"):
        return {"ok": False, "error": "rclone not installed on the Pi"}
    dest = f"{g.rclone_remote}:{g.folder}"
    links: list[str] = []
    try:
        for f in files:
            subprocess.run(["rclone", "copy", str(f), dest],
                           check=True, capture_output=True, text=True, timeout=120)
            if g.make_share_link:
                r = subprocess.run(["rclone", "link", f"{dest}/{f.name}"],
                                   capture_output=True, text=True, timeout=60)
                if r.returncode == 0 and r.stdout.strip():
                    links.append(r.stdout.strip())
        return {"ok": True, "links": links, "dest": dest}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": (e.stderr or str(e)).strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ftp_upload(files: list[Path], c: FTPDestination) -> dict:
    try:
        ftp: FTP = FTP_TLS() if c.use_tls else FTP()
        ftp.connect(c.host, c.port, timeout=30)
        ftp.login(c.username, c.password)
        if c.use_tls and isinstance(ftp, FTP_TLS):
            ftp.prot_p()
        ftp.set_pasv(c.passive)
        # ensure remote dir exists (mkdir each segment, ignore errors)
        for seg in [s for s in c.remote_dir.split("/") if s]:
            try:
                ftp.mkd(seg)
            except Exception:
                pass
            ftp.cwd(seg)
        for f in files:
            with open(f, "rb") as fh:
                ftp.storbinary(f"STOR {f.name}", fh)
        ftp.quit()
        return {"ok": True, "count": len(files), "dir": c.remote_dir}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def upload_all(files: list[Path], settings: Settings) -> dict:
    results: dict = {}
    if settings.storage.gdrive.enabled:
        results["gdrive"] = gdrive_upload(files, settings.storage.gdrive)
    if settings.storage.ftp.enabled:
        results["ftp"] = ftp_upload(files, settings.storage.ftp)
    return results
