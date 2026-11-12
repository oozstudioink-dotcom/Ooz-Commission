"""Sponsor finder: which brands already work with creators like you, and a first email to each.

find     reads the descriptions of the most-viewed drawing videos of the last two months and
         lists the brands named there as sponsors, discount codes or affiliate partners
verify   keeps a brand only when the words quoted as evidence really are in that description
         (a model can misread; nothing reaches you on its say-so alone)
draft    writes a short first email per brand from your channel's real numbers. Drafts only:
         the bot never emails anyone, and it never invents a contact address.
"""
from __future__ import annotations

import re
from datetime import timedelta
from statistics import median

FIND_SYSTEM = """You read YouTube video descriptions from art and drawing channels and list the \
companies that work with those creators: named sponsors ("sponsored by", "thanks to X for \
sending"), discount codes, and affiliate links to art-supply brands or shops. Leave out the \
creator's own shop, merch, Patreon, memberships and social links, music credits, and plain \
Amazon links with no brand named. For each company give its name, the kind of deal, the numbers \
of the videos it appears in, and "evidence": a short phrase copied exactly from one of those \
descriptions that shows the deal. List nothing you cannot quote."""

FIND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["brands"], "properties": {
    "brands": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["name", "kind", "videos", "evidence"],
        "properties": {"name": {"type": "string"}, "kind": {"type": "string"}, "evidence": {"type": "string"},
                       "videos": {"type": "array", "items": {"type": "integer"}}}}}}}

DRAFT_SYSTEM = """You write first-contact emails from a small creator to a brand's partnerships \
team. Who the creator is, what they want and their numbers are given; use only those facts. \
Write one email per brand: a subject line and a body under 140 words.

Lead with the work, not the numbers: the creator is a working comic artist in the manga \
tradition whose videos show the brand's kind of tool being used by hand, and the portfolio link \
lets the brand judge the art for itself. Say plainly that the channel is small and growing, give \
the numbers once exactly as given, and never round them up or describe the audience as bigger \
than it is. Do not oversell: no "huge", "massive", "viral", no promises about views or sales.

Ask for what fits a channel this size, in this order: an affiliate link or discount code, or \
product to use on camera; say the creator is open to any other arrangement the brand prefers. If \
the brand makes something on the creator's wish list, name that item. Offer one concrete video \
idea using their product. Mention that samples of finished pages are attached (the creator \
attaches them). Plain, warm, no emojis. Sign off with the channel name. Do not invent facts \
about the brand, any past relationship, or any contact name."""

DRAFT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["emails"], "properties": {
    "emails": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["brand", "subject", "body"],
        "properties": {"brand": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}}}}}


def gather(yt, queries: list[str], now, days: int = 60, per_query: int = 15) -> list[dict]:
    """The most-viewed recent drawing videos (longer ones too: that is where sponsors are named)
    whose descriptions hold a link or a deal word, each once, most views first."""
    after = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for q in queries:
        for duration in ("medium", "short"):
            ids += [i for i in yt.search_shorts(q, after, per_query, duration) if i not in ids]
    deal = re.compile(r"https?://|sponsor|code\b|affiliate|discount|% off|partner", re.I)
    vids = [v for v in yt.videos(ids) if deal.search(v.get("description", ""))]
    return sorted(vids, key=lambda v: -v["views"])


def find(videos: list[dict], model: str, route: dict | None = None, client=None) -> dict:
    from .llm import ask
    text = "\n\n".join(f"Video {k} (channel: {v['channel']})\n{v['description'][:700]}" for k, v in enumerate(videos))
    return ask(FIND_SYSTEM, FIND_SCHEMA, text, model=model, max_tokens=4000, route=route, client=client)


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def verify(found: list[dict], videos: list[dict], own_channel: str = "", top: int = 6) -> list[dict]:
    """Only brands whose quoted evidence is really in one of the descriptions named; ranked by
    how many different channels they work with, then by those videos' views."""
    out: dict[str, dict] = {}
    for b in found:
        name, quote = str(b.get("name") or "").strip(), _flat(b.get("evidence", ""))
        ks = [k for k in b.get("videos", []) if isinstance(k, int) and 0 <= k < len(videos)]
        ks = [k for k in ks if videos[k]["channel"] != own_channel]
        real = [k for k in ks if len(quote) >= 6 and quote in _flat(videos[k]["description"])]
        if not name or not real:
            continue
        ks = [k for k in ks if name.casefold() in videos[k]["description"].casefold()] or real
        e = out.setdefault(name.casefold(), {"name": name, "kind": str(b.get("kind") or "").strip()[:40],
                                             "evidence": str(b["evidence"]).strip()[:140], "ks": set()})
        e["ks"].update(ks)
    ranked = []
    for e in out.values():
        ks = sorted(e.pop("ks"), key=lambda k: -videos[k]["views"])
        ranked.append({**e, "channels": sorted({videos[k]["channel"] for k in ks}),
                       "views": sum(videos[k]["views"] for k in ks), "example": videos[ks[0]]["id"]})
    ranked.sort(key=lambda e: (-len(e["channels"]), -e["views"]))
    return ranked[:top]


def channel_facts(me: dict, recent: list[dict]) -> dict:
    """Your numbers, exactly as YouTube reports them (the emails quote these and nothing else)."""
    views = [v["views"] for v in recent]
    return {"channel": me.get("title", ""), "link": f"https://www.youtube.com/channel/{me.get('id', '')}",
            "subscribers": int(me.get("subscribers") or 0), "videos": int(me.get("videos") or 0),
            "typical_views": int(median(views)) if views else 0, "best_views": max(views) if views else 0}


def draft(brands: list[dict], facts: dict, model: str, route: dict | None = None, client=None,
          about: dict | None = None) -> dict:
    from .llm import ask
    about = about or {}
    text = (f"Creator: {facts['channel']} ({facts['link']})\nSubscribers: {facts['subscribers']}\n"
            f"Videos posted: {facts['videos']}\nTypical views on a recent video: {facts['typical_views']}\n"
            f"Best recent video: {facts['best_views']} views\n"
            f"About: {about.get('about') or 'traditional manga inking; short timelapses and longer inking sessions'}\n"
            f"Portfolio: {about.get('portfolio') or facts['link']}\n"
            f"Wish list (tools the creator wants to work with): {', '.join(about.get('wishlist') or []) or 'none given'}\n"
            "\nBrands:\n"
            + "\n".join(f"- {b['name']}: {b['kind']} with {len(b['channels'])} similar channel(s); "
                        f"seen as \"{b['evidence']}\"" for b in brands))
    return ask(DRAFT_SYSTEM, DRAFT_SCHEMA, text, model=model, max_tokens=4000, effort="medium", route=route,
               client=client)


def honest(body: str, facts: dict) -> bool:
    """An email may only quote numbers that are your real ones."""
    allowed = {str(facts[k]) for k in ("subscribers", "videos", "typical_views", "best_views")}
    nums = {n.replace(",", "") for n in re.findall(r"\b\d[\d,]{2,}\b", body)}
    return all(n in allowed for n in nums)


def _n(v: float) -> str:
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3 else f"{v:.0f}"


def list_text(brands: list[dict], scanned: int) -> str:
    if not brands:
        return (f"\U0001f91d Sponsor finder: I read {scanned} descriptions from top drawing videos and found no "
                "brand deals I could confirm. Try again in a few weeks, or tell Claude other searches to use.")
    lines = [f"\U0001f91d Brands working with creators like you (from {scanned} top drawing videos)"]
    for k, b in enumerate(brands, 1):
        lines.append(f"\n{k}. {b['name']} · {b['kind']}\n   {len(b['channels'])} channel(s): "
                     f"{', '.join(b['channels'][:3])}\n   seen as: “{b['evidence']}”\n   e.g. youtu.be/{b['example']}")
    lines.append("\nDrafts for the top ones follow. I never send anything: copy a draft into your own email, and "
                 "find the brand's partnerships or collab address on its website.")
    return "\n".join(lines)
