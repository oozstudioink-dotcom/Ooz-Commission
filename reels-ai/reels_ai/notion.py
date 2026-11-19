"""The Content Pipeline in Notion, kept up to date by the bot.

record_short     you tap 📤 Uploaded on a Short: its row (planned for that character, else today's
                 empty Short slot, else a new row) is marked Published with today's date
record_longdraw  a LongDraw is built and uploaded privately: the next empty LongDraw slot gets its
                 character, episode and YouTube link (Status: Editing), and you're told which day
                 to schedule it for
sync_youtube     every few hours: LongDraws you scheduled in YouTube Studio become Scheduled with
                 that date, and Published once they're public

Needs the NOTION_TOKEN secret (an internal integration) with the Content Pipeline shared to it.
Nothing is ever deleted; only the rows named above are written.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import date

API = "https://api.notion.com/v1"
DONE = ("Published", "Archived")
SHORT_PLATFORMS = ["YouTube Shorts", "TikTok", "Instagram Reel"]


class NotionError(RuntimeError):
    pass


class Notion:
    def __init__(self, token: str, database_id: str, data_source_id: str = "", timeout: int = 30):
        self.token = re.sub(r"\s+", "", token or "")
        if not self.token:
            raise NotionError("NOTION_TOKEN is empty")
        self.db, self.ds, self.timeout = database_id, data_source_id, timeout
        self.mode = "ds" if data_source_id else "db"     # newer API: data sources; older: databases

    def _req(self, method: str, path: str, body: dict | None = None, version: str = "2022-06-28",
             again: bool = True) -> dict:
        from .drive import _retry
        req = urllib.request.Request(f"{API}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Notion-Version": version,
                                              "Content-Type": "application/json"})
        try:
            with _retry(lambda: urllib.request.urlopen(req, timeout=self.timeout), tries=4 if again else 1) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("message", "")
            except ValueError:
                msg = ""
            raise NotionError(f"HTTP {e.code}: {msg or e.reason}") from None

    def _ds_or_db(self, ds_call, db_call):
        if self.mode == "ds":
            try:
                return ds_call()
            except NotionError as e:
                if not re.match(r"HTTP (400|404)", str(e)):
                    raise
                self.mode = "db"                          # this workspace's API doesn't know data sources
        return db_call()

    def query(self, filter: dict | None = None) -> list[dict]:
        pages, cursor = [], None
        while True:
            body = {"page_size": 100, **({"filter": filter} if filter else {}),
                    **({"start_cursor": cursor} if cursor else {})}
            res = self._ds_or_db(lambda: self._req("POST", f"/data_sources/{self.ds}/query", body, "2025-09-03"),
                                 lambda: self._req("POST", f"/databases/{self.db}/query", body))
            pages += res.get("results", [])
            if not res.get("has_more") or len(pages) >= 500:
                return pages
            cursor = res.get("next_cursor")

    def update(self, page_id: str, props: dict) -> None:
        self._req("PATCH", f"/pages/{page_id}", {"properties": props})

    def create(self, props: dict) -> None:
        self._ds_or_db(
            lambda: self._req("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": self.ds},
                                                 "properties": props}, "2025-09-03", again=False),
            lambda: self._req("POST", "/pages", {"parent": {"database_id": self.db}, "properties": props},
                              again=False))                 # a new row is never sent twice


# ------------------------------------------------------------------ properties
def p_title(t: str) -> dict:
    return {"title": [{"text": {"content": t[:200]}}]}


def p_text(t: str) -> dict:
    return {"rich_text": [{"text": {"content": t[:1900]}}] if t else []}


def p_select(name: str) -> dict:
    return {"select": {"name": name}}


def p_date(start: str) -> dict:
    return {"date": {"start": start}}


def read(page: dict, name: str):
    """A property's plain value: text, select name, number, url or date start ("" / None when empty)."""
    prop = (page.get("properties") or {}).get(name) or {}
    kind = prop.get("type") or next((k for k in ("title", "rich_text", "select", "number", "url", "date")
                                     if k in prop), "")
    val = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(x.get("plain_text") or x.get("text", {}).get("content", "") for x in val or [])
    if kind == "select":
        return (val or {}).get("name", "")
    if kind == "date":
        return (val or {}).get("start", "") or ""
    return val


def squash(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def day_of(page: dict) -> str:
    return (read(page, "Publish Date") or "")[:10]


def label(d: str) -> str:
    return date.fromisoformat(d).strftime("%a %b %-d") if d else "no date"


# ------------------------------------------------------------------ what the bot does
def record_short(nt, character: str, today: date) -> str:
    """A Short you uploaded today, in the pipeline. Returns what was done, for Telegram."""
    t, key = today.isoformat(), squash(character)
    rows = [p for p in nt.query({"or": [{"property": "Format", "select": {"equals": "Short"}},
                                         {"property": "Format", "select": {"is_empty": True}}]})
            if read(p, "Format") in ("Short", "", None)]
    if key and any(squash(read(p, "Character")) == key and read(p, "Status") == "Published" and day_of(p) == t
                   for p in rows):
        return f"{character} is already marked Published today"
    open_ = sorted((p for p in rows if read(p, "Status") not in DONE), key=lambda p: day_of(p) or "9999")
    planned = [p for p in open_ if key and squash(read(p, "Character")) == key]
    if planned:
        p = planned[0]
        nt.update(p["id"], {"Status": p_select("Published"), "Format": p_select("Short"),
                            "Publish Date": p_date(t if day_of(p) != t else read(p, "Publish Date"))})
        return f"“{read(p, 'Title') or character}” marked Published ({label(t)})"
    title = f"Short · {character} · {label(t)}"
    slot = [p for p in open_ if not read(p, "Character").strip() and day_of(p) == t]
    if slot:
        nt.update(slot[0]["id"], {"Title": p_title(title), "Character": p_text(character),
                                  "Status": p_select("Published"), "Format": p_select("Short")})
        return f"today's Short slot filled with {character} and marked Published"
    nt.create({"Title": p_title(title), "Format": p_select("Short"), "Character": p_text(character),
               "Status": p_select("Published"), "Publish Date": p_date(t),
               "Platform": {"multi_select": [{"name": n} for n in SHORT_PLATFORMS]}})
    return f"added {character} as a Short published {label(t)} (there was no slot for today)"


def record_longdraw(nt, character: str, episode: int, youtube_url: str, today: date) -> str:
    """A LongDraw uploaded privately: into the next empty LongDraw slot. Returns what was done."""
    rows = [p for p in nt.query({"property": "Format", "select": {"equals": "LongDraw"}})
            if read(p, "Format") == "LongDraw"]
    if youtube_url and any(read(p, "YouTube") == youtube_url for p in rows):
        return "it's already in the pipeline"
    open_ = sorted((p for p in rows if read(p, "Status") not in DONE), key=lambda p: day_of(p) or "9999")
    key = squash(character)
    planned = [p for p in open_ if key and squash(read(p, "Character")) == key]
    empty = [p for p in open_ if not read(p, "Character").strip() and (day_of(p) or "") >= today.isoformat()]
    props = {"Title": p_title(f"LongDraw EP. {episode:02d} · {character}"), "Character": p_text(character),
             "Episode": {"number": episode}, "Status": p_select("Editing"),
             "Notes": p_text("Built and uploaded privately by the bot. Schedule it for this slot in YouTube "
                             "Studio; the row turns Scheduled, then Published, by itself.")}
    if youtube_url:
        props["YouTube"] = {"url": youtube_url}
    target = (planned or empty or [None])[0]
    if target:
        nt.update(target["id"], props)
        when = day_of(target)
        return (f"EP. {episode:02d} · {character} is in the {label(when)} LongDraw slot: schedule it on YouTube "
                f"for {label(when)}, 12 PM EST" if when else f"EP. {episode:02d} · {character} updated")
    nt.create({**props, "Format": p_select("LongDraw"), "Platform": {"multi_select": [{"name": "YouTube Long-form"}]}})
    return f"EP. {episode:02d} · {character} added (there was no empty LongDraw slot: add more in Notion)"


def video_id(url: str) -> str:
    m = re.search(r"(?:youtu\.be/|v=|/shorts/|/live/)([\w-]{6,})", url or "")
    return m.group(1) if m else ""


def sync_youtube(nt, videos: list[dict]) -> list[str]:
    """LongDraw rows follow YouTube: scheduled there -> Scheduled with that date; public -> Published."""
    by_id = {v["id"]: v for v in videos if v.get("seconds", 0) > 90}
    changed = []
    for p in nt.query({"and": [{"property": "Format", "select": {"equals": "LongDraw"}},
                               {"property": "YouTube", "url": {"is_not_empty": True}}]}):
        v = by_id.get(video_id(read(p, "YouTube") or ""))
        if not v:
            continue
        if v.get("privacy", "public") == "public" and v.get("published"):
            status, when = "Published", v["published"]
        elif v.get("publish_at"):
            status, when = "Scheduled", v["publish_at"]
        else:
            continue
        if read(p, "Status") != status or day_of(p) != when[:10]:
            nt.update(p["id"], {"Status": p_select(status), "Publish Date": p_date(when)})
            changed.append(f"{read(p, 'Title') or v.get('title', '')}: {status} {label(when[:10])}")
    return changed


def open_slots(nt, today: date) -> dict[str, list[str]]:
    """The next empty slots, for /notion."""
    out: dict[str, list[str]] = {"Short": [], "LongDraw": []}
    for p in sorted(nt.query(), key=lambda p: day_of(p) or "9999"):
        f = read(p, "Format")
        if f in out and read(p, "Status") not in DONE and not read(p, "Character").strip() \
                and (day_of(p) or "") >= today.isoformat() and len(out[f]) < 5:
            out[f].append(label(day_of(p)))
    return out


def from_env(cfg: dict):
    """The pipeline client, or None when Notion isn't set up ([notion] in config.toml + NOTION_TOKEN)."""
    import os
    nc = cfg.get("notion", {})
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not nc.get("enabled", True) or not token or not nc.get("database_id"):
        return None
    return Notion(token, nc["database_id"], nc.get("data_source_id", ""))
