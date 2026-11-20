"""Frame work: candidate discovery, contact sheets, sharp-frame refinement, compression."""

from __future__ import annotations

import io
import math
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from . import ffmpeg
from .util import UserError, fmt_time, fmt_time_file, info

# ---------------------------------------------------------------- candidate discovery


def detect_scenes(video: Path, threshold: float) -> list[float]:
    """Timestamps where the picture changes a lot (camera cut, new angle, part comes off)."""
    # Plain software decode. (Tried -hwaccel videotoolbox: ~8x *slower* on Intel Macs
    # because every frame is copied back from the GPU for the scale/select filters.)
    vf = f"fps=4,scale=320:-2,select='gt(scene,{threshold})',metadata=print:file=-"
    proc = ffmpeg.run(["-threads", "0", "-i", str(video), "-vf", vf, "-an", "-sn", "-f", "null", "-"])
    return [float(x) for x in re.findall(r"pts_time:(\d+(?:\.\d+)?)", proc.stdout)]


def merge_times(scenes: list[float], duration: float, interval: float, min_gap: float) -> list[float]:
    """Scene changes (+0.6 s so the new shot has settled) plus a regular sweep so long
    static shots — the norm in garage videos — still get covered."""
    times = {round(min(duration - 0.5, s + 0.6), 2) for s in scenes}
    t = min(2.0, duration / 2)
    while t < duration - 0.5:
        times.add(round(t, 2))
        t += interval
    kept: list[float] = []
    for t in sorted(times):
        if t < 0:
            continue
        if not kept or t - kept[-1] >= min_gap:
            kept.append(t)
    return kept


def thin_evenly(times: list[float], max_count: int) -> list[float]:
    if len(times) <= max_count:
        return times
    step = len(times) / max_count
    return [times[int(i * step)] for i in range(max_count)]


# ---------------------------------------------------------------- frame grabbing


def grab(video: Path, t: float, width: int | None = None) -> Image.Image:
    """Decode a single frame at time t (fast input seeking) into a PIL image."""
    vf = ["-vf", f"scale={width}:-2:flags=lanczos"] if width else []
    cmd = [
        ffmpeg.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1", *vf,
        "-f", "image2pipe", "-vcodec", "png", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise UserError(f"Could not grab frame at {fmt_time(t)}: {proc.stderr.decode(errors='replace')[-300:]}")
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def grab_many(video: Path, times: list[float], width: int | None, workers: int = 4) -> list[Image.Image]:
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda t: grab(video, t, width), times))


def signature(img: Image.Image, size: int = 16) -> list[int]:
    return list(img.convert("L").resize((size, size), Image.Resampling.BILINEAR).getdata())


def sig_delta(a: list[int], b: list[int]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def sharpness(img: Image.Image) -> float:
    """Edge-energy score: higher = crisper (less motion blur / focus hunting)."""
    g = img.convert("L")
    if g.width > 640:
        g = g.resize((640, round(640 * g.height / g.width)), Image.Resampling.BILINEAR)
    return ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]


# ---------------------------------------------------------------- candidates + sheets


def build_candidates(
    video: Path,
    sheets_dir: Path,
    duration: float,
    *,
    interval: float = 8.0,
    scene_threshold: float = 0.30,
    min_gap: float = 2.0,
    max_candidates: int = 240,
    dedupe_delta: float = 5.0,
    per_sheet: int = 16,
    proxy: Path | None = None,
) -> list[dict]:
    """`proxy` = a low-res copy with identical timing (e.g. GoPro .LRV); used for the
    analysis passes only, which is far faster than decoding 1080p/4K masters."""
    src = proxy or video
    info("  detecting scene changes…" + (f" (using proxy {proxy.name})" if proxy else ""))
    scenes = detect_scenes(src, scene_threshold)
    times = merge_times(scenes, duration, interval, min_gap)
    times = thin_evenly(times, max_candidates * 2)
    info(f"  {len(scenes)} scene changes; sampling {len(times)} candidate frames…")

    thumbs = grab_many(src, times, 480)
    kept: list[tuple[float, Image.Image]] = []
    last_sig: list[int] | None = None
    for t, im in zip(times, thumbs):
        sig = signature(im)
        if last_sig is not None and sig_delta(sig, last_sig) < dedupe_delta:
            continue
        kept.append((t, im))
        last_sig = sig
    if len(kept) > max_candidates:
        idx = thin_evenly(list(range(len(kept))), max_candidates)
        kept = [kept[i] for i in idx]
    info(f"  kept {len(kept)} distinct frames after de-duplication")

    cands: list[dict] = []
    for n, (t, im) in enumerate(kept, start=1):
        cands.append({"n": n, "t": round(t, 2), "time": fmt_time(t), "sheet": (n - 1) // per_sheet + 1, "_img": im})

    sheets_dir.mkdir(parents=True, exist_ok=True)
    for old in sheets_dir.glob("sheet-*.jpg"):
        old.unlink()
    n_sheets = math.ceil(len(cands) / per_sheet)
    for s in range(n_sheets):
        chunk = cands[s * per_sheet : (s + 1) * per_sheet]
        _contact_sheet(chunk).save(sheets_dir / f"sheet-{s + 1:02d}.jpg", quality=80, optimize=True)
    for c in cands:
        c.pop("_img")
    return cands


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _contact_sheet(chunk: list[dict], cols: int = 4, tile_w: int = 400) -> Image.Image:
    first = chunk[0]["_img"]
    tile_h = round(tile_w * first.height / first.width)
    rows = math.ceil(len(chunk) / cols)
    pad = 6
    sheet = Image.new("RGB", (cols * (tile_w + pad) + pad, rows * (tile_h + pad) + pad), (20, 20, 20))
    draw = ImageDraw.Draw(sheet)
    font = _font(22)
    for i, c in enumerate(chunk):
        x = pad + (i % cols) * (tile_w + pad)
        y = pad + (i // cols) * (tile_h + pad)
        sheet.paste(ImageOps.fit(c["_img"], (tile_w, tile_h)), (x, y))
        label = f"#{c['n']}  {c['time']}"
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.rectangle((x, y, x + bbox[2] + 12, y + bbox[3] + 8), fill=(0, 0, 0))
        draw.text((x + 6, y + 3), label, fill=(255, 220, 0), font=font)
    return sheet


# ---------------------------------------------------------------- final step images


def refine_time(video: Path, t: float, window: float = 1.0, samples: int = 7) -> float:
    """Pick the sharpest frame within ±window of t (slight bias toward the requested t)."""
    if samples < 2 or window <= 0:
        return t
    ts = [max(0.0, t - window + i * (2 * window) / (samples - 1)) for i in range(samples)]
    imgs = grab_many(video, ts, 640)
    best_t, best_score = t, -1.0
    for ti, im in zip(ts, imgs):
        score = sharpness(im) * (1 - 0.2 * abs(ti - t) / window)
        if score > best_score:
            best_t, best_score = ti, score
    return best_t


def save_compressed(img: Image.Image, path: Path, width: int = 1280, max_kb: int = 150) -> int:
    """Resize to `width`, light unsharp-mask for legibility, then JPEG at the highest
    quality that fits under `max_kb`. Returns final size in bytes."""
    if img.width > width:
        img = img.resize((width, round(width * img.height / img.width)), Image.Resampling.LANCZOS)
        img = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=45, threshold=3))
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b""
    for _ in range(4):
        for q in (82, 76, 70, 64, 58, 52):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, optimize=True, progressive=True, subsampling="4:2:0")
            data = buf.getvalue()
            if len(data) <= max_kb * 1024:
                path.write_bytes(data)
                return len(data)
        # still too big at q52 — shrink 10% and retry
        img = img.resize((round(img.width * 0.9), round(img.height * 0.9)), Image.Resampling.LANCZOS)
    path.write_bytes(data)
    return len(data)


def export_step_image(
    video: Path, t: float, path: Path, *, width: int, max_kb: int, refine: bool
) -> tuple[float, int]:
    t_final = refine_time(video, t) if refine else t
    size = save_compressed(grab(video, t_final), path, width=width, max_kb=max_kb)
    return t_final, size


def image_name(step_no: int, t: float, suffix: str = "") -> str:
    return f"step-{step_no:02d}{suffix}-{fmt_time_file(t)}.jpg"
