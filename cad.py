"""Geometry: build a part JSON into a solid with build123d, validate it, compare it with a reference STEP.

Also the build worker: the MCP server runs `python cad.py` once per build (JSON request on stdin, JSON result on
stdout), so a hanging or crashing build can be killed without taking the server down.
"""
from __future__ import annotations

import ast
import builtins
import json
import math
import sys
import tempfile
import time
import warnings
from pathlib import Path

import build123d
from build123d import (Axis, BuildLine, BuildSketch, CenterOf, Circle, Cone, Cylinder, GeomType, Line as LineEdge,
                       Locations, Plane, Pos, Rectangle, RectangleRounded, Rot, SlotCenterToCenter, SlotOverall,
                       ThreePointArc, Vector, chamfer, export_step, extrude, import_step, make_face, revolve)
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.GeomAbs import GeomAbs_Cylinder
from pydantic import ValidationError

import standards
from spec import (Arc, Counterbore, Countersink, ExtrudedProfile, Extrusion, Hole, Keyway, Line, Part, RetainingGroove,
                  RevolvedProfile, Revolution, Slot, ThreadedHole)

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------- run_script fallback: restricted Python

ALLOWED_MODULES = {"build123d", "math"}
BLOCKED_NAMES = {
    "open", "exec", "eval", "compile", "input", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "breakpoint", "help", "memoryview", "__import__", "__builtins__", "exit", "quit",
}
SAFE_BUILTINS = {
    name: getattr(builtins, name) for name in (
        "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float", "int", "isinstance", "len",
        "list", "map", "max", "min", "pow", "print", "range", "reversed", "round", "set", "sorted", "str",
        "sum", "tuple", "zip", "ValueError", "Exception",
    )
}


class ScriptRejected(Exception):
    pass


def check_script(code: str):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ScriptRejected(f"syntax error on line {e.lineno}: {e.msg}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad = [a.name for a in node.names if a.name.split(".")[0] not in ALLOWED_MODULES]
            if bad:
                raise ScriptRejected(f"import of {bad} is not allowed, only build123d and math")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                raise ScriptRejected(f"import from '{node.module}' is not allowed, only build123d and math")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ScriptRejected(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in BLOCKED_NAMES or node.id.startswith("__")):
            raise ScriptRejected(f"use of '{node.id}' is not allowed")
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise ScriptRejected("global and nonlocal are not allowed")


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split(".")[0] not in ALLOWED_MODULES or level != 0:
        raise ImportError(f"import of '{name}' is not allowed")
    return __import__(name, globals, locals, fromlist, level)


def run_script(code: str, model):
    check_script(code)
    namespace = {"__builtins__": {**SAFE_BUILTINS, "__import__": guarded_import}, "math": math, "model": model}
    namespace.update({k: getattr(build123d, k) for k in build123d.__all__})
    exec(compile(code, "<run_script>", "exec"), namespace)
    result = namespace.get("model")
    if not isinstance(result, build123d.Shape) or result.volume <= 0:
        raise ScriptRejected("script must leave a solid with positive volume in the variable 'model'")
    return result


# ---------------------------------------------------------------- build

class BuildError(Exception):
    def __init__(self, code: str, message: str, target: str | None = None):
        super().__init__(message)
        self.code, self.message, self.target = code, message, target

    def to_dict(self):
        return {"code": self.code, "message": self.message, "target": self.target}


def arc_midpoint(arc: Arc) -> tuple[float, float]:
    cx, cy = arc.center
    a0 = math.atan2(arc.start[1] - cy, arc.start[0] - cx)
    a1 = math.atan2(arc.end[1] - cy, arc.end[0] - cx)
    sweep = (a0 - a1) % (2 * math.pi) if arc.clockwise else (a1 - a0) % (2 * math.pi)
    if sweep == 0:
        sweep = 2 * math.pi
    mid = a0 - sweep / 2 if arc.clockwise else a0 + sweep / 2
    return cx + arc.radius * math.cos(mid), cy + arc.radius * math.sin(mid)


def sketch_plane(name: str, offset: float = 0, origin: tuple[float, float] = (0, 0)) -> tuple[Plane, int]:
    if name == "XY":
        return Plane(origin=(0, 0, offset), x_dir=(1, 0, 0), z_dir=(0, 0, 1)), 1
    if name == "XZ":
        return Plane(origin=(origin[0], origin[1] + offset, 0), x_dir=(1, 0, 0), z_dir=(0, -1, 0)), -1
    return Plane(origin=(offset, 0, 0), x_dir=(0, 1, 0), z_dir=(1, 0, 0)), 1


def profile_face(profile, plane: Plane = Plane.XY, target: str = "profile"):
    if profile.segments:
        for i, seg in enumerate(profile.segments):
            if isinstance(seg, Arc) and (seg.start is None or seg.end is None):
                raise BuildError("arc_missing_endpoints", "arc needs 'from' and 'to' points to be built", f"{target}.segments[{i}]")
    with BuildSketch(plane) as sketch:
        if profile.segments:
            try:
                with BuildLine():
                    for i, seg in enumerate(profile.segments):
                        if isinstance(seg, Line):
                            if math.dist(seg.start, seg.end) < 1e-6:
                                raise BuildError("zero_length_segment", "a line starts and ends at the same point",
                                                 f"{target}.segments[{i}]")
                            LineEdge(seg.start, seg.end)
                        else:
                            ThreePointArc(seg.start, arc_midpoint(seg), seg.end)
                make_face()
            except BuildError:
                raise
            except Exception as e:
                raise BuildError("open_profile", f"the segments do not form one closed, non-crossing outline ({e})", target)
        elif profile.circle:
            with Locations(profile.circle.center):
                Circle(profile.circle.radius)
        elif profile.rectangle:
            r = profile.rectangle
            with Locations(r.center):
                if r.corner_radius > 0:
                    RectangleRounded(r.size[0], r.size[1], r.corner_radius)
                else:
                    Rectangle(r.size[0], r.size[1])
        else:
            s = profile.slot
            (x1, y1), (x2, y2) = s.center_1, s.center_2
            with Locations(((x1 + x2) / 2, (y1 + y2) / 2)):
                SlotCenterToCenter(math.dist((x1, y1), (x2, y2)), s.width,
                                   rotation=math.degrees(math.atan2(y2 - y1, x2 - x1)))
    if not sketch.sketch.faces():
        raise BuildError("open_profile", "the profile does not form a closed area", target)
    return sketch.sketch


def revolved(profile, axis_position=(0, 0), target: str = "profile"):
    plane, _ = sketch_plane("XZ", origin=axis_position)
    face = profile_face(profile, plane, target)
    return revolve(face, axis=Axis((axis_position[0], axis_position[1], 0), (0, 0, 1)), revolution_arc=360)


AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
ROTATE_TO_AXIS = {"z": Rot(0, 0, 0), "x": Rot(0, 90, 0), "y": Rot(90, 0, 0)}


def place_on_axis(axis: str, position, along: float):
    a, b = position
    if axis == "z":
        return Pos(a, b, along)
    if axis == "x":
        return Pos(along, a, b)
    return Pos(a, along, b)


def value(v, target: str):
    if v is None or v.value is None:
        raise BuildError("missing_value", "required value is missing", target)
    return v.value


def hole_edges(part, x: float, y: float, radius: float):
    return [
        e for e in part.edges().filter_by(GeomType.CIRCLE)
        if abs(e.radius - radius) < 1e-3 and math.dist((e.arc_center.X, e.arc_center.Y), (x, y)) < 1e-3
    ]


def extent_along(solid, axis: str) -> tuple[float, float]:
    bb = solid.bounding_box()
    i = AXIS_INDEX[axis]
    return (bb.min.X, bb.min.Y, bb.min.Z)[i], (bb.max.X, bb.max.Y, bb.max.Z)[i]


def drilled(solid, f):
    position = value(f.position, f"{f.id}.position")
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise BuildError("bad_position", "position must be two numbers", f"{f.id}.position")
    d = float(value(f.diameter if isinstance(f, Hole) else f.modeled_diameter, f"{f.id}.diameter"))
    lo, hi = extent_along(solid, f.axis)
    if f.through:
        length, center = hi - lo + 2, (lo + hi) / 2
    else:
        length = float(value(f.depth, f"{f.id}.depth"))
        center = hi - length / 2 if f.from_side == "max" else lo + length / 2
    tool = place_on_axis(f.axis, position, center) * ROTATE_TO_AXIS[f.axis] * Cylinder(d / 2, length)
    result = solid - tool
    if abs(result.volume - solid.volume) < 1e-9:
        raise BuildError("hole_misses_part", "the hole does not cut any material; check its position and axis", f"{f.id}.position")
    return result


POINT_ALONG = {"z": Rot(0, 0, 0), "x": Rot(0, 90, 0), "y": Rot(-90, 0, 0)}


def optional(v):
    return None if v is None or v.value is None else float(v.value)


def from_standard(f, target: str, fn, *args):
    try:
        return fn(*args)
    except standards.StandardError as e:
        raise BuildError("standard_lookup_failed", str(e), target)


def screw_values(f) -> dict:
    keys = ["hole_diameter", "counterbore_diameter", "counterbore_depth"] if isinstance(f, Counterbore) else ["hole_diameter", "countersink_diameter"]
    values = {k: optional(getattr(f, k)) for k in keys}
    if any(v is None for v in values.values()):
        if f.screw is None or f.screw.value is None:
            missing = [k for k, v in values.items() if v is None]
            raise BuildError("missing_value", f"give the screw size (e.g. 'M6') or all of: {', '.join(missing)}", f"{f.id}.screw")
        fn = standards.counterbore if isinstance(f, Counterbore) else standards.countersink
        ref = from_standard(f, f"{f.id}.screw", fn, f.screw.value, f.series)
        values = {k: v if v is not None else float(ref[k]) for k, v in values.items()}
    return values


def entry_cut(solid, f, local_tool):
    position = value(f.position, f"{f.id}.position")
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise BuildError("bad_position", "position must be two numbers", f"{f.id}.position")
    lo, hi = extent_along(solid, f.axis)
    if f.from_side == "max":
        tool = place_on_axis(f.axis, position, hi) * POINT_ALONG[f.axis] * local_tool
    else:
        tool = place_on_axis(f.axis, position, lo) * POINT_ALONG[f.axis] * Rot(180, 0, 0) * local_tool
    result = solid - tool
    if abs(result.volume - solid.volume) < 1e-9:
        raise BuildError("hole_misses_part", "the feature does not cut any material; check its position and axis", f"{f.id}.position")
    return result


def screw_hole(solid, f):
    v = screw_values(f)
    lo, hi = extent_along(solid, f.axis)
    length = hi - lo + 2 if f.through else float(value(f.depth, f"{f.id}.depth"))
    hole = Pos(0, 0, (1 - length) / 2 if f.through else -length / 2) * Cylinder(v["hole_diameter"] / 2, length + (1 if f.through else 0))
    if isinstance(f, Counterbore):
        if v["counterbore_diameter"] <= v["hole_diameter"]:
            raise BuildError("bad_counterbore", "counterbore diameter must be larger than the hole diameter", f"{f.id}.counterbore_diameter")
        t = v["counterbore_depth"]
        head = Pos(0, 0, (1 - t) / 2) * Cylinder(v["counterbore_diameter"] / 2, t + 1)
    else:
        if v["countersink_diameter"] <= v["hole_diameter"]:
            raise BuildError("bad_countersink", "countersink diameter must be larger than the hole diameter", f"{f.id}.countersink_diameter")
        half = math.radians(f.angle_deg / 2)
        h = (v["countersink_diameter"] - v["hole_diameter"]) / 2 / math.tan(half)
        extra = 0.5
        head = Pos(0, 0, (extra - h) / 2) * Cone(v["hole_diameter"] / 2, v["countersink_diameter"] / 2 + extra * math.tan(half), h + extra)
    return entry_cut(solid, f, hole + head)


def keyway_values(f: Keyway) -> tuple[float, float]:
    width, depth = optional(f.width), optional(f.depth)
    if width is None or depth is None:
        ref = from_standard(f, f"{f.id}.shaft_diameter", standards.keyway, float(value(f.shaft_diameter, f"{f.id}.shaft_diameter")))
        width = width if width is not None else float(ref["b"])
        depth = depth if depth is not None else float(ref["t1_shaft"] if f.location == "shaft" else ref["t2_hub"])
    return width, depth


def keyway_cut(solid, f: Keyway):
    d = float(value(f.shaft_diameter, f"{f.id}.shaft_diameter"))
    length = float(value(f.length, f"{f.id}.length"))
    z0 = float(value(f.z_start, f"{f.id}.z_start"))
    width, depth = keyway_values(f)
    if length < width and f.form == "A":
        raise BuildError("bad_keyway", "a form A keyway must be at least as long as it is wide", f"{f.id}.length")
    r = d / 2
    start, amount = (r - depth, depth + 1) if f.location == "shaft" else (r - 1, depth + 1)
    plane = Plane(origin=(start, 0, z0 + length / 2), x_dir=(0, 0, 1), z_dir=(1, 0, 0))
    with BuildSketch(plane) as sk:
        if f.form == "A":
            SlotOverall(length, width)
        else:
            Rectangle(length, width)
    tool = Pos(f.axis_position[0], f.axis_position[1], 0) * Rot(0, 0, f.angle_deg) * extrude(sk.sketch, amount=amount)
    result = solid - tool
    if abs(result.volume - solid.volume) < 1e-9:
        raise BuildError("feature_misses_part", "the keyway does not cut any material; check z_start and the diameter", f"{f.id}.z_start")
    return result


def groove_cut(solid, f: RetainingGroove):
    d1 = float(value(f.nominal_diameter, f"{f.id}.nominal_diameter"))
    d2, m = optional(f.groove_diameter), optional(f.width)
    if d2 is None or m is None:
        ref = from_standard(f, f"{f.id}.nominal_diameter", standards.retaining_groove, d1, f.location)
        d2 = d2 if d2 is not None else float(ref["d2_groove"])
        m = m if m is not None else float(ref["m_width"])
    if (f.location == "shaft" and d2 >= d1) or (f.location == "bore" and d2 <= d1):
        raise BuildError("bad_groove", "groove diameter must be smaller than a shaft or larger than a bore", f"{f.id}.groove_diameter")
    z0 = float(value(f.z_start, f"{f.id}.z_start"))
    outer, inner = (d1 / 2 + 1, d2 / 2) if f.location == "shaft" else (d2 / 2, max(d1 / 2 - 1, 0.01))
    ring = Cylinder(outer, m) - Cylinder(inner, m)
    result = solid - Pos(f.axis_position[0], f.axis_position[1], z0 + m / 2) * ring
    if abs(result.volume - solid.volume) < 1e-9:
        raise BuildError("feature_misses_part", "the groove does not cut any material; check z_start and the diameter", f"{f.id}.z_start")
    return result


def build(part: Part):
    body = part.base_body
    if isinstance(body, ExtrudedProfile):
        solid = extrude(profile_face(body.profile, Plane.XY, "base_body.profile"), amount=body.extrude_height)
    elif isinstance(body, RevolvedProfile):
        solid = revolved(body.profile, (0, 0), "base_body.profile")
    else:
        raise BuildError("unsupported_base_body", f"base body type '{body.type}' is not supported", "base_body")
    for f in part.features:
        if isinstance(f, (Hole, ThreadedHole)):
            solid = drilled(solid, f)
        elif isinstance(f, Slot):
            (x1, y1), (x2, y2) = value(f.arc_center_1, f"{f.id}.arc_center_1"), value(f.arc_center_2, f"{f.id}.arc_center_2")
            width = value(f.width, f"{f.id}.width")
            lo, hi = extent_along(solid, "z")
            depth = hi - lo + 1 if f.through else float(value(f.depth, f"{f.id}.depth"))
            with BuildSketch(Plane.XY.offset(hi)) as sk:
                with Locations(((x1 + x2) / 2, (y1 + y2) / 2)):
                    SlotCenterToCenter(math.dist((x1, y1), (x2, y2)), width,
                                       rotation=math.degrees(math.atan2(y2 - y1, x2 - x1)))
            solid = solid - extrude(sk.sketch, amount=-depth)
        elif isinstance(f, Extrusion):
            plane, sign = sketch_plane(f.plane, f.offset)
            body_part = extrude(profile_face(f.profile, plane, f"{f.id}.profile"), amount=sign * f.distance)
            solid = solid + body_part if f.mode == "add" else solid - body_part
        elif isinstance(f, Revolution):
            body_part = revolved(f.profile, f.axis_position, f"{f.id}.profile")
            solid = solid + body_part if f.mode == "add" else solid - body_part
        elif isinstance(f, (Counterbore, Countersink)):
            solid = screw_hole(solid, f)
        elif isinstance(f, Keyway):
            solid = keyway_cut(solid, f)
        elif isinstance(f, RetainingGroove):
            solid = groove_cut(solid, f)
    for f in part.features:
        if isinstance(f, Hole) and f.chamfer is not None and f.axis == "z":
            x, y = f.position.value
            edges = hole_edges(solid, x, y, f.diameter.value / 2)
            if not edges:
                raise BuildError("chamfer_edges_not_found", "no hole edges found to chamfer", f"{f.id}.chamfer")
            solid = chamfer(edges, length=float(f.chamfer.value))
    for s in part.scripts:
        try:
            solid = run_script(s.code, solid)
        except ScriptRejected as e:
            raise BuildError("script_rejected", str(e), f"scripts.{s.id}")
        except Exception as e:
            raise BuildError("script_failed", f"{type(e).__name__}: {e}", f"scripts.{s.id}")
    if solid is None or solid.volume <= 0:
        raise BuildError("empty_result", "build produced no solid", None)
    return solid


# ---------------------------------------------------------------- validate (runs after every build)

LINEAR_TOL = 0.01


def check(name, passed, target=None, expected=None, measured=None, deviation=None, hint=None):
    result = {"check": name, "passed": bool(passed)}
    for key, val in (("target", target), ("expected", expected), ("measured", measured), ("deviation", deviation), ("hint", hint)):
        if val is not None:
            result[key] = val
    return result


def r3(values):
    return [round(v, 3) for v in values] if isinstance(values, (list, tuple)) else round(values, 3)


PLANE_COORDS = {"z": (0, 1), "x": (1, 2), "y": (0, 2)}


def concave_cylinders(solid, axes=("z",)):
    groups = {}
    for face in solid.faces():
        surface = BRepAdaptor_Surface(face.wrapped)
        if surface.GetType() != GeomAbs_Cylinder:
            continue
        cyl = surface.Cylinder()
        ax = cyl.Axis()
        direction = (ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
        axis = next((n for n, i in (("x", 0), ("y", 1), ("z", 2)) if abs(abs(direction[i]) - 1) < 1e-6), None)
        if axis not in axes:
            continue
        loc = (ax.Location().X(), ax.Location().Y(), ax.Location().Z())
        i, j = PLANE_COORDS[axis]
        k = 3 - i - j
        r = cyl.Radius()
        point = face.center()
        p = (point.X, point.Y, point.Z)
        toward = [0.0, 0.0, 0.0]
        toward[i], toward[j] = loc[i] - p[i], loc[j] - p[j]
        if face.normal_at(point).dot(Vector(*toward)) <= 0:
            continue
        bb = face.bounding_box()
        span = (bb.size.X, bb.size.Y, bb.size.Z)[k]
        key = (axis, round(loc[i], 3), round(loc[j], 3), round(r, 3))
        group = groups.setdefault(key, {"axis": axis, "center": (loc[i], loc[j]), "radius": r, "area": 0.0, "height": span})
        group["area"] += face.area
        group["height"] = max(group["height"], span)
    for g in groups.values():
        g["coverage"] = g["area"] / (2 * math.pi * g["radius"] * g["height"]) if g["height"] > 0 else 0
    return list(groups.values())


def revolved_box(profile, ax=0.0, ay=0.0):
    e = profile.extent()
    if e is None:
        return None
    r = max(abs(e[0]), abs(e[2]))
    return [ax - r, ay - r, e[1], ax + r, ay + r, e[3]]


def extrusion_box(f: Extrusion):
    e = f.profile.extent()
    if e is None:
        return None
    lo, hi = sorted((f.offset, f.offset + f.distance))
    if f.plane == "XY":
        return [e[0], e[1], lo, e[2], e[3], hi]
    if f.plane == "XZ":
        return [e[0], lo, e[1], e[2], hi, e[3]]
    return [lo, e[0], e[1], hi, e[2], e[3]]


def expected_bbox(part: Part):
    body = part.base_body
    if isinstance(body, ExtrudedProfile):
        e = body.profile.extent()
        box = None if e is None else [e[0], e[1], 0, e[2], e[3], body.extrude_height]
    else:
        box = revolved_box(body.profile)
    if box is None:
        return None
    for f in part.features:
        add = None
        if isinstance(f, Extrusion) and f.mode == "add":
            add = extrusion_box(f)
        elif isinstance(f, Revolution) and f.mode == "add":
            add = revolved_box(f.profile, *f.axis_position)
        if add is None:
            continue
        box = [min(box[i], add[i]) for i in range(3)] + [max(box[i], add[i]) for i in range(3, 6)]
    return [box[3] - box[0], box[4] - box[1], box[5] - box[2]]


def declared_axes(part: Part) -> list[tuple[str, tuple[float, float]]]:
    axes = []
    if isinstance(part.base_body, RevolvedProfile):
        axes.append(("z", (0.0, 0.0)))
    for f in part.features:
        if isinstance(f, Revolution):
            axes.append(("z", tuple(f.axis_position)))
        elif isinstance(f, (Counterbore, Countersink)):
            axes.append((f.axis, tuple(f.position.value)))
        elif isinstance(f, Extrusion) and f.mode == "cut":
            axis = {"XY": "z", "XZ": "y", "YZ": "x"}[f.plane]
            if f.profile.circle:
                axes.append((axis, tuple(f.profile.circle.center)))
            if f.profile.slot:
                axes += [(axis, tuple(f.profile.slot.center_1)), (axis, tuple(f.profile.slot.center_2))]
            if f.profile.rectangle and f.profile.rectangle.corner_radius > 0:
                axes.append((axis, None))
            if f.profile.segments:
                axes += [(axis, tuple(s.center)) for s in f.profile.segments if isinstance(s, Arc)]
    return axes


def position_hint(expected, measured_candidates):
    ex, ey = expected
    for mx, my in measured_candidates:
        if math.dist((mx, my), (ey, ex)) < LINEAR_TOL and abs(ex - ey) > LINEAR_TOL:
            return "x and y look swapped"
        if math.dist((mx, my), (ex, -ey)) < LINEAR_TOL and abs(ey) > LINEAR_TOL:
            return "y sign looks flipped, check the y direction of the origin"
        if math.dist((mx, my), (-ex, ey)) < LINEAR_TOL and abs(ex) > LINEAR_TOL:
            return "x sign looks flipped, check the x direction of the origin"
    return None


def check_geometry(solid):
    solids = solid.solids()
    closed = all(BRep_Tool.IsClosed_s(sh.wrapped) for sh in solid.shells())
    return [
        check("single_solid", len(solids) == 1, expected=1, measured=len(solids),
              hint=None if len(solids) == 1 else "model falls apart into several bodies, a cut may split it"),
        check("closed_shell", closed, hint=None if closed else "surface has gaps, the solid is not watertight"),
        check("kernel_valid", BRepCheck_Analyzer(solid.wrapped).IsValid()),
        check("positive_volume", solid.volume > 0, measured=r3(solid.volume)),
    ]


def check_bbox(part, solid):
    expected = expected_bbox(part)
    if expected is None:
        return [check("overall_size", True, hint="skipped, profile not fully specified")]
    bb = solid.bounding_box()
    measured = [bb.size.X, bb.size.Y, bb.size.Z]
    deviation = [m - e for m, e in zip(measured, expected)]
    passed = all(abs(d) <= LINEAR_TOL for d in deviation)
    hint = None
    if not passed:
        axes = [a for a, d in zip("xyz", deviation) if abs(d) > LINEAR_TOL]
        hint = f"overall size differs along {', '.join(axes)}"
    return [check("overall_size", passed, expected=r3(expected), measured=r3(measured), deviation=r3(deviation), hint=hint)]


def check_holes(part, cylinders):
    results = []
    holes = [c for c in cylinders if c["coverage"] > 0.98]
    used = set()
    for f in part.features:
        if not isinstance(f, (Hole, ThreadedHole, Counterbore, Countersink)):
            continue
        if isinstance(f, (Counterbore, Countersink)):
            d = screw_values(f)["hole_diameter"]
        else:
            d = f.diameter.value if isinstance(f, Hole) else f.modeled_diameter.value
        x, y = f.position.value
        same_size = [i for i, h in enumerate(holes)
                     if h["axis"] == f.axis and abs(2 * h["radius"] - d) <= LINEAR_TOL and i not in used]
        if not same_size:
            near = min(holes, key=lambda h: math.dist(h["center"], (x, y)), default=None)
            results.append(check("hole_diameter", False, target=f.id, expected=d,
                                 measured=r3(2 * near["radius"]) if near else None,
                                 hint="no hole with this diameter found in the model"))
            continue
        best = min(same_size, key=lambda i: math.dist(holes[i]["center"], (x, y)))
        used.add(best)
        mx, my = holes[best]["center"]
        results.append(check("hole_diameter", True, target=f.id, expected=d, measured=r3(2 * holes[best]["radius"])))
        dev = [mx - x, my - y]
        passed = math.dist((mx, my), (x, y)) <= LINEAR_TOL
        hint = None
        if not passed:
            hint = position_hint((x, y), [holes[i]["center"] for i in same_size]) or \
                f"hole deviates by dx={dev[0]:.3f}, dy={dev[1]:.3f}"
        results.append(check("hole_position", passed, target=f.id, expected=r3([x, y]), measured=r3([mx, my]), deviation=r3(dev), hint=hint))
    declared = declared_axes(part)

    def is_declared(h):
        return any(a == h["axis"] and (c is None or math.dist(c, h["center"]) <= LINEAR_TOL) for a, c in declared)

    extra = [h for i, h in enumerate(holes) if i not in used and not is_declared(h)]
    if extra:
        results.append(check("unexpected_holes", False, measured=[{"diameter": r3(2 * h["radius"]), "center": r3(list(h["center"]))} for h in extra],
                             hint="model has holes that the JSON does not describe"))
    return results


def check_slots(part, cylinders):
    results = []
    for f in part.features:
        if not isinstance(f, Slot):
            continue
        w = f.width.value
        for key in ("arc_center_1", "arc_center_2"):
            x, y = getattr(f, key).value
            match = [c for c in cylinders if c["axis"] == "z" and abs(2 * c["radius"] - w) <= LINEAR_TOL
                     and math.dist(c["center"], (x, y)) <= LINEAR_TOL]
            results.append(check("slot_end", bool(match), target=f"{f.id}.{key}", expected={"width": w, "center": [x, y]},
                                 hint=None if match else "no slot end with this width at this position"))
    return results


def check_step_roundtrip(solid):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "roundtrip.step"
        try:
            export_step(solid, str(path))
            back = import_step(str(path))
        except Exception as e:
            return [check("step_roundtrip", False, hint=f"export or re-import failed: {e}")]
        vol_dev = back.volume - solid.volume
        b0, b1 = solid.bounding_box().size, back.bounding_box().size
        size_dev = max(abs(b0.X - b1.X), abs(b0.Y - b1.Y), abs(b0.Z - b1.Z))
        valid = BRepCheck_Analyzer(back.wrapped).IsValid()
        passed = valid and abs(vol_dev) <= 1e-6 * max(solid.volume, 1) + 1e-6 and size_dev <= 1e-6
        return [check("step_roundtrip", passed, measured={"reimported_valid": valid, "volume_deviation": r3(vol_dev), "size_deviation": r3(size_dev)},
                      hint=None if passed else "re-imported STEP differs from the built model")]


def validate(part: Part, solid) -> dict:
    cylinders = concave_cylinders(solid, axes=("x", "y", "z"))
    checks = check_geometry(solid) + check_bbox(part, solid) + check_holes(part, cylinders) + check_slots(part, cylinders) + check_step_roundtrip(solid)
    failed = [c for c in checks if not c["passed"]]
    return {"passed": not failed, "total": len(checks), "failed": len(failed), "checks": checks}


# ---------------------------------------------------------------- compare with a reference STEP (testing only)

UP_AXIS_TURNS = [(None, 0), (Axis.Y, 90), (Axis.Y, -90), (Axis.X, 90), (Axis.X, -90), (Axis.Y, 180)]


def reference_holes(solid):
    return [{"axis": c["axis"], "d": 2 * c["radius"], "c": c["center"]}
            for c in concave_cylinders(solid, axes=("x", "y", "z")) if c["coverage"] > 0.98]


def candidates(solid):
    for mirrored in (False, True):
        base = solid.mirror(Plane.YZ) if mirrored else solid
        for up_axis, up_angle in UP_AXIS_TURNS:
            tipped = base.rotate(up_axis, up_angle) if up_axis else base
            for angle in (0, 90, 180, 270):
                yield {"tip": f"{up_axis_name(up_axis)}{up_angle}" if up_axis else "none", "rotation_deg": angle,
                       "mirrored": mirrored}, tipped.rotate(Axis.Z, angle) if angle else tipped


def up_axis_name(axis):
    return "X" if axis is Axis.X else "Y"


def sym_diff(a, b):
    try:
        va, vb = a.volume, b.volume
        d = (a - b).volume + (b - a).volume
    except Exception:
        return None
    if not math.isfinite(d) or d < -1e-6 or d < abs(va - vb) - 1e-3 * max(va, vb) or d > va + vb + 1e-3 * max(va, vb):
        return None
    return max(d, 0.0)


def size_of(shape):
    b = shape.bounding_box()
    return (b.size.X, b.size.Y, b.size.Z)


def shifts_for(shape, ref, ref_holes, anchor):
    raw = []
    if anchor:
        i, j = {"z": (0, 1), "x": (1, 2), "y": (0, 2)}[anchor["axis"]]
        k = 3 - i - j
        sb, rb = shape.bounding_box(), ref.bounding_box()
        along = ((rb.min.X, rb.min.Y, rb.min.Z)[k] - (sb.min.X, sb.min.Y, sb.min.Z)[k])
        for h in reference_holes(shape):
            if h["axis"] == anchor["axis"] and abs(h["d"] - anchor["d"]) <= 0.05:
                v = [0.0, 0.0, 0.0]
                v[i], v[j], v[k] = anchor["c"][0] - h["c"][0], anchor["c"][1] - h["c"][1], along
                raw.append(Vector(*v))
    c = ref.center(CenterOf.MASS) - shape.center(CenterOf.MASS)
    raw.append(c)
    sb, rb = shape.bounding_box(), ref.bounding_box()
    raw.append(Vector(rb.min.X - sb.min.X, rb.min.Y - sb.min.Y, rb.min.Z - sb.min.Z))
    out = []
    for s in raw:
        out.append(s)
        out.append(Vector(round(s.X, 2), round(s.Y, 2), round(s.Z, 2)))
    return out


def compare(solid, reference_path: Path, time_limit: float = 90.0) -> dict:
    ref = import_step(str(reference_path))
    rb = ref.bounding_box()
    ref_size = (rb.size.X, rb.size.Y, rb.size.Z)
    ref_holes = reference_holes(ref)
    anchor = max(ref_holes, key=lambda h: h["d"]) if ref_holes else None
    poses = [(pose, shape, max(abs(a - b) for a, b in zip(size_of(shape), ref_size))) for pose, shape in candidates(solid)]
    close = [p for p in poses if p[2] <= max(1.0, 0.02 * max(ref_size))]
    if not close:
        close = sorted(poses, key=lambda p: p[2])[:4]
    best = None
    start = time.monotonic()
    timed_out = False
    for pose, shape, _ in sorted(close, key=lambda p: p[2]):
        if time.monotonic() - start > time_limit:
            timed_out = True
            break
        for shift in shifts_for(shape, ref, ref_holes, anchor):
            if time.monotonic() - start > time_limit:
                timed_out = True
                break
            try:
                moved = shape.translate(shift)
            except Exception:
                continue
            diff = sym_diff(moved, ref)
            if diff is None:
                continue
            if best is None or diff < best[0]:
                best = (diff, pose, moved)
    if best is None:
        raise ValueError("timed out before any valid comparison" if timed_out
                         else "no pose gave a valid boolean comparison with the reference")
    diff, pose, moved = best
    mb = moved.bounding_box()
    got_holes = reference_holes(moved)
    matched, used = [], set()
    for h in sorted(ref_holes, key=lambda h: -h["d"]):
        options = [(i, g) for i, g in enumerate(got_holes)
                   if i not in used and g["axis"] == h["axis"] and abs(g["d"] - h["d"]) <= 0.05]
        if not options:
            matched.append({"reference_d": round(h["d"], 3), "axis": h["axis"], "found": False})
            continue
        i, g = min(options, key=lambda o: math.dist(o[1]["c"], h["c"]))
        used.add(i)
        matched.append({"reference_d": round(h["d"], 3), "axis": h["axis"], "found": True,
                        "position_error": round(math.dist(g["c"], h["c"]), 3)})
    holes_ok = all(m["found"] and m["position_error"] <= 0.05 for m in matched) and len(got_holes) == len(ref_holes)
    size_dev = [round(a - b, 3) for a, b in ((mb.size.X, rb.size.X), (mb.size.Y, rb.size.Y), (mb.size.Z, rb.size.Z))]
    return {
        "reference": str(reference_path),
        "best_pose": pose,
        "volume": round(moved.volume, 2),
        "reference_volume": round(ref.volume, 2),
        "differing_volume": round(diff, 3),
        "differing_volume_pct": round(100 * diff / ref.volume, 3),
        "size_deviation": size_dev,
        "holes_expected": len(ref_holes),
        "holes_found": len(got_holes),
        "holes": matched,
        "holes_ok": holes_ok,
        "match": diff <= 0.001 * ref.volume and holes_ok,
        "timed_out": timed_out,
    }


# ---------------------------------------------------------------- spec check before building

def check_spec(data: dict) -> tuple[list[str], object]:
    """Schema, consistency rules and a test build. Returns (problems, solid or None)."""
    try:
        part = Part.model_validate(data)
    except ValidationError as e:
        return [f"schema: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:15]], None
    issues = part.consistency_issues()
    if issues:
        return issues, None
    try:
        return [], build(part)
    except BuildError as e:
        return [f"the model could not be built ({e.code}) at {e.target}: {e.message}"], None
    except Exception as e:
        return [f"the model could not be built: {type(e).__name__}: {e}. Check that every profile closes, does not "
                f"cross itself, and that arcs run in the right direction"], None


# ---------------------------------------------------------------- build worker (called by mcp_server.py)

def limit_memory(megabytes: int):
    if megabytes <= 0:
        return
    try:
        import resource
        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, ValueError, OSError):
        pass


def worker():
    request = json.loads(sys.stdin.read())
    limit_memory(request.get("memory_mb", 0))
    result = {"ok": False}
    try:
        part = Part.model_validate(request["part"])
        spec = Part.model_validate(request["spec"]) if request.get("spec") else part
        solid = build(part)
        bb = solid.bounding_box()
        holes = [c for c in concave_cylinders(solid) if c["coverage"] > 0.98]
        result.update(
            ok=True,
            volume=round(solid.volume, 3),
            bbox_size=[round(bb.size.X, 3), round(bb.size.Y, 3), round(bb.size.Z, 3)],
            bbox_min=[round(bb.min.X, 3), round(bb.min.Y, 3), round(bb.min.Z, 3)],
            holes_found=[{"diameter": round(2 * h["radius"], 3), "center": [round(h["center"][0], 3), round(h["center"][1], 3)]} for h in holes],
            validation=validate(spec, solid),
        )
        if request.get("export"):
            export_step(solid, request["export"])
            result["step_file"] = request["export"]
    except BuildError as e:
        result["error"] = e.to_dict()
    except Exception as e:
        result["error"] = {"code": type(e).__name__, "message": str(e), "target": None}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    worker()
