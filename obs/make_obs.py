"""OBS stream package for Ooz Studio Ink: themed overlays, an importable scene collection, previews.

Run from the repo root:  python obs/make_obs.py
Writes obs/package/ (the folder you copy to your computer) and obs/previews/.

Scenes: Starting Soon, Drawing, Drawing Focus, Just Chatting, Be Right Back, Final Piece, Ending.
Your cameras live in two helper scenes (Page Cam, Face Cam): add each camera once there and every
scene that shows it updates. One YouTube chat source is shared by the scenes that show chat.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "obs" / "package"
FOLDER = "Ooz Studio Ink OBS"                       # the folder name on your computer
INSTALL = {"Windows": f"C:/{FOLDER}", "Mac": f"/Users/Shared/{FOLDER}"}
W, H = 1920, 1080
CHAT = (544, 878)                                   # the chat source's own size (Just Chatting)

# Where the cameras and the chat sit in each scene: x, y, width, height on a 1920x1080 canvas.
LAYOUT = {
    "drawing": {"page": (32, 32, 1376, 774), "face": (1432, 32, 456, 256), "chat": (1432, 312, 456, 736)},
    "chatting": {"face": (32, 32, 1280, 720), "chat": (1344, 32, 544, 878)},
    "focus": {"page": (0, 0, 1920, 1080)},
    "nocam": {"page": (32, 32, 1376, 774), "card": (1432, 32, 456, 256), "chat": (1432, 312, 456, 736)},
    "reveal": {"page": (240, 120, 1440, 810)},
}

CONFIG_JS = """// Edit this before a stream (any text editor), then right-click an overlay in OBS > Refresh.
window.OOZ = {
  topic: "Inking by hand",                 // what you're drawing today, e.g. "Inking Sasuke Uchiha"
  countdownMinutes: 5,                     // Starting Soon counts down from this each time you switch to it
  faceCam: true,                           // false = no face camera: that window shows your studio mark instead
  nextStream: "Next long video: Friday \\u00b7 12 PM EST",
  commissionLink: "oozstudioink-dotcom.github.io/Ooz-Commission",
  socials: [
    ["Instagram", "@oozstudioink1"],
    ["TikTok", "@oozstudioink1"],
    ["YouTube", "Ooz Studio Ink"],
    ["X", "@OozStudioInk"],
    ["Facebook", "Ooz Studio Ink"],
  ],
  schedule: [
    "New Shorts \\u00b7 Mon \\u00b7 Wed \\u00b7 Fri",
    "New long video \\u00b7 every Friday \\u00b7 12 PM EST",
  ],
};
"""

MARK_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
<circle cx="50" cy="50" r="45" fill="none" stroke="#f5f4f0" stroke-width="5"/>
<rect x="15" y="48.6" width="70" height="2.8" fill="#f5f4f0"/>
<circle cx="50" cy="18" r="5" fill="#c0392b"/><circle cx="50" cy="34" r="5" fill="#c0392b"/>
<circle cx="50" cy="66" r="5" fill="#c0392b"/><circle cx="50" cy="82" r="5" fill="#c0392b"/>
</svg>
"""

STYLE = """@font-face{font-family:Anton;src:url(assets/fonts/Anton-Regular.ttf)}
@font-face{font-family:Inter;font-weight:500;src:url(assets/fonts/Inter-Medium.otf)}
@font-face{font-family:Inter;font-weight:600;src:url(assets/fonts/Inter-SemiBold.otf)}
@font-face{font-family:Inter;font-weight:700;src:url(assets/fonts/Inter-Bold.otf)}
:root{--ink:#151515;--ink2:#1c1c1c;--paper:#f5f4f0;--red:#c0392b;--grey:#a9a9a4}
*{box-sizing:border-box}
html,body{margin:0;width:1920px;height:1080px;overflow:hidden;font-family:Inter,Arial,sans-serif;color:var(--paper)}
body.v{width:1080px;height:1920px}
.panel{background:rgba(21,21,21,.88);padding:18px 22px}
body.bg{background:var(--ink)}
body.clear{background:transparent}
.abs{position:absolute}
.win{position:absolute;background:#0d0d0d;outline:3px solid var(--paper);display:flex;align-items:center;
  justify-content:center;overflow:hidden}
.win .hint{font:600 20px Inter;letter-spacing:.32em;color:#4a4a48;text-transform:uppercase;text-align:center;line-height:1.7}
.win.sample .hint{display:none}
.win img.sample{width:100%;height:100%;object-fit:cover;filter:grayscale(1) contrast(1.05)}
.tag{position:absolute;background:var(--red);color:var(--paper);font:700 15px Inter;letter-spacing:.2em;
  padding:7px 12px 6px;text-transform:uppercase;line-height:1}
.mark{width:72px;height:72px}
.brand{display:flex;align-items:center;gap:18px}
.brand .name{font-family:Anton;font-size:46px;letter-spacing:.02em;line-height:1}
.live{display:inline-flex;align-items:center;gap:10px;background:var(--red);font:700 17px Inter;
  letter-spacing:.22em;padding:7px 14px 6px;line-height:1;text-transform:uppercase}
.live i{width:10px;height:10px;border-radius:50%;background:var(--paper);animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.25}}
.kicker{font:600 16px Inter;letter-spacing:.32em;color:var(--grey);text-transform:uppercase}
.topic{font:600 28px Inter}
.big{font-family:Anton;line-height:.95;letter-spacing:.01em;text-transform:uppercase}
.rule{height:6px;background:var(--red)}
.pill{display:inline-block;background:var(--red);font:700 18px Inter;letter-spacing:.2em;padding:9px 16px 8px;
  text-transform:uppercase;line-height:1}
.link{font:600 24px Inter;color:var(--paper);white-space:nowrap}
.rot{position:relative;height:34px}
.rot span{position:absolute;left:0;top:0;white-space:nowrap;opacity:0;transform:translateY(8px);
  transition:opacity .7s,transform .7s;font:600 24px Inter}
.rot span.on{opacity:1;transform:none}
.rot.right span{left:auto;right:0}
.rot b{color:var(--grey);font-weight:600;letter-spacing:.18em;font-size:16px;text-transform:uppercase;margin-right:12px}
.socials{display:grid;grid-template-columns:auto 1fr;gap:10px 22px;font:600 26px Inter}
.socials b{font-weight:600;letter-spacing:.2em;font-size:17px;color:var(--grey);text-transform:uppercase;align-self:center}
.art-bg{position:absolute;inset:0;background-size:cover;background-position:center;filter:grayscale(1);opacity:.13}
.fade-left{position:absolute;inset:0;background:linear-gradient(90deg,var(--ink) 38%,rgba(21,21,21,.6) 70%,rgba(21,21,21,.2))}
.chat-sample{width:100%;height:100%;padding:18px;display:flex;flex-direction:column;justify-content:flex-end;gap:14px;
  font:500 21px Inter;background:rgba(13,13,13,.92)}
.chat-sample p{margin:0;line-height:1.35}
.chat-sample b{color:#e2786c;font-weight:700;margin-right:8px}
.slides{position:absolute}
.slides img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity 1.6s}
.slides img.on{opacity:1}
"""

APP_JS = """(function(){
var C=window.OOZ||{}, Q=location.search.indexOf('preview')>=0;
function $(s){return Array.prototype.slice.call(document.querySelectorAll(s));}
$('[data-ooz]').forEach(function(e){var v=C[e.getAttribute('data-ooz')]; if(v!=null) e.textContent=v;});
$('.socials').forEach(function(e){(C.socials||[]).forEach(function(s){
  var b=document.createElement('b'); b.textContent=s[0]; var t=document.createElement('span'); t.textContent=s[1];
  e.appendChild(b); e.appendChild(t);});});
$('.schedule').forEach(function(e){(C.schedule||[]).forEach(function(s){var d=document.createElement('div');
  d.textContent=s; e.appendChild(d);});});
// rotating line: socials, then the schedule, then the commission link
$('.rot').forEach(function(e){
  var items=(C.socials||[]).map(function(s){return [s[0],s[1]];});
  (C.schedule||[]).forEach(function(s){items.push(['Schedule',s]);});
  if(C.commissionLink) items.push(['Commissions',C.commissionLink]);
  var spans=items.map(function(it){var s=document.createElement('span'); var b=document.createElement('b');
    b.textContent=it[0]; s.appendChild(b); s.appendChild(document.createTextNode(it[1])); e.appendChild(s); return s;});
  var i=0; if(spans.length){spans[0].className='on';
    setInterval(function(){spans[i].className=''; i=(i+1)%spans.length; spans[i].className='on';},6000);}
});
// countdown (Starting Soon): restarts each time the scene is shown
$('[data-countdown]').forEach(function(e){
  var end=Date.now()+(Number(C.countdownMinutes)||5)*60000;
  function tick(){var s=Math.max(0,Math.round((end-Date.now())/1000));
    e.textContent=s>0?(Math.floor(s/60)+':'+('0'+s%60).slice(-2)):'Starting now';}
  tick(); setInterval(tick,1000);
});
// slideshow (Be Right Back)
if(!Q) $('[data-preview-only]').forEach(function(e){e.remove();});   // in OBS the video plays here
$('.slides').forEach(function(e){var imgs=e.querySelectorAll('img'), i=0; if(!imgs.length) return;
  imgs[0].className='on'; setInterval(function(){imgs[i].className=''; i=(i+1)%imgs.length; imgs[i].className='on';},7000);});
// no face camera: the studio mark fills its window
if(C.faceCam===false){ $('.win[data-sample="face"]').forEach(function(w){w.removeAttribute('data-sample');
  w.innerHTML='<div style="text-align:center"><img src="assets/mark.svg" style="width:110px;height:110px">'+
    '<div class="kicker" style="margin-top:16px">Inking live</div></div>';}); }
// preview mode (only for the preview pictures): sample art in the camera windows
if(Q && document.body.classList.contains('clear')){var bgi=document.createElement('img'); bgi.className='sample';
  bgi.src='assets/art/01-samurai.jpg'; bgi.style.cssText='position:absolute;inset:0;width:100%;height:100%;object-fit:cover;filter:grayscale(1);z-index:-1';
  document.body.insertBefore(bgi, document.body.firstChild);}
if(Q){ $('.win[data-sample]').forEach(function(w){var k=w.getAttribute('data-sample'); w.classList.add('sample');
    if(k==='chat'){w.innerHTML='<div class="chat-sample"><p><b>inkfan_22</b>the hatching on the hair is crazy</p>'+
      '<p><b>mangamike</b>what pen is that?</p><p><b>sketchdaily</b>first time catching this live \\ud83d\\udd25</p>'+
      '<p><b>oozstudioink1</b>Sakura nib, 0.3 \\u270d\\ud83c\\udffe</p><p><b>lena.draws</b>do Gojo next!</p></div>';}
    else {var i=document.createElement('img'); i.className='sample';
      i.src=k==='face'?'assets/art/04-fighter-and-beast.jpg':'assets/art/01-samurai.jpg'; w.appendChild(i);} }); }
})();
"""

CHAT_CSS = """/* YouTube chat in the Ooz Studio Ink look (paste into the chat source's Custom CSS if it's ever reset) */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@500;700&display=swap');
body, yt-live-chat-app, yt-live-chat-renderer { background: rgba(13,13,13,0.92) !important; overflow: hidden !important; }
yt-live-chat-header-renderer, #panel-pages, yt-live-chat-message-input-renderer, #input-panel,
yt-live-chat-viewer-engagement-message-renderer, #ticker, yt-live-chat-banner-manager,
#author-photo, #chat-badges, yt-live-chat-author-badge-renderer, #menu, #reaction-control-panel-overlay { display: none !important; }
yt-live-chat-text-message-renderer { padding: 6px 16px !important; font-family: Inter, Arial, sans-serif !important; }
yt-live-chat-text-message-renderer #author-name { color: #e2786c !important; font-weight: 700 !important; font-size: 19px !important; }
yt-live-chat-text-message-renderer #author-name[type="owner"] { color: #f5f4f0 !important; background: #c0392b !important; padding: 2px 6px !important; }
yt-live-chat-text-message-renderer #message { color: #f5f4f0 !important; font-weight: 500 !important; font-size: 21px !important; line-height: 1.35 !important; }
yt-live-chat-paid-message-renderer #card { background: #c0392b !important; }
#item-scroller { overflow: hidden !important; }
"""


README = """OOZ STUDIO INK - OBS STREAM SCENES
====================================

Scenes
  Starting Soon   countdown, today's topic, schedule, commission link
  Drawing         page camera big, face camera, YouTube chat, commissions + rotating socials
  Drawing · No Webcam   page camera big and chat, no face camera
  Drawing Focus   page camera full screen with a small LIVE badge and rotating socials
  Just Chatting   face camera big, chat big
  Be Right Back   a looping video of your sketchbook pieces (7 s each, crossfading)
  Final Piece     the page camera framed for the reveal, with the commission link
  Ending          thanks, next stream, every social, commission link, schedule

SET UP (once, about 10 minutes)
1. Unzip, then move the folder "Ooz Studio Ink OBS" to:
     Windows:  C:\\            (so it is C:\\Ooz Studio Ink OBS)
     Mac:      /Users/Shared    (Finder > Go > Go to Folder > /Users/Shared)
2. OBS > Scene Collection > Import > pick "Ooz Studio Ink - OBS scenes (Windows).json" or
   "(Mac).json" > Import. Then Scene Collection > Ooz Studio Ink.
   If OBS says files are missing: Search Directory > pick the "Ooz Studio Ink OBS" folder.
3. Settings > Video: Base and Output resolution 1920x1080.
4. Cameras (each camera is added ONCE and shows up in every scene that uses it):
   - Click the scene "Page Cam" (with the gear) > + > Video Capture Device > your overhead camera
     (a webcam, or your iPhone: Continuity Camera on a Mac, or an app like Camo / Iriun on Windows).
     Then right-click the camera > Transform > Fit to screen.
   - Same in "Face Cam" for a face camera. No face camera? Open overlays/config.js and set
     faceCam: false - that window then shows your studio mark.
5. Chat: when your stream is created on YouTube, copy its video ID (the part after watch?v= in
   the link). In OBS double-click "YouTube Chat" and replace PASTE_YOUR_VIDEO_ID with it.
   The chat is already styled to match (youtube-chat.css has the same style if it's ever reset).
6. Settings > Audio: pick your microphone. Settings > Stream: YouTube > Connect Account.
7. Optional: Settings > Hotkeys, give each scene a key (F1-F7) to switch fast while drawing.

BEFORE EACH STREAM
- Open overlays/config.js in Notepad / TextEdit, change topic (e.g. "Inking Sasuke Uchiha"),
  countdownMinutes and nextStream, save. In OBS right-click any overlay > Refresh.
- Update the YouTube Chat video ID (step 5).

VERTICAL (FACEBOOK / INSTAGRAM LIVE, 1080x1920)
- Import "Ooz Studio Ink - Vertical for Facebook + Instagram Live (...).json" the same way. It's
  its own scene collection: Starting Soon, Drawing, Just Chatting, Be Right Back, Final Piece, Ending.
- Make an OBS profile for it so the resolution switches by itself: Profile > New > "Vertical",
  then Settings > Video: Base and Output 1080x1920. Switch profile + scene collection together.
- Add your cameras once in its own Page Cam / Face Cam scenes (Fit to screen). Turn your phone or
  camera upright for the page if you can: it fills the whole vertical screen.
- No chat window: Facebook and Instagram show the comments over the stream themselves, so the
  bottom third of the screen is kept clear for them, and the top bar sits under their header.
- Facebook: Live Producer (facebook.com/live/producer) > Streaming software > copy the stream key
  into OBS Settings > Stream (Service: Facebook Live, or Custom).
- Instagram: Instagram Live Producer (needs a professional account): on instagram.com on a computer
  > Create > Live video > copy the URL and stream key into Settings > Stream > Custom.
- Both at once needs a multistream plugin (e.g. "Multiple RTMP outputs") or a service like Restream.

Everything is plain HTML in overlays/, so colors and text can be changed any time.
To rebuild this package: python obs/make_obs.py (in the Ooz-Commission repo).
"""


def page(cls: str, body: str, title: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>'
            f'<link rel="stylesheet" href="style.css"></head><body class="{cls}">\n{body}\n'
            '<script src="config.js"></script><script src="app.js"></script></body></html>\n')


def win(rect, label: str, sample: str, tag: str = "") -> str:
    x, y, w, h = rect
    t = f'<div class="tag" style="left:{x}px;top:{y - 32}px">{tag}</div>' if tag else ""
    return (f'{t}<div class="win" data-sample="{sample}" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">'
            f'<div class="hint">{label}</div></div>')


BRAND = '<div class="brand"><img class="mark" src="assets/mark.svg"><div class="name">OOZ STUDIO INK</div></div>'


def pages() -> dict[str, str]:
    d, c, r = LAYOUT["drawing"], LAYOUT["chatting"], LAYOUT["reveal"]
    out = {}
    out["starting.html"] = page("bg", f"""
<div class="art-bg" style="background-image:url(assets/art/01-samurai.jpg);left:700px"></div><div class="fade-left"></div>
<div class="abs" style="left:140px;top:120px">{BRAND}</div>
<div class="abs" style="left:140px;top:300px">
  <div class="kicker">The stream starts in</div>
  <div class="big" style="font-size:190px;margin-top:14px">Starting soon</div>
  <div class="rule" style="width:180px;margin:34px 0 30px"></div>
  <div class="big" data-countdown style="font-size:110px;color:var(--red)">5:00</div>
  <div class="topic" style="margin-top:30px"><span class="kicker" style="margin-right:16px">Today</span><span data-ooz="topic"></span></div>
</div>
<div class="abs" style="left:140px;bottom:90px;width:1640px;display:flex;justify-content:space-between;align-items:flex-end">
  <div><div class="kicker" style="margin-bottom:14px">Schedule</div><div class="schedule" style="font:600 24px Inter;line-height:1.6"></div></div>
  <div style="text-align:right"><div class="pill">Commissions open</div><div class="link" style="margin-top:12px" data-ooz="commissionLink"></div></div>
</div>""", "Starting Soon")

    out["drawing.html"] = page("bg", f"""
{win(d["page"], "Page camera<br>(add it in the scene &ldquo;&#9881; Page Cam&rdquo;)", "page")}
{win(d["face"], "Face camera", "face")}
{win(d["chat"], "YouTube chat", "chat")}
<div class="abs" style="left:32px;top:832px;width:1376px;height:216px;display:flex;justify-content:space-between;align-items:center">
  <div>{BRAND}
    <div style="display:flex;align-items:center;gap:18px;margin-top:20px"><span class="live"><i></i>Live</span>
      <span class="topic" data-ooz="topic"></span></div></div>
  <div style="text-align:right;width:700px">
    <div class="pill">Commissions open</div>
    <div class="link" style="margin:12px 0 18px;font-size:22px" data-ooz="commissionLink"></div>
    <div class="rot right" style="width:700px"></div></div>
</div>""", "Drawing")

    n = LAYOUT["nocam"]
    cx, cy, cw, ch = n["card"]
    out["drawing-nocam.html"] = page("bg", f"""
{win(n["page"], "Page camera<br>(add it in the scene &ldquo;&#9881; Page Cam&rdquo;)", "page")}
{win(n["chat"], "YouTube chat", "chat")}
<div class="abs" style="left:{cx}px;top:{cy}px;width:{cw}px;height:{ch}px;background:var(--ink2);outline:3px solid var(--paper);
  display:flex;flex-direction:column;justify-content:center;padding:0 30px">
  <div class="brand" style="gap:16px"><img src="assets/mark.svg" style="width:64px;height:64px"><div class="name" style="font-size:40px">OOZ STUDIO INK</div></div>
  <div style="display:flex;align-items:center;gap:14px;margin-top:22px"><span class="live"><i></i>Live</span>
    <span class="kicker">Today</span></div>
  <div class="topic" style="margin-top:14px;font-size:24px" data-ooz="topic"></div>
</div>
<div class="abs" style="left:32px;top:832px;width:1376px;height:216px;display:flex;justify-content:space-between;align-items:center">
  <div><div class="pill">Commissions open</div>
    <div class="link" style="margin-top:14px;font-size:26px" data-ooz="commissionLink"></div></div>
  <div style="width:640px"><div class="kicker" style="text-align:right;margin-bottom:12px">Follow along</div>
    <div class="rot right" style="width:640px"></div></div>
</div>""", "Drawing · No Webcam")

    out["focus.html"] = page("clear", """
<div class="abs" style="left:28px;top:28px;display:flex;align-items:center;gap:14px;background:rgba(21,21,21,.86);padding:12px 18px 12px 12px">
  <img src="assets/mark.svg" style="width:44px;height:44px"><span class="live"><i></i>Live</span>
  <span class="topic" style="font-size:22px" data-ooz="topic"></span></div>
<div class="abs" style="right:28px;bottom:28px;background:rgba(21,21,21,.86);padding:14px 20px;width:620px">
  <div class="rot"></div></div>""", "Drawing Focus")

    out["chatting.html"] = page("bg", f"""
{win(c["face"], "Face camera<br>(add it in the scene &ldquo;&#9881; Face Cam&rdquo;)", "face")}
{win(c["chat"], "YouTube chat", "chat")}
<div class="abs" style="left:32px;top:790px;width:1280px;height:258px;display:flex;justify-content:space-between;align-items:center">
  <div>{BRAND}
    <div style="display:flex;align-items:center;gap:18px;margin-top:20px"><span class="live"><i></i>Live</span>
      <span class="kicker">Just chatting</span></div>
    <div class="topic" style="margin-top:14px" data-ooz="topic"></div></div>
  <div style="text-align:right"><div class="pill">Commissions open</div>
    <div class="link" style="margin-top:12px" data-ooz="commissionLink"></div></div>
</div>
<div class="abs" style="left:1344px;top:940px;width:544px;height:108px;background:var(--ink2);padding:0 24px;
  display:flex;align-items:center"><div class="rot" style="width:100%"></div></div>""", "Just Chatting")

    arts = "".join(f'<img src="assets/art/{p.name}">' for p in sorted((ROOT / "portfolio").glob("*.jpg")))
    out["brb.html"] = page("bg", f"""
<div class="slides" data-preview-only style="left:920px;top:0;width:1000px;height:1080px">{arts}</div>
<div class="abs" data-preview-only style="left:920px;top:0;width:220px;height:1080px;background:linear-gradient(90deg,var(--ink),rgba(21,21,21,0))"></div>
<div class="abs" style="left:120px;top:120px">{BRAND}</div>
<div class="abs" style="left:120px;top:360px">
  <div class="kicker">Short break</div>
  <div class="big" style="font-size:170px;margin-top:14px">Be right<br>back</div>
  <div class="rule" style="width:180px;margin:34px 0 26px"></div>
  <div class="topic" style="color:var(--grey)">From the sketchbook while you wait</div>
</div>
<div class="abs" style="left:120px;bottom:90px;width:700px"><div class="rot"></div></div>""", "Be Right Back")

    out["reveal.html"] = page("bg", f"""
<div class="abs" style="left:240px;top:34px;width:1440px;display:flex;justify-content:space-between;align-items:center">
  <div class="big" style="font-size:58px">The final piece</div>
  <div class="topic" data-ooz="topic"></div></div>
{win(r["page"], "Page camera", "page")}
<div class="abs" style="left:240px;top:960px;width:1440px;display:flex;justify-content:space-between;align-items:center">
  <div class="brand" style="gap:14px"><img src="assets/mark.svg" style="width:52px;height:52px"><div class="name" style="font-size:34px">OOZ STUDIO INK</div></div>
  <div style="display:flex;align-items:center;gap:20px"><div class="pill">Commissions open</div>
    <div class="link" data-ooz="commissionLink"></div></div>
</div>""", "Final Piece")

    out["ending.html"] = page("bg", f"""
<div class="art-bg" style="background-image:url(assets/art/05-the-clash.jpg);left:760px"></div><div class="fade-left"></div>
<div class="abs" style="left:140px;top:120px">{BRAND}</div>
<div class="abs" style="left:140px;top:300px">
  <div class="kicker">That's it for today</div>
  <div class="big" style="font-size:170px;margin-top:14px">Thanks for<br>watching</div>
  <div class="rule" style="width:180px;margin:34px 0 28px"></div>
  <div class="topic" data-ooz="nextStream"></div>
</div>
<div class="abs" style="left:140px;bottom:90px;width:1640px;display:flex;justify-content:space-between;align-items:flex-end">
  <div class="socials"></div>
  <div style="text-align:right"><div class="pill">Commissions open</div>
    <div class="link" style="margin-top:12px" data-ooz="commissionLink"></div>
    <div class="schedule" style="font:600 20px Inter;line-height:1.6;color:var(--grey);margin-top:18px"></div></div>
</div>""", "Ending")
    return out


# ------------------------------------------------------------------ the OBS scene collection
def uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ooz-obs/{name}"))


def source(name: str, kind: str, settings: dict) -> dict:
    return {"name": name, "uuid": uid(name), "id": kind, "versioned_id": kind, "settings": settings,
            "mixers": 0, "sync": 0, "flags": 0, "volume": 1.0, "balance": 0.5, "enabled": True, "muted": False,
            "push-to-mute": False, "push-to-mute-delay": 0, "push-to-talk": False, "push-to-talk-delay": 0,
            "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0, "monitoring_type": 0,
            "private_settings": {}}


def item(name: str, n: int, rect=None) -> dict:
    x, y, w, h = rect or (0, 0, W, H)
    return {"name": name, "source_uuid": uid(name), "visible": True, "locked": rect is None, "rot": 0.0,
            "pos": {"x": float(x), "y": float(y)}, "scale": {"x": 1.0, "y": 1.0}, "align": 5,
            "bounds_type": 2 if rect else 0, "bounds_align": 0, "bounds_crop": False,
            "bounds": {"x": float(w), "y": float(h)} if rect else {"x": 0.0, "y": 0.0},
            "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "id": n,
            "group_item_backup": False, "scale_filter": "disable", "blend_method": "default", "blend_type": "normal",
            "show_transition": {"duration": 0}, "hide_transition": {"duration": 0}, "private_settings": {}}


def scene(name: str, items: list[tuple]) -> dict:
    """items: (source name, rect or None for full screen), bottom first."""
    return source(name, "scene", {"custom_size": False, "id_counter": len(items),
                                   "items": [item(n, i + 1, r) for i, (n, r) in enumerate(items)]})


def collection(root: str) -> dict:
    ov = lambda f: f"{root}/overlays/{f}"  # noqa: E731
    browser = lambda name, f, restart=False: source(name, "browser_source", {  # noqa: E731
        "is_local_file": True, "local_file": ov(f), "width": W, "height": H, "fps_custom": False,
        "reroute_audio": False, "restart_when_active": restart, "shutdown": False,
        "css": "body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }"})
    sources = [
        browser("Overlay · Starting Soon", "starting.html", restart=True),
        browser("Overlay · Drawing", "drawing.html"),
        browser("Overlay · Drawing Focus", "focus.html"),
        browser("Overlay · Drawing (No Webcam)", "drawing-nocam.html"),
        browser("Overlay · Just Chatting", "chatting.html"),
        browser("Overlay · Be Right Back", "brb.html"),
        browser("Overlay · Final Piece", "reveal.html"),
        source("Sketchbook Slideshow", "ffmpeg_source", {
            "local_file": f"{root}/overlays/assets/sketchbook-slideshow.mp4", "is_local_file": True,
            "looping": True, "restart_on_activate": False, "hw_decode": True, "close_when_inactive": True,
            "clear_on_media_end": False}),
        browser("Overlay · Ending", "ending.html"),
        source("YouTube Chat", "browser_source", {
            "is_local_file": False, "url": "https://www.youtube.com/live_chat?is_popout=1&v=PASTE_YOUR_VIDEO_ID",
            "width": CHAT[0], "height": CHAT[1], "fps_custom": False, "reroute_audio": False,
            "restart_when_active": False, "shutdown": False, "css": CHAT_CSS}),
        scene("\u2699 Page Cam", []),                         # add your overhead camera here, once
        scene("\u2699 Face Cam", []),                         # add your face camera here, once
    ]
    d, c, r = LAYOUT["drawing"], LAYOUT["chatting"], LAYOUT["reveal"]
    scenes = [
        ("Starting Soon", [("Overlay · Starting Soon", None)]),
        ("Drawing", [("Overlay · Drawing", None), ("\u2699 Page Cam", d["page"]), ("\u2699 Face Cam", d["face"]),
                     ("YouTube Chat", d["chat"])]),
        ("Drawing \u00b7 No Webcam", [("Overlay · Drawing (No Webcam)", None),
                                     ("\u2699 Page Cam", LAYOUT["nocam"]["page"]), ("YouTube Chat", LAYOUT["nocam"]["chat"])]),
        ("Drawing Focus", [("\u2699 Page Cam", LAYOUT["focus"]["page"]), ("Overlay · Drawing Focus", None)]),
        ("Just Chatting", [("Overlay · Just Chatting", None), ("\u2699 Face Cam", c["face"]),
                           ("YouTube Chat", c["chat"])]),
        ("Be Right Back", [("Overlay · Be Right Back", None), ("Sketchbook Slideshow", (920, 0, 1000, 1080))]),
        ("Final Piece", [("Overlay · Final Piece", None), ("\u2699 Page Cam", r["page"])]),
        ("Ending", [("Overlay · Ending", None)]),
    ]
    sources += [scene(n, its) for n, its in scenes]
    order = [n for n, _ in scenes] + ["\u2699 Page Cam", "\u2699 Face Cam"]
    return {"name": "Ooz Studio Ink", "current_scene": "Starting Soon", "current_program_scene": "Starting Soon",
            "scene_order": [{"name": n} for n in order], "sources": sources, "groups": [],
            "quick_transitions": [{"name": "Cut", "duration": 300, "hotkeys": [], "id": 1, "fade_to_black": False},
                                  {"name": "Fade", "duration": 300, "hotkeys": [], "id": 2, "fade_to_black": False},
                                  {"name": "Fade", "duration": 300, "hotkeys": [], "id": 3, "fade_to_black": True}],
            "transitions": [], "saved_projectors": [], "current_transition": "Fade", "transition_duration": 400,
            "preview_locked": False, "scaling_enabled": False, "scaling_level": 0, "scaling_off_x": 0.0,
            "scaling_off_y": 0.0, "modules": {}, "version": 2}


# ------------------------------------------------------------------ vertical (Facebook / Instagram Live)
# 1080x1920. On phones, Facebook and Instagram draw their own header over the top ~220 px and the
# comments over the bottom third, so the overlays sit just under the header and keep the bottom clear.
# Comments come from the platform itself, so there is no chat source here.
VW, VH = 1080, 1920
V_BRB_VIDEO = (0, 860, 1080, 1060)


def vtop(kicker: str = "") -> str:
    """The small bar under the phone header: mark, LIVE, topic, then the rotating socials line."""
    k = f'<span class="kicker">{kicker}</span>' if kicker else '<span class="topic" style="font-size:24px" data-ooz="topic"></span>'
    return f"""<div class="abs panel" style="left:30px;top:230px;width:1020px">
  <div style="display:flex;align-items:center;gap:16px"><img src="assets/mark.svg" style="width:48px;height:48px">
    <span class="live"><i></i>Live</span>{k}</div>
  <div class="rot" style="margin-top:14px;width:976px"></div></div>"""


def vpages() -> dict[str, str]:
    out = {}
    out["v-starting.html"] = page("bg v", f"""
<div class="art-bg" style="background-image:url(assets/art/01-samurai.jpg)"></div>
<div class="abs" style="inset:0;background:linear-gradient(180deg,var(--ink) 30%,rgba(21,21,21,.55) 75%,var(--ink))"></div>
<div class="abs" style="left:80px;top:250px">{BRAND}</div>
<div class="abs" style="left:80px;top:430px;width:920px">
  <div class="kicker">The stream starts in</div>
  <div class="big" style="font-size:200px;margin-top:14px">Starting<br>soon</div>
  <div class="rule" style="width:180px;margin:36px 0 30px"></div>
  <div class="big" data-countdown style="font-size:140px;color:var(--red)">5:00</div>
  <div class="topic" style="margin-top:28px"><span class="kicker" style="margin-right:16px">Today</span><span data-ooz="topic"></span></div>
  <div class="schedule" style="font:600 24px Inter;line-height:1.6;margin-top:44px;color:var(--grey)"></div>
  <div style="margin-top:36px"><div class="pill">Commissions open</div>
    <div class="link" style="margin-top:12px;font-size:22px" data-ooz="commissionLink"></div></div>
</div>""", "Starting Soon (vertical)")
    out["v-drawing.html"] = page("clear v", vtop(), "Drawing (vertical)")
    out["v-chatting.html"] = page("clear v", vtop("Just chatting"), "Just Chatting (vertical)")
    out["v-reveal.html"] = page("clear v", """<div class="abs panel" style="left:30px;top:230px;width:1020px;display:flex;
  justify-content:space-between;align-items:center"><div class="big" style="font-size:54px">The final piece</div>
  <div class="pill">Commissions open</div></div>
<div class="abs panel" style="left:30px;top:350px;width:1020px;padding:12px 22px"><div class="link" style="font-size:22px"
  data-ooz="commissionLink"></div></div>""", "Final Piece (vertical)")
    arts = "".join(f'<img src="assets/art/{p.name}">' for p in sorted((ROOT / "portfolio").glob("*.jpg")))
    x, y, w, h = V_BRB_VIDEO
    out["v-brb.html"] = page("bg v", f"""
<div class="slides" data-preview-only style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">{arts}</div>
<div class="abs" data-preview-only style="left:0;top:{y}px;width:{w}px;height:220px;background:linear-gradient(180deg,var(--ink),rgba(21,21,21,0))"></div>
<div class="abs" style="left:80px;top:250px">{BRAND}</div>
<div class="abs" style="left:80px;top:410px">
  <div class="kicker">Short break</div>
  <div class="big" style="font-size:170px;margin-top:14px">Be right back</div>
  <div class="topic" style="color:var(--grey);margin-top:24px">From the sketchbook while you wait</div>
</div>""", "Be Right Back (vertical)")
    out["v-ending.html"] = page("bg v", f"""
<div class="art-bg" style="background-image:url(assets/art/05-the-clash.jpg)"></div>
<div class="abs" style="inset:0;background:linear-gradient(180deg,var(--ink) 35%,rgba(21,21,21,.6) 80%,var(--ink))"></div>
<div class="abs" style="left:80px;top:250px">{BRAND}</div>
<div class="abs" style="left:80px;top:420px;width:920px">
  <div class="kicker">That's it for today</div>
  <div class="big" style="font-size:180px;margin-top:14px">Thanks for<br>watching</div>
  <div class="rule" style="width:180px;margin:34px 0 26px"></div>
  <div class="topic" data-ooz="nextStream"></div>
  <div style="margin-top:34px"><div class="pill">Commissions open</div>
    <div class="link" style="margin-top:12px;font-size:22px" data-ooz="commissionLink"></div></div>
  <div class="socials" style="margin-top:40px"></div>
</div>""", "Ending (vertical)")
    return out


def vcollection(root: str) -> dict:
    ov = lambda f: f"{root}/overlays/{f}"  # noqa: E731
    browser = lambda name, f, restart=False: source(name, "browser_source", {  # noqa: E731
        "is_local_file": True, "local_file": ov(f), "width": VW, "height": VH, "fps_custom": False,
        "reroute_audio": False, "restart_when_active": restart, "shutdown": False,
        "css": "body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }"})
    sources = [
        browser("Overlay · Starting Soon", "v-starting.html", restart=True),
        browser("Overlay · Drawing", "v-drawing.html"),
        browser("Overlay · Just Chatting", "v-chatting.html"),
        browser("Overlay · Be Right Back", "v-brb.html"),
        browser("Overlay · Final Piece", "v-reveal.html"),
        browser("Overlay · Ending", "v-ending.html"),
        source("Sketchbook Slideshow", "ffmpeg_source", {
            "local_file": ov("assets/sketchbook-slideshow-vertical.mp4"), "is_local_file": True, "looping": True,
            "restart_on_activate": False, "hw_decode": True, "close_when_inactive": True, "clear_on_media_end": False}),
        scene("⚙ Page Cam", []),
        scene("⚙ Face Cam", []),
    ]
    scenes = [
        ("Starting Soon", [("Overlay · Starting Soon", None)]),
        ("Drawing", [("⚙ Page Cam", None), ("Overlay · Drawing", None)]),
        ("Just Chatting", [("⚙ Face Cam", None), ("Overlay · Just Chatting", None)]),
        ("Be Right Back", [("Overlay · Be Right Back", None), ("Sketchbook Slideshow", V_BRB_VIDEO)]),
        ("Final Piece", [("⚙ Page Cam", None), ("Overlay · Final Piece", None)]),
        ("Ending", [("Overlay · Ending", None)]),
    ]
    out = collection(root)                       # same transitions and settings as the landscape one
    sources += [scene(n, its) for n, its in scenes]
    out.update(name="Ooz Studio Ink (Vertical)", sources=sources,
               scene_order=[{"name": n} for n, _ in scenes] + [{"name": "⚙ Page Cam"}, {"name": "⚙ Face Cam"}])
    return out


def build() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    ovl = OUT / "overlays"
    (ovl / "assets" / "fonts").mkdir(parents=True)
    (ovl / "assets" / "art").mkdir(parents=True)
    for name, html in {**pages(), **vpages()}.items():
        (ovl / name).write_text(html)
    (ovl / "style.css").write_text(STYLE)
    (ovl / "app.js").write_text(APP_JS)
    (ovl / "config.js").write_text(CONFIG_JS)
    (ovl / "assets" / "mark.svg").write_text(MARK_SVG)
    (OUT / "youtube-chat.css").write_text(CHAT_CSS)
    fonts = ovl / "assets" / "fonts"
    shutil.copy(ROOT / "reels-ai" / "fonts" / "Anton-Regular.ttf", fonts)
    shutil.copy(ROOT / "reels-ai" / "fonts" / "OFL-Anton.txt", fonts)
    for f in ("Inter-Medium.otf", "Inter-SemiBold.otf", "Inter-Bold.otf"):
        shutil.copy(Path("/usr/share/fonts/opentype/inter") / f, fonts)
    shutil.copy("/usr/share/doc/fonts-inter/copyright", fonts / "LICENSE-Inter.txt")
    from PIL import Image
    for p in sorted((ROOT / "portfolio").glob("*.jpg")):    # your sketchbook pieces, for Be Right Back
        img = Image.open(p).convert("RGB")
        img.thumbnail((1600, 1600))
        img.save(ovl / "assets" / "art" / p.name, quality=88)
    slideshow(sorted((ovl / "assets" / "art").glob("*.jpg")), ovl / "assets" / "sketchbook-slideshow.mp4")
    slideshow(sorted((ovl / "assets" / "art").glob("*.jpg")), ovl / "assets" / "sketchbook-slideshow-vertical.mp4",
              size=V_BRB_VIDEO[2:], edge_side="top")
    (OUT / "READ ME FIRST.txt").write_text(README)
    for os_name, root in INSTALL.items():
        (OUT / f"Ooz Studio Ink - OBS scenes ({os_name}).json").write_text(
            json.dumps(collection(root), indent=1, ensure_ascii=False))
        (OUT / f"Ooz Studio Ink - Vertical for Facebook + Instagram Live ({os_name}).json").write_text(
            json.dumps(vcollection(root), indent=1, ensure_ascii=False))


def slideshow(arts: list[Path], out: Path, hold: float = 7.0, fade: float = 1.0, size=(1000, 1080),
              edge_side: str = "left") -> Path:
    """The Be Right Back loop: each drawing for `hold` seconds, crossfading into the next, the last
    back into the first (a seamless loop). Light to play: 24 fps H.264, no sound, hardware-decoded,
    and OBS closes it while the scene isn't showing."""
    import subprocess
    w, h = size
    seq = arts + arts[:1]
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for a in seq:
        cmd += ["-loop", "1", "-t", f"{hold + fade}", "-framerate", "24", "-i", str(a)]
    # each picture covers the window; the left edge fades into the page colour like the old slideshow
    prep = "".join(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,format=yuv420p[p{i}];"
                   for i in range(len(seq)))
    chain, last = "", "p0"
    for i in range(1, len(seq)):
        chain += f"[{last}][p{i}]xfade=transition=fade:duration={fade}:offset={i * hold}[x{i}];"
        last = f"x{i}"
    # cut from 1 s in to the moment the last drawing has faded back into the first: a seamless loop
    loop = f"[{last}]trim=start={fade}:end={fade + len(arts) * hold},setpts=PTS-STARTPTS[loop];"
    strip, ramp = (f"220x{h}", "1-X/W") if edge_side == "left" else (f"{w}x220", "1-Y/H")
    edge = (f"color=c=0x151515:s={strip}:r=24,format=rgba,geq=r=21:g=21:b=21:a='255*({ramp})'[edge];"
            f"[loop][edge]overlay=0:0:shortest=1[v]")
    subprocess.run(cmd + ["-filter_complex", prep + chain + loop + edge, "-map", "[v]", "-t", f"{len(arts) * hold}",
                          "-r", "24", "-c:v", "libx264", "-preset", "slow", "-crf", "24", "-tune", "stillimage",
                          "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(out)], check=True)
    return out


def zip_it() -> Path:
    """obs/Ooz Studio Ink OBS.zip, with the package inside a folder named for the install path."""
    import zipfile
    dest = ROOT / "obs" / f"{FOLDER}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OUT.rglob("*")):
            if f.is_file():
                z.write(f, f"{FOLDER}/{f.relative_to(OUT)}")
    return dest


if __name__ == "__main__":
    build()
    print(f"wrote {OUT} and {zip_it()}")
