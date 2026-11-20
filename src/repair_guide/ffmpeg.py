"""Locate and run ffmpeg.

Prefers a system ffmpeg on PATH (e.g. from Homebrew); otherwise falls back to the
static binary bundled with the ``imageio-ffmpeg`` wheel, so nothing extra needs
installing — including on Intel Macs.
"""

from __future__ import annotations

import functools
import re
import shutil
import subprocess
from pathlib import Path

from .util import UserError


@functools.cache
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - depends on environment
        raise UserError(
            "ffmpeg not found. Install it (`brew install ffmpeg`) or reinstall repair-guide "
            "so the bundled imageio-ffmpeg binary is available."
        ) from exc


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if check and proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise UserError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return proc


def probe(path: Path) -> dict:
    """Duration / size / fps via `ffmpeg -i` (imageio-ffmpeg ships no ffprobe)."""
    proc = run(["-i", str(path)], check=False)
    err = proc.stderr
    if "Invalid data found" in err or "No such file" in err:
        raise UserError(f"Can't read video file: {path}")
    out: dict = {"duration": None, "width": None, "height": None, "fps": None}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        h, mi, s = m.groups()
        out["duration"] = int(h) * 3600 + int(mi) * 60 + float(s)
    m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
    if m:
        out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", err)
    if m:
        out["fps"] = float(m.group(1))
    out["has_audio"] = bool(re.search(r"Stream #.*Audio:", err))
    return out
