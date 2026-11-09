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
