"""Optional AI copywriting with Google's Gemini API (free tier is plenty for this volume).

Given the finished drawing and the character name, Gemini writes a hook per style and one
caption in the Ooz Studio Ink voice. Any failure returns None and the templates in hooks.py
are used instead, so a missing key or an outage never blocks a render.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-flash-latest"

STYLE_BRIEFS = {
    "reveal_first": "video opens on the finished piece, then rewinds to the blank page",
    "speedrun": "whole sketch-to-ink process in under 10 seconds",
    "before_after": "wipe between the pencil sketch and the final ink",
    "detail_zoom": "zooms into the face/detail while it is being inked",
    "ink_only": "only the inking stage, no pencils",
    "process_cuts": "short jump cuts of the pen inking at near real speed",
}

PROMPT = """You write short-form video copy for Ooz Studio Ink, a traditional manga inking artist.
The image is the finished drawing from a timelapse video{subject_line}.

Write:
1. "subject": the character / series name if you recognise it, else "" (never guess wildly).
2. "hooks": one on-screen hook per video style below. 2-6 words each, punchy, curiosity-driven,
   no hashtags, no emojis, never clickbait lies. Styles:
{styles}
3. "caption": one caption (max 150 characters) in a warm, confident artist voice. Use the hand
   emoji ✍\U0001f3fe once and end with a question that invites comments plus \U0001f447\U0001f3fe.
4. "hashtags": 8-10 hashtags (with #): the character/series first, then manga/anime art and
   inking/process tags. Lowercase, no spaces.

Reply with JSON only: {{"subject": "...", "hooks": {{"style": "..."}}, "caption": "...", "hashtags": ["#..."]}}"""


def final_frame(video: Path, dst: Path) -> Path | None:
    """Grab a frame from the last second (usually the finished drawing) as a small JPEG."""
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.7", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale=512:-2", str(dst)], check=True, timeout=60)
        return dst if dst.exists() else None
    except (subprocess.SubprocessError, OSError):
        return None


def write_copy(image: Path, subject: str | None, styles: list[str], api_key: str,
               model: str = DEFAULT_MODEL, endpoint: str = ENDPOINT, timeout: int = 120) -> dict | None:
    key = re.sub(r"\s+", "", api_key or "")
    if not key or not image:
        return None
    styles = [s for s in styles if s in STYLE_BRIEFS]
    prompt = PROMPT.format(
        subject_line=f" of {subject}" if subject else "",
        styles="\n".join(f"   - {s}: {STYLE_BRIEFS[s]}" for s in styles))
    body = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(Path(image).read_bytes()).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9},
    }
    req = urllib.request.Request(endpoint.format(model=model), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        out = json.loads(re.sub(r"^```(?:json)?|```$", "", text.strip()).strip())
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
        print(f"warn: Gemini copy failed, using templates: {e}")
        return None
    return clean(out, styles)


def ping(api_key: str, model: str = DEFAULT_MODEL, endpoint: str = ENDPOINT, timeout: int = 90,
         tries: int = 2) -> str:
    """Connection test: a one-word request. Returns the reply; raises RuntimeError with a
    plain-language reason (bad key, quota, model name) otherwise."""
    key = re.sub(r"\s+", "", api_key or "")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY secret found")
    body = {"contents": [{"parts": [{"text": "Reply with the single word: ready"}]}]}
    req = urllib.request.Request(endpoint.format(model=model), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        for attempt in range(tries):                  # the first call of the day can be slow
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
                break
            except TimeoutError:
                if attempt == tries - 1:
                    raise RuntimeError(f"no answer within {timeout}s (twice); Gemini may be busy, "
                                       "run the check again later") from None
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()[:40]
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        hint = {400: "the key looks wrong; copy it again from aistudio.google.com",
                403: "the key isn't allowed to use Gemini; create a new one in AI Studio",
                404: f"model {model!r} not found; check [ai] model in config.toml",
                429: "free-tier limit reached for now; it resets on its own"}.get(e.code, "")
        raise RuntimeError(f"HTTP {e.code}: {hint or msg[:150]}") from None
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
        raise RuntimeError(str(e)[:150]) from None


def clean(out: dict, styles: list[str]) -> dict | None:
    """Keep only well-formed fields; anything odd falls back to the templates."""
    if not isinstance(out, dict):
        return None
    hooks = {s: str(h).strip().strip('"')[:60] for s, h in (out.get("hooks") or {}).items()
             if s in styles and isinstance(h, str) and 0 < len(h.strip()) <= 60}
    caption = str(out.get("caption") or "").strip()[:220]
    tags = [t if t.startswith("#") else f"#{t}" for t in out.get("hashtags") or [] if isinstance(t, str)]
    tags = [re.sub(r"[^#\w]", "", t.lower()) for t in tags][:10]
    subject = str(out.get("subject") or "").strip()[:40]
    if not hooks and not caption:
        return None
    return {"hooks": hooks, "caption": caption, "hashtags": [t for t in tags if len(t) > 1],
            "subject": subject}
