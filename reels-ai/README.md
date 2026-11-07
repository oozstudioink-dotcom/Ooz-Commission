# Ooz Reels AI

Turns a manga-inking timelapse into several ready-to-post Instagram trial-reel variants.
Free to run: Python + OpenCV + FFmpeg, no paid AI.

## Stage 1 (this): the editing engine

```bash
pip install -r requirements.txt          # also needs ffmpeg on PATH
python -m reels_ai make path/to/timelapse.mov --subject "Naruto"
```

Output goes to `output/<video name>/`:

| File | What it is |
|---|---|
| `variants/*.mp4` | 720p+/1080p vertical H.264 + AAC, ready to upload |
| `review.html` | open in a browser to watch every variant side by side with its hook and caption |
| `variants.json` | hook text, caption + hashtags, duration per variant |
| `analysis.json` | the beats it detected (in seconds) |

Options: `--variants reveal_first ink_only` (pick some), `--seed 2` (different hook picks),
`--audio track.mp3` (lay music under every variant; silent track otherwise), `--sheets` (contact sheets).

### What it detects (no AI, plain computer vision)

- **ink start / end**: each pixel that is ink in the finished frame is traced back to the
  moment it stopped being blank paper. The hand can't fool it, because it ignores moments
  a pixel was covered.
- **peak moment**: when the most ink goes down fastest (usually a solid black fill). Variants slow down here.
- **clean sketch / clean reveal**: frames with the least hand/pen on the page.
- **detail box**: the most line-dense area (usually the face) and when it gets inked.
- **arm sweeps**: frames where an arm covers the page are dropped from the edit.

### Variants (all 8-15 s: channel data shows ~7-10 s average watch time)

| Name | Idea | Look |
|---|---|---|
| `reveal_first` | Finished piece first, flash back to blank page, speed-ramped process, end card | clean white paper |
| `speedrun` | ~9 s loopable cut with progress bar | natural |
| `before_after` | Sketch to ink wipe, then the inking | warm cream paper |
| `detail_zoom` | Punches in on the face while it's inked, pulls back to reveal | clean |
| `ink_only` | Skips pencils, straight into the ink | high-contrast B&W |

Hooks, end cards and captions live in `reels_ai/hooks.py`; edit freely.

## Roadmap

1. ~~Editing engine + review page~~
2. Gemini (free tier) picks moments and writes hooks; compare against stage 1
3. Instagram API: pull Reel stats, rank winners, fetch source videos (or raw files from Google Drive)
4. GitHub Actions daily schedule + trial-reel auto-posting with daily caps and an approval switch
