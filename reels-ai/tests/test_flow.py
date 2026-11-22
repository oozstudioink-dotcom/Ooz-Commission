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
        self.assertEqual(b.markups[100 + 1]["inline_keyboard"][0][0]["callback_data"], f"pu:{vids['rewind']}")
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
