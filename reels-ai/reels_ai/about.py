"""'About this drawing': who or what the drawing is, for the one-line topic and a short note.

lookup(name)          from the drawing's name alone: a well-known character, scene or series gets
                      {subject, series, note}; anything else (an ink study, your own design, a name
                      the writer isn't sure of) gets None, and the bot asks you instead
polish(name, words)   what you typed about it, reworded into a clean topic and note

Notes are kept in the bot's memory (about_notes) by name, so a drawing is only asked about once.
Only the name you gave is used: the writer never guesses a character from nothing.
"""
from __future__ import annotations

import os
import re

LOOKUP = """You help an artist who inks drawings by hand describe a drawing video. You get the name \
the artist gave the drawing. If it clearly names a character, scene or page from a well-known \
manga, anime, comic or film, answer "known": true with: "subject" (the character or scene, full \
name, e.g. "Sasuke Uchiha"), "series" (the work it is from, e.g. "Naruto"; empty if the subject is \
the series itself), and "note" (one or two plain sentences: who they are, which series, who \
created it and the year it began if you're sure). Facts only, no hype, no emoji, no hashtags. \
Answer "known": false with empty fields when the name is generic (an ink study, practice, a \
sketch), the artist's own design, or anything you aren't sure of. Never add a character or series \
that the name doesn't state or clearly imply."""

POLISH = """You help an artist who inks drawings by hand describe a drawing video. You get the \
drawing's name and a few words the artist typed about it. Write "subject": a short name for what \
is drawn (under 60 characters, e.g. "an ink study of hands" or "an original samurai character"), \
"series": the work it is from if the artist named one, else empty, and "note": one or two clear, \
warm sentences in the artist's own voice (first person) that say what the drawing is and what \
they were going for. Use only what the artist said; add no facts, no hype, no emoji, no hashtags."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["known", "subject", "series", "note"],
          "properties": {"known": {"type": "boolean"}, "subject": {"type": "string"},
                         "series": {"type": "string"}, "note": {"type": "string"}}}
POLISH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["subject", "series", "note"],
                 "properties": {"subject": {"type": "string"}, "series": {"type": "string"},
                                "note": {"type": "string"}}}

HEADING = "About this drawing"


def key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def route() -> dict | None:
    """Gemini's free tier first when its key is set, Claude after; None when neither is."""
    gem = os.environ.get("GEMINI_API_KEY", "").strip()
    claude = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if not (gem or claude):
        return None
    return {"first": "gemini" if gem else "claude", "gemini_key": gem, "claude": claude}


def _asker(model: str, how: dict | None):
    from .llm import ask as _ask
    return lambda s, sc, t: _ask(s, sc, t, model, max_tokens=500, route=how)


def _clean(t) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def lookup(name: str, model: str = "claude-opus-5-5", how: dict | None = None, ask=None) -> dict | None:
    """{subject, series, note} for a well-known drawing, or None (unknown, unsure, no model)."""
    name = _clean(name)
    how = how if how is not None else route()
    if not name or (how is None and ask is None):
        return None
    try:
        out = (ask or _asker(model, how))(LOOKUP, SCHEMA, f"The drawing's name: {name}")
    except Exception as e:  # noqa: BLE001 - asked again next time
        print(f"warn: about this drawing: {str(e)[:150]}")
        return None
    info = {"subject": _clean(out.get("subject"))[:80], "series": _clean(out.get("series"))[:80],
            "note": _clean(out.get("note"))[:400]}
    if not out.get("known") or len(info["note"]) < 15 or not info["subject"] or "#" in info["note"]:
        return None
    return info


def polish(name: str, words: str, model: str = "claude-opus-5-5", how: dict | None = None, ask=None) -> dict:
    """Your words about a drawing, reworded. Without a model they are used as you typed them."""
    words = _clean(words)
    plain = {"subject": _clean(name) or "this drawing", "series": "", "note": words[:400]}
    how = how if how is not None else route()
    if how is None and ask is None:
        return plain
    try:
        out = (ask or _asker(model, how))(POLISH, POLISH_SCHEMA, f"The drawing's name: {name}\nWhat the artist said: {words}")
    except Exception as e:  # noqa: BLE001
        print(f"warn: polishing the note: {str(e)[:150]}")
        return plain
    note = _clean(out.get("note"))[:400]
    return {"subject": _clean(out.get("subject"))[:80] or plain["subject"], "series": _clean(out.get("series"))[:80],
            "note": note if len(note) >= 10 and "#" not in note else plain["note"]}


def about(name: str, model: str = "claude-opus-5-5", how: dict | None = None, ask=None) -> str:
    """Just the note (older callers)."""
    info = lookup(name, model, how, ask)
    return info["note"] if info else ""


def with_about(desc: str, note: str) -> str:
    """The description with an 'About this drawing' section above the chapters (or after the
    opening lines). An existing section is left as it is."""
    if not note or HEADING.lower() in (desc or "").lower():
        return desc
    parts = (desc or "").split("\n\n")
    block = f"{HEADING}\n{note}"
    at = next((i for i, p in enumerate(parts) if p.startswith("Chapters")), None)
    if at is None:
        at = min(2, len(parts))
    return "\n\n".join(parts[:at] + [block] + parts[at:])
