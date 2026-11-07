"""YouTube client (stdlib only): channel check, Shorts upload, per-video analytics.

Auth: an OAuth client (YT_CLIENT_ID / YT_CLIENT_SECRET) plus a long-lived refresh token
(YT_REFRESH_TOKEN) created once in Google's OAuth Playground. A fresh access token is
fetched on every run.

Google forces API uploads from un-audited projects to *private*. We ask for public; if
YouTube stores it private anyway, the result says so and the owner flips it in Studio.
"""
from __future__ import annotations

import json
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
        if self._access:
            return self._access
        body = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.token_url, data=body),
                                        timeout=self.timeout) as r:
                self._access = json.load(r)["access_token"]
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


def _describe(e: urllib.error.HTTPError) -> str:
    try:
        data = json.loads(e.read() or b"{}")
        err = data.get("error")
        if isinstance(err, dict):
            return f"{err.get('message', e)} (HTTP {e.code})"
        return f"{err}: {data.get('error_description', '')} (HTTP {e.code})"
    except (ValueError, AttributeError):
        return str(e)
