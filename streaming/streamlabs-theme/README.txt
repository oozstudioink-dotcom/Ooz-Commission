OOZ STUDIO INK - STREAMLABS THEME
=================================
Black, off-white and brush red, the same look as the channel, the OBS scenes and the Twitch/Kick art.

WHAT'S IN HERE
  alert-follower.webm   follow animation   ("NEW FOLLOWER", red brush stroke, ink-dot mark spins in)
  alert-tip.webm        tip / donation     ("THANK YOU")
  alert-sub.webm        subscriber         ("NEW SUBSCRIBER")
  alert-raid.webm       raid / host        ("RAID INCOMING")
  alert-sound.mp3       short swish + two-note chime for every alert
  alert-image.png       still version of the mark, for any alert type without its own animation
  alert-box.css         custom CSS for the Alert Box (the viewer's name, amount and message under the animation)
  chat-box.css          custom CSS for the Chat Box
  event-list.css        custom CSS for the Event List (recent follows/tips for the side of the screen)
  preview/alerts-preview.mp4   all four alerts with sound on a dark background, to look at
  src/                  how the animations were made (not needed in Streamlabs)

The animations are transparent videos (3.6 seconds, 640x360), so they sit over your drawing without a box.

1) ALERT BOX  (streamlabs.com > Dashboard > All Widgets > Alert Box)
   General settings:
     Layout: image above text
     Alert duration: 4 seconds    Text animation delay: 0
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
   800x600 and put it top-centre or over the empty corner of your page.

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
