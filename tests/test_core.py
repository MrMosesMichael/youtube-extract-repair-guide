import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from repair_guide import manual, transcript
from repair_guide.render import RenderCtx, render_html, render_markdown, reassembly_order, tool_index
from repair_guide.schema import Guide
from repair_guide.source import pick_subtitle, time_link, youtube_id
from repair_guide.torque import display, find_torques, normalize_unit, to_nm
from repair_guide.util import fmt_time, parse_time

FIXTURES = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------ util


@pytest.mark.parametrize(
    "raw,sec",
    [(125, 125.0), ("125", 125.0), ("2:05", 125.0), ("1:02:05", 3725.0), ("2m05s", 125.0), ("4.5", 4.5)],
)
def test_parse_time(raw, sec):
    assert parse_time(raw) == sec


def test_fmt_time():
    assert fmt_time(125.4) == "2:05"
    assert fmt_time(3725) == "1:02:05"


# ------------------------------------------------------------------ torque


@pytest.mark.parametrize(
    "text,canon",
    [
        ("Nm", "N·m"), ("N·m", "N·m"), ("N.m", "N·m"), ("newton meters", "N·m"),
        ("ft-lb", "ft·lb"), ("lb ft", "ft·lb"), ("ft. lbs.", "ft·lb"), ("foot pounds", "ft·lb"), ("lb-ft", "ft·lb"),
        ("inch lbs.", "in·lb"), ("in-lb", "in·lb"), ("lb in", "in·lb"), ("inch pounds", "in·lb"),
        ("N·cm", "N·cm"), ("kgf·m", "kgf·m"),
    ],
)
def test_normalize_unit(text, canon):
    assert normalize_unit(text) == canon


def test_bad_unit():
    with pytest.raises(ValueError):
        normalize_unit("furlongs")


def test_conversions():
    assert to_nm(1, "ft-lb") == pytest.approx(1.35582, rel=1e-4)
    assert to_nm(12, "in lb") == pytest.approx(1.35582, rel=1e-4)
    d = display(175, "ft lb")
    assert d.inline() == "175 ft·lb · 237 N·m"  # heavy spec: no in·lb
    d = display(12, "Nm")
    assert d.inline() == "12 N·m · 8.9 ft·lb · 106 in·lb"  # light spec: in·lb shown
    d = display(2.5, "N·m")
    assert "250 N·cm" in d.inline()  # tiny spec: N·cm shown
    assert display(18, "ft lb", 22).inline().startswith("18–22 ft·lb")


def test_find_torques_manual_style():
    hits = find_torques("Tighten the screw to 12 Nm (106 inch lbs.) .")
    assert [(h.value, h.unit) for h in hits] == [(12, "N·m"), (106, "in·lb")]
    hits = find_torques("Wheel Nut Torque 140 ft lb (190 Nm)")
    assert [(h.value, h.unit) for h in hits] == [(140, "ft·lb"), (190, "N·m")]
    assert find_torques("use an 18 millimeter socket") == []


# ------------------------------------------------------------------ transcript

VTT = """WEBVTT
Kind: captions

00:00:13.000 --> 00:00:15.000 align:start position:0%
take the wheel off use a 22<00:00:14.000><c> millimeter</c>

00:00:15.000 --> 00:00:15.010
take the wheel off use a 22 millimeter

00:00:15.010 --> 00:00:18.000
take the wheel off use a 22 millimeter
socket to take the lug nuts off
"""


def test_parse_vtt_dedupes_rolling_captions():
    segs = transcript.parse_vtt(VTT)
    assert [s["text"] for s in segs] == [
        "take the wheel off use a 22 millimeter",
        "socket to take the lug nuts off",
    ]
    assert segs[0]["start"] == 13.0


def test_transcript_markdown_has_chapters_and_stamps():
    segs = [{"start": 5, "end": 8, "text": "hello."}, {"start": 70, "end": 72, "text": "bye."}]
    md = transcript.to_markdown(segs, [{"title": "Intro", "start": 0}, {"title": "Outro", "start": 60}])
    assert "## Chapter: Intro (0:00)" in md and "## Chapter: Outro (1:00)" in md
    assert "[0:05] hello." in md and "[1:10] bye." in md


# ------------------------------------------------------------------ source


def test_youtube_id_and_links():
    assert youtube_id("https://youtu.be/QdnaKChdDCo?si=x") == "QdnaKChdDCo"
    assert youtube_id("https://www.youtube.com/watch?v=QdnaKChdDCo&t=3") == "QdnaKChdDCo"
    meta = {"kind": "youtube", "id": "QdnaKChdDCo"}
    assert time_link(meta, 125.7) == "https://www.youtube.com/watch?v=QdnaKChdDCo&t=125s"
    local = {"kind": "local", "path": "/tmp/a b.mp4"}
    assert time_link(local, 10).endswith("a%20b.mp4#t=10")


def test_pick_subtitle_prefers_human_then_orig():
    raw = {
        "subtitles": {},
        "automatic_captions": {
            "en": [{"ext": "vtt", "url": "auto-en"}],
            "en-orig": [{"ext": "vtt", "url": "auto-orig"}],
            "en-de": [{"ext": "vtt", "url": "translated"}],
        },
    }
    assert pick_subtitle(raw, ["en"]) == ("auto-orig", "en-orig", True)
    raw["subtitles"] = {"en-US": [{"ext": "vtt", "url": "human"}]}
    assert pick_subtitle(raw, ["en"]) == ("human", "en-US", False)


# ------------------------------------------------------------------ schema + render


def _guide_dict():
    return {
        "title": "Front caliper",
        "summary": "Swap it.",
        "steps": [
            {"title": "Remove caliper bolts", "phase": "removal", "t_start": "0:45", "instructions": ["Remove."],
             "tools": [{"name": "socket", "size": "18mm"}, {"name": "ratchet"}], "torque_refs": ["caliper-bolt"]},
            {"title": "Remove bracket", "phase": "removal", "t_start": 90, "instructions": ["Remove."],
             "tools": [{"name": "socket", "size": "21mm"}], "torque_refs": ["bracket"]},
            {"title": "Install bracket", "phase": "installation", "t_start": 150, "instructions": ["Install."],
             "tools": [{"name": "socket", "size": "21mm"}, {"name": "torque wrench", "detail": "1/2\" drive"}],
             "torque_refs": ["bracket"]},
        ],
        "torque_specs": [
            {"id": "caliper-bolt", "fastener": "Caliper bolts", "value": 108, "unit": "Nm",
             "sources": [{"kind": "manual", "ref": "Brakes > Caliper"}]},
            {"id": "bracket", "fastener": "Bracket bolts", "value": 221, "unit": "lb ft",
             "sources": [{"kind": "web", "url": "https://example.com", "ref": "forum"}]},
            {"id": "hose", "fastener": "Hose bolt"},
        ],
    }


def test_schema_accepts_good_guide():
    g = Guide.model_validate(_guide_dict())
    assert g.steps[0].t_start == 45.0
    assert g.torque_specs[0].unit == "N·m"
    assert g.torque_specs[1].unverified and g.torque_specs[2].missing


def test_schema_rejects_unknown_ref_and_unsourced_value():
    d = _guide_dict()
    d["steps"][0]["torque_refs"] = ["nope"]
    with pytest.raises(ValidationError, match="unknown torque ids"):
        Guide.model_validate(d)
    d = _guide_dict()
    d["torque_specs"][0]["sources"] = []
    with pytest.raises(ValidationError, match="needs at least one source"):
        Guide.model_validate(d)


def test_render_outputs(tmp_path):
    g = Guide.model_validate(_guide_dict())
    ctx = RenderCtx(guide=g, meta={"kind": "youtube", "id": "QdnaKChdDCo", "url": "https://youtu.be/QdnaKChdDCo",
                                    "title": "T", "channel": "C", "duration": 400})
    md = render_markdown(ctx)
    assert "**Caliper bolts** — **108 N·m · 80 ft·lb**" in md
    assert "SPEC NEEDED" in md and "unverified" in md
    assert "[▶ 0:45](https://www.youtube.com/watch?v=QdnaKChdDCo&t=45s)" in md
    # reassembly order: installation-step specs first
    assert [t.id for t in reassembly_order(g)][0] == "bracket"
    # tools merged by name+size, sorted mm ascending
    labels = [lbl for lbl, _ in tool_index(g)]
    assert labels.index("18mm socket") < labels.index("21mm socket")
    assert dict(tool_index(g))["21mm socket"] == [2, 3]
    html = render_html(ctx, tmp_path)
    assert "<html" in html and "Bracket bolts" in html and "<script>" in html


# ------------------------------------------------------------------ manual index


def test_manual_index_and_search(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    entry = manual.build_index("test", FIXTURES / "manual")
    assert entry["pages"] == 1 and entry["torque_lines"] == 2
    cfg = {"manuals": {"test": entry}}
    hits = manual.search_torque("test", cfg, "brake hose bolt")
    assert hits and "40 Nm" in hits[0].line
    assert hits[0].converted.startswith("40 N·m · 30 ft·lb")
    assert "Brakes" in hits[0].crumbs
    pages = manual.search_pages("test", cfg, "caliper replacement")
    assert pages[0].title == "Brake Caliper Replacement - Front"
    text = manual.page_text("test", cfg, pages[0].path)
    assert "Tighten the bolts to 108 Nm" in text
    assert "LEMON" not in text and "nav junk" not in text and "^" not in text


def test_manual_page_outside_root_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    entry = manual.build_index("test", FIXTURES / "manual")
    with pytest.raises(Exception, match="not inside"):
        manual.page_text("test", {"manuals": {"test": entry}}, "/etc/hosts")


def test_schema_json_is_valid():
    json.dumps(Guide.model_json_schema())
