"""Upload finished photos to Google Drive / Amazon S3 (both via rclone) and/or FTP.

Google Drive and S3 are configured entirely from the admin panel — no `rclone config`
on the CLI. For each upload we synthesise a throwaway rclone.conf from the stored
settings (OAuth token for Drive, access keys for S3) and pass it via `rclone --config`.
Returns per-destination dicts with `ok` and, when possible, share `links` for the QR.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from ftplib import FTP, FTP_TLS
from pathlib import Path

from .models import FTPDestination, GDriveDestination, S3Destination, Settings


def _have_rclone() -> bool:
    return shutil.which("rclone") is not None


def _rclone(conf: str | None, args: list[str], check: bool, timeout: int):
    cmd = ["rclone"]
    if conf:
        cmd += ["--config", conf]
    cmd += args
    return subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=timeout)


def _write_conf(lines: list[str]) -> str:
    fd, path = tempfile.mkstemp(suffix=".conf", prefix="rclone_")
    os.write(fd, ("\n".join(lines) + "\n").encode())
    os.close(fd)
    os.chmod(path, 0o600)               # holds the OAuth token / secret keys
    return path


# ---- Google Drive ---------------------------------------------------------
def _gdrive_conf(g: GDriveDestination) -> str:
    ini = [f"[{g.rclone_remote}]", "type = drive", "scope = drive.file"]
    if g.client_id:
        ini.append(f"client_id = {g.client_id}")
    if g.client_secret:
        ini.append(f"client_secret = {g.client_secret}")
    if g.team_drive:
        ini.append(f"team_drive = {g.team_drive}")
    ini.append(f"token = {g.token}")
    return _write_conf(ini)


def gdrive_upload(files: list[Path], g: GDriveDestination, subdir: str = "") -> dict:
    if not _have_rclone():
        return {"ok": False, "error": "rclone not installed"}
    # App-managed remote from the admin OAuth token. When there is no token, fall back
    # to a pre-existing system rclone remote (legacy `rclone config`), if any.
    if not g.token:
        return {"ok": False, "error": "Google Drive not connected — click Connect in the admin panel"}
    conf = _gdrive_conf(g)
    dest = f"{g.rclone_remote}:{g.folder}" + (f"/{subdir.strip('/')}" if subdir else "")
    links: list[str] = []
    try:
        for f in files:
            _rclone(conf, ["copy", str(f), dest], check=True, timeout=120)
            if g.make_share_link:
                r = _rclone(conf, ["link", f"{dest}/{f.name}"], check=False, timeout=60)
                if r.returncode == 0 and r.stdout.strip():
                    links.append(r.stdout.strip())
        return {"ok": True, "links": links, "dest": dest}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": (e.stderr or str(e)).strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        os.unlink(conf)


# ---- Amazon S3 (and S3-compatible) ----------------------------------------
def _s3_conf(c: S3Destination) -> str:
    ini = [f"[{_S3_REMOTE}]", "type = s3",
           f"provider = {c.provider or 'AWS'}",
           f"access_key_id = {c.access_key_id}",
           f"secret_access_key = {c.secret_access_key}",
           f"region = {c.region}"]
    if c.endpoint_url:
        ini.append(f"endpoint = {c.endpoint_url}")
    return _write_conf(ini)


_S3_REMOTE = "s3"


def _s3_public_url(c: S3Destination, key: str) -> str:
    if c.public_url_base:
        return c.public_url_base.rstrip("/") + "/" + key
    if c.endpoint_url:
        return c.endpoint_url.rstrip("/") + f"/{c.bucket}/{key}"
    return f"https://{c.bucket}.s3.{c.region}.amazonaws.com/{key}"


def s3_upload(files: list[Path], c: S3Destination, subdir: str = "") -> dict:
    if not _have_rclone():
        return {"ok": False, "error": "rclone not installed"}
    if not (c.bucket and c.access_key_id and c.secret_access_key):
        return {"ok": False, "error": "S3 not configured (need bucket, access key, secret)"}
    conf = _s3_conf(c)
    prefix = "/".join(p for p in (c.prefix.strip("/"), subdir.strip("/")) if p)
    dest = f"{_S3_REMOTE}:{c.bucket}" + (f"/{prefix}" if prefix else "")
    links: list[str] = []
    try:
        for f in files:
            _rclone(conf, ["copy", str(f), dest], check=True, timeout=120)
            if c.make_share_link:
                key = (prefix + "/" if prefix else "") + f.name
                links.append(_s3_public_url(c, key))
        return {"ok": True, "links": links, "dest": dest}
    except subprocess.CalledProcessError as e:
        return {"ok": False, "error": (e.stderr or str(e)).strip()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        os.unlink(conf)


def s3_upload_one(f: Path, c: S3Destination, subdir: str = "") -> dict:
    """Upload a single file and return its public URL (for guest share links)."""
    res = s3_upload([f], c, subdir=subdir)
    if res.get("ok"):
        res["link"] = (res.get("links") or [None])[0]
    return res


# ---- FTP / FTPS -----------------------------------------------------------
def ftp_upload(files: list[Path], c: FTPDestination) -> dict:
    try:
        ftp: FTP = FTP_TLS() if c.use_tls else FTP()
        ftp.connect(c.host, c.port, timeout=30)
        ftp.login(c.username, c.password)
        if c.use_tls and isinstance(ftp, FTP_TLS):
            ftp.prot_p()
        ftp.set_pasv(c.passive)
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


# ---- dispatch (shared by the inline path and the background sync worker) ---
def enabled_dests(settings: Settings) -> list[str]:
    st = settings.storage
    return [d for d, on in (("gdrive", st.gdrive.enabled),
                            ("s3", st.s3.enabled),
                            ("ftp", st.ftp.enabled)) if on]


def dispatch(dest: str, files: list[Path], settings: Settings, subdir: str = "") -> dict:
    """Run one destination's upload. Single source of truth for the dest -> fn mapping.
    `subdir` (the session id) keeps remote names collision-free across sessions."""
    st = settings.storage
    if dest == "gdrive":
        return gdrive_upload(files, st.gdrive, subdir=subdir)
    if dest == "s3":
        return s3_upload(files, st.s3, subdir=subdir)
    if dest == "ftp":
        return ftp_upload(files, st.ftp)
    return {"ok": False, "error": f"unknown destination '{dest}'"}


def upload_all(files: list[Path], settings: Settings) -> dict:
    """Legacy inline path (used when background_sync is off)."""
    return {d: dispatch(d, files, settings) for d in enabled_dests(settings)}
