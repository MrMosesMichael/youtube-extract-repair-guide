"""Torque unit parsing and conversion.

All conversion happens here, in code — never let the LLM do the arithmetic.

Display rules (see ``TorqueDisplay``):
  * N·m and ft·lb are always shown.
  * in·lb is also shown for light specs (<= 30 ft·lb / ~40.7 N·m), where
    inch-pound wrenches are the right tool.
  * N·cm is also shown for very small specs (< 5 N·m), e.g. sensor screws.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NM_PER_FTLB = 1.3558179483314004
NM_PER_INLB = NM_PER_FTLB / 12
NM_PER_KGFM = 9.80665

# canonical unit -> N·m per unit
UNITS: dict[str, float] = {
    "N·m": 1.0,
    "N·cm": 0.01,
    "ft·lb": NM_PER_FTLB,
    "in·lb": NM_PER_INLB,
    "kgf·m": NM_PER_KGFM,
    "kgf·cm": NM_PER_KGFM / 100,
}

_ALIASES: list[tuple[str, str]] = [
    # order matters: more specific first
    (r"n\s*[·.\-*]?\s*cm|newton[\s-]*cent[ie]met(er|re)s?", "N·cm"),
    (r"n\s*[·.\-*]?\s*m|newton[\s-]*met(er|re)s?|nm", "N·m"),
    (r"kgf?\s*[·.\-*]?\s*cm", "kgf·cm"),
    (r"kgf?\s*[·.\-*]?\s*m", "kgf·m"),
    (r"(in(ch)?(es)?|\")\s*[·.\-*]?\s*(lb|pound)s?f?|(lb|pound)s?f?\s*[·.\-*]?\s*in(ch)?(es)?", "in·lb"),
    (r"(f(oo|ee)?t|')\s*[·.\-*]?\s*(lb|pound)s?f?|(lb|pound)s?f?\s*[·.\-*]?\s*f(oo|ee)?t", "ft·lb"),
]


def normalize_unit(unit: str) -> str:
    """Map free-form unit text ('lb ft', 'inch lbs.', 'Nm', 'newton meters') to a canonical unit."""
    u = unit.strip().lower().rstrip(".").replace("‐", "-")
    u = re.sub(r"\s+", " ", u)
    for pattern, canon in _ALIASES:
        if re.fullmatch(pattern, u):
            return canon
    raise ValueError(
        f"Unrecognised torque unit {unit!r}. Use one of: N·m, N·cm, ft·lb, in·lb, kgf·m, kgf·cm "
        "(common spellings like 'Nm', 'lb-ft', 'ft lbs', 'inch lbs' are accepted)."
    )


def to_nm(value: float, unit: str) -> float:
    return value * UNITS[normalize_unit(unit)]


def _round(v: float) -> str:
    """Workshop precision: whole numbers from 20 up, one decimal below, two below 1."""
    if v >= 20:
        return f"{v:.0f}"
    if v >= 1:
        return f"{v:.1f}".removesuffix(".0")
    return f"{v:.2f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class TorqueDisplay:
    nm: str
    ftlb: str
    inlb: str | None
    ncm: str | None
    source_unit: str  # canonical unit the spec was published in

    def inline(self) -> str:
        """One-line form, source unit first: '175 ft·lb · 237 N·m'."""
        parts = {"N·m": f"{self.nm} N·m", "ft·lb": f"{self.ftlb} ft·lb"}
        if self.inlb:
            parts["in·lb"] = f"{self.inlb} in·lb"
        if self.ncm:
            parts["N·cm"] = f"{self.ncm} N·cm"
        order = ["N·m", "ft·lb", "in·lb", "N·cm"]
        first = self.source_unit if self.source_unit in parts else None
        seq = ([first] if first else []) + [u for u in order if u != first and u in parts]
        return " · ".join(parts[u] for u in seq)


def display(value: float, unit: str, value_max: float | None = None) -> TorqueDisplay:
    """Convert a spec (optionally a range) into every display unit."""
    canon = normalize_unit(unit)
    lo = value * UNITS[canon]
    hi = value_max * UNITS[canon] if value_max is not None else None

    def fmt(conv: float) -> str:
        a = _round(lo / conv)
        return a if hi is None else f"{a}–{_round(hi / conv)}"

    peak = hi if hi is not None else lo
    return TorqueDisplay(
        nm=fmt(1.0),
        ftlb=fmt(NM_PER_FTLB),
        inlb=fmt(NM_PER_INLB) if peak <= 30 * NM_PER_FTLB + 1e-9 else None,
        ncm=fmt(0.01) if peak < 5 else None,
        source_unit=canon,
    )


# --- extraction from free text (service manuals, transcripts) -------------------------

_NUM = r"(\d+(?:\.\d+)?)(?:\s*[-–]\s*(\d+(?:\.\d+)?))?"
_UNIT_WORDS = (
    r"N\s*[·.]?\s*m\b|Nm\b|N\s*[·.]?\s*cm\b|newton[\s-]*met(?:er|re)s?"
    r"|lb\.?[\s-]*ft\.?|ft\.?[\s-]*lbs?\.?|foot[\s-]*pounds?|pound[\s-]*feet"
    r"|lb\.?[\s-]*in\.?|in(?:ch)?\.?[\s-]*lbs?\.?|inch[\s-]*pounds?"
    r"|kgf?\s*[·.]?\s*cm|kgf?\s*[·.]?\s*m\b"
)
TORQUE_RE = re.compile(rf"{_NUM}\s*({_UNIT_WORDS})", re.IGNORECASE)


@dataclass(frozen=True)
class FoundTorque:
    value: float
    value_max: float | None
    unit: str  # canonical
    raw: str


def find_torques(text: str) -> list[FoundTorque]:
    """Pull every 'NN unit' torque mention out of a string.

    '12 Nm (106 inch lbs.)' -> two hits (12 N·m and 106 in·lb).
    """
    out: list[FoundTorque] = []
    for m in TORQUE_RE.finditer(text):
        try:
            unit = normalize_unit(m.group(3))
        except ValueError:
            continue
        out.append(
            FoundTorque(
                value=float(m.group(1)),
                value_max=float(m.group(2)) if m.group(2) else None,
                unit=unit,
                raw=m.group(0),
            )
        )
    return out
