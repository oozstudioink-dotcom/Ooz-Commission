"""Titles for long videos, written to be found and clicked: the bot does this by itself when a
long video is built (drawing videos and lessons each have their own brief).

Gemini's free tier first, Claude when it's busy; the plain template title when neither answers.
Never clickbait, never the words on your avoid list ([ai] avoid_words), never an episode number
(that's on the thumbnail and the first line of the description).
"""
from __future__ import annotations

import re

DRAWING = """You write the YouTube title for a long video of a comic artist drawing and inking one \
piece by hand, start to end, sped up. Put the words people search for first: the character's full \
name and the series, then what the video is (e.g. "drawing", "manga inking", "timelapse"). Make it \
specific and inviting, 45 to 70 characters, Title Case, no all caps, no emoji, no hashtags, no \
episode numbers, no clickbait or claims the video doesn't deliver. Keep it clean: nothing that \
could read two ways, and never these words: {avoid}."""

LESSON = """You write the YouTube title for a drawing lesson by a comic artist who draws by hand. \
Lead with what the viewer learns, in the words people search for (e.g. "How to Draw Hands", "Inking \
Hair for Beginners"), 45 to 70 characters, Title Case, no all caps, no emoji, no hashtags, no \
lesson numbers, no clickbait. Keep it clean: nothing that could read two ways, and never these \
words: {avoid}."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["title"],
          "properties": {"title": {"type": "string"}}}


def clean(title: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip().strip('"“”')
    title = re.sub(r"\s*#\w+", "", title)
    return title[:100]


def write_title(name: str, info: dict | None, lesson: bool, fallback: str, model: str = "claude-opus-5-5",
                how: dict | None = None, ask=None) -> str:
    """A click-worthy, honest title for the video, or `fallback` when no model answers."""
    from .about import route
    from .hooks import avoid_words, avoided
    info = info or {}
    how = how if how is not None else route()
    if how is None and ask is None:
        return fallback
    brief = (LESSON if lesson else DRAWING).format(avoid=", ".join(f'"{w}"' for w in avoid_words()))
    facts = (f"The video: {name}\nWhat it is: {info.get('subject') or name}"
             + (f"\nSeries / source: {info['series']}" if info.get("series") else "")
             + (f"\nAbout it: {info['note']}" if info.get("note") else ""))
    if ask is None:
        from .llm import ask as _ask
        ask = lambda s, sc, t: _ask(s, sc, t, model, max_tokens=200, route=how)  # noqa: E731
    try:
        title = clean(ask(brief, SCHEMA, facts).get("title"))
    except Exception as e:  # noqa: BLE001 - the template title is fine
        print(f"warn: title: {str(e)[:150]}")
        return fallback
    if len(title) < 15 or avoided(title) or re.search(r"\b(EP|Episode|Lesson)\.?\s*\d", title, re.I) \
            or title.isupper():
        return fallback
    return title
