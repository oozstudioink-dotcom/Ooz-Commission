OOZ STUDIO INK - STREAMLABS THEME
=================================
Manga style: every alert is an inked manga panel that slams onto the screen with speed lines, red
screentone, a Japanese sound effect breaking out of the panel, a narration box, and the studio credit,
then a brush slash wipes it away. Black, off-white and brush red, like the rest of the channel.

WHAT'S IN HERE
  alert-follower.webm   follow     "NEW FOLLOWER"    ドン (DON, the impact beat)   "A new reader joins the story..."
  alert-tip.webm        tip        "THANK YOU"       キラッ (KIRA, sparkle)        "The studio thanks you..."
  alert-sub.webm        sub        "NEW SUBSCRIBER"  ゴゴゴ (GOGOGO, the rumble)    "A new member of the studio..."
  alert-raid.webm       raid       "RAID INCOMING"   ドドド (DODODO, charging)      "An army approaches..."  (rush lines)
  alert-sound.mp3       whoosh + impact hit + small sparkle, timed to the panel slam
  alert-image.png       still version of the mark, for any alert type without its own animation
  alert-box.css         custom CSS for the Alert Box: the viewer's name slams in under the panel in a manga
                        caption box (white, inked border, red offset shadow), with their tip message under it
  chat-box.css          custom CSS for the Chat Box
  event-list.css        custom CSS for the Event List (recent follows/tips for the side of the screen)
  preview/alerts-preview.mp4   all four alerts with sound on a dark background, to look at
  src/                  how the animations were made (not needed in Streamlabs)

The animations are transparent videos (4 seconds, 800x450), so they sit over your drawing without a box.

1) ALERT BOX  (streamlabs.com > Dashboard > All Widgets > Alert Box)
   General settings:
     Layout: image above text
     Alert duration: 4 seconds    Text animation delay: 0    Alert animation in/out: none (the video has its own)
   Then for each tab - Follows, Subscriptions, Donations (tips), Raids - and on Kick through
   Streamlabs, the matching Kick follow/sub tabs:
     Image/Video:  upload the matching .webm (Follows = alert-follower, Donations = alert-tip,
                   Subs = alert-sub, Raids = alert-raid). Bits/Cheers and Hosts can use alert-tip / alert-raid.
     Sound:        alert-sound.mp3, volume about 40%
     Message template (the animation already says what happened, so the text is just who):
                   Follows:   {name}
                   Subs:      {name} · {months} months
                   Donations: {name} · {amount}
                   Raids:     {name} + {count} raiders
     Font: leave it, the CSS sets it.
     Enable Custom HTML/CSS > CSS tab: replace everything with alert-box.css
   In Streamlabs Desktop the Alert Box source then picks this up by itself. Size the source to about
   900x650 and put it top-centre or over the empty corner of your page.

2) CHAT BOX  (All Widgets > Chat Box)
   Theme: Clean.  Background: transparent.  Font size: 18.
   Enable Custom HTML/CSS > CSS tab: replace with chat-box.css
   Messages show as dark cards with a red edge and your names in the soft red.

3) EVENT LIST  (All Widgets > Event List)
   Theme: Clean. Pick Follows, Subs, Tips, Raids. Max events: 5.
   Enable Custom HTML/CSS > CSS tab: replace with event-list.css

4) SCENES
   Your scenes (Starting Soon, Drawing, Just Chatting, BRB with the sketchbook loop, Ending, and the
   vertical ones) are in obs/Ooz Studio Ink OBS.zip. In Streamlabs Desktop: Settings > General >
   Import from OBS, or Scene Collections > Import, and pick the .json from that zip. Then add the
   Alert Box, Chat Box and Event List from the Widgets list on top of each scene you want them in.

TIP: Streamlabs Desktop > right-click the Alert Box > "Test Widget" plays each alert so you can
place it without waiting for a real follow.
