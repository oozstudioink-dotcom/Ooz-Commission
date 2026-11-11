"""Which styles to make: your ratings, view counts, and the AI-written copy."""
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


class LearningMixin:
    # ------------------------------------------------------------------ AI copy
    def ai_copy(self, src: Path, d: dict, styles: list[str], tmp: Path) -> dict | None:
        """Gemini-written hooks + caption when GEMINI_API_KEY is set; None -> templates."""
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key or self.cfg.get("ai", {}).get("enabled", True) is False:
            return None
        from ..ai import DEFAULT_MODEL, final_frame, write_copy
        frame = final_frame(src, tmp / "final.jpg")
        from ..ai import candidate_models
        copy = None
        wanted = self.cfg.get("ai", {}).get("model", DEFAULT_MODEL)
        for model in candidate_models(key, wanted, self.state.get("ai_model"), limit=3):
            copy = write_copy(frame, d.get("subject"), styles, key, model=model)
            if copy:                              # busy/retired models fall through to the next
                self.state["ai_model"] = model
                break
        if copy and not d.get("subject"):
            copy = strip_guessed_names(copy)        # unlabelled: never name a character
        return copy

    def feedback(self, style: str, liked: bool) -> None:
        """Your ✅/❌ and 👍/👎 per style, used by choose_styles (style_weights)."""
        fb = self.state.setdefault("style_feedback", {}).setdefault(style, {"liked": 0, "disliked": 0})
        fb["liked" if liked else "disliked"] += 1

    # ================================================================== RENDER
    def choose_styles(self, did: str) -> list[str]:
        styles = [s for s in self.cfg["variants"]["styles"] if s in STYLE_LABELS]
        uses = self.state["style_uses"]
        rng = random.Random(did)
        rng.shuffle(styles)
        weight = self.style_weights()
        # least-used first, random tie-break; styles that get more views count as "used" less,
        # so they come round more often (and the weakest gets skipped when per_drawing < styles)
        styles.sort(key=lambda s: uses.get(s, 0) / weight.get(s, 1.0))
        return styles[: self.cfg["variants"].get("per_drawing", 2)]

    def style_weights(self) -> dict[str, float]:
        """From the weekly report: average views per style vs the average style (0.5x..2x).
        Only styles with 2+ matched posts count, and only once 2+ styles have data."""
        out: dict[str, float] = {}
        scores = {s: v["avg_views"] for s, v in self.state.get("style_scores", {}).items()
                  if v.get("posts", 0) >= 2 and v.get("avg_views", 0) > 0}
        if len(scores) >= 2:
            mean = sum(scores.values()) / len(scores)
            out = {s: min(2.0, max(0.5, v / mean)) for s, v in scores.items()}
        # your own taste: share of ✅/👍 per style (3+ ratings), 0.4x (mostly skipped) .. 1.6x
        for s, fb in self.state.get("style_feedback", {}).items():
            n = fb.get("liked", 0) + fb.get("disliked", 0)
            if n >= 3:
                out[s] = out.get(s, 1.0) * (0.4 + 1.2 * fb.get("liked", 0) / n)
        return out
