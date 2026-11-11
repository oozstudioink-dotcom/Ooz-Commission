"""Labels, buttons and small helpers every part of the bot uses."""
from __future__ import annotations

import json  # noqa: F401
import os  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import tempfile  # noqa: F401
from datetime import datetime, timedelta  # noqa: F401
from pathlib import Path  # noqa: F401
from zoneinfo import ZoneInfo  # noqa: F401

from ..state import iso, load_config, load_state, new_id, now, parse, save_state  # noqa: F401
from ..telegram import Bot, TelegramError  # noqa: F401

STYLE_LABELS = {
    "reveal_first": "Reveal first",
    "speedrun": "Speedrun",
    "before_after": "Before / after",
    "detail_zoom": "Detail zoom",
    "ink_only": "Ink only",
    "process_cuts": "Process cuts",
    "one_take": "One take",
    "rewind": "Rewind",
    "chapters": "Chapters",
    "last_lines": "Last lines",
    "slow_open": "Slow open",
    "page_compilation": "Page compilation",
}


PLATFORM_LABELS = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "tiktok": "TikTok"}


MAX_BOT_DOWNLOAD = 20 * 1024 * 1024   # Telegram bots can only download files up to 20 MB


HELP = (
    "Send me a drawing timelapse (as a video) and I'll make variants for you to approve.\n"
    "Add the character name as the video's caption, e.g. Naruto.\n\n"
    "/status  overview\n/longform [project]  build a LongDraw from Drive \u203a Longform (no name = list projects)\n/redo [name] [16-34]  re-make a drawing's Shorts with the latest editing (no name = newest; all = every drawing; minutes 16-34 = use only that part of the recording)\n/inventory  every drawing and its Shorts, with their ids (v12, d4) and what's kept, waiting, dropped\n/delete v12 d4  delete Shorts (v..) or a whole drawing (d..) from your bank and this chat (/undelete to undo)\n/name <name>  name the oldest recording I asked about (or reply to my question), e.g. /name Kakashi - ink only\n/ideas  paste hooks/captions (e.g. from the Gemini app) to mix into new videos\n/retire <name>  never use a drawing again (/unretire to undo)\n/clear  delete recent chat messages (Telegram allows the last 2 days)\n/compile [title]  stitch the drawings since the last compilation into one page video\n/report  stats report now (also sent every Sunday)\n/queue  your video bank and how many days it covers\n/pause  stop all posting\n/resume  start posting again\n/help  this message"
)


def pending_markup(vid: str, drops: bool = False) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Keep" if drops else "✅ Post", "callback_data": f"ok:{vid}"},
         {"text": "❌ Skip", "callback_data": f"no:{vid}"}],
        [{"text": "✏️ Edit caption", "callback_data": f"ed:{vid}"}],
    ]}


def rating_markup(vid: str) -> dict:
    """Under each drop: how did this one turn out? Teaches the bot which styles you like."""
    return {"inline_keyboard": [[{"text": "👍 Good one", "callback_data": f"up:{vid}"},
                                 {"text": "👎 Not my style", "callback_data": f"dn:{vid}"}]]}


def label_markup(text: str) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": "noop"}]]}


def _time_range(arg: str) -> tuple[str, list | None]:
    """'Lets Go 16-34' -> ('Lets Go', [960, 2040]): minutes, or m:ss ('16:30-34')."""
    m = re.search(r"(?:^|\s)(\d{1,3}(?::\d{2})?)\s*(?:-|\u2013|to)\s*(\d{1,3}(?::\d{2})?)\s*(?:min|m)?\s*$", arg)
    if not m:
        return arg, None

    def sec(s: str) -> float:
        mm, _, ss = s.partition(":")
        return int(mm) * 60 + (int(ss) if ss else 0)

    a, b = sec(m.group(1)), sec(m.group(2))
    if b <= a:
        return arg, None
    return arg[:m.start()].strip(), [a, b]


def _mmss(s: float) -> str:
    s = int(s)
    return f"{s // 60}:{s % 60:02d}" if s % 60 else str(s // 60)


def _norm_name(text: str) -> str:
    """For matching names you type: 'Lets+Go+LIVE!', 'Lets_Go live' and 'lets go' all match."""
    import urllib.parse
    text = urllib.parse.unquote_plus(text or "").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def strip_guessed_names(copy: dict) -> dict:
    """For an unlabelled drawing: drop Gemini's character guess from the hooks, caption and hashtags."""
    guess = (copy.get("subject") or "").strip()
    out = {**copy, "subject": ""}
    if not guess:
        return out
    words = [w for w in re.findall(r"[A-Za-z]{3,}", guess)]
    bad = re.compile("|".join(re.escape(w) for w in words), re.I) if words else None
    if bad is None:
        return out
    out["hooks"] = {k: v for k, v in (copy.get("hooks") or {}).items() if not bad.search(v)}
    if copy.get("caption") and bad.search(copy["caption"]):
        out["caption"] = ""
    out["hashtags"] = [t for t in copy.get("hashtags") or [] if not bad.search(t)]
    return out


ALBUM_BYTES = 40 * 1024 * 1024        # bots may upload ~50 MB per request; albums go up as one


def album_chunks(out_dir: Path, results: list[dict], limit: int = ALBUM_BYTES) -> list[list[dict]]:
    """Split a drawing's videos into albums of at most 10 videos and `limit` bytes together."""
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for r in results:
        p = Path(out_dir) / r.get("tg_file", r["file"])
        n = p.stat().st_size if p.exists() else 0
        if cur and (len(cur) == 10 or size + n > limit):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        chunks.append(cur)
    return chunks


def dispatch_workflow(workflow: str, inputs: dict) -> None:
    """Start another workflow in this repo (allowed with the job's GITHUB_TOKEN + actions: write)."""
    import json as _json
    import urllib.request
    repo, token = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN", "")
    if not (repo and token):
        raise RuntimeError("not running in GitHub Actions (no GITHUB_TOKEN)")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches",
        data=_json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": inputs}).encode(),
        method="POST", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).close()


def shrink_for_telegram(path: Path, limit: int = 49 * 1024 * 1024) -> Path | None:
    """Bots can upload up to 50 MB; re-encode smaller if needed."""
    import subprocess
    if path.stat().st_size <= limit:
        return path
    for crf in (28, 32):
        out = path.with_name(f"{path.stem}_crf{crf}.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:v", "libx264", "-crf", str(crf),
                        "-preset", "medium", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)],
                       check=True)
        if out.stat().st_size <= limit:
            return out
    return None
