"""The bot, in parts. reels_ai/bot.py puts them together into the Runner.

commands.py     Telegram commands that change what gets made: /redo, /retire, /ideas, /clear, /longform.
drive_inbox.py  Recordings coming in: the Drive inbox, Telegram uploads, reading originals, saving results.
inventory.py    /inventory and /delete: what you have, and removing what you don't want.
naming.py       Asking what an unnamed recording is, and naming it from your reply.
drops.py        The bank and the daily drops: what is ready, what goes out when, approvals and retirements.
posting.py      Posting to platforms, stats and reports.
learning.py     Which styles to make: your ratings, view counts, and the AI-written copy.
render.py       Turning queued recordings into Shorts: watching, splitting, rendering, the reviewer's verdict, sending.
common.py       labels, buttons and small helpers every part uses
connect.py      the connection check and the one-time TikTok sign-in
"""
