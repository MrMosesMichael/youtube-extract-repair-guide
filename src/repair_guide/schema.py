"""steps.json — the contract between the analysis step (Claude) and the renderer.

Times may be written as seconds (125.5) or clock strings ("2:05", "1:02:05").
Run `repair-guide schema` to print the JSON Schema.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from .torque import normalize_unit
from .util import parse_time

Seconds = Annotated[float, BeforeValidator(parse_time)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Tool(_Model):
    name: str = Field(description="Generic tool name, e.g. 'socket', 'wrench', 'breaker bar', 'pry bar', 'brake line wrench'.")
    size: str | None = Field(None, description="Exact size as stated/visible, e.g. '22mm', '7/16\"', 'T50 Torx', 'E12'. Null if unknown — never guess.")
    detail: str | None = Field(None, description="Drive size, deep/shallow, impact, etc. e.g. '1/2\" drive, deep'.")
    size_certainty: Literal["stated", "visible", "inferred"] | None = Field(
        None, description="'stated' = said in video, 'visible' = readable on screen, 'inferred' = from manual/web/common knowledge."
    )

    def label(self) -> str:
        bits = [b for b in (self.size, self.name) if b]
        s = " ".join(bits)
        if self.detail:
            s += f" ({self.detail})"
        if self.size_certainty == "inferred":
            s += " †"
        return s


class Source(_Model):
    kind: Literal["video", "manual", "web", "user"]
    t: Seconds | None = Field(None, description="Video timestamp where it is stated (kind=video).")
    ref: str | None = Field(None, description="Manual page path / breadcrumb (kind=manual) or page title (kind=web).")
    url: str | None = Field(None, description="URL (kind=web).")
    quote: str | None = Field(None, description="Exact text as stated in the source.")

    @model_validator(mode="after")
    def _check(self) -> Source:
        if self.kind == "video" and self.t is None:
            raise ValueError("video sources need 't' (the timestamp where the spec is said/shown)")
        if self.kind == "web" and not self.url:
            raise ValueError("web sources need 'url'")
        if self.kind == "manual" and not self.ref:
            raise ValueError("manual sources need 'ref' (page path or breadcrumb)")
        return self


class TorqueSpec(_Model):
    id: str = Field(description="Short slug referenced from steps, e.g. 'caliper-bracket-bolt'.")
    fastener: str
    value: float | None = Field(None, description="Torque value in `unit`. Null when only an angle or 'snug' is specified, or the spec is unknown.")
    value_max: float | None = Field(None, description="Upper bound if the spec is a range.")
    unit: str | None = Field(None, description="Unit exactly as published: 'N·m', 'ft·lb', 'in·lb', 'Nm', 'lb ft', 'inch lbs', …")
    angle_deg: float | None = Field(None, description="Additional angle after torque (torque-to-yield), e.g. 90.")
    sequence: str | None = Field(None, description="Tightening pattern/passes, e.g. 'star pattern, 2 passes'.")
    one_time_use: bool = Field(False, description="Bolt/nut must be replaced (TTY, prevailing-torque nut, etc.).")
    threadlocker: str | None = Field(None, description="e.g. 'blue (medium)', 'red (high)', 'pre-applied'.")
    notes: str | None = None
    sources: list[Source] = Field(default_factory=list, description="Where the spec came from. Empty = SPEC NEEDED.")

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str | None) -> str | None:
        return normalize_unit(v) if v else v

    @model_validator(mode="after")
    def _check(self) -> TorqueSpec:
        if self.value is not None and not self.unit:
            raise ValueError(f"torque '{self.id}': value given without unit")
        if self.value_max is not None and self.value is None:
            raise ValueError(f"torque '{self.id}': value_max without value")
        if self.value is not None and not self.sources:
            raise ValueError(f"torque '{self.id}': a value needs at least one source (never invent specs)")
        return self

    @property
    def missing(self) -> bool:
        return self.value is None and self.angle_deg is None

    @property
    def unverified(self) -> bool:
        return bool(self.sources) and all(s.kind == "web" for s in self.sources)


Phase = Literal["prep", "removal", "inspection", "installation", "adjustment", "test", "other"]


class Step(_Model):
    title: str = Field(description="Imperative, short: 'Remove caliper bracket bolts'.")
    phase: Phase = "other"
    t_start: Seconds = Field(description="Where this step starts in the video.")
    t_end: Seconds | None = None
    frame_t: Seconds | None = Field(None, description="Best moment showing the part/fastener being worked on. Null = no image.")
    extra_frames: list[Seconds] = Field(default_factory=list, max_length=2, description="Up to 2 more images for this step.")
    caption: str | None = Field(None, description="What the image shows, e.g. 'Upper 22mm caliper bracket bolt, back side of knuckle'.")
    instructions: list[str] = Field(min_length=1)
    tools: list[Tool] = Field(default_factory=list)
    torque_refs: list[str] = Field(default_factory=list, description="TorqueSpec ids for fasteners touched in this step.")
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Part(_Model):
    name: str
    qty: str | None = None
    part_number: str | None = None
    notes: str | None = None


class Guide(_Model):
    title: str = Field(description="Job title, e.g. 'Front brake caliper & pad replacement'.")
    vehicle: str | None = Field(None, description="Vehicle shown in the video, as specific as the video allows.")
    applies_to: list[str] = Field(default_factory=list, description="Other vehicles the video says this applies to.")
    summary: str
    difficulty: Literal["easy", "moderate", "hard", "expert"] | None = None
    est_time: str | None = Field(None, description="e.g. '1.5–2 h per side'.")
    safety: list[str] = Field(default_factory=list)
    parts: list[Part] = Field(default_factory=list)
    consumables: list[str] = Field(default_factory=list, description="Brake clean, grease, threadlocker, fluid…")
    steps: list[Step] = Field(min_length=1)
    torque_specs: list[TorqueSpec] = Field(default_factory=list)
    reassembly_notes: list[str] = Field(default_factory=list, description="Anything to watch on the way back together.")

    @model_validator(mode="after")
    def _refs(self) -> Guide:
        ids = [t.id for t in self.torque_specs]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate torque_specs ids: {sorted(dupes)}")
        known = set(ids)
        for n, st in enumerate(self.steps, 1):
            bad = [r for r in st.torque_refs if r not in known]
            if bad:
                raise ValueError(f"step {n} ('{st.title}') references unknown torque ids {bad}; known: {sorted(known)}")
        return self

    def check_times(self, duration: float | None) -> list[str]:
        """Soft checks against the real video length; returns warnings."""
        if not duration:
            return []
        problems = []
        for n, st in enumerate(self.steps, 1):
            for label, t in (("t_start", st.t_start), ("t_end", st.t_end), ("frame_t", st.frame_t)):
                if t is not None and not 0 <= t <= duration + 1:
                    problems.append(f"step {n} {label}={t:.1f}s is outside the video (0–{duration:.0f}s)")
            for t in st.extra_frames:
                if not 0 <= t <= duration + 1:
                    problems.append(f"step {n} extra frame {t:.1f}s is outside the video")
        return problems
