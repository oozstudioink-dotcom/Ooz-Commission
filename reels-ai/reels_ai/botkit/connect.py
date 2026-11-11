"""The connection check (posts nothing) and the one-time TikTok sign-in."""
from __future__ import annotations

import json  # noqa: F401
import os  # noqa: F401
import random  # noqa: F401
import re  # noqa: F401
import tempfile  # noqa: F401
from datetime import datetime, timedelta  # noqa: F401
from pathlib import Path  # noqa: F401
from zoneinfo import ZoneInfo  # noqa: F401

from ..state import iso, load_config, load_state, new_id, now, parse, save_state  # noqa: F401
from ..telegram import Bot, TelegramError  # noqa: F401
from .common import (  # noqa: F401
    STYLE_LABELS, PLATFORM_LABELS, MAX_BOT_DOWNLOAD, HELP, pending_markup, rating_markup,
    label_markup, _time_range, _mmss, _norm_name, strip_guessed_names, ALBUM_BYTES, album_chunks,
    dispatch_workflow, shrink_for_telegram,
)


def tiktok_connect(runner, config: dict) -> None:
    """Exchange the one-time code from tiktok-callback.html; send the refresh token privately."""
    from ..tiktok import exchange_code
    code = os.environ.get("TIKTOK_AUTH_CODE", "").strip()
    key, secret = os.environ.get("TIKTOK_CLIENT_KEY", ""), os.environ.get("TIKTOK_CLIENT_SECRET", "")
    redirect = config.get("platforms", {}).get("tiktok", {}).get("redirect_uri", "")
    if not (code and key.strip() and secret.strip()):
        runner.say("\u26a0\ufe0f TikTok connect needs the code from the TikTok page plus the "
                   "TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET secrets.")
        raise SystemExit("missing TikTok code or client secrets")
    try:
        tokens = exchange_code(key, secret, code, redirect)
    except Exception as e:  # noqa: BLE001
        runner.say(f"\u274c TikTok connect failed: {e}\nCodes expire after a few minutes: open the "
                   "TikTok connect page again and use the new code right away.")
        raise SystemExit(1) from None
    runner.say("\u2705 TikTok connected. Last step: save this as the GitHub secret "
               "TIKTOK_REFRESH_TOKEN (then delete this message):")
    runner.say(tokens["refresh_token"])
    print("TikTok connected; refresh token sent to the owner on Telegram (not printed here).")


def check(runner) -> None:
    """Read-only connection test: account, recent posts and their stats. Posts nothing."""
    lines = ["\U0001f50c Connection check (nothing was posted)"]
    ig = runner.clients.get("instagram")
    if not ig:
        lines.append("Instagram: no IG_ACCESS_TOKEN secret found")
    else:
        try:
            me = ig.whoami()
            lines.append(f"Instagram: \u2705 @{me.get('username')} \u00b7 {me.get('followers_count', '?')} followers"
                         f" \u00b7 {me.get('media_count', '?')} posts (user id {me['resolved_user_id']})")
            for m in ig.recent_media(5):
                if m.get("media_product_type") != "REELS":
                    continue
                try:
                    st = ig.insights(m["id"])
                    stats = ", ".join(f"{k} {v}" for k, v in st.items() if v is not None) or "no stats"
                except Exception as e:  # noqa: BLE001
                    stats = f"stats unavailable: {e}"
                lines.append(f"\u2022 {m.get('timestamp', '')[:10]} {(m.get('caption') or '')[:40]!r}: {stats}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"Instagram: \u274c {e}")
    yt = runner.clients.get("youtube")
    if not yt:
        lines.append("YouTube: not connected (needs YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN secrets)")
    else:
        try:
            ch = yt.whoami()
            lines.append(f"YouTube: \u2705 {ch['title']} \u00b7 {ch.get('subscribers', '?')} subscribers"
                         f" \u00b7 {ch.get('videos', '?')} videos")
            for vid in yt.recent_videos(ch["uploads"], 3):
                try:
                    st = yt.insights(vid["id"])
                    stats = ", ".join(f"{k} {v}" for k, v in st.items())
                except Exception as e:  # noqa: BLE001
                    stats = f"stats unavailable: {e}"
                lines.append(f"\u2022 {vid['published'][:10]} {vid['title'][:40]!r}: {stats}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"YouTube: \u274c {e}")
    tt = runner.clients.get("tiktok")
    if not tt:
        lines.append("TikTok: not connected (needs TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, TIKTOK_REFRESH_TOKEN)")
    else:
        try:
            me = tt.whoami()
            lines.append(f"TikTok: \u2705 {me.get('display_name', '?')}")
            try:
                ci = tt.creator_info()
                lines.append(f"\u2022 can post as: {', '.join(ci.get('privacy_level_options') or ['?'])}"
                             f" \u00b7 max length {ci.get('max_video_post_duration_sec', '?')}s")
            except Exception as e:  # noqa: BLE001
                lines.append(f"\u2022 direct posting not available yet ({e}); inbox drafts still work")
            runner.note_rotated_token("tiktok", tt)
        except Exception as e:  # noqa: BLE001
            lines.append(f"TikTok: \u274c {e}")
    if not runner.drive:
        lines.append("Google Drive: not connected (uses the YT_* secrets with the drive permission)")
    else:
        try:
            folders = runner.drive.ensure_layout()
            n = len(runner.drive.list(folders["Inbox"], "video"))
            lines.append(f"Google Drive: \u2705 folders ready in \u201c{runner.drive.root_name}\u201d"
                         f" \u00b7 {n} video(s) waiting in Inbox")
        except Exception as e:  # noqa: BLE001
            lines.append(f"Google Drive: \u274c {e}")
    # Claude (the editor's eyes): is the key there and can it reach the model? No tokens are used.
    model = runner.cfg.get("watch", {}).get("model", "claude-opus-5-5")
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        lines.append("Claude (eyes): not connected (add the ANTHROPIC_API_KEY secret); the editor measures only")
    else:
        try:
            import anthropic
        except ImportError:
            anthropic = None
            lines.append("Claude (eyes): key is set (the connection can't be tested in this step)")
        if anthropic is not None:
            try:
                anthropic.Anthropic().models.retrieve(model)
                lines.append(f"Claude (eyes): \u2705 {model}")
            except anthropic.AuthenticationError:
                lines.append("Claude (eyes): \u274c the key was not accepted (check the ANTHROPIC_API_KEY secret)")
            except anthropic.NotFoundError:
                lines.append(f"Claude (eyes): \u274c this key can't use {model}")
            except anthropic.APIStatusError as e:
                lines.append(f"Claude (eyes): \u274c {e.status_code}: {str(e.message)[:120]}")
            except anthropic.APIConnectionError:
                lines.append("Claude (eyes): \u274c couldn't reach the API (network)")
    # Gemini: try the configured model, then a few others; remember the first that answers.
    import time as _time
    from ..ai import DEFAULT_MODEL, candidate_models, ping
    wanted = runner.cfg.get("ai", {}).get("model", DEFAULT_MODEL)
    key = os.environ.get("GEMINI_API_KEY", "")
    results, working = [], None
    from ..ai import available_models
    offered = available_models(key)
    for model in candidate_models(key, wanted, limit=5):
        t0 = _time.time()
        try:
            reply = ping(key, model, timeout=30, tries=2)
            results.append(f"\u2022 {model}: \u2705 \u201c{reply}\u201d in {_time.time() - t0:.0f}s")
            working = model
            break
        except RuntimeError as e:
            results.append(f"\u2022 {model}: \u274c {e}")
            if "no GEMINI_API_KEY" in str(e) or "key looks wrong" in str(e):
                break
    if working:
        if working != wanted:
            runner.state["ai_model"] = working
        else:
            runner.state.pop("ai_model", None)
        lines.append(f"Gemini: \u2705 using {working}")
    else:
        lines.append("Gemini: \u274c no model answered")
    lines += results
    if offered:
        lines.append("\u2022 models this key can use: " + ", ".join(offered[:8]))
    text = "\n".join(lines)
    print(text)
    runner.say(text)
