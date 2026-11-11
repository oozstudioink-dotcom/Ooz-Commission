"""Telegram commands that change what gets made: /redo, /retire, /ideas, /clear, /longform."""
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


class CommandsMixin:
    def redo(self, name: str, window: list | None = None) -> None:
        """Re-render a drawing's Shorts (e.g. after the editing improves). Unsent old versions
        leave the bank so the new ones replace them; already-dropped ones are untouched."""
        want = _norm_name(name)
        found = [(did, d) for did, d in self.state["drawings"].items()
                 if d["status"] in ("rendered", "failed", "held") and not self.retired(d)
                 and (not want or want in _norm_name(d.get("subject") or "")
                      or want in _norm_name(d.get("drive_name") or ""))]
        if not found:
            names = sorted({d.get("subject") or d.get("drive_name") or did
                            for did, d in self.state["drawings"].items() if d["status"] == "rendered"})
            self.say(f"🔎 No finished drawing matches “{name}”." if name else
                     "🔎 No finished drawings yet.")
            if names:
                self.say("Try one of: " + ", ".join(names[:20]))
            return
        did, d = max(found, key=lambda x: (x[1].get("received") or "", int(x[0][1:])))
        # every drawing made from the same recording (a page split into several drawings)
        same = [(k, x) for k, x in self.state["drawings"].items()
                if d.get("drive_id") and x.get("drive_id") == d["drive_id"] and k != did
                and x["status"] in ("rendered", "failed", "held", "queued")]
        if not window and not d.get("part"):
            d.pop("split_done", None)                     # look for separate drawings again
        if window:
            # your part of the recording: start over from it as one recording (it may be split again)
            for k, x in same:
                self.requeue(k, x)
                x["status"] = "replaced"
            same = []
            d["window"] = [round(window[0]), round(window[1])]
            for key in ("part", "split_done"):
                d.pop(key, None)
        replaced, styles = self.requeue(did, d)
        for k, x in same:
            r2, _ = self.requeue(k, x)
            replaced += r2
        label = d.get("subject") or d.get("drive_name") or did
        part = (f" using minutes {_mmss(window[0])}\u2013{_mmss(window[1])} of the recording" if window else "")
        self.say(f"\U0001f501 Re-making {label}{part} with the latest editing"
                 + (f" ({len(styles)} styles)" if styles else "") + ". It arrives within the hour"
                 + (f"; its {replaced} unsent old version(s) leave the bank." if replaced else "."))

    def redo_all(self) -> None:
        """/redo all: every finished drawing again with the current settings."""
        todo = [(did, d) for did, d in self.state["drawings"].items()
                if d["status"] in ("rendered", "failed") and not self.retired(d)]
        if not todo:
            return self.say("\U0001f50e No finished drawings yet.")
        replaced = videos = queued = 0
        for did, d in todo:
            r, styles = self.requeue(did, d, repeat_if_all_sent=False)
            replaced += r
            videos += len(styles)
            queued += bool(styles)
        if not queued:
            return self.say("\u2705 Every drawing's styles have already been posted, so there's nothing to re-make.")
        mins = float(self.cfg.get("shorts", {}).get("max_render_minutes", 40))
        self.say(f"\U0001f501 Re-making {queued} drawing(s) with the latest editing "
                 f"(about {videos} videos). {replaced} unsent old videos leave the bank and are replaced "
                 f"one for one; styles you already posted aren't repeated.\nThis runs in the background, "
                 f"about {mins:.0f} min of rendering per hourly run, so it can take a few hours. Each drawing "
                 "arrives as its own album, and I'll say \u201cAll caught up\u201d at the end.")

    def requeue(self, did: str, d: dict, repeat_if_all_sent: bool = True) -> tuple[int, list[str]]:
        """Queue one drawing for re-rendering. Returns (old versions replaced, styles to make);
        no styles = nothing queued."""
        old = [v for v in self.state["variants"].values() if v["drawing"] == did]
        unsent = [v for v in old if v["status"] in ("approved", "pending") and not v.get("dropped_at")]
        # Styles you already posted aren't re-made (no repeats in the feed); styles this drawing
        # never had (e.g. a new one) are added. If everything was posted, a single /redo re-makes
        # them all anyway (you asked for that drawing); /redo all skips it.
        made = {v["style"] for v in old}
        never = [st for st in self.cfg["variants"]["styles"] if st in STYLE_LABELS and st not in made]
        styles = [st for st in dict.fromkeys([v["style"] for v in unsent] + never) if st in STYLE_LABELS]
        if not styles and repeat_if_all_sent:
            styles = [st for st in dict.fromkeys(v["style"] for v in old) if st in STYLE_LABELS]
        # no more than per_drawing versions, picked like new drawings (least used, best liked first)
        weight, uses = self.style_weights(), self.state["style_uses"]
        styles.sort(key=lambda st: uses.get(st, 0) / weight.get(st, 1.0))
        styles = styles[: self.cfg["variants"].get("per_drawing", len(styles))]
        if not styles:
            return 0, []
        for v in unsent:
            v["status"] = "replaced"
        replaced = len(unsent)
        d.update(status="queued", redo=True, keep_in_place=True, redo_styles=styles or None)
        d.pop("error", None)
        d.pop("attempts", None)
        return replaced, styles

    def clear_chat(self, upto: int) -> None:
        """/clear: delete this chat's messages (videos and texts, yours too) going back from the
        newest. Telegram only lets bots delete messages from the last 48 hours; the video bank
        is unaffected (videos are kept by Telegram file id, not by message)."""
        misses, top = 0, upto
        while top > 0 and misses < 3:
            ids = list(range(top, max(0, top - 100), -1))
            misses = 0 if self.bot.delete_messages(self.owner, ids) else misses + 1
            top -= 100
        self.say("\U0001f9f9 Cleared what Telegram allows (the last 2 days). Your video bank and drops "
                 "are untouched.\nOlder messages can only be removed by you: open this chat \u2192 \u22ee "
                 "\u2192 Clear history (that's safe too: it doesn't touch the bot's memory).")

    # ------------------------------------------------------------------ LongDraw
    def longform_command(self, project: str) -> None:
        if not self.drive:
            self.say("LongDraw needs Google Drive connected (the drive scope on YT_REFRESH_TOKEN).")
            return
        try:
            folders = [f["name"] for f in self.drive.list(self.drive.folder("Longform"), "folder")
                       if f["name"] != "Finished"]
        except Exception as e:  # noqa: BLE001
            return self.drive_problem(e)
        match = next((n for n in folders if n.lower() == project.lower()), None) if project else None
        if not match:
            listing = "\n".join(f"\u2022 {n}" for n in folders) or "(none yet: make a folder in Drive \u203a Ooz Reels \u203a Longform)"
            hint = f"No project called \u201c{project}\u201d. " if project else ""
            self.say(f"{hint}Projects ready in Drive \u203a Longform:\n{listing}\n\nSend /longform <name> to build one.")
            return
        try:
            dispatch_workflow("longform.yml", {"project": match})
            self.say(f"\U0001f680 Started LongDraw for \u201c{match}\u201d. I'll message you when it's ready "
                     "(roughly 1 to 3 hours for 4K).")
        except Exception as e:  # noqa: BLE001
            self.say(f"\u26a0\ufe0f Couldn't start the LongDraw job: {str(e)[:200]}\n"
                     f"Start it by hand: GitHub \u203a Actions \u203a longform \u203a Run workflow, project = {match}")

    def retire_command(self, retire: bool, name: str) -> None:
        """/retire <name>: never render, drop or re-make drawings whose name (or file name) contains it.
        /unretire <name> undoes it. Their files in Drive are left alone."""
        lst = self.state.setdefault("retired", [])
        if not name:
            listed = self.cfg.get("bank", {}).get("retired", []) + lst
            return self.say("Retired: " + (", ".join(listed) if listed else "nothing yet") +
                            "\nUse /retire <name> or /unretire <name>.")
        if retire:
            if name.lower() not in [n.lower() for n in lst]:
                lst.append(name)
            hit = [d.get("subject") or d.get("drive_name") for d in self.state["drawings"].values()
                   if self.retired(d)]
            self.say(f"\U0001f4e6 Retired \u201c{name}\u201d: it won't be rendered, dropped or re-made again"
                     + (f" ({len(hit)} drawing(s): {', '.join(sorted(set(map(str, hit))))})." if hit else
                        ". No drawing matches that name yet."))
        else:
            before = len(lst)
            lst[:] = [n for n in lst if n.lower() != name.lower()]
            in_cfg = name.lower() in [n.lower() for n in self.cfg.get("bank", {}).get("retired", [])]
            self.say(f"\u267b\ufe0f \u201c{name}\u201d is back in the rotation." if len(lst) < before else
                     (f"\u201c{name}\u201d is retired in config.toml ([bank] retired); remove it there." if in_cfg
                      else f"\u201c{name}\u201d wasn't retired."))

    def save_ideas(self, text: str) -> None:
        """/ideas + pasted lines (e.g. from the Gemini app): short lines become on-screen hooks,
        longer ones captions. /ideas clear empties the list."""
        pool = self.state.setdefault("owner_ideas", {"hooks": [], "captions": []})
        if text.strip().lower() == "clear":
            pool["hooks"], pool["captions"] = [], []
            return self.say("\U0001f9f9 Cleared your saved hooks and captions.")
        added_h = added_c = 0
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)]|hook:|caption:)\s*", "", line, flags=re.I).strip().strip('"\u201c\u201d')
            if not line or line.endswith(":"):
                continue
            if len(line) <= 45 and line not in pool["hooks"]:
                pool["hooks"].append(line)
                added_h += 1
            elif 45 < len(line) <= 220 and line not in pool["captions"]:
                pool["captions"].append(line)
                added_c += 1
        pool["hooks"], pool["captions"] = pool["hooks"][-80:], pool["captions"][-40:]
        if not (added_h or added_c):
            return self.say("Paste your lines after /ideas, one per line (short = on-screen hook, "
                            "long = caption). Example:\n/ideas\nWait for the last line\nInked this one live on stream")
        self.say(f"\U0001f4a1 Saved {added_h} hook(s) and {added_c} caption(s) "
                 f"({len(pool['hooks'])} hooks, {len(pool['captions'])} captions in total). "
                 "New videos mix them in with the AI ones; they're still checked for false claims.")

    def with_owner_ideas(self, copy: dict | None, styles: list[str], did: str) -> dict | None:
        """Mix your /ideas lines into this drawing's copy: about half the styles get one of your hooks."""
        pool = self.state.get("owner_ideas") or {}
        hooks, caps = pool.get("hooks") or [], pool.get("captions") or []
        if not (hooks or caps):
            return copy
        rng = random.Random(f"ideas-{did}")
        copy = dict(copy or {})
        copy["hooks"] = dict(copy.get("hooks") or {})
        picks = rng.sample(hooks, min(len(hooks), len(styles)))
        for style, hook in zip(rng.sample(styles, len(styles)), picks):
            if rng.random() < 0.5:
                copy["hooks"][style] = hook
        if caps and rng.random() < 0.5:
            copy["caption"] = rng.choice(caps)
        return copy
