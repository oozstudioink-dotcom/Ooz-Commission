"""'About this drawing' for LongDraw descriptions: a sentence or two on the character and the
manga or comic it comes from, when the drawing is of a well-known one.

Only the name you gave the drawing is used (its folder name or the video title): the writer is
told never to guess a character from nothing, and to answer with nothing when it isn't sure.
"""
from __future__ import annotations

import os
import re

SYSTEM = """You write one short note for the description of a drawing video by a comic artist \
and character designer who inks by hand. You get the name the artist gave the drawing. If that \
name clearly is a character (or a scene) from a well-known manga, anime or comic, write one or \
two plain sentences: who the character is, which series they're from, and who created it, with \
the year it began if you are sure of it. Facts only, no hype, no emoji, no hashtags. Write \
"known": false and an empty note when the name isn't a known character or series, when it is \
the artist's own design, or when you aren't sure. Never add a character or series that isn't \
named or clearly implied by the name given."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["known", "note"],
          "properties": {"known": {"type": "boolean"}, "note": {"type": "string"}}}

HEADING = "About this drawing"


def route() -> dict | None:
    """Gemini's free tier first when its key is set, Claude after; None when neither is."""
    gem = os.environ.get("GEMINI_API_KEY", "").strip()
    claude = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if not (gem or claude):
        return None
    return {"first": "gemini" if gem else "claude", "gemini_key": gem, "claude": claude}


def about(name: str, model: str = "claude-opus-5-5", how: dict | None = None, ask=None) -> str:
    """The note for a drawing named `name`, or "" (unknown, unsure, or no model to ask)."""
    name = re.sub(r"\s+", " ", (name or "")).strip()
    how = how if how is not None else route()
    if not name or (how is None and ask is None):
        return ""
    if ask is None:
        from .llm import ask as _ask
        ask = lambda s, sc, t: _ask(s, sc, t, model, max_tokens=400, route=how)  # noqa: E731
    try:
        out = ask(SYSTEM, SCHEMA, f"The drawing's name: {name}")
    except Exception as e:  # noqa: BLE001 - a description without the note is fine
        print(f"warn: about this drawing: {str(e)[:150]}")
        return ""
    note = re.sub(r"\s+", " ", str(out.get("note") or "")).strip()
    if not out.get("known") or len(note) < 15 or "#" in note:
        return ""
    return note[:400]


def with_about(desc: str, note: str) -> str:
    """The description with an 'About this drawing' section above the chapters (or after the
    opening lines). An existing section is left as it is."""
    if not note or HEADING.lower() in (desc or "").lower():
        return desc
    parts = (desc or "").split("\n\n")
    block = f"{HEADING}\n{note}"
    at = next((i for i, p in enumerate(parts) if p.startswith("Chapters")), None)
    if at is None:
        from .audit import EP_LINE
        at = 2 if parts and EP_LINE.match(parts[0].split("\n", 1)[0]) else 1
        at = min(at, len(parts))
    return "\n\n".join(parts[:at] + [block] + parts[at:])
