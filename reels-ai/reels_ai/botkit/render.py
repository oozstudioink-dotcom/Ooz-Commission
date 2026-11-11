"""Turning queued recordings into Shorts: watching, splitting, rendering, the reviewer's verdict, sending."""
from __future__ import annotations

import json  # noqa: F401
import time
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


class RenderMixin:
    def held_back(self, did: str, d: dict, out_dir: Path, results: list[dict]) -> bool:
        """What the reviewer kept from you. True when no version passed: the drawing is held (not
        sent, not dropped) and you get one message with the reason and what to do."""
        try:
            held = json.loads((out_dir / "held.json").read_text())
        except (OSError, ValueError):
            held = []
        if not held:
            return False
        d["held"] = [f"{STYLE_LABELS.get(h['name'], h['name'])}: {'; '.join(h['reasons'])}" for h in held][:6]
        name = self.drawing_label(did, d)
        if results:
            print(f"reviewer: {len(held)} version(s) of {name} held back, {len(results)} passed")
            return False
        why = sorted({r for h in held for r in h["reasons"]})
        d["status"] = "held"
        d["error"] = "; ".join(why)[:300]
        base = d.get("subject") or (d.get("drive_name") or did).rsplit(".", 1)[0]
        self.say(f"\U0001f50d I made Shorts of {name} but none passed my own check, so I didn't send them:\n"
                 + "\n".join(f"\u2022 {w}" for w in why[:4])
                 + f"\nIf the recording shows several pages, tell me the minutes you drew in: /redo {base} 16-34. "
                   f"Or /delete {did} to drop it.")
        return True

    def send_pages_sheet(self, d: dict, out_dir: Path) -> None:
        """A recording that shows several pages (a livestream): show which part was used, so a
        wrong pick is one /redo away."""
        sheet, info = out_dir / "pages.jpg", out_dir / "pages.json"
        if not (sheet.exists() and info.exists()) or (d.get("part") and not d["part"]["label"].startswith("1 ")):
            return
        try:
            used = json.loads(info.read_text())["used"]
        except (ValueError, KeyError):
            return
        name = d.get("subject") or (d.get("drive_name") or "").rsplit(".", 1)[0]
        mmss = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"  # noqa: E731
        text = (f"\U0001f4c4 {d.get('drive_name') or name} shows more than one page. I used "
                f"{mmss(used[0])}\u2013{mmss(used[1])} (outlined green): the stretch where ink was added.\n"
                f"Wrong part? Send /redo {name} 16-34 with the minutes you drew in.")
        try:
            if hasattr(self.bot, "send_photo"):
                self.bot.send_photo(self.owner, sheet, caption=text)
            else:
                self.say(text)
        except Exception as e:  # noqa: BLE001
            print(f"warn: pages sheet: {e}")
            self.say(text)

    def may_split(self, d: dict) -> bool:
        """Look for separate drawings on the page, unless you already split them yourself: a
        numbered video in a LongDraw page folder ('3 - Kakashi.MOV') is one drawing."""
        if not self.cfg["variants"].get("split_drawings", True):
            return False
        from ..drive import CAMERA, _stem, part_number
        name = d.get("drive_name") or ""
        numbered = part_number(name) is not None and not CAMERA.match(_stem(name))   # not IMG_4412
        return not (str(d.get("drive_where") or "").startswith("Longform") and numbered)

    def split_page(self, did: str, d: dict, src: Path, tmp: Path, raw_duration: float | None,
                   max_width: int, raw_input=None) -> None:
        """One page with several separate drawings (a head close-up, two figures ...): each gets
        its own Shorts, framed on it and covering the time it was drawn. This drawing becomes the
        first; the others are queued after it. Done once per recording."""
        from ..pipeline import find_parts
        from ..watch import watcher
        d["split_done"] = True
        name = d.get("drive_name") or did
        try:
            # watched when there is a Claude key: ears (what was said) + eyes (what is on screen)
            from ..watch import saved_map
            old = saved_map(self.drive, d["drive_id"]) if self.drive and d.get("drive_id") else None
            watch = watcher(raw_input, name, self.cfg, tmp, saved=old)
            if watch and not old and self.claude_over_budget():
                watch = None                                   # measure only until next month
            parts = find_parts(src, tmp / "parts", max_width=max_width, raw_duration=raw_duration,
                               window=d.get("window"), watcher=watch)
        except Exception as e:  # noqa: BLE001 - splitting is a bonus: render the page as one
            print(f"warn: couldn't look for separate drawings: {e}")
            return
        self.keep_edit_map(d, tmp / "parts" / f"{src.stem}_parts" / "edit_map.json")
        if len(parts) < 2:
            return
        d["part"] = parts[0]
        keep = {k: v for k, v in d.items() if k not in ("status", "error", "beats", "part")}
        for p in parts[1:]:
            self.state["drawings"][new_id(self.state, "d")] = {**keep, "part": p, "status": "queued"}
        name = d.get("subject") or d.get("drive_name") or did
        titles = [f"{p['label'].split()[0]}. {p['title']}" for p in parts if p.get("title")]
        self.say(f"\U0001f5bc Found {len(parts)} separate drawings in {name}. Making Shorts for "
                 "each one, framed on it: this one first, the others on the next renders."
                 + ("\n" + "\n".join(titles) if titles else ""))
        self.checkpoint()

    def keep_edit_map(self, d: dict, path: Path) -> None:
        """Save what was seen and heard in a recording next to the bot's memory in Drive (it is
        never watched twice, and an app can show it later), and a one-line summary on the drawing."""
        if not path.exists():
            return
        try:
            seen = json.loads(path.read_text())
        except ValueError:
            return
        d["watched"] = {"pages": len(seen.get("pages", [])), "drawings": len(seen.get("drawings", [])),
                        "spoken_lines": len(seen.get("transcript", [])), "model": seen.get("model")}
        from ..watch import cost, save_map
        if seen.get("reused"):
            return
        print(f"Claude: {cost(seen) * 100:.0f} cents for this recording, ${self.spend(cost(seen)):.2f} this month")
        if not (self.drive and d.get("drive_id")):
            return
        try:
            save_map(self.drive, d["drive_id"], path.read_text())
        except Exception as e:  # noqa: BLE001 - the map is a record; rendering goes on without it
            print(f"warn: couldn't save the edit map: {e}")

    def spend(self, dollars: float) -> float:
        """Count Claude spending for this month; returns the month's total."""
        spent = self.state.setdefault("claude_spend", {})
        month = time.strftime("%Y-%m")
        spent[month] = round(spent.get(month, 0.0) + dollars, 4)
        return spent[month]

    def look_over(self, out_dir: Path, results: list[dict]) -> None:
        """Claude looks at a few frames of each finished version and adds one line of opinion to
        its message ([review] look). Advice only: you still decide. About 2 cents a drawing."""
        from .. import eyes
        from ..watch import cost
        if not self.cfg.get("review", {}).get("look", True) or not eyes.available() or self.claude_over_budget():
            return
        try:
            vids = [(r["name"], eyes.stills(out_dir / r["file"])) for r in results]
            seen = eyes.judge([v for v in vids if v[1]], model=self.cfg.get("watch", {}).get("model", eyes.MODEL))
            total = self.spend(cost(seen))
            print(f"look: {cost(seen) * 100:.1f} cents, ${total:.2f} this month")
            notes = {v["name"]: v for v in seen.get("videos", [])}
            for r in results:
                v = notes.get(r["name"])
                if v:
                    r["look"] = ("\U0001f441 Looks good: " if v["keep"] else "\U0001f441 Check this one: ") + v["note"][:160]
        except Exception as e:  # noqa: BLE001 - an opinion is a bonus; the videos go out without it
            print(f"warn: couldn't look over the videos: {str(e)[:150]}")

    def claude_over_budget(self) -> bool:
        """Has this month's Claude spending reached [watch] monthly_budget_usd? Told once a month."""
        limit = float(self.cfg.get("watch", {}).get("monthly_budget_usd", 0) or 0)
        month = time.strftime("%Y-%m")
        spent = self.state.get("claude_spend", {}).get(month, 0.0)
        if not limit or spent < limit:
            return False
        if self.state.get("claude_budget_told") != month:
            self.state["claude_budget_told"] = month
            self.say(f"\U0001f4b0 Claude has used ${spent:.2f} this month, the limit you set (${limit:.0f}). "
                     "Until next month I find the drawings by measuring only. Raise monthly_budget_usd "
                     "in config.toml to change it.")
        return True

    # ------------------------------------------------------------------ compilations
    def uncompiled(self) -> list[tuple[str, dict]]:
        return [(did, d) for did, d in self.state["drawings"].items()
                if d["status"] == "rendered" and not d.get("compiled")]

    def render_compilation(self, work_root: Path | None = None) -> None:
        req = self.state.pop("compile_request", None)
        pending = self.uncompiled()
        if not req or len(pending) < 2:
            return
        from ..compile import make_compilation
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

    def _queued_drawings(self):
        """Each drawing once, in order, including ones added while rendering (split pages)."""
        done: set[str] = set()
        while True:
            nxt = next(((k, v) for k, v in list(self.state["drawings"].items()) if k not in done), None)
            if nxt is None:
                return
            done.add(nxt[0])
            yield nxt

    def render(self, work_root: Path | None = None) -> None:
        from ..pipeline import make_variants   # heavy imports only in this phase

        import time as _time
        mode = self.cfg["approval"]["mode"]
        hours = self.cfg["approval"].get("fallback_hours", 6)
        budget = float(self.cfg.get("shorts", {}).get("max_render_minutes", 40)) * 60
        started = _time.time()
        done_now = 0
        self.stop_waiting_for_names()
        guard = float(self.cfg.get("shorts", {}).get("drop_guard_minutes", 20))
        self._proxy = {"dir": Path(tempfile.mkdtemp(dir=work_root))}
        try:
            self._render_queue(make_variants, mode, hours, budget, started, done_now, guard, work_root, _time)
        finally:
            import shutil
            shutil.rmtree(self._proxy["dir"], ignore_errors=True)
            self._proxy = None

    def _render_queue(self, make_variants, mode, hours, budget, started, done_now, guard, work_root, _time) -> None:
        for did, d in self._queued_drawings():         # also picks up drawings a split page adds
            if d["status"] != "queued":
                continue
            if self.serving and done_now:              # always-on: answer Telegram between drawings
                try:
                    self.poll()
                except Exception as e:  # noqa: BLE001 - answering must not stop the rendering
                    print(f"warn: couldn't check Telegram between drawings: {str(e)[:150]}")
                self.checkpoint()
                if self.yield_now():                   # a new run is waiting: it picks up the rest
                    return
                if d["status"] != "queued":            # you deleted or changed it meanwhile
                    continue
            if self.retired(d):                      # listed in [bank] retired: not made any more
                d["status"] = "retired"
                self.say(f"\U0001f4e6 Skipped {d.get('subject') or d.get('drive_name') or did}: it's on your "
                         "retired list ([bank] retired in config.toml).")
                continue
            wait = self.minutes_to_drop()
            if wait is not None and wait <= guard:
                # a drop is due soon and runs go one at a time: stop here so it goes out on time
                print(f"drop due in {wait:.0f} min: the remaining drawings continue on the next run")
                return
            if done_now and not self.serving and _time.time() - started > budget:
                left = sum(1 for x in self.state["drawings"].values() if x["status"] == "queued")
                self.say(f"\u23f3 Rendered {done_now} drawing(s) this run; {left} more continue on the next run.")
                return
            # a recording that made two runs run out of time is set aside, so it can't block every run
            d["attempts"] = d.get("attempts", 0) + 1
            if d["attempts"] > 2:
                d["status"] = "failed"
                d["error"] = "took longer than a whole run twice"
                name = d.get("subject") or d.get("drive_name") or did
                self.say(f"\u26a0\ufe0f {name} took longer than a whole run twice, so I've set it aside "
                         "for now. /redo it with the minutes you drew in (e.g. /redo "
                         f"{name} 16-34) to make it from just that part.")
                self.checkpoint()
                continue
            self.checkpoint()                         # remembered even if this run times out
            with tempfile.TemporaryDirectory(dir=work_root) as tmp:
                tmp = Path(tmp)
                try:
                    if d.get("source") == "drive":
                        src = self.drive_source(d, tmp)
                    else:
                        src = self.bot.download(d["file_id"], tmp / f"{did}.mp4")
                    styles = d.pop("redo_styles", None) or self.choose_styles(did)
                    d["subject"] = self.labelled_subject(d)     # drop any old AI guess
                    copy = self.with_owner_ideas(self.ai_copy(src, d, styles, tmp), styles, did)
                    big = self.shorts_4k and d.get("source") == "drive"
                    raw = self.drive_raw(d) if d.get("source") == "drive" else {}
                    if d.get("source") == "drive" and not raw and "process_cuts" in styles:
                        from ..pipeline import swap_out                      # needs the original:
                        styles = swap_out(styles, "process_cuts")              # make another style
                    if d.get("source") == "drive" and not d.get("split_done") and self.may_split(d):
                        self.split_page(did, d, src, tmp, raw.get("raw_duration"), 2160 if big else 1080,
                                        raw.get("raw_input"))
                    out_dir, results = make_variants(src, tmp / "out", styles,
                                                     seed=int(did[1:]), subject=d["subject"],
                                                     review_page=False, copy=copy,
                                                     max_width=2160 if big else 1080,
                                                     hints=d.get("hints"),
                                                     proxy=d.get("source") == "drive",
                                                     part=d.get("part"), window=d.get("window"),
                                                     review=self.cfg.get("review", {}).get("enabled", True),
                                                     **raw)
                except Exception as e:  # noqa: BLE001 - report any failure to the owner
                    d["status"] = "failed"
                    d["error"] = str(e)[:300]
                    self.say(f"⚠️ Couldn't process that video ({did}): {d['error']}")
                    self.checkpoint()
                    continue
                d.pop("attempts", None)
                if self.held_back(did, d, out_dir, results):   # nothing passed the reviewer
                    self.checkpoint()
                    continue
                d["beats"] = self.beats_summary(out_dir, results)
                self.look_over(out_dir, results)
                self.send_pages_sheet(d, out_dir)
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
                        self.checkpoint()
                        continue
                    d["status"] = "rendered"
                    self.checkpoint()
                    continue
                for r in results:
                    vid = new_id(self.state, "v")
                    created = self.clock()
                    deadline = created + timedelta(hours=hours) if mode == "approve_or_auto" else None
                    if mode == "auto":
                        note = "\U0001f916 Auto mode: queued to post."
                    elif deadline:
                        note = f"⏱ Posts automatically {self.fmt_time(deadline)} unless you skip it."
                    elif self.drops_mode:
                        note = "Tap \u2705 Keep to add it to your drops, \u274c Skip if you don't like it."
                    else:
                        note = "Waiting for your OK."
                    text = (f"\U0001f3ac {STYLE_LABELS[r['name']]} · {r['seconds']:.0f}s · {vid}"
                    f"{' · drawing ' + d['part']['label'] if d.get('part') else ''}\n"
                            f"Hook: “{r['hook']}”\n\n{r['caption']}\n\n{note}")
                    if r.get("look"):
                        text += f"\n{r['look']}"
                    if links.get(r["name"]):
                        text += f"\n\U0001f39e Full quality: {links[r['name']]}"
                    sent = self.bot.send_video(
                        self.owner, out_dir / r.get("tg_file", r["file"]), caption=text,
                        reply_markup=None if mode == "auto" else pending_markup(vid, self.drops_mode),
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
                self.checkpoint()
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
        from ..media import preview_copy
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
