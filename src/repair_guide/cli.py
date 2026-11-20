"""repair-guide command line.

Typical flow (the Claude Code skill drives this for you):

  repair-guide prepare <youtube-url | video-file> [--vehicle silverado]
  # … write <guide-dir>/steps.json (see `repair-guide schema`) …
  repair-guide build <guide-dir>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from pydantic import ValidationError

from . import __version__, config, frames, manual, source, transcript
from .render import RenderCtx, StepImage, render_html, render_markdown
from .schema import Guide
from .torque import display, normalize_unit
from .util import UserError, fmt_time, info, parse_time, read_json, slugify, write_json

# --------------------------------------------------------------------------- prepare


def cmd_prepare(a: argparse.Namespace, cfg: dict) -> None:
    src = a.source.strip()
    if a.vehicle and a.vehicle not in cfg["manuals"]:
        known = ", ".join(cfg["manuals"]) or "none"
        raise UserError(f"--vehicle '{a.vehicle}' has no manual registered (known: {known}). See `repair-guide manual add`.")

    if source.is_url(src):
        info("Reading video metadata…")
        raw = source.fetch_youtube_metadata(src)
        title, vid = raw.get("title") or "video", raw.get("id") or ""
    else:
        p = Path(src).expanduser()
        title, vid = p.stem, ""
    guide_dir = Path(a.out).expanduser() if a.out else config.output_dir(cfg) / (
        slugify(title) + (f"-{vid}" if vid else "")
    )
    work = guide_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    info(f"Guide folder: {guide_dir}")

    if source.is_url(src):
        meta = source.download_youtube(src, work, a.max_height or cfg["max_height"], a.langs or cfg["caption_langs"])
    else:
        meta = source.local_metadata(Path(src))
    video = Path(meta["video"])
    if not meta.get("duration"):
        meta["duration"] = frames.ffmpeg.probe(video)["duration"]
    meta["vehicle_manual"] = a.vehicle
    write_json(guide_dir / "source.json", meta)

    # transcript
    segs_path = work / "segments.json"
    vtt = work / "subtitles.vtt"
    origin = None
    if a.no_transcript:
        segs, origin = [], "none (--no-transcript)"
    elif vtt.exists() and not a.transcribe:
        segs = transcript.parse_vtt(vtt.read_text(encoding="utf-8"))
        sub = meta.get("subtitle") or {}
        origin = f"captions ({sub.get('lang')}, {'auto-generated' if sub.get('auto') else 'human-made'})"
    elif segs_path.exists() and read_json(segs_path).get("origin", "").startswith("whisper"):
        cached = read_json(segs_path)
        segs, origin = cached["segments"], cached["origin"]
        info("  reusing earlier local transcription")
    else:
        model = a.whisper_model or cfg["whisper_model"]
        segs, origin = transcript.transcribe(video, model), f"whisper ({model}, local)"
    write_json(segs_path, {"origin": origin, "segments": segs})

    header = [
        f"# Transcript — {meta['title']}",
        "",
        f"- Source: {meta.get('url')}",
        f"- Channel: {meta.get('channel') or 'n/a'}",
        f"- Duration: {fmt_time(meta['duration'] or 0)}",
        f"- Transcript from: {origin}",
        "",
    ]
    if meta.get("description"):
        header += ["## Video description (often lists tools, parts and torque specs)", "", meta["description"].strip(), ""]
    if meta.get("chapters"):
        header += ["## Chapters", ""] + [f"- {fmt_time(c['start'] or 0)} {c['title']}" for c in meta["chapters"]] + [""]
    header += ["## Transcript", ""]
    body = transcript.to_markdown(segs, meta.get("chapters") or []) if segs else "_(no transcript)_\n"
    (work / "transcript.md").write_text("\n".join(header) + body, encoding="utf-8")

    # candidate frames + contact sheets
    cands = frames.build_candidates(
        video, work / "sheets", meta["duration"],
        interval=a.interval, scene_threshold=a.scene, max_candidates=a.max_frames,
        proxy=Path(meta["proxy"]) if meta.get("proxy") else None,
    )
    write_json(work / "candidates.json", cands)
    sheets = sorted((work / "sheets").glob("sheet-*.jpg"))

    print(f"""
Prepared: {guide_dir}
  video:      {video}  ({fmt_time(meta['duration'] or 0)})
  transcript: {work / 'transcript.md'}  [{origin}, {len(segs)} lines]
  frames:     {len(cands)} candidates on {len(sheets)} contact sheets in {work / 'sheets'}
  manual:     {a.vehicle or '(none — pass --vehicle NAME to enable manual torque lookups)'}

Next: write {guide_dir / 'steps.json'}  (format: `repair-guide schema`)
      then run: repair-guide build "{guide_dir}"
""".rstrip())


# --------------------------------------------------------------------------- peek


def _guide_dir(p: str) -> tuple[Path, dict]:
    d = Path(p).expanduser().resolve()
    if not (d / "source.json").exists():
        raise UserError(f"{d} isn't a prepared guide folder (no source.json). Run `repair-guide prepare` first.")
    return d, read_json(d / "source.json")


def cmd_peek(a: argparse.Namespace, cfg: dict) -> None:
    d, meta = _guide_dir(a.dir)
    video = Path(meta["video"])
    out = d / "work" / "peek"
    out.mkdir(parents=True, exist_ok=True)
    if a.range:
        lo_s, _, hi_s = a.range.partition("-")
        lo, hi = parse_time(lo_s), parse_time(hi_s)
        if hi <= lo:
            raise UserError("--range must be START-END with END after START, e.g. 4:00-4:30")
        n = max(2, a.count)
        times = [lo + i * (hi - lo) / (n - 1) for i in range(n)]
        imgs = frames.grab_many(video, times, 480)
        chunk = [{"n": i + 1, "time": fmt_time(t), "_img": im} for i, (t, im) in enumerate(zip(times, imgs))]
        path = out / f"range-{fmt_time(lo).replace(':', '')}-{fmt_time(hi).replace(':', '')}.jpg"
        frames._contact_sheet(chunk).save(path, quality=80)
        print(path)
        for c, t in zip(chunk, times):
            print(f"  #{c['n']} = {t:.1f}s ({c['time']})")
        return
    if not a.times:
        raise UserError("Give one or more timestamps, or --range START-END.")
    for ts in a.times:
        t = parse_time(ts)
        img = frames.grab(video, t, a.width)
        path = out / f"peek-{t:07.1f}s.jpg"
        img.save(path, quality=82)
        print(path)


# --------------------------------------------------------------------------- build


def _format_validation(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"])
        lines.append(f"  - {loc or '(root)'}: {e['msg']}")
    return "\n".join(lines)


def _load_guide(d: Path) -> Guide:
    steps_path = d / "steps.json"
    if not steps_path.exists():
        raise UserError(f"No steps.json in {d}. Write it first (format: `repair-guide schema`).")
    try:
        return Guide.model_validate_json(steps_path.read_text(encoding="utf-8"))
    except ValidationError as err:
        raise UserError("steps.json is invalid:\n" + _format_validation(err)) from err


def cmd_build(a: argparse.Namespace, cfg: dict) -> None:
    d, meta = _guide_dir(a.dir)
    guide = _load_guide(d)
    problems = guide.check_times(meta.get("duration"))
    if problems:
        raise UserError("steps.json has timestamps outside the video:\n" + "\n".join(f"  - {p}" for p in problems))

    video = Path(meta["video"])
    if not video.exists():
        raise UserError(f"Video file is gone ({video}). Re-run `repair-guide prepare` to fetch it again.")

    width = a.image_width or cfg["image_width"]
    max_kb = a.max_kb or cfg["image_max_kb"]
    img_dir = d / "images"
    if img_dir.exists():
        shutil.rmtree(img_dir)
    ctx = RenderCtx(guide=guide, meta=meta)
    total = 0
    n_imgs = sum((1 if s.frame_t is not None else 0) + len(s.extra_frames) for s in guide.steps)
    info(f"Exporting {n_imgs} step images ({width}px wide, ≤{max_kb} KB each)…")
    for i, st in enumerate(guide.steps):
        wanted = ([st.frame_t] if st.frame_t is not None else []) + list(st.extra_frames)
        for k, t in enumerate(wanted):
            suffix = "" if k == 0 else chr(ord("a") + k)
            name = frames.image_name(i + 1, t, suffix)
            t_final, size = frames.export_step_image(
                video, t, img_dir / name, width=width, max_kb=max_kb, refine=not a.no_refine
            )
            ctx.images.setdefault(i, []).append(StepImage(f"images/{name}", t_final, size))
            total += size

    (d / "guide.md").write_text(render_markdown(ctx), encoding="utf-8")
    (d / "guide.html").write_text(render_html(ctx, d), encoding="utf-8")

    missing = [t.fastener for t in guide.torque_specs if t.missing]
    unverified = [t.fastener for t in guide.torque_specs if t.unverified]
    html_kb = (d / "guide.html").stat().st_size / 1024
    print(f"""
Built: {d}
  guide.md    — Markdown + images/ (push to Joplin: `repair-guide joplin push <dir>`)
  guide.html  — single self-contained file ({html_kb:.0f} KB), good for phone/AirDrop
  {len(guide.steps)} steps · {n_imgs} images · {total / 1024:.0f} KB total images""".rstrip())
    if missing:
        print(f"  ⚠ SPEC NEEDED: {', '.join(missing)}")
    if unverified:
        print(f"  ⚠ web-only (unverified) specs: {', '.join(unverified)}")

    if a.purge_video:
        _purge(d, meta)


# --------------------------------------------------------------------------- clean


def _purge(d: Path, meta: dict) -> None:
    work = d / "work"
    removed = 0
    if meta.get("kind") == "youtube":
        for f in work.glob("video.*"):
            removed += f.stat().st_size
            f.unlink()
    for f in [work / "audio.wav", *work.glob("peek/*")]:
        if f.exists():
            removed += f.stat().st_size
            f.unlink()
    info(f"Removed {removed / 1e6:.0f} MB of working files (your own local video files are never deleted).")


def cmd_clean(a: argparse.Namespace, cfg: dict) -> None:
    d, meta = _guide_dir(a.dir)
    _purge(d, meta)


# --------------------------------------------------------------------------- manual


def cmd_manual(a: argparse.Namespace, cfg: dict) -> None:
    if a.manual_cmd == "add":
        info(f"Indexing '{a.name}' from {a.path} (one-time; large manuals take a minute or two)…")
        entry = manual.build_index(a.name, Path(a.path))
        cfg["manuals"][a.name] = entry
        config.save(cfg)
        print(f"Manual '{a.name}' ready: {entry['pages']} pages, {entry['torque_lines']} torque lines.")
    elif a.manual_cmd == "list":
        if not cfg["manuals"]:
            print("No manuals yet. Add one: repair-guide manual add <name> <folder>")
        for name, m in cfg["manuals"].items():
            print(f"{name}: {m['path']}  ({m.get('pages', '?')} pages, {m.get('torque_lines', '?')} torque lines)")
    elif a.manual_cmd == "remove":
        m = cfg["manuals"].pop(a.name, None)
        if not m:
            raise UserError(f"No manual named '{a.name}'.")
        Path(m["index"]).unlink(missing_ok=True)
        config.save(cfg)
        print(f"Removed '{a.name}' (index deleted; the manual folder itself is untouched).")
    elif a.manual_cmd == "torque":
        hits = manual.search_torque(a.name, cfg, a.query, a.limit)
        if a.json:
            print(json.dumps([h.__dict__ for h in hits], indent=2))
            return
        if not hits:
            print("No torque lines matched. Try fewer/different words, or `manual search` for the procedure page.")
        for n, h in enumerate(hits, 1):
            print(f"[{n}] {h.converted}\n    “{h.line}”")
            if h.context:
                print(f"    context: {h.context}")
            print(f"    page: {h.crumbs}")
            print(f"    file: {h.path}" + (f"  (+{h.also_on} more pages say the same)" if h.also_on else ""))
    elif a.manual_cmd == "search":
        hits = manual.search_pages(a.name, cfg, a.query, a.limit)
        if a.json:
            print(json.dumps([h.__dict__ for h in hits], indent=2))
            return
        if not hits:
            print("No pages matched.")
        for n, h in enumerate(hits, 1):
            print(f"[{n}] {h.title}\n    {h.crumbs}\n    {h.snippet}\n    file: {h.path}")
            for tl in h.torque_lines:
                print(f"      🔩 {tl}")
    elif a.manual_cmd == "page":
        print(manual.page_text(a.name, cfg, a.path))


# --------------------------------------------------------------------------- misc


def cmd_torque(a: argparse.Namespace, cfg: dict) -> None:
    value = a.value
    hi = None
    if "-" in value.strip("-"):
        lo_s, _, hi_s = value.partition("-")
        value, hi = lo_s, float(hi_s)
    d = display(float(value), " ".join(a.unit), hi)
    print(d.inline())


def cmd_schema(a: argparse.Namespace, cfg: dict) -> None:
    print(json.dumps(Guide.model_json_schema(), indent=2))


def cmd_doctor(a: argparse.Namespace, cfg: dict) -> None:
    import importlib.util

    import yt_dlp

    ok = True
    try:
        exe = frames.ffmpeg.ffmpeg_exe()
        print(f"✓ ffmpeg: {exe}")
    except UserError as e:
        ok = False
        print(f"✗ ffmpeg: {e}")
    print(f"✓ yt-dlp {yt_dlp.version.__version__}")
    js = shutil.which("deno") or shutil.which("node")
    print(f"{'✓' if js else '✗'} JS runtime for YouTube: {js or 'none — install Node or Deno'}")
    ok &= bool(js)
    fw = importlib.util.find_spec("faster_whisper") is not None
    print(f"{'✓' if fw else '·'} local transcription (faster-whisper): {'installed' if fw else 'not installed (optional, for videos without captions)'}")
    print(f"· config: {config.config_path()}")
    print(f"· guides go to: {config.output_dir(cfg)}")
    for name, m in cfg["manuals"].items():
        exists = Path(m["index"]).exists()
        print(f"{'✓' if exists else '✗'} manual '{name}': {m['path']}")
    from . import joplin

    try:
        joplin.connect(cfg)
        print("✓ Joplin push: ready")
    except UserError as e:
        print(f"· Joplin push: not available ({str(e).split('. ')[0]})")
    sys.exit(0 if ok else 1)


def cmd_config(a: argparse.Namespace, cfg: dict) -> None:
    if a.set:
        key, _, val = a.set.partition("=")
        if key not in config.DEFAULTS or key == "manuals":
            raise UserError(f"Unknown/unsettable key '{key}'. Settable: {', '.join(k for k in config.DEFAULTS if k != 'manuals')}")
        cur = config.DEFAULTS[key]
        cfg[key] = int(val) if isinstance(cur, int) else val.split(",") if isinstance(cur, list) else val
        config.save(cfg)
    shown = {k: ("<set>" if k == "joplin_token" and v else v) for k, v in cfg.items()}
    print(json.dumps(shown, indent=2))
    print(f"# {config.config_path()}", file=sys.stderr)


# --------------------------------------------------------------------------- joplin


def cmd_joplin(a: argparse.Namespace, cfg: dict) -> None:
    from . import joplin

    if a.joplin_cmd == "status":
        base = joplin.find_server(cfg)
        print(f"✓ Joplin Data API at {base}")
        client = joplin.connect(cfg)
        n = len(client.get_all("/folders", "id"))
        print(f"✓ token accepted ({n} notebooks)")
        return
    if a.joplin_cmd == "auth":
        token = joplin.authorize(cfg)
        cfg["joplin_token"] = token
        config.save(cfg)
        print(f"✓ token saved to {config.config_path()}")
        return

    d, meta = _guide_dir(a.dir)
    guide = _load_guide(d)
    try:
        client = joplin.connect(cfg)
    except joplin.JoplinUnavailable as e:
        if a.if_available:
            print(f"Joplin push skipped: {e}")
            return
        raise
    try:
        r = joplin.push(client, d, guide, meta, cfg, notebook=a.notebook, force=a.force)
    except joplin.EditedInJoplin as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(3)
    verb = "Created" if r.created else "Updated"
    print(f"""{verb} Joplin note: {guide.title}
  notebook: {' › '.join(r.folder)}{'' if r.created or a.notebook else ' (or wherever you moved it)'}
  images:   {r.uploaded} uploaded, {r.reused} unchanged, {r.deleted} old removed
  tags:     {', '.join(r.tags)}
  open:     joplin://x-callback-url/openNote?id={r.note_id}""")


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="repair-guide", description="Turn repair videos into step-by-step guides.")
    p.add_argument("--version", action="version", version=f"repair-guide {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("prepare", help="Download/inspect a video; write transcript and frame contact sheets.")
    s.add_argument("source", help="YouTube URL or path to a local video file")
    s.add_argument("--out", help="Guide folder (default: <output_dir>/<title-slug>)")
    s.add_argument("--vehicle", help="Registered manual name for torque lookups (see `manual add`)")
    s.add_argument("--max-height", type=int, help="Max download resolution (default 1080)")
    s.add_argument("--langs", nargs="+", help="Caption language preference (default: en)")
    s.add_argument("--transcribe", action="store_true", help="Ignore captions; transcribe locally with whisper")
    s.add_argument("--no-transcript", action="store_true", help="Skip transcripts entirely (visual only)")
    s.add_argument("--whisper-model", help="faster-whisper model (default base.en; small.en is better but slower)")
    s.add_argument("--interval", type=float, default=8.0, help="Seconds between sweep frames (default 8)")
    s.add_argument("--scene", type=float, default=0.30, help="Scene-change threshold 0–1 (default 0.30)")
    s.add_argument("--max-frames", type=int, default=240, help="Max candidate frames (default 240)")
    s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("peek", help="Grab frames at exact times (or a range sheet) to choose the best screenshot.")
    s.add_argument("dir")
    s.add_argument("times", nargs="*", help="Timestamps like 4:12 or 252.5")
    s.add_argument("--range", help="START-END, e.g. 4:00-4:30 → contact sheet of --count frames")
    s.add_argument("--count", type=int, default=8)
    s.add_argument("--width", type=int, default=960)
    s.set_defaults(func=cmd_peek)

    s = sub.add_parser("build", help="Validate steps.json, export step images, render guide.md + guide.html.")
    s.add_argument("dir")
    s.add_argument("--image-width", type=int, help="Image width in px (default 1280)")
    s.add_argument("--max-kb", type=int, help="Per-image size budget in KB (default 150)")
    s.add_argument("--no-refine", action="store_true", help="Use exact frame_t instead of the sharpest nearby frame")
    s.add_argument("--purge-video", action="store_true", help="Delete the downloaded video afterwards")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("clean", help="Delete downloaded video/audio for a guide (keeps the guide).")
    s.add_argument("dir")
    s.set_defaults(func=cmd_clean)

    m = sub.add_parser("manual", help="Offline service manuals: add, list, search, torque, page, remove.")
    msub = m.add_subparsers(dest="manual_cmd", required=True)
    x = msub.add_parser("add", help="Index a manual folder (e.g. an extracted charm.li export)")
    x.add_argument("name", help="Short name, e.g. silverado")
    x.add_argument("path")
    msub.add_parser("list")
    x = msub.add_parser("remove")
    x.add_argument("name")
    for nm, hp in (("torque", "Find torque specs, e.g. 'caliper bracket'"), ("search", "Find procedure pages")):
        x = msub.add_parser(nm, help=hp)
        x.add_argument("name")
        x.add_argument("query")
        x.add_argument("--limit", type=int, default=8)
        x.add_argument("--json", action="store_true")
    x = msub.add_parser("page", help="Print a manual page as plain text")
    x.add_argument("name")
    x.add_argument("path")
    m.set_defaults(func=cmd_manual)

    j = sub.add_parser("joplin", help="Push guides into Joplin (needs its Web Clipper service enabled).")
    jsub = j.add_subparsers(dest="joplin_cmd", required=True)
    x = jsub.add_parser("push", help="Create or update the Joplin note for a guide (images become attachments)")
    x.add_argument("dir")
    x.add_argument("--notebook", help="Notebook path like 'Truck/Steering' (default: Repair Guides › <vehicle>)")
    x.add_argument("--force", action="store_true", help="Overwrite edits made to the note inside Joplin")
    x.add_argument("--if-available", action="store_true", help="Exit quietly (0) if Joplin isn't running")
    jsub.add_parser("status", help="Check that Joplin's API is reachable and the token works")
    jsub.add_parser("auth", help="Get a new API token (click Grant in Joplin)")
    j.set_defaults(func=cmd_joplin)

    s = sub.add_parser("torque", help="Convert a torque value, e.g. `repair-guide torque 175 ft-lb`")
    s.add_argument("value", help="Number or range like 18-22")
    s.add_argument("unit", nargs="+")
    s.set_defaults(func=cmd_torque)

    s = sub.add_parser("schema", help="Print the steps.json JSON Schema")
    s.set_defaults(func=cmd_schema)
    s = sub.add_parser("doctor", help="Check dependencies and config")
    s.set_defaults(func=cmd_doctor)
    s = sub.add_parser("config", help="Show config, or --set key=value")
    s.add_argument("--set")
    s.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        cfg = config.load()
        if getattr(args, "unit", None):
            normalize_unit(" ".join(args.unit))
        args.func(args, cfg)
    except UserError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
