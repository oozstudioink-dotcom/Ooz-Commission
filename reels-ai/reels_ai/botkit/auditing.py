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
        soon = audit.upcoming(measured, int(ac.get("upcoming", 5)))
        words = None
        if self._claude_ok():
            try:
                words = audit.propose(measured, weak, self.state.get("radar", {}).get("trending", []), self._model(),
                                      soon=soon)
                print(f"audit: {cost(words) * 100:.1f} cents, ${self.spend(cost(words)):.2f} this month")
            except Exception as e:  # noqa: BLE001 - the measured audit still goes out
                print(f"warn: audit words: {str(e)[:150]}")
        self.state["audit_best"] = [{"title": v["title"], "pace": v["pace"]} for v in
                                    sorted((v for v in measured if v["verdict"] == "strong"),
                                           key=lambda v: -v["pace"])[:3]]
        self.say(audit.report(measured, words))
        for r in (words or {}).get("rewrites", []):
            k = r.get("video")
            if not isinstance(k, int) or (k not in weak and k not in soon):
                continue
            v, new = measured[k], audit.safe_rewrite(measured[k], r, self.cfg.get("youtube", {}).get("description_template", ""))
            if not new:
                continue
            key = v["id"]
            self.state.setdefault("rewrites", {})[key] = {
                **new, "category": v["category"], "at": iso(self.clock()), "status": "proposed",
                "old": {"title": v["title"], "description": v["description"], "tags": v["tags"]}}
            when = ("" if k in weak else
                    f" · not out yet{' (scheduled ' + v['publish_at'][:10] + ')' if v.get('publish_at') else ' (private)'}")
            self.say(f"✏️ Proposed rewrite · youtu.be/{key}{when}\n\nNow: {v['title']}\nNew: {new['title']}\n\n"
                     f"Hashtags: {' '.join(new['hashtags'])}\nTags: {', '.join(new['tags'])}\n"
                     f"Description: your template{' (from config)' if self.cfg.get('youtube', {}).get('description_template') else ', with only the hashtag line changed'}\n\n"
                     f"Why: {new['why']}\n\n"
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

    def fix_descriptions(self) -> None:
        """/fixdescriptions: every recent video's description brought to your template (Shorts: the
        template and their own hashtags; LongDraws: chapters and credits kept, links block replaced).
        Shows what would change and waits for a tap."""
        from .. import audit
        from ..about import HEADING, about, with_about
        yt = self.clients.get("youtube")
        template = self.cfg.get("youtube", {}).get("description_template", "")
        if not yt:
            return self.say("Fixing descriptions needs the YouTube connection.")
        if not template.strip():
            return self.say("There's no description template in config.toml ([youtube] description_template).")
        try:
            videos = self._channel_videos(yt, 50)
        except Exception as e:  # noqa: BLE001
            return self.say(f"\U0001f50e Couldn't read your channel: {str(e)[:150]}")
        numbers = self._longdraw_numbers()
        todo, unnumbered = {}, []
        for v in videos:
            line = self._episode_for(v, numbers) if v.get("seconds", 0) > 90 else ""
            new = audit.fixed_description(v, template, episode=line)
            if v.get("seconds", 0) > 90 and HEADING.lower() not in new.lower():   # LongDraws: who it is
                new = with_about(new, about(v["title"], self._model()))
            if v.get("seconds", 0) > 90 and not audit.EP_LINE.match(new.split("\n", 1)[0]):
                unnumbered.append(v["title"])
            if new.strip() != (v.get("description") or "").strip():
                todo[v["id"]] = {"title": v["title"], "tags": v["tags"], "category": v["category"],
                                 "old": v.get("description", ""), "new": new}
        missing = ("\n\nNo episode number found for: " + "; ".join(t[:50] for t in unnumbered)
                   + ". Give it one with /rethumb <name> <number>.") if unnumbered else ""
        if not todo:
            return self.say(f"\u2705 All {len(videos)} recent videos already have your current description." + missing)
        self.state["desc_fix"] = {"at": iso(self.clock()), "videos": todo, "status": "proposed"}
        names = "\n".join(f"• {t['title'][:60]}" for t in list(todo.values())[:25])
        more = f"\n…and {len(todo) - 25} more" if len(todo) > 25 else ""
        self.say(f"\U0001f4dd {len(todo)} of your last {len(videos)} videos have an old description:\n{names}{more}\n\n"
                 "Shorts get your template plus their own hashtags. LongDraws start with their episode "
                 "(LongDraw EP. 05), get a short note on the character when it's from a known manga or comic, "
                 "and keep their chapters and music credits; only the old links part becomes "
                 "your template. Titles and tags stay as they are." + missing,
                 reply_markup={"inline_keyboard": [[{"text": f"✅ Update all {len(todo)}", "callback_data": "fd:all"},
                                                    {"text": "❌ Skip", "callback_data": "fs:all"}]]})

    def _longdraw_numbers(self) -> list[tuple[str, str, str]]:
        """Every finished LongDraw in Drive › Done › Longform with an episode number in its folder name:
        (its saved YouTube title, its subject, its episode line), names squashed for matching."""
        import re
        from .. import audit
        from ..drive import parse_name
        from ..series import all_series
        if not self.drive:
            return []
        squash = lambda t: re.sub(r"[^a-z0-9]+", "", (t or "").lower())  # noqa: E731
        out = []
        try:
            for place, ser in [("Done/Longform", None)] + [(f"Done/Longform/{s_['name']}", s_)
                                                          for s_ in all_series(self.cfg)]:
                for f in self.drive.list(self.drive.folder(place), "folder"):
                    m = re.match(r"\s*(?:EP|LESSON|PART)\.?\s*(\d+)\s*-\s*(.+)$", f["name"], re.I)
                    if not m:
                        continue
                    saved = ""
                    txt = next((x for x in self.drive.list(f["id"], "") if x["name"].lower().endswith(".txt")), None)
                    if txt:
                        saved = self.drive.read_text(txt["id"]).strip().split("\n", 1)[0]
                    subject = parse_name(m.group(2))[0] or m.group(2)
                    out.append((squash(saved), squash(subject), audit.episode_line(int(m.group(1)), ser)))
        except Exception as e:  # noqa: BLE001 - the numbers in the videos themselves are used instead
            print(f"warn: LongDraw numbers from Drive: {str(e)[:150]}")
        return out

    @staticmethod
    def _episode_for(video: dict, numbers: list[tuple[str, str, str]]) -> str:
        """The episode line for a long video: the Drive folder with its saved title, else the folder
        whose subject is in its title (the longest subject wins); "" leaves it to the video itself."""
        import re
        title = re.sub(r"[^a-z0-9]+", "", (video.get("title") or "").lower())
        exact = [line for saved, _, line in numbers if saved and saved == title]
        if exact:
            return exact[0]
        named = sorted(((len(subj), line) for _, subj, line in numbers if len(subj) >= 3 and subj in title),
                       reverse=True)
        return named[0][1] if named else ""

    def on_desc_fix(self, action: str, q: dict) -> None:
        fix = self.state.get("desc_fix") or {}
        mid = q.get("message", {}).get("message_id")
        if fix.get("status") != "proposed":
            return self.bot.answer_callback(q["id"], "Already handled")
        if action == "fs":
            fix["status"] = "skipped"
            self.bot.answer_callback(q["id"], "Skipped")
            return self.bot.edit_markup(self.owner, mid, label_markup("❌ Skipped: nothing changed"))
        fix["status"] = "applied"
        self.bot.answer_callback(q["id"], "Updating")
        done, failed = 0, []
        for vid, t in fix["videos"].items():
            try:
                self.clients["youtube"].update_video(vid, t["title"], t["new"], t["tags"], t["category"])
                t["done"] = True
                done += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{t['title'][:40]}: {str(e)[:80]}")
                if "insufficient" in str(e).lower() or "forbidden" in str(e).lower():
                    failed.append("Stopped: the Google sign-in can't edit videos (it needs the permission to "
                                  "manage your YouTube videos, not only upload).")
                    break
        self.bot.edit_markup(self.owner, mid, label_markup(f"✅ Updated {done} on YouTube"))
        self.say(f"\U0001f4dd Updated {done} description(s)." + ("\nNot changed:\n" + "\n".join(failed) if failed else "")
                 + "\n/undo_descriptions puts the old ones back.")

    def undo_descriptions(self) -> None:
        fix = self.state.get("desc_fix") or {}
        done = {k: t for k, t in fix.get("videos", {}).items() if t.get("done")}
        if fix.get("status") != "applied" or not done:
            return self.say("There's no description update to undo.")
        back = 0
        for vid, t in done.items():
            try:
                self.clients["youtube"].update_video(vid, t["title"], t["old"], t["tags"], t["category"])
                back += 1
            except Exception as e:  # noqa: BLE001
                print(f"warn: undo description {vid}: {str(e)[:120]}")
        fix["status"] = "undone"
        self.say(f"↩️ Put back {back} old description(s).")

    def send_calendar(self) -> None:
        from .. import audit
        from ..watch import cost
        trending = self.state.get("radar", {}).get("trending", [])
        requests = list(self.state.get("requests", {}).values())
        styles = [s for s in self.cfg["variants"]["styles"] if s in STYLE_LABELS]
        days = audit.plan_days(self.local(self.clock()), 7, self.cfg.get("delivery", {}).get("drop_days"))
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
