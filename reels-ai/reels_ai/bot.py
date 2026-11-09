"""The approval bot. Runs once per GitHub Actions tick (hourly), in two phases:

poll   (stdlib only, ~seconds): read Telegram, handle taps/commands, take in new videos,
                                auto-approve past the deadline, post anything due.
render (needs ffmpeg/OpenCV):   turn newly received timelapses into variants and send them
                                to you for approval. Only runs when there is something to render.
"""
from __future__ import annotations

import json
import os
import random
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .state import iso, load_config, load_state, new_id, now, parse, save_state
from .telegram import Bot, TelegramError

STYLE_LABELS = {
    "reveal_first": "Reveal first",
    "speedrun": "Speedrun",
    "before_after": "Before / after",
    "detail_zoom": "Detail zoom",
    "ink_only": "Ink only",
    "process_cuts": "Process cuts",
    "one_take": "One take",
    "page_compilation": "Page compilation",
}
PLATFORM_LABELS = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "tiktok": "TikTok"}
MAX_BOT_DOWNLOAD = 20 * 1024 * 1024   # Telegram bots can only download files up to 20 MB

HELP = (
    "Send me a drawing timelapse (as a video) and I'll make variants for you to approve.\n"
    "Add the character name as the video's caption, e.g. Naruto.\n\n"
    "/status  overview\n/longform [project]  build a LongDraw from Drive \u203a Longform (no name = list projects)\n/redo [name]  re-make a drawing's Shorts with the latest editing (no name = newest; all = every drawing)\n/clear  delete recent chat messages (Telegram allows the last 2 days)\n/compile [title]  stitch the drawings since the last compilation into one page video\n/report  stats report now (also sent every Sunday)\n/queue  your video bank and how many days it covers\n/pause  stop all posting\n/resume  start posting again\n/help  this message"
)


def pending_markup(vid: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Post", "callback_data": f"ok:{vid}"},
         {"text": "❌ Skip", "callback_data": f"no:{vid}"}],
        [{"text": "✏️ Edit caption", "callback_data": f"ed:{vid}"}],
    ]}


def label_markup(text: str) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": "noop"}]]}


class Runner:
    def __init__(self, bot: Bot, state: dict, config: dict, clock=now, clients: dict | None = None,
                 drive=None):
        self.bot, self.state, self.cfg, self.clock = bot, state, config, clock
        self.clients = clients or {}          # platform -> API client (e.g. Instagram)
        self.drive = drive                    # Google Drive inbox / backups (optional)
        self.tz = ZoneInfo(config.get("timezone", "UTC"))
        owner = os.environ.get("TELEGRAM_OWNER_ID")
        if owner:
            state["owner_chat_id"] = int(owner)

    # ------------------------------------------------------------------ helpers
    @property
    def owner(self) -> int | None:
        return self.state["owner_chat_id"]

    def say(self, text: str, **kw) -> dict | None:
        if self.owner is None:
            return None
        return self.bot.send_message(self.owner, text, **kw)

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    def fmt_time(self, dt: datetime) -> str:
        return self.local(dt).strftime("%a %-I:%M %p")

    # ================================================================== POLL
    def poll(self) -> bool:
        """Handle everything that doesn't need video tools. Returns True if rendering is needed."""
        updates = self.bot.get_updates(self.state["update_offset"])
        for u in updates:
            self.state["update_offset"] = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except TelegramError as e:
                print(f"warn: update {u['update_id']}: {e}")
        if not self.state["paused"]:
            self.auto_approve()
            if self.drops_mode:
                self.drop_due()
            else:
                self.post_due()
            self.warn_low_bank()
        self.check_drive_inbox()
        self.storage_check()
        self.refresh_stats()
        self.weekly_report()
        return (any(d["status"] == "queued" for d in self.state["drawings"].values())
                or bool(self.state.get("compile_request")))

    # ------------------------------------------------------------------ messages
    def on_message(self, m: dict) -> None:
        chat = m["chat"]
        if chat.get("type") != "private":
            return
        if self.owner is None:
            self.state["owner_chat_id"] = chat["id"]
            self.say("\U0001f44b You're connected. Only you can use this bot.\n\n" + HELP)
            if not (m.get("video") or m.get("document")):
                return
        if chat["id"] != self.owner:
            self.bot.send_message(chat["id"], "This is a private bot.")
            return

        doc = m.get("document") or {}
        video = m.get("video") or (doc if str(doc.get("mime_type", "")).startswith("video/") else None)
        if video:
            return self.intake(video, (m.get("caption") or "").strip())

        text = (m.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd in ("/start", "/help"):
            self.say(HELP)
        elif cmd == "/pause":
            self.state["paused"] = True
            self.say("⏸ Paused. Nothing will post or auto-approve until you send /resume.")
        elif cmd == "/resume":
            self.state["paused"] = False
            self.say("▶️ Resumed.")
        elif cmd == "/status":
            self.say(self.status_text())
        elif cmd == "/queue":
            self.say(self.queue_text())
        elif cmd == "/compile":
            pending = self.uncompiled()
            if len(pending) < 2:
                self.say("\U0001f4da I need at least 2 drawings since the last compilation. Send me more timelapses first.")
            else:
                title = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
                self.state["compile_request"] = {"title": title, "at": iso(self.clock())}
                self.say(f"\U0001f3ac Compiling {len(pending)} drawings into a page video on the next run.")
        elif cmd == "/longform":
            self.longform_command(text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else "")
        elif cmd == "/redo":
            arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            self.redo_all() if arg.lower() == "all" else self.redo(arg)
        elif cmd == "/clear":
            self.clear_chat(int(m.get("message_id") or 0))
        elif cmd == "/id":
            self.say(f"Your chat ID: {chat['id']}\n(For the commission-alerts script's TELEGRAM_CHAT_ID.)")
        elif cmd == "/report":
            self.state["report_request"] = True
            self.send_report()
        elif text and self.state["awaiting_caption"]:
            vid = self.state["awaiting_caption"]
            self.state["awaiting_caption"] = None
            v = self.state["variants"].get(vid)
            if v:
                v["caption"] = text
                tail = " Tap ✅ Post on the video when you're ready." if v["status"] == "pending" else ""
                self.say(f"✅ Caption updated for {STYLE_LABELS[v['style']]} ({vid}).{tail}")
        elif text:
            self.say("Send me a timelapse video, or /help.")

    def redo(self, name: str) -> None:
        """Re-render a drawing's Shorts (e.g. after the editing improves). Unsent old versions
        leave the bank so the new ones replace them; already-dropped ones are untouched."""
        want = name.lower()
        found = [(did, d) for did, d in self.state["drawings"].items()
                 if d["status"] in ("rendered", "failed")
                 and (not want or want in (d.get("subject") or "").lower()
                      or want in (d.get("drive_name") or "").lower())]
        if not found:
            names = sorted({d.get("subject") or d.get("drive_name") or did
                            for did, d in self.state["drawings"].items() if d["status"] == "rendered"})
            self.say(f"🔎 No finished drawing matches “{name}”." if name else
                     "🔎 No finished drawings yet.")
            if names:
                self.say("Try one of: " + ", ".join(names[:20]))
            return
        did, d = max(found, key=lambda x: (x[1].get("received") or "", int(x[0][1:])))
        replaced, styles = self.requeue(did, d)
        label = d.get("subject") or d.get("drive_name") or did
        self.say(f"\U0001f501 Re-making {label} with the latest editing"
                 + (f" ({len(styles)} styles)" if styles else "") + ". It arrives within the hour"
                 + (f"; its {replaced} unsent old version(s) leave the bank." if replaced else "."))

    def redo_all(self) -> None:
        """/redo all: every finished drawing again with the current settings."""
        todo = [(did, d) for did, d in self.state["drawings"].items() if d["status"] in ("rendered", "failed")]
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
        if not styles:
            return 0, []
        for v in unsent:
            v["status"] = "replaced"
        replaced = len(unsent)
        d.update(status="queued", redo=True, keep_in_place=True, redo_styles=styles or None)
        d.pop("error", None)
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

    # ------------------------------------------------------------------ Google Drive inbox
    def drive_problem(self, e: Exception) -> None:
        """Tell the owner about a Drive problem once per distinct message, not every run."""
        msg = str(e)[:200]
        print(f"warn: drive: {msg}")
        if self.state.get("drive_error") != msg:
            self.state["drive_error"] = msg
            self.say(f"\u26a0\ufe0f Google Drive: {msg}")

    def check_drive_inbox(self) -> None:
        """New videos in Inbox, and (shorts_from_longform) in Longform project folders, become
        Shorts. Longform sessions stay where they are so the LongDraw can use them too."""
        if not self.drive:
            return
        from .drive import describe_hints, parse_name
        sources = []                                    # (file, folder label, keep in place, folder name)
        try:
            for f in self.drive.list(self.drive.folder("Inbox"), "video"):
                sources.append((f, "Inbox", False, None))
            if self.cfg.get("drive", {}).get("shorts_from_longform", True):
                for proj in self.drive.list(self.drive.folder("Longform"), "folder"):
                    if proj["name"] == "Finished":
                        continue
                    for f in self.drive.list(proj["id"], "video"):
                        sources.append((f, f"Longform \u203a {proj['name']}", True, proj["name"]))
        except Exception as e:  # noqa: BLE001 - never let Drive break Telegram handling
            return self.drive_problem(e)
        self.state.pop("drive_error", None)
        seen = self.state.setdefault("drive_seen", [])
        new = [src for src in sources if src[0]["id"] not in seen]
        labels: dict[str, str] = {}
        for f, where, keep, project in new:
            meta = f.get("videoMediaMetadata") or {}
            did = new_id(self.state, "d")
            subject, hints = parse_name(f["name"])
            if project:                                  # LongDraw session: the folder names the piece
                p_subject, p_hints = parse_name(project)
                subject = p_subject or subject
                hints = {**p_hints, **hints}
            self.state["drawings"][did] = {
                "source": "drive", "drive_id": f["id"], "drive_name": f["name"], "drive_where": where,
                "keep_in_place": keep, "subject": subject, "received": iso(self.clock()),
                "duration": (int(meta["durationMillis"]) / 1000) if meta.get("durationMillis") else None,
                "size_gb": round(int(f.get("size") or 0) / 1e9, 2), "status": "queued",
                **({"hints": {k: v for k, v in hints.items() if k != "unknown"}} if hints else {})}
            seen.append(f["id"])
            how = describe_hints(hints)
            odd = f" (didn't understand: {', '.join(hints['unknown'])})" if hints.get("unknown") else ""
            labels[f["id"]] = (f"{f['name']} \u2192 {subject or 'name unknown'}"
                               f"{' \u00b7 ' + how if how else ''}{odd}")
        if new:
            by_place: dict[str, list[str]] = {}
            for f, where, _, _ in new:
                by_place.setdefault(where, []).append(labels[f["id"]])
            more = " \u2026"
            lines = [f"\u2022 {where}:\n   " + "\n   ".join(n[:8]) + (more if len(n) > 8 else "")
                     for where, n in by_place.items()]
            self.say(f"\U0001f4e5 Found {len(new)} new recording(s) in Drive:\n" + "\n".join(lines) +
                     "\nTurning them into 4K Shorts now (LongDraw sessions stay in their folder).")

    # ------------------------------------------------------------------ storage
    def storage_check(self) -> None:
        """Monthly: how much space raw recordings use, and Drive's overall usage. Never deletes."""
        if not self.drive:
            return
        month = self.local(self.clock()).strftime("%Y-%m")
        if self.state.get("storage_month") == month:
            return
        try:
            raw = sum(int(f.get("size") or 0) for f in self.drive.list(self.drive.folder("Inbox/Processed"), "video"))
            fin = 0
            for proj in self.drive.list(self.drive.folder("Longform/Finished"), "folder"):
                fin += sum(int(f.get("size") or 0) for f in self.drive.list(proj["id"], "video"))
            quota = self.drive.about().get("storageQuota", {})
        except Exception as e:  # noqa: BLE001
            print(f"warn: storage check: {e}")
            return
        self.state["storage_month"] = month
        warn_gb = float(self.cfg.get("drive", {}).get("storage_alert_gb", 50))
        if (raw + fin) / 1e9 < warn_gb:
            return
        used, limit = int(quota.get("usage") or 0), int(quota.get("limit") or 0)
        total = (f"\nDrive overall: {used / 1e12:.2f} TB of {limit / 1e12:.0f} TB used." if limit else "")
        self.say("\U0001f9f9 Monthly storage check\n"
                 f"\u2022 Inbox \u203a Processed (raw clips already made into Shorts): {raw / 1e9:.0f} GB\n"
                 f"\u2022 Longform \u203a Finished (sessions already in a LongDraw): {fin / 1e9:.0f} GB"
                 f"{total}\nThese are safe to delete when you're ready; the Shorts and LongDraws are in Done. "
                 "I never delete anything myself.")

    def drive_source(self, d: dict, tmp: Path) -> Path:
        """Stream a raw Drive recording into a small timelapse working copy."""
        from .drive import make_proxy
        dur = d.get("duration") or 30 * 60        # unknown -> assume long (keyframe mode)
        return make_proxy(self.drive.media_url(d["drive_id"]), self.drive.token(),
                          tmp / f"{d['drive_id']}.mp4", dur, max_side=3840 if self.shorts_4k else 2560)

    def drive_raw(self, d: dict) -> dict:
        """How Process cuts read short real-speed moments straight from the original on Drive."""
        if not (self.drive and d.get("duration")):
            return {}
        url, token = self.drive.media_url(d["drive_id"]), self.drive.fresh_token()
        return {"raw_duration": float(d["duration"]),
                "raw_input": lambda t0: ["-headers", f"Authorization: Bearer {token}\r\n",
                                         "-ss", f"{t0:.3f}", "-i", url]}

    @property
    def shorts_4k(self) -> bool:
        return str(self.cfg.get("shorts", {}).get("resolution", "4k")).lower() == "4k"

    def drive_after(self, did: str, d: dict, out_dir: Path, results: list[dict]) -> dict[str, str]:
        """Move the raw file to Inbox/Processed and save the full-quality variants to
        Done/Shorts. Returns {style: Drive link}."""
        links: dict[str, str] = {}
        try:
            if not d.get("keep_in_place"):          # LongDraw sessions stay in their project folder
                self.drive.move(d["drive_id"], self.drive.folder("Inbox/Processed"), self.drive.folder("Inbox"))
        except Exception as e:  # noqa: BLE001
            print(f"warn: couldn't move {d.get('drive_name')}: {e}")
        if not (self.cfg.get("drive", {}).get("backup_shorts", True) or self.shorts_4k):
            return links
        try:
            name = d.get("subject") or Path(d.get("drive_name") or did).stem
            folder = self.drive.folder(f"Done/Shorts/{name}")
            for r in results:
                tag = " (redo)" if d.get("redo") else ""
                up = self.drive.upload(out_dir / r["file"], folder,
                                       f"{name} - {STYLE_LABELS.get(r['name'], r['name'])}{tag}.mp4")
                if up.get("webViewLink"):
                    links[r["name"]] = up["webViewLink"]
        except Exception as e:  # noqa: BLE001
            print(f"warn: Drive backup failed: {e}")
        return links

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

    # ------------------------------------------------------------------ reports
    def weekly_report(self) -> None:
        """Sunday after 10 AM, once a week, when at least one platform is connected."""
        lt = self.local(self.clock())
        week = lt.strftime("%G-W%V")
        if not self.clients or lt.weekday() != 6 or lt.hour < 10 or self.state.get("report_week") == week:
            return
        self.state["report_week"] = week
        self.send_report()

    def send_report(self) -> None:
        from .report import build
        if not self.clients:
            self.say("\U0001f4ca Reports need at least one connected platform (Instagram or YouTube tokens).")
            return
        try:
            self.say(build(self.state, self.clients, self.clock()))
        except Exception as e:  # noqa: BLE001
            self.say(f"\u26a0\ufe0f Couldn't build the report: {str(e)[:200]}")

    # ------------------------------------------------------------------ compilations
    def uncompiled(self) -> list[tuple[str, dict]]:
        return [(did, d) for did, d in self.state["drawings"].items()
                if d["status"] == "rendered" and not d.get("compiled")]

    def render_compilation(self, work_root: Path | None = None) -> None:
        req = self.state.pop("compile_request", None)
        pending = self.uncompiled()
        if not req or len(pending) < 2:
            return
        from .compile import make_compilation
        n = len(pending)
        subjects = [d.get("subject") for _, d in pending if d.get("subject")]
        title = req.get("title") or f"One page. {n} drawings."
        with tempfile.TemporaryDirectory(dir=work_root) as tmp:
            tmp = Path(tmp)
            try:
                sources = [((self.drive_source(d, tmp) if d.get("source") == "drive"
                             else self.bot.download(d["file_id"], tmp / f"{did}.mp4")), d.get("subject"))
                           for did, d in pending]
                res = make_compilation(sources, tmp / "out", title)
            except Exception as e:  # noqa: BLE001
                self.say(f"\u26a0\ufe0f Couldn't make the page compilation: {str(e)[:200]}")
                return
            tags = " ".join(["#mangaart", "#inking", "#animeart", "#drawingprocess", "#mangaka",
                             "#timelapse", "#traditionalart"] +
                            [f"#{re.sub(r'[^a-z0-9]', '', s.lower())}" for s in subjects[:3]])
            caption = (f"One page, {n} drawings \u270d\U0001f3fe Which one is your favorite? "
                       f"\U0001f447\U0001f3fe\n\n{tags}")
            sent = self.bot.send_video(
                self.owner, res["short"], width=720, height=1280, duration=round(res["short_seconds"]),
                caption=(f"\U0001f4da Page compilation \u00b7 {res['short_seconds']:.0f}s \u00b7 added to your drops bank"))
            vid = new_id(self.state, "v")
            self.state["variants"][vid] = {
                "drawing": f"page:{','.join(did for did, _ in pending)}", "style": "page_compilation",
                "hook": title, "caption": caption, "seconds": round(res["short_seconds"], 1),
                "file_id": sent["video"]["file_id"], "message_id": sent["message_id"],
                "created": iso(self.clock()), "deadline": None, "status": "approved",
                "decided_by": "auto", "decided_at": iso(self.clock()), "posts": {}}
            long_path = shrink_for_telegram(res["long"])
            if long_path:
                self.bot.send_video(self.owner, long_path, width=720, height=1280,
                                    duration=round(res["long_seconds"]),
                                    caption=(f"\U0001f4fa Long version for YouTube \u00b7 "
                                             f"{res['long_seconds'] / 60:.1f} min \u00b7 every drawing at full length"))
            else:
                self.say("\u2139\ufe0f The long version came out over Telegram's 50 MB limit, so I couldn't send it.")
        for _, d in pending:
            d["compiled"] = True

    # ------------------------------------------------------------------ AI copy
    def ai_copy(self, src: Path, d: dict, styles: list[str], tmp: Path) -> dict | None:
        """Gemini-written hooks + caption when GEMINI_API_KEY is set; None -> templates."""
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key or self.cfg.get("ai", {}).get("enabled", True) is False:
            return None
        from .ai import DEFAULT_MODEL, final_frame, write_copy
        frame = final_frame(src, tmp / "final.jpg")
        from .ai import candidate_models
        copy = None
        wanted = self.cfg.get("ai", {}).get("model", DEFAULT_MODEL)
        for model in candidate_models(key, wanted, self.state.get("ai_model"), limit=3):
            copy = write_copy(frame, d.get("subject"), styles, key, model=model)
            if copy:                              # busy/retired models fall through to the next
                self.state["ai_model"] = model
                break
        if copy and not d.get("subject") and copy.get("subject"):
            d["subject"] = copy["subject"]          # Gemini recognised the character
        return copy

    def intake(self, video: dict, subject: str) -> None:
        size = video.get("file_size") or 0
        if size > MAX_BOT_DOWNLOAD:
            self.say(f"⚠️ That file is {size / 1e6:.0f} MB. Bots can only download up to 20 MB.\n"
                     "Send it as a normal video (not as a file) so Telegram compresses it, or trim it.")
            return
        uid = video.get("file_unique_id")
        if uid and any(d.get("unique_id") == uid for d in self.state["drawings"].values()):
            self.say("\U0001f501 I already have that video, so I'm skipping the duplicate.")
            return
        from .drive import describe_hints, parse_name
        did = new_id(self.state, "d")
        name_subject, hints = parse_name(subject) if subject else (None, {})
        hints.pop("unknown", None)
        self.state["drawings"][did] = {"subject": (name_subject or subject or "")[:60] or None,
                                       "file_id": video["file_id"], "unique_id": uid,
                                       "received": iso(self.clock()), "status": "queued",
                                       **({"hints": hints} if hints else {})}
        shown = self.state["drawings"][did]["subject"]
        how = describe_hints(hints)
        name = f" ({shown}{' \u00b7 ' + how if how else ''})" if shown or how else ""
        self.say(f"\U0001f3a8 Got it{name}! I'll make variants on the next run (within the hour).")

    # ------------------------------------------------------------------ buttons
    def on_callback(self, q: dict) -> None:
        if q["from"]["id"] != self.owner:
            return self.bot.answer_callback(q["id"], "Not yours")
        action, _, vid = (q.get("data") or "").partition(":")
        v = self.state["variants"].get(vid)
        msg = q.get("message", {})
        if action == "noop" or not v:
            return self.bot.answer_callback(q["id"])
        if v["status"] not in ("pending",) and action in ("ok", "no"):
            return self.bot.answer_callback(q["id"], f"Already {v['status']}")
        if action == "ok":
            self.decide(v, "approved", "you")
            self.bot.answer_callback(q["id"], "Queued to post")
            self.bot.edit_markup(self.owner, msg.get("message_id", v["message_id"]),
                                 label_markup("✅ Approved: queued to post"))
        elif action == "no":
            self.decide(v, "skipped", "you")
            self.bot.answer_callback(q["id"], "Skipped")
            self.bot.edit_markup(self.owner, msg.get("message_id", v["message_id"]),
                                 label_markup("❌ Skipped"))
        elif action == "ed":
            self.state["awaiting_caption"] = vid
            self.bot.answer_callback(q["id"])
            self.say(f"✏️ Send the new caption for {STYLE_LABELS[v['style']]} ({vid}):",
                     force_reply=True)

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
    @property
    def drops_mode(self) -> bool:
        return self.cfg.get("delivery", {}).get("mode") == "drops"

    @property
    def drop_times(self) -> list[str]:
        return self.cfg.get("delivery", {}).get("drop_times", ["17:00"])

    def drop_due(self) -> None:
        """Deliver one never-sent variant per drop time, shuffled, for the owner to post."""
        lt = self.local(self.clock())
        open_slots = sum(1 for hhmm in self.drop_times
                         if lt.time() >= datetime.strptime(hhmm, "%H:%M").time())
        done_today = sum(1 for v in self.state["variants"].values()
                         if v.get("dropped_at") and self.local(parse(v["dropped_at"])).date() == lt.date())
        while done_today < open_slots:
            v = self.next_drop()
            if not v:
                return
            self.deliver(v)
            done_today += 1

    def next_drop(self) -> dict | None:
        """Shuffled, but spread out: never the same drawing back-to-back when avoidable, prefer a
        different style than last time, and draw from the drawings with the most videos left so
        one drawing's variants don't pile up at the end of the bank."""
        pool = [v for v in self.state["variants"].values()
                if v["status"] == "approved" and not v.get("dropped_at")]
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
            + "Caption is in the next message."))
        self.say(v["caption"])
        v["dropped_at"] = iso(self.clock())
        v["drop_no"] = n
        v["status"] = "posted"

    # ------------------------------------------------------------------ posting
    def post_due(self) -> None:
        t = self.clock()
        lt = self.local(t)
        for platform, pc in self.cfg.get("platforms", {}).items():
            if not pc.get("enabled"):
                continue
            open_slots = sum(1 for hhmm in pc.get("post_times", ["12:00"])
                             if lt.time() >= datetime.strptime(hhmm, "%H:%M").time())
            done_today = sum(1 for v in self.state["variants"].values()
                             if (p := v.get("posts", {}).get(platform))
                             and self.local(parse(p["at"])).date() == lt.date())
            if done_today >= min(pc.get("max_per_day", 1), open_slots):
                continue
            v = self.next_for(platform)
            if v:
                self.post(v, platform, pc)

    def next_for(self, platform: str) -> dict | None:
        ready = [v for v in self.state["variants"].values()
                 if v["status"] in ("approved", "posted") and platform not in v.get("posts", {})]
        if not ready:
            return None
        ready.sort(key=lambda v: v.get("decided_at", ""))
        posted = sorted((v for v in self.state["variants"].values() if platform in v.get("posts", {})),
                        key=lambda v: v["posts"][platform]["at"])
        last_drawing = posted[-1]["drawing"] if posted else None
        fresh = [v for v in ready if v["drawing"] != last_drawing]   # never same drawing twice in a row
        return (fresh or ready)[0]

    def post(self, v: dict, platform: str, pc: dict) -> None:
        name = PLATFORM_LABELS.get(platform, platform)
        client = self.clients.get(platform) if pc.get("method") == "api" else None
        error = None
        if client:
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    video = self.bot.download(v["file_id"], Path(tmp) / "variant.mp4")
                    subject = self.state["drawings"].get(v["drawing"], {}).get("subject")
                    res = client.publish(video, {**v, "subject": subject}, pc)
                v.setdefault("posts", {})[platform] = {"at": iso(self.clock()), "method": "api",
                                                       "id": res["id"], "permalink": res.get("permalink")}
                v["status"] = "posted"
                link = f"\n{res['permalink']}" if res.get("permalink") else ""
                note = f"\n\u26a0\ufe0f {res['note']}" if res.get("note") else ""
                if res.get("note"):
                    v["posts"][platform]["note"] = res["note"]
                verb = "\U0001f4e5 Sent to your" if res.get("caption_for_user") else "\u2705 Posted to"
                self.say(f"{verb} {name}: {STYLE_LABELS.get(v['style'], v['style'])} ({v['drawing']}){link}{note}")
                if res.get("caption_for_user"):
                    self.say(v["caption"])
                self.note_rotated_token(platform, client)
                return
            except Exception as e:  # noqa: BLE001 - never lose a post slot to an API hiccup
                error = str(e)[:300]
                print(f"warn: {platform} publish failed: {error}")
        note = f"\u26a0\ufe0f Auto-post to {name} failed ({error}), so please post this one yourself.\n" if error else ""
        self.bot.send_video(self.owner, v["file_id"],
                            caption=f"{note}\U0001f4e4 Time to post on {name}\nCaption is in the next message.")
        self.say(v["caption"])
        v.setdefault("posts", {})[platform] = {"at": iso(self.clock()), "method": "manual",
                                               **({"error": error} if error else {})}
        v["status"] = "posted"

    def note_rotated_token(self, platform: str, client) -> None:
        new = getattr(client, "rotated_refresh_token", None)
        if new and self.state.get(f"{platform}_rotated_sent") != new[-6:]:
            self.state[f"{platform}_rotated_sent"] = new[-6:]
            self.say(f"\U0001f511 {PLATFORM_LABELS.get(platform, platform)} issued a new refresh token. "
                     f"When convenient, replace the GitHub secret {platform.upper()}_REFRESH_TOKEN with:\n{new}")

    # ------------------------------------------------------------------ bank
    def per_day(self, key: str) -> int:
        if key == "drops":
            return max(1, len(self.drop_times))
        return max(1, self.cfg["platforms"][key].get("max_per_day", 1))

    def bank(self) -> dict[str, int]:
        """Approved (or still pending) videos not yet sent to each enabled platform."""
        if self.drops_mode:
            return {"drops": sum(1 for v in self.state["variants"].values()
                                 if v["status"] in ("approved", "pending") and not v.get("dropped_at"))}
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
            lines.append(f"{label}: {n} videos \u2248 {n // self.per_day(p)} days")
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
                short.append(f"{label}: {days} day(s) left ({n} videos)")
        if short:
            self.state["low_bank_warned"] = today
            self.say("\U0001faab Video bank is running low\n" + "\n".join(short) +
                     "\nSend me your next page of timelapses when you can.")

    def refresh_stats(self, every_hours: int = 6, days_back: int = 14) -> None:
        """Pull metrics for recent API posts (used later to learn which styles/hooks win)."""
        t = self.clock()
        last = self.state.get("stats_at")
        if last and t - parse(last) < timedelta(hours=every_hours):
            return
        did_any = False
        for platform, client in self.clients.items():
            for v in self.state["variants"].values():
                post = v.get("posts", {}).get(platform)
                if not post or not post.get("id") or t - parse(post["at"]) > timedelta(days=days_back):
                    continue
                try:
                    post["stats"] = client.insights(post["id"])
                    post["stats_at"] = iso(t)
                    did_any = True
                except Exception as e:  # noqa: BLE001
                    print(f"warn: stats {platform} {post['id']}: {e}")
        if did_any or self.clients:
            self.state["stats_at"] = iso(t)

    # ------------------------------------------------------------------ status
    def status_text(self) -> str:
        vs = self.state["variants"].values()
        count = lambda s: sum(1 for v in vs if v["status"] == s)
        queued = sum(1 for d in self.state["drawings"].values() if d["status"] == "queued")
        lines = [
            "⏸ PAUSED" if self.state["paused"] else "▶️ Running",
            f"Drawings waiting to render: {queued}",
            f"Waiting for your OK: {count('pending')}",
            f"Approved, not yet posted: {sum(1 for v in vs if v['status'] == 'approved')}",
            f"Posted: {count('posted')}   Skipped: {count('skipped')}",
            f"Approval: {self.cfg['approval']['mode']} ({self.cfg['approval'].get('fallback_hours', 0)}h)",
        ]
        if self.drops_mode:
            lines.append(f"Drops: {len(self.drop_times)}/day at {', '.join(self.drop_times)} "
                         f"(you post them; nothing is auto-posted)")
            return "\n".join(lines)
        for p, pc in self.cfg.get("platforms", {}).items():
            if pc.get("enabled"):
                lines.append(f"{PLATFORM_LABELS.get(p, p)}: {pc.get('max_per_day', 1)}/day at "
                             f"{', '.join(pc.get('post_times', []))} ({pc.get('method')})")
        return "\n".join(lines)

    # ================================================================== RENDER
    def choose_styles(self, did: str) -> list[str]:
        styles = [s for s in self.cfg["variants"]["styles"] if s in STYLE_LABELS]
        uses = self.state["style_uses"]
        rng = random.Random(did)
        rng.shuffle(styles)
        weight = self.style_weights()
        # least-used first, random tie-break; styles that get more views count as "used" less,
        # so they come round more often (and the weakest gets skipped when per_drawing < styles)
        styles.sort(key=lambda s: uses.get(s, 0) / weight.get(s, 1.0))
        return styles[: self.cfg["variants"].get("per_drawing", 2)]

    def style_weights(self) -> dict[str, float]:
        """From the weekly report: average views per style vs the average style (0.5x..2x).
        Only styles with 2+ matched posts count, and only once 2+ styles have data."""
        scores = {s: v["avg_views"] for s, v in self.state.get("style_scores", {}).items()
                  if v.get("posts", 0) >= 2 and v.get("avg_views", 0) > 0}
        if len(scores) < 2:
            return {}
        mean = sum(scores.values()) / len(scores)
        return {s: min(2.0, max(0.5, v / mean)) for s, v in scores.items()}

    def render(self, work_root: Path | None = None) -> None:
        from .pipeline import make_variants   # heavy imports only in this phase

        import time as _time
        mode = self.cfg["approval"]["mode"]
        hours = self.cfg["approval"].get("fallback_hours", 6)
        budget = float(self.cfg.get("shorts", {}).get("max_render_minutes", 40)) * 60
        started = _time.time()
        done_now = 0
        for did, d in self.state["drawings"].items():
            if d["status"] != "queued":
                continue
            if done_now and _time.time() - started > budget:
                left = sum(1 for x in self.state["drawings"].values() if x["status"] == "queued")
                self.say(f"\u23f3 Rendered {done_now} drawing(s) this run; {left} more continue on the next run.")
                return
            with tempfile.TemporaryDirectory(dir=work_root) as tmp:
                tmp = Path(tmp)
                try:
                    if d.get("source") == "drive":
                        src = self.drive_source(d, tmp)
                    else:
                        src = self.bot.download(d["file_id"], tmp / f"{did}.mp4")
                    styles = d.pop("redo_styles", None) or self.choose_styles(did)
                    copy = self.ai_copy(src, d, styles, tmp)
                    big = self.shorts_4k and d.get("source") == "drive"
                    raw = self.drive_raw(d) if d.get("source") == "drive" else {}
                    if d.get("source") == "drive" and not raw and "process_cuts" in styles:
                        styles = [st for st in styles if st != "process_cuts"]   # needs the original
                    out_dir, results = make_variants(src, tmp / "out", styles,
                                                     seed=int(did[1:]), subject=d["subject"],
                                                     review_page=False, copy=copy,
                                                     max_width=2160 if big else 1080,
                                                     hints=d.get("hints"), **raw)
                except Exception as e:  # noqa: BLE001 - report any failure to the owner
                    d["status"] = "failed"
                    d["error"] = str(e)[:300]
                    self.say(f"⚠️ Couldn't process that video ({did}): {d['error']}")
                    continue
                d["beats"] = self.beats_summary(out_dir, results)
                links = {}
                if d.get("source") == "drive" and self.drive:
                    links = self.drive_after(did, d, out_dir, results)
                self.telegram_copies(out_dir, results)
                done_now += 1
                if mode == "auto":
                    try:
                        self.bank_album(did, d, out_dir, results, links)
                    except Exception as e:  # noqa: BLE001 - don't lose the rest of the run
                        d["status"] = "failed"
                        d["error"] = f"sending to Telegram: {str(e)[:250]}"
                        self.say(f"\u26a0\ufe0f Made {d.get('subject') or did}'s videos but couldn't send them: "
                                 f"{str(e)[:200]}. The 4K files are in Drive \u203a Done \u203a Shorts; "
                                 f"/redo {d.get('subject') or ''} tries again.")
                        continue
                    d["status"] = "rendered"
                    continue
                for r in results:
                    vid = new_id(self.state, "v")
                    created = self.clock()
                    deadline = created + timedelta(hours=hours) if mode == "approve_or_auto" else None
                    if mode == "auto":
                        note = "\U0001f916 Auto mode: queued to post."
                    elif deadline:
                        note = f"⏱ Posts automatically {self.fmt_time(deadline)} unless you skip it."
                    else:
                        note = "Waiting for your OK."
                    text = (f"\U0001f3ac {STYLE_LABELS[r['name']]} · {r['seconds']:.0f}s · {vid}\n"
                            f"Hook: “{r['hook']}”\n\n{r['caption']}\n\n{note}")
                    if links.get(r["name"]):
                        text += f"\n\U0001f39e Full quality: {links[r['name']]}"
                    sent = self.bot.send_video(
                        self.owner, out_dir / r.get("tg_file", r["file"]), caption=text,
                        reply_markup=None if mode == "auto" else pending_markup(vid),
                        width=720, height=1280, duration=round(r["seconds"]))
                    self.state["variants"][vid] = {
                        "drawing": did, "style": r["name"], "hook": r["hook"], "caption": r["caption"],
                        "seconds": round(r["seconds"], 1), "file_id": sent["video"]["file_id"],
                        "message_id": sent["message_id"], "created": iso(created),
                        "deadline": iso(deadline) if deadline else None,
                        "status": "approved" if mode == "auto" else "pending", "posts": {},
                        **({"full_link": links[r["name"]]} if links.get(r["name"]) else {}),
                    }
                    if mode == "auto":
                        self.decide(self.state["variants"][vid], "approved", "auto")
                    uses = self.state["style_uses"]
                    uses[r["name"]] = uses.get(r["name"], 0) + 1
                d["status"] = "rendered"
        if done_now and not any(x["status"] == "queued" for x in self.state["drawings"].values()):
            bank = self.bank().get("drops")
            extra = f" Bank: {bank} videos \u2248 {bank // self.per_day('drops')} days of drops." if bank else ""
            self.say(f"\u2705 All caught up: {done_now} drawing(s) rendered.{extra}")

    @staticmethod
    def beats_summary(out_dir: Path, results: list[dict]) -> dict:
        """What the analysis found, kept in the bot's memory (state.json) for troubleshooting."""
        try:
            an = json.loads((out_dir / "analysis.json").read_text())
        except (OSError, ValueError):
            return {}
        prog = an.get("ink_progress") or []
        step = max(1, len(prog) // 40)
        return {"seconds": an.get("seconds"), "has_pencil": an.get("has_pencil"),
                "work_seconds": round(an.get("n_frames", 0) / 30, 1), "speedup": an.get("speedup"),
                "source_seconds": an.get("source_duration"), "occluded_frames": an.get("occluded_frames"),
                "focus": an.get("focus"), "ink_progress": prog[::step],
                "variant_seconds": {r["name"]: round(r["seconds"], 1) for r in results}}

    def telegram_copies(self, out_dir: Path, results: list[dict]) -> None:
        """1080p copies for Telegram (bots can send 50 MB at most); originals stay full quality."""
        from .media import preview_copy
        for r in results:
            src = out_dir / r["file"]
            dst = src.with_name(src.stem + "_tg.mp4")
            try:
                r["tg_file"] = str(preview_copy(src, dst).relative_to(out_dir))
            except Exception as e:  # noqa: BLE001 - fall back to the original file
                print(f"warn: preview copy failed for {src.name}: {e}")
                r["tg_file"] = r["file"]

    def render_all(self, work_root: Path | None = None) -> None:
        self.render(work_root)
        self.render_compilation(work_root)

    def bank_album(self, did: str, d: dict, out_dir: Path, results: list[dict],
                   links: dict | None = None) -> None:
        """Auto mode: send the drawing's variants as one album and add them straight to the bank."""
        subject = d.get("subject") or "your drawing"
        for start, chunk in enumerate(album_chunks(out_dir, results)):
            if start == 0:
                styles = ", ".join(STYLE_LABELS[r["name"]] for r in results)
                bank_after = self.bank().get("drops", 0) + len(results) if self.drops_mode else None
                tail = (f"\nBank: {bank_after} videos \u2248 {bank_after // self.per_day('drops')} days"
                        if bank_after is not None else "")
                caption = f"\U0001f3a8 {subject}: {len(results)} variants added ({styles}){tail}"
                if links:
                    caption += "\n\U0001f39e 4K files saved to Drive \u203a Done \u203a Shorts (links come with each drop)"
            else:
                caption = ""
            files = [out_dir / r.get("tg_file", r["file"]) for r in chunk]
            try:
                msgs = self.bot.send_media_group(self.owner, files, caption)
            except TelegramError as e:              # e.g. too large together: send one by one
                print(f"warn: album failed ({e}); sending the videos one at a time")
                msgs = [self.bot.send_video(self.owner, f, caption=caption if i == 0 else "")
                        for i, f in enumerate(files)]
            for r, m in zip(chunk, msgs):
                vid = new_id(self.state, "v")
                self.state["variants"][vid] = {
                    "drawing": did, "style": r["name"], "hook": r["hook"], "caption": r["caption"],
                    "seconds": round(r["seconds"], 1), "file_id": m["video"]["file_id"],
                    "message_id": m["message_id"], "created": iso(self.clock()), "deadline": None,
                    "status": "approved", "decided_by": "auto", "decided_at": iso(self.clock()),
                    "posts": {}, **({"full_link": links[r["name"]]} if (links or {}).get(r["name"]) else {})}
                uses = self.state["style_uses"]
                uses[r["name"]] = uses.get(r["name"], 0) + 1


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


def run(phase: str) -> None:
    raw = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not raw.strip():
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (add it under the repo's Actions secrets)")
    # Pasted secrets often carry stray spaces/newlines or surrounding text; keep just the token.
    m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", raw)
    if not m:
        raise SystemExit("TELEGRAM_BOT_TOKEN doesn't look like a BotFather token "
                         "(expected digits, a colon, then ~35 letters/numbers). Re-paste it.")
    token = m.group(0)
    state, config = load_state(), load_config()
    runner = Runner(Bot(token), state, config, clients=make_clients(config), drive=make_drive(config))
    if phase == "tiktok-connect":
        try:
            tiktok_connect(runner, config)
        finally:
            save_state(state)
        return
    if phase == "check":
        try:
            check(runner)
        finally:
            save_state(state)
        return
    try:
        needs_render = False
        if phase in ("poll", "all"):
            needs_render = runner.poll()
        if phase == "render" or (phase == "all" and needs_render):
            runner.render_all()
            runner.poll()              # post anything that became due, without waiting an hour
    finally:
        save_state(state)
    if phase == "poll" and (out := os.environ.get("GITHUB_OUTPUT")):
        with open(out, "a") as f:
            f.write(f"render={'true' if needs_render else 'false'}\n")
    print(f"phase={phase} needs_render={needs_render}")


def make_drive(config: dict):
    dc = config.get("drive", {})
    env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    if not dc.get("enabled") or not all(env):
        return None
    from .drive import Drive
    return Drive(*env, root=dc.get("root", "Ooz Reels"))


def make_clients(config: dict) -> dict:
    clients = {}
    ig = config.get("platforms", {}).get("instagram", {})
    if ig.get("enabled") and os.environ.get("IG_ACCESS_TOKEN", "").strip():
        from .instagram import Instagram
        clients["instagram"] = Instagram(os.environ["IG_ACCESS_TOKEN"], ig.get("ig_user_id"))
    yt = config.get("platforms", {}).get("youtube", {})
    yt_env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    if yt.get("enabled") and all(yt_env):
        from .youtube import YouTube
        clients["youtube"] = YouTube(*yt_env)
    tt = config.get("platforms", {}).get("tiktok", {})
    tt_env = [os.environ.get(k, "").strip() for k in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_REFRESH_TOKEN")]
    if tt.get("enabled") and all(tt_env):
        from .tiktok import TikTok
        clients["tiktok"] = TikTok(*tt_env)
    return clients


def tiktok_connect(runner: Runner, config: dict) -> None:
    """Exchange the one-time code from tiktok-callback.html; send the refresh token privately."""
    from .tiktok import exchange_code
    code = os.environ.get("TIKTOK_AUTH_CODE", "").strip()
    key, secret = os.environ.get("TIKTOK_CLIENT_KEY", ""), os.environ.get("TIKTOK_CLIENT_SECRET", "")
    redirect = config.get("platforms", {}).get("tiktok", {}).get("redirect_uri", "")
    if not (code and key.strip() and secret.strip()):
        runner.say("\u26a0\ufe0f TikTok connect needs the code from the TikTok page plus the "
                   "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET secrets.")
        raise SystemExit("missing TikTok code or client secrets")
    try:
        tokens = exchange_code(key, secret, code, redirect)
    except Exception as e:  # noqa: BLE001
        runner.say(f"\u274c TikTok connect failed: {e}\nCodes expire after a few minutes: open the "
                   "TikTok connect page again and use the new code right away.")
        raise SystemExit(1) from None
    runner.say("\u2705 TikTok connected. Last step: save this as the GitHub secret "
               "TIKTOK_REFRESH_TOKEN (then delete this message):")
    runner.say(tokens["refresh_token"])
    print("TikTok connected; refresh token sent to the owner on Telegram (not printed here).")


def check(runner: Runner) -> None:
    """Read-only connection test: account, recent posts and their stats. Posts nothing."""
    lines = ["\U0001f50c Connection check (nothing was posted)"]
    ig = runner.clients.get("instagram")
    if not ig:
        lines.append("Instagram: no IG_ACCESS_TOKEN secret found")
    else:
        try:
            me = ig.whoami()
            lines.append(f"Instagram: \u2705 @{me.get('username')} \u00b7 {me.get('followers_count', '?')} followers"
                         f" \u00b7 {me.get('media_count', '?')} posts (user id {me['resolved_user_id']})")
            for m in ig.recent_media(5):
                if m.get("media_product_type") != "REELS":
                    continue
                try:
                    st = ig.insights(m["id"])
                    stats = ", ".join(f"{k} {v}" for k, v in st.items() if v is not None) or "no stats"
                except Exception as e:  # noqa: BLE001
                    stats = f"stats unavailable: {e}"
                lines.append(f"\u2022 {m.get('timestamp', '')[:10]} {(m.get('caption') or '')[:40]!r}: {stats}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"Instagram: \u274c {e}")
    yt = runner.clients.get("youtube")
    if not yt:
        lines.append("YouTube: not connected (needs YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN secrets)")
    else:
        try:
            ch = yt.whoami()
            lines.append(f"YouTube: \u2705 {ch['title']} \u00b7 {ch.get('subscribers', '?')} subscribers"
                         f" \u00b7 {ch.get('videos', '?')} videos")
            for vid in yt.recent_videos(ch["uploads"], 3):
                try:
                    st = yt.insights(vid["id"])
                    stats = ", ".join(f"{k} {v}" for k, v in st.items())
                except Exception as e:  # noqa: BLE001
                    stats = f"stats unavailable: {e}"
                lines.append(f"\u2022 {vid['published'][:10]} {vid['title'][:40]!r}: {stats}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"YouTube: \u274c {e}")
    tt = runner.clients.get("tiktok")
    if not tt:
        lines.append("TikTok: not connected (needs TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN)")
    else:
        try:
            me = tt.whoami()
            lines.append(f"TikTok: \u2705 {me.get('display_name', '?')}")
            try:
                ci = tt.creator_info()
                lines.append(f"\u2022 can post as: {', '.join(ci.get('privacy_level_options') or ['?'])}"
                             f" \u00b7 max length {ci.get('max_video_post_duration_sec', '?')}s")
            except Exception as e:  # noqa: BLE001
                lines.append(f"\u2022 direct posting not available yet ({e}); inbox drafts still work")
            runner.note_rotated_token("tiktok", tt)
        except Exception as e:  # noqa: BLE001
            lines.append(f"TikTok: \u274c {e}")
    if not runner.drive:
        lines.append("Google Drive: not connected (uses the YT_* secrets with the drive permission)")
    else:
        try:
            folders = runner.drive.ensure_layout()
            n = len(runner.drive.list(folders["Inbox"], "video"))
            lines.append(f"Google Drive: \u2705 folders ready in \u201c{runner.drive.root_name}\u201d"
                         f" \u00b7 {n} video(s) waiting in Inbox")
        except Exception as e:  # noqa: BLE001
            lines.append(f"Google Drive: \u274c {e}")
    # Gemini: try the configured model, then a few others; remember the first that answers.
    import time as _time
    from .ai import DEFAULT_MODEL, candidate_models, ping
    wanted = runner.cfg.get("ai", {}).get("model", DEFAULT_MODEL)
    key = os.environ.get("GEMINI_API_KEY", "")
    results, working = [], None
    from .ai import available_models
    offered = available_models(key)
    for model in candidate_models(key, wanted, limit=5):
        t0 = _time.time()
        try:
            reply = ping(key, model, timeout=30, tries=2)
            results.append(f"\u2022 {model}: \u2705 \u201c{reply}\u201d in {_time.time() - t0:.0f}s")
            working = model
            break
        except RuntimeError as e:
            results.append(f"\u2022 {model}: \u274c {e}")
            if "no GEMINI_API_KEY" in str(e) or "key looks wrong" in str(e):
                break
    if working:
        if working != wanted:
            runner.state["ai_model"] = working
        else:
            runner.state.pop("ai_model", None)
        lines.append(f"Gemini: \u2705 using {working}")
    else:
        lines.append("Gemini: \u274c no model answered")
    lines += results
    if offered:
        lines.append("\u2022 models this key can use: " + ", ".join(offered[:8]))
    text = "\n".join(lines)
    print(text)
    runner.say(text)
