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
    def test_about_this_drawing_known_names_and_your_own_words(self):
        from reels_ai import about
        known = lambda s, sc, t: {"known": True, "subject": "Afro", "series": "Afro Samurai",
                                  "note": "Afro is the lead of Afro Samurai, a manga by Takashi Okazaki."}
        unsure = lambda s, sc, t: {"known": False, "subject": "", "series": "", "note": ""}
        self.assertEqual(about.lookup("Afro Samurai", ask=known)["series"], "Afro Samurai")
        self.assertIsNone(about.lookup("ink study", ask=unsure))
        self.assertIsNone(about.lookup("Afro Samurai", ask=lambda *a: 1 / 0))              # a failure: asked later
        polished = about.polish("ink study", "hands from a berserk panel",
                                ask=lambda s, sc, t: {"subject": "an ink study of hands", "series": "Berserk",
                                                      "note": "I studied the hands from a Berserk panel."})
        self.assertEqual(polished["subject"], "an ink study of hands")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            self.assertEqual(about.polish("ink study", "just practice")["note"], "just practice")


class AskWhatItIsTest(unittest.TestCase):
    def runner(self):
        b = FakeBot()
        state = fresh_state()
        state["owner_chat_id"] = OWNER
        state["report_week"] = state["audit_week"] = "2026-W47"
        state["radar_day"] = "2026-11-18"
        return b, Runner(b, state, load_config(), clock=Clock(NOW))

    def test_an_unknown_name_is_asked_about_and_the_build_starts_after_the_answer(self):
        from reels_ai import about
        b, r = self.runner()
        started = []
        r.start_longform = started.append
        with mock.patch.object(Runner, "route", return_value={"first": "gemini", "gemini_key": "k", "claude": False}), \
                mock.patch.object(about, "lookup", return_value=None), \
                mock.patch.object(about, "polish", return_value={"subject": "an ink study of hands", "series": "",
                                                                 "note": "I studied hands in ink."}):
            self.assertIsNone(r.about_or_ask("ink study", {"longform": "ink study"}))
            self.assertEqual(started, [])
            ask = [t for k, _, t in b.sent if k == "text"][-1]
            self.assertIn("What are you drawing in “ink study”", ask)
            asked_id = b.next_msg
            b.push(**msg(OWNER, text="hands, just practice", reply_to_message={"message_id": asked_id}))
            r.poll()
        self.assertEqual(started, ["ink study"])
        self.assertEqual(r.state["about_notes"]["inkstudy"]["note"], "I studied hands in ink.")
        self.assertEqual(r.about_or_ask("Ink Study", {"longform": "x"})["subject"], "an ink study of hands")  # remembered

    def test_a_known_name_starts_right_away(self):
        from reels_ai import about
        b, r = self.runner()
        with mock.patch.object(Runner, "route", return_value={"first": "gemini", "gemini_key": "k", "claude": False}), \
                mock.patch.object(about, "lookup", return_value={"subject": "Sasuke Uchiha", "series": "Naruto",
                                                                 "note": "Sasuke is from Naruto."}):
            self.assertEqual(r.about_or_ask("Sasuke", {"longform": "Sasuke"})["series"], "Naruto")
        self.assertFalse([t for k, _, t in b.sent if "What are you drawing" in t])


class StatusSpendTest(unittest.TestCase):
    def test_status_shows_this_months_claude_spend(self):
        import time
        b = FakeBot()
        state = fresh_state()
        state["claude_spend"] = {time.strftime("%Y-%m"): 3.4, "2020-01": 99}
        r = Runner(b, state, load_config(), clock=Clock(NOW))
        self.assertIn("Claude this month: $3.40 of $15 ($11.60 left", r.status_text())


class BusyRepliesTest(unittest.TestCase):
    def test_messages_during_a_long_render_get_a_busy_reply_and_stay_waiting(self):
        import threading
        import time as _t
        b = FakeBot()
        b.peek_updates = lambda offset: [u for u in b.updates if u["update_id"] >= offset]
        answered = []
        b.answer_callback = lambda cid, text="": answered.append(text)
        state = fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(b, state, load_config(), clock=Clock(NOW))
        r.rendering = {"label": "Vegeta", "since": _t.time() - 600}
        b.push(**msg(OWNER, text="/redo Vegeta"))                         # changes Shorts: waits
        b.push(callback_query={"id": "cb", "from": {"id": OWNER}, "data": "lu:v1", "message": {"message_id": 1}})
        stop = threading.Event()
        t = threading.Thread(target=r.busy_replies, args=(stop, 0.01))
        t.start()
        _t.sleep(0.2)
        stop.set()
        t.join()
        busy = [x for k, _, x in b.sent if k == "text" and "making the Shorts for Vegeta" in x]
        self.assertEqual(len(busy), 1)                                   # once per message, not every look
        self.assertIn("started 10 min ago", busy[0])
        self.assertEqual(answered, ["Busy making Vegeta; I'll do this right after"])
        self.assertEqual(state["update_offset"], 0)                      # still waiting to be handled


    def test_quick_commands_are_answered_while_busy_and_only_once(self):
        import threading
        import time as _t
        b = FakeBot()
        b.peek_updates = lambda offset: [u for u in b.updates if u["update_id"] >= offset]
        state = fresh_state()
        state["owner_chat_id"] = OWNER
        state["report_week"] = state["audit_week"] = "2026-W47"
        state["radar_day"] = "2026-11-18"
        r = Runner(b, state, load_config(), clock=Clock(NOW))
        r.rendering = {"label": "Vegeta", "since": _t.time() - 60}
        b.push(**msg(OWNER, text="/status"))
        b.push(**msg(OWNER, text="/redo Vegeta"))
        stop = threading.Event()
        t = threading.Thread(target=r.busy_replies, args=(stop, 0.01))
        t.start()
        _t.sleep(0.2)
        stop.set()
        t.join()
        texts = lambda: [x for k, _, x in b.sent if k == "text"]
        self.assertEqual(sum("Running" in x for x in texts()), 1)            # /status answered now
        self.assertEqual(sum("making the Shorts for Vegeta" in x for x in texts()), 1)   # /redo waits
        self.assertEqual(state["handled_early"], [1])
        r.rendering = None
        with mock.patch.object(Runner, "redo") as redo:
            r.poll()                                                         # the drawing is done
        self.assertEqual(sum("Running" in x for x in texts()), 1)            # /status not answered twice
        redo.assert_called_once()
        self.assertEqual(state["handled_early"], [])
        self.assertEqual(state["update_offset"], 3)


class SmallFixesTest(unittest.TestCase):
    def test_renders_only_pause_for_a_drop_on_posting_days(self):
        cfg = load_config()
        cfg["delivery"].update(mode="drops", drop_days=["Mon", "Wed", "Fri"], drop_times=["12:00"])
        tue = datetime(2026, 11, 17, 16, 50, tzinfo=timezone.utc)       # 11:50 AM in New York, a Tuesday
        wed = datetime(2026, 11, 18, 16, 50, tzinfo=timezone.utc)
        self.assertIsNone(Runner(FakeBot(), fresh_state(), cfg, clock=Clock(tue)).minutes_to_drop())
        self.assertAlmostEqual(Runner(FakeBot(), fresh_state(), cfg, clock=Clock(wed)).minutes_to_drop(), 10, 0)

    def test_status_tells_you_to_lock_the_bot_when_the_owner_secret_is_missing(self):
        state = fresh_state()
        state["owner_chat_id"] = OWNER
        with mock.patch.dict(os.environ, {"TELEGRAM_OWNER_ID": ""}):
            r = Runner(FakeBot(), state, load_config(), clock=Clock(NOW))
            self.assertIn(f"TELEGRAM_OWNER_ID = {OWNER}", r.status_text())

    def test_the_schedule_switches_by_itself_on_january_first(self):
        from datetime import date
        from reels_ai import descriptions
        cfg = load_config()
        self.assertIn("every Friday", descriptions.schedule_block(cfg, date(2026, 12, 31)))
        self.assertIn("Wednesday and Friday", descriptions.schedule_block(cfg, date(2027, 1, 1)))
        self.assertIn("Wed · Fri", descriptions.scheduled(cfg["panels"], date(2026, 12, 26))[1])
        self.assertIn("every Friday", descriptions.scheduled(cfg["panels"], date(2026, 12, 20))[1])


class AvoidWordsTest(unittest.TestCase):
    def test_stroke_never_makes_it_on_screen(self):
        from reels_ai import hooks
        from reels_ai.ai import STYLE_BRIEFS, _prompt
        self.assertFalse(hooks.honest("Wait for the final strokes", True))
        self.assertFalse(hooks.honest("Every stroke counts", True))
        self.assertTrue(hooks.honest("Wait for the last line", True))
        self.assertFalse(any("stroke" in h.lower() for hs in hooks.HOOKS.values() for h in hs))
        self.assertFalse(any("stroke" in b.lower() for b in STYLE_BRIEFS.values()))
        prompt = _prompt("Gojo", ["last_lines"], True)
        self.assertIn('"stroke"', prompt)
        self.assertIn("double meaning", prompt)
        for line in ("Stay for the finish", "Watch every line go down", "Watch Gojo come alive",
                     "The most satisfying part"):
            self.assertFalse(hooks.honest(line, True), line)
        built_in = [h for hs in hooks.HOOKS.values() for h in hs] + hooks.CAPTIONS + hooks.END_CARDS \
            + hooks.NO_PENCIL_HOOKS
        self.assertEqual([h for h in built_in if hooks.avoided(h)], [])      # none of ours trip the list

    def test_banked_shorts_with_the_word_are_listed_once(self):
        b = FakeBot()
        state = fresh_state()
        state["owner_chat_id"] = OWNER
        state["drawings"] = {"d1": {"subject": "Gojo", "status": "rendered"}}
        state["variants"] = {"v1": {"drawing": "d1", "style": "last_lines", "status": "approved",
                                    "hook": "Wait for the final strokes"},
                             "v2": {"drawing": "d1", "style": "rewind", "status": "posted",
                                    "hook": "Every stroke"}}
        r = Runner(b, state, load_config(), clock=Clock(NOW))
        r.avoid_notice()
        r.avoid_notice()
        notes = [t for k, _, t in b.sent if "banned" in t]
        self.assertEqual(len(notes), 1)
        self.assertIn("1 Short(s)", notes[0])
        self.assertIn("/redo Gojo", notes[0])
