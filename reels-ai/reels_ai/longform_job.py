"""The LongDraw job (runs in its own GitHub workflow: hours of 4K work must not hold up the
hourly bot). Reads bot state read-only (for the Telegram chat) and never writes it; the
episode number comes from Drive (Done/Longform/"EP 03 - ..." folders).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .drive import Drive
from .drive import parse_name, session_order
from .longform import Session, build, episode_from_names, subject_title, youtube_text
from .state import load_config, load_state
from .telegram import Bot

IMAGE = ("image/",)


_OWNER: list = []


def _owner():
    """Owner chat id, read once (from the bot's memory in Drive, or the repo copy)."""
    if not _OWNER:
        from .memory import owner_chat_id
        drive = None
        try:
            env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
            if all(env):
                drive = Drive(*env, root=load_config().get("drive", {}).get("root", "Ooz Reels"))
        except Exception as e:  # noqa: BLE001
            print(f"warn: {e}")
        _OWNER.append(owner_chat_id(drive))
    return _OWNER[0]


def _notify(text: str) -> None:
    token, owner = os.environ.get("TELEGRAM_BOT_TOKEN", ""), _owner()
    print(text)
    if token.strip() and owner:
        import re
        m = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", token)
        if m:
            try:
                Bot(m.group(0)).send_message(owner, text[:4000])
            except Exception as e:  # noqa: BLE001
                print(f"warn: telegram: {e}")


def list_projects(drive: Drive, cfg: dict | None = None) -> list[dict]:
    """Project folders in Longform, and the lessons inside each series folder. Each has "path":
    its place under Longform ('Sasuke', or 'Educational Drawing/How to Draw/Lesson name')."""
    from .series import all_series, roots
    skip = {"finished"} | roots(cfg or {})
    out = [{**f, "path": f["name"]} for f in drive.list(drive.folder("Longform"), "folder")
           if f["name"].lower() not in skip]
    for s in all_series(cfg or {}):
        for f in drive.list(drive.folder(f"Longform/{s['folder']}"), "folder"):
            out.append({**f, "path": f"{s['folder']}/{f['name']}"})
    return out


def run(project: str) -> None:
    cfg = load_config()
    lcfg, pcfg = cfg.get("longform", {}), cfg.get("panels", {})
    env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    if not all(env):
        raise SystemExit("needs YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (with the drive scope)")
    drive = Drive(*env, root=cfg.get("drive", {}).get("root", "Ooz Reels"))
    t0 = time.time()

    from .series import of_path
    projects = list_projects(drive, cfg)
    want = project.strip("/").lower()
    folder = next((f for f in projects if want in (f["path"].lower(), f["name"].lower())), None)
    series = of_path(cfg, folder["path"]) if folder else None
    if series:
        lcfg = {**lcfg, "series": series}
    if not folder:
        names = ", ".join(f["name"] for f in projects) or "none yet"
        _notify(f"⚠️ No Longform folder called “{project}”. Projects: {names}")
        raise SystemExit(1)
    files = drive.list(folder["id"], "")
    videos = [f for f in files if f.get("mimeType", "").startswith("video/")]
    if not videos:
        _notify(f"⚠️ “{folder['name']}” has no videos yet.")
        raise SystemExit(1)
    videos.sort(key=session_order)            # 1, 2, ... 10 by the number in the name, then upload time
    _subj, _hints = parse_name(folder["name"])   # 'Sasuke - ink only' -> 'Sasuke'
    title_name = " - ".join([_subj, *_hints.get("unknown", [])]) if _subj else folder["name"]
    # 'Deku - My Hero Academia' keeps the series name
    _notify("\U0001f4cb Session order: " + ", ".join(f["name"] for f in videos))

    done_path = f"Done/Longform/{series['name']}" if series else "Done/Longform"   # a series counts its own lessons
    done = drive.folder(done_path)
    episode = episode_from_names([f["name"] for f in drive.list(done, "folder")],
                                 1 if series else int(lcfg.get("next_episode", 3)))
    tag = f"{series.get('badge', 'Lesson').title()} {episode:02d}" if series else f"EP {episode:02d}"
    raw_hours = sum(int((f.get("videoMediaMetadata") or {}).get("durationMillis", 0)) for f in videos) / 3.6e6
    _notify(f"\U0001f3ac LongDraw EP. {episode:02d} · {folder['name']}: starting on {len(videos)} "
            f"session(s), about {raw_hours:.1f} h of footage. This takes a while; I'll message you when it's done.")

    work = Path(tempfile.mkdtemp(dir=os.environ.get("LONGFORM_WORK", None)))
    try:
        beats = []
        beat_dir = work / "beats"
        beat_dir.mkdir()
        for b in drive.list(drive.folder("Beats"), "audio"):     # keep names: they become the credits
            safe = re.sub(r'[\\/:*?"<>|]', "_", b["name"]) or f"beat_{len(beats)}.mp3"
            beats.append(drive.download(b["id"], beat_dir / f"{len(beats):03d}_{safe}"))
        side = drive.list(drive.folder("Side Panels"), "video")
        side.sort(key=lambda f: ("commission" not in f["name"].lower(), f.get("createdTime", "")))
        advert = drive.download(side[0]["id"], work / f"advert{Path(side[0]['name']).suffix}") if side else None
        art_name = lcfg.get("art_panel_name", "YT_Longform_Finished_Art_Panel").lower()
        arts = [f for f in files if f.get("mimeType", "").startswith(IMAGE) and f["name"].lower().startswith(art_name)]
        art = drive.download(arts[0]["id"], work / f"art{Path(arts[0]['name']).suffix}") if arts else None

        class DriveSession(Session):
            def hdrs(self) -> list[str]:           # fresh token for every read (they expire after ~1 h)
                self.headers = f"Authorization: Bearer {drive.fresh_token()}\r\n"
                return super().hdrs()

        sessions = []
        for i, f in enumerate(videos):
            dur = int((f.get("videoMediaMetadata") or {}).get("durationMillis", 0)) / 1000
            s = DriveSession(drive.media_url(f["id"]), f["name"], subject_title(f["name"], i), dur,
                             headers=f"Authorization: Bearer {drive.token()}\r\n")
            sessions.append(s)

        res = build(sessions, work / "out", title_name, episode, lcfg, beats, advert, pcfg, art,
                    log=print)
        mins = res["seconds"] / 60
        title, desc, tags = youtube_text(title_name, episode, res["chapters"], pcfg, res["speed"],
                                         res["raw"], res.get("tracks"), lcfg.get("music_source", "YouTube Audio Library"), series=series)

        ep_folder = drive.folder(f"{done_path}/{tag} - {folder['name']}")
        up = drive.upload(res["video"], ep_folder)
        drive.upload(res["thumbnail"], ep_folder, mime="image/png")
        desc_file = work / "description.txt"
        desc_file.write_text(f"{title}\n\n{desc}\n")
        drive.upload(desc_file, ep_folder, f"{tag} - YouTube title and description.txt", "text/plain")

        lines = [f"✅ LongDraw EP. {episode:02d} · {folder['name']} is ready",
                 f"{mins:.0f} min · {res['raw'] / 3600:.1f} h of footage · {res['speed']:.1f}x speed",
                 f"Drive: {up.get('webViewLink', 'Done > Longform')}"]
        if lcfg.get("youtube_upload", True):
            from .youtube import YouTube
            yt = YouTube(*env)
            try:
                y = yt.upload_file(res["video"], title, desc, tags, lcfg.get("privacy", "private"))
                lines.append(f"YouTube: {y['permalink']}")
                if lcfg.get("privacy", "private") == "private":
                    lines.append("\U0001f512 Uploaded privately. When you're ready: YouTube Studio \u203a this video "
                                 "\u203a Visibility \u203a Schedule (pick your LongDraw slot).")
                if y.get("note"):
                    lines.append(f"⚠️ {y['note']}")
                try:
                    yt.set_thumbnail(y["id"], res["thumbnail_yt"])
                except Exception as e:  # noqa: BLE001
                    lines.append(f"Thumbnail not set ({str(e)[:120]}). Custom thumbnails need a phone-verified "
                                 "channel: youtube.com/verify. The thumbnail is in the Drive folder.")
            except Exception as e:  # noqa: BLE001
                lines.append(f"⚠️ YouTube upload failed ({str(e)[:160]}). The video is in Drive; "
                             "upload it by hand with the title and description file there.")
        lines += ["", "Chapters:", res["chapters"]]
        try:
            drive.move_to(folder["id"], drive.folder("Longform/Finished"))
        except Exception as e:  # noqa: BLE001
            print(f"warn: couldn't move the project folder: {e}")
        lines.append(f"\n(took {(time.time() - t0) / 60:.0f} min; the session files moved to Longform › Finished)")
        _notify("\n".join(lines))
    except Exception as e:
        _notify(f"❌ LongDraw EP. {episode:02d} · {folder['name']} failed: {str(e)[:300]}")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def reupload(query: str) -> None:
    """Upload a finished long video from Drive > Done > Longform to YouTube again, privately, with
    its saved title, description and thumbnail (for a copy stuck on 'processing')."""
    from .series import all_series
    from .thumbnail import youtube_jpeg
    from .youtube import YouTube
    cfg = load_config()
    env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    drive = Drive(*env, root=cfg.get("drive", {}).get("root", "Ooz Reels"))
    places = ["Done/Longform"] + [f"Done/Longform/{s_['name']}" for s_ in all_series(cfg)]
    want = re.sub(r"[^a-z0-9]+", "", query.lower())
    hits = [f for p in places for f in drive.list(drive.folder(p), "folder")
            if want in re.sub(r"[^a-z0-9]+", "", f["name"].lower())]
    if not hits:
        _notify(f"\u26a0\ufe0f No finished video in Drive \u203a Done \u203a Longform matches \u201c{query}\u201d.")
        raise SystemExit(1)
    folder = max(hits, key=lambda f: f.get("createdTime", ""))
    files = drive.list(folder["id"], "")
    video = next((f for f in files if f.get("mimeType", "").startswith("video/")), None)
    if not video:
        _notify(f"\u26a0\ufe0f \u201c{folder['name']}\u201d has no video file in it.")
        raise SystemExit(1)
    work = Path(tempfile.mkdtemp(dir=os.environ.get("LONGFORM_WORK", None)))
    try:
        title, desc = folder["name"], ""
        txt = next((f for f in files if f["name"].lower().endswith(".txt")), None)
        if txt:
            body = drive.read_text(txt["id"]).strip().split("\n", 1)
            title, desc = body[0].strip(), (body[1].strip() if len(body) > 1 else "")
        _notify(f"\U0001f4e4 Re-uploading \u201c{title}\u201d ({int(video.get('size') or 0) / 1e9:.1f} GB). "
                "This takes a while for 4K.")
        local = drive.download(video["id"], work / "video.mp4")
        yt = YouTube(*env)
        y = yt.upload_file(local, title, desc, [], cfg.get("longform", {}).get("privacy", "private"))
        lines = [f"\u2705 Re-uploaded: {y['permalink']}",
                 "It's private. Check it finishes processing in YouTube Studio, schedule it, then delete the "
                 "stuck copy there (Studio \u203a Content \u203a \u22ee \u203a Delete forever)."]
        png = next((f for f in files if f["name"].lower().endswith(".png")), None)
        if png:
            try:
                yt.set_thumbnail(y["id"], youtube_jpeg(drive.download(png["id"], work / "t.png"), work / "t.jpg"))
            except Exception as e:  # noqa: BLE001
                lines.append(f"Thumbnail not set ({str(e)[:120]}); it's in the Drive folder.")
        _notify("\n".join(lines))
    except SystemExit:
        raise
    except Exception as e:
        _notify(f"\u274c Re-upload of \u201c{query}\u201d failed: {str(e)[:300]}")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
