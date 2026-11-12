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

    def delete_messages(self, chat_id, ids):
        self.deleted = getattr(self, "deleted", []) + list(ids)
        return max(ids) > getattr(self, "oldest_deletable", 0)

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


def sample_video() -> Path | None:
    """A real recording for the tests that need one: REELS_TEST_VIDEO, or the developer's local
    sample. None anywhere else (e.g. on GitHub), where those tests are skipped."""
    p = Path(os.environ.get("REELS_TEST_VIDEO", "/root/.claude/uploads/e5bed814-3c70-560e-b2aa-4d61a14a2658/"
                                                 "4626688f-copy_090B8A3E-0DB0-41B2-B7D7-C69758B502D9.mov"))
    try:
        return p if p.exists() else None
    except OSError:                      # e.g. no permission to look in that folder
        return None


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
        self.cfg["approval"]["mode"] = "auto"            # albums straight into the bank
        self.cfg["delivery"]["split"], self.cfg["variants"]["per_drawing"] = [], 5   # classic one-video drops
        self.cfg["delivery"]["drop_times"] = ["17:00", "17:00"]   # two a day, as written
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
        made = min(5, 5 - sent_before + extra)                          # capped at per_drawing
        self.assertTrue(any("Re-making Naruto" in t and f"{made} styles" in t
                            for t in self.bot.texts()))           # unsent + new styles, up to per_drawing
        self.assertEqual(self.state["drawings"][naruto]["status"], "queued")
        self.stub_render(self.r)
        mine = [v for v in self.state["variants"].values() if v["drawing"] == naruto]
        self.assertEqual(sum(v["status"] == "replaced" for v in mine), 5 - sent_before)
        self.assertEqual(sum(v["status"] == "approved" and not v.get("dropped_at") for v in mine), made)
        self.assertEqual(self.r.bank()["drops"], 15 - 2 - (5 - sent_before) + made)

    def test_redo_all_requeues_every_drawing_once(self):
        self.clock.t = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
        self.r.poll()                                                 # 2 drops go out
        sent = {d: sum(1 for v in self.state["variants"].values() if v["drawing"] == d and v.get("dropped_at"))
                for d in self.state["drawings"]}
        self.bot.push(**msg(OWNER, text="/redo all"))
        self.assertTrue(self.r.poll())
        self.assertTrue(all(d["status"] == "queued" for d in self.state["drawings"].values()))
        self.assertTrue(any("Re-making 3 drawing(s)" in t for t in self.bot.texts()))
        self.stub_render(self.r)
        live = [v for v in self.state["variants"].values() if v["status"] == "approved" and not v.get("dropped_at")]
        extra = len(self.cfg["variants"]["styles"]) - 5
        self.assertEqual(len(live), sum(min(5, 5 - n + extra) for n in sent.values()))   # capped at per_drawing
        pairs = [(v["drawing"], v["style"]) for v in self.state["variants"].values()
                 if v["status"] in ("approved", "posted")]
        self.assertEqual(len(pairs), len(set(pairs)))                 # no drawing+style twice

    def test_clear_deletes_back_from_the_command(self):
        self.bot.oldest_deletable = 1000                              # older ids: past 48 h
        self.bot.push(**msg(OWNER, text="/clear", message_id=1520))
        self.r.poll()
        self.assertIn(1520, self.bot.deleted)
        self.assertIn(1001, self.bot.deleted)
        self.assertNotIn(5, self.bot.deleted)                        # stops after refusals
        self.assertTrue(any("Clear history" in t for t in self.bot.texts()))

    def test_redo_unknown_name_lists_drawings(self):
        self.bot.push(**msg(OWNER, text="/redo Luffy"))
        self.r.poll()
        self.assertTrue(any("No finished drawing" in t for t in self.bot.texts()))
        self.assertTrue(any("Deku" in t and "Goku" in t for t in self.bot.texts()))

    def test_redo_matches_names_loosely(self):
        did = next(iter(self.state["drawings"]))
        self.state["drawings"][did]["drive_name"] = "Lets+Go+LIVE!+2026.mp4"
        self.bot.push(**msg(OWNER, text="/redo lets go"))
        self.r.poll()
        self.assertEqual(self.state["drawings"][did]["status"], "queued")

    def test_redo_without_name_takes_newest(self):
        self.bot.push(**msg(OWNER, text="/redo"))
        self.r.poll()
        self.assertTrue(any("Re-making Goku" in t for t in self.bot.texts()))


class NameTagsTest(FakeVariantsMixin, unittest.TestCase):
    def test_caption_tags_reach_the_renderer(self):
        import reels_ai.pipeline as pipeline
        bot, state = FakeBot(), fresh_state()
        r = Runner(bot, state, load_config(), clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)))
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        bot.files["raw0"] = src
        bot.push(**msg(OWNER, text="hi"))
        bot.push(**msg(OWNER, video={"file_id": "raw0", "file_unique_id": "u0", "file_size": 1000},
                       caption="Sasuke - ink only - continued"))
        r.poll()
        d = next(iter(state["drawings"].values()))
        self.assertEqual(d["subject"], "Sasuke")
        self.assertEqual(d["hints"], {"pencil": False, "continued": True})
        self.assertTrue(any("Sasuke \u00b7 ink only \u00b7 continued page" in t for t in bot.texts()))
        seen = {}
        real = pipeline.make_variants

        def spy(src, out_root, names, **kw):
            seen.update(kw)
            out = Path(out_root) / "x"
            (out / "variants").mkdir(parents=True)
            res = []
            for n in names:
                (out / "variants" / f"{n}.mp4").write_bytes(b"fake")
                res.append({"name": n, "file": f"variants/{n}.mp4", "seconds": 10.0, "hook": "h", "caption": "c"})
            return out, res
        pipeline.make_variants = spy
        try:
            r.render(Path(tempfile.mkdtemp()))
        finally:
            pipeline.make_variants = real
        self.assertEqual(seen["hints"], {"pencil": False, "continued": True})


class AlbumSizeTest(unittest.TestCase):
    def test_big_drawings_split_into_albums_and_fall_back_to_singles(self):
        from reels_ai.bot import album_chunks
        from reels_ai.telegram import TelegramError
        out = Path(tempfile.mkdtemp())
        (out / "variants").mkdir()
        results = []
        for i in range(6):
            (out / "variants" / f"s{i}.mp4").write_bytes(b"x" * 9)
            results.append({"name": "speedrun", "file": f"variants/s{i}.mp4", "seconds": 9.0,
                            "hook": "h", "caption": "c"})
        self.assertEqual([len(c) for c in album_chunks(out, results, limit=20)], [2, 2, 2])
        self.assertEqual([len(c) for c in album_chunks(out, results, limit=10 ** 6)], [6])

        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(bot, state, load_config(), clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)))

        def refuse(*a, **k):
            raise TelegramError("sendMediaGroup: Request Entity Too Large")
        bot.send_media_group = refuse
        r.bank_album("d1", {"subject": "Sasuke"}, out, results)
        self.assertEqual(len(state["variants"]), 6)                  # all banked, sent one by one
        self.assertEqual(sum(k == "video" for k, _, _ in bot.sent), 6)


class ReliabilityTest(FakeVariantsMixin, unittest.TestCase):
    def test_memory_saved_after_each_drawing(self):
        bot, state = FakeBot(), fresh_state()
        saves = []
        r = Runner(bot, state, load_config(), clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)),
                   checkpoint=lambda: saves.append({d: x["status"] for d, x in state["drawings"].items()}))
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        bot.push(**msg(OWNER, text="hi"))
        for i in range(3):
            bot.files[f"raw{i}"] = src
            bot.push(**msg(OWNER, video={"file_id": f"raw{i}", "file_unique_id": f"u{i}", "file_size": 9},
                           caption=f"Char{i}"))
        r.poll()
        self.stub_render(r)
        self.assertEqual(len(saves), 6)                     # before (an attempt) and after each drawing
        self.assertEqual([list(x.values()).count("rendered") for x in saves[1::2]], [1, 2, 3])

    def test_a_recording_that_times_out_twice_is_set_aside(self):
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(bot, state, load_config(), clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)))
        state["drawings"]["d1"] = {"source": "drive", "drive_id": "f1", "drive_name": "Lets+Go+LIVE!.mp4",
                                   "subject": None, "status": "queued", "attempts": 2}
        self.stub_render(r)
        self.assertEqual(state["drawings"]["d1"]["status"], "failed")
        self.assertTrue(any("longer than a whole run twice" in t for t in bot.texts()))

    def test_failure_alert(self):
        import reels_ai.bot as botmod
        sent = []

        class B:
            def __init__(self, token):
                pass

            def send_message(self, chat, text, **kw):
                sent.append((chat, text))
        tmp = Path(tempfile.mkdtemp()) / "state.json"
        tmp.write_text(json.dumps({"owner_chat_id": 4242}))
        env = {"TELEGRAM_BOT_TOKEN": "123456789:" + "A" * 35, "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "77"}
        old_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        real_bot, real_load = botmod.Bot, botmod.load_state
        botmod.Bot, botmod.load_state = B, (lambda: {**json.loads(json.dumps(EMPTY)), **json.loads(tmp.read_text())})
        try:
            botmod.run("alert")
        finally:
            botmod.Bot, botmod.load_state = real_bot, real_load
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.assertEqual(sent[0][0], 4242)
        self.assertIn("actions/runs/77", sent[0][1])


class RetireAndTidyTest(FakeVariantsMixin, unittest.TestCase):
    def test_retired_drawings_leave_the_bank(self):
        bot, state = FakeBot(), fresh_state()
        cfg = load_config()
        cfg["timezone"] = "UTC"
        cfg["bank"]["retired"] = []
        cfg["approval"]["mode"] = "auto"
        cfg["delivery"]["split"], cfg["variants"]["per_drawing"] = [], 5   # classic one-video drops
        cfg["delivery"]["drop_times"] = ["17:00", "17:00"]   # two a day, as written
        clock = Clock(datetime(2026, 11, 7, 9, tzinfo=timezone.utc))
        r = Runner(bot, state, cfg, clock=clock)
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        bot.push(**msg(OWNER, text="hi"))
        for i, name in enumerate(["Shinobi Undercover Manga", "Naruto"]):
            bot.files[f"raw{i}"] = src
            bot.push(**msg(OWNER, video={"file_id": f"raw{i}", "file_unique_id": f"u{i}", "file_size": 9},
                           caption=name))
        r.poll()
        self.stub_render(r)
        self.assertEqual(r.bank()["drops"], 10)
        cfg["bank"]["retired"] = ["Shinobi Undercover"]
        self.assertEqual(r.bank()["drops"], 5)
        for day in range(4):
            clock.t = datetime(2026, 11, 7, 20, tzinfo=timezone.utc) + timedelta(days=day)
            r.poll()
        dropped = [v for v in state["variants"].values() if v.get("dropped_at")]
        self.assertTrue(dropped)
        self.assertTrue(all(state["drawings"][v["drawing"]]["subject"] == "Naruto" for v in dropped))
        bot.push(**msg(OWNER, text="/redo shinobi"))
        r.poll()
        self.assertTrue(any("No finished drawing" in t for t in bot.texts()))

    def test_retire_command(self):
        bot, state = FakeBot(), fresh_state()
        cfg = load_config()
        cfg["bank"]["retired"] = []
        r = Runner(bot, state, cfg, clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)))
        state["drawings"]["d5"] = {"subject": "My Hero Academia", "status": "queued"}
        bot.push(**msg(OWNER, text="hi"))
        bot.push(**msg(OWNER, text="/retire my hero"))
        r.poll()
        self.assertTrue(r.retired(state["drawings"]["d5"]))
        self.assertTrue(any("1 drawing(s): My Hero Academia" in t for t in bot.texts()))
        bot.push(**msg(OWNER, text="/unretire My Hero"))
        r.poll()
        self.assertFalse(r.retired(state["drawings"]["d5"]))

    def test_tidy_trashes_only_leftovers(self):
        from reels_ai import tidy

        class D:
            def __init__(self):
                self.trashed = []

            def folder(self, path):
                return "id:" + path

            def list(self, fid, kind="video"):
                if kind == "folder":
                    return [{"id": "f1", "name": "Naruto"}]
                return [
                    {"id": "LIVE1aaaaaaa", "name": "Naruto - Speedrun (redo).mp4", "size": "100"},
                    {"id": "CRASHaaaaaaa", "name": "Naruto - Speedrun (redo).mp4", "size": "100"},   # crashed run
                    {"id": "OLDaaaaaaaaa", "name": "Naruto - Speedrun.mp4", "size": "100"},          # replaced
                    {"id": "OTHERDRAWING", "name": "Naruto - One take.mp4", "size": "100"},          # unknown: keep
                    {"id": "POSTEDaaaaaa", "name": "Naruto - Ink only.mp4", "size": "100"},
                ]

            def trash(self, fid):
                self.trashed.append(fid)

        def link(i):
            return f"https://drive.google.com/file/d/{i}/view"
        state = {"variants": {
            "v1": {"status": "approved", "full_link": link("LIVE1aaaaaaa")},
            "v2": {"status": "replaced", "full_link": link("OLDaaaaaaaaa")},
            "v3": {"status": "posted", "full_link": link("POSTEDaaaaaa")}}}
        d = D()
        msg_ = tidy.run(d, state)
        self.assertEqual(sorted(d.trashed), ["CRASHaaaaaaa", "OLDaaaaaaaaa"])
        self.assertIn("moved 2 duplicate", msg_)


class KeepSkipTest(FakeVariantsMixin, unittest.TestCase):
    """Default setup now: every new video comes with ✅ Keep / ❌ Skip; only kept ones are dropped,
    each drop has 👍/👎, and your taste steers which styles get made."""

    def test_keep_skip_rate_and_learn(self):
        bot, state = FakeBot(), fresh_state()
        cfg = load_config()
        cfg["timezone"] = "UTC"
        clock = Clock(datetime(2026, 11, 7, 9, tzinfo=timezone.utc))
        r = Runner(bot, state, cfg, clock=clock)
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        bot.files["raw0"] = src
        bot.push(**msg(OWNER, text="hi"))
        bot.push(**msg(OWNER, video={"file_id": "raw0", "file_unique_id": "u0", "file_size": 9}, caption="Goku"))
        r.poll()
        self.stub_render(r)
        vids = {k: v for k, v in state["variants"].items()}
        self.assertTrue(all(v["status"] == "pending" for v in vids.values()))
        markup = bot.markups[next(iter(vids.values()))["message_id"]]
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "✅ Keep")
        ks = list(vids)
        bot.push(**tap(f"ok:{ks[0]}"))
        bot.push(**tap(f"no:{ks[1]}"))
        r.poll()
        self.assertEqual(state["variants"][ks[0]]["status"], "approved")
        self.assertEqual(state["variants"][ks[1]]["status"], "skipped")
        clock.t = datetime(2026, 11, 7, 20, tzinfo=timezone.utc)
        r.poll()                                     # kept and unanswered go out as options; skipped never
        dropped = sorted(k for k, v in state["variants"].items() if v.get("dropped_at"))
        self.assertEqual(dropped, sorted([ks[0], ks[2]]))
        drop_msg = next(m for m, mk in bot.markups.items()
                        if mk and mk["inline_keyboard"][0][-1].get("callback_data") == f"dn:{ks[0]}")
        bot.push(**tap(f"dn:{ks[0]}", message_id=drop_msg))
        r.poll()
        style = state["variants"][ks[0]]["style"]
        self.assertEqual(state["style_feedback"][style], {"liked": 1, "disliked": 1})

    def test_taste_weights(self):
        r = Runner(FakeBot(), fresh_state(), load_config())
        r.state["style_feedback"] = {"speedrun": {"liked": 0, "disliked": 4},
                                     "one_take": {"liked": 4, "disliked": 0},
                                     "ink_only": {"liked": 1, "disliked": 0}}          # too few: ignored
        w = r.style_weights()
        self.assertAlmostEqual(w["speedrun"], 0.4)
        self.assertAlmostEqual(w["one_take"], 1.6)
        self.assertNotIn("ink_only", w)
        r.state["style_uses"] = {s: 3 for s in r.cfg["variants"]["styles"]}
        self.assertNotIn("speedrun", r.choose_styles("d1"))


class IdeasTest(unittest.TestCase):
    def test_pasted_lines_become_hooks_and_captions(self):
        bot, state = FakeBot(), fresh_state()
        r = Runner(bot, state, load_config(), clock=Clock(datetime(2026, 11, 7, tzinfo=timezone.utc)))
        bot.push(**msg(OWNER, text="hi"))
        bot.push(**msg(OWNER, text="/ideas\nHooks:\n1. Wait for the last line\n- \u201cEvery line by hand\u201d\n"
                                    "Caption: Inked this one on stream with the chat picking the character. Who next? \U0001f447"))
        r.poll()
        self.assertEqual(state["owner_ideas"]["hooks"], ["Wait for the last line", "Every line by hand"])
        self.assertEqual(len(state["owner_ideas"]["captions"]), 1)
        styles = ["one_take", "speedrun", "ink_only", "detail_zoom", "reveal_first"]
        mixed = [r.with_owner_ideas(None, styles, f"d{i}") for i in range(20)]
        used = [h for m in mixed for h in m["hooks"].values()]
        self.assertTrue(used and set(used) <= {"Wait for the last line", "Every line by hand"})
        bot.push(**msg(OWNER, text="/ideas clear"))
        r.poll()
        self.assertEqual(state["owner_ideas"]["hooks"], [])


class NoGuessedNamesTest(unittest.TestCase):
    def test_unlabelled_drawings_never_get_a_character_name(self):
        from reels_ai.bot import strip_guessed_names
        copy = {"subject": "Naruto Uzumaki", "caption": "Naruto in pure ink. Who next?",
                "hooks": {"speedrun": "Naruto in 9 seconds", "one_take": "Wait for the last line"},
                "hashtags": ["#naruto", "#mangaart", "#narutouzumaki", "#inking"]}
        out = strip_guessed_names(copy)
        self.assertEqual(out["subject"], "")
        self.assertEqual(out["hooks"], {"one_take": "Wait for the last line"})
        self.assertEqual(out["caption"], "")
        self.assertEqual(out["hashtags"], ["#mangaart", "#inking"])

    def test_subject_comes_only_from_your_labels(self):
        r = Runner(FakeBot(), fresh_state(), load_config())
        self.assertIsNone(r.labelled_subject({"source": "drive", "drive_name": "IMG_0157.mov",
                                              "drive_where": "Inbox", "subject": "Naruto"}))   # old AI guess
        self.assertEqual(r.labelled_subject({"source": "drive", "drive_name": "3 - Sasuke.MOV",
                                             "drive_where": "Longform \u203a Naruto Page"}), "Sasuke")
        self.assertEqual(r.labelled_subject({"source": "drive", "drive_name": "4.MOV",
                                             "drive_where": "Longform \u203a Naruto Page"}), "Naruto Page")
        self.assertEqual(r.labelled_subject({"subject": "Goku"}), "Goku")                   # Telegram caption


class SplitDropsTest(FakeVariantsMixin, unittest.TestCase):
    """Default now: one drawing a day, its 3 versions sent as options, plus a still for Instagram."""

    def test_one_drawing_a_day_three_options_and_a_post_still(self):
        bot, state = FakeBot(), fresh_state()
        cfg = load_config()
        cfg["timezone"] = "UTC"
        cfg["approval"]["mode"] = "auto"
        clock = Clock(datetime(2026, 11, 7, 9, tzinfo=timezone.utc))
        r = Runner(bot, state, cfg, clock=clock)
        stills = []
        r.post_still = lambda options: stills.append(options) or None     # no ffmpeg here
        src = Path(tempfile.mkdtemp()) / "in.mp4"
        src.write_bytes(b"raw")
        bot.push(**msg(OWNER, text="hi"))
        for i, name in enumerate(["Naruto", "Sasuke", "Kakashi", "Itachi"]):
            bot.files[f"raw{i}"] = src
            bot.push(**msg(OWNER, video={"file_id": f"raw{i}", "file_unique_id": f"u{i}", "file_size": 9},
                           caption=name))
        r.poll()
        self.stub_render(r)
        self.assertEqual(len(state["variants"]), 12)                  # 3 versions per drawing
        self.assertEqual(r.bank()["drops"], 4)                        # counted in drawings
        bot.push(**msg(OWNER, text="/queue"))
        r.poll()
        self.assertTrue(any("Drawings ready: 4 = 4 days of uploads" in t for t in bot.texts()))
        for day in range(5):
            clock.t = datetime(2026, 11, 7, 20, tzinfo=timezone.utc) + timedelta(days=day)
            r.poll()
            r.poll()                                                 # extra runs never double-drop
        dropped = [v for v in state["variants"].values() if v.get("dropped_at")]
        by_drop: dict = {}
        for v in dropped:
            by_drop.setdefault(v["drop_no"], []).append(v)
        self.assertEqual(len(by_drop), 4)                            # one a day until the 4 drawings are used
        days = {vs[0]["dropped_at"][:10] for vs in by_drop.values()}
        self.assertEqual(len(days), 4)                               # never two drawings on one day
        for vs in by_drop.values():
            self.assertEqual(len({v["drawing"] for v in vs}), 1)     # one drawing per day
            self.assertEqual(sorted(v["option"] for v in vs), [1, 2, 3])
            self.assertEqual(len({v["style"] for v in vs}), 3)       # three different versions
            self.assertEqual(len({v["caption"] for v in vs}), 1)     # one caption
        drawings = [vs[0]["drawing"] for _, vs in sorted(by_drop.items())]
        self.assertEqual(len(set(drawings)), 4)                      # no drawing twice
        self.assertEqual(len([p for k, _, p in bot.sent if k == "video" and p.startswith("Option 1 of 3")]), 4)
        self.assertEqual(len([t for t in bot.texts() if "Today's drop #" in t and "pick the one you like" in t]), 4)
        self.assertEqual(len(stills), 4)                             # an Instagram still asked for each day
        self.assertEqual(len([t for t in bot.texts() if "use the last frame" in t]), 4)   # none made here: said so
        self.assertEqual(r.bank()["drops"], 0)


    def test_unanswered_versions_count_as_options_and_skipped_ones_do_not(self):
        bot, state = FakeBot(), fresh_state()
        cfg = load_config()
        cfg["timezone"] = "UTC"
        r = Runner(bot, state, cfg, clock=Clock(datetime(2026, 11, 7, 20, tzinfo=timezone.utc)))
        r.post_still = lambda options: None
        state["owner_chat_id"] = OWNER
        state["drawings"] = {"d1": {"subject": "Gojo", "status": "rendered"}}
        state["variants"] = {f"v{i}": {"drawing": "d1", "style": st, "status": status, "file_id": f"f{i}",
                                       "caption": "c", "hook": "h", "seconds": 12}
                             for i, (st, status) in enumerate([("one_take", "pending"), ("rewind", "pending"),
                                                               ("chapters", "skipped"), ("slow_open", "approved")])}
        r.drop_due()
        sent = sorted(k for k, v in state["variants"].items() if v.get("dropped_at"))
        self.assertEqual(sent, ["v0", "v1", "v3"])                   # the skipped one never goes out
        self.assertEqual(r.bank()["drops"], 0)


class RenamedInDriveTest(unittest.TestCase):
    def test_a_rename_in_drive_is_followed_and_a_timed_out_recording_is_retried_once(self):
        class D:
            name = "IMG_0163.mov"
            def folder(self, path=""): return "id:" + path
            def list(self, folder_id, kind="video"):
                return [{"id": "f1", "name": self.name}] if folder_id == "id:Inbox" and kind == "video" else []
        drive = D()
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        state["drive_seen"] = ["f1"]
        state["drawings"] = {"d1": {"source": "drive", "drive_id": "f1", "drive_name": "IMG_0163.mov", "subject": None,
                                    "status": "failed", "error": "took longer than a whole run twice", "attempts": 3}}
        r = Runner(bot, state, load_config(), drive=drive)
        drive.name = "Afro_Samurai.MOV"
        r.check_drive_inbox()
        d = state["drawings"]["d1"]
        self.assertEqual((d["drive_name"], d["status"]), ("Afro_Samurai.MOV", "queued"))
        self.assertIn("afro", d["subject"].lower())
        self.assertNotIn("attempts", d)
        self.assertTrue(any("Trying" in t and "again" in t for t in bot.texts()))
        d.update(status="failed", error="took longer than a whole run twice")
        r.check_drive_inbox()                                        # only once
        self.assertEqual(d["status"], "failed")


class StyleLearningTest(unittest.TestCase):
    def test_winning_styles_come_round_more_and_weakest_is_skipped(self):
        cfg = load_config()
        cfg["variants"]["per_drawing"] = 5
        cfg["variants"]["styles"] = ["one_take", "process_cuts", "reveal_first", "speedrun", "before_after",
                                     "detail_zoom", "ink_only"]      # this test's own list, not the config's
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
        cfg["platforms"]["instagram"].update(enabled=True, method="api", post_times=["19:00"])
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
        self.video = video = sample_video() or Path("no-sample-video")
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
        if sample_video() is None:
            self.skipTest("no sample video (set REELS_TEST_VIDEO)")
        out = Path(tempfile.mkdtemp())
        url = self.d.media_url("raw1")
        long_ = self.dmod.make_proxy(url, self.d.token(), out / "k.mp4", duration_s=40 * 60)   # keyframes
        short = self.dmod.make_proxy(url, self.d.token(), out / "s.mp4", duration_s=36.5)       # sampled
        from reels_ai.media import probe
        k, s = probe(long_), probe(short)
        # only 40 keyframes: too few, so the whole recording was sampled evenly instead
        self.assertGreater(k["duration"], 40 / 30 + 0.2)
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
        src = sample_video()
        if src is None:
            self.skipTest("no sample video (set REELS_TEST_VIDEO)")
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
        cfg["approval"]["mode"] = "auto"
        cfg["delivery"]["split"], cfg["variants"]["per_drawing"] = [], 5   # classic one-video drops
        cfg["delivery"]["drop_times"] = ["17:00", "17:00"]   # two a day, as written
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
        self.assertEqual(r.drive.ups[0][0], "id:Done/Shorts/Goku")
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
                "p1": [{"id": "s1", "name": "Naruto.MOV", "mimeType": "video/quicktime", "size": "9000000000"},
                       {"id": "s2", "name": "2.MOV", "mimeType": "video/quicktime", "size": "1000000000"}],
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
        self.assertEqual(set(drawings), {"i1", "s1", "s2"})              # Finished folder ignored
        self.assertEqual(drawings["s2"]["subject"], "Shinobi Page")      # unnamed drawing: the page names it
        self.assertEqual(drawings["s2"]["hints"], {"continued": True})   # 2nd drawing: page already has ink
        self.assertNotIn("continued", drawings["s1"].get("hints", {}))
        self.assertTrue(drawings["s1"]["keep_in_place"])
        self.assertEqual(sum("new recording" in t for t in bot.texts()), 1)
        notice = [t for t in bot.texts() if "new recording" in t][0]
        self.assertIn("Longform › Shinobi Page", notice)
        self.assertIn("Naruto.MOV → Naruto", notice)                       # a named file names the drawing
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


class SplitPageTest(FakeVariantsMixin, unittest.TestCase):
    """A page with several separate drawings becomes one drawing (and one set of Shorts) each."""

    def test_page_with_three_drawings_queues_three(self):
        import reels_ai.pipeline as pipeline

        class D:
            def folder(self, p=""):
                return "id:" + p

            def move(self, *a):
                pass

            def upload(self, path, folder, name=None, mime="video/mp4"):
                return {"id": "u", "webViewLink": "https://drive/u"}

            def media_url(self, fid):
                return f"https://drive/{fid}"

            def token(self):
                return "AT"

            fresh_token = token

        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        cfg = load_config()
        cfg["timezone"] = "UTC"
        cfg["approval"]["mode"] = "approve"
        r = Runner(bot, state, cfg, clock=Clock(datetime(2026, 11, 8, 9, tzinfo=timezone.utc)), drive=D())
        src = Path(tempfile.mkdtemp()) / "raw.mp4"
        src.write_bytes(b"x")
        r.drive_source = lambda d, tmp: src
        state["drawings"]["d1"] = {"source": "drive", "drive_id": "f1", "drive_name": "Lets+Go+LIVE!.mp4",
                                   "subject": "Deku", "status": "queued", "duration": 2100}
        state["next_id"] = 2
        parts = [{"t0": 60.0 * k, "t1": 60.0 * k + 50, "box": [0.1, 0.1, 0.5, 0.5], "label": f"{k + 1} of 3"}
                 for k in range(3)]
        calls = []
        real = pipeline.find_parts
        pipeline.find_parts = lambda *a, **kw: calls.append(1) or parts
        try:
            self.stub_render(r)
        finally:
            pipeline.find_parts = real
        drawings = state["drawings"]
        self.assertEqual(len(drawings), 3, (bot.texts(), drawings))
        self.assertEqual(sorted(d["part"]["label"] for d in drawings.values()), ["1 of 3", "2 of 3", "3 of 3"])
        self.assertTrue(all(d["split_done"] for d in drawings.values()))
        self.assertEqual(len(calls), 1)                                  # looked once, not per part
        self.assertTrue(any("3 separate drawings" in t for t in bot.texts()))
        captions = [c for k, _, c in bot.sent if k == "video"]
        self.assertTrue(any("drawing 2 of 3" in c for c in captions), captions[:2])

    def test_numbered_longdraw_videos_are_not_split(self):
        r = Runner(FakeBot(), fresh_state(), load_config())
        self.assertFalse(r.may_split({"drive_where": "Longform \u203a Page 3", "drive_name": "3 - Kakashi.MOV"}))
        self.assertTrue(r.may_split({"drive_where": "Longform \u203a Deku", "drive_name": "Lets+Go+LIVE!.mp4",
                                     "keep_in_place": True}))                  # a /redo keeps it in place
        self.assertTrue(r.may_split({"drive_where": "Longform \u203a Deku", "drive_name": "IMG_4412.MOV"}))
        self.assertTrue(r.may_split({"drive_where": "Inbox", "drive_name": "2 - Goku.MOV"}))


class FindSubjectsTest(unittest.TestCase):
    """Three drawings in three spots, made one after another, are found as three subjects."""

    @staticmethod
    def page(n=270, centres=((110, 170), (120, 470), (260, 330)), seed=1):
        rng = np.random.default_rng(seed)
        groups = []
        for cx, cy in centres:
            g = []
            for _ in range(40):
                x, y = cx + int(rng.integers(-60, 60)), cy + int(rng.integers(-60, 60))
                dx, dy = (int(v) for v in rng.integers(-30, 30, 2))
                g.append((x, y, x + dx, y + dy))
            groups.append(g)
        per = n // len(groups)
        frames = _Frames()
        for i in range(n):
            img = np.full((640, 360, 3), 240, np.uint8)
            cur, j = divmod(i, per)
            for gi, grp in enumerate(groups):
                upto = len(grp) if gi < cur else (int(len(grp) * (j + 1) / per) if gi == cur else 0)
                for x1, y1, x2, y2 in grp[:upto]:
                    cv2.line(img, (x1, y1), (x2, y2), (25, 25, 25), 3)
            if cur < len(groups):
                cx, cy = centres[cur]
                cv2.ellipse(img, (cx + 40, cy + 60), (45, 70), 30, 0, 360, (90, 130, 190), -1)
            frames.append(img)
        return frames

    def test_three_drawings(self):
        from reels_ai.analyze import find_subjects
        subs = find_subjects(self.page())
        self.assertEqual(len(subs), 3)
        centres = [((b[0] + b[2]) / 2 * 360, (b[1] + b[3]) / 2 * 640) for b in (s["box"] for s in subs)]
        for (cx, cy), (ex, ey) in zip(centres, ((110, 170), (120, 470), (260, 330))):  # in drawing order
            self.assertLess(abs(cx - ex), 50)
            self.assertLess(abs(cy - ey), 50)

    def test_one_drawing_stays_one(self):
        from reels_ai.analyze import find_subjects
        self.assertEqual(find_subjects(self.page(centres=((180, 320),))), [])


class _Frames(list):
    width, height = 360, 640


class NamingTest(unittest.TestCase):
    """Unnamed phone recordings (IMG_4412.MOV): the bot asks, your reply names and renames them."""

    def setUp(self):
        calls = self.calls = {"renamed": []}

        class D:
            def folder(self, p=""):
                return "id:" + p

            def list(self, fid, kind="video"):
                if fid == "id:Inbox" and kind == "video":
                    return [{"id": "f1", "name": "IMG_4412.MOV", "mimeType": "video/quicktime",
                             "createdTime": "2026-11-09T23:41:00Z", "videoMediaMetadata": {"durationMillis": "1380000"}},
                            {"id": "f2", "name": "Goku - ink only.MOV", "mimeType": "video/quicktime"}]
                return []

            def rename(self, fid, name):
                calls["renamed"].append((fid, name))

            def thumbnail(self, fid, dst):
                return None                                   # no preview: asks by text

        self.bot, self.state = FakeBot(), fresh_state()
        self.state["owner_chat_id"] = OWNER
        cfg = load_config()
        cfg["timezone"] = "America/New_York"
        self.clock = Clock(datetime(2026, 11, 10, 9, tzinfo=timezone.utc))
        self.r = Runner(self.bot, self.state, cfg, clock=self.clock, drive=D())
        self.r.check_drive_inbox()

    def drawing(self, fid):
        return next(d for d in self.state["drawings"].values() if d["drive_id"] == fid)

    def test_asks_only_about_unnamed_recordings(self):
        self.assertEqual(self.drawing("f1")["status"], "naming")
        self.assertEqual(self.drawing("f2")["status"], "queued")               # already named
        asks = [t for t in self.bot.texts() if "What's in this recording" in t]
        self.assertEqual(len(asks), 1)
        self.assertIn("IMG_4412.MOV", asks[0])
        self.assertIn("23 min", asks[0])
        self.assertIn("recorded Mon Nov 9, 6:41 PM", asks[0])                  # your time zone

    def test_reply_names_renames_and_queues(self):
        d = self.drawing("f1")
        self.bot.push(message={"chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
                               "text": "Kakashi - ink only", "reply_to_message": {"message_id": d["name_msg"]}})
        self.r.poll()
        self.assertEqual((d["subject"], d["status"], d["drive_name"]), ("Kakashi", "queued", "Kakashi - ink only.MOV"))
        self.assertEqual(d["hints"].get("pencil"), False)
        self.assertEqual(self.calls["renamed"], [("f1", "Kakashi - ink only.MOV")])
        self.assertEqual(self.r.labelled_subject(d), "Kakashi")
        self.assertTrue(any("IMG_4412.MOV → Kakashi" in t for t in self.bot.texts()))

    def test_name_command_and_timeout(self):
        self.r.name_command("Sasuke")
        self.assertEqual(self.drawing("f1")["subject"], "Sasuke")
        self.assertEqual(self.drawing("f1")["status"], "queued")
        self.drawing("f1")["status"] = "naming"
        self.clock.t += timedelta(hours=13)
        self.r.stop_waiting_for_names()
        self.assertEqual(self.drawing("f1")["status"], "queued")


class PickThePageTest(unittest.TestCase):
    """A livestream that shows an old finished page for a long time, then a new drawing on a
    blank page: the flip is found and the page drawn on is used, not the longest one."""

    def test_drawn_page_wins_over_longest(self):
        from reels_ai.media import drawn_stretch, page_changes
        old = FindSubjectsTest.page(n=90, centres=((90, 200), (250, 250), (120, 450), (260, 480)), seed=7)[-1]
        frames = _Frames()
        for i in range(400):
            f = old.copy()
            if i % 50 < 10:
                cv2.ellipse(f, (200 + i % 50 * 5, 400), (45, 70), 30, 0, 360, (90, 130, 190), -1)
            frames.append(f)
        frames += FindSubjectsTest.page(n=270)
        cuts = page_changes(frames)
        self.assertTrue(any(390 <= c <= 410 for c in cuts), cuts)
        a, b = drawn_stretch(frames, cuts, log=lambda *x: None)
        self.assertGreaterEqual(a, 390)
        self.assertGreaterEqual(b, 660)


class RedoRangeTest(unittest.TestCase):
    def test_time_range(self):
        from reels_ai.bot import _time_range
        self.assertEqual(_time_range("Lets Go 16-34"), ("Lets Go", [960, 2040]))
        self.assertEqual(_time_range("Lets Go 16:30-34"), ("Lets Go", [990, 2040]))
        self.assertEqual(_time_range("Naruto 5 to 9"), ("Naruto", [300, 540]))
        self.assertEqual(_time_range("3 - Kakashi"), ("3 - Kakashi", None))
        self.assertEqual(_time_range("all"), ("all", None))

    def test_redo_with_range_starts_over_from_one_recording(self):
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(bot, state, load_config())
        for k in (1, 2, 3):
            state["drawings"][f"d{k}"] = {"source": "drive", "drive_id": "f1", "drive_name": "Lets+Go+LIVE!.mp4",
                                          "subject": None, "status": "rendered", "split_done": True,
                                          "part": {"label": f"{k} of 3"}, "received": "2026-11-10"}
        bot.push(**msg(OWNER, text="/redo Lets Go 16-34"))
        r.poll()
        live = {k: d for k, d in state["drawings"].items() if d["status"] == "queued"}
        self.assertEqual(len(live), 1)
        d = next(iter(live.values()))
        self.assertEqual(d["window"], [960, 2040])
        self.assertNotIn("part", d)
        self.assertNotIn("split_done", d)
        self.assertTrue(any("16–34" in t for t in bot.texts()))

    def test_redo_remakes_every_drawing_of_a_split_page(self):
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(bot, state, load_config())
        for k in (1, 2, 3):
            state["drawings"][f"d{k}"] = {"source": "drive", "drive_id": "f1", "drive_name": "Lets+Go+LIVE!.mp4",
                                          "status": "rendered", "split_done": True, "part": {"label": f"{k} of 3"}}
        r.redo("Lets Go")
        self.assertEqual([d["status"] for d in state["drawings"].values()], ["queued"] * 3)
        self.assertTrue(all("part" in d for d in state["drawings"].values()))


class PageRecognitionTest(unittest.TestCase):
    """The drawn page is recognised in every frame; other pages anywhere in the recording drop out,
    and a nudged paper is lined back up."""

    def test_other_pages_drop_out_and_moves_are_lined_up(self):
        from reels_ai.media import PageMatcher, choose_page, track_page
        drawn = FindSubjectsTest.page(n=180, seed=1)
        other = FindSubjectsTest.page(n=90, centres=((90, 200), (250, 250), (120, 450), (260, 480)), seed=7)[-1]
        frames = _Frames()
        frames += [other] * 30                                   # showing an old page first
        frames += drawn[:90]
        frames += [other] * 25                                   # flipping to it mid-stream
        nudged = [cv2.warpAffine(f, np.float32([[1, 0, 9], [0, 1, -6]]), (360, 640),
                                 borderMode=cv2.BORDER_REPLICATE) for f in drawn[90:]]
        frames += nudged                                         # back, paper nudged a little
        ref = choose_page(frames, 30, len(frames) - 1)
        self.assertIsNotNone(ref)
        track = track_page(frames, PageMatcher(frames[ref]), list(range(len(frames))), log=lambda *a: None)
        kept = {i for i, _ in track}
        self.assertFalse(kept & set(range(0, 30)))               # the old page, never
        self.assertFalse(kept & set(range(120, 145)))            # the flip, never
        self.assertGreater(len(kept & set(range(60, 120))), 40)  # the drawing, yes
        self.assertGreater(len(kept & set(range(145, 235))), 60)
        H = dict(track)[200] if 200 in kept else None
        if H is not None and ref < 120:                           # nudged frame is shifted back
            self.assertAlmostEqual(H[0, 2], -9, delta=3)
            self.assertAlmostEqual(H[1, 2], 6, delta=3)


class DriveReadRetryTest(unittest.TestCase):
    def test_cut_off_download_is_retried(self):
        import http.client
        from unittest import mock
        import reels_ai.drive as drv
        calls = {"n": 0}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise http.client.IncompleteRead(b"x" * 10, 90)
                return b'{"ok": 1}'

        d = drv.Drive("c", "s", "r")
        with mock.patch.object(d, "_req", return_value=None, side_effect=lambda *a, **k: Resp()), \
                mock.patch("time.sleep"):
            self.assertEqual(d.read_text("f"), '{"ok": 1}')
        self.assertEqual(calls["n"], 2)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.bot, self.state = FakeBot(), fresh_state()
        self.state["owner_chat_id"] = OWNER
        self.r = Runner(self.bot, self.state, load_config())
        dr = self.state["drawings"]
        dr["d1"] = {"source": "drive", "drive_name": "Lets+Go+LIVE!.mp4", "subject": "Deku", "status": "rendered",
                    "part": {"label": "2 of 5"}, "received": "2026-11-10"}
        dr["d2"] = {"source": "drive", "drive_name": "Kakashi.MOV", "subject": "Kakashi", "status": "rendered",
                    "received": "2026-11-09"}
        dr["d3"] = {"source": "drive", "drive_name": "Lets+Go+LIVE!.mp4", "subject": "Deku", "status": "queued",
                    "part": {"label": "5 of 5"}, "received": "2026-11-10"}
        vs = self.state["variants"]
        vs["v1"] = {"drawing": "d1", "style": "speedrun", "seconds": 8.7, "status": "pending", "message_id": 11}
        vs["v2"] = {"drawing": "d1", "style": "one_take", "seconds": 13.1, "status": "approved", "message_id": 12}
        vs["v3"] = {"drawing": "d2", "style": "one_take", "seconds": 12.0, "status": "approved", "message_id": 13,
                    "dropped_at": "2026-11-09T22:00:00+00:00"}
        vs["v4"] = {"drawing": "d2", "style": "reveal_first", "seconds": 10.0, "status": "skipped", "message_id": 14}

    def test_inventory_lists_drawings_and_shorts_with_ids(self):
        text = "\n".join(self.r.inventory_text())
        self.assertIn("3 drawings", text)
        self.assertIn("d1 Deku · drawing 2 of 5", text)
        self.assertIn("v1 Speedrun", text)
        self.assertIn("d3 Deku · drawing 5 of 5 — being made", text)
        self.assertIn("✅ 1 kept", text)
        self.assertIn("\U0001f4e4 1 dropped", text)

    def test_delete_and_undelete(self):
        self.r.delete_command("v1 v3 d3")
        vs, dr = self.state["variants"], self.state["drawings"]
        self.assertEqual(vs["v1"]["status"], "deleted")
        self.assertNotEqual(vs["v3"]["status"], "deleted")                 # already dropped: history
        self.assertEqual(dr["d3"]["status"], "deleted")                     # won't be made
        self.assertIn(11, getattr(self.bot, "deleted", []))
        self.assertTrue(any("already dropped" in t for t in self.bot.texts()))
        self.r.delete_command("v1 d3", undo=True)
        self.assertEqual(vs["v1"]["status"], "pending")
        self.assertEqual(dr["d3"]["status"], "queued")

    def test_delete_by_name_asks_for_ids(self):
        self.r.delete_command("Lets Go")
        self.assertTrue(any("Which ones?" in t and "d1" in t and "d3" in t for t in self.bot.texts()))
        self.assertEqual(self.state["drawings"]["d1"]["status"], "rendered")


class ReviewerGateTest(unittest.TestCase):
    """When no version of a drawing passes the reviewer, nothing is sent: the drawing is held and
    the owner gets one message with the reason."""

    def test_nothing_passed_means_held_and_told(self):
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(bot, state, load_config())
        out = Path(tempfile.mkdtemp())
        (out / "held.json").write_text(json.dumps([
            {"name": "one_take", "reasons": ["shows a different page (around 0s, 11s)"]},
            {"name": "speedrun", "reasons": ["shows a different page (around 6s, 9s)"]}]))
        d = {"source": "drive", "drive_name": "Lets+Go+LIVE!.mp4", "subject": None, "status": "queued"}
        state["drawings"]["d1"] = d
        self.assertTrue(r.held_back("d1", d, out, []))
        self.assertEqual(d["status"], "held")
        told = [t for t in bot.texts() if "none passed my own check" in t]
        self.assertEqual(len(told), 1)
        self.assertIn("different page", told[0])
        self.assertIn("/redo Lets+Go+LIVE! 16-34", told[0])

    def test_some_passed_means_send_those_quietly(self):
        bot, state = FakeBot(), fresh_state()
        state["owner_chat_id"] = OWNER
        r = Runner(bot, state, load_config())
        out = Path(tempfile.mkdtemp())
        (out / "held.json").write_text(json.dumps([{"name": "ink_only", "reasons": ["only 4 s long"]}]))
        d = {"source": "drive", "drive_name": "Kakashi.MOV", "subject": "Kakashi", "status": "queued"}
        self.assertFalse(r.held_back("d1", d, out, [{"name": "one_take"}]))
        self.assertEqual(d["status"], "queued")
        self.assertEqual(bot.texts(), [])


class ServeTest(unittest.TestCase):
    """Always-on mode: answers at once, renders when there is work, hands over cleanly."""

    def runner(self):
        from reels_ai import bot as botmod
        bot, state = FakeBot(), fresh_state()
        r = Runner(bot, state, load_config())
        r.render_all = lambda: self.rendered.append(1)
        self.rendered, self.saves, self.t = [], [], [0.0]
        return botmod, bot, state, r

    def test_answers_saves_only_changes_and_starts_its_successor(self):
        botmod, bot, state, r = self.runner()
        bot.push(**msg(OWNER, text="/status"))
        polls = []
        real = r.poll

        def poll():
            polls.append(1)
            self.t[0] += 30                                  # each look at Telegram takes 30 s here
            return real()
        r.poll = poll
        started = []
        botmod.serve(r, lambda: self.saves.append(1), minutes=3, waiting=lambda: 0,
                     clock=lambda: self.t[0], restart=lambda: started.append(1))
        self.assertTrue(r.serving and bot.wait == 20)
        self.assertEqual(len(polls), 6)                      # kept answering for the whole 3 minutes
        self.assertTrue(any(k == "text" for k, *_ in bot.sent))
        self.assertLessEqual(len(self.saves), 3)             # memory written when it changed, and at the end
        self.assertEqual((self.rendered, started), ([], [1]))

    def test_hands_over_when_a_newer_run_waits_and_survives_a_network_blip(self):
        botmod, bot, state, r = self.runner()
        calls = []

        def poll():
            calls.append(1)
            self.t[0] += 40
            if len(calls) == 1:
                raise OSError("network down")
            return False
        r.poll = poll
        started = []
        from unittest import mock
        with mock.patch("time.sleep"):
            botmod.serve(r, lambda: None, minutes=300, waiting=lambda: 1 if self.t[0] >= 100 else 0,
                         clock=lambda: self.t[0], restart=lambda: started.append(1))
        self.assertGreaterEqual(len(calls), 2)               # went on after the blip
        self.assertLess(self.t[0], 600)                      # stopped long before the 300 minutes
        self.assertEqual(started, [])                        # the waiting run takes over: no extra start
