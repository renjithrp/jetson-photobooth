"""Guest sharing helpers: thumbnails, multi-photo ZIP download, email (SMTP) and
public cloud links (for WhatsApp-style share).

All functions here are synchronous and blocking — call them via an executor from
the FastAPI handlers. Every public entry point validates that requested photos
resolve INSIDE the captures directory (guest-supplied paths).
"""
from __future__ import annotations

import io
import logging
import re
import smtplib
import subprocess
import tempfile
import zipfile
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from PIL import Image

from . import config, uploaders
from .models import Settings

log = logging.getLogger("booth.share")

THUMB_LONG_EDGE = 480          # px, long edge of gallery thumbnails
THUMB_QUALITY = 78
EMAIL_LONG_EDGE = 2000         # px, long edge of email attachments
EMAIL_QUALITY = 85
MAX_SHARE_PHOTOS = 50          # cap any single guest request

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---- path safety ------------------------------------------------------------
def resolve_capture(url_path: str) -> Path | None:
    """Map a guest-facing '/captures/<session>/<file>' URL to a real file, refusing
    anything that escapes the captures dir (or isn't an image)."""
    if not url_path.startswith("/captures/"):
        return None
    root = config.captures_dir().resolve()
    try:
        p = (root / url_path[len("/captures/"):]).resolve()
    except Exception:
        return None
    if not p.is_relative_to(root) or not p.is_file():
        return None
    if p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        return None
    return p


def resolve_photos(url_paths: list[str]) -> list[Path]:
    out, seen = [], set()
    for u in url_paths[:MAX_SHARE_PHOTOS]:
        p = resolve_capture(str(u))
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ---- thumbnails --------------------------------------------------------------
def thumbs_dir() -> Path:
    d = config.data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def remove_session_thumbs(session: str) -> None:
    """Delete the cached thumbnails for a pruned session (they mirror the captures
    layout under data/thumbs/<session>/)."""
    import shutil
    d = thumbs_dir() / session
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def thumb_for(url_path: str) -> Path | None:
    """Return (creating on first use) the cached thumbnail for a captures URL.
    Thumbs mirror the captures layout under data/thumbs/ and regenerate when the
    source is newer (e.g. an overlay was re-applied)."""
    src = resolve_capture(url_path)
    if src is None:
        return None
    root = config.captures_dir().resolve()
    rel = src.relative_to(root)
    dst = thumbs_dir() / rel.with_suffix(".jpg")
    try:
        if dst.is_file() and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
            tmp = dst.with_suffix(".tmp.jpg")
            im.save(tmp, "JPEG", quality=THUMB_QUALITY)
            tmp.replace(dst)
        return dst
    except Exception as e:
        log.warning("thumbnail failed for %s: %s", src, e)
        return src   # fall back to the original rather than a broken image


# ---- ZIP download --------------------------------------------------------------
def build_zip(files: list[Path]) -> Path:
    """Write the photos into a temporary ZIP (stored, not compressed — they're JPEGs)
    and return its path. Caller deletes it after the response is sent."""
    tmpdir = config.data_dir() / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fd = tempfile.NamedTemporaryFile(dir=str(tmpdir), suffix=".zip", delete=False)
    root = config.captures_dir().resolve()
    with zipfile.ZipFile(fd, "w", zipfile.ZIP_STORED) as z:
        for f in files:
            # session-prefixed name keeps multi-session downloads collision-free
            arcname = str(f.relative_to(root)).replace("/", "_")
            mtime = f.stat().st_mtime
            if mtime < 315532800:            # pre-1980: photos captured before the
                # Jetson's clock synced after boot carry a 1969 mtime, and zipfile
                # raises "ZIP does not support timestamps before 1980" on them.
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                z.writestr(info, f.read_bytes())
            else:
                z.write(f, arcname=arcname)
    fd.close()
    return Path(fd.name)


# ---- email ---------------------------------------------------------------------
def valid_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match(addr or ""))


def _attach_resized(msg: EmailMessage, f: Path, budget: int) -> int:
    """Attach a downsized JPEG; returns bytes used (0 = skipped, over budget)."""
    with Image.open(f) as im:
        im = im.convert("RGB")
        im.thumbnail((EMAIL_LONG_EDGE, EMAIL_LONG_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=EMAIL_QUALITY)
    data = buf.getvalue()
    if len(data) > budget:
        return 0
    msg.add_attachment(data, maintype="image", subtype="jpeg",
                       filename=f.with_suffix(".jpg").name)
    return len(data)


def send_email(to_addr: str, files: list[Path], settings: Settings) -> dict:
    e = settings.share.email
    if not e.enabled:
        return {"ok": False, "error": "email sending is not enabled on this booth"}
    if not (e.smtp_host and e.smtp_user and e.smtp_password):
        return {"ok": False, "error": "email is not configured (SMTP host/user/password)"}
    if not valid_email(to_addr):
        return {"ok": False, "error": "invalid email address"}
    if not files:
        return {"ok": False, "error": "no photos selected"}

    msg = EmailMessage()
    booth = settings.general.booth_name
    msg["Subject"] = (e.subject or "Your photos").replace("{booth_name}", booth)
    msg["From"] = formataddr((booth, e.from_addr or e.smtp_user))
    msg["To"] = to_addr
    msg.set_content(
        f"Hi!\n\nHere are your {len(files)} photo(s) from {booth}.\n\nEnjoy!")

    budget = max(1, e.max_attach_mb) * 1024 * 1024
    attached = 0
    for f in files:
        try:
            used = _attach_resized(msg, f, budget)
        except Exception as ex:
            log.warning("email attach failed for %s: %s", f, ex)
            continue
        if used:
            budget -= used
            attached += 1
    if not attached:
        return {"ok": False, "error": "photos too large to attach — try fewer"}

    try:
        if e.use_tls:
            with smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=30) as s:
                s.starttls()
                s.login(e.smtp_user, e.smtp_password)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(e.smtp_host, e.smtp_port, timeout=30) as s:
                s.login(e.smtp_user, e.smtp_password)
                s.send_message(msg)
    except Exception as ex:
        log.warning("email send to %s failed: %s", to_addr, ex)
        return {"ok": False, "error": f"send failed: {ex}"}
    log.info("emailed %d photo(s) to %s", attached, to_addr)
    return {"ok": True, "sent": attached, "skipped": len(files) - attached}


# ---- public links (WhatsApp share) ----------------------------------------------
def public_links(files: list[Path], settings: Settings) -> dict:
    """Best-effort public URLs for the photos so a guest can paste them into WhatsApp.
    Uses whichever cloud destination is enabled (S3 preferred: instant URLs). Files are
    uploaded under <prefix>/<session>/ so names never collide across sessions."""
    st = settings.storage
    root = config.captures_dir().resolve()
    if st.s3.enabled and st.s3.bucket and st.s3.access_key_id:
        links = []
        for f in files:
            rel = f.relative_to(root)                     # <session>/<file>
            res = uploaders.s3_upload_one(f, st.s3, subdir=str(rel.parent))
            if res.get("ok") and res.get("link"):
                links.append(res["link"])
        if links:
            return {"ok": True, "links": links, "via": "s3"}
        return {"ok": False, "error": "S3 upload failed — check the S3 settings"}
    if st.gdrive.enabled and st.gdrive.token:
        links = []
        for f in files:
            rel = f.relative_to(root)
            res = uploaders.gdrive_upload([f], st.gdrive, subdir=str(rel.parent))
            if res.get("ok") and res.get("links"):
                links.extend(res["links"])
        if links:
            return {"ok": True, "links": links, "via": "gdrive"}
        return {"ok": False, "error": "Google Drive upload failed"}
    return {"ok": False, "error": "no cloud destination enabled — ask staff to enable S3 or Google Drive"}


def rclone_available() -> bool:
    try:
        return subprocess.run(["rclone", "version"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False
