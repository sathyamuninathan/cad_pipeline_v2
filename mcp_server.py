"""MCP server "cad-builder": the structured CAD tools an AI uses to build a part (FastMCP, stdio).

Every change is built in a separate `cad.py` process with a time limit, validated, and rolled back if it fails.
Run it locally with `py mcp_server.py`, or in the Docker sandbox (see DOCKER_ARGS and .mcp.json).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

import standards
from spec import Part, Profile, Segment

HERE = Path(__file__).parent.resolve()
OUTPUT_DIR = Path(os.environ.get("CAD_OUTPUT_DIR", HERE / "out"))
TIMEOUT_S = float(os.environ.get("CAD_BUILD_TIMEOUT", 60))
MEMORY_MB = int(os.environ.get("CAD_BUILD_MEMORY_MB", 0))
DOCKER_IMAGE = "cad-mcp-v2"
DOCKER_ARGS = [
    "run", "-i", "--rm",
    "--network", "none",
    "--read-only", "--tmpfs", "/tmp:size=256m",
    "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "-v", f"{HERE / 'out'}:/out",
    DOCKER_IMAGE,
]


def launch_command(docker: bool) -> tuple[str, list[str]]:
    """How a client starts this server: in the Docker sandbox, or as a local Python process."""
    if docker:
        (HERE / "out").mkdir(exist_ok=True)
        return "docker", DOCKER_ARGS
    return sys.executable, [str(HERE / "mcp_server.py")]

mcp = FastMCP(
    "cad-builder",
    instructions=(
        "Builds simple 3D parts from technical drawings. Units are millimetres. "
        "Workflow: new_part -> (optional) load_drawing_spec -> build_from_spec, or step by step: create_base_body (flat parts) or create_turned_body "
        "(lathe parts) -> add_extrusion / add_revolution / add_hole / add_threaded_hole / add_slot / add_counterbore / "
        "add_countersink / add_keyway / add_retaining_groove -> validate -> export_step. "
        "lookup_standard returns ISO/DIN values (threads, clearance holes, counterbores, keyways, retaining-ring grooves, "
        "fits, general tolerances); features that name a standard (screw size, shaft diameter) fill missing sizes from these tables. "
        "Features are applied in the order they were added. Calling an add_ tool with an existing id replaces that feature. "
        "Every tool returns the validation result; fix failed checks using their hint before exporting."
    ),
    log_level="WARNING",
)

state: dict = {"part": None, "spec": None, "history": []}


def blank_part(part_id: str, origin: str) -> dict:
    return {
        "schema_version": "0.1",
        "part_id": part_id,
        "drawing_file": "",
        "units": "mm",
        "coordinate_system": {"origin": origin, "x": "right in top view", "y": "up in top view", "z": "thickness, bottom face at z=0"},
        "base_body": None,
        "features": [],
    }


def v(value) -> dict:
    return {"value": value}


def run_worker(export: str | None = None) -> dict:
    request = {"part": state["part"], "spec": state["spec"], "export": export, "memory_mb": MEMORY_MB}
    try:
        proc = subprocess.run(
            [sys.executable, str(HERE / "cad.py")],
            input=json.dumps(request), capture_output=True, text=True, timeout=TIMEOUT_S, cwd=HERE,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "timeout", "message": f"build exceeded {TIMEOUT_S:g} s and was stopped", "target": None}}
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = proc.stderr.strip()[-800:] or "no error output"
        return {"ok": False, "error": {"code": "worker_crashed", "message": f"exit code {proc.returncode}: {detail}", "target": None}}
    return json.loads(proc.stdout)


def summarise(result: dict) -> dict:
    out = {k: result[k] for k in ("ok", "error", "volume", "bbox_size", "holes_found") if k in result}
    validation = result.get("validation")
    if validation:
        out["validation"] = {
            "passed": validation["passed"],
            "failed_checks": [c for c in validation["checks"] if not c["passed"]],
            "checks_run": validation["total"],
        }
    return out


def require_part():
    if state["part"] is None:
        raise ValueError("no part started, call new_part first")


def apply(operation: str, change) -> dict:
    require_part()
    candidate = json.loads(json.dumps(state["part"]))
    change(candidate)
    try:
        if candidate["base_body"] is not None:
            Part.model_validate(candidate)
    except ValidationError as e:
        return {"ok": False, "operation": operation, "error": {"code": "invalid_parameters", "message": str(e), "target": None}}
    previous = state["part"]
    state["part"] = candidate
    if candidate["base_body"] is None:
        state["history"].append(operation)
        return {"ok": True, "operation": operation, "note": "stored; nothing to build until create_base_body is called"}
    result = summarise(run_worker())
    if result.get("error"):
        state["part"] = previous
        return {"operation": operation, **result, "note": "this change was not applied; the part is unchanged"}
    state["history"].append(operation)
    return {"operation": operation, **result}


def upsert_feature(feature: dict):
    def change(part):
        part["features"] = [f for f in part["features"] if f["id"] != feature["id"]] + [feature]
    return change


@mcp.tool()
def new_part(part_id: str, origin_description: str = "lower-left corner of the part in the top view, bottom face") -> dict:
    """Start a new, empty part. Discards the previous part. origin_description says where x=0, y=0, z=0 is."""
    state.update(part=blank_part(part_id, origin_description), spec=None, history=[f"new_part {part_id}"])
    return {"ok": True, "part_id": part_id}


@mcp.tool()
def load_drawing_spec(spec_json: str) -> dict:
    """Load the part description extracted from the drawing (JSON matching part.schema.json).
    Validation then compares the built model against this spec instead of against the tool calls."""
    try:
        data = json.loads(spec_json)
        filled = standards.fill(data) if isinstance(data, dict) else []
        spec = Part.model_validate(data)
    except (ValidationError, ValueError) as e:
        return {"ok": False, "error": {"code": "invalid_spec", "message": str(e), "target": None}}
    state["spec"] = json.loads(spec.model_dump_json(by_alias=True))
    checked = standards.check(data)
    return {
        "ok": True,
        "part_id": spec.part_id,
        "consistency_issues": spec.consistency_issues(),
        "review_flags": [f.model_dump() for f in spec.review_flags],
        "filled_from_standards": filled,
        "standards_notes": checked["notes"],
        "tolerances": checked["tolerances"],
    }


@mcp.tool()
def build_from_spec() -> dict:
    """Build the whole part directly from the loaded drawing spec (base body and all features, in order).
    Use this first; then fix failed checks with the add_ / remove_feature tools."""
    require_part()
    if state["spec"] is None:
        return {"ok": False, "error": {"code": "no_spec", "message": "call load_drawing_spec first", "target": None}}
    spec = state["spec"]

    def change(part):
        for key in ("drawing_file", "coordinate_system", "base_body", "features", "scripts"):
            if key in spec:
                part[key] = json.loads(json.dumps(spec[key]))
    return apply("build_from_spec", change)


@mcp.tool()
def create_base_body(thickness: float, segments: list[Segment] | None = None, profile: Profile | None = None) -> dict:
    """Create the base body of a flat part: a closed outline in the XY plane (mm), extruded from z=0 up to z=thickness.
    Give either segments or profile.
    segments is an ordered, closed chain: each segment starts where the previous one ends.
    line: {"type":"line","from":[x,y],"to":[x,y]}
    arc:  {"type":"arc","center":[x,y],"radius":r,"from":[x,y],"to":[x,y],"clockwise":false}
    Use clockwise=false for convex round ends and clockwise=true for concave fillets when the outline runs counter-clockwise.
    profile is a shortcut: {"rectangle": {"center":[x,y], "size":[w,h], "corner_radius":r}} or {"circle": {...}}."""
    if (segments is None) == (profile is None):
        return {"ok": False, "error": {"code": "invalid_parameters", "message": "give either segments or profile", "target": None}}
    if profile is not None:
        outline = json.loads(profile.model_dump_json(by_alias=True, exclude_none=True))
    else:
        outline = {"segments": [json.loads(s.model_dump_json(by_alias=True, exclude_none=True)) for s in segments]}
    body = {"type": "extruded_profile", "extrude_height": thickness, "profile": outline}
    return apply(f"create_base_body thickness={thickness}", lambda p: p.update(base_body=body))


@mcp.tool()
def create_turned_body(segments: list[Segment]) -> dict:
    """Create the base body of a lathe part by rotating a half-profile 360 degrees around the Z axis.
    Coordinates are [radius, z] in mm, radius >= 0. The chain is closed and usually runs along the axis (radius 0)
    on one side, e.g. a stepped shaft: [0,0]->[17.5,0]->[17.5,35]->[22.5,35]->[22.5,100]->[0,100]->[0,0].
    A central bore can be part of the profile (inner radius > 0 instead of 0). Arcs as in create_base_body."""
    body = {"type": "revolved_profile",
            "profile": {"segments": [json.loads(s.model_dump_json(by_alias=True, exclude_none=True)) for s in segments]}}
    return apply("create_turned_body", lambda p: p.update(base_body=body))


@mcp.tool()
def add_extrusion(id: str, mode: Literal["add", "cut"], profile: Profile, distance: float,
                  plane: Literal["XY", "XZ", "YZ"] = "XY", offset: float = 0) -> dict:
    """Add material (boss, step, raised ring) or cut it away (pocket, recess, keyway) by extruding a 2D profile.
    plane: XY (profile coordinates are [x, y], extrudes along Z), XZ ([x, z], along Y) or YZ ([y, z], along X).
    offset: position of the sketch plane along that axis. distance: signed length along the axis, e.g. offset=19,
    distance=10 extrudes from z=19 up to z=29; offset=8, distance=-3 cuts 3 mm down from z=8.
    profile: exactly one of {"circle": {"center":[a,b], "radius":r}}, {"rectangle": {"center":[a,b], "size":[w,h],
    "corner_radius":r}}, {"slot": {"center_1":[a,b], "center_2":[a,b], "width":w}} or {"segments": [...]}."""
    feature = {"id": id, "type": "extrusion", "mode": mode, "plane": plane, "offset": offset, "distance": distance,
               "profile": json.loads(profile.model_dump_json(by_alias=True, exclude_none=True))}
    return apply(f"add_extrusion {id} {mode} on {plane}", upsert_feature(feature))


@mcp.tool()
def add_revolution(id: str, mode: Literal["add", "cut"], segments: list[Segment], axis_x: float = 0,
                   axis_y: float = 0) -> dict:
    """Add or cut a turned shape around a vertical axis through (axis_x, axis_y): e.g. a round boss with a fillet
    at its base, a counterbore, a groove. segments: closed half-profile in [radius, z] coordinates, as in
    create_turned_body."""
    feature = {"id": id, "type": "revolution", "mode": mode, "axis_position": [axis_x, axis_y],
               "profile": {"segments": [json.loads(s.model_dump_json(by_alias=True, exclude_none=True)) for s in segments]}}
    return apply(f"add_revolution {id} {mode} at ({axis_x},{axis_y})", upsert_feature(feature))


@mcp.tool()
def add_hole(id: str, diameter: float, x: float, y: float, through: bool = True, depth: float | None = None,
             chamfer: float | None = None, axis: Literal["x", "y", "z"] = "z",
             from_side: Literal["max", "min"] = "max") -> dict:
    """Drill a plain round hole. Default: along Z, downward from the top of the part, center at (x, y).
    For other directions set axis; then (x, y) means the two other coordinates: axis "x" -> (y, z), axis "y" -> (x, z).
    from_side "max" starts at the highest coordinate along the axis, "min" at the lowest.
    through=true cuts the whole part; otherwise give depth. chamfer = 45-degree chamfer on both hole edges (Z holes only)."""
    feature = {"id": id, "type": "hole", "through": through, "diameter": v(diameter), "position": v([x, y]),
               "axis": axis, "from_side": from_side}
    if depth is not None:
        feature["depth"] = v(depth)
    if chamfer:
        feature["chamfer"] = v(chamfer)
    return apply(f"add_hole {id} d={diameter} at ({x},{y}) axis {axis}", upsert_feature(feature))


@mcp.tool()
def add_threaded_hole(id: str, thread: str, x: float, y: float, through: bool = True, depth: float | None = None,
                      axis: Literal["x", "y", "z"] = "z", from_side: Literal["max", "min"] = "max") -> dict:
    """Add a threaded hole such as 'M8' or 'M10x0.75'. It is modelled as the tap-drill core hole (M8 -> 6.8 mm),
    looked up automatically. Position, axis and from_side work as in add_hole."""
    try:
        drill, evidence = standards.tap_drill(thread)
    except ValueError as e:
        return {"ok": False, "error": {"code": "unknown_thread", "message": str(e), "target": id}}
    feature = {"id": id, "type": "threaded_hole", "through": through, "thread": v(thread),
               "modeled_diameter": {"value": drill, "evidence": evidence}, "position": v([x, y]),
               "axis": axis, "from_side": from_side}
    if depth is not None:
        feature["depth"] = v(depth)
    return apply(f"add_threaded_hole {id} {thread} at ({x},{y}) axis {axis}", upsert_feature(feature))


@mcp.tool()
def add_slot(id: str, width: float, x1: float, y1: float, x2: float, y2: float, through: bool = True,
             depth: float | None = None) -> dict:
    """Cut a straight slot with round ends downward from the top face.
    (x1,y1) and (x2,y2) are the centers of the two round ends (mm); width = slot width."""
    feature = {"id": id, "type": "slot", "through": through, "width": v(width),
               "arc_center_1": v([x1, y1]), "arc_center_2": v([x2, y2])}
    if depth is not None:
        feature["depth"] = v(depth)
    return apply(f"add_slot {id} w={width}", upsert_feature(feature))


@mcp.tool()
def lookup_standard(query: str) -> dict:
    """Look up ISO/DIN values. Examples: 'M8', 'M10x1', 'identify 6.6' (which standard hole has this diameter),
    'clearance M8 fine', 'counterbore M6', 'countersink M6', 'keyway 35', 'groove shaft 20', 'groove bore 30',
    'undercut 1x0.4', 'splitpin 5', 'fit 20 H7/g6', 'fit 20 H7', 'tol 45 m' (ISO 2768-1 class f/m/c/v)."""
    try:
        return {"ok": True, "result": standards.lookup(query.split())}
    except (standards.StandardError, IndexError, ValueError) as e:
        return {"ok": False, "error": {"code": "lookup_failed", "message": str(e), "target": query}}


def optional_value(x):
    return None if x is None else v(x)


@mcp.tool()
def add_counterbore(id: str, x: float, y: float, screw: str | None = None, hole_diameter: float | None = None,
                    counterbore_diameter: float | None = None, counterbore_depth: float | None = None,
                    series: Literal["fine", "medium", "coarse"] = "medium", through: bool = True, depth: float | None = None,
                    axis: Literal["x", "y", "z"] = "z", from_side: Literal["max", "min"] = "max") -> dict:
    """Counterbored hole for a socket head screw (ISO 4762). Give the screw (e.g. 'M6') and any sizes written on the
    drawing; missing sizes come from ISO 273 (hole) and DIN 974-1 (counterbore). Position, axis and from_side as in add_hole."""
    feature = {"id": id, "type": "counterbore", "series": series, "position": v([x, y]), "through": through,
               "axis": axis, "from_side": from_side}
    for key, val in (("screw", screw), ("hole_diameter", hole_diameter), ("counterbore_diameter", counterbore_diameter),
                     ("counterbore_depth", counterbore_depth), ("depth", depth)):
        if val is not None:
            feature[key] = v(val)
    standards.fill({"features": [feature]})
    return apply(f"add_counterbore {id} {screw or ''} at ({x},{y}) axis {axis}", upsert_feature(feature))


@mcp.tool()
def add_countersink(id: str, x: float, y: float, screw: str | None = None, hole_diameter: float | None = None,
                    countersink_diameter: float | None = None, angle_deg: float = 90,
                    series: Literal["fine", "medium", "coarse"] = "medium", through: bool = True, depth: float | None = None,
                    axis: Literal["x", "y", "z"] = "z", from_side: Literal["max", "min"] = "max") -> dict:
    """Countersunk hole (cone at the entry) for a flat head screw (ISO 10642, 90 degrees). Give the screw (e.g. 'M6')
    and any sizes written on the drawing; missing sizes come from the tables (countersink diameter = head + 0.3 mm,
    flag it if the drawing does not give it)."""
    feature = {"id": id, "type": "countersink", "series": series, "position": v([x, y]), "through": through,
               "angle_deg": angle_deg, "axis": axis, "from_side": from_side}
    for key, val in (("screw", screw), ("hole_diameter", hole_diameter), ("countersink_diameter", countersink_diameter),
                     ("depth", depth)):
        if val is not None:
            feature[key] = v(val)
    standards.fill({"features": [feature]})
    return apply(f"add_countersink {id} {screw or ''} at ({x},{y}) axis {axis}", upsert_feature(feature))


@mcp.tool()
def add_keyway(id: str, shaft_diameter: float, length: float, z_start: float, width: float | None = None,
               depth: float | None = None, form: Literal["A", "B"] = "A", location: Literal["shaft", "hub"] = "shaft",
               angle_deg: float = 0, axis_x: float = 0, axis_y: float = 0) -> dict:
    """Keyway (Passfedernut, DIN 6885-1) on a shaft or in a hub bore whose axis is the Z axis through (axis_x, axis_y).
    The keyway runs along Z from z_start to z_start + length on the side given by angle_deg (0 = +X, 90 = +Y).
    form A = round ends, B = square ends. width and depth (t1 on a shaft, t2 in a hub) come from DIN 6885-1 when omitted.
    For a hub, shaft_diameter is the bore diameter."""
    feature = {"id": id, "type": "keyway", "location": location, "form": form, "shaft_diameter": v(shaft_diameter),
               "length": v(length), "z_start": v(z_start), "angle_deg": angle_deg, "axis_position": [axis_x, axis_y]}
    for key, val in (("width", width), ("depth", depth)):
        if val is not None:
            feature[key] = v(val)
    standards.fill({"features": [feature]})
    return apply(f"add_keyway {id} d={shaft_diameter} l={length}", upsert_feature(feature))


@mcp.tool()
def add_retaining_groove(id: str, nominal_diameter: float, z_start: float, location: Literal["shaft", "bore"] = "shaft",
                         groove_diameter: float | None = None, width: float | None = None,
                         axis_x: float = 0, axis_y: float = 0) -> dict:
    """Groove for a retaining ring (DIN 471 on a shaft, DIN 472 in a bore) around the Z axis through (axis_x, axis_y),
    from z_start to z_start + width. groove_diameter and width come from the table when omitted."""
    feature = {"id": id, "type": "retaining_groove", "location": location, "nominal_diameter": v(nominal_diameter),
               "z_start": v(z_start), "axis_position": [axis_x, axis_y]}
    for key, val in (("groove_diameter", groove_diameter), ("width", width)):
        if val is not None:
            feature[key] = v(val)
    standards.fill({"features": [feature]})
    return apply(f"add_retaining_groove {id} {location} d={nominal_diameter} at z={z_start}", upsert_feature(feature))


@mcp.tool()
def run_script(id: str, description: str, code: str) -> dict:
    """FALLBACK ONLY: use when no other tool can make the geometry (e.g. a draft angle or an angled boss).
    Python code using build123d and math. The current solid is in the variable 'model'; the script must
    assign the new solid back to 'model', e.g.:  model = model - Pos(10, 5, 0) * Cylinder(3, 20)
    Only build123d and math can be imported; files, network and system access are blocked. Runs after all
    other features, in the sandbox, with the same time limit. Calling again with the same id replaces it."""
    script = {"id": id, "description": description, "code": code}

    def change(part):
        part["scripts"] = [s for s in part.get("scripts", []) if s["id"] != id] + [script]
    return apply(f"run_script {id}: {description}", change)


@mcp.tool()
def remove_feature(id: str) -> dict:
    """Remove a feature (hole, slot, keyway, groove, extrusion, ...) or a script by id."""
    require_part()
    features, scripts = state["part"]["features"], state["part"].get("scripts", [])
    if not any(f["id"] == id for f in features + scripts):
        return {"ok": False, "error": {"code": "unknown_feature", "message": f"no feature or script with id '{id}'", "target": id}}
    return apply(f"remove_feature {id}", lambda p: p.update(
        features=[f for f in p["features"] if f["id"] != id],
        scripts=[s for s in p.get("scripts", []) if s["id"] != id]))


@mcp.tool()
def get_model_info() -> dict:
    """Current model: size, volume, holes actually found in the solid, the operation history and the part JSON."""
    require_part()
    info = {"history": state["history"], "part": state["part"], "spec_loaded": state["spec"] is not None}
    if state["part"]["base_body"] is not None:
        info.update(summarise(run_worker()))
    return info


@mcp.tool()
def validate() -> dict:
    """Run all checks on the current model and return the full report: closed valid solid, overall size,
    hole diameters and positions, slots, STEP round trip. Compares against the drawing spec if one is loaded."""
    require_part()
    if state["part"]["base_body"] is None:
        return {"ok": False, "error": {"code": "no_base_body", "message": "call create_base_body first", "target": None}}
    result = run_worker()
    return {k: result[k] for k in ("ok", "error", "validation") if k in result}


@mcp.tool()
def export_step(filename: str | None = None) -> dict:
    """Write the current model as a STEP file into the output folder and return its path and the validation result."""
    require_part()
    if state["part"]["base_body"] is None:
        return {"ok": False, "error": {"code": "no_base_body", "message": "call create_base_body first", "target": None}}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(filename or f"{state['part']['part_id']}.step").name
    path = OUTPUT_DIR / name
    result = summarise(run_worker(export=str(path))) | {"step_file": str(path)}
    (OUTPUT_DIR / f"{Path(name).stem}.part.json").write_text(json.dumps(state["part"], indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    mcp.run()
