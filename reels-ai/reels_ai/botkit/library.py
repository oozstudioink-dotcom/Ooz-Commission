"""Your video library: look at every finished Short and sort it with three buttons.

/videos            every Short not yet sorted, grouped by drawing
/videos <name>     only that drawing's
/videos trials     the variants kept for Facebook trial reels
/unuse [name]      take back every Uploaded tap (or only that drawing's)
/discard <name>    send every Short of a drawing to Delete Me

Uploaded   you posted this one: its 4K file moves to Drive > Done > Uploaded > <name>.
Variant    a check mark: keep this version. Once a version of the same drawing is Uploaded, the
           checked ones become Facebook trial variants (Done > Facebook trials > <name>).
Discard    you don't want it: its file moves to Drive > Delete Me > <name>. Nothing is ever
           deleted by the bot; you empty that folder yourself.
Undo       under an Uploaded or Discarded video, puts it back.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..state import iso
from .common import STYLE_LABELS, _norm_name, label_markup

READY = ("approved", "pending")


def library_markup(vid: str, checked: bool = False) -> dict:
    return {"inline_keyboard": [[{"text": "\U0001f4e4 Uploaded", "callback_data": f"lu:{vid}"},
                                 {"text": "\u2705 Variant" if checked else "\u2611\ufe0f Variant",
                                  "callback_data": f"lc:{vid}"},
                                 {"text": "\u274c Discard", "callback_data": f"ld:{vid}"}]]}


def undo_markup(vid: str, text: str) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": "noop:0"}],
                                [{"text": "\u21a9\ufe0f Undo", "callback_data": f"lx:{vid}"}]]}


class LibraryMixin:
    def drawing_name(self, did: str) -> str:
        d = self.state["drawings"].get(did, {})
        name = d.get("subject") or Path(d.get("drive_name") or did).stem
        return name + (f" ({d['part']['label']})" if d.get("part") else "")

    def folder_name(self, v: dict) -> str:
        d = self.state["drawings"].get(v["drawing"], {})
        return d.get("subject") or Path(d.get("drive_name") or v["drawing"]).stem

    def uploaded(self, did: str) -> bool:
        """Has a version of this drawing been marked Uploaded?"""
        return any(x["drawing"] == did and x.get("used_at") for x in self.state["variants"].values())

    def library(self, want: str = "") -> dict[str, list[str]]:
        """Shorts you haven't sorted yet: {drawing id: [variant ids]}."""
        out: dict[str, list[str]] = {}
        for vid, v in self.state["variants"].items():
            if (v["status"] in READY and not v.get("dropped_at") and not self.retired_variant(v)
                    and v.get("file_id")
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
                              library_markup(vid, bool(v.get("checked"))))
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

    def to_trial(self, x: dict, by: str) -> None:
        x.update(was=x["status"], status="trial", trial_from=by)
        self.file_to(x, f"Done/Facebook trials/{self.folder_name(x)}")

    def put_back(self, v: dict) -> None:
        """Undo an Uploaded or Discard mark, and the trial variants an upload made."""
        vid = next(k for k, x in self.state["variants"].items() if x is v)
        was_upload = bool(v.get("used_at"))
        v["status"] = v.pop("was", "approved")
        for key in ("used_at", "dropped_at", "drop_no"):
            v.pop(key, None)
        self.file_to(v, f"Done/Shorts/{self.folder_name(v)}")
        if was_upload and not self.uploaded(v["drawing"]):
            for x in self.state["variants"].values():
                if x["drawing"] == v["drawing"] and x["status"] == "trial" and x.get("trial_from", vid) == vid:
                    x["status"] = x.pop("was", "approved")
                    x.pop("trial_from", None)
                    self.file_to(x, f"Done/Shorts/{self.folder_name(x)}")

    def discard(self, v: dict) -> None:
        v.update(was=v["status"] if v["status"] in READY else "approved", status="discarded")
        for key in ("used_at", "dropped_at", "drop_no", "checked", "trial_from"):
            v.pop(key, None)
        self.file_to(v, f"Delete Me/{self.folder_name(v)}")

    def on_library(self, action: str, vid: str, q: dict) -> None:
        v = self.state["variants"].get(vid)
        mid = q.get("message", {}).get("message_id")
        if not v:
            return self.bot.answer_callback(q["id"], "That video is gone")
        open_ = v["status"] in READY + ("trial",)
        if action == "lc":                                   # the check mark: keep as a variant
            if not open_:
                return self.bot.answer_callback(q["id"], f"Already {v['status']}")
            if v["status"] == "trial":                       # unchecking a trial variant
                v["status"] = v.pop("was", "approved")
                v.pop("trial_from", None)
                v["checked"] = False
                self.file_to(v, f"Done/Shorts/{self.folder_name(v)}")
            else:
                v["checked"] = not v.get("checked")
                if v["checked"] and self.uploaded(v["drawing"]):
                    self.to_trial(v, "")                     # its drawing is already posted
            on = bool(v.get("checked"))
            self.bot.answer_callback(q["id"], "Kept as a variant" if on else "Unchecked")
            return self.bot.edit_markup(self.owner, mid, library_markup(vid, on))
        if action == "ld":
            if not open_:
                return self.bot.answer_callback(q["id"], f"Already {v['status']}")
            self.discard(v)
            self.bot.answer_callback(q["id"], "Moved to Delete Me")
            return self.bot.edit_markup(self.owner, mid, undo_markup(vid, "\u274c Discarded \u00b7 in Delete Me"))
        if action == "lu":
            if not open_:
                return self.bot.answer_callback(q["id"], f"Already {v['status']}")
            n = max((x.get("drop_no", 0) for x in self.state["variants"].values()), default=0) + 1
            v.update(was=v["status"] if v["status"] in READY else "approved", status="posted",
                     used_at=iso(self.clock()), dropped_at=iso(self.clock()), drop_no=n)
            v.pop("trial_from", None)
            moved = self.file_to(v, f"Done/Uploaded/{self.folder_name(v)}")
            sibs = [x for x in self.state["variants"].values()
                    if x["drawing"] == v["drawing"] and x is not v and x["status"] in READY and x.get("checked")]
            for x in sibs:
                self.to_trial(x, vid)
            self.bot.answer_callback(q["id"], "Marked as uploaded")
            self.bot.edit_markup(self.owner, mid, undo_markup(
                vid, "\U0001f4e4 Uploaded" + (" \u00b7 file in Done \u203a Uploaded" if moved else "")))
            if sibs:
                self.say(f"\U0001f9ea {self.folder_name(v)}: {len(sibs)} checked version(s) are now Facebook trial "
                         "variants. /videos trials shows them.")
            return None
        if action == "lx":
            if not (v["status"] == "discarded" or (v["status"] == "posted" and v.get("used_at"))):
                return self.bot.answer_callback(q["id"], "Nothing to undo")
            self.put_back(v)
            self.bot.answer_callback(q["id"], "Put back")
            return self.bot.edit_markup(self.owner, mid, library_markup(vid, bool(v.get("checked"))))

    def unuse(self, arg: str = "") -> None:
        """/unuse [name]: take back Uploaded taps made by mistake."""
        want = _norm_name(arg)
        marked = [v for v in self.state["variants"].values() if v["status"] == "posted" and v.get("used_at")
                  and (not want or want in _norm_name(self.drawing_name(v["drawing"])))]
        if not marked:
            return self.say("No videos are marked Uploaded" + (f" for \u201c{arg}\u201d." if arg else "."))
        names = sorted({self.folder_name(v) for v in marked})
        for v in marked:
            self.put_back(v)
        self.say(f"\u21a9\ufe0f Took back {len(marked)} Uploaded mark(s): {', '.join(names)}. Those videos and their "
                 "trial variants are back in /videos, and their files are back in Done \u203a Shorts.")

    def discard_named(self, arg: str) -> None:
        """/discard <name>: every Short of the matching drawings goes to Delete Me."""
        want = _norm_name(arg)
        if not want:
            return self.say("Tell me which drawing: /discard Deku")
        live = READY + ("trial", "posted", "skipped", "spare")
        hit = [v for v in self.state["variants"].values() if v["status"] in live
               and want in _norm_name(self.drawing_name(v["drawing"]))]
        if not hit:
            return self.say(f"No Shorts match \u201c{arg}\u201d.")
        names = sorted({self.folder_name(v) for v in hit})
        for v in hit:
            self.discard(v)
        self.say(f"\u274c Discarded {len(hit)} Short(s) of {', '.join(names)}. Their files are in Drive \u203a Delete Me; "
                 "nothing is deleted until you empty that folder. Your recordings are untouched.")
