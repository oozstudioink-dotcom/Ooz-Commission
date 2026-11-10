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

LAYOUT = ["Inbox", "Inbox/Processed", "Longform", "Longform/Finished", "Beats", "Thumbnail Style",
          "Side Panels", "Done", "Done/Shorts", "Done/Longform"]


class DriveError(RuntimeError):
    pass


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


CAMERA = re.compile(r"(IMG|VID|MVI|DSC|PXL|GOPR|DJI|RPReplay|Screen ?Recording)([ _\-]|\d|$)", re.I)
# downloaded livestreams / screen recordings are titled, not named after a character
STREAM = re.compile(r"\b(live|livestream|stream|replay|tiktok|tik tok|go live|lets go)\b|!", re.I)
TAGS = [   # (pattern, hint) - written after " - " in a file or folder name, any order/case
    (re.compile(r"^(ink[ -]?only|no[ -]?pencil|no[ -]?sketch|straight([ -]?to)?[ -]?ink|ink)$", re.I),
     ("pencil", False)),
    (re.compile(r"^(pencil|pencils|sketch|sketched|pencil[ -]?first)$", re.I), ("pencil", True)),
    (re.compile(r"^(continued|cont|continue|continuing|same[ -]?page)$", re.I), ("continued", True)),
]


def _stem(name: str) -> str:
    return re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "").strip()


def parse_name(name: str) -> tuple[str | None, dict]:
    """'Sasuke - ink only - continued.MOV' -> ('Sasuke', {'pencil': False, 'continued': True}).
    '3 - Kakashi.MOV' (a page's 3rd drawing) -> ('Kakashi', {}).

    The character is everything before the first ' - ' (camera names like IMG_1234 give None,
    a trailing take number is dropped). Tags after it tell the editor how the piece was drawn;
    unknown tags are kept in hints['unknown'] so the bot can say it didn't understand them."""
    stem = urllib.parse.unquote_plus(_stem(name)).strip()
    parts = [p.strip() for p in re.split(r"\s+[-\u2013\u2014]\s+|\s*\[|\]\s*", stem) if p and p.strip()]
    hints: dict = {}
    number = re.compile(r"(part|pt|session|take|clip|drawing)?\s*#?\d+", re.I)
    if len(parts) > 1 and number.fullmatch(parts[0]) and not any(rx.match(parts[1]) for rx, _ in TAGS):
        parts = parts[1:]          # '3 - Kakashi - ink only': a page's 3rd drawing, named Kakashi
    head = parts[0] if parts else ""
    for part in parts[1:]:
        for tag in re.split(r"\s*[,+&]\s*", part):
            if not tag:
                continue
            hit = next((h for rx, h in TAGS if rx.match(tag.strip())), None)
            if hit:
                hints[hit[0]] = hit[1]
            else:
                hints.setdefault("unknown", []).append(tag.strip())
    if not head or CAMERA.match(head) or number.fullmatch(head) or STREAM.search(head):
        return None, hints
    head = re.sub(r"[ _\-]*(\(\d+\)|\d+)$", "", head).replace("_", " ").strip()
    return (head[:60] or None), hints


def subject_from_name(name: str) -> str | None:
    """'Shinobi Undercover 2.MOV' -> 'Shinobi Undercover'; camera names like IMG_1234 -> None."""
    return parse_name(name)[0]


def part_number(name: str) -> int | None:
    """Order of a session inside a LongDraw folder: '1.MOV', 'Part 2', 'Sasuke 03', '04 - pencil'."""
    stem = _stem(name)
    m = (re.search(r"(?:part|pt|session|take|clip|drawing|#)\s*#?(\d+)", stem, re.I)
         or re.match(r"\s*(\d+)", stem)
         or re.search(r"(\d+)\s*$", re.split(r"\s+[-\u2013\u2014]\s+", stem)[0]))
    return int(m.group(1)) if m else None


def session_order(f: dict) -> tuple:
    """Sort key: numbered parts in ascending number order (2 before 10), then by upload time."""
    n = part_number(f.get("name", ""))
    return (n is None, n if n is not None else 0, f.get("createdTime", ""), f.get("name", ""))


def describe_hints(hints: dict) -> str:
    bits = []
    if hints.get("pencil") is False:
        bits.append("ink only")
    elif hints.get("pencil") is True:
        bits.append("pencil first")
    if hints.get("continued"):
        bits.append("continued page")
    return " \u00b7 ".join(bits)


RETRY_CODES = {429, 500, 502, 503, 504}


def _retry(fn, tries: int = 4, wait: float = 3.0):
    """Run fn(); on Google's transient errors (rate limit, 5xx, dropped connection) wait and retry."""
    import time
    for attempt in range(tries):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES or attempt == tries - 1:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if isinstance(e, urllib.error.HTTPError) or attempt == tries - 1:
                raise
        time.sleep(wait * (2 ** attempt))


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
    def fresh_token(self) -> str:
        """Force a new access token (they last ~1 h; long renders ask again per session)."""
        self._access = None
        return self.token()

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
            r = _retry(lambda: urllib.request.urlopen(req, timeout=self.timeout))
        except urllib.error.HTTPError as e:
            msg = _describe(e)
            if e.code == 403 and "insufficient" in msg.lower():
                msg += " - the Google token is missing the Drive permission; regenerate it with the drive scope"
            raise DriveError(msg) from None
        if raw:
            return r                            # still open: the caller reads (and closes) it
        with r:
            return json.load(r)

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

    def about(self) -> dict:
        return self._req("GET", f"{self.api}/about", {"fields": "storageQuota"})

    def move(self, file_id: str, to_folder: str, from_folder: str) -> None:
        self._req("PATCH", f"{self.api}/files/{file_id}",
                  {"addParents": to_folder, "removeParents": from_folder, "fields": "id"}, body={})

    def rename(self, file_id: str, name: str) -> None:
        self._req("PATCH", f"{self.api}/files/{file_id}", {"fields": "id"}, body={"name": name})

    def thumbnail(self, file_id: str, dst: Path) -> Path | None:
        """Drive's preview frame of a video (None when Drive hasn't made one)."""
        meta = self._req("GET", f"{self.api}/files/{file_id}", {"fields": "thumbnailLink"})
        link = (meta or {}).get("thumbnailLink")
        if not link:
            return None
        link = re.sub(r"=s\d+$", "=s800", link)             # a bigger preview than the default
        with self._req("GET", link, raw=True) as r:
            Path(dst).write_bytes(r.read())
        return Path(dst)

    def find_file(self, name: str, folder_id: str) -> dict | None:
        return self._find(name, folder_id, folder=False)

    def read_text(self, file_id: str) -> str:
        with self._req("GET", self.media_url(file_id), raw=True) as r:
            return r.read().decode("utf-8")

    def write_text(self, name: str, folder_id: str, text: str, file_id: str | None = None,
                   mime: str = "application/json") -> str:
        """Create or overwrite a small text file in one request. Returns its id."""
        boundary = "ooz-memory-boundary"
        meta = {"name": name, "mimeType": mime, **({} if file_id else {"parents": [folder_id]})}
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + text.encode("utf-8") + \
            f"\r\n--{boundary}--".encode()
        url = (f"{self.upload_url}/{file_id}" if file_id else self.upload_url) + "?uploadType=multipart&fields=id"
        req = urllib.request.Request(url, data=body, method="PATCH" if file_id else "POST", headers={
            "Authorization": f"Bearer {self.token()}",
            "Content-Type": f"multipart/related; boundary={boundary}"})
        def send():
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)["id"]
        try:
            return _retry(send)
        except urllib.error.HTTPError as e:
            raise DriveError(f"couldn't save {name}: {_describe(e)}") from None
        except OSError as e:
            raise DriveError(f"couldn't save {name}: {e}") from None

    def trash(self, file_id: str) -> None:
        """Move a file to Drive's Trash (restorable for 30 days). The bot never deletes outright."""
        self._req("PATCH", f"{self.api}/files/{file_id}", {"fields": "id"}, body={"trashed": True})

    def media_url(self, file_id: str) -> str:
        return f"{self.api}/files/{file_id}?alt=media"

    def download(self, file_id: str, dst: Path) -> Path:
        with self._req("GET", self.media_url(file_id), raw=True) as r, open(dst, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        return dst

    def upload(self, path: Path, folder_id: str, name: str | None = None,
               mime: str = "video/mp4") -> dict:
        """Resumable, chunked upload (works for multi-GB files). Returns {id, webViewLink}."""
        path = Path(path)
        size = path.stat().st_size
        with self._req("POST", self.upload_url, {"uploadType": "resumable", "fields": "id,webViewLink"},
                       body={"name": name or path.name, "parents": [folder_id]},
                       headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size)},
                       raw=True) as start:
            session = start.headers["Location"]
        with open(path, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                req = urllib.request.Request(session, data=chunk, method="PUT", headers={
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"})
                for attempt in range(4):                # a dropped connection resends this chunk
                    try:
                        with urllib.request.urlopen(req, timeout=600) as r:
                            return json.load(r)         # 200/201: finished
                    except urllib.error.HTTPError as e:
                        if e.code == 308:               # chunk stored, send the next one
                            break
                        if e.code not in RETRY_CODES or attempt == 3:
                            raise DriveError(f"upload failed: {_describe(e)}") from None
                    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                        if attempt == 3:
                            raise DriveError(f"upload failed: {e}") from None
                    import time
                    time.sleep(3 * (2 ** attempt))
                    req = urllib.request.Request(session, data=chunk, method="PUT", headers=dict(req.headers))
                offset = end + 1


def _frames(path: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def remote_duration(url: str, token: str) -> float | None:
    """Length of a recording on Drive in seconds (when Drive's metadata doesn't say)."""
    out = subprocess.run(["ffprobe", "-v", "error", "-headers", f"Authorization: Bearer {token}\r\n",
                          "-show_entries", "format=duration", "-of", "csv=p=0", url],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def make_proxy(url: str, token: str, dst: Path, duration_s: float | None, max_side: int = 2560,
               target_frames: int = 2700) -> Path:
    """Stream a raw recording and write a small 30 fps timelapse of it.

    Long recordings (>= 15 min) use keyframes only (iPhone: ~1 per second), so ffmpeg decodes
    a tiny fraction of the 4K stream. Shorter ones sample evenly to ~target_frames frames. Some
    files (e.g. TikTok live downloads) have very few keyframes: if keyframes give too few frames,
    the whole recording is sampled evenly instead."""
    scale = (f"scale='min(iw,{max_side})':'min(ih,{max_side})':force_original_aspect_ratio=decrease,"
             "scale=trunc(iw/2)*2:trunc(ih/2)*2")

    def run(keyframes: bool, dur: float) -> None:
        cmd = ["ffmpeg", "-v", "error", "-y", "-headers", f"Authorization: Bearer {token}\r\n"]
        if keyframes:
            cmd += ["-skip_frame", "nokey", "-i", url, "-vf", f"{scale},setpts=N/30/TB"]
        else:
            rate = max(0.05, target_frames / max(dur, 1))
            cmd += ["-i", url, "-vf", f"fps={rate:.4f},{scale},setpts=N/30/TB"]
        cmd += ["-r", "30", "-an", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt",
                "yuv420p", str(dst)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise DriveError(f"couldn't read the recording: {(res.stderr or '').strip()[-300:]}")

    dur = duration_s or remote_duration(url, token) or 30 * 60
    if dur >= 15 * 60:
        run(True, dur)
        if _frames(dst) >= min(900, target_frames // 3):
            return dst
        print(f"keyframes gave only {_frames(dst)} frames; sampling the whole recording instead")
    run(False, dur)
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
