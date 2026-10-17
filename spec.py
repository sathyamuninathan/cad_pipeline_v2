"""Part description (the JSON written from a drawing): data model, consistency rules and clean-up helpers."""
from __future__ import annotations

import json
import math
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Point = tuple[float, float]


class Status(str, Enum):
    read = "read"
    derived = "derived"
    assumed = "assumed"
    origin = "origin"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Value(Strict):
    value: float | str | list[float] | None = None
    status: Status | None = None
    source: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: str | None = None
    depends_on: list[str] = []
    review_flag: str | None = None
    tolerance: str | None = None

    @model_validator(mode="after")
    def assumed_needs_flag(self):
        if self.status == Status.assumed and not self.review_flag:
            raise ValueError("an assumed value must carry a review_flag")
        return self


class Dimension(Value):
    id: str
    count: int | None = None


class Line(Strict):
    type: Literal["line"]
    start: Point = Field(alias="from")
    end: Point = Field(alias="to")
    angle_deg: float | None = None


class Arc(Strict):
    type: Literal["arc"]
    center: Point
    radius: float = Field(gt=0)
    start: Point | None = Field(default=None, alias="from")
    end: Point | None = Field(default=None, alias="to")
    clockwise: bool = False
    note: str | None = None


Segment = Annotated[Union[Line, Arc], Field(discriminator="type")]


class CircleShape(Strict):
    center: Point = (0, 0)
    radius: float = Field(gt=0)


class RectangleShape(Strict):
    center: Point = (0, 0)
    size: Point
    corner_radius: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def positive(self):
        if self.size[0] <= 0 or self.size[1] <= 0:
            raise ValueError("rectangle size must be positive")
        if self.corner_radius * 2 > min(self.size) + 1e-9:
            raise ValueError("corner_radius is larger than half the rectangle")
        return self


class SlotShape(Strict):
    center_1: Point
    center_2: Point
    width: float = Field(gt=0)


class Profile(Strict):
    shape: str | None = None
    description: str | None = None
    segments: list[Segment] | None = None
    circle: CircleShape | None = None
    rectangle: RectangleShape | None = None
    slot: SlotShape | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        given = [k for k in ("segments", "circle", "rectangle", "slot") if getattr(self, k)]
        if len(given) != 1:
            raise ValueError("a profile needs exactly one of: segments, circle, rectangle, slot")
        return self

    def extent(self) -> tuple[float, float, float, float]:
        if self.circle:
            (cx, cy), r = self.circle.center, self.circle.radius
            return cx - r, cy - r, cx + r, cy + r
        if self.rectangle:
            (cx, cy), (w, h) = self.rectangle.center, self.rectangle.size
            return cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        if self.slot:
            s = self.slot
            xs, ys = [s.center_1[0], s.center_2[0]], [s.center_1[1], s.center_2[1]]
            return min(xs) - s.width / 2, min(ys) - s.width / 2, max(xs) + s.width / 2, max(ys) + s.width / 2
        xs, ys = [], []
        for seg in self.segments:
            if isinstance(seg, Arc):
                pts = arc_points(seg)
                if not pts:
                    return None
                xs += [p[0] for p in pts]
                ys += [p[1] for p in pts]
            else:
                xs += [seg.start[0], seg.end[0]]
                ys += [seg.start[1], seg.end[1]]
        return min(xs), min(ys), max(xs), max(ys)


def arc_points(seg: Arc, n: int = 720) -> list[Point]:
    if seg.start is None or seg.end is None:
        return []
    cx, cy = seg.center
    a0 = math.atan2(seg.start[1] - cy, seg.start[0] - cx)
    a1 = math.atan2(seg.end[1] - cy, seg.end[0] - cx)
    sweep = (a0 - a1) % (2 * math.pi) if seg.clockwise else (a1 - a0) % (2 * math.pi)
    sweep = sweep or 2 * math.pi
    sign = -1 if seg.clockwise else 1
    return [(cx + seg.radius * math.cos(a0 + sign * sweep * i / n), cy + seg.radius * math.sin(a0 + sign * sweep * i / n))
            for i in range(n + 1)]


class ExtrudedProfile(Strict):
    type: Literal["extruded_profile"]
    profile: Profile
    extrude_height: float = Field(gt=0)


class RevolvedProfile(Strict):
    type: Literal["revolved_profile"]
    profile: Profile
    axis: Literal["z"] = "z"
    angle_deg: float = Field(default=360, gt=0, le=360)


BaseBody = Annotated[Union[ExtrudedProfile, RevolvedProfile], Field(discriminator="type")]


class Hole(Strict):
    id: str
    type: Literal["hole"]
    through: bool = True
    diameter: Value
    position: Value
    depth: Value | None = None
    chamfer: Value | None = None
    axis: Literal["x", "y", "z"] = "z"
    from_side: Literal["max", "min"] = "max"


class ThreadedHole(Strict):
    id: str
    type: Literal["threaded_hole"]
    through: bool = True
    thread: Value
    modeled_diameter: Value
    position: Value
    depth: Value | None = None
    axis: Literal["x", "y", "z"] = "z"
    from_side: Literal["max", "min"] = "max"


class Slot(Strict):
    id: str
    type: Literal["slot"]
    through: bool = True
    width: Value
    arc_center_1: Value
    arc_center_2: Value
    depth: Value | None = None


class Extrusion(Strict):
    id: str
    type: Literal["extrusion"]
    mode: Literal["add", "cut"]
    plane: Literal["XY", "XZ", "YZ"] = "XY"
    offset: float = 0
    distance: float
    profile: Profile
    description: str | None = None

    @model_validator(mode="after")
    def nonzero(self):
        if abs(self.distance) < 1e-9:
            raise ValueError("extrusion distance must not be zero")
        return self


class Revolution(Strict):
    id: str
    type: Literal["revolution"]
    mode: Literal["add", "cut"]
    axis_position: Point = (0, 0)
    profile: Profile
    description: str | None = None

    @model_validator(mode="after")
    def segments_only(self):
        if not self.profile.segments:
            raise ValueError("a revolution profile must be given as segments in (radius, z)")
        return self


class Counterbore(Strict):
    id: str
    type: Literal["counterbore"]
    screw: Value | None = None
    series: Literal["fine", "medium", "coarse"] = "medium"
    position: Value
    hole_diameter: Value | None = None
    counterbore_diameter: Value | None = None
    counterbore_depth: Value | None = None
    through: bool = True
    depth: Value | None = None
    axis: Literal["x", "y", "z"] = "z"
    from_side: Literal["max", "min"] = "max"


class Countersink(Strict):
    id: str
    type: Literal["countersink"]
    screw: Value | None = None
    series: Literal["fine", "medium", "coarse"] = "medium"
    position: Value
    hole_diameter: Value | None = None
    countersink_diameter: Value | None = None
    angle_deg: float = Field(default=90, gt=0, lt=180)
    through: bool = True
    depth: Value | None = None
    axis: Literal["x", "y", "z"] = "z"
    from_side: Literal["max", "min"] = "max"


class Keyway(Strict):
    id: str
    type: Literal["keyway"]
    location: Literal["shaft", "hub"] = "shaft"
    form: Literal["A", "B"] = "A"
    shaft_diameter: Value
    length: Value
    z_start: Value
    width: Value | None = None
    depth: Value | None = None
    angle_deg: float = 0
    axis_position: Point = (0, 0)


class RetainingGroove(Strict):
    id: str
    type: Literal["retaining_groove"]
    location: Literal["shaft", "bore"] = "shaft"
    nominal_diameter: Value
    z_start: Value
    groove_diameter: Value | None = None
    width: Value | None = None
    axis_position: Point = (0, 0)


Feature = Annotated[Union[Hole, ThreadedHole, Slot, Extrusion, Revolution, Counterbore, Countersink, Keyway, RetainingGroove],
                    Field(discriminator="type")]


class Script(Strict):
    id: str
    description: str
    code: str = Field(min_length=1, max_length=20000)


class ReviewFlag(Strict):
    field: str
    severity: Literal["info", "warning", "blocking_until_confirmed"]
    reason: str
    assumed_value: float | str | None = None


class GroundTruth(Strict):
    bbox: tuple[float, float, float]
    bbox_min: tuple[float, float, float] | None = None
    volume: float
    holes: list[dict] = []
    slots: list[dict] = []


def chain_issues(segments, label: str, tolerance: float) -> list[str]:
    issues = []
    for a, b in zip(segments, segments[1:] + segments[:1]):
        if a.end is not None and b.start is not None and math.dist(a.end, b.start) > tolerance:
            issues.append(f"{label}: gap between {a.end} and {b.start}")
    for seg in segments:
        if isinstance(seg, Arc):
            for p in (seg.start, seg.end):
                if p is not None and abs(math.dist(p, seg.center) - seg.radius) > tolerance:
                    issues.append(f"{label}: arc endpoint {p} not on radius {seg.radius} around {seg.center}")
    return issues


class Part(Strict):
    schema_version: str
    part_id: str
    drawing_file: str
    reference_step: str | None = None
    units: Literal["mm"]
    drawing_scale: str | None = None
    general_tolerance: str | None = None
    coordinate_system: dict[str, str]
    base_body: BaseBody
    dimensions: list[Dimension] = []
    features: list[Feature] = []
    scripts: list[Script] = []
    review_flags: list[ReviewFlag] = []
    ground_truth_check: GroundTruth | None = None

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [d.id for d in self.dimensions] + [f.id for f in self.features]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"duplicate ids: {sorted(duplicates)}")
        return self

    def values(self):
        for d in self.dimensions:
            yield d.id, d
        for f in self.features:
            for name, v in f:
                if isinstance(v, Value):
                    yield f"{f.id}.{name}", v

    def consistency_issues(self, min_confidence: float = 0.8, tolerance: float = 1e-3) -> list[str]:
        issues = []
        known = {d.id for d in self.dimensions}
        flagged = {r.field for r in self.review_flags}
        for key, v in self.values():
            for dep in v.depends_on:
                if dep not in known:
                    issues.append(f"{key}: depends_on unknown id '{dep}'")
            covered = key in flagged or v.review_flag or any(dep in flagged for dep in v.depends_on)
            if v.confidence is not None and v.confidence < min_confidence and not covered:
                issues.append(f"{key}: confidence {v.confidence} below {min_confidence} without review flag")
            if v.status in (Status.derived, Status.assumed) and not v.evidence:
                issues.append(f"{key}: {v.status.value} value without evidence")
        profiles = [("base_body profile", self.base_body.profile, isinstance(self.base_body, RevolvedProfile))]
        profiles += [(f"{f.id} profile", f.profile, isinstance(f, Revolution)) for f in self.features
                     if isinstance(f, (Extrusion, Revolution))]
        for label, profile, revolved in profiles:
            if profile.segments:
                issues += chain_issues(profile.segments, label, tolerance)
                if revolved:
                    for seg in profile.segments:
                        pts = [seg.start, seg.end] if isinstance(seg, Line) else arc_points(seg, 36)
                        if any(p[0] < -tolerance for p in pts):
                            issues.append(f"{label}: radius must not be negative (point with r < 0)")
                            break
        return issues


def unwrap(v):
    return v["value"] if isinstance(v, dict) and "value" in v else v


def normalize(data: dict):
    body = data.get("base_body") or {}
    for k in ("extrude_height", "angle_deg"):
        if k in body:
            body[k] = unwrap(body[k])
    for seg in (body.get("profile") or {}).get("segments", []):
        for k in ("radius", "center", "from", "to", "angle_deg", "clockwise"):
            if k in seg:
                seg[k] = unwrap(seg[k])
    for f in data.get("features", []):
        for k in ("through", "offset", "distance", "axis_position", "mode", "plane", "axis", "from_side"):
            if k in f:
                f[k] = unwrap(f[k])
        for seg in (f.get("profile") or {}).get("segments") or []:
            for k in ("radius", "center", "from", "to", "clockwise"):
                if k in seg:
                    seg[k] = unwrap(seg[k])


def normalize_flags(data: dict):
    for f in data.get("review_flags", []):
        v = f.get("assumed_value")
        if isinstance(v, dict):
            v = v.get("value", json.dumps(v))
        if isinstance(v, (list, tuple)):
            v = json.dumps(v)
        if v is not None and not isinstance(v, (int, float, str)):
            v = str(v)
        if "assumed_value" in f:
            f["assumed_value"] = v


def clean(data: dict) -> list[str]:
    """Unwrap plain numbers, tidy review flags and fill sizes given only by standard. Returns what was filled."""
    import standards
    normalize(data)
    normalize_flags(data)
    return standards.fill(data)


def simplifications(data: dict) -> list[str]:
    words = ("simplif", "approximat", "omitted", "not modelled", "not modeled", "left out")
    return [f"{f.get('field')}: {f.get('reason')}" for f in data.get("review_flags", [])
            if f.get("severity") in ("warning", "blocking_until_confirmed")
            and any(w in str(f.get("reason", "")).lower() for w in words)]
