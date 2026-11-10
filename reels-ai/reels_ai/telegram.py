"""Tiny Telegram Bot API client (stdlib only, so the hourly check needs no installs)."""
from __future__ import annotations

import json
import mimetypes
import urllib.request
import uuid
from pathlib import Path

API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class Bot:
    def __init__(self, token: str, timeout: int = 60):
        self.token = token
        self.timeout = timeout

    # ------------------------------------------------------------ transport
    def _call(self, method: str, params: dict | None = None, files: dict | None = None) -> object:
        url = f"{API}/bot{self.token}/{method}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if files:
            body, ctype = _multipart(params, files)
        else:
            body, ctype = json.dumps(params).encode(), "application/json"
        req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            data = json.loads(e.read() or b"{}")
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', data)}")
        return data["result"]

    # ------------------------------------------------------------ methods used
    def get_updates(self, offset: int) -> list[dict]:
        return self._call("getUpdates", {"offset": offset, "timeout": 0,
                                         "allowed_updates": ["message", "callback_query"]})

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                     force_reply: bool = False) -> dict:
        if force_reply:
            reply_markup = {"force_reply": True, "input_field_placeholder": "New caption"}
        return self._call("sendMessage", {"chat_id": chat_id, "text": text,
                                          "reply_markup": reply_markup,
                                          "link_preview_options": {"is_disabled": True}})

    def send_photo(self, chat_id: int, photo: Path, caption: str = "", reply_markup: dict | None = None) -> dict:
        return self._call("sendPhoto", {"chat_id": chat_id, "caption": caption[:1024],
                                        "reply_markup": json.dumps(reply_markup) if reply_markup else None},
                          files={"photo": photo})

    def send_video(self, chat_id: int, video: Path | str, caption: str = "",
                   reply_markup: dict | None = None, width: int | None = None,
                   height: int | None = None, duration: int | None = None) -> dict:
        params = {"chat_id": chat_id, "caption": caption[:1024], "supports_streaming": True,
                  "width": width, "height": height, "duration": duration,
                  "reply_markup": json.dumps(reply_markup) if reply_markup else None}
        if isinstance(video, Path):
            return self._call("sendVideo", params, files={"video": video})
        params["video"] = video          # an existing file_id: re-sending is free and instant
        params["reply_markup"] = reply_markup
        return self._call("sendVideo", params)

    def send_media_group(self, chat_id: int, videos: list[Path], caption: str = "") -> list[dict]:
        """Send up to 10 videos as one album. Returns the sent messages (same order)."""
        media, files = [], {}
        for i, path in enumerate(videos[:10]):
            item = {"type": "video", "media": f"attach://v{i}", "supports_streaming": True,
                    "width": 720, "height": 1280}
            if i == 0 and caption:
                item["caption"] = caption[:1024]
            media.append(item)
            files[f"v{i}"] = path
        return self._call("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media)}, files=files)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        try:
            self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                  "reply_markup": reply_markup or {"inline_keyboard": []}})
        except TelegramError as e:      # message too old / unchanged: not worth failing a run over
            if "not modified" not in str(e):
                print(f"warn: {e}")

    def delete_messages(self, chat_id: int, message_ids: list[int]) -> bool:
        """Delete up to 100 messages at once. Telegram skips ids it can't find; it refuses
        messages older than 48 hours. Returns False when Telegram refused the batch."""
        try:
            return bool(self._call("deleteMessages", {"chat_id": chat_id,
                                                      "message_ids": json.dumps(message_ids[:100])}))
        except TelegramError as e:
            print(f"warn: delete: {e}")
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass                         # callbacks expire; the message edit still tells the user

    def download(self, file_id: str, dst: Path) -> Path:
        info = self._call("getFile", {"file_id": file_id})
        url = f"{API}/file/bot{self.token}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=300) as r, open(dst, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        return dst


def _multipart(fields: dict, files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode()
        out += (v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list, bool)) else str(v)).encode()
        out += b"\r\n"
    for k, path in files.items():
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                f"filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += path.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"
