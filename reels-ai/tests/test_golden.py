"""The test-set checker itself: does it report the right differences?"""
from __future__ import annotations

import unittest

from reels_ai.golden import compare, find_recording, load


def part(a: float, b: float) -> dict:
    return {"t0": a * 60, "t1": b * 60}


OK = {"passed": 1, "held": []}


class CompareTest(unittest.TestCase):
    def test_right_answer_has_no_problems(self):
        exp = {"name": "x", "drawings": 3, "minutes": [[1, 11], [16, 27], [27, 32]]}
        self.assertEqual(compare(exp, [part(1.2, 10.9), part(16.1, 27.5), part(27.5, 32.4)], [OK] * 3), [])
        self.assertEqual(compare({"name": "y", "drawings": 1}, [], [OK]), [])      # one drawing, no split

    def test_wrong_count_is_reported_with_where(self):
        exp = {"name": "x", "drawings": 3}
        found = [part(1.2, 2.1), part(3.7, 14.6), part(4.9, 14.6), part(16.1, 28.8), part(28.8, 33.4)]
        problems = compare(exp, found, [OK] * 5)
        self.assertEqual(len(problems), 1)
        self.assertIn("found 5 drawing(s), should be 3", problems[0])
        self.assertIn("3.7-14.6", problems[0])

    def test_wrong_minutes_and_reviewer_failures_are_reported(self):
        exp = {"name": "x", "drawings": 2, "minutes": [[1, 11], [16, 27]]}
        held = {"passed": 0, "held": [{"name": "one_take", "reasons": ["shows a different page (around 9s)"]}]}
        problems = compare(exp, [part(1, 11), part(20, 27)], [OK, held])
        self.assertTrue(any("drawing 2 cut at minutes 20.0-27.0, should be 16-27" in p for p in problems))
        self.assertTrue(any("drawing 2: no Short passed the reviewer (shows a different page" in p for p in problems))

    def test_file_is_valid_and_recordings_are_found_by_loose_name(self):
        cases = load()
        self.assertTrue(all("name" in c and int(c.get("drawings", 1)) >= 1 for c in cases))
        self.assertEqual(compare({"name": "no count yet"}, [part(0, 5), part(5, 9)], [OK, OK]), [])

        class D:
            def folder(self, p=""):
                return "id:" + p

            def list(self, fid, kind="video"):
                if kind == "video" and fid == "id:Inbox/Processed":
                    return [{"id": "f1", "name": "Lets+Go+LIVE!.mp4", "createdTime": "2026-11-09"},
                            {"id": "f2", "name": "Kakashi.MOV", "createdTime": "2026-11-10"}]
                return []

        self.assertEqual(find_recording(D(), "Lets Go LIVE")["id"], "f1")
        self.assertIsNone(find_recording(D(), "Sasuke"))


if __name__ == "__main__":
    unittest.main()
