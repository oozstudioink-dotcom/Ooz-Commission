"""The bot's memory, kept privately in Google Drive (never in the public repo).

Drive > <root>/Bot memory/
    state.json            everything the bot knows: recordings, Shorts, drops, ratings, settings
    Backups/              one copy per day of the memory as it was at the start of that day
    Edit maps/            what was seen and heard in each recording (written by the editor)

Safety rules:
  - If Drive can't be read, the run stops. Starting from a blank memory would re-make everything
    and send duplicates.
  - If state.json is damaged, the newest backup that reads correctly is used, and you are told.
  - The memory is checked and repaired on load (missing sections, wrong shapes).
  - A save that would lose most of the recordings or Shorts is refused: something went wrong,
    and yesterday's backup is still there.
  - Before the first save of each day, the memory as loaded is copied to Backups/.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .state import EMPTY, STATE_PATH, load_state, save_state

FOLDER = "Bot memory"
BACKUPS = "Bot memory/Backups"
NAME = "state.json"
MARKER = STATE_PATH.parent / ".in_drive"
SCHEMA = 2
GUARD_MIN = 10            # with at least this many recordings/Shorts, losing over half blocks the save


class MemoryError_(RuntimeError):
    pass


def tidy(data) -> dict:
    """A memory with every section present and the right shape. Raises ValueError if it isn't
    a memory at all."""
    if not isinstance(data, dict):
        raise ValueError("the memory file doesn't hold a memory")
    state = {**json.loads(json.dumps(EMPTY)), **data}
    for key, empty in EMPTY.items():
        if empty is not None and not isinstance(state.get(key), type(empty)):
            state[key] = json.loads(json.dumps(empty))
    for key in ("drawings", "variants"):
        state[key] = {k: v for k, v in state[key].items() if isinstance(v, dict)}
    state["schema"] = SCHEMA
    return state


class DriveMemory:
    def __init__(self, drive, today=None):
        self.drive = drive
        self.folder_id: str | None = None
        self.file_id: str | None = None
        self.loaded_text: str | None = None          # the memory exactly as it was read
        self.loaded_counts: dict[str, int] = {}
        self.restored_from: str | None = None        # name of the backup used, if state.json was damaged
        self.today = today or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def load(self, local: Path = STATE_PATH) -> dict:
        try:
            self.folder_id = self.drive.folder(FOLDER)
            f = self.drive.find_file(NAME, self.folder_id)
            if f:
                self.file_id = f["id"]
                text = self.drive.read_text(f["id"])
        except Exception as e:  # noqa: BLE001
            raise MemoryError_(f"couldn't read the bot's memory from Drive ({str(e)[:200]}); "
                               "nothing was changed, the next run tries again") from None
        if not f:
            state = tidy(load_state(local))            # first time: move the repo copy to Drive
            self.save(state, local)
            return state
        try:
            state = tidy(json.loads(text))
            self.loaded_text = text
        except ValueError:
            state = self._restore()
        self.loaded_counts = {k: len(state[k]) for k in ("drawings", "variants")}
        return state

    def _restore(self) -> dict:
        """state.json is damaged: the newest backup that reads correctly."""
        try:
            folder = self.drive.folder(BACKUPS)
            backups = sorted((b for b in self.drive.list(folder, "") if b["name"].startswith("state-")),
                             key=lambda b: b["name"], reverse=True)
            for b in backups:
                try:
                    text = self.drive.read_text(b["id"])
                    state = tidy(json.loads(text))
                except ValueError:
                    continue
                self.loaded_text, self.restored_from = text, b["name"]
                state["memory_restored"] = b["name"]
                print(f"memory: state.json was damaged; restored from {b['name']}")
                return state
        except Exception as e:  # noqa: BLE001
            print(f"warn: memory backups: {e}")
        raise MemoryError_("the bot's memory in Drive is damaged and no backup could be read; "
                           "nothing was changed") from None

    def _backup(self) -> None:
        """Once a day: keep the memory as it was loaded, before anything is written over it."""
        if not self.loaded_text:
            return
        try:
            folder = self.drive.folder(BACKUPS)
            name = f"state-{self.today()}.json"
            if not self.drive.find_file(name, folder):
                self.drive.write_text(name, folder, self.loaded_text)
        except Exception as e:  # noqa: BLE001 - a missed backup must not stop the bot saving
            print(f"warn: couldn't back up the memory: {e}")

    def save(self, state: dict, local: Path = STATE_PATH, marker: Path | None = None) -> None:
        for key, before in self.loaded_counts.items():
            if before >= GUARD_MIN and len(state.get(key, {})) < before / 2:
                raise MemoryError_(f"refusing to save: the memory would go from {before} to "
                                   f"{len(state.get(key, {}))} {key}. Nothing was written; "
                                   "the copy in Drive and its backups are unchanged.")
        self._backup()
        state["schema"] = SCHEMA
        save_state(state, local)             # local copy for this run only (no longer committed)
        text = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        self.file_id = self.drive.write_text(NAME, self.folder_id or self.drive.folder(FOLDER), text,
                                             self.file_id)
        marker = marker or (local.parent / MARKER.name)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("bot memory lives in Google Drive\n")


def owner_chat_id(drive=None) -> int | None:
    """For one-off messages (alerts, LongDraw job): the owner's chat id from Drive or the repo copy."""
    import os
    if os.environ.get("TELEGRAM_OWNER_ID"):
        return int(os.environ["TELEGRAM_OWNER_ID"])
    if drive is not None:
        try:
            f = drive.find_file(NAME, drive.folder(FOLDER))
            if f:
                return json.loads(drive.read_text(f["id"])).get("owner_chat_id")
        except Exception as e:  # noqa: BLE001
            print(f"warn: memory: {e}")
    return load_state().get("owner_chat_id")
