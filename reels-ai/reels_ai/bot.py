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
from .botkit.commands import CommandsMixin
from .botkit.drive_inbox import DriveMixin
from .botkit.inventory import InventoryMixin
from .botkit.naming import NamingMixin
from .botkit.drops import DropsMixin
from .botkit.posting import PostingMixin
from .botkit.learning import LearningMixin
from .botkit.render import RenderMixin
from .botkit.common import (  # noqa: F401 - re-exported: other modules and tests import them from here
    STYLE_LABELS, PLATFORM_LABELS, MAX_BOT_DOWNLOAD, HELP, pending_markup, rating_markup,
    label_markup, _time_range, _mmss, _norm_name, strip_guessed_names, ALBUM_BYTES, album_chunks,
    dispatch_workflow, shrink_for_telegram,
)
from .botkit.connect import check, tiktok_connect  # noqa: F401


class Runner(CommandsMixin, DriveMixin, InventoryMixin, NamingMixin, DropsMixin, PostingMixin, LearningMixin, RenderMixin):
    def __init__(self, bot: Bot, state: dict, config: dict, clock=now, clients: dict | None = None,
                 drive=None, checkpoint=None):
        self.bot, self.state, self.cfg, self.clock = bot, state, config, clock
        # Saves the bot's memory mid-run (after each drawing), so a run cut off by GitHub's time
        # limit keeps what it finished instead of re-rendering and re-sending it.
        self.checkpoint = checkpoint or (lambda: None)
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
        asked = str((m.get("reply_to_message") or {}).get("message_id") or "")
        if text and not cmd and asked in self.state.get("name_asks", {}):
            return self.name_drawing(self.state["name_asks"][asked], text)
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
            arg, window = _time_range(arg)
            self.redo_all() if arg.lower() == "all" else self.redo(arg, window)
        elif cmd in ("/inventory", "/list"):
            for chunk in self.inventory_text():
                self.say(chunk)
        elif cmd in ("/delete", "/undelete"):
            arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            self.delete_command(arg, undo=cmd == "/undelete")
        elif cmd == "/name":
            self.name_command(text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else "")
        elif cmd == "/ideas":
            self.save_ideas(text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else "")
        elif cmd in ("/retire", "/unretire"):
            self.retire_command(cmd == "/retire", text.split(maxsplit=1)[1].strip()
                                if len(text.split(maxsplit=1)) > 1 else "")
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

    # ------------------------------------------------------------------ buttons
    def on_callback(self, q: dict) -> None:
        if q["from"]["id"] != self.owner:
            return self.bot.answer_callback(q["id"], "Not yours")
        action, _, vid = (q.get("data") or "").partition(":")
        if action == "noname":
            d = self.state["drawings"].get(vid)
            if d and d.get("status") == "naming":
                d["status"] = "queued"
                self.bot.edit_markup(self.owner, q.get("message", {}).get("message_id"),
                                     label_markup("\u23ed Making it without a name"))
            return self.bot.answer_callback(q["id"], "OK")
        v = self.state["variants"].get(vid)
        msg = q.get("message", {})
        if action == "noop" or not v:
            return self.bot.answer_callback(q["id"])
        if v["status"] not in ("pending",) and action in ("ok", "no"):
            return self.bot.answer_callback(q["id"], f"Already {v['status']}")
        if action in ("up", "dn"):
            if v.get("rated"):
                return self.bot.answer_callback(q["id"], "Already rated")
            v["rated"] = action
            self.feedback(v["style"], action == "up")
            self.bot.answer_callback(q["id"], "Noted")
            return self.bot.edit_markup(self.owner, msg.get("message_id"), label_markup(
                "👍 Noted: more like this" if action == "up" else "👎 Noted: fewer like this"))
        if action == "ok":
            self.decide(v, "approved", "you")
            self.feedback(v["style"], True)
            self.bot.answer_callback(q["id"], "Added to your drops" if self.drops_mode else "Queued to post")
            self.bot.edit_markup(self.owner, msg.get("message_id", v["message_id"]), label_markup(
                "✅ Kept: added to your drops" if self.drops_mode else "✅ Approved: queued to post"))
        elif action == "no":
            self.decide(v, "skipped", "you")
            self.feedback(v["style"], False)
            self.bot.answer_callback(q["id"], "Skipped")
            self.bot.edit_markup(self.owner, msg.get("message_id", v["message_id"]),
                                 label_markup("❌ Skipped: won't be dropped"))
        elif action == "ed":
            self.state["awaiting_caption"] = vid
            self.bot.answer_callback(q["id"])
            self.say(f"✏️ Send the new caption for {STYLE_LABELS[v['style']]} ({vid}):",
                     force_reply=True)


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
    config = load_config()
    if phase == "alert":                       # the workflow failed or timed out: tell the owner
        state = load_state()
        owner = os.environ.get("TELEGRAM_OWNER_ID") or state.get("owner_chat_id")
        if not owner:
            from .memory import owner_chat_id
            try:
                owner = owner_chat_id(make_drive(config))
            except Exception as e:  # noqa: BLE001
                print(f"warn: {e}")
        if owner:
            url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
            what = os.environ.get("ALERT_WHAT", "bot run")
            Bot(token).send_message(int(owner), f"\u26a0\ufe0f The {what} stopped with an error. Finished work is "
                                                 f"kept; anything left over is retried on the next run.\nDetails: {url}")
        return
    drive = make_drive(config)
    save = save_state
    if drive is not None and config.get("drive", {}).get("memory_in_drive", True):
        from .memory import DriveMemory, MemoryError_
        mem = DriveMemory(drive)
        try:
            state = mem.load()
        except MemoryError_ as e:
            raise SystemExit(str(e))
        save = mem.save
    else:
        state = load_state()
    runner = Runner(Bot(token), state, config, clients=make_clients(config), drive=drive,
                    checkpoint=lambda: save(state))
    restored = state.pop("memory_restored", None)
    if restored:
        runner.say(f"\u26a0\ufe0f My memory file in Drive was damaged, so I restored it from the backup "
                   f"\u201c{restored}\u201d. Anything from after that backup may need a /redo.")
    if phase == "tiktok-connect":
        try:
            tiktok_connect(runner, config)
        finally:
            save(state)
        return
    if phase == "check":
        try:
            check(runner)
        finally:
            save(state)
        return
    if phase == "tidy-drive":
        from . import tidy
        if not runner.drive:
            raise SystemExit("Drive isn't connected")
        runner.say(tidy.run(runner.drive, state))
        return
    try:
        needs_render = False
        if phase in ("poll", "all"):
            needs_render = runner.poll()
        if phase == "render" or (phase == "all" and needs_render):
            runner.render_all()
            runner.poll()              # post anything that became due, without waiting an hour
    finally:
        save(state)
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
