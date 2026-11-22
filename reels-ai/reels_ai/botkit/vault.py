"""What you actually have: /vault counts it, /review goes through it once.

/vault    drawings uploaded, ready to upload (kept), still to review, discarded; trial reels kept;
          how many weeks of Shorts that is at 3 a week; long videos scheduled on YouTube
/review   one drawing at a time (its channel Short, or its best version from before), with
          ✅ Keep · ❌ Discard · 🔁 Make a Rewind version · ⏭ Later. Each answer brings the next one.
"""
from __future__ import annotations

from .common import STYLE_LABELS, label_markup

OPEN = ("approved", "pending")


def review_markup(did: str, can_remake: bool) -> dict:
    rows = [[{"text": "✅ Keep", "callback_data": f"rk:{did}"},
             {"text": "❌ Discard", "callback_data": f"rd:{did}"}]]
    second = [{"text": "⏭ Later", "callback_data": f"rl:{did}"}]
    if can_remake:
        second.insert(0, {"text": "\U0001f501 Make it Rewind", "callback_data": f"rr:{did}"})
    rows.append(second)
    return {"inline_keyboard": rows}


class VaultMixin:
    def _shorts_of(self, did: str) -> list[dict]:
        return [v for v in self.state["variants"].values() if v.get("drawing") == did]

    def drawing_state(self, did: str, d: dict) -> str:
        """uploaded / ready / review / discarded / other, for one drawing."""
        vs = self._shorts_of(did)
        if any(v["status"] == "posted" and v.get("used_at") for v in vs):
            return "uploaded"
        if d.get("review") == "discard":
            return "discarded"
        usable = [v for v in vs if v["status"] in OPEN]
        if not usable:
            return "discarded" if any(v["status"] == "discarded" for v in vs) else "other"
        if d.get("review") == "keep" or any(v.get("role") == "primary" for v in usable):
            return "ready"
        return "review"

    def vault_text(self) -> str:
        counts = {"uploaded": 0, "ready": 0, "review": 0, "discarded": 0}
        for did, d in self.state["drawings"].items():
            if d.get("status") in ("rendered", "held"):
                st = self.drawing_state(did, d)
                if st in counts:
                    counts[st] += 1
        trials = sum(1 for v in self.state["variants"].values() if v["status"] == "trial")
        ready = counts["ready"]
        weeks = ready / 3
        lines = ["\U0001f5c4 Your vault",
                 f"\U0001f4e4 Uploaded: {counts['uploaded']} drawing(s)",
                 f"✅ Ready to upload: {ready} ≈ {weeks:.1f} week(s) of Shorts at 3 a week",
                 f"\U0001f50d Still to review: {counts['review']}" + (" (send /review)" if counts["review"] else ""),
                 f"❌ Discarded: {counts['discarded']}",
                 f"\U0001f9ea Trial reels kept for Facebook: {trials}"]
        if counts["review"]:
            lines.append(f"If you keep all of the ones still to review: ≈ {(ready + counts['review']) / 3:.1f} weeks.")
        lines += self._scheduled_long()
        return "\n".join(lines)

    def _scheduled_long(self) -> list[str]:
        yt = self.clients.get("youtube")
        if not yt:
            return []
        try:
            from datetime import datetime
            now = self.clock()
            later = sorted(v.get("publish_at", "") for v in self._channel_videos(yt, 50)
                           if v.get("seconds", 0) > 90 and v.get("publish_at")
                           and datetime.fromisoformat(v["publish_at"].replace("Z", "+00:00")) > now)
        except Exception as e:  # noqa: BLE001
            print(f"warn: vault: {str(e)[:120]}")
            return []
        if not later:
            return ["\U0001f3ac Long videos scheduled on YouTube: none"]
        last = self.local(datetime.fromisoformat(later[-1].replace("Z", "+00:00"))).strftime("%a %b %-d")
        return [f"\U0001f3ac Long videos scheduled on YouTube: {len(later)} (the last one goes out {last})"]

    # ------------------------------------------------------------------ /review
    def review_queue(self) -> list[str]:
        later = set(self.state.get("review_later", []))
        todo = [did for did, d in self.state["drawings"].items()
                if d.get("status") in ("rendered", "held") and self.drawing_state(did, d) == "review"]
        return [d for d in todo if d not in later] + [d for d in todo if d in later]

    def review_next(self, first: bool = False) -> None:
        queue = self.review_queue()
        if not queue:
            self.state.pop("review_later", None)
            return self.say("✅ Nothing left to review. /vault shows what you have.")
        did = queue[0]
        d = self.state["drawings"][did]
        vs = [v for v in self._shorts_of(did) if v["status"] in OPEN and v.get("file_id")]
        primary = self.primary_style()
        best = next((v for v in vs if v["style"] == primary), None) or (vs[0] if vs else None)
        name = d.get("subject") or d.get("drive_name") or did
        styles = ", ".join(STYLE_LABELS.get(v["style"], v["style"]) for v in vs)
        text = (f"\U0001f50d Review {len(queue)} left · {name}\nVersions made: {styles or 'none'}\n\n"
                f"✅ Keep: it counts as ready to upload. ❌ Discard: every unposted version goes to Delete Me."
                + ("" if best and best["style"] == primary else
                   f"\n\U0001f501 Make it Rewind: makes your channel Short ({STYLE_LABELS[primary]}) from it."))
        markup = review_markup(did, not (best and best["style"] == primary))
        if best:
            self.bot.send_video(self.owner, best["file_id"], caption=text[:1024], reply_markup=markup)
        else:
            self.say(text, reply_markup=markup)

    def on_review(self, action: str, did: str, q: dict) -> None:
        d = self.state["drawings"].get(did)
        mid = q.get("message", {}).get("message_id")
        if not d:
            return self.bot.answer_callback(q["id"], "That drawing is gone")
        later = self.state.setdefault("review_later", [])
        if action == "rk":
            d["review"] = "keep"
            label = "✅ Kept: ready to upload"
        elif action == "rd":
            d["review"] = "discard"
            for v in self._shorts_of(did):
                if v["status"] in OPEN or v["status"] == "trial":
                    self.discard(v)
            label = "❌ Discarded: in Delete Me"
        elif action == "rr":
            d["review"] = "keep"
            self.requeue_primary(did, d)
            label = f"\U0001f501 Making its {STYLE_LABELS[self.primary_style()]} version"
        else:
            if did not in later:
                later.append(did)
            label = "⏭ Later"
        if action != "rl" and did in later:
            later.remove(did)
        self.bot.answer_callback(q["id"], label[:60])
        self.bot.edit_markup(self.owner, mid, label_markup(label))
        self.review_next()

    def requeue_primary(self, did: str, d: dict) -> None:
        """Make this drawing's channel Short (primary style); its older versions become trial reels."""
        for v in self._shorts_of(did):
            if v["status"] in OPEN:
                v.update(was=v["status"], status="trial", trial_from="review")
        d.update(status="queued", redo=True, keep_in_place=True, redo_styles=[self.primary_style()],
                 redo_count=d.get("redo_count", 0) + 1)
        d.pop("error", None)
