"""The bot's memory in Drive: backups, repair, and refusing to lose data."""
from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from reels_ai.memory import DriveMemory, MemoryError_, tidy
from reels_ai.state import EMPTY


class FakeDrive:
    def __init__(self):
        self.files: dict[str, dict] = {}        # id -> {"name", "folder", "text"}
        self.n = 0
        self.broken = False

    def folder(self, path=""):
        if self.broken:
            raise OSError("502 from Drive")
        return "id:" + path

    def find_file(self, name, folder_id):
        return next(({"id": k, "name": v["name"]} for k, v in self.files.items()
                     if v["name"] == name and v["folder"] == folder_id), None)

    def read_text(self, fid):
        return self.files[fid]["text"]

    def write_text(self, name, folder_id, text, file_id=None, mime="application/json"):
        if file_id is None:
            self.n += 1
            file_id = f"f{self.n}"
        self.files[file_id] = {"name": name, "folder": folder_id, "text": text}
        return file_id

    def list(self, folder_id, kind=""):
        return [{"id": k, "name": v["name"]} for k, v in self.files.items() if v["folder"] == folder_id]

    def put(self, name, folder, obj_or_text):
        text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
        return self.write_text(name, "id:" + folder, text)


def memory(n_drawings=12, n_variants=30) -> dict:
    return {**json.loads(json.dumps(EMPTY)), "owner_chat_id": 7, "next_id": 99,
            "drawings": {f"d{i}": {"status": "rendered"} for i in range(n_drawings)},
            "variants": {f"v{i}": {"drawing": "d0", "status": "approved"} for i in range(n_variants)}}


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.d = FakeDrive()
        self.local = Path(tempfile.mkdtemp()) / "state" / "state.json"
        self.day = ["2026-11-11"]
        self.mem = DriveMemory(self.d, today=lambda: self.day[0])

    def backups(self):
        return sorted(v["name"] for v in self.d.files.values() if v["folder"] == "id:Bot memory/Backups")

    def test_one_backup_a_day_of_the_memory_as_it_was(self):
        self.d.put("state.json", "Bot memory", memory())
        state = self.mem.load(self.local)
        state["drawings"]["d99"] = {"status": "queued"}
        self.mem.save(state, self.local)
        self.mem.save(state, self.local)                                   # same day: no second copy
        self.assertEqual(self.backups(), ["state-2026-11-11.json"])
        kept = json.loads(next(v["text"] for v in self.d.files.values() if v["name"] == "state-2026-11-11.json"))
        self.assertNotIn("d99", kept["drawings"])                          # as loaded, before today's changes
        self.day[0] = "2026-11-12"
        mem2 = DriveMemory(self.d, today=lambda: self.day[0])
        mem2.save(mem2.load(self.local), self.local)
        self.assertEqual(self.backups(), ["state-2026-11-11.json", "state-2026-11-12.json"])

    def test_a_save_that_would_lose_most_of_the_memory_is_refused(self):
        fid = self.d.put("state.json", "Bot memory", memory())
        self.mem.load(self.local)
        with self.assertRaises(MemoryError_):
            self.mem.save(json.loads(json.dumps(EMPTY)), self.local)       # a blank memory
        self.assertEqual(len(json.loads(self.d.files[fid]["text"])["drawings"]), 12)   # Drive untouched
        ok = memory()
        ok["drawings"].pop("d0")                                           # normal small changes are fine
        self.mem.save(ok, self.local)

    def test_damaged_memory_is_restored_from_the_newest_good_backup(self):
        self.d.put("state.json", "Bot memory", '{"drawings": {"d1": ')    # cut off mid-write
        self.d.put("state-2026-11-09.json", "Bot memory/Backups", memory(5))
        self.d.put("state-2026-11-10.json", "Bot memory/Backups", memory(8))
        self.d.put("state-2026-11-11.json", "Bot memory/Backups", "not json")
        state = self.mem.load(self.local)
        self.assertEqual(len(state["drawings"]), 8)
        self.assertEqual(state["memory_restored"], "state-2026-11-10.json")

    def test_damaged_memory_without_backups_and_unreadable_drive_stop_the_run(self):
        self.d.put("state.json", "Bot memory", "garbage")
        with self.assertRaises(MemoryError_):
            self.mem.load(self.local)
        self.d.broken = True
        with self.assertRaises(MemoryError_):
            DriveMemory(self.d).load(self.local)

    def test_a_failed_save_leaves_no_copy_on_the_machine(self):
        """A copy left behind once ended up in the public repo: none is written unless Drive has it."""
        self.d.put("state.json", "Bot memory", memory())
        state = self.mem.load(self.local)
        self.d.write_text = mock.Mock(side_effect=OSError("401 from Drive"))
        with self.assertRaises(OSError):
            self.mem.save(state, self.local)
        self.assertFalse(self.local.exists())
        self.assertFalse((self.local.parent / ".in_drive").exists())

    def test_google_tokens_are_renewed_before_they_run_out(self):
        from reels_ai import drive as drv
        d = drv.Drive("id", "secret", "refresh")
        asked = []

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s, *a): return json.dumps({"access_token": f"t{len(asked)}"}).encode()
        t = [1000.0]
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: (asked.append(1), R())[1]), \
                mock.patch("time.time", side_effect=lambda: t[0]):
            self.assertEqual((d.token(), d.token()), ("t1", "t1"))     # reused while fresh
            t[0] += 41 * 60
            self.assertEqual(d.token(), "t2")                          # asked again after 40 minutes

    def test_tidy_repairs_shapes(self):
        state = tidy({"drawings": {"d1": {"status": "queued"}, "d2": "oops"}, "variants": [], "next_id": 5})
        self.assertEqual(list(state["drawings"]), ["d1"])
        self.assertEqual(state["variants"], {})
        self.assertEqual((state["next_id"], state["paused"], state["schema"]), (5, False, 2))
        with self.assertRaises(ValueError):
            tidy(["not", "a", "memory"])


if __name__ == "__main__":
    unittest.main()
