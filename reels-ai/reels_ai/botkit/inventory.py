"""/inventory and /delete: what you have, and removing what you don't want."""
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


class InventoryMixin:
    # ------------------------------------------------------------------ inventory
    STATUS_ICON = {"pending": "\u23f3", "approved": "\u2705", "skipped": "\u274c", "posted": "\U0001f4e4",
                   "deleted": "\U0001f5d1", "spare": "\U0001f4e6"}

    def drawing_label(self, did: str, d: dict) -> str:
        name = d.get("subject") or (d.get("drive_name") or did).rsplit(".", 1)[0]
        part = f" \u00b7 drawing {d['part']['label']}" if d.get("part") else ""
        return f"{name}{part}"

    def inventory_text(self) -> list[str]:
        """Every drawing and its Shorts with ids, newest first, in message-sized chunks."""
        vs = [v for v in self.state["variants"].values() if v["status"] != "replaced"]

        def state_of(v: dict) -> str:
            return "posted" if v.get("dropped_at") or v["status"] == "posted" else v["status"]

        count = {s: sum(1 for v in vs if state_of(v) == s) for s in ("approved", "pending", "posted", "skipped")}
        by_drawing: dict[str, list] = {}
        for vid, v in self.state["variants"].items():
            if v["status"] not in ("replaced", "deleted"):
                by_drawing.setdefault(v["drawing"], []).append((vid, v))
        shown = [(did, d) for did, d in self.state["drawings"].items()
                 if d["status"] not in ("replaced", "deleted", "retired")]
        shown.sort(key=lambda x: (x[1].get("received") or "", int(x[0][1:]) if x[0][1:].isdigit() else 0),
                   reverse=True)
        bank = self.bank()
        head = [f"\U0001f4cb Inventory: {len(shown)} drawings",
                f"\u2705 {count['approved']} kept \u00b7 \u23f3 {count['pending']} waiting for you \u00b7 "
                f"\U0001f4e4 {count['posted']} dropped \u00b7 \u274c {count['skipped']} skipped",
                "Bank: " + ", ".join(f"{n} {'drawings' if p == 'drops' else p}" for p, n in bank.items()),
                "Delete with /delete and the ids, e.g. /delete v12 v13 (a Short) or /delete d4 (a whole drawing).",
                ""]
        lines = []
        for did, d in shown:
            items = by_drawing.get(did, [])
            if d["status"] in ("queued", "naming"):
                tail = " \u2014 being made" if d["status"] == "queued" else " \u2014 waiting for a name"
            elif d["status"] == "failed":
                tail = f" \u2014 failed: {(d.get('error') or '')[:60]}"
            elif d["status"] == "held":
                tail = f" \u2014 held by the reviewer: {(d.get('error') or '')[:60]}"
            elif not items:
                tail = " \u2014 no Shorts left"
            else:
                tail = ""
            lines.append(f"{did} {self.drawing_label(did, d)}{tail}")
            for vid, v in sorted(items, key=lambda x: int(x[0][1:]) if x[0][1:].isdigit() else 0):
                icon = self.STATUS_ICON.get(state_of(v), "\u2022")
                label = STYLE_LABELS.get(v["style"], v["style"])
                lines.append(f"   {icon} {vid} {label} \u00b7 {v.get('seconds', 0):.0f}s")
        out, cur = [], "\n".join(head)
        for line in lines:
            if len(cur) + len(line) + 1 > 3500:
                out.append(cur)
                cur = ""
            cur += ("\n" if cur else "") + line
        out.append(cur)
        return out

    def delete_command(self, arg: str, undo: bool = False) -> None:
        """/delete v12 v13 d4: Shorts (v..) or whole drawings (d..) leave your bank and this chat.
        Already-dropped ones stay as history. Files in Drive are left alone. /undelete restores."""
        ids = [w.lower() for w in re.split(r"[\s,]+", arg) if w]
        valid = [i for i in ids if re.fullmatch(r"[vd]\d+", i)]
        if not valid:
            want = _norm_name(arg)
            found = [(did, d) for did, d in self.state["drawings"].items()
                     if want and (want in _norm_name(d.get("subject") or "") or want in _norm_name(d.get("drive_name") or ""))
                     and d["status"] not in ("replaced",)]
            if found:
                return self.say("Which ones? Send the ids:\n" + "\n".join(
                    f"{did} {self.drawing_label(did, d)}" for did, d in found[:30]) +
                    f"\ne.g. /{'undelete' if undo else 'delete'} {found[0][0]}  (see /inventory for the Shorts' ids)")
            return self.say("Send the ids from /inventory, e.g. /delete v12 v13 or /delete d4.")
        done, skipped, msgs = [], [], []
        for i in valid:
            if i.startswith("v"):
                v = self.state["variants"].get(i)
                if not v:
                    skipped.append(f"{i} (not found)")
                    continue
                if undo:
                    if v["status"] == "deleted":
                        v["status"] = v.pop("was", "approved")
                        done.append(i)
                    continue
                if v.get("dropped_at") or v["status"] in ("posted", "replaced", "deleted"):
                    skipped.append(f"{i} ({'already dropped' if v.get('dropped_at') else v['status']})")
                    continue
                v["was"], v["status"] = v["status"], "deleted"
                if v.get("message_id"):
                    msgs.append(v["message_id"])
                done.append(i)
            else:
                d = self.state["drawings"].get(i)
                if not d:
                    skipped.append(f"{i} (not found)")
                    continue
                if undo:
                    if d["status"] == "deleted":
                        d["status"] = d.pop("was", "rendered")
                        for vid, v in self.state["variants"].items():
                            if v["drawing"] == i and v["status"] == "deleted":
                                v["status"] = v.pop("was", "approved")
                        done.append(i)
                    continue
                d["was"], d["status"] = d["status"], "deleted"     # also stops it being made / remade
                for vid, v in self.state["variants"].items():
                    if v["drawing"] == i and not v.get("dropped_at") and v["status"] in ("pending", "approved", "skipped", "spare"):
                        v["was"], v["status"] = v["status"], "deleted"
                        if v.get("message_id"):
                            msgs.append(v["message_id"])
                done.append(i)
        cleared = 0
        if msgs:
            try:
                if self.bot.delete_messages(self.owner, msgs):
                    cleared = len(msgs)
            except Exception as e:  # noqa: BLE001 - older than 2 days: Telegram won't delete
                print(f"warn: delete messages: {e}")
            if not cleared:                                # too old to delete: mark them instead
                for mid in msgs:
                    try:
                        self.bot.edit_markup(self.owner, mid, label_markup("\U0001f5d1 Deleted"))
                    except Exception:  # noqa: BLE001
                        pass
        self.checkpoint()
        if undo:
            msg = (f"\u21a9\ufe0f Restored {', '.join(done)}." if done else "Nothing to restore there.")
        else:
            msg = (f"\U0001f5d1 Deleted {', '.join(done)}: out of your bank" +
                   (" and this chat" if cleared else "") + ". Files in Drive are untouched; /undelete "
                   f"{' '.join(done)} brings them back." if done else "Nothing deleted.")
        if skipped:
            msg += "\nSkipped: " + ", ".join(skipped)
        self.say(msg)
