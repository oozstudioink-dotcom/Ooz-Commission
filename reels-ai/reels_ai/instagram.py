"""Instagram Graph API client (stdlib only): account check, stats, and Reels publishing.

Works with either kind of token Meta hands out:
  * Facebook Login for Business / system-user tokens  -> graph.facebook.com
  * Instagram Login tokens (start with "IG")          -> graph.instagram.com
Videos are sent with Meta's resumable upload, so they never need a public URL.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REEL_METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
                "ig_reels_avg_watch_time", "ig_reels_video_view_total_time"]


class InstagramError(RuntimeError):
    pass


def clean_token(raw: str) -> str:
    """Secrets pasted from a browser often pick up spaces/newlines; tokens never contain them."""
    return re.sub(r"\s+", "", raw or "")


class Instagram:
    def __init__(self, token: str, user_id: str | None = None, timeout: int = 60):
        self.token = clean_token(token)
        if not self.token:
            raise InstagramError("IG_ACCESS_TOKEN is empty")
        self.host = "https://graph.instagram.com" if self.token.startswith("IG") else "https://graph.facebook.com"
        self.user_id = str(user_id) if user_id else None
        self.timeout = timeout

    # ------------------------------------------------------------------ transport
    def _req(self, method: str, path: str, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["access_token"] = self.token
        url = f"{self.host}/{path.lstrip('/')}"
        data = None
        if method == "GET":
            url += "?" + urllib.parse.urlencode(params)
        else:
            data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise InstagramError(_describe(e)) from None

    # ------------------------------------------------------------------ account
    def whoami(self) -> dict:
        """Resolve the account (and user id) the token belongs to."""
        if self.host.endswith("instagram.com"):
            me = self._req("GET", "me", {"fields": "user_id,username,media_count,followers_count"})
            self.user_id = self.user_id or str(me.get("user_id") or me.get("id"))
        else:
            if not self.user_id:
                raise InstagramError("ig_user_id is required for Facebook-login tokens (config.toml)")
            me = self._req("GET", self.user_id, {"fields": "username,media_count,followers_count"})
        me["resolved_user_id"] = self.user_id
        return me

    def recent_media(self, limit: int = 10) -> list[dict]:
        res = self._req("GET", f"{self.user_id}/media", {
            "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
            "limit": limit})
        return res.get("data", [])

    def insights(self, media_id: str) -> dict:
        """Reel metrics as {name: value}. Metrics Meta rejects are skipped, not fatal."""
        wanted = list(REEL_METRICS)
        for _ in range(len(wanted)):
            try:
                res = self._req("GET", f"{media_id}/insights", {"metric": ",".join(wanted)})
                return {m["name"]: (m.get("values") or [{}])[0].get("value", m.get("total_value", {}).get("value"))
                        for m in res.get("data", [])}
            except InstagramError as e:
                bad = [m for m in wanted if m in str(e)]
                if not bad:
                    raise
                wanted = [m for m in wanted if m not in bad]
                if not wanted:
                    return {}
        return {}

    # ------------------------------------------------------------------ publishing
    def publish_reel(self, video: Path, caption: str, share_to_feed: bool = False,
                     wait_s: int = 600) -> dict:
        """Upload a local video as a Reel and publish it. Returns {id, permalink}."""
        if not self.user_id:
            self.whoami()
        container = self._req("POST", f"{self.user_id}/media", {
            "media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
            "share_to_feed": "true" if share_to_feed else "false"})
        cid = container["id"]
        upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{cid}"
        data = Path(video).read_bytes()
        req = urllib.request.Request(upload_uri, data=data, method="POST", headers={
            "Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(len(data)),
            "Content-Type": "application/octet-stream"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                up = json.load(r)
        except urllib.error.HTTPError as e:
            raise InstagramError("upload: " + _describe(e)) from None
        if up.get("success") is False:
            raise InstagramError(f"upload rejected: {up}")

        deadline = time.time() + wait_s
        while True:
            st = self._req("GET", cid, {"fields": "status_code,status"})
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Instagram couldn't process the video: {st.get('status')}")
            if time.time() > deadline:
                raise InstagramError("Instagram is still processing the video after 10 minutes")
            time.sleep(10)

        pub = self._req("POST", f"{self.user_id}/media_publish", {"creation_id": cid})
        media_id = pub["id"]
        try:
            link = self._req("GET", media_id, {"fields": "permalink"}).get("permalink")
        except InstagramError:
            link = None
        return {"id": media_id, "permalink": link}


def _describe(e: urllib.error.HTTPError) -> str:
    try:
        err = json.loads(e.read() or b"{}").get("error", {})
        msg = err.get("error_user_msg") or err.get("message") or str(e)
        code = err.get("code")
        return f"{msg} (code {code})" if code else msg
    except (ValueError, AttributeError):
        return str(e)
