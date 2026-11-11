"""Ears: what was said in a recording, with times.

The audio is pulled from the original recording and transcribed on the machine the bot runs on
(Whisper, an open-source speech-to-text model: no API, nothing leaves the run). The transcript
tells the editor what the pictures can't: "this one is Deku landing", or that the artist is
talking about another drawing while a page is on screen.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def pull_audio(raw_input, dst: Path, timeout: int = 1500) -> Path | None:
    """The recording's sound as a small mono file. None when it has no sound (most timelapses)."""
    cmd = ["ffmpeg", "-v", "error", "-y", *raw_input(0.0), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(dst)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0 or not dst.exists() or dst.stat().st_size < 16000 * 2 * 3:   # under 3 s
        return None
    return dst


def transcribe(audio: Path, model: str = "base") -> list[dict]:
    """[{"t0", "t1", "text"}] in recording seconds. Silence is skipped."""
    from faster_whisper import WhisperModel            # heavy import: only when there is sound
    wm = WhisperModel(model, device="cpu", compute_type="int8")
    segments, _ = wm.transcribe(str(audio), vad_filter=True, beam_size=1)
    out = []
    for s in segments:
        text = (s.text or "").strip()
        if text:
            out.append({"t0": round(float(s.start), 1), "t1": round(float(s.end), 1), "text": text})
    return out


def listen(raw_input, work: Path, model: str = "base", log=print) -> list[dict]:
    """Transcript of a recording, or [] when it is silent or anything goes wrong (the editor
    then works from the pictures alone)."""
    try:
        audio = pull_audio(raw_input, Path(work) / "audio.wav")
        if audio is None:
            return []
        lines = transcribe(audio, model)
        audio.unlink(missing_ok=True)
        log(f"ears: {len(lines)} spoken lines heard")
        return lines
    except Exception as e:  # noqa: BLE001 - ears are a help, never a blocker
        log(f"warn: couldn't transcribe the recording: {str(e)[:160]}")
        return []


def as_text(lines: list[dict], limit: int = 24000) -> str:
    """Transcript as '[12:30] words' lines, shortened evenly if it is very long."""
    rows = [f"[{int(x['t0']) // 60}:{int(x['t0']) % 60:02d}] {x['text']}" for x in lines]
    text = "\n".join(rows)
    if len(text) <= limit or not rows:
        return text
    keep = max(1, int(len(rows) * limit / len(text)))
    step = len(rows) / keep
    return "\n".join(rows[int(i * step)] for i in range(keep))
