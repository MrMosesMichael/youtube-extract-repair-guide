"""Push a built guide into Joplin through its local Data API (the Web Clipper service).

The note body is guide.md with every `images/…` link swapped for an uploaded Joplin resource
(`:/<id>`), so images sync to Joplin's mobile apps like any other attachment.

State lives in `<guide>/joplin.json` so a re-push updates the same note in place:
  note_id       — the note we created
  body_sha      — sha256 of the title+body we last wrote; if Joplin's copy differs, the user
                  edited it there and we refuse to overwrite without --force
  resources     — image file -> {id, sha}: unchanged images are reused, stale ones deleted
  tags          — tags we applied (we only ever remove our own tags, never the user's)

API reference: https://joplinapp.org/help/api/references/rest_api/
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from .schema import Guide
from .util import UserError, info, read_json, slugify, write_json

DEFAULT_PORT = 41184
PORT_SCAN = range(41184, 41195)  # Joplin moves up a port when 41184 is taken
STATE_FILE = "joplin.json"
IMG_LINK = re.compile(r"\]\((images/[^)\s]+)\)")


class JoplinUnavailable(UserError):
    """The Web Clipper service isn't answering (Joplin closed, or the service is disabled)."""


ENABLE_HINT = (
    "Joplin's Web Clipper service isn't answering. Open Joplin, then "
    "Settings › Web Clipper › Enable Web Clipper Service (the browser extension isn't needed)."
)


# --------------------------------------------------------------------------- connection


def _ping(base: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(base + "/ping", timeout=timeout) as r:
            return r.read().decode(errors="replace").strip() == "JoplinClipperServer"
    except (OSError, urllib.error.URLError):
        return False


def find_server(cfg: dict) -> str:
    url = cfg.get("joplin_url") or os.environ.get("JOPLIN_URL")
    if url:
        url = url.rstrip("/")
        if _ping(url):
            return url
        raise JoplinUnavailable(f"No Joplin Data API at {url}. {ENABLE_HINT}")
    for port in PORT_SCAN:
        base = f"http://127.0.0.1:{port}"
        if _ping(base, timeout=0.6):
            return base
    raise JoplinUnavailable(ENABLE_HINT)


def _desktop_settings_files() -> list[Path]:
    root = Path(os.environ.get("JOPLIN_PROFILE_DIR", "~/.config/joplin-desktop")).expanduser()
    files = [root / "settings.json"]
    files += sorted(root.glob("profile-*/settings.json"))
    return [f for f in files if f.exists()]


def token_candidates(cfg: dict) -> list[tuple[str, str]]:
    """(source, token) pairs in priority order. Joplin desktop keeps its API token in settings.json."""
    out = []
    for src, tok in (("config joplin_token", cfg.get("joplin_token")), ("$JOPLIN_TOKEN", os.environ.get("JOPLIN_TOKEN"))):
        if tok:
            out.append((src, tok))
    for f in _desktop_settings_files():
        try:
            tok = read_json(f).get("api.token")
        except (OSError, ValueError):
            continue
        if tok:
            out.append((str(f), tok))
    return out


class Client:
    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    # -- low level
    def _url(self, path: str, query: dict | None = None) -> str:
        q = {"token": self.token, **(query or {})}
        return f"{self.base}{path}?{urllib.parse.urlencode(q)}"

    def _send(self, req: urllib.request.Request, timeout: float = 60):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            if e.code == 403:
                raise UserError("Joplin rejected the API token (403). Run `repair-guide joplin auth`.") from e
            if e.code == 404:
                raise NotFound(body) from e
            raise UserError(f"Joplin API {req.get_method()} {req.full_url.split('?')[0]} failed: {e.code} {body}") from e
        except (OSError, urllib.error.URLError) as e:
            raise JoplinUnavailable(f"Lost connection to Joplin ({e}). {ENABLE_HINT}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")

    def call(self, method: str, path: str, data: dict | None = None, query: dict | None = None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self._url(path, query), data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        return self._send(req)

    def get_all(self, path: str, fields: str, extra: dict | None = None) -> list[dict]:
        items, page = [], 1
        while True:
            res = self.call("GET", path, query={"fields": fields, "limit": 100, "page": page, **(extra or {})})
            items += res.get("items", [])
            if not res.get("has_more"):
                return items
            page += 1

    # -- resources
    def upload_resource(self, path: Path, title: str) -> dict:
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"props\"\r\n\r\n".encode(),
            json.dumps({"title": title}).encode(),
            f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"data\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {mime}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        req = urllib.request.Request(self._url("/resources"), data=b"".join(parts), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        return self._send(req, timeout=120)

    def delete_resource(self, rid: str) -> None:
        try:
            self.call("DELETE", f"/resources/{rid}")
        except NotFound:
            pass

    # -- notes
    def get_note(self, nid: str) -> dict | None:
        """The note, or None if it's gone or in the trash."""
        try:
            note = self.call("GET", f"/notes/{nid}", query={"fields": "id,title,body,parent_id,deleted_time"})
        except NotFound:
            return None
        except UserError:  # Joplin < 3.0 has no deleted_time column
            try:
                note = self.call("GET", f"/notes/{nid}", query={"fields": "id,title,body,parent_id"})
            except NotFound:
                return None
        if not note or note.get("deleted_time"):
            return None
        return note

    # -- folders
    def ensure_folder_path(self, names: list[str]) -> str:
        folders = [
            f for f in self.get_all("/folders", "id,title,parent_id,deleted_time") if not f.get("deleted_time")
        ]
        parent = ""
        for name in names:
            hit = next(
                (f for f in folders if (f.get("parent_id") or "") == parent and f["title"].strip().lower() == name.lower()),
                None,
            )
            if hit is None:
                hit = self.call("POST", "/folders", {"title": name, "parent_id": parent})
                info(f"  created notebook: {name}")
                folders.append({**hit, "parent_id": parent})
            parent = hit["id"]
        return parent

    # -- tags
    def set_tags(self, nid: str, wanted: list[str], previously_ours: list[str]) -> None:
        all_tags = {t["title"].lower(): t["id"] for t in self.get_all("/tags", "id,title")}
        on_note = {t["title"].lower(): t["id"] for t in self.get_all(f"/notes/{nid}/tags", "id,title")}
        for title in wanted:
            if title in on_note:
                continue
            tid = all_tags.get(title) or self.call("POST", "/tags", {"title": title})["id"]
            self.call("POST", f"/tags/{tid}/notes", {"id": nid})
        for title in previously_ours:
            if title not in wanted and title in on_note:
                self.call("DELETE", f"/tags/{on_note[title]}/notes/{nid}")


class NotFound(UserError):
    pass


def connect(cfg: dict) -> Client:
    base = find_server(cfg)
    cands = token_candidates(cfg)
    if not cands:
        raise UserError("No Joplin API token found. Run `repair-guide joplin auth` (you'll click Grant in Joplin).")
    for _src, tok in cands:
        c = Client(base, tok)
        try:
            c.call("GET", "/folders", query={"limit": 1, "fields": "id"})
            return c
        except UserError as e:
            if "403" not in str(e):
                raise
    raise UserError("Joplin rejected every API token found. Run `repair-guide joplin auth`.")


def authorize(cfg: dict, wait: float = 120) -> str:
    """Joplin's programmatic auth flow: ask for a token, the user clicks Grant in Joplin."""
    base = find_server(cfg)
    req = urllib.request.Request(base + "/auth", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        auth_token = json.loads(r.read())["auth_token"]
    info("Switch to Joplin and click “Grant authorization” in the dialog…")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base}/auth/check?auth_token={auth_token}", timeout=10) as r:
            res = json.loads(r.read())
        if res.get("status") == "accepted":
            return res["token"]
        if res.get("status") == "rejected":
            raise UserError("Authorization was rejected in Joplin.")
        time.sleep(1.5)
    raise UserError("Timed out waiting for authorization in Joplin.")


# --------------------------------------------------------------------------- push


@dataclass
class PushResult:
    note_id: str
    created: bool
    folder: list[str]
    uploaded: int
    reused: int
    deleted: int
    tags: list[str]


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _norm(text: str) -> str:
    # Joplin may normalise line endings / trailing whitespace on save
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().split("\n"))


def note_body(md: str, title: str) -> str:
    """guide.md minus its leading '# Title' (Joplin shows the note title itself)."""
    lines = md.split("\n")
    if lines and lines[0].strip() == f"# {title}".strip():
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines)


def vehicle_label(meta: dict, guide: Guide, cfg: dict) -> tuple[str | None, str | None]:
    """(sub-notebook name, vehicle tag). A registered service manual gives a stable per-truck name."""
    name = meta.get("vehicle_manual")
    entry = cfg.get("manuals", {}).get(name) if name else None
    if entry:
        return entry.get("label") or Path(entry["path"]).name, name.lower()
    if guide.vehicle:
        return guide.vehicle, slugify(guide.vehicle)[:40].strip("-")
    return None, None


def wanted_tags(guide: Guide, vehicle_tag: str | None) -> list[str]:
    tags = ["repair-guide"]
    if vehicle_tag:
        tags.append(vehicle_tag)
    tags += [t.strip().lower() for t in guide.tags if t.strip()]
    if any(t.missing for t in guide.torque_specs):
        tags.append("spec-needed")
    if any(t.unverified for t in guide.torque_specs):
        tags.append("unverified-spec")
    return list(dict.fromkeys(tags))


def push(client: Client, guide_dir: Path, guide: Guide, meta: dict, cfg: dict, *, notebook: str | None = None,
         force: bool = False) -> PushResult:
    md_path = guide_dir / "guide.md"
    if not md_path.exists():
        raise UserError(f"No guide.md in {guide_dir}. Run `repair-guide build` first.")
    md = md_path.read_text(encoding="utf-8")
    state_path = guide_dir / STATE_FILE
    state = read_json(state_path) if state_path.exists() else {}

    # 1. Find the existing note and make sure the user hasn't edited it in Joplin.
    note = client.get_note(state["note_id"]) if state.get("note_id") else None
    if state.get("note_id") and note is None:
        info("  previous note is gone or in the trash; creating a new one")
        state = {"tags": state.get("tags", [])}
    if note is not None and not force:
        current = _sha(_norm(note.get("title", "")) + "\n" + _norm(note.get("body", "")))
        if current != state.get("body_sha"):
            raise EditedInJoplin(
                f"The note '{note.get('title')}' was edited in Joplin since the last push. "
                "Re-run with --force to overwrite those edits."
            )

    # 2. Upload images (reuse unchanged ones by content hash).
    old_res: dict = dict(state.get("resources", {}))
    new_res: dict = {}
    uploaded = reused = 0
    for rel in dict.fromkeys(IMG_LINK.findall(md)):
        f = guide_dir / rel
        if not f.exists():
            raise UserError(f"guide.md links {rel} but the file is missing. Re-run `repair-guide build`.")
        sha = _sha(f.read_bytes())
        prev = old_res.get(rel)
        if prev and prev.get("sha") == sha:
            new_res[rel] = prev
            reused += 1
        else:
            r = client.upload_resource(f, f.name)
            new_res[rel] = {"id": r["id"], "sha": sha}
            uploaded += 1
    body = IMG_LINK.sub(lambda m: f"](:/{new_res[m.group(1)]['id']})", note_body(md, guide.title))

    # 3. Create or update the note. On update we leave parent_id alone: if the user moved it, it stays moved.
    label, vtag = vehicle_label(meta, guide, cfg)
    if notebook:
        folder_path = [p.strip() for p in notebook.split("/") if p.strip()]
    else:
        folder_path = [cfg.get("joplin_notebook") or "Repair Guides"] + ([label] if label else [])
    src_url = meta.get("url") or ""
    if note is None:
        parent = client.ensure_folder_path(folder_path)
        created = client.call("POST", "/notes", {"title": guide.title, "body": body, "parent_id": parent,
                                                  "source_url": src_url})
        nid, was_created = created["id"], True
    else:
        nid, was_created = note["id"], False
        client.call("PUT", f"/notes/{nid}", {"title": guide.title, "body": body, "source_url": src_url})
        if notebook:
            client.call("PUT", f"/notes/{nid}", {"parent_id": client.ensure_folder_path(folder_path)})

    # 4. Remove images this guide no longer uses (only resources we uploaded).
    in_use = {v["id"] for v in new_res.values()}
    stale = [v["id"] for v in old_res.values() if v["id"] not in in_use]
    for rid in stale:
        client.delete_resource(rid)

    # 5. Tags.
    tags = wanted_tags(guide, vtag)
    client.set_tags(nid, tags, state.get("tags", []))

    # 6. Remember what Joplin now holds.
    saved = client.get_note(nid) or {"title": guide.title, "body": body}
    write_json(state_path, {
        "note_id": nid,
        "body_sha": _sha(_norm(saved.get("title", "")) + "\n" + _norm(saved.get("body", ""))),
        "resources": new_res,
        "tags": tags,
        "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })
    return PushResult(nid, was_created, folder_path, uploaded, reused, len(stale), tags)


class EditedInJoplin(UserError):
    pass
