"""Stability: one bad command, Telegram limits and hiccups, and who the bot hands over to."""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from reels_ai import bot as botmod
from reels_ai import telegram
from reels_ai.bot import Runner
from reels_ai.state import load_config
from tests.test_bot import OWNER, Clock, FakeBot, fresh_state, msg

NOW = datetime(2026, 11, 18, 17, tzinfo=timezone.utc)


class OneBadCommandTest(unittest.TestCase):
    def test_a_command_that_breaks_is_reported_and_the_next_still_runs(self):
        b = FakeBot()
        state = fresh_state()
        state["report_week"] = state["audit_week"] = "2026-W47"
        state["radar_day"] = "2026-11-18"
        r = Runner(b, state, load_config(), clock=Clock(NOW))
        b.push(**msg(OWNER, text="hi"))                                   # claims the bot
        r.poll()
        b.sent.clear()
        b.push(**msg(OWNER, text="/status"))
        b.push(**msg(OWNER, text="/help"))
        with mock.patch.object(Runner, "status_text", side_effect=KeyError("drawings")):
            r.poll()
        texts = [t for k, _, t in b.sent if k == "text"]
        self.assertTrue(any("That didn't work (KeyError" in t for t in texts))
        self.assertTrue(any("/rethumb" in t for t in texts))              # /help still answered
        self.assertEqual(r.state["update_offset"], 4)                     # never handled twice


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


class TelegramTest(unittest.TestCase):
    def test_too_many_requests_waits_and_sends_once(self):
        calls = []

        def opener(req, timeout=0):
            calls.append(json.loads(req.data))
            if len(calls) == 1:
                raise http_error(429, {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                       "parameters": {"retry_after": 1}})
            return FakeResponse(json.dumps({"ok": True, "result": {"message_id": 5}}).encode())

        with mock.patch("urllib.request.urlopen", opener), mock.patch("time.sleep") as slept:
            got = telegram.Bot("t").send_message(1, "hi")
        self.assertEqual(got["message_id"], 5)
        self.assertEqual(len(calls), 2)
        slept.assert_called_with(1)

    def test_a_proxy_error_page_is_a_telegram_error_not_a_crash(self):
        def opener(req, timeout=0):
            raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b"<html>bad gateway</html>"))

        with mock.patch("urllib.request.urlopen", opener):
            with self.assertRaisesRegex(telegram.TelegramError, "HTTP 400"):
                telegram.Bot("t").send_message(1, "hi")

    def test_long_messages_go_in_pieces_with_the_buttons_on_the_last(self):
        sent = []

        def opener(req, timeout=0):
            sent.append(json.loads(req.data))
            return FakeResponse(json.dumps({"ok": True, "result": {"message_id": len(sent)}}).encode())

        text = "\n".join(f"• video {i} " + "x" * 60 for i in range(150))
        with mock.patch("urllib.request.urlopen", opener):
            telegram.Bot("t").send_message(1, text, reply_markup={"inline_keyboard": []})
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(s["text"]) <= 4096 for s in sent))
        self.assertEqual("\n".join(s["text"] for s in sent), text)
        self.assertNotIn("reply_markup", sent[0])
        self.assertIn("reply_markup", sent[-1])


class HandoverTest(unittest.TestCase):
    def test_the_safety_timer_does_not_interrupt_a_running_bot(self):
        runs = {"workflow_runs": [{"event": "schedule"}, {"event": "push"}, {"event": "workflow_dispatch"}]}
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}), \
                mock.patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse(json.dumps(runs).encode())):
            self.assertEqual(botmod.runs_waiting(), 4)                    # 2 per status asked, schedule left out
        only_timer = {"workflow_runs": [{"event": "schedule"}]}
        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}), \
                mock.patch("urllib.request.urlopen", lambda req, timeout=0: FakeResponse(json.dumps(only_timer).encode())):
            self.assertEqual(botmod.runs_waiting(), 0)


if __name__ == "__main__":
    unittest.main()


class LongDrawDescriptionTest(unittest.TestCase):
    def test_every_song_is_listed_with_when_it_starts(self):
        from reels_ai.longform import youtube_text
        _, desc, _ = youtube_text("Gojo", 6, "0:00 Intro\n0:05 Gojo\n9:00 Ink", {}, 4.0, 3600,
                                  [("Night Walk", 0.0), ("Late Ink", 192.0), ("Night Walk", 3725.0)])
        self.assertIn("Music (YouTube Audio Library):\n• 0:00 Night Walk\n• 3:12 Late Ink\n"
                      "• 1:02:05 Night Walk", desc)

    def test_about_this_drawing_only_for_a_known_character(self):
        from reels_ai import about
        from reels_ai.longform import youtube_text
        known = lambda s, sc, t: {"known": True, "note": "Afro is the lead of Afro Samurai, a manga by Takashi Okazaki."}
        unsure = lambda s, sc, t: {"known": False, "note": "Maybe someone?"}
        self.assertIn("Takashi Okazaki", about.about("Afro Samurai", ask=known))
        self.assertEqual(about.about("my own guy", ask=unsure), "")
        self.assertEqual(about.about("Afro Samurai", ask=lambda *a: 1 / 0), "")          # a failure: no note
        _, desc, _ = youtube_text("Afro Samurai", 5, "0:00 Intro\n0:05 Afro\n9:00 Ink", {}, 4.0, 3600,
                                  note="Afro is the lead of Afro Samurai.")
        self.assertLess(desc.index("About this drawing\nAfro is the lead"), desc.index("Chapters"))
        old = "LongDraw EP. 01\n\nShinobi: 2 hours\n\nI'm a comic artist."
        got = about.with_about(old, "Note.")
        self.assertEqual(got, "LongDraw EP. 01\n\nShinobi: 2 hours\n\nAbout this drawing\nNote.\n\nI'm a comic artist.")
        self.assertEqual(about.with_about(got, "Other."), got)                            # once only
