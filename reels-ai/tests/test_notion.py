"""The Notion Content Pipeline: what the bot writes there. Notion itself is a stand-in here."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from datetime import date, datetime, timezone
from unittest import mock

from reels_ai import notion
from reels_ai.bot import Runner
from reels_ai.state import load_config
from tests.test_bot import OWNER, Clock, FakeBot, fresh_state


def row(rid, title="", fmt="", character="", status="Idea", when="", url=""):
    props = {"Title": {"type": "title", "title": [{"plain_text": title}]},
             "Character": {"type": "rich_text", "rich_text": [{"plain_text": character}] if character else []},
             "Format": {"type": "select", "select": {"name": fmt} if fmt else None},
             "Status": {"type": "select", "select": {"name": status}},
             "Publish Date": {"type": "date", "date": {"start": when} if when else None},
             "YouTube": {"type": "url", "url": url or None}}
    return {"id": rid, "properties": props}


class FakeNotion:
    def __init__(self, rows):
        self.rows, self.updates, self.created = rows, [], []

    def query(self, filter=None):
        return self.rows

    def update(self, pid, props):
        self.updates.append((pid, props))

    def create(self, props):
        self.created.append(props)


def val(props, name):
    p = props[name]
    for k in ("select", "date"):
        if k in p:
            return (p[k] or {}).get("name") or (p[k] or {}).get("start")
    if "rich_text" in p:
        return p["rich_text"][0]["text"]["content"]
    if "title" in p:
        return p["title"][0]["text"]["content"]
    return p.get("number", p.get("url"))


TODAY = date(2027, 1, 6)


class ShortsTest(unittest.TestCase):
    def test_a_planned_character_is_marked_published(self):
        nt = FakeNotion([row("a", "Gojo idea", "", "Gojo", when="2026-11-30"),
                         row("b", "Short · Wed Jan 6", "Short", when="2027-01-06T12:00:00.000-05:00")])
        msg = notion.record_short(nt, "Gojo", TODAY)
        pid, props = nt.updates[0]
        self.assertEqual((pid, val(props, "Status"), val(props, "Publish Date")), ("a", "Published", "2027-01-06"))
        self.assertIn("marked Published", msg)

    def test_otherwise_today_slot_is_filled_or_a_row_added(self):
        nt = FakeNotion([row("b", "Short · Wed Jan 6", "Short", when="2027-01-06T12:00:00.000-05:00"),
                         row("c", "Short · Fri Jan 8", "Short", when="2027-01-08T12:00:00.000-05:00")])
        notion.record_short(nt, "Vegeta", TODAY)
        pid, props = nt.updates[0]
        self.assertEqual((pid, val(props, "Character"), val(props, "Status")), ("b", "Vegeta", "Published"))
        self.assertEqual(val(props, "Title"), "Short · Vegeta · Wed Jan 6")
        nt2 = FakeNotion([row("c", "Short · Fri Jan 8", "Short", when="2027-01-08T12:00:00.000-05:00")])
        notion.record_short(nt2, "Vegeta", date(2027, 1, 7))                 # a Thursday: no slot
        self.assertEqual(nt2.updates, [])                                     # Friday's slot is left alone
        self.assertEqual(val(nt2.created[0], "Publish Date"), "2027-01-07")
        nt3 = FakeNotion([row("d", "Short · Vegeta", "Short", "Vegeta", "Published", "2027-01-06")])
        self.assertIn("already", notion.record_short(nt3, "Vegeta", TODAY))   # a second tap: nothing new
        self.assertEqual((nt3.updates, nt3.created), ([], []))


class LongDrawTest(unittest.TestCase):
    def test_a_finished_longdraw_fills_the_next_empty_slot(self):
        nt = FakeNotion([row("old", "LongDraw · Fri Jan 1", "LongDraw", when="2027-01-01"),     # past
                         row("w", "LongDraw · Wed Jan 6", "LongDraw", when="2027-01-06T12:00:00.000-05:00"),
                         row("f", "LongDraw · Fri Jan 8", "LongDraw", when="2027-01-08T12:00:00.000-05:00"),
                         row("s", "Short · Wed Jan 6", "Short", when="2027-01-06")])
        msg = notion.record_longdraw(nt, "Gojo", 8, "https://youtu.be/abc123XYZ", date(2027, 1, 5))
        pid, props = nt.updates[0]
        self.assertEqual(pid, "w")
        self.assertEqual((val(props, "Title"), val(props, "Episode"), val(props, "YouTube")),
                         ("LongDraw EP. 08 · Gojo", 8, "https://youtu.be/abc123XYZ"))
        self.assertIn("schedule it on YouTube for Wed Jan 6", msg)
        nt.rows[1] = row("w", "LongDraw EP. 08 · Gojo", "LongDraw", "Gojo", "Editing", "2027-01-06",
                         "https://youtu.be/abc123XYZ")
        self.assertIn("already", notion.record_longdraw(nt, "Gojo", 8, "https://youtu.be/abc123XYZ", TODAY))

    def test_rows_follow_youtube(self):
        nt = FakeNotion([row("w", "LongDraw EP. 08 · Gojo", "LongDraw", "Gojo", "Editing", "2027-01-06",
                             "https://youtu.be/abc123XYZ"),
                         row("p", "LongDraw EP. 07 · Afro", "LongDraw", "Afro", "Scheduled", "2026-12-11",
                             "https://youtu.be/def456UVW")])
        videos = [{"id": "abc123XYZ", "seconds": 3600, "privacy": "private", "publish_at": "2027-01-08T17:00:00Z"},
                  {"id": "def456UVW", "seconds": 3600, "privacy": "public", "published": "2026-12-11T17:00:00Z"}]
        changed = notion.sync_youtube(nt, videos)
        self.assertEqual([(p, val(x, "Status"), val(x, "Publish Date")) for p, x in nt.updates],
                         [("w", "Scheduled", "2027-01-08T17:00:00Z"), ("p", "Published", "2026-12-11T17:00:00Z")])
        self.assertEqual(len(changed), 2)


class BotTest(unittest.TestCase):
    def runner(self):
        b = FakeBot()
        state = fresh_state()
        state["owner_chat_id"] = OWNER
        return b, Runner(b, state, load_config(), clock=Clock(datetime(2027, 1, 6, 18, tzinfo=timezone.utc)))

    def test_notion_without_a_token_explains_the_setup(self):
        b, r = self.runner()
        r.notion_status()
        self.assertTrue(any("NOTION_TOKEN" in t and "never in this chat" in t for k, _, t in b.sent))

    def test_uploaded_short_goes_to_notion(self):
        b, r = self.runner()
        r.notion = FakeNotion([row("b", "Short · Wed Jan 6", "Short", when="2027-01-06T12:00:00.000-05:00")])
        r.state["drawings"]["d1"] = {"subject": "Vegeta", "status": "rendered"}
        r.notion_short({"drawing": "d1", "style": "one_take"})
        self.assertEqual(val(r.notion.updates[0][1], "Character"), "Vegeta")
        self.assertTrue(any("Notion: today's Short slot filled with Vegeta" in t for k, _, t in b.sent))

    def test_from_env_needs_the_token(self):
        cfg = load_config()
        with mock.patch.dict(os.environ, {"NOTION_TOKEN": ""}):
            self.assertIsNone(notion.from_env(cfg))
        with mock.patch.dict(os.environ, {"NOTION_TOKEN": "secret_x"}):
            self.assertEqual(notion.from_env(cfg).ds, "e4a0104a-3b9f-83e0-8a88-07da64220d3a")


class ClientTest(unittest.TestCase):
    def test_falls_back_to_the_databases_api_and_never_resends_a_new_row(self):
        calls = []

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=0):
            calls.append((req.get_method(), req.full_url, req.headers.get("Notion-version")))
            if "/data_sources/" in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 400, "x", {}, io.BytesIO(b'{"message":"bad version"}'))
            return R(json.dumps({"results": [row("a")], "has_more": False}).encode())

        nt = notion.Notion("secret_x", "db1", "ds1")
        with mock.patch("urllib.request.urlopen", opener), mock.patch("time.sleep"):
            self.assertEqual(len(nt.query()), 1)
            nt.create({"Title": notion.p_title("x")})
        self.assertIn("/databases/db1/query", calls[1][1])
        self.assertEqual(calls[-1][1].rsplit("/", 1)[1], "pages")
        self.assertEqual(sum(1 for c in calls if c[1].endswith("/pages")), 1)


if __name__ == "__main__":
    unittest.main()
