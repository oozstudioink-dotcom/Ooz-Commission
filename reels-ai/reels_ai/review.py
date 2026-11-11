"""The reviewer: every finished Short is checked before it is sent to you.

It looks for the faults that used to reach Telegram:
  - too short (the edit only caught the last seconds of a drawing)
  - stutter (the same frame shown again and again: stop-motion look)
  - blank or black stretches
  - another page in the video (a different drawing at the start, in a wipe, or at the end)

Each check answers with a plain reason. A Short that fails is not sent: the editor makes another
style in its place, and if nothing passes the drawing is held and you get one message saying why.
The thresholds were set on real videos: Shorts you rejected must fail, Shorts you liked and the
reference clips must pass (tests/test_review.py holds the measurements).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

MIN_SECONDS = 6.0          # shorter than this never showed a whole drawing
MAX_REPEATS = 0.22         # share of frames identical to the one before (holds count too)
MAX_BLANK = 0.12           # share of sampled frames that are black or empty
SAMPLES = 22               # frames compared for "is this all one page?"
MIN_INK = 0.012            # a frame needs this much ink (share of the area) to be judged
SAME = 0.45                # how much of the smaller drawing's ink sits in the bigger one, above chance
FOREIGN_FRAMES = 2         # this many clearly-other-page frames fail the Short


@dataclass
class Review:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)
    seconds: float = 0.0
    repeats: float = 0.0
    blank: float = 0.0
    foreign: list[float] = field(default_factory=list)      # times (s) of other-page frames

    def fail(self, why: str) -> None:
        self.ok = False
        self.reasons.append(why)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reasons": self.reasons, "seconds": round(self.seconds, 1),
                "repeats": round(self.repeats, 3), "blank": round(self.blank, 3),
                "foreign_at": [round(t, 1) for t in self.foreign]}


def _ink(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(dark ink, hand) masks of the middle of a frame, small. On-video text sits at the top and
    bottom, so only the band between is compared."""
    from .analyze import _skin
    small = cv2.resize(frame, (180, 320), interpolation=cv2.INTER_AREA)[86:282]
    # undo a colour grade (warm/sepia) first: paper back to neutral, or it reads as skin
    p = np.percentile(small.reshape(-1, 3), 90, axis=0).astype(np.float32)
    small = np.clip(small.astype(np.float32) * (float(p.mean()) / np.maximum(p, 1.0)), 0, 255).astype(np.uint8)
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    paper = cv2.GaussianBlur(cv2.dilate(g, np.ones((9, 9), np.uint8)), (0, 0), 4)
    hand = cv2.dilate(_skin(small).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    return ((paper - g) > np.maximum(35, 0.22 * paper)) & ~hand & (paper > 110), hand   # ink on paper, not a dark desk


RICH = 0.045               # a frame with this much ink is a well-inked page: it must be recognisable


def _shifted(small: np.ndarray, big: np.ndarray, big_hand: np.ndarray) -> float:
    """Share of `small`'s ink that lands on `big`'s ink, above what chance would give, allowing
    the paper to have been nudged (up to a tenth of the frame). 0..1."""
    target = ((cv2.dilate(big.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0) | big_hand).astype(np.float32)
    H, W = small.shape
    core = small[int(H * 0.1):int(H * 0.9), int(W * 0.1):int(W * 0.9)].astype(np.float32)
    n = float(core.sum())
    if n < 40:
        return 0.0
    hit = cv2.matchTemplate(target, core, cv2.TM_CCORR) / n
    h, w = core.shape
    cover = cv2.matchTemplate(target, np.ones_like(core), cv2.TM_CCORR) / float(h * w)
    return float(((hit - cover) / np.maximum(1e-3, 1 - cover)).max())


def _same(a, b, frames=None, i: int = 0, j: int = 0, cache: dict | None = None) -> bool | None:
    """Do two frames show the same page? None when it can't be told.
    A page only gains ink, so the drawing with less ink must sit inside the one with more: as
    filmed (or nudged), or after lining the two up when the camera zoomed or moved. Two well-inked
    frames that can't be lined up at all are different pages."""
    (ia, ha), (ib, hb) = a, b
    first_small = ia.sum() <= ib.sum()
    small, big, bh = (ia, ib, hb) if first_small else (ib, ia, ha)
    if small.sum() < MIN_INK * small.size:
        return None
    if _shifted(small, big, bh) >= SAME:
        return True
    rich = small.mean() >= RICH and big.mean() >= RICH
    if frames is None:
        return False if rich else None
    from .media import PageMatcher
    lo, hi = (i, j) if first_small else (j, i)
    cache = cache if cache is not None else {}
    if hi not in cache:
        cache[hi] = PageMatcher(frames[hi])
    H = cache[hi].match(frames[lo])
    if H is None:
        return False if rich else None
    h, w = frames[hi].shape[:2]
    warped = cv2.warpPerspective(frames[lo], H, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    ws, _ = _ink(warped)
    if ws.sum() < MIN_INK * ws.size * 0.5:
        return None
    return _shifted(ws, big, bh) >= SAME * 0.8 or (None if not rich else False)


def review(path: Path, min_seconds: float = MIN_SECONDS) -> Review:
    """Watch a finished Short and say whether it can be sent."""
    r = Review()
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if n < 2:
        cap.release()
        r.fail("the video file is empty")
        return r
    want = set(np.linspace(0, n - 1, SAMPLES).astype(int).tolist())
    frames, times = [], []
    prev, same, total, blank = None, 0, 0, 0
    k = 0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        g = cv2.cvtColor(cv2.resize(f, (90, 160), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            total += 1
            same += float(np.abs(g - prev).mean()) < 0.3
        prev = g
        if k in want:
            frames.append(f)
            times.append(k / fps)
            blank += g.mean() < 25 or g.std() < 3            # black, or one flat colour
        k += 1
    cap.release()
    r.seconds = k / fps
    r.repeats = same / max(1, total)
    r.blank = blank / max(1, len(frames))

    if r.seconds < min_seconds:
        r.fail(f"only {r.seconds:.0f} s long: it doesn't show the whole drawing")
    if r.repeats > MAX_REPEATS:
        r.fail(f"stutters: {r.repeats * 100:.0f}% of frames repeat the one before")
    if r.blank > MAX_BLANK:
        r.fail("has black or empty stretches")

    # one page throughout? compare every sampled frame with every other
    inks = [_ink(f) for f in frames]
    m = len(frames)
    cache: dict = {}
    rel: dict[tuple[int, int], bool | None] = {}
    for i in range(m):
        for j in range(i + 1, m):
            rel[i, j] = rel[j, i] = _same(inks[i], inks[j], frames, i, j, cache)
    agree = [sum(1 for j in range(m) if j != i and rel[i, j]) for i in range(m)]
    # the page of the video: the frames that agree with a well-inked frame most others agree with
    # (a frame with a hand over the page "fits" anything, so it can't be the yardstick)
    rich = [i for i in range(m) if inks[i][0].mean() >= RICH] or list(range(m))
    if m and max(agree[i] for i in rich) >= 3:
        main = max(rich, key=lambda i: agree[i])
        group = [main]
        for _ in range(2):                       # grow the group with frames that fit most of it
            for i in sorted(range(m), key=lambda i: -agree[i]):
                if i in group:
                    continue
                judged = [rel[i, j] for j in group if rel[i, j] is not None]
                if judged and sum(1 for x in judged if x) >= 0.7 * len(judged):
                    group.append(i)
        for i in range(m):
            if i in group:
                continue
            judged = [rel[i, j] for j in group if rel[i, j] is not None]
            if len(judged) >= 3 and sum(1 for x in judged if x is False) >= 0.7 * len(judged):
                r.foreign.append(times[i])
        if len(r.foreign) >= FOREIGN_FRAMES:
            at = ", ".join(f"{t:.0f}s" for t in r.foreign[:4])
            r.fail(f"shows a different page (around {at})")
    return r
