"""The Notion Content Pipeline from Telegram (the logic is in reels_ai/notion.py).

📤 Uploaded on a Short   that Short's row is marked Published
every few hours           LongDraws scheduled or published on YouTube update their rows
/notion                   is it connected, and the next empty slots
"""
from __future__ import annotations

from datetime import timedelta

from ..state import iso, parse

SETUP = ("Notion isn't connected yet. Three steps:\n"
         "1. notion.so/profile/integrations › New integration › name it Ooz Reels bot, type Internal › "
         "Save, then copy its Internal Integration Secret.\n"
         "2. GitHub › your repo › Settings › Secrets and variables › Actions › New repository secret: "
         "name NOTION_TOKEN, paste the secret there (never in this chat).\n"
         "3. In Notion open Content Pipeline › ••• (top right) › Connections › add Ooz Reels bot.\n"
         "Then send /notion again: I restart by myself within two hours, or right after the next merge.")


class NotionMixin:
    def avoid_notice(self) -> None:
        """Once per avoid list ([ai] avoid_words): Shorts already in the bank whose on-screen text or
        caption uses one of those words, with the /redo that re-makes them."""
        from ..hooks import avoid_words, avoided
        words = sorted(avoid_words())
        if self.state.get("avoid_notice") == words:
            return
        self.state["avoid_notice"] = words
        hit: dict[str, list[str]] = {}
        for v in self.state["variants"].values():
            if v.get("status") in ("approved", "pending", "trial") and not v.get("dropped_at") \
                    and (avoided(v.get("hook") or "") or avoided(v.get("caption") or "")):
                d = self.state["drawings"].get(v.get("drawing"), {})
                hit.setdefault(d.get("subject") or d.get("drive_name") or v.get("drawing", "?"), []).append(
                    v.get("hook") or v.get("style", ""))
        if hit:
            lines = [f"• {name}: {', '.join(repr(h) for h in hooks[:3])}" for name, hooks in sorted(hit.items())]
            self.say(f"\U0001f9f9 {sum(map(len, hit.values()))} Short(s) in your bank use a word you've banned "
                     f"({', '.join(words)}):\n" + "\n".join(lines) + "\n\nNew Shorts never will. To re-make these, "
                     "send /redo with the name, e.g. /redo " + sorted(hit)[0] + ". Shorts you already posted "
                     "aren't touched.")

    def _today(self):
        return self.local(self.clock()).date()

    def notion_short(self, v: dict) -> None:
        """After 📤 Uploaded: the Short's row in the pipeline."""
        if not getattr(self, "notion", None):
            return
        d = self.state["drawings"].get(v.get("drawing"), {})
        name = d.get("subject") or self.folder_name(v)
        try:
            from ..notion import record_short
            self.say(f"\U0001f4d2 Notion: {record_short(self.notion, name, self._today())}.")
        except Exception as e:  # noqa: BLE001 - the upload is recorded here either way
            print(f"warn: notion short: {str(e)[:200]}")
            self.say(f"\U0001f4d2 Couldn't update Notion ({str(e)[:120]}). /notion checks the connection.")

    def notion_sync(self) -> None:
        """Every [notion] sync_hours: LongDraw rows follow what you did on YouTube."""
        yt = self.clients.get("youtube")
        if not getattr(self, "notion", None) or not yt:
            return
        hours = float(self.cfg.get("notion", {}).get("sync_hours", 6))
        last = self.state.get("notion_sync_at")
        if last and self.clock() - parse(last) < timedelta(hours=hours):
            return
        self.state["notion_sync_at"] = iso(self.clock())
        try:
            from ..notion import sync_youtube
            changed = sync_youtube(self.notion, self._channel_videos(yt, 50))
        except Exception as e:  # noqa: BLE001
            print(f"warn: notion sync: {str(e)[:200]}")
            return
        if changed:
            self.say("\U0001f4d2 Notion pipeline updated from YouTube:\n" + "\n".join(f"• {c}" for c in changed))

    def notion_status(self) -> None:
        if not getattr(self, "notion", None):
            return self.say(SETUP)
        try:
            from ..notion import open_slots
            slots = open_slots(self.notion, self._today())
        except Exception as e:  # noqa: BLE001
            hint = (" The Content Pipeline isn't shared with the integration yet: in Notion open it › ••• › "
                    "Connections › add Ooz Reels bot." if "404" in str(e) else "")
            if "401" in str(e):
                hint = " The NOTION_TOKEN secret looks wrong: copy the integration's secret again."
            return self.say(f"\U0001f4d2 Notion is set up but didn't answer ({str(e)[:120]}).{hint}")
        self.state.pop("notion_sync_at", None)              # catch up with YouTube on the next look
        self.say("\U0001f4d2 Notion is connected to your Content Pipeline.\n"
                 f"Next empty Short slots: {', '.join(slots['Short']) or 'none'}\n"
                 f"Next empty LongDraw slots: {', '.join(slots['LongDraw']) or 'none'}\n\n"
                 "📤 Uploaded on a Short marks it Published there; a finished LongDraw fills the next LongDraw "
                 "slot; scheduling or publishing on YouTube updates the row by itself.")
