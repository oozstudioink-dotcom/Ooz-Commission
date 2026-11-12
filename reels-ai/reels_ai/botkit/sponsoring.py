"""The sponsor finder in Telegram (the logic is in reels_ai/sponsors.py)."""
from __future__ import annotations

from ..state import iso


class SponsorMixin:
    def send_sponsors(self) -> None:
        from .. import sponsors
        from ..watch import cost
        yt = self.clients.get("youtube")
        if not yt:
            return self.say("\U0001f91d The sponsor finder needs the YouTube connection.")
        find_route, write_route = self.route("sponsors"), self.route("emails")
        if not find_route:
            return self.say("\U0001f91d The sponsor finder needs Gemini or Claude, and neither is available right "
                            "now (no Gemini key, and Claude is off or at its monthly limit).")
        sc = self.cfg.get("sponsors", {})
        self.say("\U0001f91d Looking at who sponsors creators like you. This takes a minute.")
        try:
            vids = sponsors.gather(yt, sc.get("queries", ["manga drawing", "anime drawing tutorial", "inking art",
                                                          "art supplies haul"]), self.clock(),
                                   int(sc.get("days", 60)))[:int(sc.get("videos", 40))]
            me = yt.whoami()
            found = sponsors.find(vids, self._model(), find_route)
            print(f"sponsors by {found.get('by')}: {cost(found) * 100:.1f} cents, ${self.spend(cost(found)):.2f} this month")
        except Exception as e:  # noqa: BLE001
            print(f"warn: sponsors: {str(e)[:200]}")
            return self.say(f"\U0001f91d Couldn't finish the sponsor search: {str(e)[:150]}")
        brands = sponsors.verify(found.get("brands", []), vids, own_channel=me.get("title", ""))
        self.state["sponsors"] = {"at": iso(self.clock()), "brands": brands}
        self.say(sponsors.list_text(brands, len(vids)))
        top = brands[:int(sc.get("drafts", 3))]
        if not top or not write_route:
            return
        try:
            facts = sponsors.channel_facts(me, self._channel_videos(yt, 20))
            got = sponsors.draft(top, facts, self._model(), write_route, about=sc)
            print(f"emails by {got.get('by')}: {cost(got) * 100:.1f} cents, ${self.spend(cost(got)):.2f} this month")
        except Exception as e:  # noqa: BLE001 - the list already went out
            print(f"warn: sponsor emails: {str(e)[:200]}")
            return self.say("✍️ I couldn't write the drafts this time; send /sponsors again later.")
        names = {b["name"].casefold() for b in top}
        for m in got.get("emails", []):
            if m.get("brand", "").casefold() not in names:
                continue
            if not sponsors.honest(m["body"], facts):
                print(f"warn: dropped a draft for {m['brand']}: it quoted a number that isn't yours")
                continue
            self.say(f"✉️ Draft for {m['brand']}\n\nSubject: {m['subject'].strip()}\n\n{m['body'].strip()}")
