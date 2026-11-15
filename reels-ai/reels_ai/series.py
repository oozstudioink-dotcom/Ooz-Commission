"""Series: recordings that belong to a set (a book you are studying, a course) and get their own
folder, numbering and thumbnail instead of the LongDraw ones.

A series is one [[series]] block in config.toml: a name, the Drive folder under Longform that
holds its lessons, the words in a file name that mark a recording as part of it, and how its
thumbnail and titles read. A recording dropped in Inbox whose name matches goes, once its Shorts
are made, to Longform > <folder> > <recording name>, ready for /longform.
"""
from __future__ import annotations

import re


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def all_series(cfg: dict) -> list[dict]:
    return [s for s in cfg.get("series", []) if s.get("name") and s.get("folder")]


def match(cfg: dict, file_name: str) -> dict | None:
    """The series a recording belongs to, from the words in its name."""
    name = f" {_norm(file_name)} "
    for s in all_series(cfg):
        if any(f" {_norm(w)} " in name for w in s.get("match", [])):
            return s
    return None


def of_path(cfg: dict, project_path: str) -> dict | None:
    """The series a Longform project path sits in ('Educational Drawing/How to Draw/...')."""
    p = project_path.strip("/").lower() + "/"
    for s in all_series(cfg):
        if p.startswith(s["folder"].strip("/").lower() + "/"):
            return s
    return None


def roots(cfg: dict) -> set[str]:
    """Top-level Longform folders that hold series, not projects."""
    return {s["folder"].strip("/").split("/")[0].lower() for s in all_series(cfg)}


def brand(s: dict | None) -> dict:
    """How a video is dressed: thumbnail words, the badge word, a small line over the art."""
    if not s:
        return {"words": ("LONG", "DRAW"), "badge": "EP.", "kicker": "", "label": "LongDraw EP."}
    w = s.get("words") or ["STUDY", "SERIES"]
    return {"words": (str(w[0]), str(w[1])), "badge": s.get("badge", "LESSON"),
            "kicker": s.get("kicker", ""), "label": f"{s['name']} | {s.get('badge', 'Lesson').title()}",
            "name": s["name"]}
