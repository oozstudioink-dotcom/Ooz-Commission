"""The channel auditor and the content calendar.

audit      which of your recent videos are doing better or worse than your usual, measured from
           YouTube's own numbers (views per day since posting, likes and comments per view)
propose    new titles, descriptions and tags for the weakest ones, written by Claude from the
           video's own details, your best performers and this week's trends. Nothing is changed
           on YouTube here: the bot shows each proposal and applies it only when you tap Apply.
calendar   a plan for the next seven days from the trends, your viewers' requests and what has
           worked on your channel.

The measuring is plain arithmetic. Claude is used for the writing and the planning; without a
key you still get the measured audit and a simple calendar.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from statistics import median

MIN_AGE_DAYS = 2          # younger videos are still being shown around: not judged yet
WEAK, STRONG = 0.6, 1.6   # views per day against your usual: below = weak, above = strong

SYSTEM = """You audit a small YouTube channel for its owner, a traditional manga inking artist who \
posts short drawing videos. You get the channel's recent videos with their numbers (the "pace" is \
views per day against the channel's usual: 1.0 is typical), this week's trending characters in \
drawing videos, and which videos are weak.

Write:
- "summary": two or three plain sentences on what is working and what is not, from the numbers.
- "findings": up to four specific, practical observations (patterns in titles, length, subjects).
- "rewrites": for each weak video listed, a better title, description and tags.

Rules for rewrites. Title: under 60 characters, the character or subject first, plain words a \
person would search, no clickbait and no claim the video doesn't deliver, at most one hashtag. \
Description: two or three short lines; keep every link, credit and @mention from the current \
description exactly; end with a question. Tags: 8 to 12, the character and series first. Use \
only character and series names that already appear in that video's own title, description or \
tags: never guess who is in a drawing. If a video has no name, keep the rewrite about the \
drawing and the inking. Keep "#shorts" if the current title or description has it."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary", "findings", "rewrites"],
          "properties": {
              "summary": {"type": "string"},
              "findings": {"type": "array", "items": {"type": "string"}},
              "rewrites": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["video", "title", "description", "tags", "why"],
                  "properties": {"video": {"type": "integer"}, "title": {"type": "string"},
                                 "description": {"type": "string"}, "why": {"type": "string"},
                                 "tags": {"type": "array", "items": {"type": "string"}}}}}}}

PLAN_SYSTEM = """You plan one week of short drawing videos for a traditional manga inking artist. \
You get the characters getting views this week, the characters the artist's viewers asked for, \
the artist's best recent videos, the editing styles available, and the days to fill. Give one \
idea per day: the character to draw (only from the lists given; requests from viewers come \
first, never repeat a character in the week), the editing style, a 2 to 6 word on-screen hook, \
and one short reason. Vary the styles."""

PLAN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["days"], "properties": {
    "days": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["day", "character", "style", "hook", "why"],
        "properties": {"day": {"type": "string"}, "character": {"type": "string"}, "style": {"type": "string"},
                       "hook": {"type": "string"}, "why": {"type": "string"}}}}}}


def measure(videos: list[dict], now: datetime) -> list[dict]:
    """Each video with its age, views per day, and pace against the channel's usual (median)."""
    out = []
    for v in videos:
        try:
            when = datetime.fromisoformat(v["published"].replace("Z", "+00:00"))
        except (ValueError, KeyError, AttributeError):
            continue
        age = max(0.25, (now - when).total_seconds() / 86400)
        out.append({**v, "age_days": round(age, 1), "per_day": v["views"] / age,
                    "engagement": (v.get("likes", 0) + v.get("comments", 0)) / max(1, v["views"])})
    judged = [v["per_day"] for v in out if v["age_days"] >= MIN_AGE_DAYS]
    usual = median(judged) if judged else 0.0
    for v in out:
        v["pace"] = round(v["per_day"] / usual, 2) if usual else 1.0
        v["verdict"] = ("new" if v["age_days"] < MIN_AGE_DAYS else
                        "weak" if v["pace"] < WEAK else "strong" if v["pace"] > STRONG else "usual")
    return out


def weakest(measured: list[dict], n: int = 3) -> list[int]:
    """Indexes of the weakest judged videos, worst first."""
    weak = [k for k, v in enumerate(measured) if v["verdict"] == "weak"]
    return sorted(weak, key=lambda k: measured[k]["pace"])[:n]


def _n(v: float) -> str:
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.1f}K" if v >= 1e3 else f"{v:.0f}"


def report(measured: list[dict], words: dict | None = None) -> str:
    """The audit as one Telegram message."""
    judged = [v for v in measured if v["verdict"] != "new"]
    if not judged:
        return "\U0001f50e Channel audit: no videos old enough to judge yet (they need two days)."
    lines = [f"\U0001f50e Channel audit: your last {len(measured)} videos",
             f"Usual pace: {_n(median(v['per_day'] for v in judged))} views a day per video"]
    if words and words.get("summary"):
        lines.append("\n" + words["summary"].strip())
    best = sorted(judged, key=lambda v: -v["pace"])[:3]
    worst = sorted((v for v in judged if v["verdict"] == "weak"), key=lambda v: v["pace"])[:3]
    lines.append("\nDoing best:")
    lines += [f"• {v['title'][:60]}: {_n(v['views'])} views, {v['pace']:.1f}x your usual" for v in best]
    if worst:
        lines.append("\nFalling behind:")
        lines += [f"• {v['title'][:60]}: {_n(v['views'])} views, {v['pace']:.1f}x your usual" for v in worst]
    else:
        lines.append("\nNothing is falling clearly behind.")
    for f in (words or {}).get("findings", [])[:4]:
        lines.append(f"\n• {f.strip()}")
    return "\n".join(lines)


def _ask(system: str, schema: dict, text: str, model: str, client=None, max_tokens: int = 6000,
         route: dict | None = None) -> dict:
    from .llm import ask
    return ask(system, schema, text, model=model, max_tokens=max_tokens, effort="medium", route=route, client=client)


def propose(measured: list[dict], weak: list[int], trending: list[dict], model: str, client=None) -> dict:
    """Claude's reading of the audit and its rewrites for the weak videos (see SCHEMA)."""
    rows = [f"{k}. [{v['verdict']}, pace {v['pace']}] {v['title']} | {_n(v['views'])} views in {v['age_days']} days, "
            f"{v['seconds']} s, {v.get('likes', 0)} likes, {v.get('comments', 0)} comments" for k, v in enumerate(measured)]
    detail = [f"Video {k}\ntitle: {measured[k]['title']}\ndescription: {measured[k]['description'][:600]}\n"
              f"tags: {', '.join(measured[k]['tags'][:15])}" for k in weak]
    trends = ", ".join(f"{t['name']} ({t.get('series') or '?'})" for t in trending) or "none known"
    text = ("Recent videos:\n" + "\n".join(rows) + f"\n\nTrending in drawing videos this week: {trends}\n\n"
            "Weak videos to rewrite:\n\n" + ("\n\n".join(detail) or "(none)"))
    return _ask(SYSTEM, SCHEMA, text, model, client)


def safe_rewrite(video: dict, r: dict) -> dict | None:
    """A proposal checked before you ever see it: right lengths, links kept, nothing empty."""
    title = re.sub(r"\s+", " ", str(r.get("title") or "")).strip()[:100]
    desc = str(r.get("description") or "").strip()[:4900]
    tags = [re.sub(r"[<>#]", "", str(t)).strip()[:40] for t in r.get("tags") or []]
    tags = list(dict.fromkeys(t for t in tags if t))[:15]
    if len(title) < 5 or not desc or "<" in title or ">" in title:
        return None
    for link in re.findall(r"https?://\S+", video.get("description", "")):
        if link not in desc:                           # never lose a link you put there
            desc += f"\n{link}"
    if title == video.get("title") and desc == video.get("description"):
        return None
    return {"title": title, "description": desc, "tags": tags, "why": str(r.get("why") or "").strip()[:200]}


def plan_days(start: datetime, days: int = 7) -> list[str]:
    return [(start + timedelta(days=k)).strftime("%a %b %-d") for k in range(days)]


def simple_plan(day_names: list[str], trending: list[dict], requests: list[dict], styles: list[str]) -> list[dict]:
    """No Claude: requests first, then trends, one a day, styles in turn."""
    picks = [(r["name"], f"asked for by {r['by']}") for r in requests]
    picks += [(t["name"], f"{_n(t['views'])} views this week across {t['channels']} channel(s)") for t in trending]
    seen, uniq = set(), []
    for name, why in picks:
        if name.lower() not in seen:
            seen.add(name.lower())
            uniq.append((name, why))
    return [{"day": d, "character": uniq[k][0], "style": styles[k % len(styles)] if styles else "",
             "hook": "", "why": uniq[k][1]} for k, d in enumerate(day_names) if k < len(uniq)]


def plan(day_names: list[str], trending: list[dict], requests: list[dict], best: list[dict], styles: list[str],
         model: str, client=None, route: dict | None = None) -> dict:
    text = ("Days to fill: " + ", ".join(day_names)
            + "\nGetting views this week: " + (", ".join(f"{t['name']} ({t.get('series') or '?'}, {_n(t['views'])} views)"
                                                         for t in trending) or "none known")
            + "\nAsked for by viewers: " + (", ".join(f"{r['name']} (by {r['by']})" for r in requests) or "none")
            + "\nThe artist's best recent videos: " + ("; ".join(f"{v['title']} ({v['pace']}x usual)" for v in best) or "none yet")
            + "\nEditing styles: " + ", ".join(styles))
    return _ask(PLAN_SYSTEM, PLAN_SCHEMA, text, model, client, max_tokens=3000, route=route)


def calendar_text(days: list[dict], times: list[str], labels: dict) -> str:
    if not days:
        return ("\U0001f4c5 No calendar yet: I need this week's trends first. Send /trends, then /calendar.")
    lines = ["\U0001f4c5 Your next 7 days" + (f" (post at {', '.join(times)})" if times else "")]
    for d in days:
        style = labels.get(d.get("style"), d.get("style") or "")
        hook = f" · “{d['hook']}”" if d.get("hook") else ""
        lines.append(f"\n{d['day']}: {d['character']}" + (f" · {style}" if style else "") + hook
                     + (f"\n   {d['why']}" if d.get("why") else ""))
    lines.append("\nIt's a suggestion: draw what you like, and name the recording after the character.")
    return "\n".join(lines)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
