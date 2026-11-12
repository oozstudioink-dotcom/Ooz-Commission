"""YouTube client (stdlib only): channel check, Shorts upload, per-video analytics.

Auth: an OAuth client (YT_CLIENT_ID / YT_CLIENT_SECRET) plus a long-lived refresh token
(YT_REFRESH_TOKEN) created once in Google's OAuth Playground. A fresh access token is
fetched on every run.

Google forces API uploads from un-audited projects to *private*. We ask for public; if
YouTube stores it private anyway, the result says so and the owner flips it in Studio.
"""
from __future__ import annotations

import json
import time
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
ANALYTICS = "https://youtubeanalytics.googleapis.com/v2/reports"
METRICS = ["views", "averageViewDuration", "averageViewPercentage", "subscribersGained",
           "likes", "comments", "shares"]


class YouTubeError(RuntimeError):
    pass


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def shorts_title(hook: str, subject: str | None) -> str:
    """'<hook> | <subject> manga inking #Shorts', trimmed to YouTube's 100 chars."""
    tail = f" | {subject} manga inking #Shorts" if subject else " | manga inking #Shorts"
    hook = hook.strip().rstrip(".")
    return (hook[: 100 - len(tail)] + tail)[:100]


class YouTube:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, timeout: int = 60):
        self.client_id, self.client_secret = _clean(client_id), _clean(client_secret)
        self.refresh_token = _clean(refresh_token)
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise YouTubeError("YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN are all required")
        self.timeout = timeout
        self._access: str | None = None
        self.token_url, self.api, self.upload_url, self.analytics_url = TOKEN_URL, API, UPLOAD, ANALYTICS

    # ------------------------------------------------------------------ transport
    def _token(self) -> str:
        if self._access and time.time() - getattr(self, "_access_at", 0.0) <= 40 * 60:
            return self._access                        # tokens last ~1 h; the bot runs for hours
        body = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.token_url, data=body),
                                        timeout=self.timeout) as r:
                self._access = json.load(r)["access_token"]
                self._access_at = time.time()
        except urllib.error.HTTPError as e:
            msg = _describe(e)
            if "invalid_grant" in msg:
                msg += (" - the refresh token was revoked or expired. If the Google app is still in "
                        "'Testing', publish it to production and create a new refresh token.")
            raise YouTubeError(f"sign-in failed: {msg}") from None
        return self._access

    def _req(self, method: str, url: str, params: dict | None = None, body: dict | bytes | None = None,
             headers: dict | None = None, raw: bool = False):
        if params:
            url += "?" + urllib.parse.urlencode(params)
        h = {"Authorization": f"Bearer {self._token()}", **(headers or {})}
        data = None
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json; charset=UTF-8"
        elif body is not None:
            data = body
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=300 if data else self.timeout)
        except urllib.error.HTTPError as e:
            raise YouTubeError(_describe(e)) from None
        with r:
            return r if raw else json.load(r)

    # ------------------------------------------------------------------ account
    def whoami(self) -> dict:
        res = self._req("GET", f"{self.api}/channels",
                        {"part": "snippet,statistics,contentDetails", "mine": "true"})
        items = res.get("items") or []
        if not items:
            raise YouTubeError("this Google account has no YouTube channel (pick the channel when signing in)")
        ch = items[0]
        return {"id": ch["id"], "title": ch["snippet"]["title"],
                "subscribers": ch["statistics"].get("subscriberCount"),
                "videos": ch["statistics"].get("videoCount"),
                "uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"]}

    def recent_videos(self, uploads_playlist: str, limit: int = 5) -> list[dict]:
        res = self._req("GET", f"{self.api}/playlistItems",
                        {"part": "snippet,contentDetails", "playlistId": uploads_playlist,
                         "maxResults": limit})
        return [{"id": i["contentDetails"]["videoId"], "title": i["snippet"]["title"],
                 "published": i["contentDetails"].get("videoPublishedAt", "")}
                for i in res.get("items", [])]

    # ------------------------------------------------------------------ radar
    def search_shorts(self, query: str, after: str, limit: int = 25) -> list[str]:
        """Ids of the most-viewed short videos for a search, published after an RFC 3339 time."""
        res = self._req("GET", f"{self.api}/search",
                        {"part": "id", "type": "video", "videoDuration": "short", "order": "viewCount",
                         "q": query, "publishedAfter": after, "maxResults": limit})
        return [i["id"]["videoId"] for i in res.get("items", []) if i.get("id", {}).get("videoId")]

    def videos(self, ids: list[str]) -> list[dict]:
        out = []
        for k in range(0, len(ids), 50):
            res = self._req("GET", f"{self.api}/videos", {"part": "snippet,statistics,contentDetails",
                                                          "id": ",".join(ids[k:k + 50])})
            for v in res.get("items", []):
                m = re.fullmatch(r"PT(?:(\d+)M)?(?:(\d+)S)?", v["contentDetails"].get("duration", ""))
                out.append({"id": v["id"], "title": v["snippet"].get("title", ""),
                            "tags": v["snippet"].get("tags", []), "channel": v["snippet"].get("channelTitle", ""),
                            "views": int(v["statistics"].get("viewCount", 0)),
                            "likes": int(v["statistics"].get("likeCount", 0)),
                            "comments": int(v["statistics"].get("commentCount", 0)),
                            "description": v["snippet"].get("description", ""),
                            "published": v["snippet"].get("publishedAt", ""),
                            "category": v["snippet"].get("categoryId", "1"),
                            "seconds": (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) if m else 999})
        return out

    def update_video(self, video_id: str, title: str, description: str, tags: list[str], category: str) -> None:
        """Change a published video's title, description and tags (nothing else is touched)."""
        self._req("PUT", f"{self.api}/videos", {"part": "snippet"},
                  body={"id": video_id, "snippet": {"title": title[:100], "description": description[:4900],
                                                    "tags": tags[:30], "categoryId": category}})

    def channel_comments(self, channel_id: str, limit: int = 100) -> list[dict]:
        """The newest top-level comments on this channel's videos: [{author, text, likes, video}]."""
        res = self._req("GET", f"{self.api}/commentThreads",
                        {"part": "snippet", "allThreadsRelatedToChannelId": channel_id, "order": "time",
                         "maxResults": min(100, limit), "textFormat": "plainText"})
        out = []
        for i in res.get("items", []):
            c = i["snippet"]["topLevelComment"]["snippet"]
            out.append({"author": c.get("authorDisplayName", ""), "text": c.get("textDisplay", ""),
                        "likes": int(c.get("likeCount", 0)), "video": i["snippet"].get("videoId", ""),
                        "own": c.get("authorChannelId", {}).get("value") == channel_id})
        return out

    def insights(self, video_id: str, since: date | None = None) -> dict:
        start = since or date.today() - timedelta(days=90)
        res = self._req("GET", self.analytics_url, {
            "ids": "channel==MINE", "startDate": start.isoformat(),
            "endDate": date.today().isoformat(), "metrics": ",".join(METRICS),
            "filters": f"video=={video_id}"})
        cols = [c["name"] for c in res.get("columnHeaders", [])]
        rows = res.get("rows") or []
        return dict(zip(cols, rows[0])) if rows else {m: 0 for m in METRICS}

    # ------------------------------------------------------------------ publishing
    def publish(self, video: Path, variant: dict, settings: dict) -> dict:
        title = shorts_title(variant.get("hook", ""), variant.get("subject"))
        meta = {
            "snippet": {"title": title, "description": variant["caption"][:4900],
                        "categoryId": str(settings.get("category_id", 1)),
                        "tags": [t.lstrip("#") for t in re.findall(r"#\w+", variant["caption"])][:15]},
            "status": {"privacyStatus": settings.get("privacy", "public"),
                       "selfDeclaredMadeForKids": False},
        }
        data = Path(video).read_bytes()
        start = self._req("POST", self.upload_url, {"uploadType": "resumable", "part": "snippet,status"},
                          body=meta, headers={"X-Upload-Content-Type": "video/mp4",
                                              "X-Upload-Content-Length": str(len(data))}, raw=True)
        location = start.headers.get("Location")
        if not location:
            raise YouTubeError("YouTube didn't return an upload URL")
        res = self._req("PUT", location, body=data, headers={"Content-Type": "video/mp4"})
        vid = res["id"]
        stored = res.get("status", {}).get("privacyStatus")
        note = None
        if settings.get("privacy", "public") == "public" and stored != "public":
            note = ("YouTube saved it as PRIVATE (uploads stay private until Google approves the "
                    "API audit). Open YouTube Studio and set it to Public.")
        return {"id": vid, "permalink": f"https://youtube.com/shorts/{vid}", "note": note}


    def upload_file(self, path: Path, title: str, description: str, tags: list[str],
                    privacy: str = "public", category_id: int = 1, chunk: int = 32 * 1024 * 1024) -> dict:
        """Chunked resumable upload for big files (a 25 min 4K video is several GB)."""
        path = Path(path)
        size = path.stat().st_size
        meta = {"snippet": {"title": title[:100], "description": description[:4900],
                            "tags": tags[:30], "categoryId": str(category_id)},
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
        start = self._req("POST", self.upload_url, {"uploadType": "resumable", "part": "snippet,status"},
                          body=meta, headers={"X-Upload-Content-Type": "video/mp4",
                                              "X-Upload-Content-Length": str(size)}, raw=True)
        location = start.headers.get("Location")
        if not location:
            raise YouTubeError("YouTube didn't return an upload URL")
        offset = 0
        with open(path, "rb") as f:
            while True:
                f.seek(offset)
                data = f.read(chunk)
                end = offset + len(data) - 1
                req = urllib.request.Request(location, data=data, method="PUT", headers={
                    "Authorization": f"Bearer {self._token()}", "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{end}/{size}"})
                try:
                    with urllib.request.urlopen(req, timeout=900) as r:
                        res = json.load(r)
                        break
                except urllib.error.HTTPError as e:
                    if e.code != 308:
                        raise YouTubeError(f"upload failed: {_describe(e)}") from None
                    rng = e.headers.get("Range")             # what YouTube actually stored
                    offset = int(rng.split("-")[1]) + 1 if rng else end + 1
        vid = res["id"]
        stored = res.get("status", {}).get("privacyStatus")
        note = None
        if privacy == "public" and stored != "public":
            note = ("YouTube saved it as PRIVATE (uploads stay private until Google approves the API "
                    "audit). Open YouTube Studio and set it to Public.")
        return {"id": vid, "permalink": f"https://youtu.be/{vid}", "note": note}

    def set_thumbnail(self, video_id: str, jpeg: Path) -> None:
        """Needs a phone-verified channel (youtube.com/verify)."""
        url = self.upload_url.replace("/videos", "/thumbnails/set")
        self._req("POST", url, {"videoId": video_id, "uploadType": "media"},
                  body=Path(jpeg).read_bytes(), headers={"Content-Type": "image/jpeg"})


def _describe(e: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(e.read() or b"{}")
        err = data.get("error")
        if isinstance(err, dict):
            return f"{err.get('message', e)} (HTTP {e.code})"
        return f"{err}: {data.get('error_description', '')} (HTTP {e.code})"
    except (ValueError, AttributeError):
        return str(e)
