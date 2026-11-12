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

    def test_a_rewrite_keeps_your_links_and_bad_ones_are_dropped(self):
        new = audit.safe_rewrite(VIDEOS[1], {"title": "  Naruto   inked by hand ", "description": "Watch the ink go on.",
                                             "tags": ["#naruto", "naruto", "<b>"], "why": "names the subject"})
        self.assertEqual(new["title"], "Naruto inked by hand")
        self.assertIn("https://ooz.example/commissions", new["description"])
        self.assertEqual(new["tags"], ["naruto", "b"])
        self.assertIsNone(audit.safe_rewrite(VIDEOS[1], {"title": "x", "description": "d", "tags": []}))
        self.assertIsNone(audit.safe_rewrite(VIDEOS[1], {"title": VIDEOS[1]["title"],
                                                         "description": VIDEOS[1]["description"], "tags": []}))

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
                 "rewrites": [{"video": 1, "title": "Naruto inked by hand", "description": "Ink only.",
                               "tags": ["naruto"], "why": "names the subject"},
                              {"video": 2, "title": "Should be ignored", "description": "not weak", "tags": [], "why": ""}],
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
        self.assertIn("https://ooz.example/commissions", self.yt.updated[0][2])
        self.bot.push(**tap("ta:id1"))                                   # a second tap does nothing
        self.bot.push(**msg(OWNER, text="/undo_rewrite"))
        r.poll()
        self.assertEqual(len(self.yt.updated), 2)
        self.assertEqual(self.yt.updated[1][:2], ("id1", "drawing 4"))   # the old title is back
        self.assertGreater(sum(r.state["claude_spend"].values()), 0)

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
