"""The approval bot. Runs once per GitHub Actions tick (hourly), in two phases:

poll   (stdlib only, ~seconds): read Telegram, handle taps/commands, take in new videos,
                                auto-approve past the deadline, post anything due.
render (needs ffmpeg/OpenCV):   turn newly received timelapses into variants and send them
                                to you for approval. Only runs when there is something to render.
"""
from __future__ import annotations

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
    "page_compilation": "Page compilation",
}
PLATFORM_LABELS = {"youtube": "YouTube Shorts", "instagram": "Instagram Reels", "tiktok": "TikTok"}
MAX_BOT_DOWNLOAD = 20 * 1024 * 1024   # Telegram bots can only download files up to 20 MB

HELP = (
    "Send me a drawing timelapse (as a video) and I'll make variants for you to approve.\n"
    "Add the character name as the video's caption, e.g. Naruto.\n\n"
    "/status  overview\n/compile [title]  stitch the drawings since the last compilation into one page video\n/report  stats report now (also sent every Sunday)\n/music  your music library (send me audio files to add tracks)\n/nomusic, /musicon  silence or re-enable music\n/queue  your video bank and how many days it covers\n/pause  stop all posting\n/resume  start posting again\n/help  this message"
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
    def __init__(self, bot: Bot, state: dict, config: dict, clock=now, clients: dict | None = None):
        self.bot, self.state, self.cfg, self.clock = bot, state, config, clock
        self.clients = clients or {}          # platform -> API client (e.g. Instagram)
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
        audio = m.get("audio") or (doc if str(doc.get("mime_type", "")).startswith("audio/") else None)
        if audio:
            return self.add_music(audio, (m.get("caption") or "").strip())
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
        elif cmd == "/id":
            self.say(f"Your chat ID: {chat['id']}\n(For the commission-alerts script's TELEGRAM_CHAT_ID.)")
        elif cmd == "/report":
            self.state["report_request"] = True
            self.send_report()
        elif cmd == "/music":
            self.say(self.music_text())
        elif cmd == "/nomusic":
            self.state["music_off"] = True
            self.say("\U0001f507 New variants will be silent. /musicon to turn the library back on.")
        elif cmd == "/musicon":
            self.state["music_off"] = False
            self.say("\U0001f3b5 Music library is on again.")
        elif cmd == "/clearmusic":
            self.state["music"] = []
            self.say("\U0001f5d1 Music library emptied.")
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
                sources = [(self.bot.download(d["file_id"], tmp / f"{did}.mp4"), d.get("subject"))
                           for did, d in pending]
                res = make_compilation(sources, tmp / "out", title, audio=self.pick_track(tmp))
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
        copy = write_copy(frame, d.get("subject"), styles, key,
                          model=self.cfg.get("ai", {}).get("model", DEFAULT_MODEL))
        if copy and not d.get("subject") and copy.get("subject"):
            d["subject"] = copy["subject"]          # Gemini recognised the character
        return copy

    # ------------------------------------------------------------------ music
    def add_music(self, audio: dict, title: str) -> None:
        if (audio.get("file_size") or 0) > MAX_BOT_DOWNLOAD:
            self.say("\u26a0\ufe0f That audio file is over 20 MB. Send a shorter clip or an mp3.")
            return
        lib = self.state.setdefault("music", [])
        if any(t["unique_id"] == audio.get("file_unique_id") for t in lib):
            self.say("\U0001f501 That track is already in your library.")
            return
        name = title or audio.get("title") or audio.get("file_name") or f"Track {len(lib) + 1}"
        lib.append({"file_id": audio["file_id"], "unique_id": audio.get("file_unique_id"),
                    "title": name[:60], "uses": 0})
        self.say(f"\U0001f3b5 Added \u201c{name[:60]}\u201d. Your library has {len(lib)} track(s); "
                 "new variants will use them in rotation.")

    def music_text(self) -> str:
        lib = self.state.get("music", [])
        if not lib:
            return ("\U0001f3b5 No music yet. Send me audio files (royalty-free tracks or your own pen-sound "
                    "recordings) and I'll lay them under new variants.")
        state = "off (/musicon)" if self.state.get("music_off") else "on (/nomusic to silence)"
        rows = [f"{i + 1}. {t['title']} \u00b7 used {t['uses']}x" for i, t in enumerate(lib)]
        return f"\U0001f3b5 Music library, {state}\n" + "\n".join(rows)

    def pick_track(self, tmp: Path) -> Path | None:
        lib = self.state.get("music", [])
        if not lib or self.state.get("music_off"):
            return None
        t = min(lib, key=lambda t: t["uses"])
        try:
            path = self.bot.download(t["file_id"], tmp / "track.audio")
        except Exception as e:  # noqa: BLE001 - music is optional; never block a render on it
            print(f"warn: music download failed: {e}")
            return None
        t["uses"] += 1
        return path

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
        did = new_id(self.state, "d")
        self.state["drawings"][did] = {"subject": subject[:60] or None, "file_id": video["file_id"],
                                       "unique_id": uid, "received": iso(self.clock()),
                                       "status": "queued"}
        name = f" ({subject})" if subject else ""
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
            "Caption is in the next message."))
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
        styles.sort(key=lambda s: uses.get(s, 0))          # least-used first, random tie-break
        return styles[: self.cfg["variants"].get("per_drawing", 2)]

    def render(self, work_root: Path | None = None) -> None:
        from .pipeline import make_variants   # heavy imports only in this phase

        mode = self.cfg["approval"]["mode"]
        hours = self.cfg["approval"].get("fallback_hours", 6)
        for did, d in self.state["drawings"].items():
            if d["status"] != "queued":
                continue
            with tempfile.TemporaryDirectory(dir=work_root) as tmp:
                tmp = Path(tmp)
                try:
                    src = self.bot.download(d["file_id"], tmp / f"{did}.mp4")
                    styles = self.choose_styles(did)
                    copy = self.ai_copy(src, d, styles, tmp)
                    out_dir, results = make_variants(src, tmp / "out", styles,
                                                     seed=int(did[1:]), subject=d["subject"],
                                                     audio=self.pick_track(tmp), review_page=False,
                                                     copy=copy)
                except Exception as e:  # noqa: BLE001 - report any failure to the owner
                    d["status"] = "failed"
                    d["error"] = str(e)[:300]
                    self.say(f"⚠️ Couldn't process that video ({did}): {d['error']}")
                    continue
                if mode == "auto":
                    self.bank_album(did, d, out_dir, results)
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
                    sent = self.bot.send_video(
                        self.owner, out_dir / r["file"], caption=text,
                        reply_markup=None if mode == "auto" else pending_markup(vid),
                        width=720, height=1280, duration=round(r["seconds"]))
                    self.state["variants"][vid] = {
                        "drawing": did, "style": r["name"], "hook": r["hook"], "caption": r["caption"],
                        "seconds": round(r["seconds"], 1), "file_id": sent["video"]["file_id"],
                        "message_id": sent["message_id"], "created": iso(created),
                        "deadline": iso(deadline) if deadline else None,
                        "status": "approved" if mode == "auto" else "pending", "posts": {},
                    }
                    if mode == "auto":
                        self.decide(self.state["variants"][vid], "approved", "auto")
                    uses = self.state["style_uses"]
                    uses[r["name"]] = uses.get(r["name"], 0) + 1
                d["status"] = "rendered"

    def render_all(self, work_root: Path | None = None) -> None:
        self.render(work_root)
        self.render_compilation(work_root)

    def bank_album(self, did: str, d: dict, out_dir: Path, results: list[dict]) -> None:
        """Auto mode: send the drawing's variants as one album and add them straight to the bank."""
        subject = d.get("subject") or "your drawing"
        for start in range(0, len(results), 10):
            chunk = results[start:start + 10]
            if start == 0:
                styles = ", ".join(STYLE_LABELS[r["name"]] for r in results)
                bank_after = self.bank().get("drops", 0) + len(results) if self.drops_mode else None
                tail = (f"\nBank: {bank_after} videos \u2248 {bank_after // self.per_day('drops')} days"
                        if bank_after is not None else "")
                caption = f"\U0001f3a8 {subject}: {len(results)} variants added ({styles}){tail}"
            else:
                caption = ""
            msgs = self.bot.send_media_group(self.owner, [out_dir / r["file"] for r in chunk], caption)
            for r, m in zip(chunk, msgs):
                vid = new_id(self.state, "v")
                self.state["variants"][vid] = {
                    "drawing": did, "style": r["name"], "hook": r["hook"], "caption": r["caption"],
                    "seconds": round(r["seconds"], 1), "file_id": m["video"]["file_id"],
                    "message_id": m["message_id"], "created": iso(self.clock()), "deadline": None,
                    "status": "approved", "decided_by": "auto", "decided_at": iso(self.clock()),
                    "posts": {}}
                uses = self.state["style_uses"]
                uses[r["name"]] = uses.get(r["name"], 0) + 1


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
    runner = Runner(Bot(token), state, config, clients=make_clients(config))
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
    text = "\n".join(lines)
    print(text)
    runner.say(text)
