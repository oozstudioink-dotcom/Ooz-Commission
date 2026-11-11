"""Analyse one timelapse and render variants (used by the CLI and the bot)."""
from __future__ import annotations

import html
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .analyze import analyze, analyze_spaced, find_subjects
from .media import (FPS, FrameStore, PageMatcher, choose_page, contact_sheet, crop_box, crop_vertical,
                    drawn_stretch, fill_track, ink_added, steady_growth, normalize, page_changes, same_page, track_page, trim,
                    write_page)
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
           ["-t", f"{span:.3f}", "-frames:v", str(n), "-an", "-vf",
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


class Page:
    """The working copy cut to the page you drew on: which proxy frames it holds, and how to
    recognise that page in other frames (the dense re-read)."""

    def __init__(self, work: Path, store: FrameStore, norm: dict, src_idx: list[int], dt: float,
                 matcher: PageMatcher | None = None, ref: int | None = None, others: list[int] | None = None):
        self.work, self.store, self.norm, self.src_idx, self.dt = work, store, norm, src_idx, dt
        self.matcher, self.ref, self.others = matcher, ref, others or []
        self.stretches: list = []                          # (ink added, first, last) between flips
        self.cuts: list = []                               # frames where the page changes

    def time(self, f: int) -> float:
        """Recording seconds of working-copy frame f."""
        return self.src_idx[int(min(max(f, 0), len(self.src_idx) - 1))] * self.dt


_SRC: dict = {}            # the recording being worked on: its working copy and what was found in it
STORE_CACHE_BYTES = 1.5e9  # keep its frames in memory between drawings only when they are small


def _forget_source() -> None:
    c = _SRC.pop("c", None)
    if c:
        shutil.rmtree(c["dir"], ignore_errors=True)


def _source(src: Path, max_width: int) -> dict:
    """The recording, read and measured ONCE however many drawings are made from it: the
    working copy, where the pages change, and where each page was recognised."""
    st = src.stat()
    key = (str(src), st.st_size, st.st_mtime_ns, max_width)
    c = _SRC.get("c")
    if c is None or c["key"] != key:
        _forget_source()
        d = Path(tempfile.mkdtemp(prefix="recording_"))
        full = d / "full.mp4"
        norm = normalize(src, full, keep_wide=True, max_wide_h=max(1440, max_width * 16 // 9), max_w=max_width)
        print(f"normalized {src.name} -> {norm['width']}x{norm['height']} (x{norm['speedup']:.1f} speed)")
        c = _SRC["c"] = {"key": key, "dir": d, "full": full, "norm": norm, "store": None, "pages": {}, "tracks": {}}
    else:
        print(f"{src.name}: already read this run (using what was found)")
    return c


def _frames(c: dict) -> FrameStore:
    if c["store"] is not None:
        return c["store"]
    store = FrameStore(c["full"])
    if sum(len(b) for b in store._frames) <= STORE_CACHE_BYTES:
        c["store"] = store
    return store


def _one_page(src: Path, out_dir: Path, max_width: int, window: list | None = None,
              scale: float = 1.0, ref_time: float | None = None, span: tuple | None = None) -> Page:
    """Working copy of the recording, cut to the one page that was drawn on.

    The page is RECOGNISED in every frame (PageMatcher): frames of other pages, flips and other
    views are left out wherever they are, and small paper or camera moves are lined up.
    window: [start, end] in recording seconds that you chose (/redo <name> 16-34).
    ref_time: the frame (recording seconds) that shows the page/view to use (find_parts).
    A contact sheet of the recording (pages.jpg/pages.json) shows which frames were used.
    Reading the recording, finding its page changes and recognising a page are each done once
    per recording (_source), not once per drawing."""
    c = _source(src, max_width)
    norm = dict(c["norm"])
    store = _frames(c)
    work = out_dir / "work.mp4"
    dt = norm["speedup"] * scale / FPS                        # recording seconds per frame
    n = len(store)
    lo, hi = 0, n - 1
    if window:
        lo = int(max(0, min(n - 2, window[0] / dt)))
        hi = int(max(lo + 1, min(n - 1, window[1] / dt)))
        print(f"your choice: {window[0] / 60:.1f}-{window[1] / 60:.1f} min = frames {lo}-{hi}")
    wkey = (lo, hi, bool(window))
    if wkey not in c["pages"]:
        # where the drawing happened: between page flips, the stretch where ink was added
        cuts = page_changes(store) if not window else []
        stretches: list = []
        first, last = drawn_stretch(store, cuts, scored_out=stretches) if cuts else (lo, hi)
        if cuts:
            print(f"{len(cuts)} page change(s); drawing at frames {first}-{last} of {n}")
        first, last = same_page(store, first, last)
        auto_ref = None
        if (cuts or window) and last - first >= 40:            # several pages (a livestream): recognise
            auto_ref = choose_page(store, first, last)         # the drawn one in every frame
        c["pages"][wkey] = (cuts, stretches, first, last, auto_ref)
    cuts, stretches, first, last, auto_ref = c["pages"][wkey]
    ref = auto_ref if ref_time is None else int(min(max(round(ref_time / dt), lo), hi))
    keep = list(range(first, last + 1))
    matcher, others, moved = None, [], False
    if ref is not None:
        matcher = PageMatcher(store[ref])
        tkey = (ref, lo, hi)
        if tkey not in c["tracks"]:
            seen: list = []
            c["tracks"][tkey] = (fill_track(store, track_page(store, matcher, list(range(lo, hi + 1)), others=seen),
                                            cuts, lo, hi), seen)
        track, others = c["tracks"][tkey][0], list(c["tracks"][tkey][1])
        if len(track) >= max(30, 0.25 * (last - first + 1)):
            keep = [i for i, _ in track]
            moved = write_page(store, track, (store.width, store.height), out_dir / "work_page.mp4")
            norm["page_frames"] = len(keep)
            norm["page_view_ref"] = round(ref * dt, 1)
        else:
            matcher, others = None, []
            if span:                                       # a drawing with known times: use those
                first = int(max(lo, min(hi - 1, span[0] / dt)))
                last = int(max(first + 1, min(hi, span[1] / dt)))
                keep = list(range(first, last + 1))
                print(f"page recognition found too little; using the drawing's own minutes "
                      f"({span[0] / 60:.1f}-{span[1] / 60:.1f})")
            else:
                print("page recognition found too little; using the drawn stretch as is")
    if matcher is None and (first, last) != (0, n - 1):
        trim(c["full"], out_dir / "work_page.mp4", first, last)
    full_store = store
    if keep != list(range(n)):
        if cuts or len(keep) < n:
            contact_sheet(store, out_dir / "pages.jpg", dt, (keep[0], keep[-1]), used=set(keep))
            (out_dir / "pages.json").write_text(json.dumps({"used": [round(keep[0] * dt), round((keep[-1] + 1) * dt)],
                                                            "total": round(n * dt), "frames": len(keep), "of": n}))
        (out_dir / "work_page.mp4").rename(work)
        store = FrameStore(work)
        norm["page_changes"] = cuts
    else:
        shutil.copy(c["full"], work)
        store = FrameStore(work) if c["store"] is None else store
    norm["stabilised"] = moved
    pg = Page(work, store, norm, keep, dt, matcher, ref, others)
    pg.stretches = stretches
    pg.cuts = cuts
    pg.full_store = full_store
    return pg


def find_parts(src: Path, out_root: Path, max_width: int = 1080, raw_duration: float | None = None,
               window: list | None = None, watcher=None) -> list[dict]:
    """The separate drawings in a recording, as parts make_variants renders one by one:
    [{"t0", "t1": recording seconds, "box": (x0, y0, x1, y1) fractions, "ref_t", "label"}].
    Separate drawings on the page you drew on, and the page drawn on from another view (paper
    moved, a close-up). [] when it's one drawing."""
    src = Path(src).expanduser().resolve()
    out_dir = Path(out_root).resolve() / (src.stem + "_parts")
    out_dir.mkdir(parents=True, exist_ok=True)
    from .media import probe
    src_s = probe(src)["duration"]
    pg = _one_page(src, out_dir, max_width, window, (raw_duration or src_s) / max(src_s, 1e-6))
    if watcher is not None:
        # watched (ears + eyes): Claude says what is there, the measurements say exactly where
        try:
            from .watch import parts_from_map
            seen = watcher(pg.full_store, pg.dt)
            (out_dir / "edit_map.json").write_text(json.dumps(seen, indent=1, ensure_ascii=False))
            parts = parts_from_map(seen, pg.full_store, pg.dt, pg.cuts)
            pg.work.unlink(missing_ok=True)
            if len(parts) < 2:
                print("watched: one drawing in this recording")
                return []
            print(f"watched: {len(parts)} separate drawings: " + ", ".join(
                f"{p['label']} {p['title'] or ''} at {p['t0'] / 60:.1f}-{p['t1'] / 60:.1f} min".replace("  ", " ")
                for p in parts))
            return parts
        except Exception as e:  # noqa: BLE001 - watching is a help: fall back to measuring
            print(f"warn: couldn't watch the recording ({str(e)[:200]}); measuring instead")
    ref_t = None if pg.ref is None else round(pg.ref * pg.dt, 2)
    parts = [{"t0": round(pg.time(s["frames"][0]), 2), "t1": round(pg.time(s["frames"][1]) + pg.dt, 2),
              "box": list(s["box"]), "ref_t": ref_t} for s in find_subjects(pg.store)]
    # other stretches you drew in (between flips, ink added) and views of the page: each its own
    # drawing, with its own reference frame
    views = []
    main = set(pg.src_idx)
    other_drawn = [(a, b) for s, a, b in pg.stretches
                   if s >= 0.01 and sum(1 for i in range(a, b + 1) if i in main) < 0.5 * (b - a + 1)]
    if pg.others or other_drawn:
        store = pg.full_store
        for a, b in other_drawn:
            if not steady_growth(store, a, b):               # a flip or a page moved around
                continue
            ref = choose_page(store, a, b)
            if ref is not None:
                views.append({"t0": round(a * pg.dt, 2), "t1": round((b + 1) * pg.dt, 2),
                              "box": [0, 0, 1, 1], "ref_t": round(ref * pg.dt, 2)})
        runs, cur = [], [pg.others[0]] if pg.others else []
        for i in pg.others[1:]:
            if i - cur[-1] <= 4:
                cur.append(i)
            else:
                runs.append(cur)
                cur = [i]
        if cur:
            runs.append(cur)
        for r in runs:
            if len(r) < 40 or ink_added(store, r[0], r[-1]) < 0.003:
                continue
            if any(min(v["t1"], (r[-1] + 1) * pg.dt) - max(v["t0"], r[0] * pg.dt) > 0.5 * len(r) * pg.dt
                   for v in views):                         # already one of the drawn stretches
                continue
            ref = choose_page(store, r[0], r[-1])
            if ref is not None:
                views.append({"t0": round(r[0] * pg.dt, 2), "t1": round((r[-1] + 1) * pg.dt, 2),
                              "box": [0, 0, 1, 1], "ref_t": round(ref * pg.dt, 2)})
    if views and not parts:                                   # the main view is one drawing too
        parts = [{"t0": round(pg.time(0), 2), "t1": round(pg.time(len(pg.src_idx) - 1) + pg.dt, 2),
                  "box": [0, 0, 1, 1], "ref_t": ref_t}]
    parts += views
    pg.work.unlink(missing_ok=True)
    if len(parts) < 2:
        return []
    parts.sort(key=lambda p: p["t0"])
    for k, p in enumerate(parts):
        p["label"] = f"{k + 1} of {len(parts)}"
    print(f"{len(parts)} separate drawings: " +
          ", ".join(f"{p['label']} at {p['t0'] / 60:.1f}-{p['t1'] / 60:.1f} min" for p in parts))
    return parts


def make_variants(src: Path, out_root: Path, names: list[str] | None = None, seed: int = 1,
                  subject: str | None = None, audio: Path | None = None, sheets: bool = False,
                  keep_work: bool = False, review_page: bool = True,
                  copy: dict | None = None, max_width: int = 1080, raw_input: RawInput | None = None,
                  raw_duration: float | None = None, hints: dict | None = None,
                  proxy: bool = False, part: dict | None = None,
                  window: list | None = None, review: bool = False) -> tuple[Path, list[dict]]:
    """Analyse `src` and render the requested variants. Returns (output dir, results).

    raw_input / raw_duration: where the original recording is when `src` is only a sampled
    proxy of it (Drive). Without them `src` itself is the original - unless `proxy`: then the
    real time between frames is unknown, and the hand isn't motion-blurred (it could ghost).
    hints: from the file name ('Sasuke - ink only - continued'): pencil True/False, continued.
    part: one drawing of several on the page, from find_parts(): only its time, framed on it.
    review: every version is watched by the reviewer (review.py) before it counts; one that fails
    is replaced by another style, and what was held back is listed in held.json."""
    hints = hints or {}
    how = {"continued": bool(hints.get("continued")), "pencil": hints.get("pencil")}
    src = Path(src).expanduser().resolve()
    out_dir = Path(out_root).resolve() / src.stem
    (out_dir / "variants").mkdir(parents=True, exist_ok=True)
    (out_dir / "thumbs").mkdir(exist_ok=True)

    t0 = time.time()
    from .media import probe
    src_s = probe(src)["duration"]
    pg = _one_page(src, out_dir, max_width, window, (raw_duration or src_s) / max(src_s, 1e-6),
                   ref_time=(part or {}).get("ref_t"),
                   span=(part["t0"], part["t1"]) if part else None)
    work, store, norm = pg.work, pg.store, pg.norm
    total = raw_duration or norm["source_duration"]
    scale = total / max(norm["source_duration"], 1e-6)
    raw = raw_input or (lambda t0: ["-ss", f"{t0:.3f}", "-i", str(src)])
    stride = 1
    times = [pg.time(f) for f in range(len(store))]          # recording seconds of each frame
    if part:
        # one drawing of several: only the time it was being drawn ...
        f0 = next((f for f, s in enumerate(times) if s >= part["t0"] - pg.dt / 2), 0)
        f1 = max([f for f, s in enumerate(times) if s <= part["t1"]] or [len(times) - 1])
        f1 = max(f1, min(len(times) - 1, f0 + 1))
        print(f"drawing {part.get('label', '')}: frames {f0}-{f1} of the page")
        if (f0, f1) != (0, len(store) - 1):
            cut = out_dir / "work_part.mp4"
            trim(work, cut, f0, f1)
            work.unlink(missing_ok=True)
            cut.rename(work)
            store = FrameStore(work)
            times = times[f0:f1 + 1]
            norm["part_frames"] = [f0, f1]
    real_dt = pg.dt
    if pg.dt > SPARSE_DT and not (proxy and raw_input is None):
        t_a, t_b = times[0], times[-1] + pg.dt
        dense = densify(raw, t_a, t_b, (store.width, store.height), _pixels(src), len(store),
                        out_dir / "work_dense.mp4")
        if dense is not None:
            ddt = (t_b - t_a) / len(dense)
            kept = list(range(len(dense)))
            if pg.matcher is not None:
                # the dense frames come straight from the recording: recognise the page again
                track = track_page(dense, pg.matcher, kept)
                if len(track) >= 1.3 * len(store):
                    write_page(dense, track, (store.width, store.height), out_dir / "work_dense2.mp4")
                    kept = [i for i, _ in track]
                    dense = FrameStore(out_dir / "work_dense2.mp4")
                    (out_dir / "work_dense.mp4").unlink(missing_ok=True)
                    (out_dir / "work_dense2.mp4").rename(out_dir / "work_dense.mp4")
                else:
                    dense = None
            if dense is not None:
                stride = max(1, round(len(dense) / len(store)))   # analyse at the old spacing
                store = dense
                times = [t_a + i * ddt for i in kept]
                real_dt = ddt
                work.unlink(missing_ok=True)
                (out_dir / "work_dense.mp4").rename(work)
                norm["dense_frames"] = len(store)
    if proxy and raw_input is None:
        real_dt = None
    was_wide = store.width / store.height > 9 / 16 + 0.02
    part_filter = ""
    if part and list(part["box"]) != [0, 0, 1, 1]:
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
    if "process_cuts" in names and norm.get("stabilised"):
        names = swap_out(names, "process_cuts", a.has_pencil)  # raw cuts wouldn't be lined up like the rest
        print(f"page was lined up (paper/camera moved): made {names[-1]} instead of process_cuts")
    if "process_cuts" in names:
        # working-copy seconds -> frame -> real recording time (one page, maybe re-read densely)
        clips = process_cuts_clips(a, frame_filter, raw,
                                   lambda s: times[int(min(len(times) - 1, max(0, round(s * FPS / norm["speedup"]))))],
                                   norm["speedup"], total,
                                 out_dir, (store.width, store.height))
        if len(clips) < 4:
            names = swap_out(names, "process_cuts", a.has_pencil)
            print(f"warn: only {len(clips)} real-speed moments; made {names[-1]} instead of process_cuts")
    plans = build(a, names, seed, subject, copy, clips)
    results, held = [], []
    tried = set(names)

    def make(plan):
        dst = out_dir / "variants" / f"{plan.name}.mp4"
        render(plan, store, dst, audio, a.focus, a.usable)
        verdict = None
        if review:                                    # the reviewer watches it before you do
            from .review import review as watch
            verdict = watch(dst)
        return dst, verdict

    for plan in plans:
        first = plan.name
        for attempt in range(3):
            dst, verdict = make(plan)
            if verdict is None or verdict.ok:
                break
            print(f"  reviewer held back {plan.name}: {'; '.join(verdict.reasons)}")
            held.append({"name": plan.name, "reasons": verdict.reasons})
            dst.unlink(missing_ok=True)
            # another style in its place, so the drawing still gets its full set
            spare = [n for n in FALLBACK_ORDER if n not in tried and (a.has_pencil or n != "before_after")]
            if not spare or attempt == 2:
                plan = None
                break
            tried.add(spare[0])
            plan = build(a, spare[:1], seed, subject, copy, clips)[0]
        if plan is None:
            print(f"  no style passed in place of {first}")
            continue
        _thumb(dst, 0.02, out_dir / "thumbs" / f"{plan.name}.jpg")
        if sheets:
            _contact_sheet(dst, out_dir / "thumbs" / f"{plan.name}_sheet.jpg")
        results.append({"name": plan.name, "file": f"variants/{plan.name}.mp4",
                        "seconds": plan.seconds, "description": plan.description,
                        "grade": plan.grade, "hook": plan.hook, "caption": plan.caption,
                        **({"review": verdict.to_dict()} if verdict else {})})
        print(f"  rendered {plan.name:13s} {plan.seconds:5.1f}s  hook: {plan.hook!r}"
              f"{'  (reviewed: ok)' if verdict else ''}")
    (out_dir / "held.json").write_text(json.dumps(held, indent=2, ensure_ascii=False))
    (out_dir / "variants.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
    if review_page:
        _review_page(out_dir, src.name, results)
    if not keep_work:
        work.unlink(missing_ok=True)
    print(f"done in {time.time() - t0:.0f}s -> {out_dir}")
    return out_dir, results
