"""One channel Short (primary style) + Facebook trial reels per drawing, with instant buttons."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from reels_ai.bot import Runner
from reels_ai.state import load_config
from tests.test_bot import OWNER, Clock, FakeBot, fresh_state, tap

NOW = datetime(2026, 11, 23, 17, tzinfo=timezone.utc)


def runner():
    b = FakeBot()
    st = fresh_state()
    st["owner_chat_id"] = OWNER
    st["drawings"]["d1"] = {"subject": "Vegeta", "status": "rendered", "source": "drive"}
    return b, Runner(b, st, load_config(), clock=Clock(NOW))


def results(styles):
    tmp = Path(tempfile.mkdtemp())
    out = []
    for s in styles:
        (tmp / f"{s}.mp4").write_bytes(b"x")
        out.append({"name": s, "file": f"{s}.mp4", "hook": "Line by line", "caption": "cap", "seconds": 12.0})
    return tmp, out


class FlowTest(unittest.TestCase):
    def test_rewind_first_then_four_trial_reels(self):
        b, r = runner()
        styles = r.choose_styles("d1")
        self.assertEqual(styles[0], "rewind")
        self.assertEqual(len(styles), 5)
        self.assertEqual(len(set(styles)), 5)
        self.assertEqual(r.shorts_folder("rewind", "Vegeta"), "Completed Shorts/Vegeta")
        self.assertEqual(r.shorts_folder("one_take", "Vegeta"), "Trial Reel Shorts/Vegeta")

    def test_sent_at_once_with_the_right_buttons_and_taps(self):
        b, r = runner()
        out, res = results(["one_take", "rewind", "slow_open"])
        r.send_flow("d1", r.state["drawings"]["d1"], out, res, {})
        sent = [x for x in b.sent if x[0] == "video"]
        self.assertIn("your channel Short (Rewind)", sent[0][2])                # channel Short comes first
        self.assertIn("trial reel: One take", sent[1][2])
        vids = {v["style"]: k for k, v in r.state["variants"].items()}
        self.assertEqual(r.state["variants"][vids["rewind"]]["role"], "primary")
        self.assertEqual([x["callback_data"] for row in b.markups[100 + 1]["inline_keyboard"] for x in row],
                         [f"ps:{vids['rewind']}", f"pu:{vids['rewind']}", f"pr:{vids['rewind']}"])
        r.notion = None
        for data in (f"pu:{vids['rewind']}", f"tk:{vids['one_take']}", f"td:{vids['slow_open']}"):
            b.push(**tap(data))
        r.poll()
        V = r.state["variants"]
        self.assertEqual((V[vids["rewind"]]["status"], V[vids["one_take"]]["status"], V[vids["slow_open"]]["status"]),
                         ("posted", "trial", "discarded"))

    def test_redo_makes_a_new_take_of_the_channel_short(self):
        b, r = runner()
        out, res = results(["rewind"])
        r.send_flow("d1", r.state["drawings"]["d1"], out, res, {})
        vid = next(iter(r.state["variants"]))
        b.push(**tap(f"pr:{vid}"))
        r.poll()
        d = r.state["drawings"]["d1"]
        self.assertEqual((d["status"], d["redo_styles"], d["redo_count"]), ("queued", ["rewind"], 1))
        self.assertEqual(r.state["variants"][vid]["status"], "replaced")

    def test_styles_and_primary(self):
        b, r = runner()
        self.assertIn("⭐ Rewind", r.styles_text())
        r.set_primary("one take")
        self.assertEqual(r.primary_style(), "one_take")
        self.assertEqual(r.choose_styles("d1")[0], "one_take")
        r.set_primary("nonsense")
        self.assertEqual(r.primary_style(), "one_take")


if __name__ == "__main__":
    unittest.main()


class LongVideoTest(unittest.TestCase):
    def test_longform_asks_drawing_or_lesson_and_files_lessons(self):
        from unittest import mock
        b, r = runner()
        moved = []

        class D:
            def folder(self, p=""): return "id:" + p
            def move_to(self, fid, to): moved.append((fid, to))
        r.drive = D()
        projects = [{"id": "p1", "name": "Hands", "path": "Hands"}]
        started = []
        r.start_longform = started.append
        with mock.patch("reels_ai.longform_job.list_projects", return_value=projects), \
                mock.patch.object(Runner, "about_or_ask", return_value={}):
            r.longform_command("Hands")
            ask = [t for k, _, t in b.sent if "what kind of long video" in t]
            self.assertEqual(len(ask), 1)
            token = next(iter(r.state["kind_asks"]))
            b.push(**tap(f"lk:{token}:e"))
            r.poll()
        self.assertEqual(moved, [("p1", "id:Longform/Educational Drawing/Studio Lessons")])
        self.assertEqual(started, ["Educational Drawing/Studio Lessons/Hands"])

    def test_lesson_recordings_skip_shorts(self):
        b, r = runner()
        moved = []

        class D:
            def folder(self, p=""): return "id:" + p
            def move_to(self, fid, to): moved.append((fid, to))
            def list(self, fid, kind="video"):
                return [{"id": "x", "name": "Hands - lesson.MOV"}, {"id": "y", "name": "Gojo.MOV"}] if fid == "id:Inbox" else []
        r.drive = D()
        r.check_drive_inbox()
        self.assertEqual(moved, [("x", "id:Longform/Educational Drawing/Studio Lessons/Hands - lesson")])
        self.assertEqual([d.get("drive_id") for d in r.state["drawings"].values() if d.get("drive_id")], ["y"])

    def test_lesson_description_and_titles(self):
        from reels_ai import descriptions, titles
        cfg = load_config()
        d = descriptions.longdraw(cfg, "Studio Lessons · Lesson 03", descriptions.topic_line("Inking hair", lesson=True),
                                  "How I ink hair with a brush pen.", "0:00 Intro\n0:05 Hair\n9:00 Ink")
        self.assertTrue(d.startswith("Welcome to Studio Lessons · Lesson 03! Grab a pencil"))
        self.assertIn("Today's lesson: Inking hair.", d)
        self.assertIn("What you will learn\nHow I ink hair", d)
        self.assertIn("New Shorts: Monday, Wednesday and Friday\n", d + "\n")
        t = titles.write_title("Hands", {"subject": "drawing hands"}, True, "FALLBACK",
                               ask=lambda s, sc, txt: {"title": "How to Draw Hands From Simple Shapes"})
        self.assertEqual(t, "How to Draw Hands From Simple Shapes")


class VaultTest(unittest.TestCase):
    def test_vault_counts_and_review_walk_through(self):
        b, r = runner()
        D, V = r.state["drawings"], r.state["variants"]
        D.clear()
        for i, st in enumerate(["up", "old1", "old2", "new"]):
            D[f"d{i}"] = {"subject": f"S{i}", "status": "rendered"}
        V.update({
            "v1": {"drawing": "d0", "style": "rewind", "status": "posted", "used_at": "x"},
            "v2": {"drawing": "d1", "style": "one_take", "status": "approved", "file_id": "f2"},
            "v3": {"drawing": "d2", "style": "rewind", "status": "pending", "file_id": "f3"},
            "v4": {"drawing": "d3", "style": "rewind", "status": "pending", "role": "primary", "file_id": "f4"},
            "v5": {"drawing": "d3", "style": "slow_open", "status": "trial"}})
        txt = r.vault_text()
        self.assertIn("Uploaded: 1", txt)
        self.assertIn("Ready to upload: 1", txt)
        self.assertIn("Still to review: 2", txt)
        self.assertIn("Trial reels kept for Facebook: 1", txt)
        r.review_next(first=True)
        self.assertIn("Review 2 left · S1", [x for x in b.sent if x[0] == "video"][-1][2])
        b.push(**tap("rr:d1"))                                              # old style: make its Rewind version
        r.poll()
        self.assertEqual((D["d1"]["status"], D["d1"]["redo_styles"]), ("queued", ["rewind"]))
        self.assertEqual(V["v2"]["status"], "trial")
        self.assertIn("Review 1 left · S2", [x for x in b.sent if x[0] == "video"][-1][2])
        b.push(**tap("rd:d2"))
        r.poll()
        self.assertEqual(V["v3"]["status"], "discarded")
        self.assertTrue(any("Nothing left to review" in x[2] for x in b.sent))


class ScheduleAndScoreTest(unittest.TestCase):
    def test_schedule_on_youtube_takes_the_next_free_posting_day(self):
        b, r = runner()                                   # Mon Nov 23, 12 PM in New York
        out, res = results(["rewind", "rewind"])
        r.send_flow("d1", r.state["drawings"]["d1"], out, res, {})
        uploads = []

        class YT:
            def upload_file(self, path, title, desc, tags, privacy, publish_at=None):
                uploads.append((title, privacy, publish_at, desc))
                return {"id": f"y{len(uploads)}", "permalink": f"https://youtu.be/y{len(uploads)}"}
        r.clients["youtube"] = YT()
        r.bot.download = lambda fid, dst: (Path(dst).write_bytes(b"x"), Path(dst))[1]
        a, c = list(r.state["variants"])
        b.push(**tap(f"ps:{a}"))
        b.push(**tap(f"ps:{c}"))
        r.poll()
        self.assertEqual([u[2] for u in uploads], ["2026-11-25T17:00:00Z", "2026-11-27T17:00:00Z"])  # Wed, then Fri
        self.assertEqual(uploads[0][1], "private")
        self.assertIn("Commission link", uploads[0][3])
        self.assertIn("#shorts", uploads[0][3])
        v = r.state["variants"][a]
        self.assertEqual((v["status"], v["posts"]["youtube"]["id"]), ("posted", "y1"))
        self.assertTrue(any("Scheduled on YouTube for Wed Nov 25" in x[2] for x in b.sent))

    def test_trial_scores_rank_styles_and_suggest_a_switch(self):
        from reels_ai.botkit.shorts_flow import parse_views
        self.assertEqual([parse_views(x) for x in ("12k", "1.2m", "12,345", "abc")], [12000, 1200000, 12345, None])
        b, r = runner()
        for v in ("12k", "9k", "15k"):
            r.score_command(f"one take {v}")
        r.score_command("slow open 2k")
        r.state["variants"]["v9"] = {"drawing": "d1", "style": "speedrun", "status": "trial"}
        r.score_command("v9 500")
        txt = r.styles_text()
        self.assertLess(txt.index("One take: 12,000"), txt.index("Slow open: 2,000"))
        self.assertIn("/primary one take", txt)
        r.score_command("nonsense")
        self.assertTrue(any("Tell me the style" in x[2] for x in b.sent))
