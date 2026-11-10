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
    has_pencil: bool = True         # False = straight to ink (no pencil sketch stage to show)
    work_center: tuple[float, float] = (0.5, 0.5)    # centre of this clip's new pencil/ink
    work_span: tuple[float, float] = (0.0, 1.0)      # left/right edge of all new work (fractions)

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


def analyze(store: FrameStore, continued: bool = False, pencil: bool | None = None) -> Analysis:
    """continued: the owner says the page already had ink (only ink drawn in this clip counts).
    pencil: the owner says whether a pencil sketch came first (None = detect it)."""
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
        # A session that continues an already-inked page may add only a small share of the
        # page's ink, so judge "enough seen" in pixels, not as a share of all ink on the page.
        seen = mask & (t > 0)
        if continued:
            return seen if seen.any() else mask
        return seen if seen.sum() >= max(150, 0.02 * mask.sum()) else mask

    ink_obs = observed(ink_t, ink_map)
    ink_prog = progress(ink_t, ink_obs)
    mark_prog = progress(mark_t, observed(mark_t, mark_map & ~ink_map))

    def to_frame(w: int) -> int:
        return int(min(n - 1, max(0, w * win)))

    # Everything drawn, fine lines included. Thin pen lines look grey at analysis size and fall
    # under the ink threshold (which bold fills set), so inking is only done when BOTH the bold
    # ink and all line work have settled; otherwise the edit would jump to the finished piece
    # while small details are still being inked.
    all_t = np.where(ink_map, ink_t, mark_t)
    all_obs = observed(all_t, mark_map)
    all_prog = progress(all_t, all_obs)

    ink_start = to_frame(_first_crossing(ink_prog, 0.015))
    ink_end = to_frame(max(_first_crossing(ink_prog, 0.97), _first_crossing(all_prog, 0.97)))
    draw_start = to_frame(max(0, _first_crossing(mark_prog, 0.005) - 1))
    if ink_end <= ink_start:
        ink_end = min(n - 1, ink_start + FPS)

    # --- satisfying moment: fastest ink gain -------------------------------------------
    gain = np.convolve(np.diff(ink_prog, prepend=ink_prog[:1]), np.ones(3) / 3, mode="same")
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
    work_span = (0.0, 1.0)
    if new_work.sum() > 200:
        ys, xs = np.nonzero(new_work)
        x0, x1 = np.percentile(xs, [1, 99])
        y0, y1 = np.percentile(ys, [1, 99])
        work_center = (float((x0 + x1) / 2 / AW), float((y0 + y1) / 2 / ah))
        # every new line, fine ones included, so a small side drawing isn't cropped off
        _, sxs = np.nonzero(all_obs | new_work)
        sx0, sx1 = np.percentile(sxs, [0.3, 99.7])
        work_span = (float(sx0 / AW), float((sx1 + 1) / AW))
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

    # --- pencil or straight ink? -------------------------------------------------------
    # Just before inking, a pencil sketch sits under where the ink will go (light marks next to
    # the future ink). Straight-to-ink sessions have blank paper there, and their "pencil
    # stage" (first marks -> first ink) is only a moment.
    w_i = min(nw - 1, ink_start // win)
    light = dark_any[w_i] & ~dark_ink[w_i] & ~w_hand[w_i]
    near = cv2.dilate(light.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    pencil_frac = float((near & ink_obs).sum()) / max(1, int(ink_obs.sum()))
    pencil_phase = ink_start - draw_start
    has_pencil = bool(pencil_frac >= 0.12 or pencil_phase >= max(4 * FPS, 0.2 * (ink_end - draw_start)))
    if pencil is not None:
        has_pencil = bool(pencil)                     # the file name says so

    usable = hand_score <= max(0.2, 2.5 * float(np.median(hand_score)))
    # ink_end must be a frame where the page is visible
    while ink_end > ink_start and not usable[ink_end]:
        ink_end -= 1

    return Analysis(
        n_frames=n, fps=FPS, width=store.width, height=store.height,
        draw_start=draw_start, sketch_idx=sketch_idx, ink_start=ink_start, ink_end=ink_end,
        peak_idx=peak_idx, reveal_idx=reveal_idx,
        detail_box=(box[0] / AW, box[1] / ah, bw / AW, bh / ah), focus=focus, work_center=work_center, work_span=work_span,
        detail_start=detail_start, detail_end=max(detail_end, detail_start + FPS),
        hand_score=hand_score.round(4).tolist(), ink_progress=ink_prog.round(4).tolist(),
        window_frames=win, usable=usable.tolist(), has_pencil=has_pencil,
    )


class _Every:
    """Every k-th frame of a store (read-only view)."""

    def __init__(self, store, k: int):
        self.store, self.k = store, k
        self.width, self.height = store.width, store.height

    def __len__(self) -> int:
        return (len(self.store) + self.k - 1) // self.k

    def __getitem__(self, i: int):
        return self.store[min(int(i) * self.k, len(self.store) - 1)]


def analyze_spaced(store, k: int, **kw) -> Analysis:
    """Analyse every k-th frame, then map the result back onto all frames.

    A page re-read densely for smooth rendering is analysed at the spacing the beats were tuned
    on (where inking starts and ends, the reveal), so the dense frames only change how smooth the
    Short looks, never which part of the drawing it shows."""
    if k <= 1:
        return analyze(store, **kw)
    a = analyze(_Every(store, k), **kw)
    n = len(store)

    def at(i: int) -> int:
        return int(min(n - 1, max(0, i * k)))

    def spread(vals: list) -> list:
        return [vals[min(len(vals) - 1, i // k)] for i in range(n)]

    hand = np.array(spread(a.hand_score), np.float32)
    usable = spread(a.usable)
    # reveal: the dense frame with the least hand around the chosen one
    lo, hi = at(a.reveal_idx - 1), at(a.reveal_idx + 1)
    scores = []
    for i in range(lo, hi + 1):
        small = cv2.resize(store[i], (AW, int(round(store.height * AW / store.width))),
                           interpolation=cv2.INTER_AREA)
        scores.append(float(_skin(small).mean()))
    reveal = lo + int(np.argmin(scores)) if scores else at(a.reveal_idx)
    return Analysis(
        n_frames=n, fps=a.fps, width=a.width, height=a.height,
        draw_start=at(a.draw_start), sketch_idx=at(a.sketch_idx), ink_start=at(a.ink_start),
        ink_end=at(a.ink_end), peak_idx=at(a.peak_idx), reveal_idx=max(reveal, at(a.ink_end)) if a.reveal_idx >= a.ink_end else reveal,
        detail_box=a.detail_box, focus=a.focus, detail_start=at(a.detail_start),
        detail_end=at(a.detail_end), hand_score=hand.round(4).tolist(), ink_progress=a.ink_progress,
        window_frames=a.window_frames * k, usable=usable, has_pencil=a.has_pencil,
        work_center=a.work_center, work_span=a.work_span)


def find_subjects(store, max_k: int = 4, min_share: float = 0.1, min_sil: float = 0.5) -> list[dict]:
    """Separate drawings on one page (e.g. a head close-up, a figure, a second figure), found
    by WHERE their ink is on the finished page and WHEN it went down: drawings made one after
    another in different spots form separate groups.

    Returns [{"box": (x0, y0, x1, y1) as fractions of the frame, "frames": (first, last)}],
    earliest first; [] when the page reads as one drawing."""
    n = len(store)
    win = max(1, int(round(WINDOW_S * FPS)))
    if n < 12 * win:
        return []
    ah = int(round(store.height * AW / store.width))
    grays, hands, hs = [], [], np.zeros(n, np.float32)
    bg, bh = [], []
    for i in range(n):
        small = cv2.resize(store[i], (AW, ah), interpolation=cv2.INTER_AREA)
        hand = _skin(small)
        hs[i] = hand.mean()
        bg.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        bh.append(hand)
        if len(bg) == win or i == n - 1:
            grays.append(np.median(np.stack(bg), axis=0).astype(np.uint8))
            hm = np.mean(np.stack(bh), axis=0) > 0.3
            hands.append(cv2.dilate(hm.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0)
            bg, bh = [], []
    nw = len(grays)
    tail = max(1, int(n * 0.2))
    ok = np.nonzero(hs[n - tail:] <= hs[n - tail:].min() + 0.005)[0]
    reveal = cv2.cvtColor(cv2.resize(store[int(n - tail + ok[-1])], (AW, ah), interpolation=cv2.INTER_AREA),
                          cv2.COLOR_BGR2GRAY)
    d = _darkness(reveal)
    thr = max(60.0, float(np.percentile(d[d > 20], 75))) if (d > 20).any() else 60.0
    ink_map = d > thr * 0.6                                   # every inked line on the finished page
    ink_t = _inked_at([_darkness(g) > thr * 0.6 for g in grays], hands, ink_map)
    mark_map = (d > 18) & ~ink_map
    mark_t = _inked_at([_darkness(g) > 12 for g in grays], hands, mark_map)

    ys, xs = np.nonzero(ink_t > 0)                            # lines we saw go down in this clip
    if len(xs) < 300:
        return []
    rng = np.random.default_rng(0)
    pick = rng.choice(len(xs), size=min(len(xs), 6000), replace=False)
    xs, ys, ts = xs[pick], ys[pick], ink_t[ys[pick], xs[pick]]
    feat = np.stack([xs / AW, ys / AW, 0.7 * ts / nw], axis=1).astype(np.float32)

    def silhouette(lab: np.ndarray, k: int) -> float:
        s = rng.choice(len(feat), size=min(len(feat), 1500), replace=False)
        f, l = feat[s], lab[s]
        dist = np.sqrt(((f[:, None, :] - f[None, :, :]) ** 2).sum(-1))
        a = np.array([dist[i, l == l[i]].sum() / max(1, (l == l[i]).sum() - 1) for i in range(len(f))])
        b = np.array([min(dist[i, l == c].mean() for c in range(k) if c != l[i] and (l == c).any())
                      for i in range(len(f))])
        return float(np.mean((b - a) / np.maximum(a, b).clip(1e-6)))

    best = None
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-3)
    for k in range(2, max_k + 1):
        _, lab, _ = cv2.kmeans(feat, k, None, crit, 4, cv2.KMEANS_PP_CENTERS)
        lab = lab.ravel()
        shares = np.bincount(lab, minlength=k) / len(lab)
        if shares.min() < min_share:
            continue
        sil = silhouette(lab, k)
        if sil >= min_sil and (best is None or sil > best[0] + 0.02):
            best = (sil, k, lab)
    if best is None:
        return []
    _, k, lab = best
    W, H = AW, ah
    parts = []
    for c in range(k):
        m = lab == c
        x0, x1 = np.percentile(xs[m], [2, 98])
        y0, y1 = np.percentile(ys[m], [2, 98])
        box = (x0 / W, y0 / H, x1 / W, y1 / H)
        # its pencil sketch comes first: start at the earliest marks inside its box
        bx = (slice(int(y0), int(y1) + 1), slice(int(x0), int(x1) + 1))
        mt = mark_t[bx]
        starts = [np.percentile(ts[m], 2)]
        if (mt > 0).sum() > 100:
            starts.append(np.percentile(mt[mt > 0], 5))
        w0 = max(0, int(min(starts)) - 1)
        w1 = min(nw, int(np.percentile(ts[m], 98)) + 1)
        parts.append({"box": tuple(round(float(v), 4) for v in box),
                      "frames": (int(w0 * win), int(min(n - 1, w1 * win)))})
    # two groups on top of each other are one drawing in two phases, not two drawings
    for a in range(k):
        for b in range(a + 1, k):
            if _iou(parts[a]["box"], parts[b]["box"]) > 0.5:
                return []
    return sorted(parts, key=lambda p: p["frames"][0])


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0
