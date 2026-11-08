"""Find the story beats of a drawing timelapse with plain computer vision (no paid AI).

Beats found:
  draw_start   first marks appear on the page
  sketch_idx   cleanest view of the finished pencil sketch (hand off the page, right before ink)
  ink_start    inking begins            ink_end    inking is basically done
  peak_idx     the moment the most ink goes down fastest (usually a solid black fill)
  reveal_idx   cleanest view of the finished piece
  detail_box   the most line-dense area (usually the face) plus when it gets inked
  focus        where this clip's drawing is on the page (cx, cy, zoom). One page often holds
               several finished drawings; variants are framed on the new one.

Key trick: anything that is ink in the finished frame stays ink. So for every ink pixel we
find the last moment it was still blank paper (ignoring moments the hand covered it). That
gives a per-pixel "inked at" time that the hand can't fool.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import cv2
import numpy as np

from .media import FPS, FrameStore

AW = 360          # analysis width
WINDOW_S = 0.5    # temporal median window


@dataclass
class Analysis:
    n_frames: int
    fps: int
    width: int
    height: int
    draw_start: int
    sketch_idx: int
    ink_start: int
    ink_end: int
    peak_idx: int
    reveal_idx: int
    detail_box: tuple[float, float, float, float]   # x, y, w, h as fractions of the frame
    focus: tuple[float, float, float]                # cx, cy, zoom of the framing crop
    detail_start: int
    detail_end: int
    hand_score: list[float]
    ink_progress: list[float]       # per window, 0..1
    window_frames: int
    usable: list[bool]              # False = hand/arm covering much more of the page than usual
    work_center: tuple[float, float] = (0.5, 0.5)    # centre of this clip's new pencil/ink

    def clean_range(self, a: int, b: int) -> list[int]:
        idx = [i for i in range(a, b + 1) if self.usable[i]]
        return idx or list(range(a, b + 1))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("hand_score")
        d.pop("usable")
        d["occluded_frames"] = int(len(self.usable) - sum(self.usable))
        d["seconds"] = {k: round(getattr(self, k) / self.fps, 2) for k in
                        ("draw_start", "sketch_idx", "ink_start", "ink_end", "peak_idx",
                         "reveal_idx", "detail_start", "detail_end")}
        return d


def _skin(small_bgr: np.ndarray) -> np.ndarray:
    ycc = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2YCrCb)
    y, cr, cb = ycc[..., 0], ycc[..., 1], ycc[..., 2]
    hsv = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2HSV)
    skin = (cr > 138) & (cr < 180) & (cb > 80) & (cb < 130) & (y > 30)
    pen = (hsv[..., 1] > 70) & (hsv[..., 2] < 230)       # coloured pen bodies, sleeves
    return skin | pen


def _darkness(gray: np.ndarray) -> np.ndarray:
    """How much darker each pixel is than the local paper (lighting-independent)."""
    g = gray.astype(np.float32)
    paper = cv2.dilate(g, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (31, 31)))
    paper = cv2.GaussianBlur(paper, (0, 0), 6)
    return paper - g


def _inked_at(window_dark: list[np.ndarray], window_hand: list[np.ndarray], target: np.ndarray) -> np.ndarray:
    """For each target pixel, index of the first window after which it is permanently dark."""
    n = len(window_dark)
    t = np.zeros(target.shape, np.int32)
    settled = ~target
    for w in range(n - 1, -1, -1):
        blank = ~window_dark[w] & ~window_hand[w] & ~settled
        t[blank] = w + 1
        settled |= blank
    t[~target] = -1
    return t


def _first_crossing(curve: np.ndarray, level: float) -> int:
    idx = np.nonzero(curve >= level)[0]
    return int(idx[0]) if len(idx) else len(curve) - 1


def analyze(store: FrameStore) -> Analysis:
    n = len(store)
    scale = AW / store.width
    ah = int(round(store.height * scale))
    win = max(1, int(round(WINDOW_S * FPS)))

    hand_score = np.zeros(n, np.float32)
    w_gray, w_hand = [], []
    buf_g, buf_h = [], []
    for i in range(n):
        small = cv2.resize(store[i], (AW, ah), interpolation=cv2.INTER_AREA)
        hand = _skin(small)
        hand_score[i] = hand.mean()
        buf_g.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        buf_h.append(hand)
        if len(buf_g) == win or i == n - 1:
            w_gray.append(np.median(np.stack(buf_g), axis=0).astype(np.uint8))
            hm = np.mean(np.stack(buf_h), axis=0) > 0.3
            w_hand.append(cv2.dilate(hm.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0)
            buf_g, buf_h = [], []
    nw = len(w_gray)

    # --- reveal: cleanest frame near the end -------------------------------------------
    tail = max(1, int(n * 0.2))
    tail_scores = hand_score[n - tail:]
    ok = np.nonzero(tail_scores <= tail_scores.min() + 0.005)[0]
    reveal_idx = int(n - tail + ok[-1])

    reveal = cv2.cvtColor(cv2.resize(store[reveal_idx], (AW, ah), interpolation=cv2.INTER_AREA),
                          cv2.COLOR_BGR2GRAY)
    d_reveal = _darkness(reveal)
    ink_thr = max(60.0, float(np.percentile(d_reveal[d_reveal > 20], 75))) if (d_reveal > 20).any() else 60.0
    ink_map = d_reveal > ink_thr
    mark_map = d_reveal > 18          # anything drawn (pencil or ink)

    dark_ink = [_darkness(g) > ink_thr * 0.6 for g in w_gray]
    dark_any = [_darkness(g) > 12 for g in w_gray]

    ink_t = _inked_at(dark_ink, w_hand, ink_map)
    mark_t = _inked_at(dark_any, w_hand, mark_map & ~ink_map)

    def progress(times: np.ndarray, mask: np.ndarray) -> np.ndarray:
        vals = times[mask]
        if vals.size == 0:
            return np.linspace(0, 1, nw)
        return np.array([(vals <= w).mean() for w in range(nw)])

    # Pixels never seen as blank paper (t == 0) were already drawn when the clip starts, or
    # hidden under the hand until then. We don't know when they were inked, so they say nothing
    # about this clip's timing: leave them out (unless that leaves almost nothing).
    def observed(t: np.ndarray, mask: np.ndarray) -> np.ndarray:
        seen = mask & (t > 0)
        return seen if seen.sum() > 0.15 * max(1, mask.sum()) else mask

    ink_obs = observed(ink_t, ink_map)
    ink_prog = progress(ink_t, ink_obs)
    mark_prog = progress(mark_t, observed(mark_t, mark_map & ~ink_map))

    def to_frame(w: int) -> int:
        return int(min(n - 1, max(0, w * win)))

    ink_start = to_frame(_first_crossing(ink_prog, 0.015))
    ink_end = to_frame(_first_crossing(ink_prog, 0.97))
    draw_start = to_frame(max(0, _first_crossing(mark_prog, 0.005) - 1))
    if ink_end <= ink_start:
        ink_end = min(n - 1, ink_start + FPS)

    # --- satisfying moment: fastest ink gain -------------------------------------------
    gain = np.convolve(np.diff(ink_prog, prepend=0), np.ones(3) / 3, mode="same")
    peak_idx = to_frame(int(np.argmax(gain)))

    # --- clean pencil sketch right before ink ----------------------------------------
    lo = max(draw_start, ink_start - 4 * FPS)
    hi = min(n - 1, ink_start + FPS // 3)
    sketch_idx = int(lo + np.argmin(hand_score[lo:hi + 1])) if hi > lo else ink_start

    # --- focus: frame the drawing made in THIS clip, not older ones on the same page ---
    mark_obs = observed(mark_t, mark_map & ~ink_map)
    # Ink is the reliable signal (faint pencil detection also catches paper edges and shading);
    # fall back to pencil + ink only when a clip has barely any ink.
    new_work = ink_obs if ink_obs.sum() > 200 else ink_obs | mark_obs
    focus = (0.5, 0.5, 1.0)
    work_center = (0.5, 0.5)
    if new_work.sum() > 200:
        ys, xs = np.nonzero(new_work)
        x0, x1 = np.percentile(xs, [1, 99])
        y0, y1 = np.percentile(ys, [1, 99])
        work_center = (float((x0 + x1) / 2 / AW), float((y0 + y1) / 2 / ah))
        fw, fh = (x1 - x0) * 1.25 + AW * 0.04, (y1 - y0) * 1.2 + ah * 0.04   # breathing room
        if fw / fh < AW / ah:
            fw = fh * AW / ah
        else:
            fh = fw * ah / AW
        zoom = min(AW / fw, ah / fh, 1.8)       # cap: source resolution limits how far we crop
        if zoom >= 1.12:
            focus = (float((x0 + x1) / 2 / AW), float((y0 + y1) / 2 / ah), float(zoom))
    fzoom = focus[2]

    # --- detail box: densest NEW line work for a punch-in (sized relative to the focus view) ---
    lines = ink_obs.astype(np.uint8) - cv2.erode(ink_obs.astype(np.uint8), np.ones((3, 3), np.uint8))
    bw, bh = int(AW / fzoom / 2.2), int(ah / fzoom / 2.2)
    density = cv2.boxFilter(lines.astype(np.float32), -1, (bw, bh), normalize=False,
                            borderType=cv2.BORDER_CONSTANT)
    # keep the whole box on screen
    density[: bh // 2, :] = density[-(bh - bh // 2):, :] = 0
    density[:, : bw // 2] = density[:, -(bw - bw // 2):] = 0
    cy, cx = np.unravel_index(int(np.argmax(density)), density.shape)
    box = (cx - bw // 2, cy - bh // 2, bw, bh)
    box_mask = np.zeros_like(ink_map)
    box_mask[box[1]:box[1] + bh, box[0]:box[0] + bw] = True
    box_prog = progress(ink_t, observed(ink_t, ink_map & box_mask))
    detail_start = to_frame(_first_crossing(box_prog, 0.10))
    detail_end = to_frame(_first_crossing(box_prog, 0.90))

    usable = hand_score <= max(0.2, 2.5 * float(np.median(hand_score)))
    # ink_end must be a frame where the page is visible
    while ink_end > ink_start and not usable[ink_end]:
        ink_end -= 1

    return Analysis(
        n_frames=n, fps=FPS, width=store.width, height=store.height,
        draw_start=draw_start, sketch_idx=sketch_idx, ink_start=ink_start, ink_end=ink_end,
        peak_idx=peak_idx, reveal_idx=reveal_idx,
        detail_box=(box[0] / AW, box[1] / ah, bw / AW, bh / ah), focus=focus, work_center=work_center,
        detail_start=detail_start, detail_end=max(detail_end, detail_start + FPS),
        hand_score=hand_score.round(4).tolist(), ink_progress=ink_prog.round(4).tolist(),
        window_frames=win, usable=usable.tolist(),
    )
