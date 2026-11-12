"""Ears and eyes: the edit map and what the editor makes of it. Claude is replaced by a stand-in
here (no key, no cost); the real thing is exercised by the test set on real recordings."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from reels_ai import ears, eyes, watch
from tests.test_review import drawing, page, strokes, write


class Frames(list):
    width, height = 360, 640


def seen(drawings, pages=None, frames=20):
    return {"pages": pages or [{"id": "A", "description": "sketchbook page", "drawn_on": True},
                               {"id": "B", "description": "finished comic page", "drawn_on": False}],
            "frames": [{"index": i, "page": "A", "activity": "drawing"} for i in range(frames)],
            "drawings": drawings, "notes": ""}


def one(page_id, first, last, box=(0, 0, 1, 1), title="", named=False):
    return {"page": page_id, "title": title, "named_by_artist": named, "first_frame": first, "last_frame": last,
            "box": dict(zip(("x0", "y0", "x1", "y1"), box)), "what": ""}


class PartsFromMapTest(unittest.TestCase):
    def setUp(self):
        self.store = Frames(drawing(600))                       # 600 frames, 1 s each below
        self.times = [15 + 30 * i for i in range(20)]           # a storyboard frame every 30 s

    def parts(self, drawings, cuts=(), **kw):
        m = {"frame_times": self.times, **seen(drawings, **kw)}
        return watch.parts_from_map(m, self.store, 1.0, list(cuts))

    def test_three_drawings_in_order_with_times_boxes_and_labels(self):
        got = self.parts([one("A", 12, 19, (0.5, 0.3, 1.0, 0.8), "Deku landing", True),
                          one("A", 0, 5, (0.0, 0.0, 0.5, 0.4), "head close-up, top left"),
                          one("A", 6, 11, (0.0, 0.5, 0.5, 1.0))])
        self.assertEqual([p["label"] for p in got], ["1 of 3", "2 of 3", "3 of 3"])
        self.assertEqual([p["title"] for p in got], ["head close-up, top left", "", "Deku landing"])
        self.assertEqual((got[0]["t0"], got[0]["t1"]), (0.0, 180.0))       # frames 0-5, half a gap each side
        self.assertEqual((got[2]["t0"], got[2]["t1"]), (360.0, 600.0))
        self.assertTrue(all(p["box"] != [0, 0, 1, 1] for p in got))         # they share a page: framed
        self.assertTrue(all(p["t0"] <= p["ref_t"] <= p["t1"] for p in got))

    def test_boundaries_snap_to_measured_page_changes(self):
        got = self.parts([one("A", 0, 5), one("C", 6, 19)], cuts=[171, 400],
                         pages=[{"id": "A", "description": "", "drawn_on": True},
                                {"id": "C", "description": "", "drawn_on": True}])
        self.assertEqual(got[0]["t1"], 171.0)                               # 180 by the storyboard, 171 measured
        self.assertEqual(got[1]["t0"], 171.0)
        self.assertTrue(all(p["box"] == [0, 0, 1, 1] for p in got))         # one drawing per page: whole frame

    def test_pages_that_were_only_shown_and_bad_indexes_are_left_out(self):
        got = self.parts([one("A", 0, 9), one("B", 10, 12), one("A", 14, 99)])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["label"], "1 of 1")


class EditMapTest(unittest.TestCase):
    def test_storyboard_size_follows_the_recording_length(self):
        store = Frames(drawing(240))
        self.assertEqual(len(eyes.storyboard(store, 0.5)), eyes.MIN_FRAMES)         # 2 min: the minimum
        self.assertEqual(len(eyes.storyboard(store, 9.0)), 72)                      # 36 min: one per 30 s
        self.assertTrue(all(jpg[:2] == b"\xff\xd8" for _, jpg in eyes.storyboard(store, 0.5)))

    def test_map_holds_what_was_seen_and_heard(self):
        store = Frames(drawing(240))
        heard = [{"t0": 12.0, "t1": 15.0, "text": "this one is Deku landing"}]
        asked = {}

        def look(frames, transcript_text, file_name, model=None):
            asked.update(n=len(frames), text=transcript_text, name=file_name, model=model)
            return seen([one("A", 0, 5)], frames=len(frames))

        m = watch.edit_map(store, 0.5, "Lets+Go+LIVE!.mp4", heard, look=look, log=lambda *a: None)
        self.assertEqual(asked["n"], len(m["frame_times"]))
        self.assertIn("[0:12] this one is Deku landing", asked["text"])
        self.assertEqual(asked["model"], "claude-opus-5-5")
        self.assertEqual((m["transcript"], len(m["drawings"]), m["file"]), (heard, 1, "Lets+Go+LIVE!.mp4"))

    def test_no_key_means_no_watcher(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertIsNone(watch.watcher(None, "x.mp4", {}, Path(".")))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            self.assertIsNotNone(watch.watcher(None, "x.mp4", {}, Path(".")))
            self.assertIsNone(watch.watcher(None, "x.mp4", {"watch": {"enabled": False}}, Path(".")))

    def test_a_recording_watched_before_is_not_paid_for_again(self):
        old = {"frame_times": [1.0, 2.0], "seconds": 300.0, "drawings": [], "tokens": {"in": 36000, "out": 4000}}
        self.assertAlmostEqual(watch.cost(old), 0.224)
        store = Frames([None] * 600)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch.object(eyes, "look", side_effect=AssertionError("asked Claude")):
            run = watch.watcher(None, "x.mp4", {}, Path("."), log=lambda *a: None, saved=old)
            m = run(store, 0.5)                                   # same length: reused, even with no key
            self.assertTrue(m["reused"])
            self.assertEqual(watch.cost(m), 0.0)
            with self.assertRaises(RuntimeError):                 # another length: not this recording
                run(Frames([None] * 100), 0.5)

    def test_finished_videos_get_one_line_of_opinion_and_the_cost_is_counted(self):
        from reels_ai.bot import Runner
        from reels_ai.state import load_config
        from tests.test_bot import FakeBot, fresh_state
        r = Runner(FakeBot(), fresh_state(), load_config())
        results = [{"name": "one_take", "file": "a.mp4"}, {"name": "rewind", "file": "b.mp4"}]
        verdict = {"videos": [{"name": "one_take", "keep": True, "note": "whole drawing in frame"},
                              {"name": "rewind", "keep": False, "note": "the drawing is cut off on the left"}],
                   "tokens": {"in": 3000, "out": 200}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(eyes, "stills", return_value=[b"jpg"]), \
                mock.patch.object(eyes, "judge", return_value=verdict):
            r.look_over(Path("."), results)
        self.assertIn("Looks good", results[0]["look"])
        self.assertIn("Check this one: the drawing is cut off", results[1]["look"])
        self.assertAlmostEqual(sum(r.state["claude_spend"].values()), 0.016)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):       # no key: nothing asked, no crash
            r.look_over(Path("."), [{"name": "x", "file": "x.mp4"}])

    def test_claude_writes_the_captions_and_never_names_an_unlabelled_drawing(self):
        from reels_ai import ai
        from reels_ai.bot import Runner
        from reels_ai.state import load_config
        from tests.test_bot import FakeBot, fresh_state
        r = Runner(FakeBot(), fresh_state(), load_config())
        made = {"hooks": {"one_take": "Watch Goku appear"}, "caption": "Inking Goku by hand", "subject": "Goku",
                "hashtags": ["#goku", "#inking"], "tokens": {"in": 900, "out": 300}}
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k", "GEMINI_API_KEY": ""}), \
                mock.patch.object(ai, "final_frame", return_value=Path("f.jpg")), \
                mock.patch.object(ai, "write_copy_claude", side_effect=lambda *a, **k: json.loads(json.dumps(made))):
            named = r.ai_copy(Path("v.mp4"), {"subject": "Goku"}, ["one_take"], Path("."))
            blank = r.ai_copy(Path("v.mp4"), {}, ["one_take"], Path("."))
        self.assertEqual(named["caption"], "Inking Goku by hand")
        self.assertNotIn("tokens", named)
        self.assertNotIn("Goku", json.dumps(blank))
        self.assertAlmostEqual(sum(r.state["claude_spend"].values()), 0.0192)


class AskClaudeTest(unittest.TestCase):
    """The request itself: right model, structured answer, fallback on, refusals not swallowed."""

    def client(self, stop_reason="end_turn", answer=None):
        sent = {}
        msg = SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5",
                              usage=SimpleNamespace(input_tokens=30000, output_tokens=900),
                              content=[SimpleNamespace(type="thinking", thinking=""),
                                       SimpleNamespace(type="text", text=json.dumps(answer or seen([one("A", 0, 1)], frames=2)))])

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return msg

        def stream(**kw):
            sent.update(kw)
            return Stream()

        return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream))), sent

    def test_request_and_answer(self):
        client, sent = self.client()
        frames = [(0.0, b"\xff\xd8a"), (30.0, b"\xff\xd8b")]
        out = eyes.look(frames, "[0:05] hello", "x.mp4", client=client)
        self.assertEqual(sent["model"], "claude-opus-5-5")
        self.assertEqual(sent["fallbacks"], "default")
        self.assertEqual(sent["betas"], ["server-side-fallback-2026-07-01"])
        self.assertEqual(sent["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("thinking", sent)                                   # always on for this model
        content = sent["messages"][0]["content"]
        self.assertEqual(sum(1 for c in content if c["type"] == "image"), 2)
        self.assertTrue(any("Frame 1 at 0:30" in c.get("text", "") for c in content))
        self.assertEqual((len(out["drawings"]), out["tokens"]["in"]), (1, 30000))

    def test_refusal_and_cut_off_answers_raise(self):
        for reason in ("refusal", "max_tokens"):
            client, _ = self.client(stop_reason=reason)
            with self.assertRaises(RuntimeError):
                eyes.look([(0.0, b"x")], "", "x.mp4", client=client)


class FindPartsWatchedTest(unittest.TestCase):
    """find_parts with a watcher: Claude's drawings become the parts; if watching fails the
    editor measures as before."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        lines = strokes(1, ((120, 260), (240, 400)))
        frames = [page(lines, 1 + i * len(lines) // 300, (60 + (i * 7) % 240, 330 + (i * 5) % 200))
                  for i in range(300)]
        self.src = write(frames, self.tmp / "rec.mp4")

    def test_watched_parts_are_used_and_the_map_is_saved(self):
        from reels_ai.pipeline import find_parts

        def watcher(store, dt):
            n = len(eyes.storyboard(store, dt))
            return {"frame_times": [i * len(store) * dt / n for i in range(n)],
                    **seen([one("A", 0, n // 2 - 1, (0.1, 0.2, 0.6, 0.6), "first"),
                            one("A", n // 2, n - 1, (0.4, 0.4, 0.9, 0.8), "second")], frames=n)}

        parts = find_parts(self.src, self.tmp / "out", watcher=watcher)
        self.assertEqual([p["title"] for p in parts], ["first", "second"])
        self.assertTrue((self.tmp / "out" / "rec_parts" / "edit_map.json").exists())

    def test_watching_failure_falls_back_to_measuring(self):
        from reels_ai.pipeline import find_parts

        def broken(store, dt):
            raise RuntimeError("Claude declined to describe this recording")

        parts = find_parts(self.src, self.tmp / "out2", watcher=broken)
        self.assertEqual(len(parts), 2)                         # the two drawings, found by measuring
        self.assertNotIn("title", parts[0])                     # (measured parts carry no titles)


class EarsTest(unittest.TestCase):
    def test_transcript_text_and_silent_recordings(self):
        lines = [{"t0": 65.0, "t1": 70.0, "text": "now the eyes"}, {"t0": 754.2, "t1": 760.0, "text": "last page"}]
        self.assertEqual(ears.as_text(lines), "[1:05] now the eyes\n[12:34] last page")
        long = [{"t0": float(i), "t1": i + 1.0, "text": "word " * 20} for i in range(2000)]
        self.assertLessEqual(len(ears.as_text(long, limit=5000)), 5200)
        tmp = Path(tempfile.mkdtemp())
        silent = write(drawing(30), tmp / "silent.mp4")                     # a video with no sound
        self.assertEqual(ears.listen(lambda t0: ["-ss", f"{t0}", "-i", str(silent)], tmp, log=lambda *a: None), [])


if __name__ == "__main__":
    unittest.main()


class EarsDecodeTest(unittest.TestCase):
    def test_sound_is_read_without_the_librarys_file_reader(self):
        """transcribe() hands Whisper the samples themselves (its own file reader crashed on GitHub)."""
        import sys
        import wave
        got = {}

        class FakeModel:
            def __init__(self, *a, **k):
                pass

            def transcribe(self, audio, **kw):
                got["audio"] = audio
                return iter([SimpleNamespace(start=1.0, end=2.5, text=" this is Deku landing ")]), None

        tmp = Path(tempfile.mkdtemp()) / "a.wav"
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.sin(np.arange(16000) / 20) * 8000).astype(np.int16).tobytes())
        with mock.patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=FakeModel)}):
            lines = ears.transcribe(tmp)
        self.assertEqual(lines, [{"t0": 1.0, "t1": 2.5, "text": "this is Deku landing"}])
        self.assertEqual((got["audio"].dtype, len(got["audio"])), (np.float32, 16000))
        self.assertLessEqual(float(np.abs(got["audio"]).max()), 1.0)
