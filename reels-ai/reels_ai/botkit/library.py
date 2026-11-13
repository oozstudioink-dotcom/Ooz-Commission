"""Your video library: look at every finished Short, and tell the bot which ones you used.

/videos            every Short that is ready, grouped by drawing, each with Used / Decline buttons
/videos <name>     only that drawing's
/videos trials     the versions set aside for Facebook trial reels

Used      the video is marked as posted and its 4K file moves to Drive > Done > Uploaded > <name>.
          The drawing's other versions become Facebook trial variants and move to
          Done > Facebook trials > <name>, so a drawing is never posted twice as the main video.
Decline   the video leaves your library (its file stays in Done > Shorts).
Undo      puts a Used tap back as it was.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..state import iso
from .common import STYLE_LABELS, _norm_name, label_markup

READY = ("approved", "pending")


def library_markup(vid: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Used", "callback_data": f"lu:{vid}"},
                                 {"text": "❌ Decline", "callback_data": f"ld:{vid}"}]]}


def undo_markup(vid: str, text: str) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": "noop:0"}],
                                [{"text": "↩️ Undo", "callback_data": f"lx:{vid}"}]]}


class LibraryMixin:
    def drawing_name(self, did: str) -> str:
        d = self.state["drawings"].get(did, {})
        name = d.get("subject") or Path(d.get("drive_name") or did).stem
        return name + (f" ({d['part']['label']})" if d.get("part") else "")

    def library(self, want: str = "") -> dict[str, list[str]]:
        """Drawings with Shorts you haven't used or declined: {drawing id: [variant ids]}."""
        used = {v["drawing"] for v in self.state["variants"].values() if v.get("dropped_at")}
        out: dict[str, list[str]] = {}
        for vid, v in self.state["variants"].items():
            if (v["status"] in READY and not v.get("dropped_at") and v["drawing"] not in used
                    and not self.retired_variant(v) and v.get("file_id")
                    and (not want or want in _norm_name(self.drawing_name(v["drawing"])))):
                out.setdefault(v["drawing"], []).append(vid)
        return out

    def send_library(self, arg: str = "") -> None:
        arg = arg.strip()
        if arg.lower() in ("trials", "trial", "facebook"):
            return self.send_trials()
        lib = self.library(_norm_name(arg))
        if not lib:
            return self.say(f"\U0001f39e No ready Shorts match “{arg}”." if arg else
                            "\U0001f39e No Shorts are waiting. Drop a recording in Drive › Inbox.")
        total = sum(len(v) for v in lib.values())
        self.say(f"\U0001f39e Your library: {total} Shorts from {len(lib)} drawing(s).\nTap ✅ Used on the one you "
                 "post (its other versions become Facebook trial variants) or ❌ Decline to remove one.")
        for did, vids in lib.items():
            for k, vid in enumerate(sorted(vids), 1):
                v = self.state["variants"][vid]
                self.send_one(vid, f"{self.drawing_name(did)} · {k} of {len(vids)} · "
                                   f"{STYLE_LABELS.get(v['style'], v['style'])} · {v.get('seconds', '?')}s · {vid}"
                              + (f"\n\U0001f39e 4K: {v['full_link']}" if v.get("full_link") else ""),
                              library_markup(vid))
        first = self.state["variants"][sorted(next(iter(lib.values())))[0]]
        if len(lib) == 1 and first.get("caption"):
            self.say(first["caption"])

    def send_trials(self) -> None:
        trials = [(vid, v) for vid, v in self.state["variants"].items() if v["status"] == "trial" and v.get("file_id")]
        if not trials:
            return self.say("\U0001f9ea No Facebook trial variants yet. They appear when you mark a video ✅ Used.")
        self.say(f"\U0001f9ea {len(trials)} trial variant(s): versions of drawings you already posted, for Facebook "
                 "trial reels. Files are in Drive › Done › Facebook trials.")
        for vid, v in trials:
            self.send_one(vid, f"Trial · {self.drawing_name(v['drawing'])} · "
                               f"{STYLE_LABELS.get(v['style'], v['style'])} · {vid}"
                          + (f"\n\U0001f39e 4K: {v['full_link']}" if v.get("full_link") else ""), library_markup(vid))

    def send_one(self, vid: str, caption: str, markup: dict) -> None:
        try:
            self.bot.send_video(self.owner, self.state["variants"][vid]["file_id"], caption=caption,
                                reply_markup=markup)
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"warn: library {vid}: {str(e)[:150]}")

    def file_to(self, v: dict, folder: str) -> bool:
        """Move a Short's 4K file in Drive. False when there is no file or Drive refuses."""
        m = re.search(r"/d/([\w-]+)", v.get("full_link") or "")
        if not (m and self.drive):
            return False
        try:
            self.drive.move_to(m.group(1), self.drive.folder(folder))
            return True
        except Exception as e:  # noqa: BLE001 - the mark still counts
            print(f"warn: couldn't move the file to {folder}: {str(e)[:150]}")
            return False

    def on_library(self, action: str, vid: str, q: dict) -> None:
        v = self.state["variants"].get(vid)
        mid = q.get("message", {}).get("message_id")
        if not v:
            return self.bot.answer_callback(q["id"], "That video is gone")
        name = self.state["drawings"].get(v["drawing"], {}).get("subject") or \
            Path(self.state["drawings"].get(v["drawing"], {}).get("drive_name") or v["drawing"]).stem
        if action == "ld":
            if v["status"] not in READY + ("trial",):
                return self.bot.answer_callback(q["id"], f"Already {v['status']}")
            v["was"], v["status"] = v["status"], "skipped"
            self.bot.answer_callback(q["id"], "Declined")
            return self.bot.edit_markup(self.owner, mid, label_markup("❌ Declined: out of your library"))
        if action == "lu":
            if v["status"] not in READY + ("trial",):
                return self.bot.answer_callback(q["id"], f"Already {v['status']}")
            was_trial = v["status"] == "trial"
            n = max((x.get("drop_no", 0) for x in self.state["variants"].values()), default=0) + 1
            v.update(was=v["status"], status="posted", used_at=iso(self.clock()), dropped_at=iso(self.clock()), drop_no=n)
            moved = self.file_to(v, f"Done/Uploaded/{name}")
            sibs = [] if was_trial else [x for x in self.state["variants"].values()
                                         if x["drawing"] == v["drawing"] and x is not v and x["status"] in READY]
            for x in sibs:
                x["was"], x["status"] = x["status"], "trial"
                self.file_to(x, f"Done/Facebook trials/{name}")
            v["made_trials"] = len(sibs)
            self.bot.answer_callback(q["id"], "Marked as used")
            self.bot.edit_markup(self.owner, mid, undo_markup(vid, "✅ Used" + (" · file in Done › Uploaded" if moved else "")))
            if sibs:
                self.say(f"✅ {name}: marked as used. Its {len(sibs)} other version(s) are now Facebook trial "
                         "variants (Drive › Done › Facebook trials). /videos trials shows them.")
            return None
        if action == "lx":
            if v["status"] != "posted" or not v.get("used_at"):
                return self.bot.answer_callback(q["id"], "Nothing to undo")
            v["status"] = v.pop("was", "approved")
            for key in ("used_at", "dropped_at", "drop_no", "made_trials"):
                v.pop(key, None)
            self.file_to(v, f"Done/Shorts/{name}")
            for x in self.state["variants"].values():
                if x["drawing"] == v["drawing"] and x["status"] == "trial":
                    x["status"] = x.pop("was", "approved")
                    self.file_to(x, f"Done/Shorts/{name}")
            self.bot.answer_callback(q["id"], "Put back")
            return self.bot.edit_markup(self.owner, mid, library_markup(vid))
