"""Joplin push against an in-memory fake of Joplin's Data API."""

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from repair_guide import joplin
from repair_guide.schema import Guide

TOKEN = "t0ken"


class FakeJoplin:
    def __init__(self):
        self.folders, self.notes, self.resources, self.tags = {}, {}, {}, {}
        self.note_tags: set[tuple[str, str]] = set()

    @staticmethod
    def nid():
        return uuid.uuid4().hex


def make_handler(db: FakeJoplin):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _out(self, code, obj=None):
            raw = b"" if obj is None else (obj if isinstance(obj, bytes) else json.dumps(obj).encode())
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def _route(self, method):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path.rstrip("/").split("/")[1:]
            if p == ["ping"]:
                return self._out(200, b"JoplinClipperServer")
            if q.get("token") != TOKEN:
                return self._out(403, {"error": "Invalid token"})
            body = self._body()
            data = json.loads(body) if body and self.headers.get("Content-Type", "").startswith("application/json") else {}

            def page(items):
                return {"items": items, "has_more": False}

            if p == ["folders"]:
                if method == "GET":
                    return self._out(200, page(list(db.folders.values())))
                f = {"id": db.nid(), "title": data["title"], "parent_id": data.get("parent_id", "")}
                db.folders[f["id"]] = f
                return self._out(200, f)
            if p == ["resources"] and method == "POST":
                m = re.search(rb'name="props"\r\n\r\n(.*?)\r\n--', body, re.S)
                r = {"id": db.nid(), "title": json.loads(m.group(1))["title"], "size": len(body)}
                db.resources[r["id"]] = r
                return self._out(200, r)
            if len(p) == 2 and p[0] == "resources" and method == "DELETE":
                return self._out(200 if db.resources.pop(p[1], None) else 404)
            if p == ["notes"] and method == "POST":
                n = {"id": db.nid(), "deleted_time": 0, **data}
                db.notes[n["id"]] = n
                return self._out(200, n)
            if len(p) >= 2 and p[0] == "notes":
                n = db.notes.get(p[1])
                if n is None:
                    return self._out(404, {"error": "not found"})
                if len(p) == 3 and p[2] == "tags":
                    return self._out(200, page([db.tags[t] for t, x in db.note_tags if x == n["id"]]))
                if method == "GET":
                    return self._out(200, n)
                if method == "PUT":
                    n.update(data)
                    return self._out(200, n)
            if p == ["tags"]:
                if method == "GET":
                    return self._out(200, page(list(db.tags.values())))
                t = {"id": db.nid(), "title": data["title"].lower()}
                db.tags[t["id"]] = t
                return self._out(200, t)
            if len(p) == 3 and p[0] == "tags" and p[2] == "notes" and method == "POST":
                db.note_tags.add((p[1], data["id"]))
                return self._out(200, {})
            if len(p) == 4 and p[0] == "tags" and method == "DELETE":
                db.note_tags.discard((p[1], p[3]))
                return self._out(200)
            return self._out(404, {"error": f"no route {method} {u.path}"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_DELETE(self):
            self._route("DELETE")

    return H


@pytest.fixture
def server():
    db = FakeJoplin()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield db, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


GUIDE = {
    "title": "Inner tie rod",
    "vehicle": "2001 Chevrolet Silverado 2500 HD",
    "summary": "Swap it.",
    "tags": ["Steering"],
    "steps": [{"title": "Loosen", "t_start": 10, "frame_t": 12, "instructions": ["Do it."], "torque_refs": ["jam"]}],
    "torque_specs": [{"id": "jam", "fastener": "Jam nut"}],
}


def _guide_dir(tmp_path: Path, img=b"jpeg-1") -> Path:
    d = tmp_path / "g"
    (d / "images").mkdir(parents=True)
    (d / "images" / "step-01-00m12s.jpg").write_bytes(img)
    (d / "guide.md").write_text(
        "# Inner tie rod\n\nIntro\n\n[![Jam nut](images/step-01-00m12s.jpg)](https://youtu.be/x&t=9s)\n", encoding="utf-8"
    )
    return d


def _push(base, d, cfg=None, **kw):
    cfg = {"manuals": {}, **(cfg or {})}
    meta = {"url": "https://www.youtube.com/watch?v=x", "vehicle_manual": "silverado"}
    return joplin.push(joplin.Client(base, TOKEN), d, Guide.model_validate(GUIDE), meta, cfg, **kw)


CFG = {"manuals": {"silverado": {"path": "/m/2006 Chevrolet Silverado 2500 4WD V8-6.6L DSL Turbo"}}}


def test_first_push_creates_note_folders_resources_tags(server, tmp_path):
    db, base = server
    d = _guide_dir(tmp_path)
    r = _push(base, d, CFG)
    assert r.created and r.uploaded == 1
    assert r.folder == ["Repair Guides", "2006 Chevrolet Silverado 2500 4WD V8-6.6L DSL Turbo"]
    note = db.notes[r.note_id]
    assert not note["body"].startswith("# Inner tie rod")  # title not duplicated in body
    rid = next(iter(db.resources))
    assert f"](:/{rid})" in note["body"] and "images/" not in note["body"]
    assert note["source_url"] == "https://www.youtube.com/watch?v=x"
    assert set(r.tags) == {"repair-guide", "silverado", "steering", "spec-needed"}
    assert len([1 for _, n in db.note_tags if n == r.note_id]) == 4
    assert json.loads((d / "joplin.json").read_text())["note_id"] == r.note_id


def test_repush_updates_in_place_and_reuses_images(server, tmp_path):
    db, base = server
    d = _guide_dir(tmp_path)
    first = _push(base, d, CFG)
    second = _push(base, d, CFG)
    assert not second.created and second.note_id == first.note_id
    assert second.uploaded == 0 and second.reused == 1 and len(db.notes) == 1 and len(db.resources) == 1
    # a changed image is re-uploaded and the old attachment deleted
    (d / "images" / "step-01-00m12s.jpg").write_bytes(b"jpeg-2")
    third = _push(base, d, CFG)
    assert third.uploaded == 1 and third.deleted == 1 and len(db.resources) == 1


def test_repush_refuses_to_clobber_joplin_edits(server, tmp_path):
    db, base = server
    d = _guide_dir(tmp_path)
    r = _push(base, d, CFG)
    db.notes[r.note_id]["body"] += "\n\nMy own note: used a crowfoot."
    with pytest.raises(joplin.EditedInJoplin):
        _push(base, d, CFG)
    assert _push(base, d, CFG, force=True).note_id == r.note_id


def test_repush_after_trash_creates_new_note_and_drops_stale_tags(server, tmp_path):
    db, base = server
    d = _guide_dir(tmp_path)
    r = _push(base, d, CFG)
    # user adds their own tag; it must survive our tag updates
    own = {"id": "own", "title": "done"}
    db.tags["own"] = own
    db.note_tags.add(("own", r.note_id))
    GUIDE["torque_specs"][0].update(value=68, unit="Nm", sources=[{"kind": "manual", "ref": "Alignment"}])
    try:
        r2 = _push(base, d, CFG)
        on_note = {db.tags[t]["title"] for t, n in db.note_tags if n == r2.note_id}
        assert "spec-needed" not in on_note and "done" in on_note
        db.notes[r.note_id]["deleted_time"] = 123
        r3 = _push(base, d, CFG)
        assert r3.created and r3.note_id != r.note_id
    finally:
        GUIDE["torque_specs"][0] = {"id": "jam", "fastener": "Jam nut"}


def test_explicit_notebook_and_vehicle_fallback(server, tmp_path):
    _, base = server
    d = _guide_dir(tmp_path)
    r = _push(base, d, {}, notebook="Truck / Steering")
    assert r.folder == ["Truck", "Steering"]
    g = Guide.model_validate(GUIDE)
    label, tag = joplin.vehicle_label({}, g, {"manuals": {}})
    assert label == "2001 Chevrolet Silverado 2500 HD" and tag == "2001-chevrolet-silverado-2500-hd"


def test_connect_reads_token_and_reports_unavailable(server, tmp_path, monkeypatch):
    _, base = server
    prof = tmp_path / "joplin-desktop"
    prof.mkdir()
    (prof / "settings.json").write_text(json.dumps({"api.token": TOKEN}))
    monkeypatch.setenv("JOPLIN_PROFILE_DIR", str(prof))
    monkeypatch.delenv("JOPLIN_TOKEN", raising=False)
    c = joplin.connect({"joplin_url": base})
    assert c.token == TOKEN
    with pytest.raises(joplin.JoplinUnavailable):
        joplin.connect({"joplin_url": "http://127.0.0.1:9"})
