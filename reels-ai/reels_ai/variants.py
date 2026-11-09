"""Variant recipes. Each recipe turns the analysed beats into a frame-by-frame edit plan.

All variants target 8-15 s: channel data shows viewers watch ~7-10 s on average, so the
finished piece (or a strong visual) must land in the first second and the whole arc must fit.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from . import fx, hooks
from .analyze import Analysis
from .media import FPS, Encoder, FrameStore


@dataclass
class Shot:
    idx: float                      # source frame (fractional = cross-fade of neighbours)
    cam: tuple[float, float, float] = (0.5, 0.5, 1.0)
    alt: int | None = None          # second frame for wipes
    wipe: float | None = None
    flash: float = 0.0
    bar: float | None = None
    src: str | None = None          # None = the timelapse; else a real-speed clip (Plan.clips)


@dataclass
class TextCard:
    text: str
    style: str
    start: float
    end: float
    y: float = fx.SAFE_TOP


@dataclass
class Plan:
    name: str
    description: str
    grade: str
    shots: list[Shot] = field(default_factory=list)
    texts: list[TextCard] = field(default_factory=list)
    hook: str = ""
    caption: str = ""
    clips: dict = field(default_factory=dict)      # real-speed clips: key -> FrameStore

    @property
    def seconds(self) -> float:
        return len(self.shots) / FPS


# ---------------------------------------------------------------- timing helpers
MAX_SLOW = 2.0   # at most this many output frames per source frame (in-betweens are cross-fades)


def ramp(an: Analysis, a: int, b: int, dur: float, focus: int | None = None,
         slow: float = 4.0) -> list[float]:
    """Source positions (fractional frame numbers) covering [a, b] in `dur` seconds.

    With `focus`, time stretches around that moment and the rest speeds up to compensate - a
    speed ramp. Positions are fractional, so when the clip has fewer frames than the edit needs
    the renderer cross-fades between neighbours instead of showing a frame twice (no stop-motion
    stutter). Frames where an arm blocks the page are stepped over.
    """
    b = max(b, a + 1)
    src = np.array(an.clean_range(a, min(b, an.n_frames - 1)), dtype=np.float64)
    m = len(src)
    n_out = max(1, int(round(dur * FPS)))
    if m < 2:
        return [float(src[0])] * n_out
    n_out = min(n_out, int(MAX_SLOW * (m - 1)) + 1)
    w = np.ones(m)
    if focus is not None and a <= focus <= b:
        sigma = max(FPS * 0.6, m * 0.05)
        w += (slow - 1) * np.exp(-((src - focus) / sigma) ** 2)
    seg = (w[:-1] + w[1:]) / 2                       # output frames spent on each source step
    dens = seg / seg.sum() * (n_out - 1)
    for _ in range(100):                             # cap the slow-down, hand the rest out evenly
        over = dens > MAX_SLOW + 1e-9
        if not over.any():
            break
        spare = (dens[over] - MAX_SLOW).sum()
        dens[over] = MAX_SLOW
        free = dens < MAX_SLOW - 1e-9
        if not free.any():
            break
        dens[free] += spare * dens[free] / dens[free].sum()
    cum = np.concatenate([[0.0], np.cumsum(dens)])
    u = np.interp(np.arange(n_out) * cum[-1] / max(1, n_out - 1), cum, np.arange(m))
    return [float(x) for x in np.interp(u, np.arange(m), src)]


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
    open_ = hold(a.reveal_idx, 1.0)
    p.shots += shots(open_, push(len(open_), 1.12, 1.0))
    p.shots += [Shot(a.draw_start, flash=1 - k / 6) for k in range(6)]
    pre = 2.5 if a.has_pencil else 0.0
    if pre:
        p.shots += shots(ramp(a, a.draw_start, a.ink_start, pre))
    p.shots += shots(ramp(a, a.ink_start, a.ink_end, 9.0 - pre, focus=a.peak_idx))
    end = hold(a.reveal_idx, 1.8)
    t_end = p.seconds
    p.shots += shots(end, push(len(end), 1.0, 1.05))
    p.hook = hooks.pick_hook("reveal_first", rng, subject, round(p.seconds), a.has_pencil)
    p.texts = [TextCard(p.hook, "outline", 0, 2.4),
               TextCard(hooks.pick_end_card(rng), "box", t_end + 0.1, p.seconds)]
    return p


def speedrun(a: Analysis, rng, subject) -> Plan:
    p = Plan("speedrun", "Ultra-short loopable cut with a progress bar; built for replays.", "natural")
    pre = 3.0 if a.has_pencil else 0.0
    body = (ramp(a, a.draw_start, a.ink_start, pre) if pre else []) + \
        ramp(a, a.ink_start, a.ink_end, 7.5 - pre, focus=a.peak_idx, slow=2.5)
    body += hold(a.reveal_idx, 1.2)
    total = len(body)
    p.shots = [Shot(i, bar=k / (total - 1)) for k, i in enumerate(body)]
    p.hook = hooks.pick_hook("speedrun", rng, subject, round(p.seconds), a.has_pencil)
    p.texts = [TextCard(p.hook, "tag", 0, p.seconds, y=fx.SAFE_TOP - 0.01)]
    return p


def before_after(a: Analysis, rng, subject) -> Plan:
    p = Plan("before_after", "Opens with a sketch-to-ink wipe comparison, then shows the inking.", "warm")
    seq: list[Shot] = []
    for k in range(int(0.4 * FPS)):
        seq.append(Shot(a.sketch_idx, alt=a.reveal_idx, wipe=0.0))
    n = int(1.2 * FPS)
    seq += [Shot(a.sketch_idx, alt=a.reveal_idx, wipe=fx.ease(k / (n - 1))) for k in range(n)]
    seq += [Shot(a.sketch_idx, alt=a.reveal_idx, wipe=1.0) for _ in range(int(0.4 * FPS))]
    n = int(0.8 * FPS)
    seq += [Shot(a.sketch_idx, alt=a.reveal_idx, wipe=1 - fx.ease(k / (n - 1))) for k in range(n)]
    p.shots = seq
    t1 = p.seconds
    p.shots += shots(ramp(a, a.ink_start, a.ink_end, 6.5, focus=a.peak_idx))
    end = hold(a.reveal_idx, 1.3)
    p.shots += shots(end, push(len(end), 1.0, 1.04))
    p.hook = hooks.pick_hook("before_after", rng, subject, round(p.seconds), a.has_pencil)
    p.texts = [TextCard(p.hook, "box", 0, t1),
               TextCard("Now watch the ink", "box", t1, t1 + 1.5)]
    return p


def detail_zoom(a: Analysis, rng, subject) -> Plan:
    p = Plan("detail_zoom", "Punches in on the most detailed area (usually the face) while it is "
             "being inked, then pulls back for the full reveal.", "clean")
    x, y, w, h = a.detail_box
    cx, cy = fx.to_view(x + w / 2, y + h / 2, a.focus)
    dz = max(1.2, min(1.8, 2.0 / a.focus[2]))     # total zoom (focus x detail) stays <= ~2x
    pre = 2.0 if a.has_pencil else 0.0
    if pre:
        p.shots += shots(ramp(a, a.draw_start, a.ink_start, pre))
    ink = ramp(a, a.ink_start, a.ink_end, 10.5 - pre, focus=a.peak_idx, slow=2.0)
    max_zoom_frames = int(len(ink) * 0.45)
    zoomed = 0
    z = 1.0
    for i in ink:
        want = dz if (a.detail_start <= i <= a.detail_end and zoomed < max_zoom_frames) else 1.0
        zoomed += want > 1
        z += (want - z) * 0.12                      # smooth camera
        p.shots.append(Shot(i, (cx, cy, z)))
    end = hold(a.reveal_idx, 1.6)
    p.shots += [Shot(i, c) for i, c in zip(end, push(len(end), max(z, 1.25), 1.0, cx, cy))]
    p.hook = hooks.pick_hook("detail_zoom", rng, subject, round(p.seconds), a.has_pencil)
    p.texts = [TextCard(p.hook, "outline", 0, 2.4)]
    return p


def ink_only(a: Analysis, rng, subject) -> Plan:
    p = Plan("ink_only", "Skips the pencils entirely: straight into the ink, high-contrast B&W look.", "mono")
    p.shots += shots(ramp(a, a.ink_start, a.ink_end, 8.0, focus=a.peak_idx, slow=3.0))
    end = hold(a.reveal_idx, 1.4)
    p.shots += shots(end, push(len(end), 1.0, 1.05))
    p.hook = hooks.pick_hook("ink_only", rng, subject, round(p.seconds), a.has_pencil)
    p.texts = [TextCard(p.hook, "ink", 0, 2.6)]
    return p


CUT_SECONDS = 1.1     # each real-speed moment on screen
CUTS = 8


def pick_moments(a: Analysis, n: int = CUTS) -> list[int]:
    """Timelapse frames where ink is going down, spread evenly through the inking.

    Each moment is the busiest stretch of its slice of the ink stage (most new ink), on a frame
    where the page isn't blocked by an arm."""
    win = max(1, a.window_frames)
    prog = np.asarray(a.ink_progress, dtype=float)
    gain = np.diff(prog, prepend=prog[:1]) if len(prog) else np.zeros(0)
    lo, hi = a.ink_start // win, max(a.ink_start // win + 1, a.ink_end // win)
    edges = np.linspace(lo, hi, n + 1)
    out: list[int] = []
    for k in range(n):
        w0, w1 = int(edges[k]), max(int(edges[k]) + 1, int(edges[k + 1]))
        seg = gain[w0:w1]
        w = w0 + (int(np.argmax(seg)) if seg.size else 0)
        f = int(min(a.n_frames - 1, w * win + win // 2))
        ok = [i for i in range(max(0, f - win), min(a.n_frames, f + win)) if a.usable[i]]
        f = min(ok, key=lambda i: abs(i - f)) if ok else f
        if not out or f > out[-1]:
            out.append(f)
    return out


def process_cuts(a: Analysis, rng, subject, clips: dict | None = None) -> Plan:
    p = Plan("process_cuts", "Real-speed jump cuts: short moments of the pen actually moving, hopping "
             "forward through the inking, finished piece at the start and end.", "clean")
    if not clips:
        raise ValueError("process_cuts needs real-speed clips from the original recording")
    p.clips = clips
    open_ = hold(a.reveal_idx, 0.6)
    p.shots += shots(open_, push(len(open_), 1.08, 1.0))
    for k, (key, store) in enumerate(clips.items()):
        n = len(store)
        z0, z1 = (1.0, 1.05) if k % 2 == 0 else (1.05, 1.0)      # a slow push, alternating
        p.shots += [Shot(i, c, src=key) for i, c in zip(range(n), push(n, z0, z1))]
    t_end = p.seconds
    end = hold(a.reveal_idx, 1.3)
    p.shots += shots(end, push(len(end), 1.0, 1.04))
    p.hook = hooks.pick_hook("process_cuts", rng, subject, round(p.seconds), a.has_pencil)
    p.texts = [TextCard(p.hook, "outline", 0, t_end),
               TextCard(hooks.pick_end_card(rng), "box", t_end + 0.1, p.seconds)]
    return p


RECIPES: dict[str, Callable] = {
    "reveal_first": reveal_first,
    "speedrun": speedrun,
    "before_after": before_after,
    "detail_zoom": detail_zoom,
    "ink_only": ink_only,
    "process_cuts": process_cuts,
}


def build(a: Analysis, names: list[str], seed: int, subject: str | None,
          copy: dict | None = None, clips: dict | None = None) -> list[Plan]:
    """`copy` (from ai.write_copy) replaces template hooks/captions where it has them.
    `clips`: real-speed clips from the original recording (needed by process_cuts)."""
    plans = []
    for name in names:
        rng = random.Random(f"{seed}-{name}")
        if name == "process_cuts":
            plan = process_cuts(a, rng, subject, clips)
        else:
            plan = RECIPES[name](a, rng, subject)
        plan.caption = hooks.make_caption(rng, subject, a.has_pencil)
        ai_hook = (copy or {}).get("hooks", {}).get(name)
        if ai_hook and not a.has_pencil and hooks.PENCIL.search(ai_hook):
            ai_hook = None                     # AI guessed a sketch stage this session doesn't have
        if ai_hook:
            for card in plan.texts:
                if card.text == plan.hook:
                    card.text = ai_hook
            plan.hook = ai_hook
        if copy and copy.get("caption") and (a.has_pencil or not hooks.PENCIL.search(copy["caption"])):
            tags = copy.get("hashtags") or hooks.make_caption(rng, subject).split("\n\n")[-1].split()
            plan.caption = f"{copy['caption']}\n\n{' '.join(tags)}"
        plans.append(plan)
    return plans


# ---------------------------------------------------------------- render
BLEND = 6        # max source frames blended into one output frame (motion blur)
MIN_SPAN = 1.5   # source frames of motion trail kept even at real speed or slower


def _snap(j: int, usable: list[bool] | None, step: int, limit: int) -> int | None:
    """Nearest frame at or beyond j (stepping by `step`) where no arm blocks the page."""
    for k in range(limit + 1):
        i = j + k * step
        if i < 0 or (usable is not None and i >= len(usable)):
            return None
        if usable is None or usable[i]:
            return i
    return None


def _at(x: float, usable: list[bool] | None, limit: int) -> dict[int, float]:
    """A fractional position as a cross-fade of the clean frames either side of it."""
    lo, f = int(np.floor(x)), x - np.floor(x)
    if f < 1e-3:
        lo_i = _snap(lo, usable, -1, limit) if usable is not None else lo
        return {lo if lo_i is None else lo_i: 1.0}
    a = _snap(lo, usable, -1, limit)
    b = _snap(lo + 1, usable, +1, limit)
    if a is None or b is None or a == b:
        return {lo if a is None and b is None else (a if b is None else b): 1.0}
    f = (x - a) / (b - a) if b > a else f
    return {a: 1 - f, b: f}


Window = tuple[tuple[int, float], ...]


def blend_windows(pos: list[float], usable: list[bool] | None = None, blend: int = BLEND,
                  min_span: float = MIN_SPAN) -> list[Window]:
    """For each output frame, the source frames (with weights) it is made of.

    - Speed-up: the frames it skips are blended in (motion blur, newest weighted most), so ink
      builds up smoothly and the hand glides instead of jumping.
    - Slower than the clip (sparse recordings): neighbouring frames cross-fade, so nothing is
      ever shown twice in a row (no stop-motion stutter).
    - Holds, flashbacks and long cuts stay single sharp frames; arm-sweep frames are left out.
    """
    out: list[Window] = []
    prev = None
    lim = 4 * blend
    for p in pos:
        step = None if prev is None else p - prev
        prev = p
        if step is None or step <= 1e-6 or step > lim or blend <= 1:
            parts = _at(p, usable, lim)
        else:
            span = min(max(step, min_span), blend)
            n = int(np.clip(np.ceil(span * 2), 2, 2 * blend))
            parts: dict[int, float] = {}
            for k in range(n):
                x = p - span * (n - 1 - k) / n          # oldest .. newest (x = p)
                for i, wt in _at(max(x, 0.0), usable, lim).items():
                    parts[i] = parts.get(i, 0.0) + wt * (k + 1)
        tot = sum(parts.values())
        out.append(tuple(sorted((i, round(wt / tot, 3)) for i, wt in parts.items() if wt / tot >= 0.02)))
    return out


def mix(store, win: Window) -> np.ndarray:
    """Weighted average of source frames (running uint8 blend: fast even at 4K)."""
    if len(win) == 1:
        return store[win[0][0]]
    out, total = None, 0.0
    for i, wt in win:
        total += wt
        f = store[i]
        out = f if out is None else cv2.addWeighted(out, 1 - wt / total, f, wt / total, 0)
    return out


def single(idx: float) -> Window:
    return ((int(round(idx)), 1.0),)


def render(plan: Plan, store: FrameStore, out: Path, audio: Path | None = None,
           focus: tuple[float, float, float] = (0.5, 0.5, 1.0), usable: list[bool] | None = None,
           blend: int = BLEND) -> Path:
    w, h = store.width, store.height
    enc = Encoder(out, w, h, audio, duration=plan.seconds)
    stores = {None: store, **plan.clips}
    windows: list[Window] = []
    k = 0
    while k < len(plan.shots):                     # blend within each run from one source
        e = k
        while e < len(plan.shots) and plan.shots[e].src == plan.shots[k].src:
            e += 1
        windows += blend_windows([s.idx for s in plan.shots[k:e]],
                                 usable if plan.shots[k].src is None else None, blend)
        k = e
    graded: dict[tuple, np.ndarray] = {}

    def frame(win: Window, src: str | None = None) -> np.ndarray:
        key = (src, win)
        if key not in graded:
            if len(graded) > 8:
                graded.clear()
            img = mix(stores[src], win)
            if img.shape[:2] != (h, w):
                img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            graded[key] = fx.grade(img, plan.grade)
        return graded[key]

    fade = 0.15
    try:
        for k, s in enumerate(plan.shots):
            t = k / FPS
            img = frame(windows[k] if s.wipe is None else single(s.idx), s.src)
            if s.wipe is not None and s.alt is not None:
                img = fx.wipe(img, frame(single(s.alt)), s.wipe)
            img = fx.camera(img, *focus, s.cam)     # frame this clip's drawing, then the shot's move
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
