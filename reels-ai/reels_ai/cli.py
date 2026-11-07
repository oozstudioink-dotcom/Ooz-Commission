"""Command line.

python -m reels_ai make VIDEO [--subject NAME]    render variants locally
python -m reels_ai bot --phase poll|render|all    one run of the Telegram approval bot
"""
from __future__ import annotations

import argparse
from pathlib import Path

STYLES = ["reveal_first", "speedrun", "before_after", "detail_zoom", "ink_only"]


def make(args: argparse.Namespace) -> None:
    from .pipeline import make_variants   # heavy imports (OpenCV) only when rendering

    make_variants(Path(args.video), Path(args.out), args.variants, args.seed, args.subject,
                  Path(args.audio) if args.audio else None, args.sheets, args.keep_work)


def bot(args: argparse.Namespace) -> None:
    from .bot import run

    run(args.phase)


def main() -> None:
    ap = argparse.ArgumentParser(prog="reels_ai")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make", help="analyse a timelapse and render variants")
    m.add_argument("video")
    m.add_argument("--out", default="output")
    m.add_argument("--subject", help="character / piece name used in hooks and hashtags")
    m.add_argument("--variants", nargs="+", choices=STYLES)
    m.add_argument("--audio", help="audio file to lay under every variant (looped)")
    m.add_argument("--seed", type=int, default=1, help="change to get different hook picks")
    m.add_argument("--sheets", action="store_true", help="also write contact sheets")
    m.add_argument("--keep-work", action="store_true")
    m.set_defaults(func=make)
    b = sub.add_parser("bot", help="one run of the Telegram approval bot")
    b.add_argument("--phase", choices=["poll", "render", "all", "check"], default="all",
                   help="check = read-only connection test of the social accounts")
    b.set_defaults(func=bot)
    args = ap.parse_args()
    args.func(args)
