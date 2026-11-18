"""'About this drawing' in Telegram: known names are looked up; for anything else (an ink study,
your own character) the bot asks what you're drawing, rewords your answer, and remembers it.

Asked from /longform (the build starts once you answer or skip) and /fixdescriptions (that
video's description is updated once you answer)."""
from __future__ import annotations

from ..state import iso, new_id


def skip_markup(token: str) -> dict:
    return {"inline_keyboard": [[{"text": "⏭ Skip (just use the name)", "callback_data": f"abs:{token}"}]]}


class AboutMixin:
    def _notes(self) -> dict:
        return self.state.setdefault("about_notes", {})

    def known_about(self, *names: str) -> dict | None:
        from ..about import key
        for n in names:
            if n and key(n) in self._notes():
                return self._notes()[key(n)]
        return None

    def remember_about(self, names, info: dict) -> None:
        from ..about import key
        for n in names:
            if n and key(n):
                self._notes()[key(n)] = info

    def about_or_ask(self, name: str, then: dict, also=()) -> dict | None:
        """{subject, series, note} when known (remembered or looked up); {} when no writer is set up;
        None when the bot has asked you: `then` runs after you answer or skip."""
        from ..about import lookup
        names = [name, *also]
        got = self.known_about(*names)
        if got is not None:
            return got
        how = self.route("about")
        if how is None:
            return {}
        info = lookup(name, self._model(), how)
        if info:
            self.remember_about(names, info)
            return info
        token = new_id(self.state, "q")
        m = self.say(f"✍️ What are you drawing in “{name}”? I couldn't tell from the name, and it goes in the "
                     "video's description (About this drawing). Reply to this message with a few words, e.g. "
                     "“an ink study of hands from a Berserk panel” or “my own samurai character”, and I'll "
                     "polish it.", reply_markup=skip_markup(token))
        self.state.setdefault("about_asks", {})[token] = {
            "name": name, "also": list(also), "then": then, "at": iso(self.clock()),
            "msg": (m or {}).get("message_id")}
        return None

    def about_ask_for(self, reply_to: str) -> str | None:
        return next((t for t, a in self.state.get("about_asks", {}).items() if str(a.get("msg")) == str(reply_to)), None)

    def on_about_reply(self, token: str, text: str) -> None:
        from ..about import polish
        ask = self.state.get("about_asks", {}).pop(token, None)
        if not ask:
            return
        info = polish(ask["name"], text, self._model(), self.route("about"))
        self.remember_about([ask["name"], *ask["also"]], info)
        self.say(f"✍️ Got it. About this drawing:\n{info['note']}")
        self.after_about(ask["then"], info)

    def on_about_skip(self, token: str, q: dict) -> None:
        from .common import label_markup
        ask = self.state.get("about_asks", {}).pop(token, None)
        if not ask:
            return self.bot.answer_callback(q["id"], "Already answered")
        info = {"subject": ask["name"], "series": "", "note": ""}
        self.remember_about([ask["name"], *ask["also"]], info)
        self.bot.answer_callback(q["id"], "Skipped")
        self.bot.edit_markup(self.owner, q.get("message", {}).get("message_id"), label_markup("⏭ Using just the name"))
        self.after_about(ask["then"], info)

    def after_about(self, then: dict, info: dict) -> None:
        if then.get("longform"):
            return self.start_longform(then["longform"])
        if then.get("video"):
            return self.refresh_one_longdraw(then["video"], then.get("label", ""), then.get("subject", ""), info)
