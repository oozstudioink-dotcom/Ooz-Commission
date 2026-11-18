"""The channel auditor and the content calendar. YouTube and Claude are stand-ins here."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from reels_ai import audit
from reels_ai.bot import Runner
from reels_ai.state import load_config
from tests.test_bot import OWNER, Clock, FakeBot, fresh_state, msg, tap

NOW = datetime(2026, 11, 15, 17, tzinfo=timezone.utc)          # a Sunday, noon in New York


def vid(i, title, views, days, desc="Inked by hand https://ooz.example/commissions", tags=("naruto",)):
    return {"id": f"id{i}", "title": title, "tags": list(tags), "channel": "me", "views": views, "likes": views // 20,
            "comments": 2, "description": desc, "category": "1", "seconds": 20,
            "published": (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")}


VIDEOS = [vid(0, "Naruto ink timelapse", 1000, 10), vid(1, "drawing 4", 100, 10), vid(2, "Kakashi inked", 5000, 10),
          vid(3, "Sasuke speed ink", 900, 9), vid(4, "posted today", 5, 0.5), vid(5, "untitled", 150, 10)]


class FakeYT:
    def __init__(self):
        self.updated = []

    def whoami(self):
        return {"id": "me", "uploads": "UU"}

    def recent_videos(self, playlist, limit=5):
        return [{"id": v["id"]} for v in VIDEOS]

    def videos(self, ids):
        return VIDEOS

    def update_video(self, vid_, title, description, tags, category):
        self.updated.append((vid_, title, description, tags, category))


class MeasureTest(unittest.TestCase):
    def test_pace_is_against_your_own_usual_and_new_videos_are_not_judged(self):
        m = audit.measure(VIDEOS, NOW)
        self.assertEqual([v["verdict"] for v in m], ["usual", "weak", "strong", "usual", "new", "weak"])
        self.assertEqual(audit.weakest(m, 3), [1, 5])                    # worst first
        text = audit.report(m)
        self.assertIn("Kakashi inked", text.split("Falling behind")[0])
        self.assertIn("drawing 4", text.split("Falling behind")[1])

    def test_a_rewrite_changes_only_title_tags_and_hashtags(self):
        video = {**VIDEOS[1], "description": "Inked by hand https://ooz.example/commissions\n\n#old #tags"}
        new = audit.safe_rewrite(video, {"title": "  Naruto   inked by hand #viral", "hashtags": ["#Naruto", "anime art", "naruto"],
                                         "tags": ["#naruto", "naruto", "<b>"], "why": "names the subject"})
        self.assertEqual(new["title"], "Naruto inked by hand")                 # no hashtags in the title
        self.assertEqual(new["description"], "Inked by hand https://ooz.example/commissions\n\n#naruto #animeart")
        self.assertEqual(new["tags"], ["naruto", "b"])
        tpl = audit.safe_rewrite(video, {"title": "Naruto inked by hand", "hashtags": ["#naruto"], "tags": []},
                                 template="\nMy fixed text https://site\n")
        self.assertEqual(tpl["description"], "My fixed text https://site\n\n#naruto")   # the template, word for word
        self.assertIsNone(audit.safe_rewrite(VIDEOS[1], {"title": "x", "hashtags": [], "tags": []}))
        self.assertIsNone(audit.safe_rewrite({**VIDEOS[1], "description": "d\n\n#a"},
                                             {"title": VIDEOS[1]["title"], "hashtags": ["#a"], "tags": []}))

    def test_private_and_scheduled_videos_get_a_title_check_not_a_verdict(self):
        later = (NOW + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        vids = VIDEOS + [{**vid(6, "drawing 7", 0, 10), "privacy": "private", "publish_at": later},
                         {**vid(7, "ink 3", 0, 10), "privacy": "private"}]
        m = audit.measure(vids, NOW)
        self.assertEqual([v["verdict"] for v in m[6:]], ["upcoming", "upcoming"])
        self.assertEqual([v["verdict"] for v in m[:6]], ["usual", "weak", "strong", "usual", "new", "weak"])
        self.assertNotIn(6, audit.weakest(m, 9))
        self.assertEqual(audit.upcoming(m), [6, 7])                         # scheduled soonest first
        self.assertIn("Not out yet: 2", audit.report(m))

    def test_fixed_description_keeps_hashtags_and_leaves_longdraws_to_their_layout(self):
        tpl = "I draw comics.\n\nInstagram: https://www.instagram.com/oozstudioink1/"
        short = {"description": "old caption linktr.ee/x\n\n#gojo #jjk #shorts", "seconds": 30}
        self.assertEqual(audit.fixed_description(short, tpl), tpl + "\n\n#gojo #jjk #shorts")
        long = {"seconds": 3600, "description": "Welcome to LongDraw EP. 05!\n\nChapters\n0:00 Start\n\n#manga"}
        self.assertEqual(audit.fixed_description(long, tpl), long["description"])
        self.assertEqual(audit.fixed_description(long, tpl, ["#afro"]), long["description"].replace("#manga", "#afro"))
        self.assertEqual(audit.guess_episode_line(long), "LongDraw EP. 05")
        self.assertEqual(audit.guess_episode_line({"title": "Shinobigoto", "description": "LongDraw #01\nhi"}),
                         "LongDraw EP. 01")
        self.assertEqual(audit.guess_episode_line({"title": "Anatomy | Lesson 2 | Hands"}), "Anatomy \u00b7 Lesson 02")
        self.assertEqual(audit.guess_episode_line({"title": "Bruce Lee"}), "")

    def test_longdraws_are_matched_to_their_drive_folder(self):
        from reels_ai.botkit.auditing import AuditMixin
        numbers = [("", "sasuke", "LongDraw EP. 02", "Sasuke"), ("brucelee", "brucelee", "LongDraw EP. 04", "Bruce Lee"),
                   ("", "sasukeuchiha", "LongDraw EP. 09", "Sasuke Uchiha"),
                   ("afrosamuraimanga", "afro", "LongDraw EP. 05", "Afro")]
        pick = AuditMixin._episode_for
        self.assertEqual(pick({"title": "Sasuke Manga Inking Timelapse"}, numbers), ("LongDraw EP. 02", "Sasuke"))
        self.assertEqual(pick({"title": "Sasuke Uchiha ink"}, numbers)[0], "LongDraw EP. 09")       # longest name
        self.assertEqual(pick({"title": "Afro Samurai Manga"}, numbers), ("LongDraw EP. 05", "Afro"))  # saved title
        self.assertEqual(pick({"title": "Shinobigoto"}, numbers), ("", ""))

    def test_a_longdraw_rewrite_keeps_its_episode_number(self):
        v = {**VIDEOS[1], "title": "LongDraw EP. 05 | Afro Samurai | Manga Inking Timelapse", "seconds": 3600}
        self.assertIsNone(audit.safe_rewrite(v, {"title": "Afro Samurai ink drawing", "hashtags": ["#afrosamurai"], "tags": []}))
        ok = audit.safe_rewrite(v, {"title": "LongDraw EP. 05 | Afro Samurai Manga Art | Ink Timelapse",
                                    "hashtags": ["#afrosamurai"], "tags": []})
        self.assertIsNotNone(ok)

    def test_posting_days_only(self):
        days = audit.plan_days(datetime(2026, 11, 16, tzinfo=timezone.utc), 7, ["Mon", "Wed", "Fri"])
        self.assertEqual(days, ["Mon Nov 16", "Wed Nov 18", "Fri Nov 20"])

    def test_simple_calendar_puts_requests_first_and_never_repeats(self):
        days = audit.plan_days(NOW)
        plan = audit.simple_plan(days, [{"name": "Goku", "views": 9000, "channels": 3}, {"name": "sasuke", "views": 1, "channels": 1}],
                                 [{"name": "Sasuke", "by": "@early"}], ["one_take", "slow_open"])
        self.assertEqual([(d["character"], d["style"]) for d in plan], [("Sasuke", "one_take"), ("Goku", "slow_open")])
        self.assertIn("No calendar yet", audit.calendar_text([], [], {}))


class BotAuditTest(unittest.TestCase):
    def runner(self):
        self.bot, self.yt = FakeBot(), FakeYT()
        state = fresh_state()
        state["report_week"] = state["audit_week"] = "2026-W46"          # not the weekly run: commands only
        state["radar_day"] = "2026-11-15"
        r = Runner(self.bot, state, load_config(), clock=Clock(NOW), clients={"youtube": self.yt})
        self.bot.push(**msg(OWNER, text="hi"))
        r.poll()
        return r

    def texts(self):
        return [t for k, _, t in self.bot.sent if k == "text"]

    def test_audit_proposes_and_changes_youtube_only_when_you_tap_apply(self):
        r = self.runner()
        words = {"summary": "Named characters do best.", "findings": ["Titles without a name fall behind."],
                 "rewrites": [{"video": 1, "title": "Naruto inked by hand", "hashtags": ["#naruto"],
                               "tags": ["naruto"], "why": "names the subject"},
                              {"video": 2, "title": "Should be ignored", "hashtags": [], "tags": [], "why": ""}],
                 "tokens": {"in": 2000, "out": 800}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), mock.patch.object(audit, "propose", return_value=words):
            self.bot.push(**msg(OWNER, text="/audit"))
            r.poll()
        self.assertTrue(any("Named characters do best." in t for t in self.texts()))
        proposals = [t for t in self.texts() if "Proposed rewrite" in t]
        self.assertEqual(len(proposals), 1)                              # only the weak video
        self.assertEqual(self.yt.updated, [])                            # nothing changed yet
        self.bot.push(**tap("ta:id1"))
        r.poll()
        self.assertEqual(self.yt.updated[0][:2], ("id1", "Naruto inked by hand"))
        self.assertTrue(self.yt.updated[0][2].endswith("#naruto"))
        self.assertIn("comic artist", self.yt.updated[0][2])              # the description template from config
        self.bot.push(**tap("ta:id1"))                                   # a second tap does nothing
        self.bot.push(**msg(OWNER, text="/undo_rewrite"))
        r.poll()
        self.assertEqual(len(self.yt.updated), 2)
        self.assertEqual(self.yt.updated[1][:2], ("id1", "drawing 4"))   # the old title is back
        self.assertGreater(sum(r.state["claude_spend"].values()), 0)

    def test_fixdescriptions_lists_then_updates_on_tap_and_can_undo(self):
        r = self.runner()
        self.bot.push(**msg(OWNER, text="/fixdescriptions"))
        r.poll()
        ask = [t for t in self.texts() if "have an old description" in t]
        self.assertEqual(len(ask), 1)
        self.assertEqual(self.yt.updated, [])                            # nothing until the tap
        self.bot.push(**tap("fd:all"))
        r.poll()
        self.assertEqual(len(self.yt.updated), len(VIDEOS))
        self.assertTrue(all("comic artist" in u[2] for u in self.yt.updated))
        self.assertEqual(self.yt.updated[0][1], VIDEOS[0]["title"])       # titles untouched
        self.bot.push(**msg(OWNER, text="/undo_descriptions"))
        r.poll()
        self.assertEqual(self.yt.updated[-1][2], VIDEOS[-1]["description"])

    def test_without_claude_the_measured_audit_and_a_simple_calendar_still_arrive(self):
        r = self.runner()
        r.state["radar"] = {"trending": [{"name": "Goku", "series": "Dragon Ball", "views": 9000, "channels": 3}]}
        r.state["requests"] = {"sasuke": {"name": "Sasuke", "by": "@early"}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.bot.push(**msg(OWNER, text="/audit"))
            self.bot.push(**msg(OWNER, text="/calendar"))
            r.poll()
        self.assertTrue(any("Channel audit" in t and "Falling behind" in t for t in self.texts()))
        cal = [t for t in self.texts() if "Your next 7 days" in t][0]
        self.assertLess(cal.index("Sasuke"), cal.index("Goku"))
        self.assertFalse([t for t in self.texts() if "Proposed rewrite" in t])


if __name__ == "__main__":
    unittest.main()
