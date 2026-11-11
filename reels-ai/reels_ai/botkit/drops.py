"""The bank and the daily drops: what is ready, what goes out when, approvals and retirements."""
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
from .common import (  # noqa: F401
    STYLE_LABELS, PLATFORM_LABELS, MAX_BOT_DOWNLOAD, HELP, pending_markup, rating_markup,
    label_markup, _time_range, _mmss, _norm_name, strip_guessed_names, ALBUM_BYTES, album_chunks,
    dispatch_workflow, shrink_for_telegram,
)


class DropsMixin:
    def minutes_to_drop(self) -> float | None:
        """Minutes until the next drop time today (None when there is none left today)."""
        if not self.drops_mode:
            return None
        lt = self.local(self.clock())
        waits = []
        for hhmm in self.drop_times:
            at = datetime.strptime(hhmm, "%H:%M").time()
            due = lt.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
            if due > lt:
                waits.append((due - lt).total_seconds() / 60)
        return min(waits) if waits else None

    def decide(self, v: dict, status: str, by: str) -> None:
        v["status"] = status
        v["decided_by"] = by
        v["decided_at"] = iso(self.clock())

    def auto_approve(self) -> None:
        t = self.clock()
        for vid, v in self.state["variants"].items():
            if v["status"] == "pending" and v.get("deadline") and parse(v["deadline"]) <= t:
                self.decide(v, "approved", "auto")
                self.bot.edit_markup(self.owner, v["message_id"],
                                     label_markup("⏱ Auto-approved: queued to post"))

    # ------------------------------------------------------------------ drops (manual posting)
    def retired(self, d: dict | None) -> bool:
        """Drawings you've used enough ([bank] retired in config.toml): no more drops or redos."""
        names = [_norm_name(n) for n in self.cfg.get("bank", {}).get("retired", []) + self.state.get("retired", [])
                 if n.strip()]
        text = _norm_name(f"{(d or {}).get('subject') or ''} {(d or {}).get('drive_name') or ''}")
        return any(n in text for n in names)

    def retired_variant(self, v: dict) -> bool:
        return self.retired(self.state["drawings"].get(v.get("drawing")))

    @property
    def drops_mode(self) -> bool:
        return self.cfg.get("delivery", {}).get("mode") == "drops"

    @property
    def drop_times(self) -> list[str]:
        return self.cfg.get("delivery", {}).get("drop_times", ["17:00"])

    @property
    def split(self) -> list[str]:
        """Platforms that each get their own version of a drawing per drop ([delivery] split)."""
        return [p for p in self.cfg.get("delivery", {}).get("split", []) if p in PLATFORM_LABELS]

    def drop_due(self) -> None:
        """Deliver one drop per drop time, shuffled, for the owner to post."""
        lt = self.local(self.clock())
        open_slots = sum(1 for hhmm in self.drop_times
                         if lt.time() >= datetime.strptime(hhmm, "%H:%M").time())
        today = [v for v in self.state["variants"].values()
                 if v.get("dropped_at") and self.local(parse(v["dropped_at"])).date() == lt.date()]
        done_today = len({v.get("drop_no") for v in today}) if self.split else len(today)
        while done_today < open_slots:
            if self.split:
                pairs = self.next_split_drop()
                if not pairs:
                    return
                self.deliver_split(pairs)
            else:
                v = self.next_drop()
                if not v:
                    return
                self.deliver(v)
            done_today += 1

    def split_ready(self) -> dict[str, list[str]]:
        """Drawings that can still be dropped: {drawing: [kept, unsent variant ids]}. A drawing
        that already went out (in any drop, old style included) is used up: no repeats."""
        used = {v["drawing"] for v in self.state["variants"].values() if v.get("dropped_at")}
        ready: dict[str, list[str]] = {}
        for vid, v in self.state["variants"].items():
            if (v["status"] == "approved" and not v.get("dropped_at") and v["drawing"] not in used
                    and not self.retired_variant(v)):
                ready.setdefault(v["drawing"], []).append(vid)
        return ready

    def next_split_drop(self) -> list[tuple[str, str]]:
        """One drawing, a different version for each platform: [(platform, variant id)].
        Drawings with a full set first (shuffled); each platform gets the style it has had least."""
        ready = self.split_ready()
        if not ready:
            return []
        sent = len({v.get("drop_no") for v in self.state["variants"].values() if v.get("dropped_at")})
        rng = random.Random(f"{self.local(self.clock()).date()}-{sent}")
        full = max(min(len(v), len(self.split)) for v in ready.values())
        did = rng.choice(sorted(d for d, v in ready.items() if min(len(v), len(self.split)) == full))
        uses = self.state.setdefault("platform_style_uses", {})
        left = list(ready[did])
        pairs = []
        for p in self.split:
            if not left:
                break
            counts = uses.get(p, {})
            vid = min(left, key=lambda k: (counts.get(self.state["variants"][k]["style"], 0), rng.random()))
            left.remove(vid)
            pairs.append((p, vid))
        return pairs

    def deliver_split(self, pairs: list[tuple[str, str]]) -> None:
        variants = self.state["variants"]
        n = max((x.get("drop_no", 0) for x in variants.values()), default=0) + 1
        first = variants[pairs[0][1]]
        did = first["drawing"]
        subject = self.state["drawings"].get(did, {}).get("subject") or "this piece"
        caption = first["caption"]
        uses = self.state.setdefault("platform_style_uses", {})
        for p, vid in pairs:
            v = variants[vid]
            self.bot.send_video(self.owner, v["file_id"], caption=(
                f"\U0001f3ac Drop #{n} \u00b7 {subject} \u00b7 for {PLATFORM_LABELS[p]}\n"
                f"{STYLE_LABELS.get(v['style'], v['style'])} \u00b7 {v.get('seconds', '?')}s \u00b7 "
                f"hook on video: \u201c{v.get('hook', '')}\u201d\n"
                + (f"\U0001f39e 4K file: {v['full_link']}\n" if v.get("full_link") else "")
                + "\U0001f3b5 Add a trending sound when you post."), reply_markup=rating_markup(vid))
            v.update(dropped_at=iso(self.clock()), drop_no=n, status="posted", platform=p, caption=caption)
            uses.setdefault(p, {})[v["style"]] = uses.get(p, {}).get(v["style"], 0) + 1
        missing = [PLATFORM_LABELS[p] for p in self.split[len(pairs):]]
        self.say(caption + ("\n\n(No version for " + ", ".join(missing) + " this time.)" if missing else ""))
        for vid in self.split_ready().get(did, []):           # this drawing is done: spares stay out
            variants[vid]["status"] = "spare"

    def next_drop(self) -> dict | None:
        """Shuffled, but spread out: never the same drawing back-to-back when avoidable, prefer a
        different style than last time, and draw from the drawings with the most videos left so
        one drawing's variants don't pile up at the end of the bank."""
        pool = [v for v in self.state["variants"].values()
                if v["status"] == "approved" and not v.get("dropped_at") and not self.retired_variant(v)]
        if not pool:
            return None
        sent = [v for v in self.state["variants"].values() if v.get("dropped_at")]
        last = max(sent, key=lambda v: v.get("drop_no", 0)) if sent else None
        rng = random.Random(f"{self.local(self.clock()).date()}-{len(sent)}")
        if last:
            other = [v for v in pool if v["drawing"] != last["drawing"]]
            pool = other or pool
        left = {}
        for v in pool:
            left[v["drawing"]] = left.get(v["drawing"], 0) + 1
        most = max(left.values())
        pool = [v for v in pool if left[v["drawing"]] == most]
        if last:
            fresh_style = [v for v in pool if v["style"] != last["style"]]
            pool = fresh_style or pool
        pool.sort(key=lambda v: (v["drawing"], v["style"]))
        return rng.choice(pool)

    def deliver(self, v: dict) -> None:
        n = max((x.get("drop_no", 0) for x in self.state["variants"].values()), default=0) + 1
        subject = self.state["drawings"].get(v["drawing"], {}).get("subject") or "this piece"
        where = self.cfg.get("delivery", {}).get("post_to", "TikTok \u00b7 Reels \u00b7 Shorts")
        self.bot.send_video(self.owner, v["file_id"], caption=(
            f"\U0001f3ac Drop #{n}: {subject} \u00b7 {STYLE_LABELS.get(v['style'], v['style'])}"
            f" \u00b7 {v.get('seconds', '?')}s\nHook on video: \u201c{v.get('hook', '')}\u201d\n\n"
            f"Post to: {where}\n\U0001f3b5 Add a trending sound when you post.\n"
            + (f"\U0001f39e 4K file to post: {v['full_link']}\n" if v.get("full_link") else "")
            + "Caption is in the next message."), reply_markup=rating_markup(next((k for k, x in self.state["variants"].items() if x is v), "")))
        self.say(v["caption"])
        v["dropped_at"] = iso(self.clock())
        v["drop_no"] = n
        v["status"] = "posted"

    # ------------------------------------------------------------------ bank
    def per_day(self, key: str) -> int:
        if key == "drops":
            return max(1, len(self.drop_times))
        return max(1, self.cfg["platforms"][key].get("max_per_day", 1))

    def bank(self) -> dict[str, int]:
        """Approved (or still pending) videos not yet sent to each enabled platform."""
        if self.drops_mode and self.split:                   # drawings ready (each drop = one drawing)
            used = {v["drawing"] for v in self.state["variants"].values() if v.get("dropped_at")}
            return {"drops": len({v["drawing"] for v in self.state["variants"].values()
                                  if v["status"] in ("approved", "pending") and not v.get("dropped_at")
                                  and v["drawing"] not in used and not self.retired_variant(v)})}
        if self.drops_mode:
            return {"drops": sum(1 for v in self.state["variants"].values()
                                 if v["status"] in ("approved", "pending") and not v.get("dropped_at")
                                 and not self.retired_variant(v))}
        out = {}
        for p, pc in self.cfg.get("platforms", {}).items():
            if pc.get("enabled"):
                out[p] = sum(1 for v in self.state["variants"].values()
                             if v["status"] in ("approved", "pending", "posted") and p not in v.get("posts", {}))
        return out

    def queue_text(self) -> str:
        lines = ["\U0001f3e6 Video bank"]
        for p, n in self.bank().items():
            label = "Drops" if p == "drops" else PLATFORM_LABELS.get(p, p)
            unit = "drawings" if p == "drops" and self.split else "videos"
            lines.append(f"{label}: {n} {unit} \u2248 {n // self.per_day(p)} days")
        pending = sum(1 for v in self.state["variants"].values() if v["status"] == "pending")
        if pending:
            lines.append(f"({pending} still waiting for your OK are counted)")
        queued = sum(1 for d in self.state["drawings"].values() if d["status"] == "queued")
        if queued:
            lines.append(f"{queued} drawing(s) waiting to be turned into variants")
        return "\n".join(lines)

    def warn_low_bank(self) -> None:
        low_at = self.cfg.get("bank", {}).get("warn_below_days", 3)
        today = self.local(self.clock()).date().isoformat()
        if self.state.get("low_bank_warned") == today or not self.state["variants"]:
            return
        short = []
        for p, n in self.bank().items():
            days = n // self.per_day(p)
            if days < low_at:
                label = "Drops" if p == "drops" else PLATFORM_LABELS.get(p, p)
                unit = "drawings" if p == "drops" and self.split else "videos"
                short.append(f"{label}: {days} day(s) left ({n} {unit})")
        if short:
            self.state["low_bank_warned"] = today
            self.say("\U0001faab Video bank is running low\n" + "\n".join(short) +
                     "\nSend me your next page of timelapses when you can.")
