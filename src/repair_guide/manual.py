"""Offline service-manual index + search (SQLite FTS5).

Built for HTML manual exports such as Operation CHARM (charm.li), but works on any
folder of HTML pages. Two tables:

* ``pages``   – full text of every page, for "find the procedure" searches.
* ``torques`` – every line that contains a torque value, with the preceding line as
  context, for "what's the spec for X" lookups.

The manual itself is never copied into the repo or the guide; only the local index
lives under ~/.cache/repair-guide/manuals/.
"""

from __future__ import annotations

import html as htmllib
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .config import cache_dir
from .torque import display, find_torques
from .util import UserError, info

SCHEMA_VERSION = "2"

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "for", "on", "in", "with", "torque", "spec",
    "specs", "specification", "tighten", "bolt", "bolts", "what", "is", "nut", "nuts",
}


# ----------------------------------------------------------------------------- parsing

_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
_MAIN_START = re.compile(r"<div class=['\"]main['\"]>", re.I)
_FOOTER = re.compile(r"<div class=['\"][^'\"]*footer[^'\"]*['\"]>", re.I)
_BLOCK_END = re.compile(r"<br\s*/?>|</(p|div|tr|li|h[1-6]|table|ul|ol)>", re.I)


def html_to_text(raw: str) -> str:
    m = _MAIN_START.search(raw)
    if m:
        raw = raw[m.end():]
        f = _FOOTER.search(raw)
        if f:
            raw = raw[: f.start()]
    raw = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", raw)
    raw = re.sub(r"(?is)<div class=['\"][^'\"]*(floaty-message|lemon-message)[^'\"]*['\"]>.*?</div>", " ", raw)
    raw = re.sub(r"(?i)</t[dh]>", " \t ", raw)
    raw = _BLOCK_END.sub("\n", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    text = htmllib.unescape(raw)
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
        ln = re.sub(r"^\^\s*", "", ln)  # CHARM uses '^' as a bullet glyph
        if ln and not ln.startswith("Expand All (for easy ctrl-f)"):
            lines.append(ln)
    return "\n".join(lines)


def page_title(raw: str, fallback: str) -> str:
    m = _TITLE_RE.search(raw)
    if not m:
        return fallback
    t = htmllib.unescape(m.group(1)).strip()
    return re.split(r"\s+[—|]\s+", t)[0].strip() or fallback


def crumbs_for(root: Path, page: Path) -> str:
    rel = page.parent.relative_to(root)
    return " > ".join(urllib.parse.unquote(p) for p in rel.parts)


# ----------------------------------------------------------------------------- index


def index_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)
    return cache_dir() / "manuals" / f"{safe}.sqlite"


def build_index(name: str, root: Path) -> dict:
    root = root.expanduser().resolve()
    if root.suffix.lower() == ".zip":
        raise UserError("That's a .zip — extract it first and point at the folder.")
    if not root.is_dir():
        raise UserError(f"Not a folder: {root}")
    pages = [p for p in root.rglob("*.html") if p.parent != root]
    if not pages:
        raise UserError(f"No .html pages found under {root}")

    db_path = index_path(name)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(
        """
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE VIRTUAL TABLE pages USING fts5(title, crumbs, body, path UNINDEXED, nav UNINDEXED, tokenize='porter unicode61');
        CREATE VIRTUAL TABLE torques USING fts5(line, context, title, crumbs, path UNINDEXED, tokenize='porter unicode61');
        """
    )
    t0 = time.time()
    n_torque = 0
    for i, page in enumerate(pages, 1):
        raw = page.read_text(encoding="utf-8", errors="replace")
        body = html_to_text(raw)
        crumbs = crumbs_for(root, page)
        title = page_title(raw, crumbs.split(" > ")[-1] if crumbs else page.stem)
        rel = str(page.relative_to(root))
        # navigation/index pages just list child pages; rank them below real procedures
        nav = int("Expand All (for easy ctrl-f)" in raw or len(body) < 200)
        con.execute("INSERT INTO pages VALUES (?,?,?,?,?)", (title, crumbs, body, rel, nav))
        lines = body.splitlines()
        for j, ln in enumerate(lines):
            if find_torques(ln):
                ctx = " / ".join(lines[max(0, j - 2) : j])[-300:]
                con.execute("INSERT INTO torques VALUES (?,?,?,?,?)", (ln[:600], ctx, title, crumbs, rel))
                n_torque += 1
        if i % 2000 == 0:
            info(f"  indexed {i}/{len(pages)} pages…")
    stats = {"pages": len(pages), "torque_lines": n_torque}
    con.executemany(
        "INSERT INTO meta VALUES (?,?)",
        [("schema", SCHEMA_VERSION), ("root", str(root)), ("name", name),
         ("pages", str(len(pages))), ("torque_lines", str(n_torque)), ("built", time.strftime("%Y-%m-%d %H:%M"))],
    )
    con.commit()
    con.execute("INSERT INTO pages(pages) VALUES('optimize')")
    con.execute("INSERT INTO torques(torques) VALUES('optimize')")
    con.commit()
    con.close()
    tmp.replace(db_path)
    info(f"  done in {time.time() - t0:.0f}s: {stats['pages']} pages, {n_torque} torque lines")
    return {"path": str(root), "index": str(db_path), **stats}


# ----------------------------------------------------------------------------- search


def _terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", query.lower())
    return [w for w in words if w not in STOPWORDS] or words


def _fts_query(terms: list[str], op: str) -> str:
    return f" {op} ".join(f'"{t}"' for t in terms)


@dataclass
class TorqueHit:
    line: str
    context: str
    title: str
    crumbs: str
    path: str
    converted: str
    also_on: int = 0


@dataclass
class PageHit:
    title: str
    crumbs: str
    path: str
    snippet: str
    torque_lines: list[str]


def _open(name: str, cfg: dict) -> tuple[sqlite3.Connection, Path]:
    m = cfg.get("manuals", {}).get(name)
    if not m:
        known = ", ".join(cfg.get("manuals", {})) or "none yet"
        raise UserError(f"No manual named '{name}' (known: {known}). Add one: repair-guide manual add <name> <folder>")
    db = Path(m["index"])
    if not db.exists():
        raise UserError(f"Index for '{name}' is missing; rebuild: repair-guide manual add {name} '{m['path']}'")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if not row or row[0] != SCHEMA_VERSION:
        con.close()
        raise UserError(
            f"The '{name}' index was built by an older repair-guide; rebuild it:\n"
            f"  repair-guide manual add {name} '{m['path']}'"
        )
    return con, Path(m["path"])


def search_torque(name: str, cfg: dict, query: str, limit: int = 8) -> list[TorqueHit]:
    con, root = _open(name, cfg)
    terms = _terms(query)
    rows: list[tuple] = []
    for op in ("AND", "OR"):
        rows = con.execute(
            "SELECT line, context, title, crumbs, path FROM torques WHERE torques MATCH ? "
            "ORDER BY bm25(torques, 6.0, 2.0, 4.0, 1.0) LIMIT ?",
            (_fts_query(terms, op), limit * 6),
        ).fetchall()
        if rows:
            break
    con.close()
    seen: dict[str, TorqueHit] = {}
    for line, ctx, title, crumbs, path in rows:
        key = re.sub(r"\W+", " ", line.lower()).strip()
        if key in seen:
            seen[key].also_on += 1
            continue
        found = find_torques(line)
        conv = display(found[0].value, found[0].unit, found[0].value_max).inline() if found else ""
        seen[key] = TorqueHit(line, ctx, title, crumbs, str(root / path), conv)
        if len(seen) >= limit:
            break
    return list(seen.values())


def search_pages(name: str, cfg: dict, query: str, limit: int = 8) -> list[PageHit]:
    con, root = _open(name, cfg)
    terms = _terms(query)
    rows: list[tuple] = []
    for op in ("AND", "OR"):
        rows = con.execute(
            "SELECT title, crumbs, path, snippet(pages, 2, '«', '»', ' … ', 24), body FROM pages "
            "WHERE pages MATCH ? ORDER BY nav, bm25(pages, 8.0, 3.0, 1.0) LIMIT ?",
            (_fts_query(terms, op), limit),
        ).fetchall()
        if rows:
            break
    con.close()
    hits = []
    for title, crumbs, path, snip, body in rows:
        tl = [ln for ln in body.splitlines() if find_torques(ln)][:6]
        hits.append(PageHit(title, crumbs, str(root / path), snip.replace("\n", " "), tl))
    return hits


def page_text(name: str, cfg: dict, path: str) -> str:
    con, root = _open(name, cfg)
    con.close()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    if root.resolve() not in p.parents:
        raise UserError("That page is not inside the manual folder.")
    raw = p.read_text(encoding="utf-8", errors="replace")
    crumbs = crumbs_for(root.resolve(), p)
    return f"# {page_title(raw, p.parent.name)}\n{crumbs}\n\n{html_to_text(raw)}\n"
