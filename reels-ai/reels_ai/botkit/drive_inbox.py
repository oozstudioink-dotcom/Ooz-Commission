"""Recordings coming in: the Drive inbox, Telegram uploads, reading originals, saving results."""
from __future__ import annotations

import json  # noqa: F401
import os  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import tempfile  # noqa: F401
from datetime import datetime, timedelta  # noqa: F401
from pathlib import Path  # noqa: F401
from zoneinfo import ZoneInfo  # noqa: F401

from ..state import iso, load_config, load_state, new_id, now, parse, save_state  # noqa: F401
from ..telegram import Bot, TelegramError  # noqa: F401
from .common import (  # noqa: F401
    STYLE_LABELS, PLATFORM_LABELS, MAX_BOT_DOWNLOAD, HELP, pending_markup, rating_markup,
    label_markup, _time_range, _mmss, _norm_name, strip_guessed_names, ALBUM_BYTES, album_chunks,
    dispatch_workflow, shrink_for_telegram,
)


class DriveMixin:
    # ------------------------------------------------------------------ Google Drive inbox
    def drive_problem(self, e: Exception) -> None:
        """Tell the owner about a Drive problem once per distinct message, not every run."""
        msg = str(e)[:200]
        print(f"warn: drive: {msg}")
        if self.state.get("drive_error") != msg:
            self.state["drive_error"] = msg
            self.say(f"\u26a0\ufe0f Google Drive: {msg}")

    def check_drive_inbox(self) -> None:
        """New videos in Inbox, and (shorts_from_longform) in Longform project folders, become
        Shorts. Longform sessions stay where they are so the LongDraw can use them too."""
        if not self.drive:
            return
        from ..drive import describe_hints, parse_name
        sources = []                                    # (file, folder label, keep in place, folder name)
        try:
            for f in self.drive.list(self.drive.folder("Inbox"), "video"):
                sources.append((f, "Inbox", False, None))
            if self.cfg.get("drive", {}).get("shorts_from_longform", True):
                for proj in self.drive.list(self.drive.folder("Longform"), "folder"):
                    if proj["name"] == "Finished":
                        continue
                    for f in self.drive.list(proj["id"], "video"):
                        sources.append((f, f"Longform \u203a {proj['name']}", True, proj["name"]))
        except Exception as e:  # noqa: BLE001 - never let Drive break Telegram handling
            return self.drive_problem(e)
        self.state.pop("drive_error", None)
        seen = self.state.setdefault("drive_seen", [])
        self.follow_known(sources, seen, parse_name)
        new = [src for src in sources if src[0]["id"] not in seen]
        labels: dict[str, str] = {}
        for f, where, keep, project in new:
            meta = f.get("videoMediaMetadata") or {}
            did = new_id(self.state, "d")
            subject, hints = parse_name(f["name"])
            if project:                                  # one drawing of a LongDraw page
                from ..drive import part_number
                p_subject, p_hints = parse_name(project)
                subject = subject or p_subject           # '3 - Kakashi.MOV' names it, else the page does
                hints = {**p_hints, **hints}
                n = part_number(f["name"])
                if n is not None and n > 1:              # drawings 2.. start on a page that has ink
                    hints.setdefault("continued", True)
            self.state["drawings"][did] = {
                "source": "drive", "drive_id": f["id"], "drive_name": f["name"], "drive_where": where,
                "keep_in_place": keep, "subject": subject, "received": iso(self.clock()),
                "duration": (int(meta["durationMillis"]) / 1000) if meta.get("durationMillis") else None,
                "size_gb": round(int(f.get("size") or 0) / 1e9, 2), "status": "queued",
                **({"hints": {k: v for k, v in hints.items() if k != "unknown"}} if hints else {})}
            if not subject and self.naming_on():
                self.state["drawings"][did].update(status="naming", name_asked=iso(self.clock()),
                                                   created=f.get("createdTime"))
            seen.append(f["id"])
            how = describe_hints(hints)
            odd = f" (didn't understand: {', '.join(hints['unknown'])})" if hints.get("unknown") else ""
            labels[f["id"]] = (f"{f['name']} \u2192 {subject or 'name unknown'}"
                               f"{' \u00b7 ' + how if how else ''}{odd}")
        if new:
            by_place: dict[str, list[str]] = {}
            for f, where, _, _ in new:
                by_place.setdefault(where, []).append(labels[f["id"]])
            more = " \u2026"
            lines = [f"\u2022 {where}:\n   " + "\n   ".join(n[:8]) + (more if len(n) > 8 else "")
                     for where, n in by_place.items()]
            self.say(f"\U0001f4e5 Found {len(new)} new recording(s) in Drive:\n" + "\n".join(lines) +
                     "\nTurning them into 4K Shorts now (LongDraw sessions stay in their folder).")
            for did, d in self.state["drawings"].items():
                if d.get("status") == "naming" and not d.get("name_msg"):
                    self.ask_name(did, d)

    # ------------------------------------------------------------------ storage
    def storage_check(self) -> None:
        """Monthly: how much space raw recordings use, and Drive's overall usage. Never deletes."""
        if not self.drive:
            return
        month = self.local(self.clock()).strftime("%Y-%m")
        if self.state.get("storage_month") == month:
            return
        try:
            raw = sum(int(f.get("size") or 0) for f in self.drive.list(self.drive.folder("Inbox/Processed"), "video"))
            fin = 0
            for proj in self.drive.list(self.drive.folder("Longform/Finished"), "folder"):
                fin += sum(int(f.get("size") or 0) for f in self.drive.list(proj["id"], "video"))
            quota = self.drive.about().get("storageQuota", {})
        except Exception as e:  # noqa: BLE001
            print(f"warn: storage check: {e}")
            return
        self.state["storage_month"] = month
        warn_gb = float(self.cfg.get("drive", {}).get("storage_alert_gb", 50))
        if (raw + fin) / 1e9 < warn_gb:
            return
        used, limit = int(quota.get("usage") or 0), int(quota.get("limit") or 0)
        total = (f"\nDrive overall: {used / 1e12:.2f} TB of {limit / 1e12:.0f} TB used." if limit else "")
        self.say("\U0001f9f9 Monthly storage check\n"
                 f"\u2022 Inbox \u203a Processed (raw clips already made into Shorts): {raw / 1e9:.0f} GB\n"
                 f"\u2022 Longform \u203a Finished (sessions already in a LongDraw): {fin / 1e9:.0f} GB"
                 f"{total}\nThese are safe to delete when you're ready; the Shorts and LongDraws are in Done. "
                 "I never delete anything myself.")

    def follow_known(self, sources, seen, parse_name) -> None:
        """Recordings the bot already knows: a new name you give the file in Drive becomes its
        name here too, and one set aside for taking too long (before runs had no time limit)
        is tried again, once."""
        by_file: dict[str, list] = {}
        for did, d in self.state["drawings"].items():
            if d.get("drive_id"):
                by_file.setdefault(d["drive_id"], []).append((did, d))
        for f, where, _, _ in sources:
            if f["id"] not in seen:
                continue
            for did, d in by_file.get(f["id"], []):
                if (d.get("drive_name") != f["name"] and f["name"] not in d.get("old_names", [])
                        and d.get("status") not in ("replaced", "deleted")):
                    old = d.get("drive_name")
                    d["drive_name"] = f["name"]
                    named = parse_name(f["name"])[0]
                    if named and d.get("subject_by") != "you":
                        d["subject"] = named
                    print(f"renamed in Drive: {old} -> {f['name']}")
                if (d.get("status") == "failed" and str(d.get("error") or "").startswith("took longer")
                        and not d.get("retried_unlimited") and not d.get("part")):
                    d["retried_unlimited"] = True
                    self.requeue(did, d)
                    self.say(f"\U0001f501 Trying {d.get('subject') or d['drive_name']} again: it was set aside "
                             "for taking too long, and long recordings now have as long as they need.")

    def drive_source(self, d: dict, tmp: Path) -> Path:
        """Stream a raw Drive recording into a small timelapse working copy."""
        from ..drive import make_proxy
        dur = d.get("duration") or 30 * 60        # unknown -> assume long (keyframe mode)
        # one recording can hold several drawings: read it from Drive once per run, not once each
        kept = getattr(self, "_proxy", None)
        if kept is not None:
            if kept.get("id") == d["drive_id"] and kept["path"].exists():
                print(f"{d.get('drive_name')}: already read from Drive this run")
                return kept["path"]
            if kept.get("path"):
                kept["path"].unlink(missing_ok=True)
            tmp = kept["dir"]
        path = make_proxy(self.drive.media_url(d["drive_id"]), self.drive.token(),
                          tmp / f"{d['drive_id']}.mp4", dur, max_side=3840 if self.shorts_4k else 2560)
        if kept is not None:
            kept.update(id=d["drive_id"], path=path)
        return path

    def drive_raw(self, d: dict) -> dict:
        """How Process cuts read short real-speed moments straight from the original on Drive."""
        if not self.drive:
            return {}
        if not d.get("duration"):                 # Drive didn't say: ask the file itself
            try:
                from ..drive import remote_duration
                d["duration"] = remote_duration(self.drive.media_url(d["drive_id"]), self.drive.token())
            except Exception:  # noqa: BLE001 - unknown length: no raw reads, nothing worse
                d["duration"] = None
        if not d.get("duration"):
            return {}
        url = self.drive.media_url(d["drive_id"])
        # the token is asked for at each read: a long drawing can outlast one (they last an hour)
        return {"raw_duration": float(d["duration"]),
                "raw_input": lambda t0: ["-headers", f"Authorization: Bearer {self.drive.token()}\r\n",
                                         "-ss", f"{t0:.3f}", "-i", url]}

    @property
    def shorts_4k(self) -> bool:
        return str(self.cfg.get("shorts", {}).get("resolution", "4k")).lower() == "4k"

    def drive_after(self, did: str, d: dict, out_dir: Path, results: list[dict]) -> dict[str, str]:
        """Move the raw file to Inbox/Processed and save the full-quality variants to
        Done/Shorts. Returns {style: Drive link}."""
        links: dict[str, str] = {}
        try:
            if not d.get("keep_in_place"):          # LongDraw sessions stay in their project folder
                named = self.labelled_subject(d)
                if named and self.cfg.get("drive", {}).get("processed_to_longform", True):
                    # a named recording goes to its own LongDraw folder, ready for /longform <name>
                    self.drive.move(d["drive_id"], self.drive.folder(f"Longform/{named}"), self.drive.folder("Inbox"))
                    for x in self.state["drawings"].values():
                        if x.get("drive_id") == d["drive_id"]:
                            x.update(keep_in_place=True, drive_where=f"Longform \u203a {named}")
                    self.say(f"\U0001f4c1 Moved {d.get('drive_name')} to Drive \u203a Longform \u203a {named}. "
                             f"Send /longform {named} when you want its LongDraw.")
                else:
                    self.drive.move(d["drive_id"], self.drive.folder("Inbox/Processed"), self.drive.folder("Inbox"))
        except Exception as e:  # noqa: BLE001
            print(f"warn: couldn't move {d.get('drive_name')}: {e}")
        if not (self.cfg.get("drive", {}).get("backup_shorts", True) or self.shorts_4k):
            return links
        try:
            name = d.get("subject") or Path(d.get("drive_name") or did).stem
            folder = self.drive.folder(f"Done/Shorts/{name}")
            for r in results:
                tag = " (redo)" if d.get("redo") else ""
                up = self.drive.upload(out_dir / r["file"], folder,
                                       f"{name} - {STYLE_LABELS.get(r['name'], r['name'])}{tag}.mp4")
                if up.get("webViewLink"):
                    links[r["name"]] = up["webViewLink"]
        except Exception as e:  # noqa: BLE001
            print(f"warn: Drive backup failed: {e}")
        return links

    def labelled_subject(self, d: dict) -> str | None:
        """The character only YOU named: file name, page folder or Telegram caption. Never a guess
        (Gemini once called a Sasuke drawing 'Naruto' on several videos)."""
        if d.get("subject_by") == "you":                     # you named it in Telegram
            return d.get("subject")
        if d.get("source") != "drive":
            return d.get("subject") if d.get("subject_by") != "ai" else None
        from ..drive import parse_name
        subject = parse_name(d.get("drive_name") or "")[0]
        where = d.get("drive_where") or ""
        if not subject and "\u203a" in where:
            subject = parse_name(where.split("\u203a", 1)[1].strip())[0]
        return subject

    def intake(self, video: dict, subject: str) -> None:
        size = video.get("file_size") or 0
        if size > MAX_BOT_DOWNLOAD:
            self.say(f"⚠️ That file is {size / 1e6:.0f} MB. Bots can only download up to 20 MB.\n"
                     "Send it as a normal video (not as a file) so Telegram compresses it, or trim it.")
            return
        uid = video.get("file_unique_id")
        if uid and any(d.get("unique_id") == uid for d in self.state["drawings"].values()):
            self.say("\U0001f501 I already have that video, so I'm skipping the duplicate.")
            return
        from ..drive import describe_hints, parse_name
        did = new_id(self.state, "d")
        name_subject, hints = parse_name(subject) if subject else (None, {})
        hints.pop("unknown", None)
        self.state["drawings"][did] = {"subject": (name_subject or subject or "")[:60] or None,
                                       "file_id": video["file_id"], "unique_id": uid,
                                       "received": iso(self.clock()), "status": "queued",
                                       **({"hints": hints} if hints else {})}
        shown = self.state["drawings"][did]["subject"]
        how = describe_hints(hints)
        name = f" ({shown}{' \u00b7 ' + how if how else ''})" if shown or how else ""
        self.say(f"\U0001f3a8 Got it{name}! I'll make variants on the next run (within the hour).")
