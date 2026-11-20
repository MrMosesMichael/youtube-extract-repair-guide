# repair-guide

Turn an automotive repair video into a guide you can follow in the garage:

- **Every step** has a screenshot of where the work happens (the bolt, the clip, the cable end). Images are 1280 px JPEGs, ~80–150 KB each.
- **Tools per step** list exact sizes ("18mm socket", "T50 Torx"), plus a merged checklist of everything to pull out of the toolbox.
- **Torque specs for reassembly** in N·m, ft·lb, and in·lb for light specs (N·cm for tiny ones). Each spec cites its source: the video at a timestamp, your service manual page, or a web page flagged *unverified*. Missing specs say **SPEC NEEDED** instead of being guessed.
- **▶ timestamp links** on every step and screenshot jump into the video at that moment, so you can watch the bit you need.
- **Pushes straight into Joplin** as a note with the screenshots attached, filed under a per-vehicle notebook and tagged. Re-pushing after an edit updates the same note.
- Also writes **Markdown + images** for Obsidian and the like, plus a **self-contained HTML** file for your phone. The HTML has tick-off checkboxes and dark mode.

It works on YouTube URLs and on **your own footage** (GoPro etc.). Local files without captions are transcribed on-device with Whisper.

```
## 🔩 Torque specs (reassembly order)

- **Caliper bracket bolts (front)** — **300 N·m · 221 ft·lb** · threadlocker: optional, medium
  _service manual (Brakes > Hydraulic System > Brake Caliper > … > Brake Caliper Bracket Replacement - Front) · steps 5, 7_
- **Brake hose to caliper bolt** — **40 N·m · 30 ft·lb · 354 in·lb** · 2 new copper gaskets
- **Caliper bleed screw** — **12 N·m · 8.9 ft·lb · 106 in·lb**

### 5. Remove the caliper bracket
[▶ 1:30–2:04](https://www.youtube.com/watch?v=…&t=90s) · Removal
[screenshot: breaking the bracket bolts loose on the back of the knuckle]
**Tools:** 21mm socket, breaker bar
```

## How it works

Repair videos are messy: auto-captions mishear "21" as "22", torque specs often aren't said at all, and the useful moment is a two-second shot of a socket on a bolt. So the work is split between a CLI and Claude Code:

| Step | Who | What |
|---|---|---|
| `prepare` | CLI | Downloads the video (≤1080p) and captions, or transcribes locally. Finds scene changes plus a regular sweep, drops near-duplicate frames, and builds labelled **contact sheets**. |
| analyse | Claude Code skill | Reads the transcript and every contact sheet. Picks steps, tools and the best frame per step (`peek` zooms in when unsure). Looks up torque specs in the video, then your **service manual index**, then the web. Writes `steps.json`. |
| `build` | CLI | Validates `steps.json`, nudges each frame to the sharpest one within ±1 s, compresses images to the size budget, converts all torque units **in code**, and renders `guide.md` + `guide.html`. |

The model never does arithmetic on torque values, and it can't add a spec without a source; validation rejects it.

## Install

Works on macOS (Intel and Apple Silicon) and Linux. You need [uv](https://docs.astral.sh/uv/) and Node.js or Deno, which yt-dlp uses for YouTube.

```bash
uv tool install 'repair-guide[transcribe] @ git+https://github.com/MrMosesMichael/youtube-extract-repair-guide'
repair-guide doctor
```

No Homebrew ffmpeg is needed: a static ffmpeg ships inside the `imageio-ffmpeg` wheel, and a system `ffmpeg` on your PATH is preferred if one exists. Drop `[transcribe]` if you only use captioned YouTube videos. On Intel Macs the extra pins `onnxruntime<1.20`, the last release with Intel-Mac wheels.

> **zsh / oh-my-zsh note:** keep the single quotes around `'repair-guide[transcribe] @ …'`, otherwise zsh treats `[...]` as a glob and fails with `no matches found`.

Then add the Claude Code plugin, which provides the `/repair-guide` command:

```
/plugin marketplace add MrMosesMichael/youtube-extract-repair-guide
/plugin install repair-guide@repair-guide
```

### Optional: index your service manual

Point it at an extracted HTML manual, for example an [Operation CHARM](https://charm.li) export. Any folder of HTML pages works.

```bash
repair-guide manual add silverado "~/Manuals/2006 Chevrolet Silverado 2500 4WD V8-6.6L DSL Turbo"
#  → 26,807 pages, 3,472 torque lines indexed in ~25 s
repair-guide manual torque silverado "front caliper bracket"
```

The manual is only indexed into `~/.cache/repair-guide/`. Nothing is copied or uploaded.

## Use

In Claude Code:

```
/repair-guide https://www.youtube.com/watch?v=QdnaKChdDCo --vehicle silverado
/repair-guide ~/Footage/GOPR0914.MP4
```

Guides land in `~/RepairGuides/<video-title>/`:

```
guide.md          Markdown with relative image links (Joplin/Obsidian)
guide.html        single file, images embedded, works offline on a phone
images/           step-03-00m50s.jpg …
steps.json        the structured guide (edit it and re-run `build` to tweak)
source.json       video metadata
work/             video, transcript, contact sheets (`repair-guide clean` frees the space)
```

### Joplin

Turn on Joplin's local API once: **Settings › Web Clipper › Enable Web Clipper Service** (no browser extension needed). After that, `/repair-guide` pushes every finished guide automatically, or push by hand:

```bash
repair-guide joplin status                 # API reachable? token OK?
repair-guide joplin push ~/RepairGuides/<guide>
```

- **Notebook:** `Repair Guides › <vehicle>`. With `--vehicle silverado` the sub-notebook is named after the manual folder, so every guide for that truck lands together. Override with `--notebook 'Truck/Steering'`, or change the top level with `repair-guide config --set joplin_notebook=Garage`.
- **Images** become Joplin attachments, so they sync to the phone app. Tapping one opens the video at that moment.
- **Tags:** `repair-guide`, the vehicle, the system (`brakes`, `steering`…), plus `spec-needed` / `unverified-spec` when a torque spec is missing or web-only.
- **Re-push** updates the same note in place, re-uploads only changed images, and deletes the ones no longer used. If you edited the note inside Joplin, it stops and asks for `--force` rather than overwrite your edits. If you moved the note to another notebook, it stays there. Your own tags are never removed.
- **Token:** read from Joplin desktop's own settings. If that fails, `repair-guide joplin auth` asks Joplin for one (click *Grant* in Joplin).

Prefer importing by hand? File › Import › *MD - Markdown (Directory)* on the guide folder works too.

### CLI reference

```
repair-guide prepare <url|file> [--vehicle NAME] [--transcribe] [--whisper-model small.en] [--interval 8]
repair-guide peek <dir> 4:31 4:33            # full-size frames at exact times
repair-guide peek <dir> --range 4:20-4:40    # mini contact sheet across a span
repair-guide build <dir> [--image-width 1280] [--max-kb 150] [--no-refine] [--purge-video]
repair-guide joplin push <dir> [--notebook 'A/B'] [--force]
repair-guide joplin status | auth
repair-guide manual add|list|remove|torque|search|page …
repair-guide torque 175 ft-lb                # → 175 ft·lb · 237 N·m
repair-guide schema                          # JSON Schema for steps.json
repair-guide config [--set output_dir=~/Garage/Guides]
repair-guide clean <dir>                     # delete downloaded video (never your own files)
repair-guide doctor
```

GoPro tip: if a clip's `.LRV` low-res proxy sits next to it, `prepare` analyses the proxy instead. On a 50 s 1080p60 clip that took 7 s instead of a minute, and final screenshots still come from the full-quality MP4.

## Development

```bash
git clone https://github.com/MrMosesMichael/youtube-extract-repair-guide
cd youtube-extract-repair-guide
uv sync --all-extras
uv run pytest
claude --plugin-dir .          # try the skill against your working copy
```

## Roadmap

- Join multi-file GoPro recordings (GOPR/GP01/GP02 chapters) into one guide
- Optional arrow/circle annotations on screenshots
- Multi-video synthesis (merge two videos of the same job)

## Credits

Built with ideas from two open-source projects. Details are in [NOTICE](NOTICE).

- [**claude-code-youtube-extract**](https://github.com/muckybuzzwoo/claude-code-youtube-extract) by Mucky / buzzwoo (Apache-2.0): Claude Code plugin + skill packaging, yt-dlp caption handling, ffmpeg scene-score frame detection, and perceptual de-duplication.
- [**youtube-practice-guide**](https://github.com/LongJingSU/youtube-practice-guide) by LongJingSU (MIT): timestamped contact sheets for model review, "the guide supplements the video" with a timestamp on every step, and self-contained HTML alongside Markdown.

Also standing on [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [Pillow](https://python-pillow.org) and [pydantic](https://docs.pydantic.dev).

## Disclaimer

Generated guides are notes, not a service manual. Torque specs are only as good as their source: check every spec against the factory service information for your exact vehicle, especially anything marked *unverified*. Respect video creators' copyright, and keep the guides you generate for personal use.

## License

MIT. See [LICENSE](LICENSE).
