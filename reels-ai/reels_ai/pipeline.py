"""Analyse one timelapse and render variants (used by the CLI and the bot)."""
from __future__ import annotations

import html
import json
import subprocess
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .analyze import analyze, analyze_spaced, find_subjects
from .media import (FPS, FrameStore, crop_box, crop_vertical, longest_stretch, normalize, page_changes,
                    same_page, trim)
from .variants import CUT_SECONDS, RECIPES, build, pick_moments, render

FALLBACK_ORDER = ["one_take", "reveal_first", "detail_zoom", "ink_only", "speedrun", "before_after"]


def swap_out(names: list[str], style: str, pencil: bool = True) -> list[str]:
    """Replace a style that can't be made for this drawing with one it isn't getting yet, so every
    drawing still gets its full set of versions (one per platform)."""
    spare = [n for n in FALLBACK_ORDER if n not in names and (pencil or n != "before_after")]
    out = [n for n in names if n != style]
    return out + spare[:1] if style in names else out


RawInput = Callable[[float], list[str]]     # seek time (s) -> ffmpeg input args for the original


def process_cuts_clips(a, frame_filter: str, raw_input: RawInput, src_time: Callable[[float], float],
                     speedup: float, total: float, out_dir: Path, size: tuple[int, int]) -> dict:
    """Cut short real-speed moments straight from the original recording (dense frames: the pen
    actually glides), framed like the timelapse. Long sessions play at 2x, short clips at 1x."""
    speed = 2.0 if total > 180 else 1.0
    raw_len = CUT_SECONDS * speed
    w, h = size
    clips = {}
    for k, f in enumerate(pick_moments(a)):
        t = src_time(f / FPS * speedup)
        t0 = min(max(0.0, t - raw_len * 0.6), max(0.0, total - raw_len))
        dst = out_dir / f"cut{k}.mp4"
        cmd = (["ffmpeg", "-v", "error", "-y"] + raw_input(t0) +
               ["-frames:v", str(int(round(CUT_SECONDS * FPS))), "-an", "-vf",
                f"setpts=PTS/{speed},fps={FPS},{frame_filter}scale={w}:{h}:flags=lanczos,setsar=1",
                "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(dst)])
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            print(f"warn: real-speed cut at {t0:.1f}s failed: {(res.stderr or '').strip()[-200:]}")
            continue
        try:
            store = FrameStore(dst)
        except RuntimeError:
            continue
        if len(store) >= FPS // 2:
            clips[f"cut{k}"] = store
        dst.unlink(missing_ok=True)
    return clips


def _pixels(path: Path) -> int:
    """Width x height of a video (the working copy's source stands in for the original)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip().split(",")
    try:
        return int(out[0]) * int(out[1])
    except (ValueError, IndexError):
        return 3840 * 2160


def _thumb(path: Path, at: float, dst: Path) -> None:
    cap = cv2.VideoCapture(str(path))
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * at))
    ok, f = cap.read()
    cap.release()
    if ok:
        cv2.imwrite(str(dst), cv2.resize(f, (270, 480), interpolation=cv2.INTER_AREA))


def _contact_sheet(path: Path, dst: Path, cols: int = 8) -> None:
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    tiles = []
    for k in range(cols):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(k * (n - 1) / (cols - 1)))
        ok, f = cap.read()
        tiles.append(cv2.resize(f, (180, 320)) if ok else np.zeros((320, 180, 3), np.uint8))
    cap.release()
    cv2.imwrite(str(dst), np.hstack(tiles))


def _review_page(out_dir: Path, source: str, results: list[dict]) -> Path:
    cards = []
    for r in results:
        cards.append(f"""
<article>
  <video src="{html.escape(r['file'])}" controls playsinline loop preload="metadata"></video>
  <h2>{html.escape(r['name'].replace('_', ' '))} <small>{r['seconds']:.1f}s</small></h2>
  <p class="desc">{html.escape(r['description'])}</p>
  <p><b>Hook:</b> {html.escape(r['hook'])}</p>
  <details><summary>Caption</summary><pre>{html.escape(r['caption'])}</pre></details>
</article>""")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reel variants</title>
<style>
:root{{--bg:#f6f5f2;--fg:#151515;--muted:#666;--card:#fff;--line:#e3e1dc}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--fg:#eee;--muted:#9a9a9a;--card:#1c1c1c;--line:#2c2c2c}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;padding:16px}}
h1{{font-size:20px;margin:0 0 4px}} .src{{color:var(--muted);margin:0 0 16px}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
article{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px}}
video{{width:100%;aspect-ratio:9/16;background:#000;border-radius:8px}}
h2{{font-size:16px;margin:10px 0 4px;text-transform:capitalize}} small{{color:var(--muted);font-weight:400}}
.desc{{color:var(--muted);margin:0 0 8px}} pre{{white-space:pre-wrap;font:13px/1.4 inherit}}
</style></head><body>
<h1>Reel variants</h1><p class="src">From {html.escape(source)}</p>
<main>{''.join(cards)}</main></body></html>"""
    p = out_dir / "review.html"
    p.write_text(page)
    return p


DENSE_DT = 0.2            # aim for a frame every 0.2 s of real drawing on the page that's used
SPARSE_DT = 0.3           # re-read the page when the working copy is sparser than this
DENSE_MAX = 5400          # frames on one page
DENSE_PIXELS = 2700 * 2160 * 3840      # RAM: no more pixels than a 4K working copy already holds
DECODE_BUDGET = 15 * 60 * 3840 * 2160  # seconds x pixels of original to decode (~15 min of 4K)


def densify(raw: RawInput, t0: float, t1: float, size: tuple[int, int], src_px: int, have: int,
            dst: Path) -> FrameStore | None:
    """Re-read real time t0..t1 of the original recording at ~DENSE_DT, at the working size.

    Long recordings are first read sparsely (about a frame a second) to stay fast. Squeezing a
    page into a Short then jumps the hand a second of drawing per frame: stop-motion. Dense
    frames let each Short frame blend the moments in between - the hand smears and glides like a
    real timelapse. Returns None when it wouldn't add frames or would be too slow to decode."""
    w, h = size
    span = t1 - t0
    n = int(min(DENSE_MAX, span / DENSE_DT, DENSE_PIXELS / (w * h)))
    if span <= 0 or n < have * 1.3:
        return None
    if span * src_px > DECODE_BUDGET:
        print(f"page is {span / 60:.0f} min of {src_px / 1e6:.1f} MP video: too long to re-read densely")
        return None
    cmd = (["ffmpeg", "-v", "error", "-y"] + raw(t0) +
           ["-t", f"{span:.3f}", "-an", "-vf",
            f"fps={n / span:.5f},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h},setsar=1,setpts=N/{FPS}/TB",
            "-r", str(FPS), "-c:v", "libx264", "-crf", "14", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(dst)])
    t = time.time()
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        print(f"warn: dense re-read failed: {(res.stderr or '').strip()[-200:]}")
        return None
    try:
        store = FrameStore(dst)
    except RuntimeError:
        return None
    if len(store) < have * 1.3 or (store.width, store.height) != (w, h):
        return None
    print(f"re-read the page densely: {have} -> {len(store)} frames "
          f"({span / len(store):.2f} s apart, {time.time() - t:.0f}s)")
    return store


def _one_page(src: Path, out_dir: Path, max_width: int) -> tuple[Path, FrameStore, dict, int]:
    """Working copy of the recording, cut to the one page that was drawn on most."""
    work = out_dir / "work.mp4"
    norm = normalize(src, work, keep_wide=True, max_wide_h=max(1440, max_width * 16 // 9), max_w=max_width)
    print(f"normalized {src.name} -> {norm['width']}x{norm['height']} (x{norm['speedup']:.1f} speed)")
    store = FrameStore(work)
    # several pages in one recording (a livestream flipping between pages): keep the longest
    # stretch on one page, so frames from different pages are never blended together
    offset = 0
    cuts = page_changes(store)
    first, last = longest_stretch(len(store), cuts) if cuts else (0, len(store) - 1)
    if cuts:
        print(f"{len(cuts)} page change(s); using frames {first}-{last} of {len(store)} (one page)")
    # a flip the hands hid from page_changes (end of a livestream): keep only the same page
    first, last = same_page(store, first, last)
    if (first, last) != (0, len(store) - 1):
        part = out_dir / "work_page.mp4"
        trim(work, part, first, last)
        work.unlink(missing_ok=True)
        part.rename(work)
        store = FrameStore(work)
        offset = first
        norm["page_changes"] = cuts
    return work, store, norm, offset


def find_parts(src: Path, out_root: Path, max_width: int = 1080, raw_duration: float | None = None) -> list[dict]:
    """The separate drawings on the page of a recording, as parts make_variants can render one
    by one: [{"t0", "t1": recording seconds, "box": (x0, y0, x1, y1) fractions, "label"}].
    [] when the page holds one drawing."""
    src = Path(src).expanduser().resolve()
    out_dir = Path(out_root).resolve() / (src.stem + "_parts")
    out_dir.mkdir(parents=True, exist_ok=True)
    work, store, norm, offset = _one_page(src, out_dir, max_width)
    subjects = find_subjects(store)
    work.unlink(missing_ok=True)
    if len(subjects) < 2:
        return []
    scale = (raw_duration or norm["source_duration"]) / max(norm["source_duration"], 1e-6)
    dt = norm["speedup"] * scale / FPS
    parts = [{"t0": round((offset + s["frames"][0]) * dt, 2), "t1": round((offset + s["frames"][1] + 1) * dt, 2),
              "box": list(s["box"]), "label": f"{k + 1} of {len(subjects)}"} for k, s in enumerate(subjects)]
    print(f"{len(parts)} separate drawings on this page: " +
          ", ".join(f"{p['label']} at {p['t0'] / 60:.1f}-{p['t1'] / 60:.1f} min" for p in parts))
    return parts


def make_variants(src: Path, out_root: Path, names: list[str] | None = None, seed: int = 1,
                  subject: str | None = None, audio: Path | None = None, sheets: bool = False,
                  keep_work: bool = False, review_page: bool = True,
                  copy: dict | None = None, max_width: int = 1080, raw_input: RawInput | None = None,
                  raw_duration: float | None = None, hints: dict | None = None,
                  proxy: bool = False, part: dict | None = None) -> tuple[Path, list[dict]]:
    """Analyse `src` and render the requested variants. Returns (output dir, results).

    raw_input / raw_duration: where the original recording is when `src` is only a sampled
    proxy of it (Drive). Without them `src` itself is the original - unless `proxy`: then the
    real time between frames is unknown, and the hand isn't motion-blurred (it could ghost).
    hints: from the file name ('Sasuke - ink only - continued'): pencil True/False, continued.
    part: one drawing of several on the page, from find_parts(): only its time, framed on it."""
    hints = hints or {}
    how = {"continued": bool(hints.get("continued")), "pencil": hints.get("pencil")}
    src = Path(src).expanduser().resolve()
    out_dir = Path(out_root).resolve() / src.stem
    (out_dir / "variants").mkdir(parents=True, exist_ok=True)
    (out_dir / "thumbs").mkdir(exist_ok=True)

    t0 = time.time()
    work, store, norm, offset = _one_page(src, out_dir, max_width)
    # real recording time of a working-copy frame
    total = raw_duration or norm["source_duration"]
    scale = total / max(norm["source_duration"], 1e-6)
    raw = raw_input or (lambda t0: ["-ss", f"{t0:.3f}", "-i", str(src)])
    stride = 1
    t_first, real_dt = offset / FPS * norm["speedup"] * scale, norm["speedup"] * scale / FPS
    if part:
        # one drawing of several on the page: only the time it was being drawn ...
        f0 = int(max(0, min(len(store) - 2, (part["t0"] - t_first) / real_dt)))
        f1 = int(max(f0 + 1, min(len(store) - 1, (part["t1"] - t_first) / real_dt)))
        print(f"drawing {part.get('label', '')}: frames {f0}-{f1} of the page")
        if (f0, f1) != (0, len(store) - 1):
            cut = out_dir / "work_part.mp4"
            trim(work, cut, f0, f1)
            work.unlink(missing_ok=True)
            cut.rename(work)
            store = FrameStore(work)
            t_first += f0 * real_dt
            norm["part_frames"] = [f0, f1]
    if real_dt > SPARSE_DT and not (proxy and raw_input is None):
        dense = densify(raw, t_first, t_first + len(store) * real_dt, (store.width, store.height),
                        _pixels(src), len(store), out_dir / "work_dense.mp4")
        if dense is not None:
            stride = max(1, round(len(dense) / len(store)))   # analyse at the old spacing
            store, real_dt = dense, (len(store) * real_dt) / len(dense)
            work.unlink(missing_ok=True)
            (out_dir / "work_dense.mp4").rename(work)
            norm["dense_frames"] = len(store)
    if proxy and raw_input is None:
        real_dt = None
    was_wide = store.width / store.height > 9 / 16 + 0.02
    part_filter = ""
    if part:
        # ... and framed on that drawing
        boxed = out_dir / "work_box.mp4"
        cb = crop_box(work, boxed, tuple(part["box"]), max_w=max_width)
        work.unlink(missing_ok=True)
        boxed.rename(work)
        store = FrameStore(work)
        part_filter = cb["filter"]
        norm.update(width=store.width, height=store.height, part_box=list(part["box"]))
    store.real_dt = real_dt
    norm["real_seconds_per_frame"] = real_dt and round(real_dt, 3)
    a = analyze_spaced(store, stride, **how)
    wide = store.width / store.height > 9 / 16 + 0.02
    frame_filter = ""
    if not was_wide:
        frame_filter = "crop=iw:'min(ih,trunc(iw*16/9/2)*2)',"          # taller than 9:16: centre
    frame_filter += part_filter
    if wide:
        # Landscape recording: cut a vertical frame around this clip's drawing, then re-analyse.
        vert = out_dir / "work_vertical.mp4"
        crop = crop_vertical(work, vert, a.work_center[0], out_w=max_width, span=a.work_span)
        frame_filter = crop["filter"]
        print(f"landscape -> vertical crop at x={crop['crop_x']} ({crop['crop_w']}px wide"
              f"{', whole width of the new work on a blurred backdrop' if crop['letterbox'] else ''})")
        work.unlink(missing_ok=True)
        vert.rename(work)
        store = FrameStore(work)
        store.real_dt = real_dt
        a = analyze_spaced(store, stride, **how)
        norm.update(width=store.width, height=store.height, landscape_crop=crop)
    (out_dir / "analysis.json").write_text(json.dumps({**norm, **a.to_dict()}, indent=2))
    print("beats (s):", a.to_dict()["seconds"])

    names = list(names or RECIPES)
    if not a.has_pencil and "before_after" in names:
        # Straight-to-ink session: no sketch to compare, so make another style instead.
        names = swap_out(names, "before_after", pencil=False)
        spare = names[-1:]
        print("no pencil stage: before_after swapped for", spare[:1] or "nothing")
    clips = None
    if "process_cuts" in names:
        # working-copy seconds -> frame -> real recording time (one page, maybe re-read densely)
        clips = process_cuts_clips(a, frame_filter, raw,
                                   lambda s: t_first + s * FPS / norm["speedup"] * (real_dt or norm["speedup"] * scale / FPS),
                                   norm["speedup"], total,
                                 out_dir, (store.width, store.height))
        if len(clips) < 4:
            names = swap_out(names, "process_cuts", a.has_pencil)
            print(f"warn: only {len(clips)} real-speed moments; made {names[-1]} instead of process_cuts")
    plans = build(a, names, seed, subject, copy, clips)
    results = []
    for plan in plans:
        dst = out_dir / "variants" / f"{plan.name}.mp4"
        render(plan, store, dst, audio, a.focus, a.usable)
        _thumb(dst, 0.02, out_dir / "thumbs" / f"{plan.name}.jpg")
        if sheets:
            _contact_sheet(dst, out_dir / "thumbs" / f"{plan.name}_sheet.jpg")
        results.append({"name": plan.name, "file": f"variants/{plan.name}.mp4",
                        "seconds": plan.seconds, "description": plan.description,
                        "grade": plan.grade, "hook": plan.hook, "caption": plan.caption})
        print(f"  rendered {plan.name:13s} {plan.seconds:5.1f}s  hook: {plan.hook!r}")
    (out_dir / "variants.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    if review_page:
        _review_page(out_dir, src.name, results)
    if not keep_work:
        work.unlink(missing_ok=True)
    print(f"done in {time.time() - t0:.0f}s -> {out_dir}")
    return out_dir, results
