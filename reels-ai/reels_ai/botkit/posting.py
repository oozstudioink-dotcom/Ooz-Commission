"""Posting to platforms, stats and reports."""
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


class PostingMixin:
    # ------------------------------------------------------------------ what to draw next
    def trend_radar(self) -> None:
        """Once a day after [radar] hour (your time): what is getting views and what was asked for."""
        rc = self.cfg.get("radar", {})
        lt = self.local(self.clock())
        day = lt.strftime("%Y-%m-%d")
        if (not rc.get("enabled", True) or "youtube" not in self.clients or lt.hour < int(rc.get("hour", 9))
                or self.state.get("radar_day") == day):
            return
        self.state["radar_day"] = day
        self.send_radar(quiet=True)

    def send_radar(self, quiet: bool = False) -> None:
        from .. import eyes, radar
        from ..watch import cost
        yt = self.clients.get("youtube")
        if not yt:
            return None if quiet else self.say("\U0001f4c8 The trend radar needs the YouTube connection.")
        rc = self.cfg.get("radar", {})
        try:
            videos = radar.gather(yt, rc.get("queries", ["anime drawing", "manga drawing", "speed drawing anime"]),
                                  self.clock(), int(rc.get("days", 7)))[:40]
        except Exception as e:  # noqa: BLE001
            print(f"warn: radar: {str(e)[:200]}")
            return None if quiet else self.say(f"\U0001f4c8 Couldn't read YouTube's trends: {str(e)[:150]}")
        comments = []
        try:
            me = self.state.setdefault("yt_channel", {}).get("id") or yt.whoami()["id"]
            self.state["yt_channel"]["id"] = me
            comments = [c for c in yt.channel_comments(me) if not c["own"]]
        except Exception as e:  # noqa: BLE001 - comments need one more YouTube permission; trends still go out
            print(f"warn: radar comments: {str(e)[:200]}")
        named = None
        if eyes.available() and not self.claude_over_budget():
            try:
                named = radar.read_names(videos, comments, model=self.cfg.get("watch", {}).get("model", eyes.MODEL))
                print(f"radar: {cost(named) * 100:.1f} cents, ${self.spend(cost(named)):.2f} this month")
            except Exception as e:  # noqa: BLE001 - count hashtags instead
                print(f"warn: radar names: {str(e)[:150]}")
        if named is None:
            named = {"trending": radar.by_hashtags(videos), "requests": radar.by_phrases(comments)}
        since = self.clock() - timedelta(days=14)
        def recent(d: dict) -> bool:
            try:
                return parse(d["created"]) >= since
            except Exception:  # noqa: BLE001 - no date: count it as recent
                return True
        drawn = {radar._key(d.get("subject")) for d in self.state["drawings"].values()
                 if d.get("subject") and recent(d)}
        asked = radar.rank_requests(named["requests"], comments)
        self.state["requests"] = {radar._key(r["name"]): {"name": r["name"], "by": r["first"]} for r in asked}
        top = radar.rank_trending(named["trending"], videos, drawn, top=6)
        # kept for the content calendar (what is getting views this week)
        self.state["radar"] = {"at": iso(self.clock()), "trending": [
            {"name": t["name"], "series": t["series"], "views": t["views"], "channels": t["channels"]} for t in top]}
        self.say(radar.message(top[:3], asked))

    def credit_request(self, d: dict, results: list[dict]) -> None:
        """A drawing named after something a viewer asked for: thank them in the caption."""
        from ..radar import _key
        r = self.state.get("requests", {}).get(_key(d.get("subject")))
        if not r or not d.get("subject"):
            return
        for x in results:
            x["caption"] = f"Requested by {r['by']} \U0001f64f Who should I draw next?\n{x['caption']}"

    # ------------------------------------------------------------------ reports
    def weekly_report(self) -> None:
        """Sunday after 10 AM, once a week, when at least one platform is connected."""
        lt = self.local(self.clock())
        week = lt.strftime("%G-W%V")
        if not self.clients or lt.weekday() != 6 or lt.hour < 10 or self.state.get("report_week") == week:
            return
        self.state["report_week"] = week
        self.send_report()

    def send_report(self) -> None:
        from ..report import build
        if not self.clients:
            self.say("\U0001f4ca Reports need at least one connected platform (Instagram or YouTube tokens).")
            return
        try:
            self.say(build(self.state, self.clients, self.clock()))
        except Exception as e:  # noqa: BLE001
            self.say(f"\u26a0\ufe0f Couldn't build the report: {str(e)[:200]}")

    # ------------------------------------------------------------------ posting
    def post_due(self) -> None:
        t = self.clock()
        lt = self.local(t)
        for platform, pc in self.cfg.get("platforms", {}).items():
            if not pc.get("enabled"):
                continue
            open_slots = sum(1 for hhmm in pc.get("post_times", ["12:00"])
                             if lt.time() >= datetime.strptime(hhmm, "%H:%M").time())
            done_today = sum(1 for v in self.state["variants"].values()
                             if (p := v.get("posts", {}).get(platform))
                             and self.local(parse(p["at"])).date() == lt.date())
            if done_today >= min(pc.get("max_per_day", 1), open_slots):
                continue
            v = self.next_for(platform)
            if v:
                self.post(v, platform, pc)

    def next_for(self, platform: str) -> dict | None:
        ready = [v for v in self.state["variants"].values()
                 if v["status"] in ("approved", "posted") and platform not in v.get("posts", {})]
        if not ready:
            return None
        ready.sort(key=lambda v: v.get("decided_at", ""))
        posted = sorted((v for v in self.state["variants"].values() if platform in v.get("posts", {})),
                        key=lambda v: v["posts"][platform]["at"])
        last_drawing = posted[-1]["drawing"] if posted else None
        fresh = [v for v in ready if v["drawing"] != last_drawing]   # never same drawing twice in a row
        return (fresh or ready)[0]

    def post(self, v: dict, platform: str, pc: dict) -> None:
        name = PLATFORM_LABELS.get(platform, platform)
        client = self.clients.get(platform) if pc.get("method") == "api" else None
        error = None
        if client:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    video = self.bot.download(v["file_id"], Path(tmp) / "variant.mp4")
                    subject = self.state["drawings"].get(v["drawing"], {}).get("subject")
                    res = client.publish(video, {**v, "subject": subject}, pc)
                v.setdefault("posts", {})[platform] = {"at": iso(self.clock()), "method": "api",
                                                       "id": res["id"], "permalink": res.get("permalink")}
                v["status"] = "posted"
                link = f"\n{res['permalink']}" if res.get("permalink") else ""
                note = f"\n\u26a0\ufe0f {res['note']}" if res.get("note") else ""
                if res.get("note"):
                    v["posts"][platform]["note"] = res["note"]
                verb = "\U0001f4e5 Sent to your" if res.get("caption_for_user") else "\u2705 Posted to"
                self.say(f"{verb} {name}: {STYLE_LABELS.get(v['style'], v['style'])} ({v['drawing']}){link}{note}")
                if res.get("caption_for_user"):
                    self.say(v["caption"])
                self.note_rotated_token(platform, client)
                return
            except Exception as e:  # noqa: BLE001 - never lose a post slot to an API hiccup
                error = str(e)[:300]
                print(f"warn: {platform} publish failed: {error}")
        note = f"\u26a0\ufe0f Auto-post to {name} failed ({error}), so please post this one yourself.\n" if error else ""
        self.bot.send_video(self.owner, v["file_id"],
                            caption=f"{note}\U0001f4e4 Time to post on {name}\nCaption is in the next message.")
        self.say(v["caption"])
        v.setdefault("posts", {})[platform] = {"at": iso(self.clock()), "method": "manual",
                                               **({"error": error} if error else {})}
        v["status"] = "posted"

    def note_rotated_token(self, platform: str, client) -> None:
        new = getattr(client, "rotated_refresh_token", None)
        if new and self.state.get(f"{platform}_rotated_sent") != new[-6:]:
            self.state[f"{platform}_rotated_sent"] = new[-6:]
            self.say(f"\U0001f511 {PLATFORM_LABELS.get(platform, platform)} issued a new refresh token. "
                     f"When convenient, replace the GitHub secret {platform.upper()}_REFRESH_TOKEN with:\n{new}")

    def refresh_stats(self, every_hours: int = 6, days_back: int = 14) -> None:
        """Pull metrics for recent API posts (used later to learn which styles/hooks win)."""
        t = self.clock()
        last = self.state.get("stats_at")
        if last and t - parse(last) < timedelta(hours=every_hours):
            return
        did_any = False
        for platform, client in self.clients.items():
            for v in self.state["variants"].values():
                post = v.get("posts", {}).get(platform)
                if not post or not post.get("id") or t - parse(post["at"]) > timedelta(days=days_back):
                    continue
                try:
                    post["stats"] = client.insights(post["id"])
                    post["stats_at"] = iso(t)
                    did_any = True
                except Exception as e:  # noqa: BLE001
                    print(f"warn: stats {platform} {post['id']}: {e}")
        if did_any or self.clients:
            self.state["stats_at"] = iso(t)

    # ------------------------------------------------------------------ status
    def status_text(self) -> str:
        vs = self.state["variants"].values()
        count = lambda s: sum(1 for v in vs if v["status"] == s)
        queued = sum(1 for d in self.state["drawings"].values() if d["status"] == "queued")
        lines = [
            "⏸ PAUSED" if self.state["paused"] else "▶️ Running",
            f"Drawings waiting to render: {queued}",
            f"Waiting for your OK: {count('pending')}",
            f"Approved, not yet posted: {sum(1 for v in vs if v['status'] == 'approved')}",
            f"Posted: {count('posted')}   Skipped: {count('skipped')}",
            f"Approval: {self.cfg['approval']['mode']} ({self.cfg['approval'].get('fallback_hours', 0)}h)",
        ]
        if self.drops_mode:
            lines.append(f"Drops: {len(self.drop_times)}/day at {', '.join(self.drop_times)} "
                         f"(you post them; nothing is auto-posted)")
            return "\n".join(lines)
        for p, pc in self.cfg.get("platforms", {}).items():
            if pc.get("enabled"):
                lines.append(f"{PLATFORM_LABELS.get(p, p)}: {pc.get('max_per_day', 1)}/day at "
                             f"{', '.join(pc.get('post_times', []))} ({pc.get('method')})")
        return "\n".join(lines)
