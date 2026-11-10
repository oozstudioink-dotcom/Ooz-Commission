"""LongDraw builder: several raw sessions -> one 20-30 min sped-up 4K YouTube video.

Per project (Drive > Ooz Reels > Longform > <project>/):
  1. scan each session cheaply (keyframes, small) to find dead time: hand off the page and
     nothing changing for a while (setup, breaks) -> cut
  2. choose one speed so the kept footage lands inside target_minutes
  3. render each session straight from Drive at full quality:
       portrait  -> [left panel | drawing | right panel] at 16:9
       landscape -> full frame
  4. glitch-in title intro, concat, beat tape from Drive > Beats (loudness-matched,
     crossfaded, looped), chapters, LongDraw thumbnail
  5. upload to Drive > Done > Longform > "EP 03 - <project>" (+ YouTube when enabled)
"""
from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import panels
from .analyze import _skin
from .media import FPS
from .thumbnail import CREAM, RED, _anton, clearest, crop_art, make_thumbnail, youtube_jpeg

SIZES = {"4k": (3840, 2160), "1080p": (1920, 1080)}
ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-profile:v", "high", "-video_track_timescale", "15360"]


@dataclass
class Session:
    src: str                         # file path or URL
    name: str
    title: str
    duration: float
    headers: str | None = None       # e.g. "Authorization: Bearer ...\r\n" for Drive
    portrait: bool = True
    keep: list[tuple[float, float]] = field(default_factory=list)

    @property
    def kept(self) -> float:
        return sum(b - a for a, b in self.keep)

    def hdrs(self) -> list[str]:
        return ["-headers", self.headers] if self.headers else []

    def inp(self) -> list[str]:
        return self.hdrs() + ["-i", self.src]


def _run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[-400:]}")


def probe_duration(s: Session) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", *s.hdrs(),
                          "-show_entries", "format=duration", "-of", "csv=p=0", s.src],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


# ---------------------------------------------------------------- 1. dead time
def scan(s: Session, work: Path, idle_s: float = 8.0, pad_s: float = 1.0) -> Session:
    """Find the parts worth keeping from a small keyframe/sampled proxy of the session."""
    proxy = work / f"scan_{abs(hash(s.src)) % 10**8}.mp4"

    def read(keyframes: bool) -> list:
        cmd = ["ffmpeg", "-v", "error", "-y", *s.hdrs()]
        cmd += (["-skip_frame", "nokey"] if keyframes else []) + ["-i", s.src]
        rate = max(0.5, min(1.0, 900 / max(s.duration, 1))) if s.duration >= 15 * 60 else \
            max(1.0, 900 / max(s.duration, 1))
        vf = "scale=320:-2" if keyframes else f"fps={rate:.3f},scale=320:-2"
        _run(cmd + ["-an", "-vf", f"{vf},setpts=N/30/TB", "-r", "30", "-c:v", "libx264", "-crf", "28",
                    "-preset", "ultrafast", str(proxy)])
        cap = cv2.VideoCapture(str(proxy))
        out = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            out.append(f)
        cap.release()
        proxy.unlink(missing_ok=True)
        return out

    frames = read(keyframes=s.duration >= 15 * 60)
    if s.duration >= 15 * 60 and len(frames) < s.duration / 4:
        # few keyframes (e.g. TikTok live downloads): look at the whole session instead
        frames = read(keyframes=False)
    if not frames:
        raise RuntimeError(f"couldn't read {s.name}")
    h, w = frames[0].shape[:2]
    s.portrait = h > w
    step = s.duration / len(frames)
    idle = []
    prev = None
    for f in frames:
        hand = _skin(f)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.int16)
        mask = ~cv2.dilate(hand.astype(np.uint8), np.ones((9, 9), np.uint8)).astype(bool)
        if prev is None:
            diff = 0.0                                   # first frame: judged on the hand alone
        else:
            diff = float(np.abs(g - prev)[mask].mean()) if mask.any() else 99.0
        idle.append(hand.mean() < 0.01 and diff < 1.5)
        prev = g
    # runs of idle frames longer than idle_s are cut (keeping pad_s either side)
    keep, i, n = [], 0, len(idle)
    seg_start = 0.0
    while i < n:
        if idle[i]:
            j = i
            while j < n and idle[j]:
                j += 1
            if (j - i) * step >= idle_s:
                # pad only where footage continues; a dead start/end is cut completely
                a = i * step + (pad_s if i > 0 else 0.0)
                b = j * step - (pad_s if j < n else 0.0)
                if a > seg_start:
                    keep.append((seg_start, a))
                seg_start = b
            i = j
        else:
            i += 1
    if s.duration > seg_start:
        keep.append((seg_start, s.duration))
    s.keep = [(round(a, 2), round(b, 2)) for a, b in keep if b - a > 0.5] or [(0.0, s.duration)]
    return s


def choose_speed(kept_s: float, lo_min: float, hi_min: float) -> float:
    if kept_s <= hi_min * 60:
        return 1.0                                   # already short enough; never slow down
    return kept_s / (((lo_min + hi_min) / 2) * 60)   # aim for the middle of the range


# ---------------------------------------------------------------- 2. render
def _select(keep: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t\\,{a:.2f}\\,{b:.2f})" for a, b in keep)


def panel_widths(W: int, H: int) -> tuple[int, int, int]:
    """Centre video, left panel and right panel widths - all even, as the encoder needs."""
    vw = int(round(H * 9 / 16)) // 2 * 2
    pw = (W - vw) // 4 * 2
    return vw, pw, W - vw - pw


def render_session(s: Session, speed: float, out: Path, size: tuple[int, int], cards: list,
                   clock_s: float, work: Path, intro_panels: bool, rotate_s: float = 300.0) -> float:
    W, H = size
    out_s = s.kept / speed
    base = f"[0:v]select='{_select(s.keep)}',setpts=N/({FPS * speed:.5f})/TB,fps={FPS}"
    if s.portrait:
        vw, pw, rw = panel_widths(W, H)
        left = panels.render_left(work / "left.mp4", pw, H, out_s + 1, intro=intro_panels)
        right = panels.render_right(work / "right.mp4", rw, H, out_s + 1, cards, start_s=clock_s,
                                     rotate_s=rotate_s)
        fc = (f"{base},scale={vw}:{H}:flags=lanczos,setsar=1[c];"
              f"[1:v][c][2:v]hstack=inputs=3:shortest=1[v]")
        cmd = ["ffmpeg", "-v", "error", "-y", *s.inp(), "-i", str(left), "-i", str(right),
               "-filter_complex", fc, "-map", "[v]"]
    else:
        fc = (f"{base},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1[v]")
        cmd = ["ffmpeg", "-v", "error", "-y", *s.inp(), "-filter_complex", fc, "-map", "[v]"]
    _run(cmd + ["-an", *ENC, str(out)])
    for p in ("left.mp4", "right.mp4"):
        (work / p).unlink(missing_ok=True)
    return _duration(out)


def _duration(p: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(p)], capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def render_intro(out: Path, size: tuple[int, int], episode: int, project: str, seconds: float = 3.0) -> float:
    from PIL import Image, ImageDraw
    from .fx import font
    W, H = size
    img = Image.new("RGB", (W, H), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((W // 2, int(H * 0.30)), "LONG", font=_anton(int(H * 0.26)), fill=CREAM, anchor="mm")
    d.text((W // 2, int(H * 0.56)), "DRAW", font=_anton(int(H * 0.26)), fill=RED, anchor="mm")
    d.text((W // 2, int(H * 0.77)), " ".join(f"EP. {episode:02d}"), font=font("black", int(H * 0.05)),
           fill=CREAM, anchor="mm")
    if project:
        d.text((W // 2, int(H * 0.85)), " ".join(project.upper()), font=font("bold", int(H * 0.03)),
               fill=(150, 146, 138), anchor="mm")
    card = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    frames = panels.glitch_frames(card, seed=episode)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                          "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-an", *ENC, str(out)],
                         stdin=subprocess.PIPE)
    n = int(seconds * FPS)
    for k in range(n):
        p.stdin.write((frames[k] if k < len(frames) else card).tobytes())
    p.stdin.close()
    if p.wait():
        raise RuntimeError("intro encode failed")
    return n / FPS


# ---------------------------------------------------------------- 3. audio
def track_name(p: Path) -> str:
    """'03_Night_Walk-Artist.mp3' -> 'Night Walk-Artist' (for the description's music credits)."""
    stem = re.sub(r"^\d+[\s._-]+", "", Path(p).stem)
    return re.sub(r"\s+", " ", stem.replace("_", " ")).strip() or Path(p).stem


def beat_tape(beats: list[Path], seconds: float, out: Path, xfade: float = 3.0, seed: int = 0,
              played: list | None = None) -> Path:
    """Shuffle, loudness-match (-16 LUFS), crossfade and loop the beats to cover `seconds`.
    `played` (optional list) receives the tracks that are actually heard, in order."""
    if not beats:
        _run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
              "-t", f"{seconds:.2f}", "-c:a", "aac", "-b:a", "192k", str(out)])
        return out
    durs = {b: _duration(b) for b in beats}
    rng = random.Random(seed)
    order, total = [], 0.0
    while total < seconds + xfade:
        batch = list(beats)
        rng.shuffle(batch)
        if order and len(batch) > 1 and batch[0] == order[-1]:   # no track twice in a row
            batch.append(batch.pop(0))
        for b in batch:
            order.append(b)
            total += max(durs[b] - xfade, 1.0)
            if total >= seconds + xfade:
                break
    if played is not None:
        start = 0.0
        for b in order:
            if start < seconds:
                played.append(b)
            start += max(durs[b] - xfade, 1.0)
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for b in order:
        cmd += ["-i", str(b)]
    parts = [f"[{i}:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo[a{i}]"
             for i in range(len(order))]
    chain = "a0"
    for i in range(1, len(order)):
        parts.append(f"[{chain}][a{i}]acrossfade=d={xfade}:c1=tri:c2=tri[x{i}]")
        chain = f"x{i}"
    fade_at = max(0.0, seconds - 4)
    parts.append(f"[{chain}]atrim=0:{seconds:.2f},afade=t=in:d=1.5,afade=t=out:st={fade_at:.2f}:d=4[out]")
    _run(cmd + ["-filter_complex", ";".join(parts), "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(out)])
    return out


# ---------------------------------------------------------------- 4. assemble
def fmt_ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build(sessions: list[Session], out_dir: Path, project: str, episode: int, cfg: dict,
          beats: list[Path], advert: Path | None, panel_cfg: dict, art: Path | None = None,
          log=print) -> dict:
    """Returns {"video", "thumbnail", "thumbnail_yt", "chapters", "seconds", "speed", ...}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    size = SIZES.get(cfg.get("resolution", "4k"), SIZES["4k"])
    lo, hi = cfg.get("target_minutes", [20, 30])
    work = Path(tempfile.mkdtemp(dir=out_dir))
    try:
        for s in sessions:
            s.duration = s.duration or probe_duration(s)
            scan(s, work)
            log(f"{s.name}: {s.duration / 60:.1f} min, keeping {s.kept / 60:.1f} min "
                f"({'portrait' if s.portrait else 'landscape'})")
        kept = sum(s.kept for s in sessions)
        speed = choose_speed(kept, lo, hi)
        log(f"total kept {kept / 60:.1f} min -> speed {speed:.2f}x -> {kept / speed / 60:.1f} min")

        cards = panels.build_cards(panel_widths(*size)[2], size[1], panel_cfg, advert, work) \
            if any(s.portrait for s in sessions) else []
        parts, chapters, clock = [], [], 0.0
        intro = work / "000_intro.mp4"
        clock += render_intro(intro, size, episode, project)
        parts.append(intro)
        chapters.append((0.0, "Intro"))
        for i, s in enumerate(sessions):
            part = work / f"{i + 1:03d}.mp4"
            chapters.append((clock, s.title))
            clock += render_session(s, speed, part, size, cards, clock, work, intro_panels=(i == 0),
                                    rotate_s=float(panel_cfg.get("rotate_minutes", 5)) * 60)
            parts.append(part)
            log(f"rendered {s.name} ({clock / 60:.1f} min so far)")

        listing = work / "parts.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        silent = work / "video.mp4"
        _run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
              str(silent)])
        for p in parts:
            p.unlink(missing_ok=True)
        played: list[Path] = []
        audio = beat_tape(beats, clock, work / "beats.m4a", seed=episode, played=played)
        tracks = []
        for b in played:                                  # unique, in first-heard order
            name = track_name(b)
            if name not in tracks:
                tracks.append(name)
        video = out_dir / f"LongDraw EP {episode:02d} - {project}.mp4"
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(silent), "-i", str(audio), "-map", "0:v", "-map", "1:a",
              "-c", "copy", "-shortest", "-movflags", "+faststart", str(video)])

        # thumbnail: provided art, else a crop of the finished page from the last session
        if art and art.exists():                         # your photo: cropped to the drawing too
            art_img = crop_art(cv2.imread(str(art)))
        else:                                             # the clearest of the last 20 s (least hand)
            s = sessions[-1]
            end = s.keep[-1][1]
            frames = []
            for k in range(8):
                f = work / f"last{k}.png"
                try:
                    _run(["ffmpeg", "-v", "error", "-y", *s.hdrs(),
                          "-ss", f"{max(0.0, end - 0.5 - k * 2.5):.2f}", "-i", s.src, "-frames:v", "1", str(f)])
                    img = cv2.imread(str(f))
                    if img is not None:
                        frames.append(img)
                except Exception as e:  # noqa: BLE001
                    print(f"warn: thumbnail frame: {e}")
            art_img = crop_art(clearest(frames))
        thumb = make_thumbnail(art_img, episode, out_dir / f"LongDraw EP {episode:02d} - thumbnail.png")
        thumb_yt = youtube_jpeg(thumb, out_dir / f"LongDraw EP {episode:02d} - thumbnail.jpg")
        if len(chapters) < 3 or any(b[0] - a[0] < 10 for a, b in zip(chapters, chapters[1:])):
            chapters = [c for c in chapters if c[0] == 0.0 or c[1] != "Intro"]
        chapter_text = "\n".join(f"{fmt_ts(t)} {name}" for t, name in chapters)
        (out_dir / "chapters.txt").write_text(chapter_text + "\n")
        return {"video": video, "thumbnail": thumb, "thumbnail_yt": thumb_yt, "chapters": chapter_text,
                "seconds": clock, "speed": speed, "kept": kept,
                "raw": sum(s.duration for s in sessions), "tracks": tracks}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def youtube_text(project: str, episode: int, chapters: str, panel_cfg: dict, speed: float,
                 raw_s: float, tracks: list[str] | None = None,
                 music_source: str = "YouTube Audio Library") -> tuple[str, str, list[str]]:
    title = f"LongDraw EP. {episode:02d} | {project} | Manga Inking Timelapse"[:100]
    socials = "\n".join(f"{p}: {h}" for p, h in panel_cfg.get("socials", []))
    link = panel_cfg.get("link", "")
    music = ""
    if tracks:
        src = f" ({music_source})" if music_source else ""
        music = f"\n\nMusic{src}:\n" + "\n".join(f"\u2022 {t}" for t in tracks)
    tags_line = "#manga #mangaart #inking #timelapse #drawing #traditionalart #oozstudioink"
    head = (f"{project}: {raw_s / 3600:.1f} hours of pencils and ink, sped up {speed:.1f}x. "
            f"Hand-drawn manga page by Ooz Studio Ink \u270d\U0001f3fe\n\n"
            f"Commissions are open: {link}\n\nChapters\n{chapters}\n\n{socials}")
    room = 4900 - len(head) - len(tags_line) - 4               # YouTube limit: 5000 characters
    if len(music) > room:
        music = music[:max(0, room - 2)].rsplit("\n", 1)[0] + "\n\u2026"
    desc = f"{head}{music}\n\n{tags_line}"
    tags = ["manga", "manga art", "inking", "timelapse", "drawing", "traditional art", "longdraw",
            "ooz studio ink", project]
    return title, desc, tags


def subject_title(name: str, i: int) -> str:
    """Chapter name for a session file: 'Part 3' for numbered files ('3.MOV', 'Part 3'),
    'Sasuke (Part 3)' for 'Sasuke 3.MOV', its own name otherwise, 'Session N' for camera names."""
    from .drive import CAMERA, _stem, parse_name, part_number
    if CAMERA.match(_stem(name)):
        return f"Session {i + 1}"
    subject, n = parse_name(name)[0], part_number(name)
    if subject:
        return f"{subject} (Part {n})" if n is not None else subject
    return f"Part {n}" if n is not None else f"Session {i + 1}"


def episode_from_names(names: list[str], floor: int) -> int:
    nums = [int(m.group(1)) for n in names if (m := re.search(r"EP\.?\s*(\d+)", n, re.I))]
    return max([floor] + [n + 1 for n in nums])


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps({k: str(v) for k, v in data.items()}, indent=2))
