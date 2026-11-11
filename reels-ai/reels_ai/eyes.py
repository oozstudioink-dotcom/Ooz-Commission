"""Eyes: Claude looks at a storyboard of the recording and says what is on screen when.

The pixel measurements (page changes, ink growth, page recognition) are precise but don't
understand what they see: an old finished page being shown, two views of one drawing, a hand
resting on the paper. Claude is given frames across the recording, with their times and the
transcript, and answers the questions that need understanding: which pages appear, which one is
being drawn on, how many separate drawings were made, and when.

One request per recording. Needs ANTHROPIC_API_KEY; without it the editor uses measurements only.
"""
from __future__ import annotations

import base64
import json
import os

import cv2
import numpy as np

MODEL = "claude-opus-5-5"
MAX_FRAMES = 72
MIN_FRAMES = 12
SECONDS_PER_FRAME = 30          # about one frame for every 30 s of recording
LONG_SIDE = 768

SYSTEM = """You help edit an artist's recordings into short videos. The artist draws manga-style \
ink art on paper (sometimes digital art) and films the page from above; many recordings are \
livestreams where they also flip through a sketchbook to show finished pages, move the paper, \
change the camera distance, or talk about other work.

You are given still frames from one recording, in time order, each with its time, and a \
transcript of what the artist said when there is one. An automatic editor will cut the recording \
using your answer, so accuracy matters more than detail: a wrong page or a drawing split in two \
ruins the video, and the artist will not be there to catch it.

What the editor needs from you:

- Pages. Every distinct sheet that appears. A page being drawn on gains lines between frames; a \
finished page that is only shown looks the same every time it appears. The same sheet seen closer, \
further away, rotated or shifted is still the same page.
- Each frame's page and what is happening in it: "drawing" (pen or pencil on the page, or the page \
clearly being worked on), "showing" (holding up or displaying a page without working on it), \
"flipping" (mid page-turn, blurred, or between pages), or "other" (no page in view, hands only, \
setup).
- Drawings. Each separate piece the artist made during this recording: one figure, bust, panel or \
study that stands on its own on the page. Count carefully. Work that continues after a pause, after \
showing another page, or from a different camera distance is the same drawing. Several figures \
drawn as one composition are one drawing; figures made one after another in different parts of the \
page, each finished before the next, are separate drawings. Pages that were only shown are not \
drawings. Give the first and last frame in which each drawing is being worked on, and where it \
sits in the frame (fractions of the frame width and height) in a frame where the whole page is \
visible.
- Names. Give a drawing a title only from what the artist said or the file name. Do not identify \
characters or series from how the art looks; if the artist never named it, describe it plainly \
("head close-up, top left")."""

SCHEMA = {
    "type": "object",
    "properties": {
        "pages": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "description": {"type": "string"},
                           "drawn_on": {"type": "boolean"}},
            "required": ["id", "description", "drawn_on"], "additionalProperties": False}},
        "frames": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "page": {"type": "string"},
                           "activity": {"type": "string", "enum": ["drawing", "showing", "flipping", "other"]}},
            "required": ["index", "page", "activity"], "additionalProperties": False}},
        "drawings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "page": {"type": "string"}, "title": {"type": "string"},
                "named_by_artist": {"type": "boolean"},
                "first_frame": {"type": "integer"}, "last_frame": {"type": "integer"},
                "box": {"type": "object",
                        "properties": {"x0": {"type": "number"}, "y0": {"type": "number"},
                                       "x1": {"type": "number"}, "y1": {"type": "number"}},
                        "required": ["x0", "y0", "x1", "y1"], "additionalProperties": False},
                "what": {"type": "string"}},
            "required": ["page", "title", "named_by_artist", "first_frame", "last_frame", "box", "what"],
            "additionalProperties": False}},
        "notes": {"type": "string"},
    },
    "required": ["pages", "frames", "drawings", "notes"],
    "additionalProperties": False,
}


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def storyboard(store, dt: float) -> list[tuple[float, bytes]]:
    """Frames across the whole recording: [(recording seconds, JPEG)]. Around each sample point
    the frame with the least hand over the page is taken, so more of the drawing shows."""
    from .analyze import _skin
    n = len(store)
    k = int(min(MAX_FRAMES, max(MIN_FRAMES, round(n * dt / SECONDS_PER_FRAME))))
    k = min(k, n)
    out = []
    for c in np.linspace(0, n - 1, k):
        near = sorted({int(min(n - 1, max(0, round(c + d)))) for d in (-4, 0, 4)})
        best = min(near, key=lambda i: float(_skin(cv2.resize(store[i], (96, 170))).mean()))
        img = store[best]
        h, w = img.shape[:2]
        s = LONG_SIDE / max(h, w)
        if s < 1:
            img = cv2.resize(img, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if ok:
            out.append((best * dt, buf.tobytes()))
    return out


def _stamp(t: float) -> str:
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def look(frames: list[tuple[float, bytes]], transcript_text: str, file_name: str,
         client=None, model: str = MODEL, effort: str = "high") -> dict:
    """Ask Claude what the storyboard shows. Returns the parsed answer (see SCHEMA); raises on
    an API failure or a refusal, and the caller falls back to measurements."""
    import anthropic
    client = client or anthropic.Anthropic()
    content: list[dict] = [{"type": "text", "text":
                            f"Recording file name: {file_name}\n{len(frames)} frames follow, in time order."}]
    for i, (t, jpg) in enumerate(frames):
        content.append({"type": "text", "text": f"Frame {i} at {_stamp(t)}"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.standard_b64encode(jpg).decode("utf-8")}})
    content.append({"type": "text", "text":
                    ("Transcript of what the artist said ([minute:second] at the start of each line):\n"
                     + transcript_text) if transcript_text else
                    "There is no transcript: the recording is silent or nothing was said."})
    content.append({"type": "text", "text":
                    "Describe this recording for the editor. Label every frame by its index."})
    with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                       # a declined request is retried on a fallback model
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude declined to describe this recording")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off")
    text = next(b.text for b in msg.content if b.type == "text")
    out = json.loads(text)
    out["model"] = msg.model
    out["tokens"] = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
    return out
