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
    return {"inline_keyboard": [[{"text": "\U0001f4c5 Schedule on YouTube", "callback_data": f"ps:{vid}"}],
                                [{"text": "\U0001f4e4 Uploaded", "callback_data": f"pu:{vid}"},
                                 {"text": "\U0001f501 Redo", "callback_data": f"pr:{vid}"}]]}


def parse_views(text: str) -> int | None:
    """'12k' -> 12000, '1.2m' -> 1200000, '12,345' -> 12345."""
    import re
    m = re.fullmatch(r"\s*([\d.,]+)\s*([km])?\s*(views?)?\s*", (text or "").lower())
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1))


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
            tail = ("\U0001f4c5 Schedule on YouTube puts it in your next free Mon/Wed/Fri slot. Posted it yourself? "
                    "\U0001f4e4 Uploaded. Not right? \U0001f501 Redo makes a new take."
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
        if action == "ps":                                   # scheduled on YouTube by the bot
            self.bot.answer_callback(q["id"], "Scheduling on YouTube\u2026")
            return self.schedule_short(vid, v, mid)
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

    # ------------------------------------------------------------------ YouTube scheduling
    def next_short_slot(self):
        """The next posting day ([delivery] drop_days) at [delivery] youtube_time with no Short of yours
        scheduled yet, starting at least an hour from now. A local datetime."""
        from datetime import datetime, timedelta
        dc = self.cfg.get("delivery", {})
        days = {d[:3].title() for d in dc.get("drop_days") or ["Mon", "Wed", "Fri"]}
        hh, mm = (int(x) for x in str(dc.get("youtube_time", "12:00")).split(":"))
        taken = {x.get("yt_at", "")[:10] for x in self.state["variants"].values() if x.get("yt_at")}
        now = self.local(self.clock())
        day = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for _ in range(120):
            if day.strftime("%a") in days and day >= now + timedelta(hours=1) and \
                    self.local(day).date().isoformat() not in taken:
                return day
            day += timedelta(days=1)
        raise RuntimeError("no free posting day in the next four months")

    def schedule_short(self, vid: str, v: dict, mid) -> None:
        """Upload the channel Short to YouTube, private, going public in the next free slot."""
        import re
        import tempfile
        from datetime import timezone
        from pathlib import Path
        from .. import descriptions
        from ..youtube import shorts_title
        yt = self.clients.get("youtube")
        if not yt:
            return self.say("Scheduling needs the YouTube connection.")
        try:
            slot = self.next_short_slot()
            with tempfile.TemporaryDirectory() as tmp:
                m = re.search(r"/d/([\w-]+)", v.get("full_link") or "")
                if m and self.drive:                         # the full-quality file from Drive
                    path = self.drive.download(m.group(1), Path(tmp) / "short.mp4")
                else:                                        # else Telegram's copy
                    path = self.bot.download(v["file_id"], Path(tmp) / "short.mp4")
                d = self.state["drawings"].get(v.get("drawing"), {})
                subject = d.get("subject")
                tags_line = " ".join(w for w in (v.get("caption") or "").split() if w.startswith("#"))
                desc = descriptions.shorts_body(self.cfg) + "\n\n" + " ".join(dict.fromkeys(f"{tags_line} #shorts".split()))
                at = slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                y = yt.upload_file(path, shorts_title(v.get("hook") or "", subject), desc,
                                   [t.lstrip("#") for t in tags_line.split()][:15], "private", publish_at=at)
        except Exception as e:  # noqa: BLE001
            return self.say(f"\u26a0\ufe0f Couldn't schedule it on YouTube: {str(e)[:200]}")
        v.update(status="posted", used_at=iso(self.clock()), dropped_at=iso(self.clock()), decided_by="you",
                 yt_id=y["id"], yt_at=iso(slot))
        v.setdefault("posts", {})["youtube"] = {"id": y["id"], "at": iso(slot), "url": y["permalink"]}
        self.file_to(v, f"{COMPLETED}/Uploaded/{self.folder_name(v)}")
        when = slot.strftime("%a %b %-d, %-I:%M %p")
        self.bot.edit_markup(self.owner, mid, label_markup(f"\U0001f4c5 On YouTube {when}"))
        self.say(f"\U0001f4c5 Scheduled on YouTube for {when} EST: {y['permalink']}\nPost it on Instagram and "
                 "TikTok yourself that day (they don't allow scheduling from here).")
        self.feedback(v["style"], True)
        if getattr(self, "notion", None):
            try:
                from ..notion import record_short
                self.say(f"\U0001f4d2 Notion: {record_short(self.notion, subject or self.folder_name(v), slot.date(), 'Scheduled')}.")
            except Exception as e:  # noqa: BLE001
                print(f"warn: notion: {str(e)[:150]}")

    # ------------------------------------------------------------------ trial reel results
    def score_command(self, arg: str) -> None:
        """/score <style or video id> <views>: how a trial reel did on Facebook."""
        import re
        parts = (arg or "").strip().rsplit(maxsplit=1)
        views = parse_views(parts[-1]) if parts else None
        what = parts[0] if len(parts) == 2 else ""
        style = None
        if re.fullmatch(r"v\d+", what.strip()):
            style = (self.state["variants"].get(what.strip()) or {}).get("style")
        else:
            want = _norm_name(what)
            style = next((s for s in STYLE_LABELS if want and want in (_norm_name(s), _norm_name(STYLE_LABELS[s]))), None)
        if not style or views is None:
            return self.say("Tell me the style (or the video id from its message) and its views, e.g. "
                            "/score one take 12k or /score v412 3500. /styles lists the names.")
        self.state.setdefault("trial_scores", {}).setdefault(style, []).append({"views": views, "at": iso(self.clock())})
        n = len(self.state["trial_scores"][style])
        self.say(f"\U0001f4ca {STYLE_LABELS[style]}: {views:,} views noted ({n} result(s) so far). /styles compares them.")

    def trial_ranking(self) -> list[tuple[str, float, int]]:
        """(style, average views, results) for styles with trial results, best first."""
        out = [(st, sum(x["views"] for x in xs) / len(xs), len(xs))
               for st, xs in self.state.get("trial_scores", {}).items() if xs and st in STYLE_LABELS]
        return sorted(out, key=lambda r: -r[1])

    def channel_views(self, style: str) -> tuple[float, int]:
        """Average YouTube views of your channel Shorts in this style (from the bot's stats)."""
        vs = [x["posts"]["youtube"]["stats"].get("views", 0) for x in self.state["variants"].values()
              if x.get("style") == style and (x.get("posts") or {}).get("youtube", {}).get("stats")]
        return (sum(vs) / len(vs), len(vs)) if vs else (0.0, 0)

    def trial_prompt(self) -> None:
        """Sundays (after [audit] hour): ask how the week's trial reels did."""
        lt = self.local(self.clock())
        week = lt.strftime("%G-W%V")
        if (not self.flow_on or lt.weekday() != 6 or lt.hour < int(self.cfg.get("audit", {}).get("hour", 11))
                or self.state.get("trial_prompt_week") == week):
            return
        self.state["trial_prompt_week"] = week
        kept = sum(1 for v in self.state["variants"].values() if v["status"] == "trial")
        if not kept:
            return
        self.say("\U0001f9ea How did this week's Facebook trial reels do? For each one worth noting, send its style "
                 "and views, e.g. /score one take 12k (or the video id: /score v412 3500). After a few weeks /styles "
                 "shows which effect wins.")

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
        rank = self.trial_ranking()
        if rank:
            lines.append("\n\U0001f9ea Facebook trial reels, average views:")
            lines += [f"{i}. {STYLE_LABELS[st]}: {avg:,.0f} ({n} result{'s' if n != 1 else ''})"
                      for i, (st, avg, n) in enumerate(rank, 1)]
            best, avg, n = rank[0]
            if best != primary and n >= 3:
                lines.append(f"\U0001f3c6 {STYLE_LABELS[best]} is winning on Facebook. To make it your channel Short: "
                             f"/primary {STYLE_LABELS[best].lower()}")
        ch, chn = self.channel_views(primary)
        if chn:
            lines.append(f"\n\u2b50 {STYLE_LABELS[primary]} on YouTube: {ch:,.0f} average views over {chn} Short(s).")
        return ("\U0001f3a8 Your Short styles (\u2b50 = your channel Short):\n" + "\n".join(lines)
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
