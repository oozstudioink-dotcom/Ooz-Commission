"""Bot flow tests against a fake Telegram. Run: python -m unittest discover -s tests

Set REELS_TEST_VIDEO=/path/to/timelapse.mov to also run the real render step.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reels_ai.bot import Runner
from reels_ai.state import EMPTY, load_config

OWNER, STRANGER = 111, 999


class FakeBot:
    def __init__(self):
        self.updates: list[dict] = []
        self.sent: list[tuple] = []          # (kind, chat, payload)
        self.markups: dict[int, dict] = {}
        self.next_msg = 100
        self.files: dict[str, Path] = {}

    def push(self, **u):
        u["update_id"] = len(self.updates) + 1
        self.updates.append(u)

    def get_updates(self, offset):
        return [u for u in self.updates if u["update_id"] >= offset]

    def send_message(self, chat_id, text, reply_markup=None, force_reply=False):
        self.sent.append(("text", chat_id, text))
        self.next_msg += 1
        return {"message_id": self.next_msg}

    def send_video(self, chat_id, video, caption="", reply_markup=None, **kw):
        self.sent.append(("video", chat_id, caption))
        self.next_msg += 1
        fid = f"file{self.next_msg}"
        if isinstance(video, Path):                 # keep a copy: the renderer's temp dir is deleted
            keep = Path(tempfile.mkdtemp()) / video.name
            shutil.copy(video, keep)
            self.files[fid] = keep
        self.markups[self.next_msg] = reply_markup
        return {"message_id": self.next_msg, "video": {"file_id": fid}}

    def edit_markup(self, chat_id, message_id, reply_markup):
        self.markups[message_id] = reply_markup

    def answer_callback(self, cid, text=""):
        self.sent.append(("answer", None, text))

    def download(self, file_id, dst):
        shutil.copy(self.files[file_id], dst)
        return dst

    def texts(self):
        return [p for k, _, p in self.sent if k == "text"]


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


def msg(chat, **kw):
    return {"message": {"chat": {"id": chat, "type": "private"}, "from": {"id": chat}, **kw}}


def tap(data, message_id=0):
    return {"callback_query": {"id": "cb", "from": {"id": OWNER}, "data": data,
                               "message": {"message_id": message_id}}}


def fresh_state():
    return json.loads(json.dumps(EMPTY))


class FakeVariantsMixin:
    """Swap the heavy renderer for a stub so poll/approval logic tests run in milliseconds."""

    def stub_render(self, runner, n=2):
        import reels_ai.pipeline as pipeline

        tmp = Path(tempfile.mkdtemp())
        real = pipeline.make_variants

        def fake(src, out_root, names, seed=1, subject=None, **kw):
            out = Path(out_root) / "x"
            (out / "variants").mkdir(parents=True)
            res = []
            for name in names:
                (out / "variants" / f"{name}.mp4").write_bytes(b"fake")
                res.append({"name": name, "file": f"variants/{name}.mp4", "seconds": 10.0,
                            "hook": f"hook-{name}", "caption": f"caption-{name}"})
            return out, res

        pipeline.make_variants = fake
        try:
            runner.render(tmp)
        finally:
            pipeline.make_variants = real


class BotFlowTest(FakeVariantsMixin, unittest.TestCase):
    def setUp(self):
        os.environ.pop("TELEGRAM_OWNER_ID", None)
        self.cfg = load_config()
        self.cfg["timezone"] = "UTC"
        self.cfg["platforms"]["youtube"]["post_times"] = ["17:00"]
        self.cfg["platforms"]["instagram"]["enabled"] = False
        self.clock = Clock(datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc))
        self.bot = FakeBot()
        self.state = fresh_state()
        self.r = Runner(self.bot, self.state, self.cfg, clock=self.clock)
        self.src = Path(tempfile.mkdtemp()) / "in.mp4"
        self.src.write_bytes(b"raw")
        self.bot.files["raw1"] = self.src

    def send_video(self, chat=OWNER, caption="Naruto", fid="raw1"):
        self.bot.push(**msg(chat, video={"file_id": fid, "file_size": 5_000_000}, caption=caption))

    def test_first_sender_becomes_owner_and_strangers_are_ignored(self):
        self.bot.push(**msg(OWNER, text="hi"))
        self.bot.push(**msg(STRANGER, text="hi"))
        self.send_video(chat=STRANGER)
        self.assertFalse(self.r.poll())
        self.assertEqual(self.state["owner_chat_id"], OWNER)
        self.assertEqual(self.state["drawings"], {})
        self.assertIn("private bot", self.bot.sent[-1][2])

    def test_full_flow_approve_skip_autoapprove_and_post(self):
        self.bot.push(**msg(OWNER, text="hi"))
        self.send_video()
        self.assertTrue(self.r.poll())                       # needs render
        self.stub_render(self.r)
        vids = sorted(self.state["variants"])
        self.assertEqual(len(vids), 2)
        styles = {self.state["variants"][v]["style"] for v in vids}
        self.assertEqual(len(styles), 2)                     # two different styles
        v1, v2 = vids
        m1 = self.state["variants"][v1]["message_id"]
        self.assertEqual(self.bot.markups[m1]["inline_keyboard"][0][0]["callback_data"], f"ok:{v1}")

        # edit caption on v1, then approve it; leave v2 alone
        self.bot.push(**tap(f"ed:{v1}", m1))
        self.bot.push(**msg(OWNER, text="My own caption #manga"))
        self.bot.push(**tap(f"ok:{v1}", m1))
        self.r.poll()
        self.assertEqual(self.state["variants"][v1]["caption"], "My own caption #manga")
        self.assertEqual(self.state["variants"][v1]["status"], "approved")
        self.assertIn("Approved", self.bot.markups[m1]["inline_keyboard"][0][0]["text"])
        self.assertEqual(self.state["variants"][v2]["status"], "pending")
        self.assertFalse(any(k == "video" and "Time to post" in p for k, _, p in self.bot.sent))  # before 17:00

        # 17:05 -> slot open: v1 posted (manual), only one per day
        self.clock.t = self.clock.t.replace(hour=17, minute=5)
        self.r.poll()
        self.assertEqual(self.state["variants"][v1]["status"], "posted")
        self.assertIn("My own caption #manga", self.bot.texts())
        posts_today = sum(1 for k, _, p in self.bot.sent if k == "video" and "Time to post" in p)
        self.assertEqual(posts_today, 1)
        self.assertEqual(self.state["variants"][v2]["status"], "approved")   # 6h fallback passed at 15:00
        self.r.poll()
        self.assertEqual(sum(1 for k, _, p in self.bot.sent if k == "video" and "Time to post" in p), 1)

        # next day 17:05 -> v2 posts
        self.clock.t += timedelta(days=1)
        self.r.poll()
        self.assertEqual(self.state["variants"][v2]["status"], "posted")

    def test_skip_and_pause(self):
        self.bot.push(**msg(OWNER, text="hi"))
        self.send_video()
        self.r.poll()
        self.stub_render(self.r)
        v1, v2 = sorted(self.state["variants"])
        self.bot.push(**tap(f"no:{v1}"))
        self.bot.push(**msg(OWNER, text="/pause"))
        self.r.poll()
        self.assertEqual(self.state["variants"][v1]["status"], "skipped")
        self.clock.t += timedelta(hours=10)                  # past deadline + slot, but paused
        self.r.poll()
        self.assertEqual(self.state["variants"][v2]["status"], "pending")
        self.bot.push(**msg(OWNER, text="/resume"))
        self.r.poll()
        self.assertEqual(self.state["variants"][v2]["status"], "posted")
        self.assertEqual(self.state["variants"][v1]["status"], "skipped")

    def test_never_same_drawing_twice_in_a_row(self):
        self.bot.push(**msg(OWNER, text="hi"))
        self.send_video(caption="A")
        self.send_video(caption="B")
        self.r.poll()
        self.stub_render(self.r)
        for vid in list(self.state["variants"]):
            self.bot.push(**tap(f"ok:{vid}"))
        self.clock.t = self.clock.t.replace(hour=17, minute=1)
        order = []
        for day in range(4):
            self.r.poll()
            posted = [v for v in self.state["variants"].values() if v["status"] == "posted"]
            order.append(max(posted, key=lambda v: v["posts"]["youtube"]["at"])["drawing"])
            self.clock.t += timedelta(days=1)
        self.assertTrue(all(a != b for a, b in zip(order, order[1:])), order)

    def test_same_video_twice_is_skipped(self):
        for _ in range(2):
            self.bot.push(**msg(OWNER, video={"file_id": "raw1", "file_unique_id": "U1",
                                             "file_size": 1000}, caption="Naruto"))
        self.r.poll()
        self.assertEqual(len(self.state["drawings"]), 1)
        self.assertIn("already have", self.bot.texts()[-1])

    def test_too_big_file_rejected(self):
        self.bot.push(**msg(OWNER, video={"file_id": "x", "file_size": 50_000_000}))
        self.assertFalse(self.r.poll())
        self.assertIn("20 MB", self.bot.texts()[-1])


@unittest.skipUnless(os.environ.get("REELS_TEST_VIDEO"), "set REELS_TEST_VIDEO to run a real render")
class RealRenderTest(unittest.TestCase):
    def test_real_render(self):
        bot, state = FakeBot(), fresh_state()
        bot.files["raw"] = Path(os.environ["REELS_TEST_VIDEO"])
        r = Runner(bot, state, load_config(), clock=lambda: datetime(2026, 11, 7, 9, tzinfo=timezone.utc))
        bot.push(**msg(OWNER, video={"file_id": "raw", "file_size": 1}, caption="Naruto"))
        self.assertTrue(r.poll())
        r.render()
        self.assertEqual(len(state["variants"]), 2)
        for v in state["variants"].values():
            self.assertGreater(bot.files[v["file_id"]].stat().st_size, 100_000)
            self.assertEqual(v["status"], "pending")
            self.assertIsNotNone(v["deadline"])
        print([(v["style"], v["seconds"], v["hook"]) for v in state["variants"].values()])


class FakeInstagram:
    def __init__(self, fail=False):
        self.fail, self.posted, self.asked = fail, [], []

    def publish_reel(self, video, caption, share_to_feed=False):
        assert Path(video).exists()
        if self.fail:
            raise RuntimeError("boom")
        self.posted.append((caption, share_to_feed))
        return {"id": f"m{len(self.posted)}", "permalink": "https://instagram.com/reel/x"}

    def insights(self, media_id):
        self.asked.append(media_id)
        return {"views": 123, "shares": 4}


class InstagramPostingTest(FakeVariantsMixin, unittest.TestCase):
    def make(self, ig):
        cfg = load_config()
        cfg["timezone"] = "UTC"
        cfg["platforms"]["youtube"]["enabled"] = False
        cfg["platforms"]["instagram"].update(method="api", post_times=["19:00"])
        clock = Clock(datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc))
        bot, state = FakeBot(), fresh_state()
        r = Runner(bot, state, cfg, clock=clock, clients={"instagram": ig})
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        bot.files["raw1"] = src
        bot.push(**msg(OWNER, text="hi"))
        bot.push(**msg(OWNER, video={"file_id": "raw1", "file_size": 1000}, caption="Naruto"))
        r.poll()
        self.stub_render(r)
        for vid in list(state["variants"]):
            bot.push(**tap(f"ok:{vid}"))
        r.poll()
        clock.t = clock.t.replace(hour=19, minute=5)
        return r, bot, state, clock

    def test_api_post_publishes_and_collects_stats(self):
        ig = FakeInstagram()
        r, bot, state, clock = self.make(ig)
        r.poll()
        self.assertEqual(len(ig.posted), 1)
        self.assertFalse(ig.posted[0][1])                         # Reels tab only
        posted = [v for v in state["variants"].values() if v["status"] == "posted"]
        self.assertEqual(posted[0]["posts"]["instagram"]["id"], "m1")
        self.assertTrue(any("Posted to Instagram" in t for t in bot.texts()))
        clock.t += timedelta(hours=7)
        r.poll()
        self.assertIn("m1", ig.asked)
        self.assertEqual(posted[0]["posts"]["instagram"]["stats"]["views"], 123)

    def test_api_failure_falls_back_to_manual(self):
        r, bot, state, clock = self.make(FakeInstagram(fail=True))
        r.poll()
        v = [v for v in state["variants"].values() if v["status"] == "posted"][0]
        self.assertEqual(v["posts"]["instagram"]["method"], "manual")
        self.assertIn("boom", v["posts"]["instagram"]["error"])
        self.assertTrue(any(k == "video" and "Auto-post to Instagram Reels failed" in p for k, _, p in bot.sent))

    def test_queue_and_low_bank_alert(self):
        r, bot, state, clock = self.make(FakeInstagram())
        bot.push(**msg(OWNER, text="/queue"))
        r.poll()                                                  # posts 1 of 2 -> 1 left
        self.assertTrue(any("Video bank" in t and "Instagram" in t for t in bot.texts()))
        self.assertTrue(any("running low" in t for t in bot.texts()))
        n = sum("running low" in t for t in bot.texts())
        r.poll()
        self.assertEqual(sum("running low" in t for t in bot.texts()), n)   # once per day


class InstagramClientTest(unittest.TestCase):
    """The real client against a local fake of Meta's endpoints (resumable upload flow)."""

    def test_publish_reel_flow(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse

        import reels_ai.instagram as igmod

        calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urlparse(self.path)
                calls.append(("GET", u.path, parse_qs(u.query)))
                if u.path == "/c1":
                    self._send({"status_code": "FINISHED"})
                elif u.path == "/m9":
                    self._send({"permalink": "https://instagram.com/reel/abc"})
                else:
                    self._send({"error": {"message": "nope", "code": 100}}, 400)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                u = urlparse(self.path)
                if u.path == "/upload/c1":
                    calls.append(("UPLOAD", self.headers["Authorization"], self.headers["file_size"], len(body)))
                    return self._send({"success": True})
                q = parse_qs(body.decode())
                calls.append(("POST", u.path, q))
                if u.path == "/123/media":
                    self._send({"id": "c1", "uri": f"http://127.0.0.1:{srv.server_port}/upload/c1"})
                elif u.path == "/123/media_publish":
                    self._send({"id": "m9"})

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        ig = igmod.Instagram(" EAAtoken\n", "123")
        ig.host = f"http://127.0.0.1:{srv.server_port}"
        vid = Path(tempfile.mkdtemp()) / "v.mp4"
        vid.write_bytes(b"x" * 5000)
        res = ig.publish_reel(vid, "Caption ✍\U0001f3fe #manga")
        srv.shutdown()
        self.assertEqual(res, {"id": "m9", "permalink": "https://instagram.com/reel/abc"})
        create = calls[0][2]
        self.assertEqual(create["media_type"], ["REELS"])
        self.assertEqual(create["upload_type"], ["resumable"])
        self.assertEqual(create["share_to_feed"], ["false"])
        self.assertEqual(create["access_token"], ["EAAtoken"])   # whitespace stripped
        self.assertEqual(calls[1], ("UPLOAD", "OAuth EAAtoken", "5000", 5000))
        self.assertEqual(calls[-2][1], "/123/media_publish")
