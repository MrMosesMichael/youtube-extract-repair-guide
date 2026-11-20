"""Resolve a video source (YouTube URL or local file) into metadata + a local video file."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path

from . import ffmpeg
from .util import UserError, info

YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


def is_url(src: str) -> bool:
    return bool(re.match(r"https?://", src.strip()))


def youtube_id(url: str) -> str | None:
    m = YT_ID_RE.search(url)
    return m.group(1) if m else None


def time_link(meta: dict, seconds: float) -> str | None:
    """Deep link that opens the video at `seconds`."""
    t = max(0, int(seconds))
    if meta.get("kind") == "youtube" and meta.get("id"):
        return f"https://www.youtube.com/watch?v={meta['id']}&t={t}s"
    if meta.get("kind") == "url" and meta.get("url"):
        return meta["url"]
    if meta.get("kind") == "local" and meta.get("path"):
        # Media-fragment URI: browsers seek <video>/file:// playback to #t=
        return Path(meta["path"]).as_uri() + f"#t={t}"
    return None


def _ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": ffmpeg.ffmpeg_exe(),
        # YouTube needs a JS runtime for signature solving; allow Node (common on dev
        # machines) as well as yt-dlp's default, Deno.
        "js_runtimes": {"deno": {}, "node": {}},
    }
    opts.update(extra)
    return opts


def fetch_youtube_metadata(url: str) -> dict:
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as ydl:
            raw = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise UserError(f"yt-dlp could not read {url}: {exc}") from exc
    return raw


def pick_subtitle(raw: dict, langs: list[str]) -> tuple[str, str, bool] | None:
    """Return (vtt_url, lang, is_auto). Human-made captions win over auto-generated."""
    for is_auto, table in ((False, raw.get("subtitles") or {}), (True, raw.get("automatic_captions") or {})):
        keys = list(table)
        for want in langs:
            # exact first, then prefix (en -> en-US), but avoid machine translations (en-xx-...)
            cands = [k for k in keys if k == want] or [
                k for k in keys if k.startswith(want + "-") and k.count("-") == 1
            ]
            if is_auto:
                # 'en-orig' is the untranslated auto track; prefer it
                cands = sorted(cands, key=lambda k: 0 if k.endswith("-orig") else 1)
                orig = [k for k in keys if k == f"{want}-orig"]
                cands = orig + [c for c in cands if c not in orig]
            for k in cands:
                for fmt in table[k]:
                    if fmt.get("ext") == "vtt" and fmt.get("url"):
                        return fmt["url"], k, is_auto
    return None


def download_youtube(url: str, work: Path, max_height: int, langs: list[str]) -> dict:
    import yt_dlp

    raw = fetch_youtube_metadata(url)
    if raw.get("_type") == "playlist":
        raise UserError("That's a playlist URL. Pass a single video URL.")
    work.mkdir(parents=True, exist_ok=True)

    meta = {
        "kind": "youtube",
        "id": raw.get("id"),
        "url": raw.get("webpage_url") or url,
        "title": raw.get("title") or "Untitled video",
        "channel": raw.get("channel") or raw.get("uploader"),
        "upload_date": raw.get("upload_date"),
        "duration": raw.get("duration"),
        "description": raw.get("description") or "",
        "chapters": [
            {"title": c.get("title"), "start": c.get("start_time"), "end": c.get("end_time")}
            for c in (raw.get("chapters") or [])
        ],
        "subtitle": None,
    }

    sub = pick_subtitle(raw, langs)
    if sub:
        sub_url, lang, is_auto = sub
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            vtt = ydl.urlopen(sub_url).read().decode("utf-8", "replace")
        (work / "subtitles.vtt").write_text(vtt, encoding="utf-8")
        meta["subtitle"] = {"lang": lang, "auto": is_auto}
        info(f"  captions: {lang} ({'auto-generated' if is_auto else 'human-made'})")
    else:
        info("  captions: none available — will need local transcription")

    video_path = work / "video.mp4"
    if not video_path.exists():
        info(f"  downloading video (≤{max_height}p)…")
        h = max_height
        fmt = (
            f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/b[height<={h}]/b"
        )
        opts = _ydl_opts(
            format=fmt,
            outtmpl=str(work / "video.%(ext)s"),
            merge_output_format="mp4",
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([meta["url"]])
        except Exception as exc:
            raise UserError(f"Video download failed: {exc}") from exc
        if not video_path.exists():
            got = sorted(work.glob("video.*"))
            got = [g for g in got if g.suffix not in (".vtt", ".part")]
            if not got:
                raise UserError("Download finished but no video file was produced.")
            got[0].rename(video_path)
    meta["video"] = str(video_path)
    return meta


def find_proxy(path: Path, duration: float | None) -> Path | None:
    """GoPro writes a low-res .LRV proxy beside each clip with identical timing."""
    for cand in path.parent.iterdir():
        if cand.stem == path.stem and cand.suffix.lower() == ".lrv":
            d = ffmpeg.probe(cand)["duration"]
            if d and duration and abs(d - duration) < 0.5:
                return cand
    return None


def local_metadata(path: Path) -> dict:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise UserError(f"File not found: {path}")
    p = ffmpeg.probe(path)
    proxy = find_proxy(path, p["duration"])
    return {
        "proxy": str(proxy) if proxy else None,
        "kind": "local",
        "id": None,
        "url": path.as_uri(),
        "path": str(path),
        "title": path.stem.replace("_", " "),
        "channel": None,
        "upload_date": None,
        "duration": p["duration"],
        "description": "",
        "chapters": [],
        "subtitle": None,
        "video": str(path),
    }


def describe(meta: dict) -> str:
    host = urllib.parse.urlparse(meta.get("url") or "").netloc or "local file"
    return f"{meta.get('title')} — {meta.get('channel') or host}"
