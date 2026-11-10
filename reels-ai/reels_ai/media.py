"""Video I/O: normalize any input to a vertical working copy, cache frames, encode output."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

FPS = 30
MAX_WORK_SECONDS = 90  # long raw timelapses are pre-sped to fit this
MAX_W, MAX_H = 1080, 1920


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    st = data["streams"][0]
    return {"width": int(st["width"]), "height": int(st["height"]),
            "duration": float(data["format"]["duration"])}


def normalize(src: Path, dst: Path, keep_wide: bool = False, max_wide_h: int = 1440,
              max_w: int = MAX_W) -> dict:
    """Make a 9:16, 30fps, <=90s H.264 working copy.

    Wider inputs get a blurred backdrop, unless keep_wide: then they keep their shape (height
    capped at max_wide_h) so the caller can crop a vertical frame around the drawing."""
    info = probe(src)
    # ffprobe reports pre-rotation size; ffmpeg autorotates, so decide from a decoded frame.
    cap = cv2.VideoCapture(str(src))
    ok, frame = cap.read()
    cap.release()
    h, w = (frame.shape[:2] if ok else (info["height"], info["width"]))

    out_w = min(max_w, w if w / h <= 9 / 16 else int(h * 9 / 16))
    out_w -= out_w % 2
    out_h = int(round(out_w * 16 / 9))
    out_h -= out_h % 2

    speed = max(1.0, info["duration"] / MAX_WORK_SECONDS)
    tempo = f"setpts=PTS/{speed:.4f}," if speed > 1 else ""

    if keep_wide and w / h > 9 / 16 + 0.01:
        out_h = min(max_wide_h, h)
        out_h -= out_h % 2
        out_w = int(round(w * out_h / h))
        out_w -= out_w % 2
        vf = f"{tempo}fps={FPS},scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    elif abs(w / h - 9 / 16) < 0.01:
        vf = f"{tempo}fps={FPS},scale={out_w}:{out_h}:flags=lanczos,setsar=1"
    elif w / h < 9 / 16:  # taller than 9:16: crop top/bottom
        vf = f"{tempo}fps={FPS},scale={out_w}:-2:flags=lanczos,crop={out_w}:{out_h},setsar=1"
    else:  # wider: fit inside, blurred fill behind
        vf = (f"{tempo}fps={FPS},split[a][b];"
              f"[a]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},boxblur=30[bg];"
              f"[b]scale={out_w}:-2:flags=lanczos[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-an", "-vf", vf,
         "-c:v", "libx264", "-crf", "14", "-preset", "fast", "-pix_fmt", "yuv420p", str(dst)],
        check=True,
    )
    return {"source_duration": info["duration"], "speedup": speed, "width": out_w, "height": out_h}


def vertical_filter(src_w: int, src_h: int, xf: float, wf: float, ow: int, oh: int) -> str:
    """ffmpeg filter (ending in ',') that turns a wide frame into an ow x oh vertical one.

    xf/wf: left edge and width of the wanted window as fractions of the source width, full
    height. A window up to 9:16 is cropped and scaled; a wider one (drawings spread across the
    page) is shown whole on a blurred copy of itself, so nothing new is cut off. Written in
    iw/ih terms so it fits the raw 4K recording and the working copy alike."""
    crop = f"crop=trunc(iw*{wf:.5f}/2)*2:ih:trunc(iw*{xf:.5f}):0"
    if wf * src_w <= src_h * 9 / 16 + 2:
        return f"{crop},scale={ow}:{oh}:flags=lanczos,setsar=1,"
    return (f"split[lbA][lbB];[lbA]{crop},scale={ow}:-2:flags=lanczos[lbF];"
            f"[lbB]{crop},scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},"
            f"boxblur=40:2,eq=brightness=-0.05[lbG];[lbG][lbF]overlay=(W-w)/2:(H-h)/2,setsar=1,")


def crop_vertical(src: Path, dst: Path, center_x: float, out_w: int = 1080,
                  span: tuple[float, float] | None = None) -> dict:
    """Cut a full-height vertical window out of a wide working copy, centred on center_x (0..1).

    span: left/right edge of all new work. If it doesn't fit a 9:16 window, the window widens
    to take it all in (letterboxed on a blurred copy) instead of cutting a drawing off.
    out_w is a cap: the picture is never enlarged past the crop's own width."""
    info = probe(src)
    w, h = info["width"], info["height"]
    base = int(round(h * 9 / 16))
    cw = base
    if span:
        need = (span[1] - span[0]) * w * 1.08                  # a little breathing room
        cw = int(min(w, max(base, need)))
        center_x = (span[0] + span[1]) / 2 if need > base else center_x
    cw -= cw % 2
    x = int(round(min(max(center_x * w - cw / 2, 0), w - cw)))
    ow = min(out_w, base)
    ow -= ow % 2
    oh = int(round(ow * 16 / 9))
    oh -= oh % 2
    vf = vertical_filter(w, h, x / w, cw / w, ow, oh)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-an", "-vf", vf.rstrip(","),
         "-c:v", "libx264", "-crf", "14", "-preset", "fast", "-pix_fmt", "yuv420p", str(dst)],
        check=True)
    return {"width": ow, "height": oh, "crop_x": x, "crop_w": cw, "letterbox": cw > base + 2,
            "filter": vf, "xf": x / w, "wf": cw / w}


class FrameStore:
    """All frames of the working copy, held as high-quality JPEG bytes (random access, low RAM)."""

    def __init__(self, path: Path):
        cap = cv2.VideoCapture(str(path))
        self._frames: list[bytes] = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            self._frames.append(cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, 96])[1].tobytes())
        cap.release()
        if not self._frames:
            raise RuntimeError(f"could not decode any frames from {path}")
        first = self[0]
        self.height, self.width = first.shape[:2]
        self._cache: dict[int, np.ndarray] = {0: first}

    def __len__(self) -> int:
        return len(self._frames)

    def __getitem__(self, i: int) -> np.ndarray:
        i = int(min(max(i, 0), len(self._frames) - 1))
        cache = getattr(self, "_cache", None)
        if cache is not None and i in cache:
            return cache[i]
        img = cv2.imdecode(np.frombuffer(self._frames[i], np.uint8), cv2.IMREAD_COLOR)
        if cache is not None:
            if len(cache) > 64:
                cache.clear()
            cache[i] = img
        return img


class Encoder:
    """Pipe BGR frames into ffmpeg -> Instagram-friendly MP4 (H.264 high, AAC track, faststart)."""

    def __init__(self, path: Path, width: int, height: int, audio: Path | None = None,
                 duration: float | None = None, volume: float = 0.9, crf: int = 17):
        cmd = ["ffmpeg", "-v", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-"]
        if audio:
            cmd += ["-stream_loop", "-1", "-i", str(audio)]
            fade = (f",afade=t=in:d=0.3,afade=t=out:st={max(0.0, duration - 1.2):.2f}:d=1.2"
                    if duration else "")
            cmd += ["-filter:a", f"aresample=44100,volume={volume}{fade}", "-ac", "2"]
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        cmd += ["-map", "0:v", "-map", "1:a", "-shortest",
                "-c:v", "libx264", "-profile:v", "high", "-crf", str(crf), "-preset", "medium",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(path)]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError("ffmpeg encode failed")


def preview_copy(src: Path, dst: Path, width: int = 1080, crf: int = 21) -> Path:
    """Smaller copy for Telegram (bots can send at most 50 MB); returns src if already small."""
    info = probe(src)
    if info["width"] <= width and Path(src).stat().st_size < 45 * 1024 * 1024:
        return Path(src)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"scale={width}:-2:flags=lanczos",
                    "-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", str(dst)], check=True)
    return Path(dst)


def page_changes(store: "FrameStore", win: int = 6, threshold: float = 0.5) -> list[int]:
    """Frames where the camera moved to a different page (e.g. flipping pages on a livestream).

    In one drawing session ink only ever appears. A page change does two things at once: most of
    the inked lines that were there vanish, and a lot of different lines appear. The page before
    and after each frame is a median of several frames, and anything a hand, arm or pen covers in
    either stretch is ignored, so moving your hand around, erasing, or uncovering lines doesn't
    count."""
    from .analyze import _skin
    n = len(store)
    if n < 4 * win:
        return []
    gray, hand = [], []
    for i in range(n):
        small = cv2.resize(store[i], (128, 228), interpolation=cv2.INTER_AREA)
        gray.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        hand.append(_skin(small))
    gray = np.stack(gray).astype(np.float32)
    hand = np.stack(hand)
    min_ink = 0.008 * gray.shape[1] * gray.shape[2]          # a real amount of drawing on the page
    grow = np.ones((5, 5), np.uint8)

    def ink(img: np.ndarray) -> np.ndarray:
        paper = cv2.GaussianBlur(cv2.dilate(img, np.ones((9, 9), np.uint8)), (0, 0), 4)
        return (paper - img) > 75                     # dark ink only: pencil may be erased

    scores = np.zeros(n)
    for i in range(win, n - win, 2):
        covered = cv2.dilate(hand[i - win:i + win].any(axis=0).astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
        before = ink(np.median(gray[i - win:i], axis=0)) & ~covered
        after = ink(np.median(gray[i:i + win], axis=0)) & ~covered
        s = 0.0
        if before.sum() >= min_ink:                   # inked lines vanished: never happens on one page
            s = float((before & ~(cv2.dilate(after.astype(np.uint8), grow) > 0)).sum()) / float(before.sum())
        if after.sum() >= 2 * min_ink:                # a lot of finished ink appeared at once
            s = max(s, float((after & ~(cv2.dilate(before.astype(np.uint8), grow) > 0)).sum()) / float(after.sum()))
        scores[i] = scores[i + 1] = s
    cuts = []
    i = win
    while i < n - win:
        if scores[i] > threshold:
            j = i
            while j + 1 < n - win and scores[j + 1] > threshold:   # one change, several frames wide
                j += 1
            cuts.append(int(i + int(np.argmax(scores[i:j + 1]))))
            i = j + win
        else:
            i += 1
    return cuts


def same_page(store: "FrameStore", first: int, last: int, win: int = 6, tol: float = 0.45,
              log=print) -> tuple[int, int]:
    """Trim frames off either end of first..last that don't show the same page as its middle.

    On one page ink only ever gets added: every line in the middle of the stretch is still there
    at the end, and every line at the start is already there in the middle. A stretch that runs
    into another page (a flip at the end of a livestream, where the hands hide the flip from
    page_changes) breaks that, so those frames are cut. The middle is taken as the page being
    drawn; small camera shifts are lined up before comparing."""
    from .analyze import _skin
    n = last - first + 1
    if n < 6 * win:
        return first, last
    step = max(1, n // 600)                                   # ~600 frames is plenty to judge
    idx = list(range(first, last + 1, step))
    gray, hand = [], []
    for i in idx:
        small = cv2.resize(store[i], (128, 228), interpolation=cv2.INTER_AREA)
        gray.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32))
        hand.append(_skin(small))
    grow = np.ones((7, 7), np.uint8)

    def ink(img: np.ndarray, level: float = 40) -> np.ndarray:
        paper = cv2.GaussianBlur(cv2.dilate(img, np.ones((9, 9), np.uint8)), (0, 0), 4)
        return (paper - img) > level

    def page(a: int, b: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        g = np.median(np.stack(gray[a:b]), axis=0).astype(np.float32)
        h = cv2.dilate(np.stack(hand[a:b]).any(axis=0).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        return g, ink(g) & ~h, h

    m = len(idx)
    ref_g, ref_ink, ref_h = page(int(m * 0.45), max(int(m * 0.45) + 2, int(m * 0.55)))
    ref_bold = ink(ref_g, 75) & ~ref_h        # real ink only: pencil may be erased later on
    if ref_ink.sum() < 0.004 * ref_ink.size:                  # nearly blank middle: nothing to judge
        return first, last
    win_n = max(2, win // step if step > 1 else win)
    bad = []
    for a in range(0, m - win_n + 1, max(1, win_n // 2)):
        g, k, h = page(a, a + win_n)
        (dx, dy), resp = cv2.phaseCorrelate(ref_g, g)
        if resp > 0.1 and abs(dx) < 20 and abs(dy) < 20:      # line the window up with the middle
            mat = np.float32([[1, 0, -dx], [0, 1, -dy]])
            k = cv2.warpAffine(k.astype(np.uint8), mat, k.shape[::-1]) > 0
            h = cv2.warpAffine(h.astype(np.uint8), mat, h.shape[::-1]) > 0
        seen = ~(h | ref_h)
        later = a >= m // 2
        # later frames must keep the middle's lines; earlier frames must have no lines it lacks
        have, want = (k, ref_bold) if later else (ref_ink, k)
        want = want & seen
        if want.sum() < 0.002 * want.size:
            continue
        miss = float((want & ~(cv2.dilate(have.astype(np.uint8), grow) > 0)).sum()) / float(want.sum())
        if miss > tol:
            bad.append((a, later))
    if not bad:
        return first, last
    lo, hi = 0, m - 1
    heads = [a for a, later in bad if not later]
    tails = [a for a, later in bad if later]
    if heads:
        lo = max(heads) + win_n
    if tails:
        hi = min(tails) - 1
    if hi - lo < m // 3:                                      # unsure: keep what we had
        return first, last
    nf, nl = idx[min(lo, m - 1)], idx[max(hi, 0)]
    log(f"same page only: kept frames {nf}-{nl} of {first}-{last} (the rest showed another page)")
    return nf, nl


def crop_box(src: Path, dst: Path, box: tuple[float, float, float, float], max_w: int = 1080,
             margin: float = 0.06) -> dict:
    """Cut a 9:16 window around one drawing on the page (box: x0, y0, x1, y1 as fractions).

    The window takes in the whole box plus a little room, as large as it needs to be (never
    past the frame), and is scaled to a vertical working size. Returns the ffmpeg filter (in
    iw/ih terms, so it fits the original recording too) and the window."""
    info = probe(src)
    W, H = info["width"], info["height"]
    x0, y0, x1, y1 = box
    bw, bh = (x1 - x0 + 2 * margin) * W, (y1 - y0 + 2 * margin) * H
    ch = min(H, max(bh, bw * 16 / 9))
    cw = ch * 9 / 16
    if cw > W:
        cw, ch = W, W * 16 / 9
    cw, ch = int(cw) // 2 * 2, int(ch) // 2 * 2
    cx, cy = (x0 + x1) / 2 * W, (y0 + y1) / 2 * H
    x = int(round(min(max(cx - cw / 2, 0), W - cw)))
    y = int(round(min(max(cy - ch / 2, 0), H - ch)))
    ow = min(max_w, W if W / H <= 9 / 16 + 0.02 else int(H * 9 / 16)) // 2 * 2
    oh = int(round(ow * 16 / 9)) // 2 * 2
    xf, yf, wf, hf = x / W, y / H, cw / W, ch / H
    vf = (f"crop=trunc(iw*{wf:.5f}/2)*2:trunc(ih*{hf:.5f}/2)*2:trunc(iw*{xf:.5f}):trunc(ih*{yf:.5f}),")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-an", "-vf",
                    f"{vf}scale={ow}:{oh}:flags=lanczos,setsar=1",
                    "-c:v", "libx264", "-crf", "14", "-preset", "fast", "-pix_fmt", "yuv420p", str(dst)],
                   check=True)
    return {"filter": vf, "width": ow, "height": oh, "crop": (x, y, cw, ch)}


def ink_added(store: "FrameStore", a: int, b: int, k: int = 6) -> float:
    """How much new ink appears between the start and the end of frames a..b (share of the
    frame). A page you're drawing on gains ink; an old page you're just showing gains none."""
    from .analyze import _skin
    if b - a < 2 * k:
        return 0.0

    def page(idx: list[int]) -> tuple[np.ndarray, np.ndarray]:
        gs, hs = [], []
        for i in idx:
            small = cv2.resize(store[i], (128, 228), interpolation=cv2.INTER_AREA)
            gs.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32))
            hs.append(_skin(small))
        g = np.median(np.stack(gs), axis=0).astype(np.float32)
        h = cv2.dilate(np.stack(hs).any(axis=0).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
        paper = cv2.GaussianBlur(cv2.dilate(g, np.ones((9, 9), np.uint8)), (0, 0), 4)
        return (paper - g) > 40, h

    span = max(k, (b - a) // 20)
    start, h0 = page(list(np.linspace(a, a + span, k).astype(int)))
    end, h1 = page(list(np.linspace(b - span, b, k).astype(int)))
    grown = end & ~(cv2.dilate(start.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & ~h0 & ~h1
    return float(grown.mean())


def drawn_stretch(store: "FrameStore", cuts: list[int], log=print) -> tuple[int, int]:
    """Of the stretches between page changes, the one where you drew: the most ink added.
    (The longest one can be an old page you were showing on a livestream.)"""
    n = len(store)
    edges = [0] + sorted(cuts) + [n]
    spans = [(a, b - 1) for a, b in zip(edges, edges[1:]) if b - a >= max(30, n // 50)]
    if not spans:
        return longest_stretch(n, cuts)
    scored = [(ink_added(store, a, b), a, b) for a, b in spans]
    best = max(scored)
    if best[0] < 0.002:                       # nobody drew anywhere we can see: longest it is
        return longest_stretch(n, cuts)
    log("pages: " + ", ".join(f"{a}-{b} +{s * 100:.1f}% ink" for s, a, b in scored))
    return best[1], best[2]


def contact_sheet(store: "FrameStore", dst: Path, seconds_per_frame: float, span: tuple[int, int],
                  cols: int = 6, rows: int = 4, used: set | None = None) -> Path:
    """A grid of frames across the recording with their times; the frames used are outlined green."""
    import bisect
    n = len(store)
    kept = sorted(used) if used else None

    def is_used(i: int) -> bool:
        if kept is None:
            return span[0] <= i <= span[1]
        j = bisect.bisect_left(kept, i)
        return any(0 <= q < len(kept) and abs(kept[q] - i) <= 2 for q in (j - 1, j))

    idx = np.linspace(0, n - 1, cols * rows).astype(int)
    tw = 180
    th = int(round(store.height * tw / store.width))
    sheet = np.full((rows * (th + 30), cols * tw, 3), 20, np.uint8)
    for k, i in enumerate(idx):
        r, c = divmod(k, cols)
        img = cv2.resize(store[int(i)], (tw, th), interpolation=cv2.INTER_AREA)
        if is_used(i):
            cv2.rectangle(img, (0, 0), (tw - 1, th - 1), (60, 220, 60), 6)
        y, x = r * (th + 30), c * tw
        sheet[y:y + th, x:x + tw] = img
        s = int(i * seconds_per_frame)
        cv2.putText(sheet, f"{s // 60}:{s % 60:02d}", (x + 6, y + th + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                    (60, 220, 60) if is_used(i) else (230, 230, 230), 2, cv2.LINE_AA)
    cv2.imwrite(str(dst), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return dst


def longest_stretch(n: int, cuts: list[int]) -> tuple[int, int]:
    """The longest run of frames between page changes: (first, last)."""
    edges = [0] + sorted(cuts) + [n]
    a, b = max(zip(edges, edges[1:]), key=lambda ab: ab[1] - ab[0])
    return a, b - 1


def trim(src: Path, dst: Path, first: int, last: int) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-an", "-vf",
                    f"select='between(n,{first},{last})',setpts=N/{FPS}/TB", "-r", str(FPS),
                    "-c:v", "libx264", "-crf", "14", "-preset", "fast", "-pix_fmt", "yuv420p", str(dst)],
                   check=True)


# ---------------------------------------------------------------- page recognition
class PageMatcher:
    """Recognises one page in any frame, like panorama stitching: distinctive points of its lines
    (corners, crossings, a signature) are matched, and a consistent geometric fit (homography)
    says it's the same page, and how it moved or zoomed relative to the reference frame.
    Other pages, flips and close-ups of something else don't fit."""

    WIDTH = 540

    def __init__(self, ref: np.ndarray):
        self.size = (ref.shape[1], ref.shape[0])
        self.s = self.WIDTH / ref.shape[1]
        self.small = (self.WIDTH, int(round(ref.shape[0] * self.s)))
        self.orb = cv2.ORB_create(2500, fastThreshold=12)
        self.kp, self.des = self.features(ref)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)

    def features(self, img: np.ndarray):
        g = cv2.cvtColor(cv2.resize(img, (self.WIDTH, int(round(img.shape[0] * self.s)))), cv2.COLOR_BGR2GRAY)
        g = cv2.createCLAHE(2.0, (8, 8)).apply(g)
        return self.orb.detectAndCompute(g, None)

    def fit(self, kp, des, kp2, des2) -> np.ndarray | None:
        """Homography (small coords) taking points of (kp, des) onto (kp2, des2), or None."""
        if des is None or des2 is None or len(kp) < 20 or len(kp2) < 20:
            return None
        good = []
        for pair in self.bf.knnMatch(des, des2, k=2):
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                good.append(pair[0])
        if len(good) < 15:
            return None
        src = np.float32([kp[m.queryIdx].pt for m in good])
        dst = np.float32([kp2[m.trainIdx].pt for m in good])
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if H is None or mask is None:
            return None
        inl = int(mask.sum())
        if inl < 25 or inl < 0.2 * len(good):
            return None
        det = float(np.linalg.det(H[:2, :2]))
        if not 0.35 < det < 3.0 or abs(H[2, 0]) > 0.002 or abs(H[2, 1]) > 0.002:
            return None                                        # a wild fit: not really the page
        # a fit that says the camera moved a lot needs much stronger evidence than "same place"
        # (a pencil page against its inked self can give a few dozen chance agreements)
        if _moved(H, self.small) > 0.05 and (inl < 80 or inl < 0.4 * len(good)):
            return None
        return H

    def to_full(self, H: np.ndarray) -> np.ndarray:
        S = np.diag([self.s, self.s, 1.0])
        return np.linalg.inv(S) @ H @ S

    def match(self, img: np.ndarray) -> np.ndarray | None:
        kp, des = self.features(img)
        H = self.fit(kp, des, self.kp, self.des)
        return None if H is None else self.to_full(H)


def _moved(H: np.ndarray, size: tuple[int, int]) -> float:
    """How far a homography moves the frame's corners, as a share of its width."""
    w, h = size
    pts = np.float32([[0, 0], [w, 0], [0, h], [w, h]]).reshape(-1, 1, 2)
    return float(np.abs(cv2.perspectiveTransform(pts, H) - pts).max() / w)


def choose_page(store: "FrameStore", a: int, b: int, k: int = 36) -> int | None:
    """The frame that best shows the page being drawn in a..b: the page seen most often there
    (other pages flipped to are seen briefly), its latest clear view (the most ink)."""
    from .analyze import _skin
    idx = sorted(set(np.linspace(a, b, k).astype(int).tolist()))
    if len(idx) < 4:
        return None
    m = PageMatcher(store[idx[-1]])
    feats, hand = [], []
    for i in idx:
        img = store[i]
        feats.append(m.features(img))
        hand.append(float(_skin(cv2.resize(img, (128, 228))).mean()))
    same = np.zeros((len(idx), len(idx)), bool)
    for x in range(len(idx)):
        for y in range(x + 1, len(idx)):
            ok = m.fit(*feats[x], *feats[y]) is not None
            same[x, y] = same[y, x] = ok
    score = same.sum(1)
    if score.max() < 2:
        return None
    best = max(range(len(idx)), key=lambda z: (score[z], z))       # the page seen most; later wins ties
    # the same view of it (not a zoomed-in close-up or a moved camera): that's the shot to keep
    size = (PageMatcher.WIDTH, int(round(store.height * PageMatcher.WIDTH / store.width)))
    group = [best]
    for z in range(len(idx)):
        if z != best and same[best, z]:
            H = m.fit(*feats[z], *feats[best])
            if H is not None and _moved(H, size) < 0.05:
                group.append(z)
    group.sort()
    late = group[len(group) // 2:]                                 # the most finished views ...
    pick = min(late, key=lambda z: (hand[z], -z))                  # ... with the least hand in the way
    return idx[pick]


def track_page(store: "FrameStore", matcher: PageMatcher, frames: list[int], log=print,
               max_move: float = 0.12, others: list | None = None) -> list[tuple[int, np.ndarray]]:
    """Every frame of `frames` that shows the page, with the homography onto the reference.
    Frames that match the reference directly are kept; around them, frames are followed from
    neighbour to neighbour (the start of a drawing has too few lines to match the finished
    page). Flips, other pages and blank paper drop out."""
    feats: dict[int, tuple] = {}

    def feat(i):
        if i not in feats:
            feats[i] = matcher.features(store[i])
        return feats[i]

    direct: dict[int, np.ndarray] = {}
    for i in frames:
        kp, des = feat(i)
        H = matcher.fit(kp, des, matcher.kp, matcher.des)
        if H is not None:
            direct[i] = H
    if not direct:
        return []
    kept = dict(direct)
    pos = {f: n for n, f in enumerate(frames)}
    size = (PageMatcher.WIDTH, int(round(store.height * matcher.s)))
    # follow the page backwards (and forwards) from frames that matched, through neighbours
    for step in (-1, 1):
        for start in sorted(direct, reverse=step < 0):
            n, misses, last = pos[start], 0, start
            while misses < 25:
                n += step
                if n < 0 or n >= len(frames):
                    break
                i = frames[n]
                if i in kept:
                    last, misses = i, 0
                    continue
                kp, des = feat(i)
                kp2, des2 = feat(last)
                H = matcher.fit(kp, des, kp2, des2)
                if H is None:
                    misses += 1
                    continue
                # frame -> neighbour -> reference; a neighbour in the same place keeps its fit
                # (multiplying hundreds of tiny measuring errors would drift)
                kept[i] = kept[last] if _moved(H, size) < 0.01 else kept[last] @ H
                last, misses = i, 0
            if len(feats) > 4000:
                feats.clear()
    size = (PageMatcher.WIDTH, int(round(store.height * matcher.s)))
    out, other_view = [], 0
    for i in frames:
        if i not in kept:
            continue
        if _moved(kept[i], size) > max_move:                     # a close-up or another angle: a
            other_view += 1                                        # different shot, not stretched in
            if others is not None:
                others.append(i)
            continue
        out.append((i, matcher.to_full(kept[i])))
    log(f"page recognition: {len(out)} of {len(frames)} frames show the page "
        f"({len(direct)} matched it directly; {other_view} from another view left out)")
    return out


def write_page(store: "FrameStore", track: list[tuple[int, np.ndarray]], size: tuple[int, int], dst: Path) -> bool:
    """Write the tracked frames, each lined up with the reference (paper moves and zooms undone).
    Returns whether anything had to be lined up."""
    w, h = size
    moved = sum(_moved(H, size) > 0.01 for _, H in track) > 0.1 * len(track)   # really moved, not noise
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                          "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-crf", "14", "-preset", "fast",
                          "-pix_fmt", "yuv420p", str(dst)], stdin=subprocess.PIPE)
    for i, H in track:
        img = store[i]
        if moved and _moved(H, size) > 0.01:
            img = cv2.warpPerspective(img, H, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        p.stdin.write(np.ascontiguousarray(img).tobytes())
    p.stdin.close()
    p.wait()
    return moved


def fill_track(store: "FrameStore", track: list[tuple[int, np.ndarray]], cuts: list[int], lo: int, hi: int,
               max_gap: int = 90, log=print) -> list[tuple[int, np.ndarray]]:
    """Frames recognition missed but that belong with the page: gaps between recognised frames
    (a hand over the page) and, before the first one, the pencil stage - frames with no more ink
    than the page had then (an old finished page flipped to has more). Never across a page change,
    never after the last recognised frame (where the flipping usually is)."""
    if not track:
        return track
    H = dict(track)
    edges = [lo] + sorted(c for c in cuts if lo < c <= hi) + [hi + 1]

    from .analyze import _skin

    def ink(i: int) -> np.ndarray:
        small = cv2.resize(store[i], (128, 228), interpolation=cv2.INTER_AREA)
        g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
        paper = cv2.GaussianBlur(cv2.dilate(g, np.ones((9, 9), np.uint8)), (0, 0), 4)
        hand = cv2.dilate(_skin(small).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0   # not ink
        return ((paper - g) > 75) & ~hand

    def earlier_state(i: int, ref: np.ndarray) -> bool:
        """Frame i could be this page before ref: no more ink, and its ink where ref has ink."""
        k = ink(i)
        if k.mean() > ref.mean() * 1.1 + 0.002:
            return False
        if k.sum() < 0.002 * k.size:                         # barely anything drawn yet
            return True
        grown = cv2.dilate(ref.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        return float((k & ~grown).sum()) / float(k.sum()) < 0.3

    added = 0
    for a, b in zip(edges, edges[1:]):
        rec = sorted(i for i in H if a <= i < b)
        if not rec:
            continue
        for x, y in zip(rec, rec[1:]):                       # gaps inside the stretch
            if 1 < y - x <= max_gap:
                for i in range(x + 1, y):
                    H[i] = H[x]
                    added += 1
        ref = ink(rec[0])                                     # the pencil stage before it
        fails = 0
        for i in range(rec[0] - 1, a - 1, -1):
            if not earlier_state(i, ref):                     # the pen or a shadow can fool one
                fails += 1                                     # frame; a run of them is a page
                if fails > 8:
                    break
                continue
            fails = 0
            H[i] = H[rec[0]]
            added += 1
    if added:
        log(f"page recognition: +{added} frames (hand in the way, pencil stage)")
    return sorted(H.items())
