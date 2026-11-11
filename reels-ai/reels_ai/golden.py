"""The test set: run the editor on real recordings and compare with the known right answers.

tests/golden.toml lists recordings in Drive and what each must give (how many drawings, when
they were made). This runs the same steps the bot runs - read the recording, find its drawings,
make a Short of each, let the reviewer watch it - and reports every difference. Nothing is sent
to Telegram chats or posted, and the bot's memory is not touched.
"""
from __future__ import annotations

import os
import tempfile
import time
import tomllib
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "golden.toml"
TOLERANCE_MIN = 1.5          # a drawing's start/end may be this far from the written minutes


def load(path: Path = GOLDEN) -> list[dict]:
    return tomllib.loads(Path(path).read_text()).get("recording", [])


def compare(expected: dict, parts: list[dict], reviews: list[dict]) -> list[str]:
    """Differences between what the editor found and the right answer ([] = all correct).
    parts: find_parts() output ([] = one drawing); reviews: one {"passed": n, "held": [...]} per drawing."""
    problems = []
    found = max(1, len(parts))
    want = int(expected.get("drawings", 1))
    if found != want:
        spans = ", ".join(f"{p['t0'] / 60:.1f}-{p['t1'] / 60:.1f}" for p in parts) or "the whole recording"
        problems.append(f"found {found} drawing(s), should be {want} (found at minutes: {spans})")
    minutes = expected.get("minutes")
    if minutes and parts and len(parts) == len(minutes):
        for k, ((a, b), p) in enumerate(zip(sorted(minutes), sorted(parts, key=lambda p: p["t0"]))):
            if abs(p["t0"] / 60 - a) > TOLERANCE_MIN or abs(p["t1"] / 60 - b) > TOLERANCE_MIN:
                problems.append(f"drawing {k + 1} cut at minutes {p['t0'] / 60:.1f}-{p['t1'] / 60:.1f}, "
                                f"should be {a}-{b}")
    if expected.get("pass", "all") == "all":
        for k, r in enumerate(reviews):
            if not r.get("passed"):
                why = "; ".join(sorted({x for h in r.get("held", []) for x in h["reasons"]})) or "nothing was made"
                problems.append(f"drawing {k + 1}: no Short passed the reviewer ({why})")
    return problems


def find_recording(drive, name: str) -> dict | None:
    """The newest Drive video whose name contains `name` (Test set, Inbox, Processed, Longform)."""
    from .bot import _norm_name
    want = _norm_name(name)
    folders = []
    for path in ("Test set", "Inbox", "Inbox/Processed"):
        try:
            folders.append(drive.folder(path))
        except Exception:  # noqa: BLE001 - a missing folder is just not searched
            pass
    try:
        for proj in drive.list(drive.folder("Longform"), "folder"):
            folders.append(proj["id"])
            if proj["name"] == "Finished":
                folders += [f["id"] for f in drive.list(proj["id"], "folder")]
    except Exception:  # noqa: BLE001
        pass
    hits = [f for fid in folders for f in drive.list(fid, "video") if want in _norm_name(f["name"])]
    return max(hits, key=lambda f: f.get("createdTime") or "") if hits else None


def run_one(drive, expected: dict, work: Path, style: str = "one_take", log=print) -> list[str]:
    from .drive import make_proxy, remote_duration
    from .pipeline import find_parts, make_variants
    import json
    f = find_recording(drive, expected["name"])
    if not f:
        return [f"no recording in Drive matches “{expected['name']}”"]
    url, token = drive.media_url(f["id"]), drive.fresh_token()
    meta = f.get("videoMediaMetadata") or {}
    dur = (int(meta["durationMillis"]) / 1000) if meta.get("durationMillis") else remote_duration(url, token)
    log(f"== {f['name']} ({(dur or 0) / 60:.1f} min)")
    src = make_proxy(url, token, work / f"{f['id']}.mp4", dur or 30 * 60, max_side=2560)
    raw = {"raw_duration": float(dur), "raw_input": lambda t0: ["-headers", f"Authorization: Bearer {token}\r\n",
                                                                 "-ss", f"{t0:.3f}", "-i", url]} if dur else {}
    parts = find_parts(src, work / "parts", raw_duration=dur)
    reviews = []
    for k, part in enumerate(parts or [None]):
        out, res = make_variants(src, work / f"d{k}", [style], review_page=False, proxy=True, part=part,
                                 review=True, **raw)
        held = json.loads((out / "held.json").read_text()) if (out / "held.json").exists() else []
        reviews.append({"passed": len(res), "held": held})
    return compare(expected, parts, reviews)


def run(only: str = "") -> int:
    from .drive import Drive
    from .state import load_config
    env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    if not all(env):
        raise SystemExit("the test set needs the Drive connection (YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN)")
    drive = Drive(*env, root=load_config().get("drive", {}).get("root", "Ooz Reels"))
    cases = [c for c in load() if not only or only.lower() in c["name"].lower()]
    lines, failed = [], 0
    for case in cases:
        t0 = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                problems = run_one(drive, case, Path(tmp))
            except Exception as e:  # noqa: BLE001 - one broken recording must not hide the others
                problems = [f"the editor crashed: {str(e)[:200]}"]
        mins = (time.time() - t0) / 60
        if problems:
            failed += 1
            lines.append(f"❌ {case['name']} ({mins:.0f} min)")
            lines += [f"   • {p}" for p in problems]
        else:
            lines.append(f"✅ {case['name']}: {case.get('drawings', 1)} drawing(s), all correct ({mins:.0f} min)")
    report = f"Test set: {len(cases) - failed} of {len(cases)} recordings correct\n" + "\n".join(lines)
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text("## " + report.replace("\n", "\n\n"))
    return 1 if failed else 0
