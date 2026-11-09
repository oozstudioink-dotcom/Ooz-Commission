"""Bot flow tests against a fake Telegram. Run: python -m unittest discover -s tests

Set REELS_TEST_VIDEO=/path/to/timelapse.mov to also run the real render step.
"""
from __future__ import annotations

import json
import re

import cv2
import numpy as np
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

    def send_media_group(self, chat_id, videos, caption=""):
        out = []
        for i, v in enumerate(videos):
            m = self.send_video(chat_id, v, caption=caption if i == 0 else "")
            out.append(m)
        self.sent.append(("album", chat_id, caption))
        return out

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


def platforms_config():
    """The per-platform schedule with approval buttons (the drops feed has its own tests)."""
    cfg = load_config()
    cfg["delivery"]["mode"] = "platforms"
    cfg["approval"]["mode"] = "approve_or_auto"
    cfg["variants"]["per_drawing"] = 2
    cfg["platforms"]["tiktok"]["enabled"] = False
    return cfg


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
        self.cfg = platforms_config()
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


class DropsFeedTest(FakeVariantsMixin, unittest.TestCase):
    """Default setup: auto-approve into a bank, shuffled manual drops, nothing auto-posted."""

    def setUp(self):
        os.environ.pop("TELEGRAM_OWNER_ID", None)
        self.cfg = load_config()
        self.cfg["timezone"] = "UTC"
        self.clock = Clock(datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc))
        self.bot, self.state = FakeBot(), fresh_state()
        self.r = Runner(self.bot, self.state, self.cfg, clock=self.clock,
                        clients={"instagram": FakeInstagram()})          # must stay unused
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        self.bot.push(**msg(OWNER, text="hi"))
        for i, name in enumerate(["Naruto", "Deku", "Goku"]):
            fid = f"raw{i}"
            self.bot.files[fid] = src
            self.bot.push(**msg(OWNER, video={"file_id": fid, "file_unique_id": f"u{i}", "file_size": 1000},
                                caption=name))
        self.r.poll()
        self.stub_render(self.r)

    def drops(self):
        return [p for k, _, p in self.bot.sent if k == "video" and p.startswith("\U0001f3ac Drop")]

    def test_variants_land_in_bank_as_albums(self):
        self.assertEqual(len(self.state["variants"]), 15)              # 3 drawings x 5 styles
        self.assertTrue(all(v["status"] == "approved" for v in self.state["variants"].values()))
        albums = [p for k, _, p in self.bot.sent if k == "album"]
        self.assertEqual(len(albums), 3)
        self.assertIn("5 variants added", albums[0])
        self.assertFalse(any(m for m in self.bot.markups.values()))   # no approval buttons

    def test_shuffled_drops_never_repeat_and_never_auto_post(self):
        order = []
        for day in range(8):
            self.clock.t = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc) + timedelta(days=day)
            self.r.poll()
            self.r.poll()                                             # extra runs never double-drop
        dropped = sorted((v for v in self.state["variants"].values() if v.get("dropped_at")),
                         key=lambda v: v["drop_no"])
        self.assertEqual(len(dropped), 15)                            # 2/day until the bank is empty
        ids = [(v["drawing"], v["style"]) for v in dropped]
        self.assertEqual(len(set(ids)), 15)                           # never the same variant twice
        self.assertTrue(all(a[0] != b[0] for a, b in zip(ids, ids[1:])), ids)   # no drawing back-to-back
        self.assertEqual(len(self.drops()), 15)
        self.assertIn("trending sound", self.drops()[0])
        self.assertEqual(self.r.clients["instagram"].posted, [])      # nothing auto-posted
        self.assertTrue(any("running low" in t for t in self.bot.texts()))

    def test_both_drops_at_once(self):
        self.cfg["delivery"]["drop_times"] = ["17:00", "17:00"]
        self.clock.t = datetime(2026, 11, 7, 16, 59, tzinfo=timezone.utc)
        self.r.poll()
        self.assertEqual(len(self.drops()), 0)
        self.clock.t = self.clock.t.replace(hour=17, minute=1)
        self.r.poll()
        self.r.poll()
        self.assertEqual(len(self.drops()), 2)

    def test_drop_times(self):
        self.cfg["delivery"]["drop_times"] = ["17:00", "19:00"]
        self.clock.t = datetime(2026, 11, 7, 16, 59, tzinfo=timezone.utc)
        self.r.poll()
        self.assertEqual(len(self.drops()), 0)
        self.clock.t = self.clock.t.replace(hour=17, minute=1)
        self.r.poll()
        self.assertEqual(len(self.drops()), 1)
        self.clock.t = self.clock.t.replace(hour=23)
        self.r.poll()
        self.assertEqual(len(self.drops()), 2)
        self.bot.push(**msg(OWNER, text="/queue"))
        self.r.poll()
        self.assertTrue(any("Drops: 13 videos" in t for t in self.bot.texts()))


class RedoTest(DropsFeedTest):
    """/redo re-makes a drawing's Shorts; unsent old versions leave the bank, sent ones stay."""

    def test_redo_replaces_unsent_versions(self):
        self.clock.t = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
        self.r.poll()                                                 # 2 drops go out
        naruto = next(did for did, d in self.state["drawings"].items() if d["subject"] == "Naruto")
        sent_before = sum(1 for v in self.state["variants"].values()
                          if v["drawing"] == naruto and v.get("dropped_at"))
        self.bot.push(**msg(OWNER, text="/redo naru"))
        self.assertTrue(self.r.poll())                                # rendering needed
        extra = len(self.cfg["variants"]["styles"]) - 5                 # styles it never had
        self.assertTrue(any("Re-making Naruto" in t and f"{5 - sent_before + extra} styles" in t
                            for t in self.bot.texts()))           # unsent + the styles it never had
        self.assertEqual(self.state["drawings"][naruto]["status"], "queued")
        self.stub_render(self.r)
        mine = [v for v in self.state["variants"].values() if v["drawing"] == naruto]
        self.assertEqual(sum(v["status"] == "replaced" for v in mine), 5 - sent_before)
        self.assertEqual(sum(v["status"] == "approved" and not v.get("dropped_at") for v in mine),
                         5 - sent_before + extra)
        self.assertEqual(self.r.bank()["drops"], 15 - 2 + extra)      # swapped one for one, + new styles

    def test_redo_unknown_name_lists_drawings(self):
        self.bot.push(**msg(OWNER, text="/redo Luffy"))
        self.r.poll()
        self.assertTrue(any("No finished drawing" in t for t in self.bot.texts()))
        self.assertTrue(any("Deku" in t and "Goku" in t for t in self.bot.texts()))

    def test_redo_without_name_takes_newest(self):
        self.bot.push(**msg(OWNER, text="/redo"))
        self.r.poll()
        self.assertTrue(any("Re-making Goku" in t for t in self.bot.texts()))


class StyleLearningTest(unittest.TestCase):
    def test_winning_styles_come_round_more_and_weakest_is_skipped(self):
        cfg = load_config()
        r = Runner(FakeBot(), fresh_state(), cfg, clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)))
        self.assertEqual(len(r.choose_styles("d1")), 5)
        r.state["style_scores"] = {"process_cuts": {"avg_views": 3000, "posts": 4},
                                   "speedrun": {"avg_views": 200, "posts": 3},
                                   "reveal_first": {"avg_views": 900, "posts": 2},
                                   "ink_only": {"avg_views": 5000, "posts": 1}}     # 1 post: ignored
        r.state["style_uses"] = {s: 4 for s in cfg["variants"]["styles"]}
        picked = r.choose_styles("d9")
        self.assertNotIn("speedrun", picked)
        self.assertEqual(picked[0], "process_cuts")
        self.assertNotIn("ink_only", r.style_weights())


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

    def publish(self, video, variant, settings):
        return self.publish_reel(video, variant["caption"], settings.get("share_to_feed", False))

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
        cfg = platforms_config()
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


class YouTubeClientTest(unittest.TestCase):
    """The real YouTube client against a local fake of Google's endpoints."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse

        self.calls = calls = []
        stored_privacy = self.stored_privacy = ["private"]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def do_POST(self):
                u, body = urlparse(self.path), self._body()
                if u.path == "/token":
                    q = parse_qs(body.decode())
                    calls.append(("TOKEN", q["refresh_token"][0], q["grant_type"][0]))
                    return self._send({"access_token": "AT"})
                if u.path == "/upload":
                    calls.append(("START", parse_qs(u.query), json.loads(body), self.headers["Authorization"],
                                  self.headers["X-Upload-Content-Length"]))
                    return self._send({}, headers={"Location": f"http://127.0.0.1:{srv.server_port}/session1"})
                self._send({"error": {"message": "unexpected"}}, 400)

            def do_PUT(self):
                body = self._body()
                calls.append(("PUT", self.path, len(body), self.headers["Content-Type"]))
                self._send({"id": "VID1", "status": {"privacyStatus": stored_privacy[0]}})

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                calls.append(("GET", u.path, q))
                if u.path == "/api/channels":
                    return self._send({"items": [{"id": "UC1", "snippet": {"title": "Ooz Studio Ink"},
                                                  "statistics": {"subscriberCount": "19", "videoCount": "4"},
                                                  "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}}}]})
                if u.path == "/analytics":
                    return self._send({"columnHeaders": [{"name": "views"}, {"name": "subscribersGained"}],
                                       "rows": [[812, 2]]})
                self._send({"error": {"message": "nope"}}, 404)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai.youtube import YouTube
        base = f"http://127.0.0.1:{srv.server_port}"
        self.yt = YouTube(" cid ", "secret\n", " 1//refresh ")
        self.yt.token_url, self.yt.api = f"{base}/token", f"{base}/api"
        self.yt.upload_url, self.yt.analytics_url = f"{base}/upload", f"{base}/analytics"

    def test_upload_flow_and_private_lock_note(self):
        vid = Path(tempfile.mkdtemp()) / "v.mp4"
        vid.write_bytes(b"x" * 4321)
        variant = {"hook": "Wait for the ink", "subject": "Deku",
                   "caption": "Inking Deku by hand ✍\U0001f3fe\n\n#mha #inking #mangaart"}
        res = self.yt.publish(vid, variant, {"privacy": "public", "category_id": 1})
        self.assertEqual(res["id"], "VID1")
        self.assertEqual(res["permalink"], "https://youtube.com/shorts/VID1")
        self.assertIn("PRIVATE", res["note"])
        token, start, put = self.calls[0], self.calls[1], self.calls[2]
        self.assertEqual(token, ("TOKEN", "1//refresh", "refresh_token"))   # whitespace stripped
        self.assertEqual(start[1]["uploadType"], ["resumable"])
        meta = start[2]
        self.assertEqual(meta["snippet"]["title"], "Wait for the ink | Deku manga inking #Shorts")
        self.assertEqual(meta["snippet"]["tags"], ["mha", "inking", "mangaart"])
        self.assertEqual(meta["status"]["privacyStatus"], "public")
        self.assertFalse(meta["status"]["selfDeclaredMadeForKids"])
        self.assertEqual(start[3], "Bearer AT")
        self.assertEqual(start[4], "4321")
        self.assertEqual(put, ("PUT", "/session1", 4321, "video/mp4"))

    def test_public_when_allowed_has_no_note(self):
        self.stored_privacy[0] = "public"
        vid = Path(tempfile.mkdtemp()) / "v.mp4"
        vid.write_bytes(b"x")
        res = self.yt.publish(vid, {"hook": "h", "subject": None, "caption": "c"}, {})
        self.assertIsNone(res["note"])

    def test_whoami_and_insights(self):
        ch = self.yt.whoami()
        self.assertEqual((ch["title"], ch["subscribers"], ch["uploads"]), ("Ooz Studio Ink", "19", "UU1"))
        self.assertEqual(self.yt.insights("VID1"), {"views": 812, "subscribersGained": 2})
        q = [c for c in self.calls if c[0] == "GET" and c[1] == "/analytics"][0][2]
        self.assertEqual(q["filters"], ["video==VID1"])
        self.assertEqual(q["ids"], ["channel==MINE"])

    def test_title_trimmed_to_100(self):
        from reels_ai.youtube import shorts_title
        t = shorts_title("x" * 200, "Naruto")
        self.assertEqual(len(t), 100)
        self.assertTrue(t.endswith("| Naruto manga inking #Shorts"))


class TikTokClientTest(unittest.TestCase):
    """The real TikTok client against a local fake of TikTok's endpoints."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import parse_qs, urlparse

        self.calls = calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def do_POST(self):
                u, body = urlparse(self.path), self._body()
                if u.path == "/v2/oauth/token/":
                    q = parse_qs(body.decode())
                    calls.append(("TOKEN", q["grant_type"][0], q.get("refresh_token", q.get("code"))[0]))
                    return self._send({"access_token": "act", "refresh_token": "rft-NEW", "open_id": "o1"})
                j = json.loads(body or b"{}")
                calls.append(("POST", u.path, j, self.headers["Authorization"]))
                ok = {"error": {"code": "ok", "message": ""}}
                up = f"http://127.0.0.1:{srv.server_port}/up/1"
                if u.path.endswith("/inbox/video/init/") or u.path.endswith("/post/publish/video/init/"):
                    return self._send({"data": {"publish_id": "p1", "upload_url": up}, **ok})
                if u.path.endswith("/creator_info/query/"):
                    return self._send({"data": {"privacy_level_options": ["SELF_ONLY"]}, **ok})
                if u.path.endswith("/status/fetch/"):
                    return self._send({"data": {"status": "SEND_TO_USER_INBOX"}, **ok})
                self._send({"error": {"code": "not_found", "message": u.path}})

            def do_PUT(self):
                body = self._body()
                calls.append(("PUT", self.path, len(body), self.headers["Content-Range"]))
                self.send_response(201)
                self.end_headers()

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai.tiktok import TikTok
        self.tt = TikTok(" key ", "sec\n", " rft-OLD ")
        self.tt.api = f"http://127.0.0.1:{srv.server_port}"
        self.vid = Path(tempfile.mkdtemp()) / "v.mp4"
        self.vid.write_bytes(b"x" * 777)

    def test_inbox_draft_flow(self):
        res = self.tt.publish(self.vid, {"caption": "Inking #manga"}, {"mode": "inbox"})
        self.assertEqual(res["id"], "p1")
        self.assertTrue(res["caption_for_user"])
        self.assertEqual(self.calls[0], ("TOKEN", "refresh_token", "rft-OLD"))
        init = self.calls[1]
        self.assertEqual(init[1], "/v2/post/publish/inbox/video/init/")
        self.assertEqual(init[2]["source_info"], {"source": "FILE_UPLOAD", "video_size": 777,
                                                  "chunk_size": 777, "total_chunk_count": 1})
        self.assertEqual(init[3], "Bearer act")
        self.assertEqual(self.calls[2], ("PUT", "/up/1", 777, "bytes 0-776/777"))
        self.assertEqual(self.tt.rotated_refresh_token, "rft-NEW")

    def test_direct_post_falls_back_to_allowed_privacy(self):
        self.tt.wait = lambda pid: {"status": "PUBLISH_COMPLETE"}
        res = self.tt.publish(self.vid, {"caption": "c"}, {"mode": "direct", "privacy": "PUBLIC_TO_EVERYONE"})
        init = [c for c in self.calls if c[0] == "POST" and c[1] == "/v2/post/publish/video/init/"][0]
        self.assertEqual(init[2]["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertIn("private", res["note"])

    def test_authorize_url(self):
        from reels_ai.tiktok import authorize_url
        u = authorize_url(" ck ", "https://x.github.io/cb.html")
        self.assertIn("client_key=ck", u)
        self.assertIn("redirect_uri=https%3A%2F%2Fx.github.io%2Fcb.html", u)
        self.assertIn("scope=user.info.basic%2Cvideo.upload%2Cvideo.publish", u)


class ReportTest(unittest.TestCase):
    def test_matches_posts_to_styles_and_ranks(self):
        from reels_ai.report import build, match_variant
        variants = [
            {"style": "reveal_first", "hook": "This started as a circle",
             "caption": "Inking Naruto by hand ✍\U0001f3fe Who should I draw next?\n\n#naruto #inking"},
            {"style": "ink_only", "hook": "Just the ink.",
             "caption": "Pencil to ink, start to finish. Which stage do you like more?\n\n#mangaart"},
        ]
        self.assertEqual(match_variant("Inking Naruto by hand ✍\U0001f3fe Who should I draw next? #naruto",
                                       variants)["style"], "reveal_first")
        self.assertIsNone(match_variant("My lunch today", variants))

        class IG:
            def whoami(self):
                return {"followers_count": 14}

            def recent_media(self, n):
                return [{"id": "a", "timestamp": "2026-11-05T18:00:00+0000", "media_product_type": "REELS",
                         "caption": "Inking Naruto by hand ✍\U0001f3fe Who should I draw next?"},
                        {"id": "b", "timestamp": "2026-11-06T18:00:00+0000", "media_product_type": "REELS",
                         "caption": "Pencil to ink, start to finish. Which stage do you like more?"}]

            def insights(self, mid):
                return {"views": 1600 if mid == "a" else 200, "ig_reels_avg_watch_time": 8800}

        state = {"variants": {"v1": variants[0], "v2": variants[1]}, "report_followers": {"instagram": 10}}
        text = build(state, {"instagram": IG()}, datetime(2026, 11, 8, 15, tzinfo=timezone.utc))
        self.assertIn("1,800 views", text)
        self.assertIn("(+4 this week)", text)
        self.assertIn("1. Reveal first: 1,600", text)
        self.assertIn("Reveal first is winning", text)
        self.assertEqual(state["report_followers"]["instagram"], 14)


class GeminiCopyTest(unittest.TestCase):
    def test_models_come_from_the_api_newest_first(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        names = ["gemini-3.1-flash", "gemini-flash-latest", "gemini-3.1-flash-lite", "gemini-2.5-flash-image",
                 "gemini-3-flash-preview", "gemini-flash-lite-latest", "text-embedding-005", "gemini-3.1-pro"]

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"models": [{"name": f"models/{n}", "supportedGenerationMethods":
                                               ["embedContent"] if "embedding" in n else ["generateContent"]}
                                              for n in names]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai import ai
        url = f"http://127.0.0.1:{srv.server_port}/models"
        got = ai.available_models("K", url=url)
        self.assertEqual(got, ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-3.1-pro",
                               "gemini-3.1-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview"])
        real = ai.MODELS_URL
        ai.available_models.__defaults__ = (url, 30)
        try:
            self.assertEqual(ai.candidate_models("K", "gemini-flash-latest", "gemini-3.1-flash")[:3],
                             ["gemini-3.1-flash", "gemini-flash-latest", "gemini-flash-lite-latest"])
        finally:
            ai.available_models.__defaults__ = (real, 30)

    def test_ping(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                good = self.headers["x-goog-api-key"] == "GOOD"
                body = (json.dumps({"candidates": [{"content": {"parts": [{"text": "ready"}]}}]}) if good
                        else json.dumps({"error": {"message": "API key not valid"}})).encode()
                self.send_response(200 if good else 400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai.ai import ping
        url = f"http://127.0.0.1:{srv.server_port}/{{model}}"
        self.assertEqual(ping(" GOOD\n", endpoint=url), "ready")
        with self.assertRaisesRegex(RuntimeError, "key looks wrong"):
            ping("BAD", endpoint=url)
        with self.assertRaisesRegex(RuntimeError, "no GEMINI_API_KEY"):
            ping("", endpoint=url)

    def test_write_copy_and_override(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen["key"] = self.headers["x-goog-api-key"]
                seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                reply = {"subject": "Naruto Uzumaki",
                         "hooks": {"reveal_first": "Ink brings him alive", "speedrun": "x" * 99,
                                   "bogus_style": "nope"},
                         "caption": "Naruto in pure ink ✍\U0001f3fe Who next? \U0001f447\U0001f3fe",
                         "hashtags": ["#Naruto", "mangaart", "#ink ing"]}
                body = json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai.ai import write_copy
        img = Path(tempfile.mkdtemp()) / "f.jpg"
        img.write_bytes(b"\xff\xd8jpeg")
        copy = write_copy(img, None, ["reveal_first", "speedrun"], " KEY\n",
                          endpoint=f"http://127.0.0.1:{srv.server_port}/{{model}}")
        self.assertEqual(seen["key"], "KEY")
        self.assertEqual(seen["body"]["contents"][0]["parts"][1]["inline_data"]["mime_type"], "image/jpeg")
        self.assertEqual(copy["hooks"], {"reveal_first": "Ink brings him alive"})   # junk dropped
        self.assertEqual(copy["hashtags"], ["#naruto", "#mangaart", "#inking"])
        self.assertEqual(copy["subject"], "Naruto Uzumaki")

        from reels_ai import variants
        from reels_ai.analyze import Analysis
        a = Analysis(n_frames=100, fps=30, width=720, height=1280, draw_start=0, sketch_idx=20, ink_start=30,
                     ink_end=90, peak_idx=60, reveal_idx=99, detail_box=(0.3, 0.2, 0.4, 0.4),
                     focus=(0.5, 0.5, 1.0), detail_start=40, detail_end=80, hand_score=[0] * 100,
                     ink_progress=[], window_frames=15, usable=[True] * 100)
        plans = {p.name: p for p in variants.build(a, ["reveal_first", "speedrun"], 1, "Naruto", copy)}
        self.assertEqual(plans["reveal_first"].hook, "Ink brings him alive")
        self.assertIn("Ink brings him alive", [c.text for c in plans["reveal_first"].texts])
        self.assertTrue(plans["speedrun"].caption.startswith("Naruto in pure ink"))
        self.assertTrue(plans["speedrun"].caption.endswith("#naruto #mangaart #inking"))
        self.assertNotEqual(plans["speedrun"].hook, "x" * 99)              # template kept

    def test_no_key_means_templates(self):
        from reels_ai.ai import write_copy
        self.assertIsNone(write_copy(Path("x.jpg"), "Naruto", ["speedrun"], ""))


class DriveTest(unittest.TestCase):
    """The real Drive client + proxy maker against a local fake of Google Drive."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlparse

        self.files = files = {"root": {"name": "My Drive", "folder": True, "parents": []}}
        self.calls = calls = []
        self.video = video = Path(os.environ.get(
            "REELS_TEST_VIDEO", "/root/.claude/uploads/e5bed814-3c70-560e-b2aa-4d61a14a2658/"
                                "4626688f-copy_090B8A3E-0DB0-41B2-B7D7-C69758B502D9.mov"))
        uploads = {}

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, obj, code=200, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

            def do_POST(self):
                u, body = urlparse(self.path), self._body()
                if u.path == "/token":
                    return self._send({"access_token": "AT"})
                assert self.headers["Authorization"] == "Bearer AT"
                if u.path == "/drive/files":                      # create folder
                    meta = json.loads(body)
                    fid = f"f{len(files)}"
                    files[fid] = {"name": meta["name"], "folder": True, "parents": meta.get("parents", ["root"])}
                    calls.append(("mkdir", meta["name"]))
                    return self._send({"id": fid})
                if u.path == "/upload":                           # start resumable upload
                    meta = json.loads(body)
                    sid = f"s{len(uploads)}"
                    uploads[sid] = {"meta": meta, "data": b"", "size": int(self.headers["X-Upload-Content-Length"])}
                    return self._send({}, headers={"Location": f"http://127.0.0.1:{srv.server_port}/session/{sid}"})
                self._send({"error": {"message": "?"}}, 404)

            def do_PUT(self):
                sid = self.path.rsplit("/", 1)[-1]
                up = uploads[sid]
                up["data"] += self._body()
                calls.append(("chunk", self.headers["Content-Range"]))
                if len(up["data"]) < up["size"]:
                    self.send_response(308)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                fid = f"f{len(files)}"
                files[fid] = {"name": up["meta"]["name"], "folder": False, "parents": up["meta"]["parents"],
                              "size": len(up["data"])}
                self._send({"id": fid, "webViewLink": f"https://drive/{fid}"})

            def do_PATCH(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                fid = u.path.rsplit("/", 1)[-1]
                self._body()
                files[fid]["parents"] = [q["addParents"][0]]
                calls.append(("move", files[fid]["name"], q["addParents"][0]))
                self._send({"id": fid})

            def do_GET(self):
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if self.headers.get("Authorization") != "Bearer AT":
                    return self._send({"error": {"message": "no auth"}}, 401)
                if u.path.startswith("/drive/files/") and q.get("alt") == ["media"]:
                    data = video.read_bytes()
                    rng = self.headers.get("Range")
                    start, end = 0, len(data) - 1
                    if rng:
                        a, b = rng.split("=")[1].split("-")
                        start, end = int(a), (int(b) if b else len(data) - 1)
                    self.send_response(206 if rng else 200)
                    self.send_header("Content-Type", "video/quicktime")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(end - start + 1))
                    if rng:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.end_headers()
                    self.wfile.write(data[start:end + 1])
                    return
                if u.path == "/drive/files":
                    query = q["q"][0]
                    import re as _re
                    parent = _re.search(r"'([^']+)' in parents", query).group(1)
                    name = _re.search(r"name = '([^']+)'", query)
                    out = [{"id": k, "name": v["name"], "mimeType": "application/vnd.google-apps.folder"
                            if v["folder"] else "video/quicktime", "size": str(v.get("size", 0)),
                            "videoMediaMetadata": {"durationMillis": "36500"}}
                           for k, v in files.items() if k != "root" and parent in v["parents"]
                           and (not name or v["name"] == name.group(1))
                           and (("mimeType = 'application/vnd.google-apps.folder'" in query) == v["folder"]
                                or "mimeType" not in query)]
                    return self._send({"files": out})
                self._send({"error": {"message": "nope"}}, 404)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai import drive as dmod
        self.dmod = dmod
        self.d = dmod.Drive(" cid ", "sec", " rt ")
        base = f"http://127.0.0.1:{srv.server_port}"
        self.d.token_url, self.d.api, self.d.upload_url = f"{base}/token", f"{base}/drive", f"{base}/upload"

    def test_layout_is_created_once(self):
        ids = self.d.ensure_layout()
        self.assertEqual(set(ids), set(self.dmod.LAYOUT))
        made = [c[1] for c in self.calls if c[0] == "mkdir"]
        self.assertEqual(made.count("Ooz Reels"), 1)
        self.assertEqual(made.count("Inbox"), 1)
        self.assertIn("Processed", made)
        fresh = self.dmod.Drive("cid", "sec", "rt")
        fresh.token_url, fresh.api = self.d.token_url, self.d.api
        self.assertEqual(fresh.folder("Inbox/Processed"), ids["Inbox/Processed"])   # found, not re-made

    def test_chunked_upload_and_move(self):
        self.dmod.CHUNK = 256 * 1024
        folder = self.d.folder("Done/Shorts/Naruto")
        f = Path(tempfile.mkdtemp()) / "v.mp4"
        f.write_bytes(os.urandom(256 * 1024 * 2 + 1000))
        res = self.d.upload(f, folder, "Naruto - Speedrun.mp4")
        self.assertEqual(self.files[res["id"]]["size"], f.stat().st_size)
        chunks = [c[1] for c in self.calls if c[0] == "chunk"]
        self.assertEqual(chunks, ["bytes 0-262143/525288", "bytes 262144-524287/525288",
                                  "bytes 524288-525287/525288"])
        self.d.move(res["id"], self.d.folder("Inbox"), folder)
        self.assertEqual(self.files[res["id"]]["parents"], [self.d.folder("Inbox")])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_proxy_streams_with_auth_both_modes(self):
        out = Path(tempfile.mkdtemp())
        url = self.d.media_url("raw1")
        long_ = self.dmod.make_proxy(url, self.d.token(), out / "k.mp4", duration_s=40 * 60)   # keyframes
        short = self.dmod.make_proxy(url, self.d.token(), out / "s.mp4", duration_s=36.5)       # sampled
        from reels_ai.media import probe
        k, s = probe(long_), probe(short)
        self.assertAlmostEqual(k["duration"], 40 / 30, delta=0.2)     # 40 keyframes at 30 fps
        self.assertGreater(s["duration"], 30)                         # ~1:1 for a short clip
        with self.assertRaises(self.dmod.DriveError):
            bad = self.dmod.Drive("c", "s", "r")
            bad.token_url, bad.api = self.d.token_url, self.d.api
            self.dmod.make_proxy(url, "WRONG", out / "x.mp4", duration_s=60)

    def test_bot_picks_up_inbox_once(self):
        inbox = self.d.folder("Inbox")
        self.files["raw1"] = {"name": "Naruto 2.MOV", "folder": False, "parents": [inbox]}
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        cfg = load_config()
        cfg["timezone"] = "UTC"
        r = Runner(bot, state, cfg, clock=lambda: datetime(2026, 11, 8, 9, tzinfo=timezone.utc), drive=self.d)
        self.assertTrue(r.poll())
        self.assertTrue(r.poll())
        drawings = list(state["drawings"].values())
        self.assertEqual(len(drawings), 1)                            # not picked up twice
        self.assertEqual((drawings[0]["subject"], drawings[0]["duration"]), ("Naruto", 36.5))
        self.assertEqual(sum("new recording" in t for t in bot.texts()), 1)


class ThumbnailTest(unittest.TestCase):
    def test_layout_and_youtube_size(self):
        from PIL import Image
        from reels_ai.thumbnail import BOXES, RED, make_thumbnail, youtube_jpeg
        out = Path(tempfile.mkdtemp())
        art = Image.new("RGB", (900, 800), (250, 250, 250))
        p = make_thumbnail(art, 3, out / "t.png")
        img = Image.open(p).convert("RGB")
        self.assertEqual(img.size, (1920, 1080))
        sx = 1920 / 2000
        ax0, ay0, ax1, ay1 = [int(v * sx) for v in BOXES["art"]]
        self.assertEqual(img.getpixel(((ax0 + ax1) // 2, (ay0 + ay1) // 2)), (250, 250, 250))
        dx0, dy0, dx1, dy1 = [int(v * sx) for v in BOXES["draw"]]
        reds = sum(1 for x in range(dx0, dx1, 7) for y in range(dy0, dy1, 7)
                   if img.getpixel((x, y)) == RED)
        self.assertGreater(reds, 500)                                  # DRAW painted in red
        jpg = youtube_jpeg(p, out / "t.jpg")
        self.assertLess(jpg.stat().st_size, 2_000_000)
        self.assertEqual(Image.open(jpg).size, (1280, 720))


class LongformTest(unittest.TestCase):
    def test_speed_episode_and_timestamps(self):
        from reels_ai.longform import choose_speed, episode_from_names, fmt_ts
        self.assertEqual(choose_speed(25 * 60, 20, 30), 1.0)          # already in range
        self.assertEqual(choose_speed(10 * 60, 20, 30), 1.0)          # never slowed down
        self.assertAlmostEqual(choose_speed(120 * 60, 20, 30) , 120 / 25)   # 2 h -> 25 min
        self.assertEqual(episode_from_names([], 3), 3)
        self.assertEqual(episode_from_names(["EP 03 - Shinobi", "EP 04 - Deku", "misc"], 3), 5)
        self.assertEqual(episode_from_names(["EP 01 - old"], 3), 3)
        self.assertEqual((fmt_ts(0), fmt_ts(195), fmt_ts(3725)), ("0:00", "3:15", "1:02:05"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_beat_tape_is_level_and_exact_length(self):
        import subprocess
        from reels_ai.longform import beat_tape
        d = Path(tempfile.mkdtemp())
        quiet, loud = d / "q.mp3", d / "l.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=12",
                        "-af", "volume=-26dB", str(quiet)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=12:c=pink:a=0.7",
                        str(loud)], check=True)
        out = beat_tape([quiet, loud], 30.0, d / "tape.m4a")
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                    "csv=p=0", str(out)], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(dur, 30.0, delta=0.15)
        levels = []
        for t in (3, 10, 17, 22):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-ss", str(t), "-t", "2", "-i", str(out), "-af",
                                "ebur128", "-f", "null", "-"], capture_output=True, text=True).stderr
            levels.append(float(re.findall(r"I:\s*(-?[\d.]+) LUFS", r)[-1]))
        self.assertLess(max(levels) - min(levels), 3.0, levels)        # -26 dB vs loud noise, levelled

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_scan_cuts_dead_time(self):
        import subprocess
        from reels_ai.longform import Session, scan
        d = Path(tempfile.mkdtemp())
        blank = d / "blank.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xd7e1e6:s=360x640:r=30:d=20",
                        "-pix_fmt", "yuv420p", str(blank)], check=True)
        src = Path(os.environ.get("REELS_TEST_VIDEO", "/root/.claude/uploads/e5bed814-3c70-560e-b2aa-4d61a14a2658/"
                                                     "4626688f-copy_090B8A3E-0DB0-41B2-B7D7-C69758B502D9.mov"))
        if not src.exists():
            self.skipTest("no sample video")
        mid = d / "mid.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-an", "-vf", "scale=360:640", "-r", "30",
                        "-pix_fmt", "yuv420p", str(mid)], check=True)
        (d / "l.txt").write_text(f"file '{blank}'\nfile '{mid}'\n")
        joined = d / "joined.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(d / "l.txt"),
                        "-c", "copy", str(joined)], check=True)
        s = scan(Session(str(joined), "Naruto.mp4", "Naruto", 56.5), d)
        self.assertTrue(s.portrait)
        self.assertGreater(s.keep[0][0], 15)                           # the 20 s blank start is cut
        self.assertLess(s.kept, 45)


class YouTubeChunkedUploadTest(unittest.TestCase):
    def test_resumes_from_reported_range(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        got = {"data": b"", "ranges": []}

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, headers=None):
                body = json.dumps(obj or {}).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.startswith("/token"):
                    return self._send(200, {"access_token": "AT"})
                if self.path.startswith("/thumb"):
                    got["thumb"] = (self.headers["Content-Type"], len(body))
                    return self._send(200, {})
                got["meta"] = json.loads(body)
                self._send(200, headers={"Location": f"http://127.0.0.1:{srv.server_port}/sess"})

            def do_PUT(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                got["ranges"].append(self.headers["Content-Range"])
                start = int(self.headers["Content-Range"].split()[1].split("-")[0])
                keep = body[: len(body) // 2] if len(got["ranges"]) == 1 else body    # first chunk: half stored
                got["data"] = got["data"][:start] + keep
                total = int(self.headers["Content-Range"].split("/")[1])
                if len(got["data"]) < total:
                    return self._send(308, headers={"Range": f"bytes=0-{len(got['data']) - 1}"})
                self._send(200, {"id": "LD3", "status": {"privacyStatus": "private"}})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1"
        from reels_ai.youtube import YouTube
        yt = YouTube("c", "s", "r")
        base = f"http://127.0.0.1:{srv.server_port}"
        yt.token_url, yt.upload_url = f"{base}/token", f"{base}/up/videos"
        f = Path(tempfile.mkdtemp()) / "big.mp4"
        payload = os.urandom(1000)
        f.write_bytes(payload)
        res = yt.upload_file(f, "LongDraw EP. 03 | Test", "desc", ["manga"], chunk=400)
        self.assertEqual(got["data"], payload)                         # resumed at the right byte
        self.assertEqual(got["ranges"][:2], ["bytes 0-399/1000", "bytes 200-599/1000"])
        self.assertEqual(res["id"], "LD3")
        self.assertIn("PRIVATE", res["note"])
        self.assertEqual(got["meta"]["snippet"]["title"], "LongDraw EP. 03 | Test")
        yt.upload_url = f"{base}/thumb/videos"
        jpg = f.with_suffix(".jpg")
        jpg.write_bytes(b"\xff\xd8" + b"0" * 50)
        yt.set_thumbnail("LD3", jpg)
        self.assertEqual(got["thumb"], ("image/jpeg", 52))


class LongformJobTest(unittest.TestCase):
    def test_job_glue(self):
        from reels_ai import longform_job as job

        tmp = Path(tempfile.mkdtemp())
        calls = {"uploads": [], "moves": [], "yt": None, "thumb": None, "built": None}

        class FakeDrive:
            def __init__(self, *a, **k):
                self.root_name = "Ooz Reels"

            def folder(self, path=""):
                return "id:" + path

            def list(self, fid, kind="video"):
                data = {
                    "id:Longform": [{"id": "p1", "name": "Shinobi Page", "mimeType": "folder"},
                                    {"id": "fin", "name": "Finished", "mimeType": "folder"}],
                    "p1": [{"id": "s2", "name": "Deku.MOV", "mimeType": "video/quicktime",
                            "createdTime": "2026-11-02", "videoMediaMetadata": {"durationMillis": "1800000"}},
                           {"id": "s1", "name": "Naruto.MOV", "mimeType": "video/quicktime",
                            "createdTime": "2026-11-01", "videoMediaMetadata": {"durationMillis": "3600000"}},
                           {"id": "a1", "name": "YT_Longform_Finished_Art_Panel.jpg", "mimeType": "image/jpeg"}],
                    "id:Done/Longform": [{"id": "e3", "name": "EP 03 - Old page", "mimeType": "folder"}],
                    "id:Beats": [{"id": "b1", "name": "lofi.mp3", "mimeType": "audio/mpeg"}],
                    "id:Side Panels": [{"id": "x", "name": "other.mov", "mimeType": "video/quicktime",
                                        "createdTime": "1"},
                                       {"id": "ad", "name": "Commissions_Advert.mov", "mimeType": "video/quicktime",
                                        "createdTime": "2"}],
                }[fid]
                if kind == "folder":
                    return [f for f in data if f["mimeType"] == "folder"]
                return [f for f in data if not kind or f["mimeType"].startswith(kind)]

            def token(self):
                return "AT"

            fresh_token = token

            def media_url(self, fid):
                return f"https://drive/{fid}"

            def download(self, fid, dst):
                Path(dst).write_bytes(b"x")
                return Path(dst)

            def upload(self, path, folder, name=None, mime="video/mp4"):
                calls["uploads"].append((folder, name or Path(path).name))
                return {"id": "u", "webViewLink": "https://drive/u"}

            def move(self, fid, to, frm):
                calls["moves"].append((fid, to))

        def fake_build(sessions, out_dir, project, episode, cfg, beats, advert, pcfg, art, log=print):
            calls["built"] = ([s.title for s in sessions], [s.src for s in sessions], episode, advert.name,
                              art.name, len(beats), sessions[0].hdrs())
            out_dir.mkdir(parents=True, exist_ok=True)
            for n in ("v.mp4", "t.png", "t.jpg"):
                (out_dir / n).write_bytes(b"x")
            return {"video": out_dir / "v.mp4", "thumbnail": out_dir / "t.png", "thumbnail_yt": out_dir / "t.jpg",
                    "chapters": "0:00 Intro\n0:03 Naruto\n12:40 Deku", "seconds": 1500, "speed": 3.6,
                    "kept": 5400, "raw": 5400}

        class FakeYT:
            def __init__(self, *a):
                pass

            def upload_file(self, path, title, desc, tags, privacy):
                calls["yt"] = (title, desc)
                return {"id": "Y", "permalink": "https://youtu.be/Y", "note": "PRIVATE until audit"}

            def set_thumbnail(self, vid, jpg):
                calls["thumb"] = vid

        import reels_ai.youtube as ytmod
        orig = (job.Drive, job.build, ytmod.YouTube, job.load_state)
        job.Drive, job.build, ytmod.YouTube = FakeDrive, fake_build, FakeYT
        job.load_state = lambda: {"owner_chat_id": None}
        os.environ.update(YT_CLIENT_ID="c", YT_CLIENT_SECRET="s", YT_REFRESH_TOKEN="r", LONGFORM_WORK=str(tmp))
        try:
            job.run("shinobi page")                                   # case-insensitive
        finally:
            job.Drive, job.build, ytmod.YouTube, job.load_state = orig
            for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN", "LONGFORM_WORK"):
                os.environ.pop(k, None)
        titles, srcs, episode, advert, art, nbeats, hdrs = calls["built"]
        self.assertEqual(titles, ["Naruto", "Deku"])                  # recording order
        self.assertEqual(srcs, ["https://drive/s1", "https://drive/s2"])
        self.assertEqual(episode, 4)                                  # after EP 03 in Done
        self.assertEqual(advert, "advert.mov")
        self.assertTrue(art.startswith("art"))
        self.assertEqual(nbeats, 1)
        self.assertEqual(hdrs, ["-headers", "Authorization: Bearer AT\r\n"])
        self.assertTrue(calls["yt"][0].startswith("LongDraw EP. 04 | Shinobi Page"))
        self.assertIn("0:03 Naruto", calls["yt"][1])
        self.assertIn("linktr.ee/oozstudioink", calls["yt"][1])
        self.assertEqual(calls["thumb"], "Y")
        self.assertIn(("p1", "id:Longform/Finished"), calls["moves"])
        self.assertEqual({f for f, _ in calls["uploads"]}, {"id:Done/Longform/EP 04 - Shinobi Page"})


class AdvertTest(unittest.TestCase):
    def test_loops_and_text_is_editable(self):
        from reels_ai.advert import RED, Advert
        a = Advert(656, 1080, 550)
        first, last = a.frame(0), a.frame(269)
        self.assertLess(float(np.abs(first.astype(int) - last.astype(int)).mean()), 1.0)   # seamless loop
        self.assertEqual(a.frame(int(3.5 * 30)).max(), 0)                                  # black gap
        offer = a.frame(int(6.3 * 30))
        reds = ((offer[..., 2] > 150) & (offer[..., 1] < 90)).sum()
        self.assertGreater(reds, 2000)                                                     # red offer card
        b = Advert(656, 1080, 550, offer=("FIRST 20 CLIENTS", "30% OFF"))
        self.assertFalse(np.array_equal(b.frame(int(6.3 * 30)), offer))

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_panel_plays_generated_advert(self):
        from reels_ai import panels
        d = Path(tempfile.mkdtemp())
        cards = panels.build_cards(328, 540, {"socials": [["X", "@a"]], "link": "x"}, None, d)
        self.assertEqual([k for k, _ in cards], ["video", "still"])
        out = panels.render_right(d / "r.mp4", 328, 540, 12.0, cards, rotate_s=6.0)
        cap = cv2.VideoCapture(str(out))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.assertEqual(n, 360)
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(1.8 * 30))
        ok, f = cap.read()
        self.assertTrue(ok and ((f[..., 2] > 150) & (f[..., 1] < 90)).sum() > 10)    # red dots playing


class MusicCreditsTest(unittest.TestCase):
    def test_track_names_and_description(self):
        from reels_ai.longform import track_name, youtube_text
        self.assertEqual(track_name(Path("003_Night_Walk - Jason Farnham.mp3")), "Night Walk - Jason Farnham")
        self.assertEqual(track_name(Path("Late Night Ink.mp3")), "Late Night Ink")
        title, desc, _ = youtube_text("Shinobi Page", 3, "0:00 Intro\n0:03 Naruto\n10:00 Deku",
                                      {"socials": [["X", "@OozStudioInk"]], "link": "linktr.ee/oozstudioink"},
                                      4.2, 7200, ["Night Walk - Jason Farnham", "Late Night Ink"])
        self.assertIn("Music (YouTube Audio Library):\n• Night Walk - Jason Farnham\n• Late Night Ink", desc)
        self.assertTrue(desc.rstrip().endswith("#oozstudioink"))
        long_desc = youtube_text("P", 3, "0:00 Intro", {}, 1, 60, [f"Track {i:03d} " + "x" * 60 for i in range(200)])[1]
        self.assertLessEqual(len(long_desc), 4900)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_beat_tape_reports_tracks_heard(self):
        import subprocess
        from reels_ai.longform import beat_tape
        d = Path(tempfile.mkdtemp())
        beats = []
        for i, f in enumerate((220, 330, 440)):
            p = d / f"{i:03d}_Beat_{i}.mp3"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f={f}:d=10", str(p)], check=True)
            beats.append(p)
        played = []
        beat_tape(beats, 12.0, d / "t.m4a", played=played)            # 12 s needs only 2 tracks
        self.assertEqual(len(played), 2)
        self.assertEqual(len(set(played)), 2)


class Shorts4KFlowTest(FakeVariantsMixin, unittest.TestCase):
    def test_links_done_notice_and_budget(self):
        class D:
            def __init__(self):
                self.ups = []

            def folder(self, p=""):
                return "id:" + p

            def move(self, *a):
                pass

            def upload(self, path, folder, name=None, mime="video/mp4"):
                self.ups.append((folder, name))
                return {"id": "u", "webViewLink": f"https://drive/{len(self.ups)}"}

        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        cfg = load_config()
        cfg["timezone"] = "UTC"
        cfg["shorts"]["max_render_minutes"] = 0          # budget: one drawing per run
        clock = Clock(datetime(2026, 11, 8, 9, tzinfo=timezone.utc))
        r = Runner(bot, state, cfg, clock=clock, drive=D())
        src = Path(tempfile.mkdtemp()) / "raw.mp4"
        src.write_bytes(b"x")
        r.drive_source = lambda d, tmp: src
        for i in range(2):
            state["drawings"][f"d{i + 1}"] = {"source": "drive", "drive_id": f"f{i}", "drive_name": f"Goku {i}.MOV",
                                              "subject": f"Goku {i}", "status": "queued"}
        state["next_id"] = 3
        self.stub_render(r)
        self.assertEqual(sum(d["status"] == "rendered" for d in state["drawings"].values()), 1)
        self.assertTrue(any("continue on the next run" in t for t in bot.texts()))
        self.assertTrue(all(v.get("full_link", "").startswith("https://drive/") for v in state["variants"].values()))
        self.assertEqual(r.drive.ups[0][0], "id:Done/Shorts/Goku 0")
        self.stub_render(r)                                # next run finishes the queue
        self.assertTrue(any(t.startswith("✅ All caught up") for t in bot.texts()))
        albums = [p for k, _, p in bot.sent if k == "album"]
        self.assertTrue(all("4K files saved to Drive" in a for a in albums))
        clock.t = clock.t.replace(hour=17, minute=5)
        r.poll()
        drops = [p for k, _, p in bot.sent if k == "video" and p.startswith("\U0001f3ac Drop")]
        self.assertEqual(len(drops), 2)
        self.assertTrue(all("4K file to post: https://drive/" in p for p in drops))


class UploadOnceAndStorageTest(FakeVariantsMixin, unittest.TestCase):
    class D:
        def __init__(self):
            self.moves, self.ups = [], []
            self.tree = {
                "id:Inbox": [{"id": "i1", "name": "Goku.MOV", "mimeType": "video/quicktime", "size": "4000000000"}],
                "id:Longform": [{"id": "p1", "name": "Shinobi Page", "mimeType": "folder"},
                                {"id": "fin", "name": "Finished", "mimeType": "folder"}],
                "p1": [{"id": "s1", "name": "Naruto.MOV", "mimeType": "video/quicktime", "size": "9000000000"}],
                "id:Inbox/Processed": [{"id": "x", "name": "old.MOV", "mimeType": "video/quicktime",
                                        "size": "60000000000"}],
                "id:Longform/Finished": [{"id": "f1", "name": "Old Page", "mimeType": "folder"}],
                "f1": [{"id": "y", "name": "a.MOV", "mimeType": "video/quicktime", "size": "20000000000"}],
            }

        def folder(self, p=""):
            return "id:" + p

        def list(self, fid, kind="video"):
            files = self.tree.get(fid, [])
            return [f for f in files if (f["mimeType"] == "folder") == (kind == "folder")]

        def about(self):
            return {"storageQuota": {"usage": "1200000000000", "limit": "5000000000000"}}

        def move(self, fid, to, frm):
            self.moves.append(fid)

        def upload(self, path, folder, name=None, mime="video/mp4"):
            self.ups.append(folder)
            return {"webViewLink": "https://drive/x"}

    def test_longform_sessions_become_shorts_without_moving(self):
        bot, state, d = FakeBot(), fresh_state(), self.D()
        state["owner_chat_id"] = OWNER
        cfg = load_config()
        cfg["timezone"] = "UTC"
        r = Runner(bot, state, cfg, clock=lambda: datetime(2026, 11, 9, 9, tzinfo=timezone.utc), drive=d)
        r.poll()
        r.poll()                                                         # second poll: nothing new
        drawings = {x["drive_id"]: x for x in state["drawings"].values()}
        self.assertEqual(set(drawings), {"i1", "s1"})                    # Finished folder ignored
        self.assertTrue(drawings["s1"]["keep_in_place"])
        self.assertEqual(sum("new recording" in t for t in bot.texts()), 1)
        self.assertIn("Longform › Shinobi Page: Naruto.MOV", [t for t in bot.texts() if "new recording" in t][0])
        src = Path(tempfile.mkdtemp()) / "raw.mp4"
        src.write_bytes(b"x")
        r.drive_source = lambda dd, tmp: src
        self.stub_render(r)
        self.assertEqual(d.moves, ["i1"])                                # only the Inbox file moved
        self.assertIn("id:Done/Shorts/Naruto", d.ups)

    def test_monthly_storage_note(self):
        bot, state, d = FakeBot(), fresh_state(), self.D()
        state["owner_chat_id"] = OWNER
        d.tree["id:Inbox"], d.tree["id:Longform"] = [], []
        cfg = load_config()
        cfg["timezone"] = "UTC"
        clock = Clock(datetime(2026, 11, 9, 9, tzinfo=timezone.utc))
        r = Runner(bot, state, cfg, clock=clock, drive=d)
        r.poll()
        r.poll()
        notes = [t for t in bot.texts() if "Monthly storage check" in t]
        self.assertEqual(len(notes), 1)
        self.assertIn("Processed (raw clips already made into Shorts): 60 GB", notes[0])
        self.assertIn("Finished (sessions already in a LongDraw): 20 GB", notes[0])
        self.assertIn("1.20 TB of 5 TB", notes[0])
        clock.t = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
        r.poll()
        self.assertEqual(sum("Monthly storage check" in t for t in bot.texts()), 2)    # next month again
        d.tree["id:Inbox/Processed"], d.tree["f1"] = [], []
        clock.t = datetime(2027, 1, 1, 9, tzinfo=timezone.utc)
        r.poll()
        self.assertEqual(sum("Monthly storage check" in t for t in bot.texts()), 2)    # under 50 GB: quiet
