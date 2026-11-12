"""Asking what an unnamed recording is, and naming it from your reply."""
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


class NamingMixin:
    # ------------------------------------------------------------------ naming
    def naming_on(self) -> bool:
        return bool(self.cfg.get("naming", {}).get("ask", True))

    def ask_name(self, did: str, d: dict) -> None:
        """Ask what an unnamed recording (IMG_4412.MOV) is, with a frame from it to recognise it."""
        bits = [d.get("drive_name") or did]
        if d.get("duration"):
            bits.append(f"{d['duration'] / 60:.0f} min")
        if d.get("created"):
            try:
                when = datetime.fromisoformat(str(d["created"]).replace("Z", "+00:00"))
                bits.append("recorded " + self.local(when).strftime("%a %b %-d, %-I:%M %p"))
            except ValueError:
                pass
        hours = self.cfg.get("naming", {}).get("wait_hours", 12)
        text = ("\U0001f3f7 What's in this recording?\n" + " \u00b7 ".join(bits) +
                "\n\nReply to this message with a name, e.g. \u201cKakashi\u201d or "
                "\u201cKakashi - ink only\u201d. I'll rename the file in Drive and use it for the Shorts. "
                f"No answer in {hours} h and I make it without a name.")
        markup = {"inline_keyboard": [[{"text": "\u23ed Skip: no name", "callback_data": f"noname:{did}"}]]}
        sent = None
        if self.drive and hasattr(self.bot, "send_photo"):
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    pic = self.drive.thumbnail(d["drive_id"], Path(tmp) / "frame.jpg")
                    if pic:
                        sent = self.bot.send_photo(self.owner, pic, caption=text, reply_markup=markup)
            except Exception as e:  # noqa: BLE001 - a picture is nice to have
                print(f"warn: no preview for {d.get('drive_name')}: {e}")
        if sent is None:
            sent = self.bot.send_message(self.owner, text, reply_markup=markup)
        d["name_msg"] = sent.get("message_id")
        self.state.setdefault("name_asks", {})[str(d["name_msg"])] = did

    def name_command(self, text: str) -> None:
        """/name Kakashi - ink only: names the oldest recording waiting for a name."""
        waiting = [did for did, d in self.state["drawings"].items() if d.get("status") == "naming"]
        if not waiting:
            return self.say("\U0001f3f7 Nothing is waiting for a name. (To name a recording I asked about, "
                            "reply to my question.)")
        if not text:
            return self.say(f"\U0001f3f7 {len(waiting)} recording(s) waiting for a name: reply to my question "
                            "about each, or send /name <name> for the oldest.")
        self.name_drawing(waiting[0], text)

    def name_drawing(self, did: str, text: str) -> None:
        """Your name for a recording: used for its Shorts and as its file name in Drive."""
        from ..drive import parse_name
        d = self.state["drawings"].get(did)
        if not d:
            return self.say("That recording isn't in my list any more.")
        label = re.sub(r"[\\/:*?\"<>|]+", " ", text).strip()[:80]
        subject, hints = parse_name(label)
        if not subject:
            return self.say(f"\U0001f914 I couldn't find a name in \u201c{text}\u201d. Reply with e.g. "
                            "\u201cKakashi\u201d or \u201cKakashi - ink only\u201d.")
        old = d.get("drive_name") or ""
        ext = old[old.rfind("."):] if "." in old else ""
        new_name = f"{label}{ext}"
        renamed = ""
        if self.drive and d.get("drive_id"):
            try:
                self.drive.rename(d["drive_id"], new_name)
                d.setdefault("old_names", []).append(old)      # a stale Drive listing isn't a rename
                d["drive_name"] = new_name
                renamed = f" Renamed in Drive to \u201c{new_name}\u201d."
            except Exception as e:  # noqa: BLE001
                print(f"warn: rename {old}: {e}")
                renamed = " (Couldn't rename the file in Drive, but I'll use the name.)"
        d["subject"] = subject
        d["subject_by"] = "you"
        clean = {k: v for k, v in hints.items() if k != "unknown"}
        if clean:
            d["hints"] = {**d.get("hints", {}), **clean}
        from ..drive import describe_hints
        how = describe_hints(clean)
        if d.get("status") == "naming":
            d["status"] = "queued"
            nxt = " Making its Shorts on the next run."
        else:
            nxt = f" Send /redo {subject} to remake its Shorts with the new name."
        self.say(f"\U0001f3f7 {old or did} \u2192 {subject}{' (' + how + ')' if how else ''}.{renamed}{nxt}")

    def stop_waiting_for_names(self) -> None:
        """Recordings nobody named within [naming] wait_hours are made without a name."""
        hours = float(self.cfg.get("naming", {}).get("wait_hours", 12))
        for did, d in self.state["drawings"].items():
            if d.get("status") != "naming":
                continue
            try:
                asked = parse(d["name_asked"])
            except (KeyError, TypeError, ValueError):
                asked = None
            if asked is None or self.clock() - asked >= timedelta(hours=hours):
                d["status"] = "queued"
