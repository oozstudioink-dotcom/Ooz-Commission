"""Inbox tools: re-run what is stuck there, join recordings you split, re-upload a long video.

/inbox              every video still in Drive > Inbox and what happened to it; anything that
                    never became Shorts (failed, waiting for a name, never seen) is queued again
/combine <name>     the recordings named <name> 1, <name> 2 … become ONE recording: Shorts are made
                    from all of them in order (found in Inbox, or wherever the bot already filed them).
                    /combine Vegeta01 and Vegeta02 joins exactly those, in that order
/reupload <name>    a finished long video in Drive > Done > Longform is uploaded to YouTube again
                    (privately, with its saved title, description and thumbnail)
"""
from __future__ import annotations

from pathlib import Path

from ..state import iso, new_id
from .common import _norm_name, dispatch_workflow

DONE = ("rendered",)
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv"}


def _tokens(text: str) -> list[str]:
    """Words and numbers apart: 'Vegeta01 - ink only' -> ['vegeta', '01', 'ink', 'only']."""
    import re
    return re.findall(r"[a-z]+|\d+", _norm_name(text))


def _names(arg: str) -> list[str]:
    """'/combine Vegeta01 and Vegeta02' (or 'Vegeta01, Vegeta02' / 'Vegeta01 + Vegeta02'): the exact
    recordings, in the order given. One name: []."""
    import re
    parts = [p.strip() for p in re.split(r"\s*(?:,|\+|&|\band\b|\bthen\b)\s*", arg or "", flags=re.I) if p.strip()]
    return parts if len(parts) > 1 else []


class InboxToolsMixin:
    def inbox_files(self) -> list[dict]:
        return self.drive.list(self.drive.folder("Inbox"), "video")

    def inbox_command(self) -> None:
        if not self.drive:
            return self.say("The Inbox needs Google Drive connected.")
        try:
            files = self.inbox_files()
        except Exception as e:  # noqa: BLE001
            return self.drive_problem(e)
        if not files:
            return self.say("\U0001f4e5 Drive › Inbox is empty: everything there has been made into Shorts.")
        seen = self.state.setdefault("drive_seen", [])
        lines, queued = [], 0
        for f in files:
            mine = [(did, d) for did, d in self.state["drawings"].items()
                    if f["id"] in (d.get("drive_ids") or [d.get("drive_id")]) and d["status"] not in ("replaced", "combined")]
            if not mine:
                if f["id"] in seen:
                    seen.remove(f["id"])                     # picked up again on the next look
                lines.append(f"• {f['name']}: never made, picking it up now")
                queued += 1
                continue
            did, d = mine[-1]
            st = d["status"]
            if st in DONE:
                lines.append(f"• {f['name']}: Shorts made (the file just wasn't moved out)")
            elif st in ("queued",):
                lines.append(f"• {f['name']}: waiting its turn")
            elif st == "naming":
                d["status"] = "queued"
                lines.append(f"• {f['name']}: was waiting for a name, making it now without one")
                queued += 1
            else:                                            # failed, held, set aside
                self.requeue(did, d)
                if d.get("status") != "queued":              # nothing was made yet: start fresh
                    d.update(status="queued")
                d.pop("keep_in_place", None)                 # moved out of Inbox once it's done
                d["redo_styles"] = None
                lines.append(f"• {f['name']}: {st} before ({str(d.pop('error', '') or '')[:60]}), trying again")
                queued += 1
        self.say(f"\U0001f4e5 Drive › Inbox: {len(files)} video(s)\n" + "\n".join(lines)
                 + (f"\n\n{queued} queued: Shorts arrive in /videos as each one finishes." if queued else ""))

    def combine_candidates(self, want: str) -> list[dict]:
        """Recordings whose names hold every word of `want` (part numbers aside): the ones in Inbox,
        and numbered parts the bot already took in over the last two weeks, wherever they are now
        (the bot starts on each new recording within minutes and then moves it out of Inbox)."""
        from datetime import timedelta
        from ..drive import part_number
        from ..state import parse
        words = [w for w in _tokens(want) if not w.isdigit()]
        hit = lambda name: bool(words) and all(w in _tokens(Path(name).stem) for w in words)  # noqa: E731
        out: dict[str, dict] = {}
        for f in self.inbox_files():
            if hit(f["name"]):
                out[f["id"]] = {**f, "in_inbox": True}
        since = self.clock() - timedelta(days=14)
        for d in self.state["drawings"].values():
            fid, name = d.get("drive_id"), d.get("drive_name") or ""
            if (d.get("source") != "drive" or d.get("drive_ids") or not fid or fid in out
                    or part_number(name) is None or not hit(name)):
                continue
            try:
                if d.get("received") and parse(d["received"]) < since:
                    continue
            except ValueError:
                pass
            out[fid] = {"id": fid, "name": name, "in_inbox": False,
                        "videoMediaMetadata": {"durationMillis": str(int((d.get("duration") or 0) * 1000))}}
        return list(out.values())

    def named_recordings(self, names: list[str]) -> tuple[list[dict], list[str]]:
        """The recordings with exactly these names (".MOV" optional), wherever they are: Inbox first,
        then any recording the bot took in. Returns (found in the order given, names not found)."""
        key = lambda n: _norm_name(Path(n).stem if Path(n).suffix.lower() in VIDEO_EXT else n)  # noqa: E731
        pool: dict[str, dict] = {}
        for f in self.inbox_files():
            pool.setdefault(key(f["name"]), {**f, "in_inbox": True})
        for d in self.state["drawings"].values():
            fid, name = d.get("drive_id"), d.get("drive_name") or ""
            if d.get("source") == "drive" and fid and not d.get("drive_ids") and key(name) not in pool:
                pool[key(name)] = {"id": fid, "name": name, "in_inbox": False,
                                   "videoMediaMetadata": {"durationMillis": str(int((d.get("duration") or 0) * 1000))}}
        found, missing = [], []
        for n in names:
            f = pool.get(key(n))
            if f and f not in found:
                found.append(f)
            elif not f:
                missing.append(n)
        return found, missing

    def combined_drawing(self, files: list[dict], label: str, ordered: bool = False) -> tuple[str, int]:
        """One drawing made from several recordings, in part order (or as given: ordered=True). The
        separate pieces' Shorts leave the library. Returns (drawing id, Shorts replaced)."""
        from ..drive import parse_name, session_order
        files = list(files) if ordered else sorted(files, key=session_order)
        ids = [f["id"] for f in files]
        replaced = 0
        for did, d in self.state["drawings"].items():       # the separate pieces stop on their own
            if d.get("drive_id") in ids and not d.get("drive_ids") and d["status"] not in ("replaced", "combined"):
                for v in self.state["variants"].values():
                    if v["drawing"] == did and v["status"] in ("approved", "pending") and not v.get("dropped_at"):
                        v["status"] = "replaced"
                        replaced += 1
                d["status"] = "combined"
        seen = self.state.setdefault("drive_seen", [])
        seen.extend(i for i in ids if i not in seen)
        subject, hints = parse_name(label)
        durs = [int((f.get("videoMediaMetadata") or {}).get("durationMillis", 0) or 0) / 1000 for f in files]
        in_inbox = [f["id"] for f in files if f.get("in_inbox", True)]
        did = new_id(self.state, "d")
        self.state["drawings"][did] = {
            "source": "drive", "drive_id": ids[0], "drive_ids": ids, "drive_durations": durs,
            "drive_from_inbox": in_inbox, "drive_name": f"{label.strip()} (combined)", "drive_where": "Inbox",
            "subject": subject or label.strip(), "received": iso(self.clock()), "duration": sum(durs) or None,
            "status": "queued",
            **({"keep_in_place": True} if len(in_inbox) < len(ids) else {}),   # parts already filed stay put
            **({"hints": {k: v for k, v in hints.items() if k != "unknown"}} if hints else {})}
        return did, replaced

    def combine_command(self, arg: str) -> None:
        if not self.drive:
            return self.say("Combining needs Google Drive connected.")
        want = _norm_name(arg)
        if not [w for w in want.split() if not w.isdigit()]:
            return self.say("Tell me which recordings: /combine Vegeta01 and Vegeta02 joins exactly those two, "
                            "in that order. /combine Gojo joins every numbered Gojo part (Gojo 1, Gojo 2 …).")
        from ..drive import parse_name, session_order
        names = _names(arg)
        if names:                                       # the exact recordings, in the order given
            try:
                files, missing = self.named_recordings(names)
            except Exception as e:  # noqa: BLE001
                return self.drive_problem(e)
            if missing or len(files) < 2:
                return self.say(f"I couldn't find {', '.join(missing) or 'two different recordings'}. Use the "
                                "file names as they are in Drive, e.g. /combine Vegeta01 and Vegeta02.")
            label = parse_name(files[0]["name"])[0] or Path(files[0]["name"]).stem
            did, replaced = self.combined_drawing(files, label, ordered=True)
            return self.say(f"\U0001f517 Joining {len(files)} recordings into one Short set, in this order:\n"
                            + "\n".join(f"{k}. {f['name']}" for k, f in enumerate(files, 1))
                            + (f"\n{replaced} Short(s) made from the separate pieces left your library." if replaced else "")
                            + "\nShorts arrive in /videos when it's done.")
        try:
            files = self.combine_candidates(want)
        except Exception as e:  # noqa: BLE001
            return self.drive_problem(e)
        if len(files) < 2:
            return self.say(f"I need at least two recordings with “{arg}” in their names, numbered like "
                            f"{arg} 1.mov, {arg} 2.mov (found {len(files)}: "
                            f"{', '.join(f['name'] for f in files) or 'none'}). I look in Drive › Inbox and at "
                            "the numbered recordings I took in over the last two weeks.")
        files.sort(key=session_order)
        did, replaced = self.combined_drawing(files, arg.strip())
        self.say(f"\U0001f517 Joining {len(files)} recordings into one, in this order:\n"
                 + "\n".join(f"{k}. {f['name']}" for k, f in enumerate(files, 1))
                 + (f"\n{replaced} Short(s) made from the separate pieces left your library." if replaced else "")
                 + "\nShorts arrive in /videos when it's done.")

    def rethumb_command(self, arg: str) -> None:
        name, _, ep = arg.strip().rpartition(" ")
        if not (name.strip() and ep.isdigit()):
            return self.say("Tell me the video and its new number: /rethumb Afro Samurai 2")
        try:
            dispatch_workflow("longform.yml", {"project": "-", "rethumb": f"{name.strip()} {int(ep)}"})
            self.say(f"\U0001f5bc Re-numbering \u201c{name.strip()}\u201d as episode {int(ep):02d}: new thumbnail, Drive "
                     "folder name and YouTube title. I'll message you when it's done.")
        except Exception as e:  # noqa: BLE001
            self.say(f"\u26a0\ufe0f Couldn't start it: {str(e)[:200]}")

    def reupload_command(self, arg: str) -> None:
        if not arg.strip():
            return self.say("Which video? /reupload Afro Samurai (any part of its folder name in "
                            "Drive › Done › Longform).")
        try:
            dispatch_workflow("longform.yml", {"project": "-", "reupload": arg.strip()})
            self.say(f"\U0001f4e4 Re-uploading “{arg.strip()}” to YouTube. It goes up privately with its "
                     "episode number from its Drive folder, its newest thumbnail and your current description; "
                     "I'll message you the link. If an older copy is on YouTube, delete it in YouTube Studio.")
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠️ Couldn't start the re-upload: {str(e)[:200]}")
