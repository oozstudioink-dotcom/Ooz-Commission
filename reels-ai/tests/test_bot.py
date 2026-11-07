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
