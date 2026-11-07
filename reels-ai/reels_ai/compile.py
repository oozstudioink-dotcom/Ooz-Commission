"""Page compilations: stitch several drawings from one page into one video.

short: ~55 s vertical Short. Quick flash of every finished drawing, then each drawing's
       sketch-to-ink (speed-ramped, framed on that drawing), ending with a pull-back to the
       whole page.
long:  every drawing's full timelapse at its own speed with a title card, for YouTube.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import fx
from .analyze import Analysis, analyze
from .media import FPS, Encoder, FrameStore, normalize
from .variants import ramp

W, H = 720, 1280


@dataclass
class Clip:
    store: FrameStore
    a: Analysis
    subject: str | None


def load_clips(sources: list[tuple[Path, str | None]], work: Path) -> list[Clip]:
    clips = []
    for i, (src, subject) in enumerate(sources):
        wk = work / f"clip{i}.mp4"
        normalize(Path(src), wk)
        store = FrameStore(wk)
        clips.append(Clip(store, analyze(store), subject))
        wk.unlink(missing_ok=True)
    return clips


def _frame(clip: Clip, idx: int, cam: tuple[float, float, float] | None = None,
           grade: str = "clean") -> np.ndarray:
    img = fx.grade(clip.store[idx], grade)
    img = fx.camera(img, *(cam or clip.a.focus))
    if img.shape[1] != W:
        img = cv2.resize(img, (W, H), interpolation=cv2.INTER_CUBIC)
    return img


def _text(img: np.ndarray, text: str, style: str, alpha: float = 1.0, y: float = 0.11) -> np.ndarray:
    return fx.overlay(img, fx.text_layer(text, style, W, H, y), alpha) if text else img


def _fade_alpha(k: int, n: int, fade: int = 5) -> float:
    return max(0.0, min(1.0, k / fade, (n - 1 - k) / fade))


def render_short(clips: list[Clip], out: Path, title: str, audio: Path | None = None,
                 target: float = 55.0) -> float:
    n = len(clips)
    frames: list[tuple[int, int, tuple | None, str, str]] = []   # (clip, idx, cam, text, style)
    # 1. flash of every finished drawing
    for ci, c in enumerate(clips):
        frames += [(ci, c.a.reveal_idx, None, title, "outline")] * int(0.35 * FPS)
    # 2. each drawing: sketch -> ink, speed ramp, framed on the drawing
    per = max(4.0, (target - len(frames) / FPS - 3.5) / n)
    for ci, c in enumerate(clips):
        a = c.a
        label = f"{ci + 1}/{n}" + (f" · {c.subject}" if c.subject else "")
        idxs = ramp(a, a.draw_start, a.ink_start, per * 0.3) + \
            ramp(a, a.ink_start, a.ink_end, per * 0.6, focus=a.peak_idx, slow=2.5)
        frames += [(ci, i, None, label, "tag") for i in idxs]
        frames += [(ci, a.reveal_idx, None, label, "tag")] * int(per * 0.1 * FPS)
    # 3. pull back to the whole page (last clip's final frame, un-cropped)
    last = clips[-1]
    cx, cy, z = last.a.focus
    m = int(1.5 * FPS)
    for k in range(m):
        e = fx.ease(k / (m - 1))
        cam = (cx + (0.5 - cx) * e, cy + (0.5 - cy) * e, z + (1.0 - z) * e)
        frames.append((n - 1, last.a.reveal_idx, cam, "", ""))
    frames += [(n - 1, last.a.reveal_idx, (0.5, 0.5, 1.0), f"The full page · {n} drawings", "box")] * int(2.0 * FPS)
    return _encode(clips, frames, out, audio, crf=19)


def render_long(clips: list[Clip], out: Path, title: str, audio: Path | None = None) -> float:
    n = len(clips)
    frames = []
    frames += [(n - 1, clips[-1].a.reveal_idx, (0.5, 0.5, 1.0), title, "box")] * int(2.5 * FPS)
    for ci, c in enumerate(clips):
        label = f"Drawing {ci + 1} of {n}" + (f": {c.subject}" if c.subject else "")
        frames += [(ci, c.a.reveal_idx, None, label, "box")] * int(1.5 * FPS)
        frames += [(ci, i, None, "", "") for i in range(c.a.draw_start, len(c.store)) if c.a.usable[i]]
        frames += [(ci, c.a.reveal_idx, None, "", "")] * int(1.5 * FPS)
    frames += [(n - 1, clips[-1].a.reveal_idx, (0.5, 0.5, 1.0), "Which one is your favorite?", "box")] * int(3 * FPS)
    return _encode(clips, frames, out, audio, crf=23)


def _encode(clips: list[Clip], frames: list, out: Path, audio: Path | None, crf: int) -> float:
    secs = len(frames) / FPS
    enc = Encoder(out, W, H, audio, duration=secs, crf=crf)
    try:
        # text fades in/out per run of identical text
        runs, start = [], 0
        for k in range(1, len(frames) + 1):
            if k == len(frames) or frames[k][3:] != frames[start][3:]:
                runs.append((start, k))
                start = k
        cache: dict = {}
        for s, e in runs:
            for k in range(s, e):
                ci, idx, cam, text, style = frames[k]
                key = (ci, idx, cam)
                if key not in cache:
                    if len(cache) > 6:
                        cache.clear()
                    cache[key] = _frame(clips[ci], idx, cam)
                img = _text(cache[key], text, style, _fade_alpha(k - s, e - s)) if text else cache[key]
                enc.write(img)
    finally:
        enc.close()
    return secs


def make_compilation(sources: list[tuple[Path, str | None]], out_dir: Path, title: str,
                     audio: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        clips = load_clips(sources, Path(tmp))
        short = out_dir / "page_short.mp4"
        long_ = out_dir / "page_long.mp4"
        s_secs = render_short(clips, short, title, audio)
        l_secs = render_long(clips, long_, title, audio)
    return {"short": short, "short_seconds": s_secs, "long": long_, "long_seconds": l_secs}
