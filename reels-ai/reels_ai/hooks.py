"""Hook text, end cards and captions. Rotated per variant; later stages learn which perform best.

`{subject}` is the character / piece name (optional), `{secs}` the clip length.
Templates that need a subject are skipped when none is given.
"""
from __future__ import annotations

import random
import re

PENCIL = re.compile(r"sketch|pencil", re.I)    # hooks/captions that only fit sessions with pencils
NO_PENCIL_CLAIM = re.compile(r"\bno (pencil|sketch)|without (a )?(pencil|sketch)|pencil[- ]free|straight to ink"
                             r"|no under ?drawing", re.I)
FALSE_ALWAYS = re.compile(r"real[- ]?time|no speed|not sped|unedited|uncut", re.I)   # videos are sped up


def honest(text: str, pencil: bool) -> bool:
    """False for copy that would misstate how this drawing was made."""
    if FALSE_ALWAYS.search(text):
        return False
    if pencil and NO_PENCIL_CLAIM.search(text):
        return False
    if not pencil and PENCIL.search(text) and not NO_PENCIL_CLAIM.search(text):
        return False
    return True

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
    "rewind": [
        "Watch it un-draw, then draw",
        "From this, back to a blank page",
        "{subject}, start to finish",
        "How this page was made",
    ],
    "chapters": [
        "{subject}, start to finish",
        "The whole process",
        "Every stage of this page",
        "Blank page to finished ink",
    ],
    "slow_open": [
        "It starts with a stick figure",
        "From a few lines to {subject}",
        "Watch what this turns into",
        "Wait for what it becomes",
    ],
    "last_lines": [
        "Wait for the last line",
        "Watch till the last line",
        "The last strokes are the best",
        "Inking {subject}",
    ],
    "one_take": [
        "Inking {subject}",
        "{subject}, start to finish",
        "Wait for the last line",
        "Watch till the last line",
    ],
    "process_cuts": [
        "Watch every line go down",
        "How I ink {subject}",
        "Inking {subject} by hand",
        "Watch the pen work",
        "Every line by hand",
    ],
}

NO_PENCIL_HOOKS = [            # only for straight-to-ink sessions
    "Straight to ink",
    "No sketch. Just ink.",
    "Inking {subject} with no sketch",
]

END_CARDS = [
    "Who should I ink next?",
    "Comment a character for next time",
    "Part 2?",
    "Follow for the next piece",
]

CAPTIONS = [
    "Inking {subject_or_this} by hand ✍\U0001f3fe Who should I draw next? \U0001f447\U0001f3fe",
    "Pencil to ink, start to finish. Which stage do you like more?",
    "Brush pen on paper, no tablet. Drop a character for the next one \U0001f447\U0001f3fe",
    "The ink stage never gets old. Save this for inking reference.",
]

BASE_TAGS = ["#mangaart", "#inking", "#animeart", "#timelapse", "#drawing", "#mangadrawing",
             "#ink", "#traditionalart", "#fanart", "#artprocess", "#mangaka", "#drawingprocess"]


def _fill(tpl: str, subject: str | None, secs: int) -> str | None:
    if "{subject}" in tpl and not subject:
        return None
    return tpl.format(subject=subject or "", secs=secs,
                      subject_or_this=subject or "this piece").strip()


def pick_hook(kind: str, rng: random.Random, subject: str | None, secs: int, pencil: bool = True) -> str:
    """pencil=False (straight-to-ink session): never mention a sketch or pencils."""
    options = [t for t in (_fill(x, subject, secs) for x in HOOKS[kind]) if t]
    if not pencil:
        options = [t for t in options if not PENCIL.search(t)]
        options += [t for t in (_fill(x, subject, secs) for x in NO_PENCIL_HOOKS) if t]
    return rng.choice(options)


def pick_end_card(rng: random.Random) -> str:
    return rng.choice(END_CARDS)


def make_caption(rng: random.Random, subject: str | None, pencil: bool = True) -> str:
    caps = [c for c in CAPTIONS if pencil or not PENCIL.search(c)]
    cap = _fill(rng.choice(caps), subject, 0)
    tags = list(BASE_TAGS)
    if subject:
        slug = "".join(ch for ch in subject.lower() if ch.isalnum())
        tags = [f"#{slug}", f"#{slug}fanart"] + tags
    rng.shuffle(tags)
    return f"{cap}\n\n{' '.join(tags[:10])}"
