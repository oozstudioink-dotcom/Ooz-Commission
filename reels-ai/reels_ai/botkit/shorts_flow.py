"""One Short for your channel, the rest for Facebook trial reels ([variants] flow = "primary").

Each drawing is made in your primary style (Rewind unless you pick another with /primary) plus
`per_drawing - 1` other styles. As soon as a drawing is done the bot sends:

  your channel Short   📤 Uploaded (you posted it: filed to Completed Shorts › Uploaded)
                       🔁 Redo     (not right: made again in the same style, a new take)
  each trial reel      ✅ Keep     (stays in Trial Reel Shorts for Facebook)
                       ❌ Discard  (to Delete Me)

Nothing waits for a drop time. /styles shows every effect with its name and how it does;
/primary <style> makes another one your channel Short from the next drawing on.
"""
from __future__ import annotations

from .common import STYLE_LABELS, label_markup, _norm_name
from ..state import iso

COMPLETED = "Completed Shorts"
TRIALS = "Trial Reel Shorts"


def primary_markup(vid: str) -> dict:
    return {"inline_keyboard": [[{"text": "\U0001f4e4 Uploaded", "callback_data": f"pu:{vid}"},
                                 {"text": "\U0001f501 Redo", "callback_data": f"pr:{vid}"}]]}


def trial_markup(vid: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Keep for trial reels", "callback_data": f"tk:{vid}"},
                                 {"text": "❌ Discard", "callback_data": f"td:{vid}"}]]}


class ShortsFlowMixin:
    # ------------------------------------------------------------------ settings
    @property
    def flow_on(self) -> bool:
        return self.cfg.get("variants", {}).get("flow") == "primary"

    def primary_style(self) -> str:
        st = self.state.get("primary_style") or self.cfg.get("variants", {}).get("primary", "rewind")
        return st if st in STYLE_LABELS else "rewind"

    def role(self, style: str) -> str:
        return "primary" if style == self.primary_style() else "trial"

    def shorts_folder(self, style: str, name: str) -> str:
        return f"{COMPLETED if self.role(style) == 'primary' else TRIALS}/{name}"

    # ------------------------------------------------------------------ delivery
    def send_flow(self, did: str, d: dict, out_dir, results: list[dict], links: dict) -> None:
        """Right after rendering: the channel Short first, then each trial reel, each with its buttons."""
        from ..state import new_id
        name = d.get("subject") or d.get("drive_name") or did
        ordered = sorted(results, key=lambda r: self.role(r["name"]) != "primary")
        for r in ordered:
            vid = new_id(self.state, "v")
            primary = self.role(r["name"]) == "primary"
            head = (f"\U0001f3ac {name} · your channel Short ({STYLE_LABELS[r['name']]}) · {r['seconds']:.0f}s"
                    if primary else
                    f"\U0001f9ea {name} · trial reel: {STYLE_LABELS[r['name']]} · {r['seconds']:.0f}s")
            tail = ("Posted it? Tap \U0001f4e4 Uploaded. Not right? \U0001f501 Redo makes a new take."
                    if primary else "For Facebook trial reels: ✅ Keep or ❌ Discard.")
            text = f"{head} · {vid}\nHook: “{r['hook']}”\n\n{r['caption']}\n\n{tail}"
            if r.get("look"):
                text += f"\n{r['look']}"
            if links.get(r["name"]):
                text += f"\n\U0001f39e Full quality: {links[r['name']]}"
            sent = self.bot.send_video(self.owner, out_dir / r.get("tg_file", r["file"]), caption=text[:1024],
                                       reply_markup=primary_markup(vid) if primary else trial_markup(vid),
                                       width=720, height=1280, duration=round(r["seconds"]))
            self.state["variants"][vid] = {
                "drawing": did, "style": r["name"], "hook": r["hook"], "caption": r["caption"],
                "seconds": round(r["seconds"], 1), "file_id": sent["video"]["file_id"],
                "message_id": sent["message_id"], "created": iso(self.clock()), "deadline": None,
                "status": "pending", "role": "primary" if primary else "trial", "posts": {},
                **({"full_link": links[r["name"]]} if links.get(r["name"]) else {})}
            uses = self.state["style_uses"]
            uses[r["name"]] = uses.get(r["name"], 0) + 1

    def on_flow(self, action: str, vid: str, q: dict) -> None:
        v = self.state["variants"].get(vid)
        mid = q.get("message", {}).get("message_id")
        if not v:
            return self.bot.answer_callback(q["id"], "That video is gone")
        if v["status"] != "pending":
            return self.bot.answer_callback(q["id"], f"Already {v['status']}")
        name = self.folder_name(v)
        if action == "pu":                                   # posted to your channel
            v.update(status="posted", used_at=iso(self.clock()), dropped_at=iso(self.clock()),
                     decided_by="you", decided_at=iso(self.clock()))
            moved = self.file_to(v, f"{COMPLETED}/Uploaded/{name}")
            self.bot.answer_callback(q["id"], "Marked as uploaded")
            self.bot.edit_markup(self.owner, mid, label_markup(
                "\U0001f4e4 Uploaded" + (" · in Completed Shorts › Uploaded" if moved else "")))
            self.feedback(v["style"], True)
            return self.notion_short(v)
        if action == "pr":                                   # a new take of the channel Short
            d = self.state["drawings"].get(v["drawing"])
            if not d:
                return self.bot.answer_callback(q["id"], "That drawing is gone")
            v["status"] = "replaced"
            self.file_to(v, f"Delete Me/{name}")
            d.update(status="queued", redo=True, keep_in_place=True, redo_styles=[v["style"]],
                     redo_count=d.get("redo_count", 0) + 1)
            d.pop("error", None)
            self.bot.answer_callback(q["id"], "Making a new take")
            return self.bot.edit_markup(self.owner, mid, label_markup("\U0001f501 Re-making this one"))
        if action == "tk":                                   # kept for Facebook trial reels
            v.update(status="trial", decided_by="you", decided_at=iso(self.clock()))
            self.bot.answer_callback(q["id"], "Kept for trial reels")
            self.feedback(v["style"], True)
            return self.bot.edit_markup(self.owner, mid, label_markup("✅ Kept · Trial Reel Shorts"))
        if action == "td":
            self.discard(v)
            self.feedback(v["style"], False)
            self.bot.answer_callback(q["id"], "Moved to Delete Me")
            return self.bot.edit_markup(self.owner, mid, label_markup("❌ Discarded · in Delete Me"))
        return self.bot.answer_callback(q["id"])

    # ------------------------------------------------------------------ /styles and /primary
    def styles_text(self) -> str:
        primary = self.primary_style()
        scores = self.state.get("style_scores", {})
        fb = self.state.get("style_feedback", {})
        vs = list(self.state["variants"].values())
        lines = []
        for st in [s for s in self.cfg["variants"]["styles"] if s in STYLE_LABELS]:
            kept = sum(1 for v in vs if v["style"] == st and v["status"] in ("trial", "posted"))
            dropped = sum(1 for v in vs if v["style"] == st and v["status"] == "discarded")
            sc = scores.get(st, {})
            views = f", {sc['avg_views']:.0f} avg views ({sc['posts']} posts)" if sc.get("posts") else ""
            f = fb.get(st, {})
            liked = f", you liked {f.get('liked', 0)}/{f.get('liked', 0) + f.get('disliked', 0)}" if f else ""
            star = "⭐ " if st == primary else "• "
            lines.append(f"{star}{STYLE_LABELS[st]} ({st}): kept {kept}, discarded {dropped}{views}{liked}")
        return ("\U0001f3a8 Your Short styles (⭐ = your channel Short):\n" + "\n".join(lines)
                + "\n\nTrial reels show their style name in the message and the Drive file name. "
                  "To make another style your channel Short: /primary <name>, e.g. /primary one take")

    def set_primary(self, arg: str) -> None:
        want = _norm_name(arg)
        hit = next((s for s in STYLE_LABELS if want and want in (_norm_name(s), _norm_name(STYLE_LABELS[s]))), None)
        if not hit or hit == "page_compilation":
            return self.say("Which style? " + ", ".join(STYLE_LABELS[s] for s in self.cfg["variants"]["styles"]
                                                         if s in STYLE_LABELS) + ". E.g. /primary rewind")
        self.state["primary_style"] = hit
        self.say(f"⭐ From the next drawing on, your channel Short is {STYLE_LABELS[hit]}. "
                 "The other styles become the trial reels.")
