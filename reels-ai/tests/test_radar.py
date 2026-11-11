"""The trend radar and request tally. YouTube and Claude are stand-ins here."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from reels_ai import radar
from reels_ai.bot import Runner
from reels_ai.state import load_config
from tests.test_bot import OWNER, Clock, FakeBot, fresh_state, msg


def vid(i, title, views, channel, tags=(), seconds=20):
    return {"id": f"id{i}", "title": title, "tags": list(tags), "channel": channel, "views": views, "seconds": seconds}


VIDEOS = [vid(0, "Speed drawing Goku #shorts #dragonball #goku", 900_000, "A"),
          vid(1, "Stickman to Goku #goku #anime", 300_000, "B"),
          vid(2, "Drawing Gojo #gojo #jujutsukaisen", 2_000_000, "C"),
          vid(3, "Naruto in 20 seconds #naruto", 50_000, "A"),
          vid(4, "Goku ink #goku", 100_000, "C")]
COMMENTS = [{"author": "@late", "text": "Please draw Sasuke next", "likes": 1, "video": "x", "own": False},
            {"author": "@mid", "text": "nice lines!", "likes": 0, "video": "x", "own": False},
            {"author": "@early", "text": "can you draw Sasuke", "likes": 3, "video": "x", "own": False},
            {"author": "@one", "text": "Do Luffy", "likes": 0, "video": "y", "own": False}]


class FakeYT:
    def search_shorts(self, q, after, limit=25):
        return [v["id"] for v in VIDEOS] + ["long"]

    def videos(self, ids):
        return VIDEOS + [vid(9, "a long video #goku", 9_999_999, "Z", seconds=600)]

    def whoami(self):
        return {"id": "me"}

    def channel_comments(self, channel_id, limit=100):
        return COMMENTS + [{"author": "me", "text": "draw Zoro", "likes": 0, "video": "x", "own": True}]


class RadarTest(unittest.TestCase):
    def test_gathers_each_short_once_and_leaves_long_videos_out(self):
        got = radar.gather(FakeYT(), ["a", "b"], datetime(2026, 11, 11, tzinfo=timezone.utc))
        self.assertEqual([v["id"] for v in got], ["id2", "id0", "id1", "id4", "id3"])

    def test_a_trend_needs_several_channels_and_skips_what_you_just_drew(self):
        named = radar.by_hashtags(VIDEOS)
        top = radar.rank_trending(named, VIDEOS, drawn=set())
        self.assertEqual(top[0]["name"], "goku")                 # 3 channels beats one bigger video
        self.assertEqual((top[0]["videos"], top[0]["channels"], top[0]["views"]), (3, 3, 1_300_000))
        self.assertNotIn("shorts", [t["name"] for t in radar.rank_trending(named, VIDEOS, set(), top=9)])
        self.assertNotIn("goku", [t["name"] for t in radar.rank_trending(named, VIDEOS, {"goku"})])

    def test_requests_are_counted_per_viewer_and_credit_the_first_to_ask(self):
        asked = radar.rank_requests(radar.by_phrases(COMMENTS), COMMENTS)
        self.assertEqual((asked[0]["name"], asked[0]["asks"], asked[0]["first"]), ("Sasuke", 2, "@early"))
        self.assertEqual(asked[1]["name"], "Luffy")

    def test_bad_numbers_from_claude_are_ignored(self):
        top = radar.rank_trending([{"name": "Ghost", "series": "", "videos": [99, -1]}], VIDEOS, set())
        self.assertEqual(top, [])

    def bot(self, hour):
        b = FakeBot()
        r = Runner(b, fresh_state(), load_config(), clock=Clock(datetime(2026, 11, 12, hour, tzinfo=timezone.utc)),
                   clients={"youtube": FakeYT()})
        b.push(**msg(OWNER, text="hi"))
        r.poll()
        return b, r

    def test_sent_once_a_day_after_the_hour_and_on_trends_and_credits_the_request(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            b, r = self.bot(12)                                  # 7 AM in New York: too early
            self.assertFalse([t for k, _, t in b.sent if k == "text" and "What to draw today" in t])
            b, r = self.bot(15)                                  # 10 AM
            texts = [t for k, _, t in b.sent if k == "text" and "What to draw today" in t]
            self.assertEqual(len(texts), 1)
            self.assertIn("goku", texts[0])
            self.assertIn("Sasuke: 2 viewer(s), first @early", texts[0])
            self.assertNotIn("Zoro", texts[0])                   # your own comment is not a request
            r.poll()
            self.assertEqual(len([t for k, _, t in b.sent if k == "text" and "What to draw today" in t]), 1)
            b.push(**msg(OWNER, text="/trends"))
            r.poll()
            self.assertEqual(len([t for k, _, t in b.sent if k == "text" and "What to draw today" in t]), 2)
            results = [{"caption": "Inked by hand"}]
            r.credit_request({"subject": "Sasuke"}, results)
            self.assertTrue(results[0]["caption"].startswith("Requested by @early"))
            r.credit_request({"subject": "Kakashi"}, other := [{"caption": "x"}])
            self.assertEqual(other[0]["caption"], "x")


if __name__ == "__main__":
    unittest.main()
