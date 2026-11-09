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
- **straight to ink**: sessions with no pencil sketch are spotted (no light marks under the
  future ink); Before / after is swapped for another style and no hook or caption mentions a sketch.
- **the end of the inking** waits for fine linework too, not just bold fills, so videos don't
  jump to the finished piece while details are still being inked.

**Smooth motion:** frames are never shown twice in a row. Blends only mix the page: the hand and arm
come solid from the newest frame, so a sweeping arm leaves no see-through copies. When a variant speeds up, the frames it
skips are blended into the frame it shows (up to 6, newest weighted most), so ink builds up
smoothly and the hand glides. When a recording has fewer frames than the edit needs (long Drive
recordings are sampled at a few frames per real second), neighbouring frames cross-fade instead of
stuttering. Holds, flashbacks and the final reveal stay razor sharp. Knobs: `BLEND`, `MIN_SPAN`,
`MAX_SLOW` in `variants.py`.

### Variants (all 8-15 s: channel data shows ~7-10 s average watch time)

7 styles; each drawing gets `per_drawing` of them (least-used first).

| Name | Idea | Look |
|---|---|---|
| `reveal_first` | Finished piece first, flash back to blank page, speed-ramped process, end card | clean white paper |
| `speedrun` | ~9 s loopable cut with progress bar | natural |
| `before_after` | Sketch to ink wipe, then the inking | warm cream paper |
| `detail_zoom` | Punches in on the face while it's inked, pulls back to reveal | clean |
| `ink_only` | Skips pencils, straight into the ink | high-contrast B&W |
| `one_take` | One locked shot from the first stroke to the last, finished piece only in the final second, one short title or none, ~13 s; built from the patterns in `references/library.json` | natural |
| `process_cuts` | ~8 jump cuts of the pen actually moving (cut straight from the original recording, 2x for long sessions), finished piece first and last; the jump-cut format common in popular manga-inking Shorts | clean |

Edit breakdowns of popular drawing Shorts (in our own words) live in `references/library.json`.
Hooks, end cards and captions live in `reels_ai/hooks.py`; edit freely. On-screen text shrinks to fit
(80% of the width, 2 lines) and sits below the TikTok / Reels / Shorts top bar (`fx.SAFE_TOP`).

## Drops feed (default)

`config.toml` → `[delivery] mode = "drops"`, `[approval] mode = "auto"`, `per_drawing = 5`:
send the bot timelapses; each becomes all 5 styles, delivered as one album and added to the bank.
At each `drop_times` slot the bot sends one variant you haven't had yet, shuffled (never the same
drawing back-to-back when avoidable, prefers a new style, spreads drawings evenly), with its caption,
for you to post with a trending sound. Nothing is auto-posted. Switch `[delivery] mode = "platforms"`
to use the per-platform schedules and API posting below.

## 4K Shorts

`[shorts] resolution = "4k"`: recordings from the Drive inbox render at up to 2160x3840.
Landscape recordings render as large as their vertical crop allows, and nothing is ever enlarged.
Full-quality files go to `Done/Shorts/<character>/`. Telegram gets 1080p previews (bots can
send 50 MB at most), and every drop message links to its 4K file. Each run renders for at most
`max_render_minutes`; the rest continues on the next run. "All caught up" is sent when the queue
is empty.

## Upload once

Sessions uploaded to `Longform/<project>/` are also turned into Shorts automatically and
stay in place for the LongDraw (`[drive] shorts_from_longform`). A monthly Telegram note reports
how much space processed raws use (`storage_alert_gb`); nothing is ever deleted by the bot.

## Google Drive inbox (raw recordings)

Drop raw recordings of any length (4K, portrait or landscape) into Drive › `Ooz Reels/Inbox/`.
Name files after the character (`Naruto.MOV`, `Shinobi Undercover 2.MOV`) to fill in the subject.
Each run:
- picks up new files once and streams them from Drive: they're never downloaded whole.
  Recordings of 15 min or more use keyframes only (iPhone: about 1 per second, so 30 min becomes
  a ~60 s timelapse); shorter ones are sampled evenly.
- **Landscape** recordings get a full-height vertical crop centred on the new ink. When the new
  work is spread wider than that (a small drawing off to one side), the frame widens to take in
  all of it, shown on a blurred copy of itself, so nothing you drew is cut off.
- Makes the usual variants. Then it moves the raw file to `Inbox/Processed/` and backs the
  variants up to `Done/Shorts/<character>/`. It never deletes your files.

Needs the Drive API enabled in the Google project and a `YT_REFRESH_TOKEN` that includes the
`https://www.googleapis.com/auth/drive` scope. `Run workflow → check` creates the folders.

## LongDraw (long-form YouTube)

1. Put every raw session for one project in Drive › `Ooz Reels/Longform/<project>/`. Optionally
   add the finished page as `YT_Longform_Finished_Art_Panel.jpg` for the thumbnail.
2. Royalty-free beats go in `Beats/`, and the commissions advert animation in `Side Panels/`
   (exported at full size on black; it is placed pixel for pixel and never enlarged).
3. In Telegram, `/longform` lists projects and `/longform <project>` starts the separate
   `longform` workflow. You can also start it from Actions › longform.

The job finds dead time (hand off the page and nothing changing for 8 s or more), picks one
speed so the result lands in `target_minutes` (20 to 30), and renders 4K straight from Drive:
- portrait sessions get the side panels: OOZ / STUDIO INK on the left; advert, socials and
  schedule rotating on the right every 5 min, with a glitch-in
- landscape sessions fill the frame

It then adds:
- a glitch-in title intro
- a beat tape: shuffled, loudness-matched to -16 LUFS, crossfaded and looped
- chapters and the LongDraw thumbnail (`EP. NN`; numbering continues from Done/Longform)

It uploads to `Done/Longform/EP NN - <project>/` and to YouTube (private until Google's audit;
the custom thumbnail needs a phone-verified channel), messages you the links, and moves the
project folder to `Longform/Finished/`. Panel text lives in `config.toml` → `[panels]`.

## Extras

- **Page compilations:** `/compile [title]` stitches every drawing since the last compilation into a
  ~55 s Short (flash of every finished drawing, each drawing's sketch-to-ink, pull-back to the whole
  page; added to the drops bank) plus a long full-length version for YouTube (sent to you).
- **Weekly report:** every Sunday (or `/report`): views, watch time and follower change from
  Instagram/YouTube. Posts are matched to variants by caption, so styles get ranked.
- **AI hooks and captions:** add a free Gemini key as the `GEMINI_API_KEY` secret
  (aistudio.google.com). Gemini writes the hooks and captions, and names the character if you didn't.
  Without a key, or if Gemini fails, the built-in hooks are used.
- **Commission alerts:** `integrations/commission-alerts.gs` goes in the commission Google Form's
  Apps Script and messages you on Telegram the moment someone submits. `/id` gives the chat ID it needs.

## Telegram approval bot

Runs hourly on GitHub Actions (`.github/workflows/reels-bot.yml`), free.

1. You send a timelapse to the bot as a video, with the character name as the caption.
2. Within the hour it makes **2 variants** (least-used styles first), sends each with
   **Post / Skip / Edit caption** buttons.
3. No answer within 6 h -> auto-approved (`config.toml` -> `[approval]`).
4. At each platform's `post_times` it posts at most `max_per_day`, never the same drawing
   twice in a row. Until the YouTube / Instagram connections exist (`method = "manual"`) it
   sends you the finished video + caption to upload yourself.

Commands: `/status`, `/queue` (video bank and days of posts it covers), `/redo [name]` (re-make a
drawing's unsent Shorts with the latest editing; no name = newest), `/pause`, `/resume`, `/help`.
The bot warns in Telegram when the bank drops below `[bank] warn_below_days`. The first person to message the bot becomes
its owner; everyone else is ignored. Memory lives in `state/state.json` (committed by the job).

Setup: repo secret `TELEGRAM_BOT_TOKEN` (from @BotFather). Scheduled jobs only run from the
default branch, so this folder and the workflow must be on `main`.

Tests: `python -m unittest discover -s tests` (add `REELS_TEST_VIDEO=clip.mov` for a real render).

## Roadmap

1. ~~Editing engine + review page~~
2. ~~Telegram approval bot, hourly GitHub Actions schedule, manual posting~~
3. YouTube: ~~Analytics stats + API upload~~ (secrets `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`; uploads stay private until Google's API audit is approved)
4. Instagram: ~~stats + Reels-tab posting~~ (set `method = "api"` after `Run workflow` → task `check` passes); trial reels once the account has 200 followers
5. Learning: ~~styles that average more views (weekly report, 2+ matched posts) are made more often~~; hooks next
