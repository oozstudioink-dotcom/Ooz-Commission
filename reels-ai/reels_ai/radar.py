"""The trend radar and the request tally: what to draw next.

Once a day the bot looks at the most-viewed short drawing videos of the last week on YouTube and
at the newest comments on your own videos, and sends you one message: the characters getting the
most views right now (with how many videos and channels, so one lucky video doesn't count as a
trend) and the characters your viewers asked for (with who asked first, to credit them).

Counting is done here from the numbers YouTube returns. Reading names out of titles and comments
is done by Claude when there is a key (under a cent a day); without one, hashtags and "draw X"
phrases are counted instead. It suggests; you decide.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import timedelta

GENERIC = set("""shorts short art artist artwork drawing draw drawings sketch sketching anime manga ink inking
inktober pen pencil speed speeddrawing speedart speedpaint timelapse tutorial howto how to easy viral fyp foryou
trending satisfying asmr fanart illustration cartoon comics comic otaku weeb edit amv youtubeshorts ytshorts
animeart animedrawing mangaart mangadrawing artshorts drawingshorts stickman stickfigure transformation the a of
in my i""".split())

ASK = re.compile(r"\b(?i:draw|do|make|next|try|can you|could you|pls|please)\b[\s:,]*(?i:draw|do|make)?\s*"
                 r"([A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,3})")

SYSTEM = """You help a manga artist decide what to draw next. You get (1) titles and tags of the \
most-viewed short drawing videos of the last week, numbered, and (2) recent comments on the \
artist's own videos, numbered. From the titles: list each fictional character that is the subject \
of at least one video, with the series it is from and the numbers of the videos about it. From the \
comments: list each character a viewer asks the artist to draw, with the numbers of the comments \
asking. Use only names that appear in the text given; never add a character that is not there. \
Merge spellings of the same character (Goku, Son Goku). Ignore generic words (anime, drawing)."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["trending", "requests"], "properties": {
    "trending": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["name", "series", "videos"],
        "properties": {"name": {"type": "string"}, "series": {"type": "string"},
                       "videos": {"type": "array", "items": {"type": "integer"}}}}},
    "requests": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["name", "comments"],
        "properties": {"name": {"type": "string"},
                       "comments": {"type": "array", "items": {"type": "integer"}}}}}}}


def gather(yt, queries: list[str], now, days: int = 7, max_seconds: int = 90) -> list[dict]:
    """The week's most-viewed short drawing videos across the searches, each once, most views first."""
    after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for q in queries:
        ids += [i for i in yt.search_shorts(q, after) if i not in ids]
    vids = [v for v in yt.videos(ids) if v["seconds"] <= max_seconds]
    return sorted(vids, key=lambda v: -v["views"])


def by_hashtags(videos: list[dict]) -> list[dict]:
    """No Claude: names are the hashtags and tags that aren't generic art words."""
    found: dict[str, list[int]] = defaultdict(list)
    for k, v in enumerate(videos):
        words = {w.lower() for w in re.findall(r"#(\w+)", v["title"])} | {t.lower().replace(" ", "") for t in v["tags"][:8]}
        for w in words:
            if w not in GENERIC and len(w) > 2 and not w.isdigit() and k not in found[w]:
                found[w].append(k)
    return [{"name": w, "series": "", "videos": ks} for w, ks in found.items()]


def by_phrases(comments: list[dict]) -> list[dict]:
    """No Claude: "draw Sasuke", "next Gojo" in a comment counts as a request."""
    found: dict[str, list[int]] = defaultdict(list)
    for k, c in enumerate(comments):
        for name in ASK.findall(c["text"]):
            found[name.strip().lower()].append(k)
    return [{"name": n.title(), "comments": ks} for n, ks in found.items()]


def read_names(videos: list[dict], comments: list[dict], client=None, model: str = "claude-opus-5-5") -> dict:
    """Claude reads the names out of the titles and comments (see SCHEMA); returns tokens too."""
    import anthropic
    client = client or anthropic.Anthropic()
    text = "Videos:\n" + "\n".join(f"{k}. {v['title'][:120]} | tags: {', '.join(v['tags'][:6])}"
                                   for k, v in enumerate(videos))
    text += "\n\nComments:\n" + ("\n".join(f"{k}. {c['text'][:200]}" for k, c in enumerate(comments)) or "(none)")
    with client.beta.messages.stream(
        model=model, max_tokens=4000, system=SYSTEM,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": text}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"Claude gave no list ({msg.stop_reason})")
    out = json.loads(next(b.text for b in msg.content if b.type == "text"))
    out["tokens"] = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
    return out


def rank_trending(named: list[dict], videos: list[dict], drawn: set[str], top: int = 3) -> list[dict]:
    """A trend is a character several channels got views with this week. Ones you drew lately
    are left out."""
    out = []
    for t in named:
        ks = sorted({k for k in t.get("videos", []) if 0 <= k < len(videos)}, key=lambda k: -videos[k]["views"])
        if not ks or _key(t["name"]) in drawn:
            continue
        out.append({"name": t["name"], "series": t.get("series", ""), "videos": len(ks),
                    "channels": len({videos[k]["channel"] for k in ks}),
                    "views": sum(videos[k]["views"] for k in ks), "best": videos[ks[0]]})
    out.sort(key=lambda t: (-min(t["channels"], 3), -t["views"]))       # 3+ channels first, then views
    return out[:top]


def rank_requests(named: list[dict], comments: list[dict], top: int = 3) -> list[dict]:
    out = []
    for r in named:
        ks = sorted({k for k in r.get("comments", []) if 0 <= k < len(comments)})
        if not ks:
            continue
        people = list(dict.fromkeys(comments[k]["author"] for k in ks))
        out.append({"name": r["name"], "asks": len(people), "likes": sum(comments[k]["likes"] for k in ks),
                    "first": comments[ks[-1]]["author"]})                 # newest first in the list
    out.sort(key=lambda r: (-r["asks"], -r["likes"]))
    return out[:top]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _n(v: int) -> str:
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3 else str(v)


def message(trending: list[dict], requests: list[dict]) -> str:
    lines = ["\U0001f4c8 What to draw today"]
    if trending:
        lines.append("\nGetting views this week (short drawing videos):")
        for k, t in enumerate(trending, 1):
            series = f" ({t['series']})" if t["series"] else ""
            lines.append(f"{k}. {t['name']}{series}: {_n(t['views'])} views across {t['videos']} video(s) from "
                         f"{t['channels']} channel(s)\n   top: youtu.be/{t['best']['id']} ({_n(t['best']['views'])})")
    else:
        lines.append("\nNo clear character trend in this week's drawing videos.")
    if requests:
        lines.append("\nYour viewers asked for:")
        for r in requests:
            lines.append(f"• {r['name']}: {r['asks']} viewer(s), first {r['first']}")
        lines.append("Name the recording after one of these and I credit whoever asked in the caption.")
    return "\n".join(lines)
