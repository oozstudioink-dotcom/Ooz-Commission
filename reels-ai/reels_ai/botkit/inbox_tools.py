"""Inbox tools: re-run what is stuck there, join recordings you split, re-upload a long video.

/inbox              every video still in Drive > Inbox and what happened to it; anything that
                    never became Shorts (failed, waiting for a name, never seen) is queued again
/combine <name>     the Inbox recordings whose names contain <name> (e.g. "Gojo 1", "Gojo 2")
                    become ONE recording: Shorts are made from all of them in order
/reupload <name>    a finished long video in Drive > Done > Longform is uploaded to YouTube again
                    (privately, with its saved title, description and thumbnail)
"""
from __future__ import annotations

from pathlib import Path

from ..state import iso, new_id
from .common import _norm_name, dispatch_workflow

DONE = ("rendered",)


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

    def combine_command(self, arg: str) -> None:
        if not self.drive:
            return self.say("Combining needs Google Drive connected.")
        want = _norm_name(arg)
        if not want:
            return self.say("Tell me the shared part of the names: /combine Gojo joins “Gojo 1”, "
                            "“Gojo 2” … from your Inbox into one recording.")
        from ..drive import parse_name, session_order
        try:
            files = [f for f in self.inbox_files() if want in _norm_name(Path(f["name"]).stem)]
        except Exception as e:  # noqa: BLE001
            return self.drive_problem(e)
        if len(files) < 2:
            return self.say(f"I need at least two Inbox recordings with “{arg}” in their names "
                            f"(found {len(files)}). Name them like {arg} 1.mov, {arg} 2.mov.")
        files.sort(key=session_order)
        ids = [f["id"] for f in files]
        replaced = 0
        for did, d in self.state["drawings"].items():       # the separate pieces stop on their own
            if d.get("drive_id") in ids and d["status"] not in ("replaced", "combined"):
                for v in self.state["variants"].values():
                    if v["drawing"] == did and v["status"] in ("approved", "pending") and not v.get("dropped_at"):
                        v["status"] = "replaced"
                        replaced += 1
                d["status"] = "combined"
        seen = self.state.setdefault("drive_seen", [])
        seen.extend(i for i in ids if i not in seen)
        subject, hints = parse_name(arg)
        durs = [int((f.get("videoMediaMetadata") or {}).get("durationMillis", 0)) / 1000 for f in files]
        did = new_id(self.state, "d")
        self.state["drawings"][did] = {
            "source": "drive", "drive_id": ids[0], "drive_ids": ids, "drive_durations": durs,
            "drive_name": f"{arg.strip()} (combined)", "drive_where": "Inbox", "subject": subject or arg.strip(),
            "received": iso(self.clock()), "duration": sum(durs) or None, "status": "queued",
            **({"hints": {k: v for k, v in hints.items() if k != "unknown"}} if hints else {})}
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
