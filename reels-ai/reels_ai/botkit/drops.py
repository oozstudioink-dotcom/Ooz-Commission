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

    @property
    def options(self) -> int:
        """How many versions of the day's drawing are sent to choose from ([delivery] options)."""
        return max(1, int(self.cfg.get("delivery", {}).get("options", len(self.split) or 3)))

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
        """Drawings that can still be dropped: {drawing: [unsent variant ids]}. You choose between
        the options on the day, so versions you haven't answered count too; ones you skipped don't.
        A drawing that already went out (in any drop, old style included) is used up: no repeats."""
        used = {v["drawing"] for v in self.state["variants"].values() if v.get("dropped_at")}
        ready: dict[str, list[str]] = {}
        for vid, v in self.state["variants"].items():
            if (v["status"] in ("approved", "pending") and not v.get("dropped_at") and v["drawing"] not in used
                    and not self.retired_variant(v)):
                ready.setdefault(v["drawing"], []).append(vid)
        return ready

    def next_split_drop(self) -> list[tuple[str, str]]:
        """One drawing and up to [delivery] options of its versions, each a different style:
        [(slot, variant id)]. Drawings with a full set first (shuffled)."""
        ready = self.split_ready()
        if not ready:
            return []
        want = self.options
        sent = len({v.get("drop_no") for v in self.state["variants"].values() if v.get("dropped_at")})
        rng = random.Random(f"{self.local(self.clock()).date()}-{sent}")
        full = max(min(len(v), want) for v in ready.values())
        did = rng.choice(sorted(d for d, v in ready.items() if min(len(v), want) == full))
        left = sorted(ready[did])
        rng.shuffle(left)
        pairs, styles = [], set()
        for vid in sorted(left, key=lambda k: self.state["variants"][k]["style"] in styles):
            st = self.state["variants"][vid]["style"]
            if len(pairs) < want and st not in styles:
                pairs.append((f"option{len(pairs) + 1}", vid)); styles.add(st)
        for vid in left:                                       # fewer styles than options: fill up
            if len(pairs) < want and vid not in [x for _, x in pairs]:
                pairs.append((f"option{len(pairs) + 1}", vid))
        return pairs

    def deliver_split(self, pairs: list[tuple[str, str]]) -> None:
        """One drawing = one day: its versions sent as options to choose from, the caption, and a
        still of the finished piece for the Instagram post. The drawing is then used up."""
        variants = self.state["variants"]
        n = max((x.get("drop_no", 0) for x in variants.values()), default=0) + 1
        first = variants[pairs[0][1]]
        did = first["drawing"]
        subject = self.state["drawings"].get(did, {}).get("subject") or "this piece"
        caption = first["caption"]
        where = self.cfg.get("delivery", {}).get("post_to", "TikTok \u00b7 Instagram Reels \u00b7 YouTube Shorts")
        self.say(f"\U0001f4c5 Today's drop #{n}: {subject}\n{len(pairs)} versions below: pick the one you like "
                 f"and post it to {where}. The others are set aside; this drawing won't come back.")
        for k, (_, vid) in enumerate(pairs, 1):
            v = variants[vid]
            self.bot.send_video(self.owner, v["file_id"], caption=(
                f"Option {k} of {len(pairs)} \u00b7 {STYLE_LABELS.get(v['style'], v['style'])} \u00b7 "
                f"{v.get('seconds', '?')}s \u00b7 hook on video: \u201c{v.get('hook', '')}\u201d\n"
                + (f"\U0001f39e 4K file: {v['full_link']}\n" if v.get("full_link") else "")
                + "\U0001f3b5 Add a trending sound when you post."), reply_markup=rating_markup(vid))
            v.update(dropped_at=iso(self.clock()), drop_no=n, status="posted", option=k, caption=caption)
        self.say(caption)
        self.send_post_still(did, [variants[vid] for _, vid in pairs], caption)
        for vid in self.split_ready().get(did, []):           # this drawing is done: spares stay out
            variants[vid]["status"] = "spare"

    def send_post_still(self, did: str, options: list[dict], caption: str) -> None:
        """The finished piece as a 4:5 image for today's Instagram feed post (same drawing as the
        Reel). Taken from the last frame of a version with no text on its final second."""
        if not self.cfg.get("delivery", {}).get("instagram_post", True):
            return
        try:
            path = self.post_still(options)
        except Exception as e:  # noqa: BLE001 - the videos already went out
            print(f"warn: post still: {str(e)[:200]}")
            path = None
        if path is None:
            return self.say("\U0001f5bc For today's Instagram post, use the last frame of any version above "
                            "(the finished piece). I couldn't make the still this time.")
        try:
            text = "\U0001f5bc Instagram post: the finished piece (4:5). Post it with the caption above."
            if hasattr(self.bot, "send_photo"):
                self.bot.send_photo(self.owner, path, caption=text)
            else:
                self.say(text)
        finally:
            path.unlink(missing_ok=True)

    def post_still(self, options: list[dict]) -> Path | None:
        import re as _re
        import subprocess
        prefer = ["one_take", "last_lines", "slow_open", "chapters", "speedrun", "reveal_first", "rewind"]
        options = sorted(options, key=lambda v: prefer.index(v["style"]) if v["style"] in prefer else 99)
        tmp = Path(tempfile.mkdtemp())
        for v in options:
            m = _re.search(r"/d/([\w-]+)", v.get("full_link") or "")
            if m and self.drive:                               # the 4K file in Drive: read only its end
                src = ["-headers", f"Authorization: Bearer {self.drive.token()}\r\n",
                       "-sseof", "-0.4", "-i", self.drive.media_url(m.group(1))]
            else:                                              # the Telegram preview
                try:
                    local = self.bot.download(v["file_id"], tmp / "v.mp4")
                except Exception as e:  # noqa: BLE001 - too big for a bot download, or gone
                    print(f"warn: post still download: {str(e)[:120]}")
                    continue
                src = ["-sseof", "-0.4", "-i", str(local)]
            frame = tmp / "frame.png"
            res = subprocess.run(["ffmpeg", "-v", "error", "-y", *src, "-frames:v", "1", str(frame)],
                                 capture_output=True, text=True, timeout=180)
            if res.returncode == 0 and frame.exists() and frame.stat().st_size > 0:
                return self.crop_post(frame, tmp / "post.jpg")
            print(f"warn: post still ffmpeg: {res.stderr.strip()[-200:]}")
        return None

    @staticmethod
    def crop_post(frame: Path, out: Path) -> Path:
        """The whole drawing in a 4:5 image (Instagram's tallest feed shape): cut to the ink with a
        margin, then widened with the paper's own colour when the drawing is taller than 4:5."""
        import cv2
        import numpy as np
        im = cv2.imread(str(frame))
        h, w = im.shape[:2]
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        ys, xs = np.nonzero(g < np.percentile(g, 50) - 40)
        if len(ys) > 50:
            y0, y1 = np.percentile(ys, [0.5, 99.5]); x0, x1 = np.percentile(xs, [0.5, 99.5])
            m = 0.07 * max(y1 - y0, x1 - x0)
            y0, y1 = int(max(0, y0 - m)), int(min(h, y1 + m)); x0, x1 = int(max(0, x0 - m)), int(min(w, x1 + m))
        else:
            y0, y1, x0, x1 = 0, h, 0, w
        bh, bw = y1 - y0, x1 - x0
        H = max(bh, round(bw * 5 / 4)); W = round(H * 4 / 5)
        # take as much real picture around the drawing as the 4:5 window holds
        cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
        ty0 = min(max(0, cy - H // 2), max(0, h - H)); tx0 = min(max(0, cx - W // 2), max(0, w - W))
        part = im[ty0:ty0 + H, tx0:tx0 + W]
        oy = (H - part.shape[0]) // 2; ox = (W - part.shape[1]) // 2
        # any space left is filled by carrying the paper's edge outward, softened so it reads as paper
        canvas = cv2.copyMakeBorder(part, oy, H - part.shape[0] - oy, ox, W - part.shape[1] - ox,
                                    cv2.BORDER_REPLICATE)
        if oy or ox:
            soft = cv2.GaussianBlur(canvas, (0, 0), 25)
            keep = np.zeros((H, W), np.float32); keep[oy:oy + part.shape[0], ox:ox + part.shape[1]] = 1
            keep = cv2.GaussianBlur(keep, (0, 0), 6)[..., None]
            canvas = (canvas * keep + soft * (1 - keep)).astype(np.uint8)
        if canvas.shape[1] > 1440:
            canvas = cv2.resize(canvas, (1440, 1800), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out), canvas, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return out

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
            if p == "drops" and self.split:
                days = n // self.per_day(p)
                lines.append(f"Drawings ready: {n} = {days} day{'s' if days != 1 else ''} of uploads")
                lines.append("Each day: 1 Short (TikTok, YouTube Shorts, Instagram Reels) + 1 Instagram "
                             "post, all from the same drawing. Each drawing is used once.")
                continue
            unit = "videos"
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
