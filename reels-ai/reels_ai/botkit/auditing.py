"""The channel auditor and content calendar in Telegram (the logic is in reels_ai/audit.py)."""
from __future__ import annotations

from ..state import iso
from .common import STYLE_LABELS, label_markup


def rewrite_markup(key: str) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Apply on YouTube", "callback_data": f"ta:{key}"},
                                 {"text": "❌ Skip", "callback_data": f"ts:{key}"}]]}


class AuditMixin:
    def weekly_audit(self) -> None:
        """Once a week ([audit] weekday, after [audit] hour your time): the audit and the calendar."""
        ac = self.cfg.get("audit", {})
        lt = self.local(self.clock())
        week = lt.strftime("%G-W%V")
        if (not ac.get("enabled", True) or "youtube" not in self.clients or lt.weekday() != int(ac.get("weekday", 6))
                or lt.hour < int(ac.get("hour", 11)) or self.state.get("audit_week") == week):
            return
        self.state["audit_week"] = week
        self.send_audit(quiet=True)
        self.send_calendar()

    def _channel_videos(self, yt, limit: int) -> list[dict]:
        ch = self.state.setdefault("yt_channel", {})
        if not ch.get("uploads"):
            me = yt.whoami()
            ch.update(id=me["id"], uploads=me["uploads"])
        ids = [v["id"] for v in yt.recent_videos(ch["uploads"], limit)]
        return yt.videos(ids)

    def _model(self) -> str:
        from .. import eyes
        return self.cfg.get("watch", {}).get("model", eyes.MODEL)

    def _claude_ok(self) -> bool:
        from .. import eyes
        return eyes.available() and not self.claude_over_budget()

    def route(self, job: str) -> dict | None:
        """Which model does a routine job: [ai] gemini_first lists the jobs Gemini's free tier
        tries first (Claude only if it fails). None when no model can be asked at all."""
        import os
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        first = "gemini" if key and job in self.cfg.get("ai", {}).get("gemini_first", []) else "claude"
        claude = self._claude_ok()
        if not claude and not key:
            return None
        return {"first": first, "gemini_key": key, "claude": claude}

    def send_audit(self, quiet: bool = False) -> None:
        from .. import audit
        from ..watch import cost
        yt = self.clients.get("youtube")
        if not yt:
            return None if quiet else self.say("\U0001f50e The audit needs the YouTube connection.")
        ac = self.cfg.get("audit", {})
        try:
            measured = audit.measure(self._channel_videos(yt, int(ac.get("videos", 20))), self.clock())
        except Exception as e:  # noqa: BLE001
            print(f"warn: audit: {str(e)[:200]}")
            return None if quiet else self.say(f"\U0001f50e Couldn't read your channel: {str(e)[:150]}")
        weak = audit.weakest(measured, int(ac.get("rewrites", 3)))
        words = None
        if self._claude_ok():
            try:
                words = audit.propose(measured, weak, self.state.get("radar", {}).get("trending", []), self._model())
                print(f"audit: {cost(words) * 100:.1f} cents, ${self.spend(cost(words)):.2f} this month")
            except Exception as e:  # noqa: BLE001 - the measured audit still goes out
                print(f"warn: audit words: {str(e)[:150]}")
        self.state["audit_best"] = [{"title": v["title"], "pace": v["pace"]} for v in
                                    sorted((v for v in measured if v["verdict"] == "strong"),
                                           key=lambda v: -v["pace"])[:3]]
        self.say(audit.report(measured, words))
        for r in (words or {}).get("rewrites", []):
            k = r.get("video")
            if not isinstance(k, int) or k not in weak:
                continue
            v, new = measured[k], audit.safe_rewrite(measured[k], r)
            if not new:
                continue
            key = v["id"]
            self.state.setdefault("rewrites", {})[key] = {
                **new, "category": v["category"], "at": iso(self.clock()), "status": "proposed",
                "old": {"title": v["title"], "description": v["description"], "tags": v["tags"]}}
            self.say(f"✏️ Proposed rewrite · youtu.be/{key}\n\nNow: {v['title']}\nNew: {new['title']}\n\n"
                     f"Description:\n{new['description'][:600]}\n\nTags: {', '.join(new['tags'])}\n\nWhy: {new['why']}\n\n"
                     "Nothing changes on YouTube unless you tap Apply. /undo_rewrite puts the old one back.",
                     reply_markup=rewrite_markup(key))

    def on_rewrite(self, action: str, key: str, q: dict) -> None:
        r = self.state.get("rewrites", {}).get(key)
        mid = q.get("message", {}).get("message_id")
        if not r or r["status"] != "proposed":
            return self.bot.answer_callback(q["id"], "Already handled")
        if action == "ts":
            r["status"] = "skipped"
            self.bot.answer_callback(q["id"], "Skipped")
            return self.bot.edit_markup(self.owner, mid, label_markup("❌ Skipped: nothing changed"))
        try:
            self.clients["youtube"].update_video(key, r["title"], r["description"], r["tags"], r["category"])
        except Exception as e:  # noqa: BLE001
            self.bot.answer_callback(q["id"], "Couldn't change it")
            return self.say(f"⚠️ YouTube refused the change: {str(e)[:200]}\nIf it says "
                            "“insufficient permission”, the Google sign-in needs the permission to "
                            "manage videos (not only upload); tell Claude and it will walk you through it.")
        r["status"] = "applied"
        self.state["last_rewrite"] = key
        self.bot.answer_callback(q["id"], "Changed on YouTube")
        self.bot.edit_markup(self.owner, mid, label_markup("✅ Applied on YouTube"))

    def undo_rewrite(self) -> None:
        key = self.state.get("last_rewrite")
        r = self.state.get("rewrites", {}).get(key or "")
        if not r or r["status"] != "applied":
            return self.say("There is no applied rewrite to undo.")
        old = r["old"]
        try:
            self.clients["youtube"].update_video(key, old["title"], old["description"], old["tags"], r["category"])
        except Exception as e:  # noqa: BLE001
            return self.say(f"⚠️ Couldn't put it back: {str(e)[:200]}")
        r["status"] = "undone"
        self.say(f"↩️ Put back the old title, description and tags on youtu.be/{key}.")

    def send_calendar(self) -> None:
        from .. import audit
        from ..watch import cost
        trending = self.state.get("radar", {}).get("trending", [])
        requests = list(self.state.get("requests", {}).values())
        styles = [s for s in self.cfg["variants"]["styles"] if s in STYLE_LABELS]
        days = audit.plan_days(self.local(self.clock()))
        plan = None
        route = self.route("calendar")
        if (trending or requests) and route:
            try:
                got = audit.plan(days, trending, requests, self.state.get("audit_best", []), styles, self._model(),
                                 route=route)
                print(f"calendar by {got.get('by')}: {cost(got) * 100:.1f} cents, ${self.spend(cost(got)):.2f} this month")
                known = {x["name"].lower() for x in trending + requests}
                plan = [d for d in got.get("days", []) if d.get("character", "").lower() in known][:7]
            except Exception as e:  # noqa: BLE001 - the simple plan goes out instead
                print(f"warn: calendar: {str(e)[:150]}")
        if not plan:
            plan = audit.simple_plan(days, trending, requests, styles)
        self.state["calendar"] = {"at": iso(self.clock()), "days": plan}
        times = sorted(set(self.cfg.get("delivery", {}).get("drop_times") or []))
        self.say(audit.calendar_text(plan, [str(t) for t in times], STYLE_LABELS))
