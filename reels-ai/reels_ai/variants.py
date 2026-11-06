"""Variant recipes. Each recipe turns the analysed beats into a frame-by-frame edit plan."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import fx, hooks
from .analyze import Analysis
from .media import FPS, Encoder, FrameStore


@dataclass
class Shot:
    idx: int
    cam: tuple[float, float, float] = (0.5, 0.5, 1.0)
    alt: int | None = None          # second frame for wipes
    wipe: float | None = None
    flash: float = 0.0
    bar: float | None = None


@dataclass
class TextCard:
    text: str
    style: str
    start: float
    end: float
    y: float = 0.11


@dataclass
class Plan:
    name: str
    description: str
    grade: str
    shots: list[Shot] = field(default_factory=list)
    texts: list[TextCard] = field(default_factory=list)
    hook: str = ""
    caption: str = ""

    @property
    def seconds(self) -> float:
        return len(self.shots) / FPS


# ---------------------------------------------------------------- timing helpers
def ramp(an: Analysis, a: int, b: int, dur: float, focus: int | None = None,
         slow: float = 4.0) -> list[int]:
    """Source frame indices covering [a, b] in `dur` seconds.

    Never slower than real time (no duplicated frames). With `focus`, time stretches around
    that moment (up to 1x) and the rest speeds up to compensate - a speed ramp.
    Frames where an arm blocks the page are skipped.
    """
    b = max(b, a + 1)
    src = np.array(an.clean_range(a, min(b, an.n_frames - 1)))
    n_out = int(min(round(dur * FPS), len(src)))
    w = np.ones(len(src))
    if focus is not None and a <= focus <= b:
        sigma = max(FPS * 0.6, len(src) * 0.05)
        w += (slow - 1) * np.exp(-((src - focus) / sigma) ** 2)
    dens = w / w.sum() * n_out
    for _ in range(20):                          # cap at 1 output frame per source frame
        over = dens > 1
        if not over.any():
            break
        spare = (dens[over] - 1).sum()
        dens[over] = 1
        free = ~over & (dens < 1)
        if not free.any():
            break
        dens[free] += spare * dens[free] / dens[free].sum()
    cum = np.cumsum(dens)
    picks = np.searchsorted(cum, np.arange(n_out) + 0.5)
    return [int(src[min(p, len(src) - 1)]) for p in picks]


def hold(idx: int, dur: float) -> list[int]:
    return [idx] * int(round(dur * FPS))


def push(n: int, z0: float, z1: float, cx=0.5, cy=0.5) -> list[tuple[float, float, float]]:
    return [(cx, cy, z0 + (z1 - z0) * fx.ease(i / max(1, n - 1))) for i in range(n)]


def shots(idxs: list[int], cams=None, **kw) -> list[Shot]:
    cams = cams or [(0.5, 0.5, 1.0)] * len(idxs)
    return [Shot(i, c, **kw) for i, c in zip(idxs, cams)]


def secs(n_shots: int) -> float:
    return n_shots / FPS


# ---------------------------------------------------------------- recipes
def reveal_first(a: Analysis, rng, subject) -> Plan:
    p = Plan("reveal_first", "Opens on the finished piece, flashes back to the blank page, "
             "speed-ramps through the process, slows on the biggest ink moment.", "clean")
    open_ = hold(a.reveal_idx, 1.3)
    p.shots += shots(open_, push(len(open_), 1.12, 1.0))
    p.shots += [Shot(a.draw_start, flash=1 - k / 6) for k in range(6)]
    p.shots += shots(ramp(a, a.draw_start, a.ink_start, 5.0))
    p.shots += shots(ramp(a, a.ink_start, a.ink_end, 8.5, focus=a.peak_idx))
    end = hold(a.reveal_idx, 2.2)
    t_end = p.seconds
    p.shots += shots(end, push(len(end), 1.0, 1.05))
    p.hook = hooks.pick_hook("reveal_first", rng, subject, round(p.seconds))
    p.texts = [TextCard(p.hook, "outline", 0, 2.6),
               TextCard(hooks.pick_end_card(rng), "box", t_end + 0.2, p.seconds)]
    return p


def speedrun(a: Analysis, rng, subject) -> Plan:
    p = Plan("speedrun", "Ultra-short loopable cut with a progress bar; built for replays.", "natural")
    body = ramp(a, a.draw_start, a.ink_start, 3.0) + ramp(a, a.ink_start, a.ink_end, 4.5, focus=a.peak_idx, slow=2.5)
    body += hold(a.reveal_idx, 1.2)
    total = len(body)
    p.shots = [Shot(i, bar=k / (total - 1)) for k, i in enumerate(body)]
    p.hook = hooks.pick_hook("speedrun", rng, subject, round(p.seconds))
    p.texts = [TextCard(p.hook, "tag", 0, p.seconds, y=0.075)]
    return p


def before_after(a: Analysis, rng, subject) -> Plan:
    p = Plan("before_after", "Opens with a sketch-to-ink wipe comparison, then shows the inking.", "warm")
    seq: list[Shot] = []
    for k in range(int(0.4 * FPS)):
        seq.append(Shot(a.sketch_idx, alt=a.reveal_idx, wipe=0.0))
    n = int(1.4 * FPS)
    seq += [Shot(a.sketch_idx, alt=a.reveal_idx, wipe=fx.ease(k / (n - 1))) for k in range(n)]
    seq += [Shot(a.sketch_idx, alt=a.reveal_idx, wipe=1.0) for _ in range(int(0.5 * FPS))]
    n = int(1.0 * FPS)
    seq += [Shot(a.sketch_idx, alt=a.reveal_idx, wipe=1 - fx.ease(k / (n - 1))) for k in range(n)]
    p.shots = seq
    t1 = p.seconds
    p.shots += shots(ramp(a, a.ink_start, a.ink_end, 9.0, focus=a.peak_idx))
    end = hold(a.reveal_idx, 1.6)
    p.shots += shots(end, push(len(end), 1.0, 1.04))
    p.hook = hooks.pick_hook("before_after", rng, subject, round(p.seconds))
    p.texts = [TextCard(p.hook, "box", 0, t1),
               TextCard("Now watch the ink", "box", t1, t1 + 1.8)]
    return p


def detail_zoom(a: Analysis, rng, subject) -> Plan:
    p = Plan("detail_zoom", "Punches in on the most detailed area (usually the face) while it is "
             "being inked, then pulls back for the full reveal.", "clean")
    x, y, w, h = a.detail_box
    cx, cy = x + w / 2, y + h / 2
    p.shots += shots(ramp(a, a.draw_start, a.ink_start, 4.0))
    ink = ramp(a, a.ink_start, a.ink_end, 12.0, focus=a.peak_idx, slow=2.0)
    max_zoom_frames = int(len(ink) * 0.45)
    zoomed = 0
    z = 1.0
    for i in ink:
        want = 1.8 if (a.detail_start <= i <= a.detail_end and zoomed < max_zoom_frames) else 1.0
        zoomed += want > 1
        z += (want - z) * 0.09                      # smooth camera
        p.shots.append(Shot(i, (cx, cy, z)))
    end = hold(a.reveal_idx, 2.2)
    p.shots += [Shot(i, c) for i, c in zip(end, push(len(end), max(z, 1.25), 1.0, cx, cy))]
    p.hook = hooks.pick_hook("detail_zoom", rng, subject, round(p.seconds))
    p.texts = [TextCard(p.hook, "outline", 0, 2.8)]
    return p


def ink_only(a: Analysis, rng, subject) -> Plan:
    p = Plan("ink_only", "Skips the pencils entirely: straight into the ink, high-contrast B&W look.", "mono")
    p.shots += shots(ramp(a, a.ink_start, a.ink_end, 9.5, focus=a.peak_idx, slow=3.0))
    end = hold(a.reveal_idx, 1.6)
    p.shots += shots(end, push(len(end), 1.0, 1.05))
    p.hook = hooks.pick_hook("ink_only", rng, subject, round(p.seconds))
    p.texts = [TextCard(p.hook, "ink", 0, 2.6)]
    return p


RECIPES: dict[str, Callable] = {
    "reveal_first": reveal_first,
    "speedrun": speedrun,
    "before_after": before_after,
    "detail_zoom": detail_zoom,
    "ink_only": ink_only,
}


def build(a: Analysis, names: list[str], seed: int, subject: str | None) -> list[Plan]:
    plans = []
    for name in names:
        rng = random.Random(f"{seed}-{name}")
        plan = RECIPES[name](a, rng, subject)
        plan.caption = hooks.make_caption(rng, subject)
        plans.append(plan)
    return plans


# ---------------------------------------------------------------- render
def render(plan: Plan, store: FrameStore, out: Path, audio: Path | None = None) -> Path:
    w, h = store.width, store.height
    enc = Encoder(out, w, h, audio)
    graded: dict[int, np.ndarray] = {}

    def frame(i: int) -> np.ndarray:
        if i not in graded:
            if len(graded) > 8:
                graded.clear()
            graded[i] = fx.grade(store[i], plan.grade)
        return graded[i]

    fade = 0.15
    try:
        for k, s in enumerate(plan.shots):
            t = k / FPS
            img = frame(s.idx)
            if s.wipe is not None and s.alt is not None:
                img = fx.wipe(img, frame(s.alt), s.wipe)
            img = fx.camera(img, *s.cam)
            for card in plan.texts:
                if card.start <= t < card.end:
                    alpha = min(1.0, (t - card.start) / fade, (card.end - t) / fade)
                    img = fx.overlay(img, fx.text_layer(card.text, card.style, w, h, card.y), alpha)
            if s.bar is not None:
                img = fx.progress_bar(img, s.bar)
            img = fx.flash(img, s.flash)
            enc.write(img)
    finally:
        enc.close()
    return out
