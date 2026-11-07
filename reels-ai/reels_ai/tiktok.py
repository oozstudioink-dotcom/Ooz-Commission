"""TikTok Content Posting API client (stdlib only).

Auth: TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET plus a refresh token (TIKTOK_REFRESH_TOKEN)
obtained once through tiktok-callback.html + the workflow's `tiktok-connect` task.

Modes (config [platforms.tiktok] mode):
  "inbox"  - upload as a draft to the creator's TikTok inbox; they add a sound and post.
             Works before TikTok's app audit and keeps trending sounds possible.
  "direct" - publish straight away. Before the audit TikTok forces these to private (SELF_ONLY).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://open.tiktokapis.com"
AUTHORIZE = "https://www.tiktok.com/v2/auth/authorize/"
SCOPES = "user.info.basic,video.upload,video.publish"


class TikTokError(RuntimeError):
    pass


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def authorize_url(client_key: str, redirect_uri: str, state: str = "ooz",
                  scopes: str = SCOPES) -> str:
    return AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_key": _clean(client_key), "scope": scopes, "response_type": "code",
        "redirect_uri": redirect_uri, "state": state})


def _post_form(url: str, fields: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        data = _json_or_empty(e)
    if "access_token" not in data:
        raise TikTokError(f"sign-in failed: {data.get('error_description') or data.get('error') or data}")
    return data


def exchange_code(client_key: str, client_secret: str, code: str, redirect_uri: str,
                  api: str = API) -> dict:
    """One-time: turn the code from tiktok-callback.html into tokens."""
    return _post_form(f"{api}/v2/oauth/token/", {
        "client_key": _clean(client_key), "client_secret": _clean(client_secret),
        "code": code.strip(), "grant_type": "authorization_code", "redirect_uri": redirect_uri})


class TikTok:
    def __init__(self, client_key: str, client_secret: str, refresh_token: str, timeout: int = 60):
        self.client_key, self.client_secret = _clean(client_key), _clean(client_secret)
        self.refresh_token = _clean(refresh_token)
        if not (self.client_key and self.client_secret and self.refresh_token):
            raise TikTokError("TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REFRESH_TOKEN are required")
        self.timeout = timeout
        self.api = API
        self._access: str | None = None
        self.rotated_refresh_token: str | None = None   # set if TikTok issued a new one

    def _token(self) -> str:
        if not self._access:
            data = _post_form(f"{self.api}/v2/oauth/token/", {
                "client_key": self.client_key, "client_secret": self.client_secret,
                "grant_type": "refresh_token", "refresh_token": self.refresh_token}, self.timeout)
            self._access = data["access_token"]
            new = data.get("refresh_token")
            if new and new != self.refresh_token:
                self.rotated_refresh_token = new
        return self._access

    def _call(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        url = f"{self.api}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json; charset=UTF-8"},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            data = _json_or_empty(e)
        err = data.get("error") or {}
        if err.get("code") not in (None, "ok"):
            raise TikTokError(f"{err.get('code')}: {err.get('message')}")
        return data.get("data") or {}

    # ------------------------------------------------------------------ account
    def whoami(self) -> dict:
        return self._call("GET", "/v2/user/info/", params={"fields": "open_id,display_name"}).get("user", {})

    def creator_info(self) -> dict:
        return self._call("POST", "/v2/post/publish/creator_info/query/", {})

    # ------------------------------------------------------------------ publishing
    def publish(self, video: Path, variant: dict, settings: dict) -> dict:
        data = Path(video).read_bytes()
        source = {"source": "FILE_UPLOAD", "video_size": len(data), "chunk_size": len(data),
                  "total_chunk_count": 1}
        mode = settings.get("mode", "inbox")
        if mode == "inbox":
            init = self._call("POST", "/v2/post/publish/inbox/video/init/", {"source_info": source})
        else:
            info = self.creator_info()
            options = info.get("privacy_level_options") or ["SELF_ONLY"]
            wanted = settings.get("privacy", "PUBLIC_TO_EVERYONE")
            privacy = wanted if wanted in options else options[0]
            init = self._call("POST", "/v2/post/publish/video/init/", {
                "post_info": {"title": variant["caption"][:2200], "privacy_level": privacy,
                              "disable_comment": False, "disable_duet": False, "disable_stitch": False,
                              "video_cover_timestamp_ms": 1000},
                "source_info": source})
        publish_id, upload_url = init["publish_id"], init["upload_url"]

        req = urllib.request.Request(upload_url, data=data, method="PUT", headers={
            "Content-Type": "video/mp4", "Content-Length": str(len(data)),
            "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}"})
        try:
            urllib.request.urlopen(req, timeout=300).close()
        except urllib.error.HTTPError as e:
            raise TikTokError(f"upload failed: HTTP {e.code}") from None

        status = self.wait(publish_id)
        if mode == "inbox":
            note = ("It's in your TikTok inbox as a draft: open TikTok, tap the notification, add a "
                    "sound, paste the caption below and post.")
            return {"id": publish_id, "permalink": None, "note": note, "caption_for_user": True}
        posted = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
        note = None
        if settings.get("privacy", "PUBLIC_TO_EVERYONE") == "PUBLIC_TO_EVERYONE" and not posted:
            note = ("TikTok posted it as private (that's forced until TikTok approves the app). "
                    "Open it in TikTok and change who can watch to Everyone.")
        return {"id": publish_id, "permalink": None, "note": note}

    def wait(self, publish_id: str, wait_s: int = 300) -> dict:
        deadline = time.time() + wait_s
        while True:
            st = self._call("POST", "/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            s = st.get("status")
            if s in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
                return st
            if s == "FAILED":
                raise TikTokError(f"TikTok rejected the video: {st.get('fail_reason')}")
            if time.time() > deadline:
                return st          # still processing; TikTok finishes on its own
            time.sleep(5)

    def insights(self, publish_id: str) -> dict:
        return {}                  # needs video.list + a public post id; added after the audit


def _json_or_empty(e: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(e.read() or b"{}")
    except ValueError:
        return {"error": {"code": f"http_{e.code}", "message": str(e)}}
