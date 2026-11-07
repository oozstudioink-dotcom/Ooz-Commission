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
