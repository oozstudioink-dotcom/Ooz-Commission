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
        return (paper - img) > 40

    scores = np.zeros(n)
    for i in range(win, n - win, 2):
        covered = cv2.dilate(hand[i - win:i + win].any(axis=0).astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
        before = ink(np.median(gray[i - win:i], axis=0)) & ~covered
        after = ink(np.median(gray[i:i + win], axis=0)) & ~covered
        if before.sum() < min_ink or after.sum() < min_ink:
            continue
        lost = float((before & ~(cv2.dilate(after.astype(np.uint8), grow) > 0)).sum()) / float(before.sum())
        new = float((after & ~(cv2.dilate(before.astype(np.uint8), grow) > 0)).sum()) / float(after.sum())
        scores[i] = scores[i + 1] = min(lost, new)
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
