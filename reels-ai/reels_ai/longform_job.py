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


def list_projects(drive: Drive) -> list[dict]:
    return [f for f in drive.list(drive.folder("Longform"), "folder") if f["name"] != "Finished"]


def run(project: str) -> None:
    cfg = load_config()
    lcfg, pcfg = cfg.get("longform", {}), cfg.get("panels", {})
    env = [os.environ.get(k, "").strip() for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")]
    if not all(env):
        raise SystemExit("needs YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN (with the drive scope)")
    drive = Drive(*env, root=cfg.get("drive", {}).get("root", "Ooz Reels"))
    t0 = time.time()

    folder = next((f for f in list_projects(drive) if f["name"].lower() == project.lower()), None)
    if not folder:
        names = ", ".join(f["name"] for f in list_projects(drive)) or "none yet"
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

    done = drive.folder("Done/Longform")
    episode = episode_from_names([f["name"] for f in drive.list(done, "folder")],
                                 int(lcfg.get("next_episode", 3)))
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
                                         res["raw"], res.get("tracks"), lcfg.get("music_source", "YouTube Audio Library"))

        ep_folder = drive.folder(f"Done/Longform/EP {episode:02d} - {folder['name']}")
        up = drive.upload(res["video"], ep_folder)
        drive.upload(res["thumbnail"], ep_folder, mime="image/png")
        desc_file = work / "description.txt"
        desc_file.write_text(f"{title}\n\n{desc}\n")
        drive.upload(desc_file, ep_folder, f"EP {episode:02d} - YouTube title and description.txt", "text/plain")

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
            drive.move(folder["id"], drive.folder("Longform/Finished"), drive.folder("Longform"))
        except Exception as e:  # noqa: BLE001
            print(f"warn: couldn't move the project folder: {e}")
        lines.append(f"\n(took {(time.time() - t0) / 60:.0f} min; the session files moved to Longform › Finished)")
        _notify("\n".join(lines))
    except Exception as e:
        _notify(f"❌ LongDraw EP. {episode:02d} · {folder['name']} failed: {str(e)[:300]}")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
