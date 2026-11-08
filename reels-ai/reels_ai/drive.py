"""Google Drive client (stdlib only) + raw-recording -> timelapse proxy.

Uses the same Google OAuth client and refresh token as YouTube (YT_CLIENT_ID / YT_CLIENT_SECRET /
YT_REFRESH_TOKEN); the token needs the `drive` scope. Raw 4K recordings are never downloaded
whole: ffmpeg streams them straight from Drive and keeps only what it needs.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
CHUNK = 16 * 1024 * 1024          # multiple of 256 KiB, as Drive requires

LAYOUT = ["Inbox", "Inbox/Processed", "Longform", "Beats", "Thumbnail Style", "Side Panels",
          "Done", "Done/Shorts", "Done/Longform"]


class DriveError(RuntimeError):
    pass


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def subject_from_name(name: str) -> str | None:
    """'Shinobi Undercover 2.MOV' -> 'Shinobi Undercover'; camera names like IMG_1234 -> None."""
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "").strip()
    if not stem or re.match(r"(IMG|VID|MVI|DSC|PXL|GOPR|DJI|RPReplay|Screen ?Recording)([ _\-]|\d|$)", stem, re.I):
        return None
    stem = re.sub(r"[ _\-]*(\(\d+\)|\d+)$", "", stem).replace("_", " ").strip()
    return stem[:60] or None


class Drive:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, root: str = "Ooz Reels",
                 timeout: int = 60):
        self.client_id, self.client_secret = _clean(client_id), _clean(client_secret)
        self.refresh_token = _clean(refresh_token)
        self.root_name, self.timeout = root, timeout
        self.token_url, self.api, self.upload_url = TOKEN_URL, API, UPLOAD
        self._access: str | None = None
        self._folders: dict[str, str] = {}

    # ------------------------------------------------------------------ transport
    def token(self) -> str:
        if not self._access:
            body = urllib.parse.urlencode({
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(self.token_url, data=body),
                                            timeout=self.timeout) as r:
                    self._access = json.load(r)["access_token"]
            except urllib.error.HTTPError as e:
                raise DriveError(f"Google sign-in failed: {_describe(e)}") from None
        return self._access

    def _req(self, method: str, url: str, params: dict | None = None, body: dict | None = None,
             headers: dict | None = None, raw: bool = False):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        h = {"Authorization": f"Bearer {self.token()}", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json; charset=UTF-8"
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            msg = _describe(e)
            if e.code == 403 and "insufficient" in msg.lower():
                msg += " - the Google token is missing the Drive permission; regenerate it with the drive scope"
            raise DriveError(msg) from None
        with r:
            return r if raw else json.load(r)

    # ------------------------------------------------------------------ folders/files
    def _find(self, name: str, parent: str | None, folder: bool) -> dict | None:
        q = [f"name = '{name.replace(chr(92), chr(92) * 2).replace(chr(39), chr(92) + chr(39))}'",
             "trashed = false"]
        q.append(f"'{parent}' in parents" if parent else "'root' in parents")
        if folder:
            q.append(f"mimeType = '{FOLDER}'")
        res = self._req("GET", f"{self.api}/files", {"q": " and ".join(q), "fields": "files(id,name)",
                                                     "pageSize": 10})
        files = res.get("files") or []
        return files[0] if files else None

    def folder(self, path: str = "") -> str:
        """Folder id for 'Inbox/Processed' etc. under the root, creating any missing piece."""
        parts = [p for p in path.strip("/").split("/") if p]
        parent = None
        for depth in range(len(parts) + 1):
            key = "/".join(parts[:depth])
            if key not in self._folders:
                name = parts[depth - 1] if depth else self.root_name
                found = self._find(name, parent, folder=True)
                if not found:
                    found = self._req("POST", f"{self.api}/files", {"fields": "id"},
                                      body={"name": name, "mimeType": FOLDER,
                                            **({"parents": [parent]} if parent else {})})
                self._folders[key] = found["id"]
            parent = self._folders[key]
        return parent

    def ensure_layout(self) -> dict[str, str]:
        return {p: self.folder(p) for p in LAYOUT}

    def list(self, folder_id: str, kind: str = "video") -> list[dict]:
        """Files in a folder. kind: 'video', 'audio', 'image', 'folder' or '' for everything."""
        q = [f"'{folder_id}' in parents", "trashed = false"]
        if kind == "folder":
            q.append(f"mimeType = '{FOLDER}'")
        elif kind:
            q.append(f"mimeType contains '{kind}/'")
        out, token = [], None
        while True:
            res = self._req("GET", f"{self.api}/files", {
                "q": " and ".join(q), "pageSize": 200, "orderBy": "createdTime",
                "fields": "nextPageToken,files(id,name,mimeType,size,createdTime,"
                          "videoMediaMetadata(width,height,durationMillis),webViewLink)",
                **({"pageToken": token} if token else {})})
            out += res.get("files") or []
            token = res.get("nextPageToken")
            if not token:
                return out

    def move(self, file_id: str, to_folder: str, from_folder: str) -> None:
        self._req("PATCH", f"{self.api}/files/{file_id}",
                  {"addParents": to_folder, "removeParents": from_folder, "fields": "id"}, body={})

    def media_url(self, file_id: str) -> str:
        return f"{self.api}/files/{file_id}?alt=media"

    def download(self, file_id: str, dst: Path) -> Path:
        r = self._req("GET", self.media_url(file_id), raw=True)
        with open(dst, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        return dst

    def upload(self, path: Path, folder_id: str, name: str | None = None,
               mime: str = "video/mp4") -> dict:
        """Resumable, chunked upload (works for multi-GB files). Returns {id, webViewLink}."""
        path = Path(path)
        size = path.stat().st_size
        start = self._req("POST", self.upload_url, {"uploadType": "resumable", "fields": "id,webViewLink"},
                          body={"name": name or path.name, "parents": [folder_id]},
                          headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                          raw=True)
        session = start.headers["Location"]
        with open(path, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                req = urllib.request.Request(session, data=chunk, method="PUT", headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"})
                try:
                    with urllib.request.urlopen(req, timeout=600) as r:
                        return json.load(r)             # 200/201: finished
                except urllib.error.HTTPError as e:
                    if e.code != 308:                   # 308 = chunk stored, send the next one
                        raise DriveError(f"upload failed: {_describe(e)}") from None
                offset = end + 1


def make_proxy(url: str, token: str, dst: Path, duration_s: float, max_side: int = 2560,
               target_frames: int = 1800) -> Path:
    """Stream a raw recording and write a small 30 fps timelapse of it.

    Long recordings (>= 15 min) use keyframes only (iPhone: ~1 per second), so ffmpeg decodes
    a tiny fraction of the 4K stream. Shorter ones sample evenly to ~target_frames frames.
    """
    scale = (f"scale='min(iw,{max_side})':'min(ih,{max_side})':force_original_aspect_ratio=decrease,"
             "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    cmd = ["ffmpeg", "-v", "error", "-y", "-headers", f"Authorization: Bearer {token}\r\n"]
    if duration_s >= 15 * 60:
        cmd += ["-skip_frame", "nokey", "-i", url, "-vf", f"{scale},setpts=N/30/TB"]
    else:
        rate = max(1.0, target_frames / max(duration_s, 1))
        cmd += ["-i", url, "-vf", f"fps={rate:.4f},{scale},setpts=N/30/TB"]
    cmd += ["-r", "30", "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise DriveError(f"couldn't read the recording: {(res.stderr or '').strip()[-300:]}")
    return dst


def _describe(e: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(e.read() or b"{}")
        err = data.get("error")
        if isinstance(err, dict):
            return f"{err.get('message', e)} (HTTP {e.code})"
        return f"{err}: {data.get('error_description', '')} (HTTP {e.code})"
    except (ValueError, AttributeError):
        return str(e)
