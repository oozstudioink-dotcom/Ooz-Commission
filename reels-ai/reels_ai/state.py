"""Bot memory: a JSON file committed back to the repo after every run."""
from __future__ import annotations

import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "state" / "state.json"
CONFIG_PATH = ROOT / "config.toml"

EMPTY = {
    "owner_chat_id": None,
    "update_offset": 0,
    "paused": False,
    "awaiting_caption": None,     # variant id the owner is writing a caption for
    "next_id": 1,
    "drawings": {},               # id -> {subject, file_id, received, status, error}
    "variants": {},               # id -> {drawing, style, hook, caption, file_id, message_id,
                                  #        status, created, deadline, decided_by, posts{platform: {...}}}
    "style_uses": {},
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        data = json.loads(path.read_text())
        return {**json.loads(json.dumps(EMPTY)), **data}
    return json.loads(json.dumps(EMPTY))


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def new_id(state: dict, prefix: str) -> str:
    n = state["next_id"]
    state["next_id"] = n + 1
    return f"{prefix}{n}"
