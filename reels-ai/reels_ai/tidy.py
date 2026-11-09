"""Drive tidy-up: move leftover duplicate Shorts in Done/Shorts to the Trash.

Re-renders (/redo) and a crashed run left several copies of the same video. A file is a leftover
when the bot's memory says so, never by name alone (different drawings of one character share a
folder and file names):
  - it belongs to a video that was replaced by a re-render, or
  - nothing references it, and the same folder holds a file for the same style that a video in
    the bank (or already posted) does reference.
Everything goes to Drive's Trash, restorable for 30 days.
"""
from __future__ import annotations

import re

LINK_ID = re.compile(r"/d/([A-Za-z0-9_-]{10,})")


def _style_key(name: str) -> str:
    """'Naruto - Speedrun (redo).mp4' -> 'naruto - speedrun'."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "")
    return re.sub(r"\s*\((redo|\d+)\)\s*$", "", stem).strip().lower()


def plan(drive, state: dict) -> list[tuple[str, dict]]:
    """[(folder name, file)] to move to the Trash. Reads Drive, changes nothing."""
    live, replaced = set(), set()
    for v in state.get("variants", {}).values():
        m = LINK_ID.search(v.get("full_link") or "")
        if m:
            (replaced if v.get("status") == "replaced" else live).add(m.group(1))
    out = []
    for folder in drive.list(drive.folder("Done/Shorts"), "folder"):
        files = drive.list(folder["id"], "video")
        kept_styles = {_style_key(f["name"]) for f in files if f["id"] in live}
        for f in files:
            if f["id"] in live:
                continue
            if f["id"] in replaced or _style_key(f["name"]) in kept_styles:
                out.append((folder["name"], f))
    return out


def run(drive, state: dict) -> str:
    todo = plan(drive, state)
    if not todo:
        return "\U0001f9f9 Drive tidy: no duplicate Shorts found in Done \u203a Shorts."
    moved, failed = [], 0
    for folder, f in todo:
        try:
            drive.trash(f["id"])
            moved.append((folder, f))
        except Exception as e:  # noqa: BLE001
            print(f"warn: couldn't trash {f['name']}: {e}")
            failed += 1
    gb = sum(int(f.get("size") or 0) for _, f in moved) / 1e9
    by: dict[str, int] = {}
    for folder, _ in moved:
        by[folder] = by.get(folder, 0) + 1
    lines = [f"\U0001f9f9 Drive tidy: moved {len(moved)} duplicate Short(s) ({gb:.1f} GB) to the Trash "
             "(restorable for 30 days):"] + [f"\u2022 Done \u203a Shorts \u203a {k}: {n}" for k, n in by.items()]
    lines.append("Kept: every video in your bank and everything already posted.")
    if failed:
        lines.append(f"\u26a0\ufe0f {failed} file(s) couldn't be moved; they're unchanged.")
    return "\n".join(lines)
