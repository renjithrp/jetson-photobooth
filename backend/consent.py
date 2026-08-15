"""Guest consent + delivery dedup for post-event photo sharing.

Two opt-in channels, both deduped so a photo is never delivered twice — the hard
requirement for group photos, where several people can opt the same shot in:

  * WhatsApp (collect-only): a guest enters a phone number and gets a set of their
    photos queued against it. When back online the admin sends manually (a wa.me
    click-to-chat link with the photos' public URLs), then marks them sent. Each
    (phone, photo) is delivered at most once.

  * Google Drive (opt-in): default OFF — a photo uploads only if at least one guest
    opts it in. Each photo uploads at most once no matter how many guests opt it in;
    the actual upload is handed to the background sync worker.

State persists to data/consent.json (atomic write under an RLock), same pattern as
the sync queue and face index. Photo identity is the guest-facing '/captures/...'
URL, validated through share.resolve_capture so a guest can't inject arbitrary paths.
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import config


def normalize_phone(raw: str) -> str | None:
    """Reduce a phone number to digits (E.164-ish, no '+') for dedup and wa.me.
    Returns None if it clearly isn't a phone number (fewer than 7 digits)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    # a leading 00 international prefix is equivalent to '+'
    if digits.startswith("00"):
        digits = digits[2:]
    return digits if 7 <= len(digits) <= 15 else None


class ConsentStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.path = None
        self._loaded = False
        # phone(normalized) -> {"raw", "photos": [url], "sent": [url], "created"}
        self.whatsapp: dict[str, dict] = {}
        # photo url -> True once opted in; uploaded is the dedup ledger
        self.drive_wanted: list[str] = []
        self.drive_uploaded: list[str] = []

    # ---- persistence ------------------------------------------------------
    def _load(self) -> None:
        self.path = config.data_dir() / "consent.json"
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.whatsapp = d.get("whatsapp", {})
                self.drive_wanted = d.get("drive_optin", [])
                self.drive_uploaded = d.get("drive_uploaded", [])
            except Exception:
                pass
        self._loaded = True

    def _ensure(self) -> None:
        if not self._loaded:
            self._load()

    def _save(self) -> None:
        if not self.path:
            return
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "whatsapp": self.whatsapp,
            "drive_optin": self.drive_wanted,
            "drive_uploaded": self.drive_uploaded,
        }))
        tmp.replace(self.path)

    @staticmethod
    def _valid_photos(urls: list[str]) -> list[str]:
        """Keep only photo URLs that resolve inside the captures dir (dedup, ordered)."""
        from . import share
        out, seen = [], set()
        for u in urls or []:
            u = str(u)
            if u in seen:
                continue
            if share.resolve_capture(u) is not None:
                seen.add(u)
                out.append(u)
        return out

    # ---- WhatsApp (collect-only) -----------------------------------------
    def whatsapp_optin(self, raw_phone: str, photos: list[str]) -> dict:
        """Queue a guest's photos against their phone number. Merges with any prior
        opt-in from the same number and dedups the photo set — opting in twice adds
        nothing new."""
        phone = normalize_phone(raw_phone)
        if not phone:
            return {"ok": False, "error": "enter a valid phone number with country code"}
        valid = self._valid_photos(photos)
        if not valid:
            return {"ok": False, "error": "no photos selected"}
        with self._lock:
            self._ensure()
            rec = self.whatsapp.get(phone)
            if rec is None:
                rec = {"raw": str(raw_phone), "photos": [], "sent": [], "created": time.time()}
                self.whatsapp[phone] = rec
            added = [u for u in valid if u not in rec["photos"]]
            rec["photos"].extend(added)
            self._save()
        return {"ok": True, "phone": phone, "added": len(added),
                "total": len(self.whatsapp[phone]["photos"])}

    def whatsapp_pending(self) -> list[dict]:
        """Recipients with photos not yet marked sent — for the admin send console."""
        with self._lock:
            self._ensure()
            out = []
            for phone, rec in self.whatsapp.items():
                unsent = [u for u in rec["photos"] if u not in rec["sent"]]
                if unsent:
                    out.append({"phone": phone, "raw": rec.get("raw", phone),
                                "photos": unsent, "count": len(unsent),
                                "created": rec.get("created", 0)})
            out.sort(key=lambda r: r["created"])
            return out

    def whatsapp_mark_sent(self, raw_phone: str, photos: list[str] | None = None) -> dict:
        """Mark photos delivered to a number (dedup ledger). photos=None marks all
        currently-queued photos for that number as sent."""
        phone = normalize_phone(raw_phone)
        with self._lock:
            self._ensure()
            rec = self.whatsapp.get(phone or "")
            if not rec:
                return {"ok": False, "error": "unknown recipient"}
            targets = self._valid_photos(photos) if photos else list(rec["photos"])
            newly = [u for u in targets if u in rec["photos"] and u not in rec["sent"]]
            rec["sent"].extend(newly)
            self._save()
        return {"ok": True, "phone": phone, "marked": len(newly)}

    # ---- Google Drive (opt-in) -------------------------------------------
    def drive_optin(self, photos: list[str]) -> dict:
        """Opt a set of photos in for Drive upload. Returns the photos that are NEW
        to the opt-in set (not already opted in and not already uploaded) so the
        caller can enqueue exactly those — the guarantee that a group photo opted in
        by several guests is only ever uploaded once."""
        valid = self._valid_photos(photos)
        if not valid:
            return {"ok": False, "error": "no photos selected", "new": []}
        with self._lock:
            self._ensure()
            already = set(self.drive_wanted) | set(self.drive_uploaded)
            new = [u for u in valid if u not in already]
            self.drive_wanted.extend(new)
            self._save()
        return {"ok": True, "added": len(new), "new": new}

    def drive_mark_uploaded(self, photos: list[str]) -> None:
        """Record photos as uploaded (dedup ledger) so they're never re-sent."""
        with self._lock:
            self._ensure()
            up = set(self.drive_uploaded)
            for u in photos or []:
                if u not in up:
                    self.drive_uploaded.append(u)
                    up.add(u)
            self._save()

    def stats(self) -> dict:
        with self._lock:
            self._ensure()
            pending_photos = sum(len([u for u in r["photos"] if u not in r["sent"]])
                                 for r in self.whatsapp.values())
            return {
                "whatsapp_recipients": len(self.whatsapp),
                "whatsapp_pending_photos": pending_photos,
                "drive_optin": len(self.drive_wanted),
                "drive_uploaded": len(self.drive_uploaded),
            }


store = ConsentStore()
