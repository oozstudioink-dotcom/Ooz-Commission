"""Every YouTube description, built the same way from [youtube] in config.toml.

A LongDraw, section by section (a blank line between sections):

    Welcome to LongDraw EP. 05! ...                     welcome, with the episode
    Today: Sasuke Uchiha from Naruto, drawn and ...     the one-line topic
    About this drawing / <a sentence or two>            when known (or written from your words)
    Upload schedule (Eastern Time) / ...                the schedule
    Commissions are open / Commission link: https://    the commission link
    DM me / Instagram: https:// ...                     every social, as a clickable link
    Chapters / 0:00 Intro ...                           the chapters
    Music (YouTube Audio Library) / one song a line     the songs, full names from Drive › Beats
    #manga #inking ...                                  hashtags

A Short: the intro line, the schedule, the commission link and the socials, then its hashtags.
Full https:// links are clickable in long-video descriptions; in Shorts descriptions YouTube
shows them as plain text.
"""
from __future__ import annotations

import re

TAGS_LINE = "#manga #mangaart #inking #timelapse #drawing #traditionalart #oozstudioink"
WELCOME = ("Welcome to {label}! Thanks for hanging out while this one comes together, from the "
           "first pencil lines to the last stroke of ink.")


def _yt(cfg: dict) -> dict:
    return cfg.get("youtube", {}) if "youtube" in cfg or "panels" in cfg else cfg


def schedule_block(cfg: dict) -> str:
    y = _yt(cfg)
    lines = y.get("schedule") or ["New Shorts: Monday, Wednesday and Friday at 12 PM EST",
                                  "New LongDraw: every Friday at 12 PM EST"]
    return "\n".join([y.get("schedule_title", "Upload schedule (Eastern Time)")] + list(lines))


def commission_block(cfg: dict) -> str:
    y = _yt(cfg)
    lines = [y.get("commission_title", "Commissions are open")]
    if y.get("commission_link"):
        lines.append(f"Commission link: {y['commission_link']}")
    if y.get("portfolio_link"):
        lines.append(f"Portfolio: {y['portfolio_link']}")
    return "\n".join(lines)


def dm_block(cfg: dict) -> str:
    y = _yt(cfg)
    socials = y.get("socials") or []
    if not socials:
        return ""
    return "\n".join([y.get("dm_title", "DM me")] + [f"{name}: {url}" for name, url in socials])


def shorts_body(cfg: dict) -> str:
    """A Short's description before its hashtags."""
    y = _yt(cfg)
    parts = [y.get("intro", ""), schedule_block(cfg), commission_block(cfg), dm_block(cfg)]
    return "\n\n".join(p for p in parts if p.strip())


def topic_line(subject: str, series: str = "", extra: str = "") -> str:
    subject = (subject or "this drawing").strip()
    if series and series.strip().lower() not in subject.lower():
        subject = f"{subject} from {series.strip()}"
    return f"Today: {subject}, drawn and inked by hand{(' ' + extra) if extra else ''}."


def longdraw(cfg: dict, label: str, topic: str, note: str = "", chapters: str = "",
             music: list[str] | None = None, music_source: str = "YouTube Audio Library",
             tags_line: str = TAGS_LINE) -> str:
    y = _yt(cfg)
    head = [y.get("welcome", WELCOME).format(label=label), topic]
    if note:
        head.append(f"About this drawing\n{note}")
    head += [schedule_block(cfg), commission_block(cfg), dm_block(cfg)]
    if chapters.strip():
        head.append(f"Chapters\n{chapters.strip()}")
    body = "\n\n".join(p for p in head if p and p.strip())
    songs = list(dict.fromkeys(m for m in (music or []) if m))
    title = f"Music ({music_source})" if music_source else "Music"
    while songs:                                    # YouTube's limit is 5000 characters
        block = "\n\n" + "\n".join([title] + songs)
        if len(body) + len(block) + len(tags_line) + 2 <= 4900:
            body += block
            break
        songs = songs[:-1]
    return f"{body}\n\n{tags_line}" if tags_line else body


# ---------------------------------------------------------------- reading an older description
TIMESTAMP = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}\s+\S")


def parse_old(desc: str) -> dict:
    """What an older LongDraw description already holds: its chapters, the songs it names, an
    'About this drawing' note, how long and how fast, and its hashtag line."""
    desc = desc or ""
    lines = desc.split("\n")
    chapters: list[str] = []
    for i, line in enumerate(lines):                      # the first run of timestamps from 0:00
        if re.match(r"^\s*0?0:00\s+\S", line):
            for x in lines[i:]:
                if TIMESTAMP.match(x):
                    chapters.append(x.strip())
                else:
                    break
            break
    music: list[str] = []
    m = re.search(r"(?im)^[ \t]*Music\b[^\n]*$", desc)
    if m:
        rest = desc[m.start():].split("\n")
        first = re.sub(r"(?i)^\s*Music\b[^:(\n]*(?:\([^)\n]*\))?\s*:?\s*", "", rest[0]).strip()
        chunk = [first] if first else []
        for x in rest[1:]:
            if not x.strip() or x.strip().startswith("#"):
                break
            chunk.append(x)
        for piece in re.split(r"\n|\s+•\s+|^•\s*", "\n".join(chunk)):
            name = re.sub(r"^[\s•\-*]*(?:(?:\d{1,2}:)?\d{1,2}:\d{2}\s+)?", "", piece).strip().rstrip(".… ")
            if len(name) > 2:
                music.append(name)
    note = ""
    n = re.search(r"(?is)About this drawing\s*\n(.+?)(?:\n\s*\n|$)", desc)
    if n:
        note = re.sub(r"\s+", " ", n.group(1)).strip()
    hours = ""
    h = re.search(r"([\d.]+)\s*hours? of (?:pencils and ink|drawing), sped up ([\d.]+)x", desc)
    if h:
        hours = f"({h.group(1)} hours of drawing, sped up {float(h.group(2)):g}x)"
    last = desc.rstrip().split("\n")[-1] if desc.strip() else ""
    tags = last.strip() if last.strip() and all(w.startswith("#") for w in last.split()) else TAGS_LINE
    return {"chapters": "\n".join(chapters), "music": music, "note": note, "hours": hours, "tags": tags}


def _squash(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def full_track_names(names: list[str], beats: list[str]) -> list[str]:
    """Song names as written in an old description (sometimes cut off) matched to the full names of
    the files in Drive › Beats; a name with no match is kept as it was."""
    out = []
    for name in names:
        key = _squash(name)
        hit = next((b for b in beats if _squash(b) == key), None) or \
            next((b for b in beats if key and len(key) >= 6 and _squash(b).startswith(key)), None)
        out.append(hit or name)
    return list(dict.fromkeys(out))


def rebuild_longdraw(cfg: dict, desc: str, label: str, subject: str, info: dict | None = None,
                     beats: list[str] | None = None, music_source: str = "YouTube Audio Library") -> str:
    """An older LongDraw description in the current layout, keeping its chapters, songs and hashtags."""
    old = parse_old(desc)
    info = info or {}
    topic = topic_line(info.get("subject") or subject, info.get("series", ""), old["hours"])
    return longdraw(cfg, label, topic, info.get("note") or old["note"], old["chapters"],
                    full_track_names(old["music"], beats or []), music_source, old["tags"])
