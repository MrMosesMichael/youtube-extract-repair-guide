"""Transcripts: parse YouTube VTT captions or transcribe locally with faster-whisper."""

from __future__ import annotations

import html
import re
from pathlib import Path

from . import ffmpeg
from .util import UserError, fmt_time, info

Segment = dict  # {"start": float, "end": float, "text": str}

_CUE_RE = re.compile(
    r"(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})\s+-->\s+(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})"
)


def _ts(h: str | None, m: str, s: str, ms: str) -> float:
    return int((h or "0:")[:-1] or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt(text: str) -> list[Segment]:
    """Parse WebVTT into clean, de-duplicated line segments.

    YouTube auto-captions "roll": each line appears in 2–3 consecutive cues. Keeping
    a line only when it differs from the previously kept line removes the repeats.
    """
    segs: list[Segment] = []
    last = ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CUE_RE.search(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        start, end = _ts(g[0], g[1], g[2], g[3]), _ts(g[4], g[5], g[6], g[7])
        i += 1
        while i < len(lines) and lines[i].strip():
            raw = re.sub(r"<[^>]+>", "", lines[i])
            raw = html.unescape(raw).strip()
            raw = re.sub(r"\s+", " ", raw)
            if raw and raw != last and raw not in ("[Music]", "[Applause]"):
                segs.append({"start": start, "end": end, "text": raw})
                last = raw
            i += 1
    return segs


def transcribe(video: Path, model_name: str = "base.en") -> list[Segment]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise UserError(
            "This video has no captions and local transcription isn't installed.\n"
            "Install it with:  uv tool install --force 'repair-guide[transcribe] @ "
            "git+https://github.com/MrMosesMichael/youtube-extract-repair-guide'\n"
            "(or rerun with --no-transcript to build a guide from visuals only)."
        ) from exc

    # Decode audio with our ffmpeg straight to 16 kHz mono float32 and hand faster-whisper
    # the array. This sidesteps its PyAV decoder, whose Intel-Mac wheels lag behind.
    import numpy as np
    import subprocess

    info("  extracting audio…")
    proc = subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise UserError(f"Audio extraction failed: {proc.stderr.decode(errors='replace')[-300:]}")
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size < 16000:
        info("  (no usable audio track)")
        return []
    info(f"  transcribing locally with whisper '{model_name}' (CPU — roughly real-time or faster)…")
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    pieces, _ = model.transcribe(audio, vad_filter=True, beam_size=5, language="en" if model_name.endswith(".en") else None)
    segs = []
    for p in pieces:
        segs.append({"start": p.start, "end": p.end, "text": p.text.strip()})
        if len(segs) % 50 == 0:
            info(f"    … {fmt_time(p.end)}")
    return segs


def to_markdown(segs: list[Segment], chapters: list[dict], block_seconds: float = 30.0) -> str:
    """Readable transcript: paragraphs every ~30 s, each prefixed with [m:ss]; chapter headings."""
    out: list[str] = []
    chapters = sorted([c for c in chapters if c.get("start") is not None], key=lambda c: c["start"])
    ci = 0
    block: list[str] = []
    block_start: float | None = None

    def flush() -> None:
        nonlocal block, block_start
        if block:
            out.append(f"[{fmt_time(block_start or 0)}] " + " ".join(block))
            out.append("")
        block, block_start = [], None

    for s in segs:
        while ci < len(chapters) and s["start"] >= chapters[ci]["start"]:
            flush()
            out.append(f"## Chapter: {chapters[ci]['title']} ({fmt_time(chapters[ci]['start'])})")
            out.append("")
            ci += 1
        if block_start is None:
            block_start = s["start"]
        block.append(s["text"])
        if s["end"] - block_start >= block_seconds and s["text"].rstrip().endswith((".", "?", "!")) or (
            s["end"] - (block_start or 0) >= block_seconds * 1.6
        ):
            flush()
    flush()
    return "\n".join(out).strip() + "\n"
