# Ooz Reels: how the whole system works

This guide explains the system in plain language. It should let you understand, debug or rebuild
it without having written the code. It follows the code as it is in this repo, so when the code
changes, update this file.

---

## 1. The big picture

```
 YOU (phone)                         THE CLOUD (free)                            OUTPUTS
 ───────────                         ────────────────                            ───────
 record drawing ──► Google Drive ─┐
                    "Ooz Reels/    │      ┌─────────────────────────────┐
                     Inbox"        ├────► │  GitHub Actions machine      │──► Telegram: albums, drops,
 send clip / tap ──► Telegram ─────┘      │  (a fresh Linux computer     │     reports, alerts
 buttons / cmds      bot                  │   that runs for a few min)   │
                                          │                              │──► Google Drive: 4K Shorts,
 cron-job.org ──── "run now" every hour ─►│  python -m reels_ai bot      │     LongDraws (backups)
 (+ GitHub's own schedule)                │   • read Telegram messages   │
                                          │   • read Drive inbox         │──► YouTube / Instagram /
 /longform ──────── starts a 2nd job ────►│   • analyse + edit video     │     TikTok (when enabled)
                                          │     (OpenCV + FFmpeg)        │
                                          │   • send / upload results    │
                                          │   • save memory → state.json │
                                          └─────────────────────────────┘
```

**Key idea:** nothing runs on your computer and nothing runs all the time. Once an hour
(or when you press *Run workflow*), GitHub starts a fresh Linux machine. It downloads this
repo, runs the bot once, saves what it learned, and shuts down. The bot's "memory" between
runs is one file, `reels-ai/state/state.json`, which the job commits back to the repo.

Because every run starts from nothing, three things must live outside the machine:

| What | Where it lives | Why |
|---|---|---|
| Code + settings | this GitHub repo (`reels-ai/`, `config.toml`) | the machine downloads it each run |
| Memory (queue, bank, drops sent) | `reels-ai/state/state.json` in the repo | committed after each run |
| Passwords / keys | GitHub **Secrets** (Settings › Secrets › Actions) | encrypted, never in the code |
| Videos | Telegram (by `file_id`) and Google Drive | too big for git |

---

## 2. Where things run, and what it costs

| Piece | Service | Cost |
|---|---|---|
| Running the code | **GitHub Actions** (`.github/workflows/*.yml`) | free for this use |
| Hourly "wake up" | **cron-job.org** calling GitHub's API, plus GitHub's own schedule | free |
| Chat interface | **Telegram Bot API** | free |
| Storage of raw/finished video | **Google Drive** (your 5 TB plan) | your plan |
| Publishing + stats | YouTube Data/Analytics API, Instagram Graph API, TikTok Content Posting API | free (quota-limited) |
| AI captions (optional) | **Gemini API** free tier | free |
| Video tools | **FFmpeg** (cutting/encoding), **OpenCV** (seeing), **Pillow** (text/graphics) | free, open source |

---

## 3. Repo map: what each file does

```
.github/workflows/
  reels-bot.yml     the hourly job: poll → (render if needed) → save state
  longform.yml      the long 4K LongDraw job (started by /longform)
reels-ai/
  config.toml       ALL settings you'd ever change (times, text, socials, resolution…)
  state/state.json  the bot's memory (written by the job, don't edit while it runs)
  reels_ai/
    __main__.py, cli.py   entry points: `python -m reels_ai make|bot|longform`
    bot.py         the brain: Telegram commands, queue, bank, drops, Drive inbox, renders
    telegram.py    tiny Telegram client (send messages/videos/albums, read updates)
    state.py       load/save state.json and config.toml
    ── video engine ──
    media.py       FFmpeg wrappers: normalize input, read frames, encode output, previews
    analyze.py     computer vision: finds ink start/end, peak moment, clean frames, framing
    variants.py    the 5 Short styles (recipes) + the frame-by-frame renderer
    fx.py          effects: zoom/camera, colour grades, text cards, wipes, progress bar
    hooks.py       hook text + caption templates
    pipeline.py    glue: video in → analysis → 5 variant files out
    ai.py          optional Gemini hooks/captions
    ── long-form ──
    longform.py    LongDraw builder: dead-time scan, speed, render, beat tape, chapters
    longform_job.py the LongDraw job: Drive in → build → Drive/YouTube out → Telegram
    panels.py      side panels (OOZ / STUDIO INK, rotating cards, glitch effect)
    advert.py      your commissions animation, rebuilt in code
    thumbnail.py   LongDraw thumbnail in your EP. style
    compile.py     /compile page compilations
    ── platforms ──
    drive.py       Google Drive (folders, upload, stream raw files)
    youtube.py     YouTube upload + analytics
    instagram.py   Instagram Reels publish + insights
    tiktok.py      TikTok inbox drafts / direct post
    report.py      weekly stats report
  tests/test_bot.py  automated tests (fake Telegram/Drive/YouTube servers)
  integrations/commission-alerts.gs   Google Form → Telegram script
  fonts/, assets/  Anton font (free licence), your logo box
privacy.html, terms.html, tiktok-callback.html   pages on your GitHub Pages site
```

---

## 4. Walkthrough: one drawing becomes 5 Shorts

You upload `Naruto.MOV` (25 min, 4K, vertical) to Drive › Ooz Reels › Inbox, or to a
project folder inside Longform: sessions there become Shorts too and stay in place for the
LongDraw (`shorts_from_longform`), so each video is uploaded once.

1. **Wake-up.** cron-job.org calls GitHub at the top of the hour, and `reels-bot.yml` starts a machine.
2. **Poll** (`bot.py → Runner.poll`). Reads new Telegram messages and button taps, then checks
   the Drive Inbox (`check_drive_inbox`). It finds `Naruto.MOV`, takes the character name from
   the file name, adds a *drawing* to `state["drawings"]` with status `queued`, and messages you
   "📥 Found 1 new recording".
3. **Render** (`Runner.render`, only when something is queued, because it installs FFmpeg/OpenCV).
   - **Make a timelapse without downloading 4K** (`drive.make_proxy`). FFmpeg streams the file from
     Drive and keeps only *keyframes* (iPhone stores one about every second, so 25 min becomes
     ~1,500 frames, a ~50 s timelapse). This is why long 4K files are cheap to process.
   - **Normalize** (`media.normalize`): 30 fps, vertical 9:16, at most 90 s, up to 2160×3840 in 4K mode.
   - **Analyse** (`analyze.py`, see section 6): finds when pencil starts, when ink starts and ends,
     the biggest ink moment, the cleanest sketch and finished frames, and where on the page the
     new drawing is (the *focus*).
   - **Edit 5 styles** (`variants.py`). Each recipe is a list of *shots*: "show source frame #812,
     zoomed 1.6× at this point, with this text at this opacity". `render()` turns that list into
     frames and pipes them into FFmpeg.
   - **Hooks/captions** come from templates (`hooks.py`), or from Gemini if `GEMINI_API_KEY` is set.
4. **Deliver.** Full-quality files are uploaded to Drive › Done › Shorts › Naruto. 1080p copies go
   to Telegram as one album. Each variant goes into the **bank** with status `approved`.
5. **Save.** `state.json` is committed back to the repo, so the next run remembers all of this.

## 5. Walkthrough: daily drops and a LongDraw

**Drops** (`bot.py → drop_due / next_drop / deliver`). Every run checks: is it past a time in
`[delivery] drop_times`, and have fewer than that many videos gone out today? If so, it picks a
banked variant that hasn't been sent: never the same drawing twice in a row, preferring a new
style, and drawing from whichever drawing has the most videos left. It sends the video, the 4K
link and the caption, then marks it `posted` with a `drop_no`. Running the job more often never
double-sends.

**LongDraw** (`/longform Shinobi Page`):
1. The bot doesn't do the heavy work itself. It calls GitHub's API to start `longform.yml`
   (`dispatch_workflow`), so the hourly bot stays fast.
2. `longform_job.run` lists the project folder in Drive, downloads the beats, finds the art panel
   image, and works out the episode number from `Done/Longform/EP NN …` folder names.
3. `longform.build`:
   - **Scan** each session cheaply (keyframes at 320 px). A moment is *dead* if no hand is
     visible and almost nothing changed since the last frame; 8 s or more of that is cut.
   - **Speed**: total kept time ÷ 25 min, never below 1×.
   - **Render** each session from Drive in one FFmpeg command: `select` keeps the live parts,
     `setpts` speeds them up, `fps=30` drops extra frames. Vertical sessions get `hstack`ed
     between the two panel videos (from `panels.py`).
   - **Intro**, then **concat** (join the parts without re-encoding).
   - **Beat tape**: each track goes through `loudnorm` (all set to −16 LUFS, YouTube's
     loudness), `acrossfade` blends 3 s between tracks, and the tape is trimmed to the exact length.
   - **Chapters** from where each session starts, and a **thumbnail** (`thumbnail.py`).
4. Upload to Drive and YouTube, message you, and move the project to `Longform/Finished`.

---

## 6. The core techniques, explained

**A video is a stack of pictures.** 30 fps means 30 images per second. Every edit here is either
choosing *which* frames to show (cutting, speed) or *changing* frames (zoom, text, colour).

**FFmpeg** is the Swiss-army knife of video. We use it to decode, re-encode (H.264/AAC MP4),
scale, crop, join and mix audio. Useful filters to know: `scale`, `crop`, `pad`, `fps`, `setpts`
(retime), `select` (keep some frames), `hstack` (side by side), `loudnorm`, `acrossfade`.
Python builds the FFmpeg command and runs it with `subprocess`.

**OpenCV + NumPy** let Python "see". A frame is a grid of numbers (height × width × 3 colours,
0–255), and analysis is maths on that grid:
- **Hand detection** (`analyze._skin`): skin tones fall in a known range in the YCrCb colour
  space, and coloured pen bodies are strongly saturated. Pixels matching either are "hand".
- **Ink detection** (`_darkness`): estimate the local paper brightness (a blurred max filter),
  then see how much darker each pixel is. A big difference is ink, a small one is pencil. This
  works under uneven lighting because it compares each pixel to its own neighbourhood.
- **"When was this pixel inked?"** (`_inked_at`): anything that is ink in the *final* frame stays
  ink. So for each ink pixel, find the last moment it was still blank paper (ignoring moments the
  hand covered it). That gives a per-pixel inking time the hand can't fool. From it come ink
  start (1.5% done), ink end (97%), and the peak (fastest gain).
- **Focus**: the area where *new* ink appeared, so older drawings on the same page are ignored.
  Shorts are cropped there, and landscape recordings get a vertical crop around it.

**Speed ramps** (`variants.ramp`): map an output duration onto a source time range. Around the
"peak" moment more output frames are given per source frame (slower), elsewhere fewer (faster),
never slower than real time.

**Text and graphics** are drawn with **Pillow** (fonts, outlines, boxes) into a transparent
layer, then blended onto the frame. The glitch effect (`panels.glitch_frames`) shifts random
horizontal slices and the red/blue channels by amounts that shrink to zero.

**Encoding** settings that matter: `crf` (quality: lower is better and bigger, 17–20 here),
`preset` (speed vs. compression), `yuv420p` (plays everywhere), `+faststart` (plays before it
fully downloads).

---

## 7. Logins, keys and tokens (the part that breaks most)

| Secret | What it is | If it stops working |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | the bot's password from @BotFather | `/revoke` in BotFather, save the new one |
| `IG_ACCESS_TOKEN` | Instagram system-user token (doesn't expire) | regenerate in the Meta app dashboard › API setup |
| `YT_CLIENT_ID` / `YT_CLIENT_SECRET` | identifies *your* Google app (project "ooz-reelz") | Google Cloud › Google Auth Platform › Clients |
| `YT_REFRESH_TOKEN` | long-lived permission for YouTube **and Drive** | OAuth Playground with the 4 scopes (README) |
| `TIKTOK_*` | TikTok app + refresh token | redo `/tiktok-callback.html` + `tiktok-connect` |
| `GEMINI_API_KEY` | optional AI copywriting | aistudio.google.com › API key |

**How Google/TikTok logins work (OAuth):** you approve the app once and get a **refresh token**
(long-lived). Every run swaps it for a short-lived **access token** (about 1 hour), which goes in
each request as `Authorization: Bearer …`. LongDraw renders take hours, so the code asks for a
fresh access token before each read (`Drive.fresh_token`).

`invalid_grant` means the refresh token died (revoked, or the Google app was set back to
"Testing"). `insufficient permission` means the token is missing a scope.

---

## 8. Running, testing, debugging

**Watch it run:** GitHub › Actions › *reels-bot* or *longform* › a run › expand a step to see its
log. The bot prints what it did. `Run workflow → task: check` tests every connection and posts
nothing.

**Run it on a computer** (Python 3.12 + FFmpeg installed):
```bash
cd reels-ai
pip install -r requirements.txt
python -m reels_ai make some_timelapse.mov --subject "Naruto" --sheets   # variants + review.html
python -m unittest discover -s tests                                     # all automated tests
```

**Common failures:**
| Symptom | Likely cause |
|---|---|
| Bot silent | runs not happening (cron-job.org disabled? GitHub Actions paused?) or bad Telegram token |
| "Couldn't process that video" | unreadable file, or a Drive permission/scope problem |
| Drive errors in Telegram | `YT_REFRESH_TOKEN` lacks the `drive` scope, or the Drive API is disabled |
| YouTube upload private | normal until Google approves the API audit; flip it to Public in Studio |
| Thumbnail not set | channel not phone-verified (youtube.com/verify) |
| Job ran out of time | a huge batch: renders resume next run (`max_render_minutes`), LongDraw limit is 340 min |

**Storage:** once a month the bot reports how much space raw clips use in
`Inbox/Processed` and `Longform/Finished` (when over `storage_alert_gb`), plus overall Drive usage.
It never deletes anything.

**Changing behaviour without code:** almost everything is in `config.toml` (times, resolution,
styles per drawing, panel text, advert text, socials, episode start). Edit it on GitHub; the next
run uses it.

---

## 9. If you had to rebuild it from scratch

The irreducible pieces, in order:
1. **A way to run code on a schedule** (GitHub Actions + cron, or any server with cron).
2. **FFmpeg** to read and write video, and **Python + OpenCV** to look at frames.
3. **The analysis idea** (section 6): per-pixel "when did this become ink", plus hand masking.
   Everything else (styles, framing, LongDraw cutting) builds on it.
4. **An edit plan → frames → encoder** loop (`variants.render`): decide what each output frame
   shows, draw it, pipe it into FFmpeg.
5. **Somewhere to keep memory** between runs (a JSON file, or a database in a product).
6. **Platform APIs** via OAuth for posting and stats.
7. **A chat/UI layer** (Telegram here; a web app in a product).

## 10. Turning it into a product (notes for later)

- Swap GitHub Actions for real servers or a job queue (GitHub's terms don't allow running a
  service for other people on Actions). Per-video compute costs cents for Shorts and dollars for
  4K LongDraws.
- Swap `state.json` for a database (users, projects, videos), and Telegram for a web dashboard.
- Each platform needs formal app review for other people's accounts (Google's restricted Drive
  scope needs a paid security assessment). An **upload → edit → download** product avoids most of
  that.
- The engine (`analyze.py`, `variants.py`, `longform.py`, `panels.py`) is the valuable part, and it
  doesn't depend on Telegram or GitHub.

## 11. Learning path (just enough)

1. **Python basics**: variables, functions, lists/dicts, files, `import`. Then read `hooks.py`
   (simplest) and `state.py`.
2. **JSON + HTTP APIs**: what a request/response is, `GET` vs `POST`, headers, tokens. Read
   `telegram.py`, the clearest API client here.
3. **FFmpeg on the command line**: trim, scale and concat a video yourself, then read `media.py`.
4. **NumPy/OpenCV**: images as arrays, thresholds, masks. Then read `analyze.py` with section 6
   open beside it.
5. **Git + GitHub Actions**: commits, branches, pull requests, and reading `reels-bot.yml`.

When reading code, start from the entry point (`cli.py`), follow one walkthrough (sections 4–5),
and run the tests after any change.
