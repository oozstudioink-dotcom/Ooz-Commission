"""The bot's memory (state.json) kept privately in Google Drive instead of the public repo.

Drive > <root>/Bot memory/state.json is the source of truth. The first run moves the repo copy
there (and leaves a marker so the workflow deletes the repo copy). If Drive can't be read, the
run stops: starting from a blank memory would re-process everything and send duplicates.
"""
from __future__ import annotations

import json
from pathlib import Path

from .state import EMPTY, STATE_PATH, load_state, save_state

FOLDER = "Bot memory"
NAME = "state.json"
MARKER = STATE_PATH.parent / ".in_drive"


class MemoryError_(RuntimeError):
    pass


class DriveMemory:
    def __init__(self, drive):
        self.drive = drive
        self.folder_id: str | None = None
        self.file_id: str | None = None

    def load(self, local: Path = STATE_PATH) -> dict:
        try:
            self.folder_id = self.drive.folder(FOLDER)
            f = self.drive.find_file(NAME, self.folder_id)
            if f:
                self.file_id = f["id"]
                data = json.loads(self.drive.read_text(f["id"]))
                return {**json.loads(json.dumps(EMPTY)), **data}
        except Exception as e:  # noqa: BLE001
            raise MemoryError_(f"couldn't read the bot's memory from Drive ({str(e)[:200]}); "
                               "nothing was changed, the next run tries again") from None
        state = load_state(local)            # first time: move the repo copy to Drive
        self.save(state)
        return state

    def save(self, state: dict, local: Path = STATE_PATH) -> None:
        save_state(state, local)             # local copy for this run only (no longer committed)
        text = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        self.file_id = self.drive.write_text(NAME, self.folder_id or self.drive.folder(FOLDER), text,
                                             self.file_id)
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text("bot memory lives in Google Drive\n")


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
