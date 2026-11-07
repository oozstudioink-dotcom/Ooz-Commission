"""Analyse one timelapse and render variants (used by the CLI and the bot)."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

import cv2
import numpy as np

from .analyze import analyze
from .media import FrameStore, normalize
from .variants import RECIPES, build, render


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


def make_variants(src: Path, out_root: Path, names: list[str] | None = None, seed: int = 1,
                  subject: str | None = None, audio: Path | None = None, sheets: bool = False,
                  keep_work: bool = False, review_page: bool = True) -> tuple[Path, list[dict]]:
    """Analyse `src` and render the requested variants. Returns (output dir, results)."""
    src = Path(src).expanduser().resolve()
    out_dir = Path(out_root).resolve() / src.stem
    (out_dir / "variants").mkdir(parents=True, exist_ok=True)
    (out_dir / "thumbs").mkdir(exist_ok=True)

    t0 = time.time()
    work = out_dir / "work.mp4"
    norm = normalize(src, work)
    print(f"normalized {src.name} -> {norm['width']}x{norm['height']} (x{norm['speedup']:.1f} speed)")
    store = FrameStore(work)
    a = analyze(store)
    (out_dir / "analysis.json").write_text(json.dumps({**norm, **a.to_dict()}, indent=2))
    print("beats (s):", a.to_dict()["seconds"])

    names = names or list(RECIPES)
    plans = build(a, names, seed, subject)
    results = []
    for plan in plans:
        dst = out_dir / "variants" / f"{plan.name}.mp4"
        render(plan, store, dst, audio)
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
