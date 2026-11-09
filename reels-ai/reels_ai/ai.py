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
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-pro-latest"

STYLE_BRIEFS = {
    "reveal_first": "video opens on the finished piece, then rewinds to the blank page",
    "speedrun": "whole sketch-to-ink process in under 10 seconds",
    "before_after": "wipe between the pencil sketch and the final ink",
    "detail_zoom": "zooms into the face/detail while it is being inked",
    "ink_only": "only the inking stage, no pencils",
    "one_take": "one continuous locked shot from first stroke to last, finished piece only at the end",
    "process_cuts": "short jump cuts of the pen inking at near real speed",
}

PROMPT = """You write short-form video copy for Ooz Studio Ink, a traditional manga inking artist.
The image is the finished drawing from a timelapse video{subject_line}.

Write:
1. "subject": {subject_rule}
2. "hooks": one on-screen hook per video style below. 2-6 words each, punchy, curiosity-driven,
   no hashtags, no emojis, never clickbait lies. Facts to respect: every video is a sped-up
   timelapse (never say "real time" or "no speed-up"); {pencil_fact}; if a hook names a
   character it must be the same one as "subject". Styles:
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


def write_copy(image: Path, subject: str | None, styles: list[str], api_key: str, pencil: bool | None = None,
               model: str = DEFAULT_MODEL, endpoint: str = ENDPOINT, timeout: int = 120) -> dict | None:
    key = re.sub(r"\s+", "", api_key or "")
    if not key or not image:
        return None
    styles = [s for s in styles if s in STYLE_BRIEFS]
    prompt = PROMPT.format(
        pencil_fact=("the artist sketched in pencil first, then inked" if pencil is True else
                     "the artist went straight to ink with no pencil sketch" if pencil is False else
                     "don't say whether a pencil sketch was used"),
        subject_line=f" of {subject}" if subject else "",
        subject_rule=(f'"{subject}" (the artist named it; use exactly this name).' if subject else
                      '"" always. The artist did not name the character: do NOT identify or name any '
                      "character or series anywhere (hooks, caption, hashtags); keep everything generic "
                      "about the drawing and the inking."),
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


FALLBACK_MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest"]   # if Pro is busy or not on the key
MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_SKIP = re.compile(r"image|tts|audio|live|embed|vision|learnlm|gemma|robotics|computer|banana|research|lyria|veo|imagen", re.I)


def available_models(api_key: str, url: str = MODELS_URL, timeout: int = 30) -> list[str]:
    """Text 'pro' and 'flash' models this key can call: '-latest' aliases first, Pro before Flash,
    then newest version.
    Model names change as Google retires versions, so the bot asks instead of guessing."""
    key = re.sub(r"\s+", "", api_key or "")
    req = urllib.request.Request(f"{url}?pageSize=200", headers={"x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            models = json.load(r).get("models") or []
    except (urllib.error.URLError, ValueError, TimeoutError):
        return []
    names = [m["name"].split("/", 1)[-1] for m in models
             if "generateContent" in (m.get("supportedGenerationMethods") or [])
             and re.search(r"flash|pro", m.get("name", "")) and not _SKIP.search(m.get("name", ""))]

    def rank(n: str):
        ver = [int(x) for x in re.findall(r"(\d+)(?:\.(\d+))?", n)[0] if x] if re.search(r"\d", n) else [99]
        return (not n.endswith("-latest"), "pro" not in n, "preview" in n or "exp" in n, [-v for v in ver],
                "lite" in n, n)
    return sorted(dict.fromkeys(names), key=rank)


def candidate_models(api_key: str, wanted: str, preferred: str | None = None, limit: int = 5) -> list[str]:
    """Order to try: the model that worked last time, the configured one, then what the key offers."""
    out = [m for m in (preferred, wanted) if m] + FALLBACK_MODELS + available_models(api_key)
    return list(dict.fromkeys(out))[:limit]


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
            except urllib.error.HTTPError as e:
                if e.code != 503 or attempt == tries - 1:
                    raise
                time.sleep(5)                         # "high demand": usually passes in seconds
            except TimeoutError:
                if attempt == tries - 1:
                    raise RuntimeError(f"no answer within {timeout}s"
                                       + (f" ({tries} tries)" if tries > 1 else "")) from None
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()[:40]
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        hint = {400: "the key looks wrong; copy it again from aistudio.google.com",
                403: "the key isn't allowed to use Gemini; create a new one in AI Studio",
                503: "busy (high demand) right now",
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
