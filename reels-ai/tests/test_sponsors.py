"""Model routing (Gemini first, Claude as backup) and the sponsor finder. All stand-ins."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from reels_ai import llm, sponsors
from reels_ai.bot import Runner
from reels_ai.state import load_config
from tests.test_bot import OWNER, Clock, FakeBot, fresh_state, msg

NOW = datetime(2026, 11, 12, 20, tzinfo=timezone.utc)
SCHEMA = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}


def vid(i, channel, desc, views=1000):
    return {"id": f"id{i}", "title": f"t{i}", "tags": [], "channel": channel, "views": views, "likes": 1, "comments": 0,
            "description": desc, "category": "1", "seconds": 300, "published": "2026-11-01T00:00:00Z"}


VIDEOS = [vid(0, "InkA", "Thanks to Kuretake for sending these brush pens! https://kuretake.example", 9000),
          vid(1, "InkB", "Use code INKB10 at Kuretake for 10% off\nMy Patreon: https://patreon.example/b", 5000),
          vid(2, "InkC", "Paper from Deleter (affiliate): https://deleter.example/?ref=c", 2000),
          vid(3, "InkA", "no deals here, just vibes", 99999)]


class RoutingTest(unittest.TestCase):
    def test_gemini_goes_first_and_claude_steps_in_when_it_fails(self):
        route = {"first": "gemini", "gemini_key": "g", "claude": True}
        with mock.patch.object(llm, "ask_gemini", return_value={"n": 1, "by": "gemini"}) as g, \
                mock.patch.object(llm, "ask_claude") as c:
            self.assertEqual(llm.ask("s", SCHEMA, "t", route=route)["by"], "gemini")
            c.assert_not_called()
        with mock.patch.object(llm, "ask_gemini", side_effect=RuntimeError("503 busy")), \
                mock.patch.object(llm, "ask_claude", return_value={"n": 2, "by": "claude", "tokens": {"in": 1, "out": 1}}):
            self.assertEqual(llm.ask("s", SCHEMA, "t", route=route)["by"], "claude")
        with mock.patch.object(llm, "ask_gemini", side_effect=RuntimeError("busy")), self.assertRaises(RuntimeError):
            llm.ask("s", SCHEMA, "t", route={"first": "gemini", "gemini_key": "g", "claude": False})
        self.assertTrue(g.called)

    def test_a_gemini_answer_of_the_wrong_shape_is_refused(self):
        self.assertTrue(llm.fits({"n": 3}, SCHEMA))
        self.assertFalse(llm.fits({"n": "three"}, SCHEMA))
        self.assertFalse(llm.fits({}, SCHEMA))
        self.assertFalse(llm.fits([{"n": 1}], SCHEMA))


class SponsorTest(unittest.TestCase):
    def test_only_brands_with_a_real_quote_are_kept_and_ranked_by_channels(self):
        found = [{"name": "Kuretake", "kind": "product sent", "videos": [0, 1], "evidence": "Thanks to Kuretake for  sending"},
                 {"name": "Deleter", "kind": "affiliate", "videos": [2], "evidence": "Paper from Deleter (affiliate)"},
                 {"name": "Copic", "kind": "sponsor", "videos": [3], "evidence": "sponsored by Copic"},      # made up
                 {"name": "Ghost", "kind": "sponsor", "videos": [42], "evidence": "x"}]
        got = sponsors.verify(found, VIDEOS)
        self.assertEqual([b["name"] for b in got], ["Kuretake", "Deleter"])
        self.assertEqual(got[0]["channels"], ["InkA", "InkB"])
        self.assertIn("Kuretake", sponsors.list_text(got, 4))
        self.assertIn("no brand deals", sponsors.list_text([], 4))

    def test_an_email_may_only_quote_your_real_numbers(self):
        facts = sponsors.channel_facts({"id": "me", "title": "Ooz Studio Ink", "subscribers": "412", "videos": "96"},
                                       [{"views": 800}, {"views": 600}, {"views": 1500}])
        self.assertEqual((facts["subscribers"], facts["typical_views"], facts["best_views"]), (412, 800, 1500))
        self.assertTrue(sponsors.honest("I have 412 subscribers and about 800 views a video.", facts))
        self.assertFalse(sponsors.honest("My videos reach 50,000 viewers a month.", facts))


class FakeYT:
    def search_shorts(self, q, after, limit=25, duration="short"):
        return [v["id"] for v in VIDEOS]

    def videos(self, ids):
        return VIDEOS

    def whoami(self):
        return {"id": "me", "title": "Ooz Studio Ink", "subscribers": "412", "videos": "96", "uploads": "UU"}

    def recent_videos(self, playlist, limit=5):
        return [{"id": "id0"}]


class BotSponsorTest(unittest.TestCase):
    def test_sponsors_command_lists_brands_and_sends_only_honest_drafts(self):
        bot, state = FakeBot(), fresh_state()
        state.update(report_week="2026-W46", audit_week="2026-W46", radar_day="2026-11-12")
        r = Runner(bot, state, load_config(), clock=Clock(NOW), clients={"youtube": FakeYT()})
        found = {"brands": [{"name": "Kuretake", "kind": "product sent", "videos": [0, 1],
                             "evidence": "Thanks to Kuretake for sending"}], "by": "gemini"}
        emails = {"emails": [{"brand": "Kuretake", "subject": "Inking with your brush pens",
                              "body": "Hi, I run Ooz Studio Ink (412 subscribers)."},
                             {"brand": "Kuretake", "subject": "x", "body": "I reach 900,000 people."},
                             {"brand": "Nobody", "subject": "x", "body": "hello"}],
                  "by": "claude", "tokens": {"in": 800, "out": 400}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "GEMINI_API_KEY": "g"}), \
                mock.patch.object(sponsors, "find", return_value=found) as f, \
                mock.patch.object(sponsors, "draft", return_value=emails) as d:
            bot.push(**msg(OWNER, text="hi"))
            bot.push(**msg(OWNER, text="/sponsors"))
            r.poll()
        self.assertEqual(f.call_args[0][2]["first"], "gemini")           # reading goes to the free model
        self.assertEqual(d.call_args[0][3]["first"], "claude")           # the emails are Claude's
        texts = [t for k, _, t in bot.sent if k == "text"]
        self.assertTrue(any("Kuretake" in t and "2 channel(s)" in t for t in texts))
        drafts = [t for t in texts if t.startswith("✉️ Draft for")]
        self.assertEqual(len(drafts), 1)
        self.assertIn("412 subscribers", drafts[0])
        self.assertAlmostEqual(sum(r.state["claude_spend"].values()), 0.0112)


if __name__ == "__main__":
    unittest.main()
