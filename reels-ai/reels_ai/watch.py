"""Watch a recording once and write down what is in it: the edit map.

Ears (ears.py) give the transcript, eyes (eyes.py) give Claude's reading of a storyboard, and the
pixel measurements (media.py) give exact frames. The map says which pages appear, what happens
in each stretch, and which separate drawings were made and when. The editor cuts from the map:
Claude decides WHAT is there (how many drawings, which page is the one being drawn), the
measurements decide exactly WHERE (the frame a page changes, where the ink is).

The map is saved next to the bot's memory in Drive, so the recording never has to be watched
again and an app can show it later.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from . import ears, eyes


def edit_map(store, dt: float, file_name: str, transcript: list[dict], look=None, model: str = eyes.MODEL,
             log=print) -> dict:
    frames = eyes.storyboard(store, dt)
    seen = (look or eyes.look)(frames, ears.as_text(transcript), file_name, model=model)
    used = seen.get("tokens") or {}
    log(f"eyes: {len(seen.get('pages', []))} page(s), {len(seen.get('drawings', []))} drawing(s) seen in "
        f"{len(frames)} frames" + (f" ({used.get('in', 0)} tokens in, {used.get('out', 0)} out)" if used else ""))
    return {"version": 1, "file": file_name, "seconds": round(len(store) * dt, 1),
            "frame_times": [round(t, 1) for t, _ in frames], "transcript": transcript, **seen}


def _tighten(frame: np.ndarray, box: list[float], pad: float = 0.12) -> list[float]:
    """Claude's rough box, pulled in to the ink actually inside it (measured on the frame)."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    x0, x1 = max(0.0, x0 - pad * bw), min(1.0, x1 + pad * bw)
    y0, y1 = max(0.0, y0 - pad * bh), min(1.0, y1 + pad * bh)
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    small = cv2.resize(g, (180, max(1, round(h * 180 / w))), interpolation=cv2.INTER_AREA)
    paper = cv2.GaussianBlur(cv2.dilate(small, np.ones((9, 9), np.uint8)), (0, 0), 4)
    ink = (paper - small) > 40
    H, W = ink.shape
    region = np.zeros_like(ink)
    region[int(y0 * H):int(np.ceil(y1 * H)), int(x0 * W):int(np.ceil(x1 * W))] = True
    ys, xs = np.nonzero(ink & region)
    if len(xs) < 60:
        return [round(v, 4) for v in (x0, y0, x1, y1)]
    bx0, bx1 = np.percentile(xs, [1, 99]) / W
    by0, by1 = np.percentile(ys, [1, 99]) / H
    return [round(float(v), 4) for v in (bx0, by0, bx1, by1)]


def parts_from_map(m: dict, store, dt: float, cuts: list[int]) -> list[dict]:
    """The drawings in an edit map as parts the editor renders one by one:
    [{"t0", "t1", "box", "ref_t", "label", "title"}], earliest first."""
    from .media import choose_page
    times = m.get("frame_times") or []
    n, total = len(store), len(store) * dt
    if len(times) < 2:
        return []
    gap = float(np.median(np.diff(times)))
    cut_t = [c * dt for c in cuts]

    def snap(t: float) -> float:
        """A boundary Claude gave to the nearest storyboard frame, moved to the exact page change
        when one was measured close by."""
        near = [c for c in cut_t if abs(c - t) <= gap]
        return min(near, key=lambda c: abs(c - t)) if near else t

    drawn_pages = {p["id"] for p in m.get("pages", []) if p.get("drawn_on")}
    drawings = []
    for d in m.get("drawings", []):
        a, b = int(d.get("first_frame", -1)), int(d.get("last_frame", -1))
        if not (0 <= a <= b < len(times)):
            continue
        if drawn_pages and d.get("page") not in drawn_pages:      # a page that was only shown
            continue
        drawings.append((a, b, d))
    per_page: dict[str, int] = {}
    for _, _, d in drawings:
        per_page[d["page"]] = per_page.get(d["page"], 0) + 1
    parts = []
    for a, b, d in sorted(drawings, key=lambda x: x[0]):
        t0 = max(0.0, snap(times[a] - gap / 2))
        t1 = min(total, snap(times[b] + gap / 2))
        if t1 - t0 < 2 * dt:
            continue
        f0, f1 = int(t0 / dt), int(min(n - 1, t1 / dt))
        ref = choose_page(store, f0, f1) if f1 - f0 >= 40 else None
        ref = ref if ref is not None else (f0 + f1) // 2
        box = [0, 0, 1, 1]
        if per_page[d["page"]] > 1:                               # shares its page: frame it
            bx = d.get("box") or {}
            rough = [float(bx.get(k, v)) for k, v in (("x0", 0), ("y0", 0), ("x1", 1), ("y1", 1))]
            if rough[2] - rough[0] > 0.05 and rough[3] - rough[1] > 0.05:
                box = _tighten(store[ref], [min(1, max(0, v)) for v in rough])
        parts.append({"t0": round(t0, 2), "t1": round(t1, 2), "box": box, "ref_t": round(ref * dt, 2),
                      "title": (d.get("title") or "").strip()[:60],
                      "named_by_artist": bool(d.get("named_by_artist"))})
    for k, p in enumerate(parts):
        p["label"] = f"{k + 1} of {len(parts)}"
    return parts


MAPS = "Bot memory/Edit maps"
PRICE_IN, PRICE_OUT = 4.0, 20.0          # dollars per million tokens (Claude Opus 5.5)


def cost(m: dict) -> float:
    """Dollars Claude charged for making this map (0 for a map read back from Drive)."""
    t = m.get("tokens") or {}
    return 0.0 if m.get("reused") else (t.get("in", 0) * PRICE_IN + t.get("out", 0) * PRICE_OUT) / 1e6


def saved_map(drive, drive_id: str) -> dict | None:
    """The map made the last time this recording was watched, if there is one."""
    try:
        f = drive.find_file(f"{drive_id}.json", drive.folder(MAPS))
        m = json.loads(drive.read_text(f["id"])) if f else None
        return m if isinstance(m, dict) and m.get("frame_times") else None
    except Exception as e:  # noqa: BLE001 - no saved map: watch it again
        print(f"warn: couldn't read the saved edit map: {str(e)[:120]}")
        return None


def save_map(drive, drive_id: str, text: str) -> None:
    folder = drive.folder(MAPS)
    old = drive.find_file(f"{drive_id}.json", folder)
    drive.write_text(f"{drive_id}.json", folder, text, file_id=(old or {}).get("id"))


def watcher(raw_input, file_name: str, cfg: dict, work: Path, log=print, saved: dict | None = None):
    """What find_parts calls to get an edit map: (frames, seconds per frame) -> map.
    None when watching is off or there is no Claude key (the editor then measures only).
    `saved` is the map from an earlier run: when it fits this recording it is used as it is,
    so a recording is listened to and shown to Claude (and paid for) only once."""
    wc = cfg.get("watch", {})
    if not wc.get("enabled", True) or not (eyes.available() or saved):
        return None

    def run(store, dt: float) -> dict:
        total = len(store) * dt
        if saved and abs(float(saved.get("seconds") or 0) - total) <= 0.03 * total + 2:
            log("watched before: using the saved edit map (no Claude cost)")
            return {**saved, "reused": True}
        if not eyes.available():
            raise RuntimeError("the saved edit map doesn't fit and there is no Claude key")
        heard = []
        if raw_input is not None and wc.get("ears", True):
            heard = ears.listen(raw_input, Path(work), wc.get("whisper_model", "base"), log=log)
        return edit_map(store, dt, file_name, heard, model=wc.get("model", eyes.MODEL), log=log)

    return run
