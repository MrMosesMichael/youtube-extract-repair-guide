"""Render a validated Guide into guide.md (Joplin/Obsidian friendly) and guide.html (self-contained)."""

from __future__ import annotations

import base64
import datetime as dt
import html
import re
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Guide, Source, Step, TorqueSpec
from .source import time_link
from .torque import display
from .util import fmt_time

PHASE_LABEL = {
    "prep": "Prep", "removal": "Removal", "inspection": "Inspection", "installation": "Install",
    "adjustment": "Adjust", "test": "Test", "other": "",
}


@dataclass
class StepImage:
    file: str  # relative to guide dir, e.g. images/step-03-04m15s.jpg
    t: float
    bytes: int


@dataclass
class RenderCtx:
    guide: Guide
    meta: dict
    images: dict[int, list[StepImage]] = field(default_factory=dict)  # step index (0-based) -> images

    def link(self, t: float) -> str | None:
        return time_link(self.meta, t)


# ------------------------------------------------------------------ shared derivations


def torque_text(ts: TorqueSpec) -> str:
    if ts.value is not None and ts.unit:
        s = display(ts.value, ts.unit, ts.value_max).inline()
        if ts.angle_deg:
            s += f" + {ts.angle_deg:g}°"
        return s
    if ts.angle_deg:
        return f"+{ts.angle_deg:g}° (angle only)"
    return "SPEC NEEDED"


def torque_flags(ts: TorqueSpec) -> list[str]:
    flags = []
    if ts.one_time_use:
        flags.append("replace — one-time-use")
    if ts.threadlocker:
        flags.append(f"threadlocker: {ts.threadlocker}")
    if ts.sequence:
        flags.append(ts.sequence)
    if ts.notes:
        flags.append(ts.notes)
    return flags


def steps_using(guide: Guide, spec_id: str) -> list[int]:
    return [i + 1 for i, s in enumerate(guide.steps) if spec_id in s.torque_refs]


def reassembly_order(guide: Guide) -> list[TorqueSpec]:
    """Order specs the way you'll meet them going back together: by installation steps
    if the video shows them, otherwise reverse of the removal order."""
    by_id = {t.id: t for t in guide.torque_specs}
    order: list[str] = []
    install = [s for s in guide.steps if s.phase in ("installation", "adjustment")]
    for s in install:
        order += [r for r in s.torque_refs if r not in order]
    for s in reversed(guide.steps):
        order += [r for r in s.torque_refs if r not in order]
    order += [t.id for t in guide.torque_specs if t.id not in order]
    return [by_id[i] for i in order]


def tool_index(guide: Guide) -> list[tuple[str, list[int]]]:
    """Unique tools (merged by name + size) with the steps they're used in."""
    found: dict[tuple, dict] = {}
    for i, st in enumerate(guide.steps, 1):
        for tl in st.tools:
            key = (tl.name.lower(), (tl.size or "").lower())
            e = found.setdefault(key, {"tool": tl, "details": [], "inferred": False, "steps": []})
            if tl.detail and tl.detail not in e["details"]:
                e["details"].append(tl.detail)
            e["inferred"] |= tl.size_certainty == "inferred"
            if i not in e["steps"]:
                e["steps"].append(i)
    rows = []
    for e in sorted(found.values(), key=lambda e: (e["tool"].name.lower(), _size_key(e["tool"].size))):
        tl = e["tool"]
        label = " ".join(b for b in (tl.size, tl.name) if b)
        if e["details"]:
            label += f" ({'; '.join(e['details'])})"
        if e["inferred"]:
            label += " †"
        rows.append((label, e["steps"]))
    return rows


def plural_steps(nums: list[int]) -> str:
    return ("step " if len(nums) == 1 else "steps ") + ", ".join(map(str, nums))


def _size_key(size: str | None) -> tuple:
    if not size:
        return (2, 0.0, "")
    m = re.match(r"(\d+(?:\.\d+)?)\s*mm", size)
    if m:
        return (0, float(m.group(1)), size)
    m = re.match(r"(\d+)/(\d+)", size)
    if m:
        return (1, int(m.group(1)) / int(m.group(2)), size)
    return (1, 99.0, size)


def any_inferred(guide: Guide) -> bool:
    return any(t.size_certainty == "inferred" for s in guide.steps for t in s.tools)


def fmt_duration(sec: float | None) -> str:
    return fmt_time(sec) if sec else "?"


# ------------------------------------------------------------------ markdown


def _md_source(ctx: RenderCtx, s: Source) -> str:
    if s.kind == "video" and s.t is not None:
        link = ctx.link(s.t)
        return f"[video ▶ {fmt_time(s.t)}]({link})" if link else f"video {fmt_time(s.t)}"
    if s.kind == "manual":
        return f"service manual ({s.ref})"
    if s.kind == "web":
        return f"[⚠️ web, unverified]({s.url})"
    return "entered by you"


def render_markdown(ctx: RenderCtx) -> str:
    g, meta = ctx.guide, ctx.meta
    out: list[str] = [f"# {g.title}", ""]

    facts = []
    if g.vehicle:
        facts.append(f"**Vehicle:** {g.vehicle}")
    if g.difficulty:
        facts.append(f"**Difficulty:** {g.difficulty}")
    if g.est_time:
        facts.append(f"**Time:** {g.est_time}")
    if facts:
        out += [" · ".join(facts), ""]
    if g.applies_to:
        out += [f"**Also applies to:** {', '.join(g.applies_to)}", ""]
    vid = f"[{meta.get('title')}]({meta.get('url')})" if meta.get("kind") != "local" else f"`{meta.get('path')}`"
    by = f" — {meta['channel']}" if meta.get("channel") else ""
    out += [f"**Video:** {vid}{by} ({fmt_duration(meta.get('duration'))})", ""]
    out += [g.summary, ""]

    if g.safety:
        out += ["## ⚠️ Safety", ""] + [f"- {s}" for s in g.safety] + [""]

    tools = tool_index(g)
    if tools:
        out += ["## 🔧 Tools", ""]
        out += [f"- [ ] {label} — {plural_steps(st)}" for label, st in tools]
        if any_inferred(g):
            out += ["", "_† size inferred (not shown or said in the video) — double-check._"]
        out.append("")

    if g.parts or g.consumables:
        out += ["## 📦 Parts & consumables", ""]
        for p in g.parts:
            bits = [p.name + (f" ({p.qty})" if p.qty else "")]
            if p.part_number:
                bits.append(f"(P/N {p.part_number})")
            if p.notes:
                bits.append(f"— {p.notes}")
            out.append("- [ ] " + " ".join(bits))
        out += [f"- [ ] {c}" for c in g.consumables]
        out.append("")

    if g.torque_specs:
        out += ["## 🔩 Torque specs (reassembly order)", ""]
        for ts in reassembly_order(g):
            spec = torque_text(ts)
            mark = "⚠️ " if ts.missing else ("⚠️ " if ts.unverified else "")
            line = f"- {mark}**{ts.fastener}** — **{spec}**"
            flags = torque_flags(ts)
            if flags:
                line += " · " + " · ".join(flags)
            used = steps_using(g, ts.id)
            src = ", ".join(_md_source(ctx, s) for s in ts.sources) or "no source — look it up before reassembly"
            line += f"  \n  _{src}{' · ' + plural_steps(used) if used else ''}_"
            out.append(line)
        out.append("")

    out += ["## Steps", ""]
    for i, st in enumerate(g.steps):
        out += _md_step(ctx, i, st)

    if g.reassembly_notes:
        out += ["## Reassembly notes", ""] + [f"- {n}" for n in g.reassembly_notes] + [""]

    out += [
        "---",
        f"_Generated {dt.date.today().isoformat()} with "
        "[repair-guide](https://github.com/MrMosesMichael/youtube-extract-repair-guide). "
        "Screenshots and ▶ links jump to that moment in the video. Always confirm torque specs "
        "against the service manual for your exact vehicle._",
        "",
    ]
    return "\n".join(out)


def _md_step(ctx: RenderCtx, i: int, st: Step) -> list[str]:
    n = i + 1
    out = [f"### {n}. {st.title}", ""]
    link = ctx.link(st.t_start)
    rng = fmt_time(st.t_start) + (f"–{fmt_time(st.t_end)}" if st.t_end else "")
    phase = PHASE_LABEL.get(st.phase, "")
    head = f"[▶ {rng}]({link})" if link else f"⏱ {rng}"
    out += [head + (f" · {phase}" if phase else ""), ""]
    for im in ctx.images.get(i, []):
        alt = (st.caption or st.title).replace("[", "(").replace("]", ")")
        ilink = ctx.link(max(0, im.t - 3))
        img = f"![{alt}]({im.file})"
        out.append(f"[{img}]({ilink})" if ilink else img)
    if st.caption and ctx.images.get(i):
        out += [f"*{st.caption}*"]
    if ctx.images.get(i):
        out.append("")
    if st.tools:
        out += ["**Tools:** " + ", ".join(t.label() for t in st.tools), ""]
    out += [f"- {x}" for x in st.instructions]
    out.append("")
    for w in st.warnings:
        out += [f"> ⚠️ **Warning:** {w}", ""]
    for nt in st.notes:
        out += [f"> 💡 {nt}", ""]
    specs = {t.id: t for t in ctx.guide.torque_specs}
    if st.torque_refs:
        out.append("**Torque on reinstall:**")
        for r in st.torque_refs:
            ts = specs[r]
            out.append(f"- {ts.fastener}: **{torque_text(ts)}**" + (" ⚠️ unverified" if ts.unverified else ""))
        out.append("")
    return out


# ------------------------------------------------------------------ html

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--card:#fff;--line:#e4e1d8;--accent:#c2410c;--warn-bg:#fff4e5;--warn:#9a3412;--note-bg:#eef6ff;--link:#1d4ed8;--chip:#f1efe8}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ecebe6;--muted:#a3a29b;--card:#1f1f1d;--line:#34332f;--accent:#fb923c;--warn-bg:#3a2410;--warn:#fdba74;--note-bg:#15263d;--link:#93c5fd;--chip:#2a2a27}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:780px;margin:0 auto;padding:16px 16px 64px}
h1{font-size:1.6rem;line-height:1.25;margin:.4em 0 .3em}h2{font-size:1.2rem;margin:1.8em 0 .6em;border-bottom:2px solid var(--line);padding-bottom:.25em}
a{color:var(--link)}.muted{color:var(--muted)}.facts{display:flex;flex-wrap:wrap;gap:6px;margin:.6em 0}
.chip{background:var(--chip);border-radius:999px;padding:2px 10px;font-size:.85rem}
ul.check{list-style:none;padding:0}ul.check li{padding:4px 0;border-bottom:1px solid var(--line)}
ul.check label{display:flex;gap:10px;align-items:flex-start;cursor:pointer}ul.check input{margin-top:5px;width:18px;height:18px;flex:none}
.tq{width:100%;border-collapse:collapse;font-size:.92rem}.tq th,.tq td{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.tq th{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.tq .v{font-weight:700;white-space:nowrap}
.tq small{display:block;color:var(--muted)}.tablewrap{overflow-x:auto}
.step{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}
.step h3{margin:0 0 4px;font-size:1.08rem;display:flex;gap:10px;align-items:flex-start}
.step h3 input{width:20px;height:20px;margin-top:2px;flex:none}.step.done{opacity:.55}
.step img{width:100%;height:auto;border-radius:8px;display:block;margin:10px 0 4px}
.cap{font-size:.88rem;color:var(--muted);margin:0 0 8px}.tools{font-size:.92rem;margin:8px 0}
.warn{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 10px;margin:8px 0;font-size:.93rem}
.note{background:var(--note-bg);border-radius:8px;padding:8px 10px;margin:8px 0;font-size:.93rem}
.torque{border-left:4px solid var(--accent);padding:4px 10px;margin:10px 0;font-size:.93rem}
.missing{color:var(--warn);font-weight:700}footer{margin-top:40px;font-size:.82rem;color:var(--muted)}
ul,ol{padding-left:1.3em}
"""

JS = """
(function(){var k='rg:'+location.pathname+':';function st(){try{return window.localStorage}catch(e){return null}}
document.querySelectorAll('input[data-k]').forEach(function(b){var s=st();try{if(s&&s.getItem(k+b.dataset.k)==='1'){b.checked=true}}catch(e){}
var card=b.closest('.step');if(card&&b.checked)card.classList.add('done');
b.addEventListener('change',function(){var s=st();try{s&&s.setItem(k+b.dataset.k,b.checked?'1':'0')}catch(e){}if(card)card.classList.toggle('done',b.checked)})})})();
"""


def _e(s: str | None) -> str:
    return html.escape(s or "", quote=True)


def _a(href: str | None, text: str) -> str:
    return f'<a href="{_e(href)}" target="_blank" rel="noopener">{text}</a>' if href else text


def _html_source(ctx: RenderCtx, s: Source) -> str:
    if s.kind == "video" and s.t is not None:
        return _a(ctx.link(s.t), f"video ▶ {fmt_time(s.t)}")
    if s.kind == "manual":
        return f"service manual — {_e(s.ref)}"
    if s.kind == "web":
        return '<span class="missing">⚠ unverified</span> ' + _a(s.url, _e(s.ref or "web source"))
    return "entered by you"


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def render_html(ctx: RenderCtx, guide_dir: Path) -> str:
    g, meta = ctx.guide, ctx.meta
    h: list[str] = []
    h.append(f"<h1>{_e(g.title)}</h1>")
    chips = [x for x in (g.vehicle, g.difficulty and f"Difficulty: {g.difficulty}", g.est_time and f"Time: {g.est_time}") if x]
    if chips:
        h.append('<div class="facts">' + "".join(f'<span class="chip">{_e(c)}</span>' for c in chips) + "</div>")
    if g.applies_to:
        h.append(f'<p class="muted">Also applies to: {_e(", ".join(g.applies_to))}</p>')
    by = f" — {_e(meta['channel'])}" if meta.get("channel") else ""
    h.append(f'<p class="muted">Video: {_a(meta.get("url"), _e(meta.get("title")))}{by} ({fmt_duration(meta.get("duration"))})</p>')
    h.append(f"<p>{_e(g.summary)}</p>")

    if g.safety:
        h.append("<h2>⚠️ Safety</h2><ul>" + "".join(f"<li>{_e(s)}</li>" for s in g.safety) + "</ul>")

    tools = tool_index(g)
    if tools:
        h.append('<h2>🔧 Tools</h2><ul class="check">')
        for n, (label, st) in enumerate(tools):
            h.append(
                f'<li><label><input type="checkbox" data-k="tool{n}"><span>{_e(label)} '
                f'<span class="muted">— {plural_steps(st)}</span></span></label></li>'
            )
        h.append("</ul>")
        if any_inferred(g):
            h.append('<p class="muted">† size inferred (not shown or said in the video) — double-check.</p>')

    if g.parts or g.consumables:
        h.append('<h2>📦 Parts &amp; consumables</h2><ul class="check">')
        n = 0
        for p in g.parts:
            txt = p.name + (f" ({p.qty})" if p.qty else "") + (f" (P/N {p.part_number})" if p.part_number else "")
            if p.notes:
                txt += f" — {p.notes}"
            h.append(f'<li><label><input type="checkbox" data-k="part{n}"><span>{_e(txt)}</span></label></li>')
            n += 1
        for c in g.consumables:
            h.append(f'<li><label><input type="checkbox" data-k="part{n}"><span>{_e(c)}</span></label></li>')
            n += 1
        h.append("</ul>")

    if g.torque_specs:
        h.append('<h2>🔩 Torque specs <span class="muted" style="font-weight:400;font-size:.8em">(reassembly order)</span></h2>')
        h.append('<div class="tablewrap"><table class="tq"><tr><th>Fastener</th><th>Spec</th><th>Source</th></tr>')
        for ts in reassembly_order(g):
            spec = torque_text(ts)
            v = f'<span class="missing">{_e(spec)}</span>' if ts.missing else _e(spec)
            if ts.unverified:
                v += ' <span class="missing">⚠</span>'
            flags = "".join(f"<small>{_e(f)}</small>" for f in torque_flags(ts))
            used = steps_using(g, ts.id)
            src = "<br>".join(_html_source(ctx, s) for s in ts.sources) or '<span class="missing">none — look it up</span>'
            if used:
                src += f"<small>{plural_steps(used)}</small>"
            h.append(f'<tr><td>{_e(ts.fastener)}{flags}</td><td class="v">{v}</td><td>{src}</td></tr>')
        h.append("</table></div>")

    h.append("<h2>Steps</h2>")
    specs = {t.id: t for t in g.torque_specs}
    for i, st in enumerate(g.steps):
        n = i + 1
        rng = fmt_time(st.t_start) + (f"–{fmt_time(st.t_end)}" if st.t_end else "")
        phase = PHASE_LABEL.get(st.phase, "")
        h.append(f'<section class="step" id="step-{n}">')
        h.append(f'<h3><input type="checkbox" data-k="step{n}" aria-label="done"><span>{n}. {_e(st.title)}</span></h3>')
        h.append(f'<div class="muted">{_a(ctx.link(st.t_start), "▶ " + rng)}{" · " + phase if phase else ""}</div>')
        for im in ctx.images.get(i, []):
            tag = f'<img src="{_data_uri(guide_dir / im.file)}" alt="{_e(st.caption or st.title)}" loading="lazy">'
            h.append(_a(ctx.link(max(0, im.t - 3)), tag))
        if st.caption and ctx.images.get(i):
            h.append(f'<p class="cap">{_e(st.caption)}</p>')
        if st.tools:
            h.append('<div class="tools"><b>Tools:</b> ' + _e(", ".join(t.label() for t in st.tools)) + "</div>")
        h.append("<ul>" + "".join(f"<li>{_e(x)}</li>" for x in st.instructions) + "</ul>")
        for w in st.warnings:
            h.append(f'<div class="warn">⚠️ <b>Warning:</b> {_e(w)}</div>')
        for nt in st.notes:
            h.append(f'<div class="note">💡 {_e(nt)}</div>')
        if st.torque_refs:
            rows = []
            for r in st.torque_refs:
                ts = specs[r]
                cls = ' class="missing"' if ts.missing or ts.unverified else ""
                rows.append(f"{_e(ts.fastener)}: <b{cls}>{_e(torque_text(ts))}</b>{' ⚠ unverified' if ts.unverified else ''}")
            h.append('<div class="torque"><b>Torque on reinstall</b><br>' + "<br>".join(rows) + "</div>")
        h.append("</section>")

    if g.reassembly_notes:
        h.append("<h2>Reassembly notes</h2><ul>" + "".join(f"<li>{_e(x)}</li>" for x in g.reassembly_notes) + "</ul>")

    h.append(
        f"<footer>Generated {dt.date.today().isoformat()} with "
        '<a href="https://github.com/MrMosesMichael/youtube-extract-repair-guide">repair-guide</a>. '
        "Tap a screenshot or ▶ link to jump to that moment in the video. Always confirm torque specs "
        "against the service manual for your exact vehicle.</footer>"
    )
    body = "\n".join(h)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(g.title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main><script>{JS}</script></body></html>\n"
    )
