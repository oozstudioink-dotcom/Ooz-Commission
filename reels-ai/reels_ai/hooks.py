"""Hook text, end cards and captions. Rotated per variant; later stages learn which perform best.

`{subject}` is the character / piece name (optional), `{secs}` the clip length.
Templates that need a subject are skipped when none is given.
"""
from __future__ import annotations

import random

HOOKS = {
    "reveal_first": [
        "Wait for the ink",
        "From a blank page to this",
        "This started as a circle",
        "Inking {subject} by hand",
        "No tablet. Just ink.",
        "Watch this come to life",
    ],
    "speedrun": [
        "Sketch → ink in {secs} seconds",
        "{subject} in {secs} seconds",
        "Blink and you'll miss it",
        "Pencil to ink speedrun",
    ],
    "before_after": [
        "Sketch vs. ink",
        "Pencil vs. ink. Which is better?",
        "Before / after inking",
        "What inking actually does",
    ],
    "detail_zoom": [
        "Watch the details",
        "The linework is everything",
        "Every line by hand",
        "Zoom in on this linework",
    ],
    "ink_only": [
        "Just the ink.",
        "The most satisfying part",
        "Inking {subject}, no talking",
        "Skip the sketch. Here's the ink.",
    ],
}

END_CARDS = [
    "Who should I ink next?",
    "Comment a character for next time",
    "Part 2?",
    "Follow for the next piece",
]

CAPTIONS = [
    "Inking {subject_or_this} by hand ✍️ Who should I draw next?",
    "Pencil to ink, start to finish. Which stage do you like more?",
    "Brush pen on paper, no tablet. Drop a character for the next one \U0001f447",
    "The ink stage never gets old. Save this for inking reference.",
]

BASE_TAGS = ["#mangaart", "#inking", "#animeart", "#timelapse", "#drawing", "#mangadrawing",
             "#ink", "#traditionalart", "#fanart", "#artprocess"]


def _fill(tpl: str, subject: str | None, secs: int) -> str | None:
    if "{subject}" in tpl and not subject:
        return None
    return tpl.format(subject=subject or "", secs=secs,
                      subject_or_this=subject or "this piece").strip()


def pick_hook(kind: str, rng: random.Random, subject: str | None, secs: int) -> str:
    options = [t for t in (_fill(x, subject, secs) for x in HOOKS[kind]) if t]
    return rng.choice(options)


def pick_end_card(rng: random.Random) -> str:
    return rng.choice(END_CARDS)


def make_caption(rng: random.Random, subject: str | None) -> str:
    cap = _fill(rng.choice(CAPTIONS), subject, 0)
    tags = list(BASE_TAGS)
    if subject:
        slug = "".join(ch for ch in subject.lower() if ch.isalnum())
        tags = [f"#{slug}", f"#{slug}fanart"] + tags
    rng.shuffle(tags)
    return f"{cap}\n\n{' '.join(tags[:10])}"
