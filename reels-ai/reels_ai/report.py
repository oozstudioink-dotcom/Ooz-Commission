"""Weekly performance report from Instagram + YouTube stats (read-only API access).

Posts are matched back to the bot's variants by caption/title similarity, so the report can
say which *styles* and *hooks* do best even though the owner posts by hand.
"""
from __future__ import annotations

import difflib
import re
from datetime import datetime, timedelta, timezone

from .state import parse

STYLE_LABELS = {"reveal_first": "Reveal first", "speedrun": "Speedrun", "before_after": "Before / after",
                "detail_zoom": "Detail zoom", "ink_only": "Ink only", "process_cuts": "Process cuts", "page_compilation": "Page compilation"}


def _norm(text: str) -> str:
    text = re.sub(r"#\w+", " ", text or "")
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return " ".join(text.split())[:120]


def match_variant(text: str, variants: list[dict], cutoff: float = 0.6) -> dict | None:
    """Best variant whose caption (or hook) resembles a post's caption/title."""
    target = _norm(text)
    if not target:
        return None
    best, best_score = None, cutoff
    for v in variants:
        for cand in (v.get("caption", ""), v.get("hook", "")):
            c = _norm(cand)
            if not c:
                continue
            score = difflib.SequenceMatcher(None, target[:len(c) + 20], c).ratio()
            if c and c in target:
                score = max(score, 0.95)
            if score > best_score:
                best, best_score = v, score
    return best


def _num(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def collect(clients: dict, since: datetime) -> dict:
    """{platform: {"account": {...}, "posts": [{title, views, watch_s, subs, when}]}}"""
    out = {}
    ig = clients.get("instagram")
    if ig:
        try:
            me = ig.whoami()
            posts = []
            for m in ig.recent_media(25):
                when = datetime.strptime(m["timestamp"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                if when < since or m.get("media_product_type") != "REELS":
                    continue
                st = ig.insights(m["id"])
                posts.append({"title": m.get("caption") or "", "views": _num(st.get("views")),
                              "watch_s": _num(st.get("ig_reels_avg_watch_time")) / 1000,
                              "shares": _num(st.get("shares")), "saves": _num(st.get("saved")),
                              "when": when, "link": m.get("permalink")})
            out["instagram"] = {"account": {"followers": me.get("followers_count")}, "posts": posts}
        except Exception as e:  # noqa: BLE001
            out["instagram"] = {"error": str(e)[:200]}
    yt = clients.get("youtube")
    if yt:
        try:
            ch = yt.whoami()
            posts = []
            for v in yt.recent_videos(ch["uploads"], 20):
                when = parse(v["published"].replace("Z", "+00:00")) if v.get("published") else None
                if when and when < since:
                    continue
                st = yt.insights(v["id"])
                posts.append({"title": v["title"], "views": _num(st.get("views")),
                              "watch_s": _num(st.get("averageViewDuration")),
                              "subs": _num(st.get("subscribersGained")), "when": when,
                              "link": f"https://youtube.com/shorts/{v['id']}"})
            out["youtube"] = {"account": {"followers": ch.get("subscribers")}, "posts": posts}
        except Exception as e:  # noqa: BLE001
            out["youtube"] = {"error": str(e)[:200]}
    return out


def build(state: dict, clients: dict, now: datetime, days: int = 7) -> str:
    since = now - timedelta(days=days)
    data = collect(clients, since)
    variants = list(state.get("variants", {}).values())
    prev = state.setdefault("report_followers", {})
    lines = [f"\U0001f4ca Weekly report · {since:%b %-d}–{now:%b %-d}"]
    style_views: dict[str, list[float]] = {}
    labels = {"instagram": "Instagram Reels", "youtube": "YouTube Shorts"}
    if not data:
        return lines[0] + "\nNo platforms connected, so there are no stats to report."
    for platform, d in data.items():
        name = labels[platform]
        if "error" in d:
            lines.append(f"\n{name}: couldn't read stats ({d['error']})")
            continue
        posts = d["posts"]
        fol = d["account"].get("followers")
        delta = ""
        if fol is not None and platform in prev:
            diff = int(_num(fol) - _num(prev[platform]))
            delta = f" ({'+' if diff >= 0 else ''}{diff} this week)"
        if fol is not None:
            prev[platform] = fol
        unit = "subscribers" if platform == "youtube" else "followers"
        lines.append(f"\n{name}: {len(posts)} posts · {int(sum(p['views'] for p in posts)):,} views"
                     f" · {fol if fol is not None else '?'} {unit}{delta}")
        if posts:
            avg_watch = sum(p["watch_s"] for p in posts) / len(posts)
            lines.append(f"Average watch time: {avg_watch:.1f}s")
        for p in sorted(posts, key=lambda p: -p["views"])[:3]:
            v = match_variant(p["title"], variants)
            tag = f" · {STYLE_LABELS.get(v['style'], v['style'])}" if v else ""
            lines.append(f"• {int(p['views']):,} views · {p['watch_s']:.1f}s{tag} · "
                         f"“{(p['title'] or '').splitlines()[0][:40]}”")
        for p in posts:
            v = match_variant(p["title"], variants)
            if v:
                style_views.setdefault(v["style"], []).append(p["views"])
    if style_views:
        ranked = sorted(style_views.items(), key=lambda kv: -sum(kv[1]) / len(kv[1]))
        lines.append("\nStyles (average views per post):")
        lines += [f"{i + 1}. {STYLE_LABELS.get(s, s)}: {int(sum(v) / len(v)):,} ({len(v)} posts)"
                  for i, (s, v) in enumerate(ranked)]
        if len(ranked) > 1:
            lines.append(f"\n\U0001f449 {STYLE_LABELS.get(ranked[0][0], ranked[0][0])} is winning so far.")
    else:
        lines.append("\nStyle ranking appears once posts use the captions from your drops "
                     "(that's how I tell which style a post was).")
    return "\n".join(lines)
