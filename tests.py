"""All tests in one file. No API calls: the AI model is replaced by a small fake server.

    py tests.py                    run every test (MCP server as a local process)
    py tests.py --docker           the MCP server test runs in the Docker sandbox instead
    py tests.py schema validate    run only these tests

Needs ..\\answer_keys (example part JSONs) and ..\\Drawing2CAD\\Drawing2CAD (drawings and reference STEP files).
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

os.environ.setdefault("CAD_BUILD_TIMEOUT", "20")
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

HERE = Path(__file__).parent.resolve()
KEYS = HERE.parent / "answer_keys"
DATA = HERE.parent / "Drawing2CAD" / "Drawing2CAD"
failures: list[str] = []


def result(ok: bool, name: str, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def key(name: str) -> dict:
    return json.loads((KEYS / f"{name}.json").read_text(encoding="utf-8"))


def v(x):
    return {"value": x}


def r(x):
    return {"value": x, "status": "read", "source": "test", "confidence": 1}


def line(a, b):
    return {"type": "line", "from": a, "to": b}


class FakeModel(BaseHTTPRequestHandler):
    """OpenAI-compatible stand-in. Subclasses set `models` and implement answer(request) -> (message, usage)."""
    models: list[str] = []

    def log_message(self, *a):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.reply({"data": [{"id": m, "pricing": {"prompt": "0.000002", "completion": "0.00001"}} for m in self.models]})

    def do_POST(self):
        message, usage = self.answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.reply({"choices": [{"message": message}], "usage": usage})


def start(handler) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


# ---------------------------------------------------------------- part description

def test_schema():
    from pydantic import ValidationError
    from spec import Part
    for path in sorted(KEYS.glob("*.json")):
        part = Part.model_validate_json(path.read_text(encoding="utf-8"))
        issues = part.consistency_issues()
        result(not issues, f"answer key {path.name} is valid", "; ".join(issues))

    base = key("Sensoraufnahme")
    bad_cases = {
        "assumed value without review flag": lambda d: d["dimensions"][2].pop("review_flag"),
        "confidence above 1": lambda d: d["dimensions"][0].update(confidence=1.5),
        "unknown feature type": lambda d: d["features"][0].update(type="laser_engraving"),
        "duplicate id": lambda d: d["dimensions"][1].update(id="overall_length"),
        "negative thickness": lambda d: d["base_body"].update(extrude_height=-5),
        "unknown field": lambda d: d.update(colour="red"),
    }
    for name, mutate in bad_cases.items():
        data = copy.deepcopy(base)
        mutate(data)
        try:
            Part.model_validate(data)
            result(False, f"rejected: {name}", "was accepted")
        except ValidationError:
            result(True, f"rejected: {name}")

    data = copy.deepcopy(base)
    data["base_body"]["profile"]["segments"][0]["to"] = [38, -10]
    result(any("gap" in i for i in Part.model_validate(data).consistency_issues()), "gap in outline detected")
    data = copy.deepcopy(base)
    data["dimensions"][0].update(confidence=0.5)
    result(any("below" in i for i in Part.model_validate(data).consistency_issues()), "low confidence detected")

    schema = json.dumps(Part.model_json_schema(by_alias=True), indent=2)
    path = HERE / "part.schema.json"
    current = path.exists() and path.read_text(encoding="utf-8") == schema
    if not current:
        path.write_text(schema, encoding="utf-8")
    result(True, "part.schema.json " + ("is up to date" if current else "was regenerated"))


# ---------------------------------------------------------------- validation catches mistakes

def test_validate():
    from build123d import Box, Location
    from cad import build, validate
    from spec import Part
    hebel = Part.model_validate(key("3-Loch-Hebel"))
    sensor = Part.model_validate(key("Sensoraufnahme"))
    hebel_solid, sensor_solid = build(hebel), build(sensor)

    def variant(part, change):
        data = copy.deepcopy(part.model_dump(by_alias=True))
        change(data)
        return Part.model_validate(data)

    def feature(data, fid):
        return next(f for f in data["features"] if f["id"] == fid)

    def expect(name, report, check, hint_word=None):
        hits = [c for c in report["checks"] if c["check"] == check and not c["passed"]]
        ok = bool(hits) and not report["passed"] and (hint_word is None or any(hint_word in c.get("hint", "") for c in hits))
        result(ok, f"{name} caught by '{check}'", hits[0].get("hint") if hits else "")

    result(validate(hebel, hebel_solid)["passed"], "correct lever passes")
    result(validate(sensor, sensor_solid)["passed"], "correct sensor passes")
    expect("hole x and y swapped", validate(variant(hebel, lambda d: feature(d, "hole_C")["position"].update(value=[40.1, 51.8])), hebel_solid), "hole_position", "swapped")
    expect("hole x sign flipped", validate(variant(hebel, lambda d: feature(d, "hole_A")["position"].update(value=[58.2, 46.7])), hebel_solid), "hole_position", "flipped")
    expect("hole moved", validate(variant(hebel, lambda d: feature(d, "hole_C")["position"].update(value=[51.8, 42.1])), hebel_solid), "hole_position", "dy=")
    expect("wrong hole diameter", validate(variant(hebel, lambda d: feature(d, "hole_B")["diameter"].update(value=11)), hebel_solid), "hole_diameter")
    expect("wrong thickness", validate(variant(hebel, lambda d: d["base_body"].update(extrude_height=9)), hebel_solid), "overall_size", "z")
    expect("hole missing in model", validate(sensor, build(variant(sensor, lambda d: d["features"].pop(1)))), "hole_diameter")
    expect("extra hole in model", validate(variant(sensor, lambda d: d["features"].pop(1)), sensor_solid), "unexpected_holes")
    expect("slot in wrong place", validate(variant(sensor, lambda d: feature(d, "slot_1")["arc_center_1"].update(value=[6, 0])), sensor_solid), "slot_end")
    expect("model in two pieces", validate(sensor, sensor_solid + Box(5, 5, 5).locate(Location((100, 0, 0)))), "single_solid")


# ---------------------------------------------------------------- ISO/DIN tables and the features built from them

def test_standards():
    import standards
    from cad import build, validate
    from spec import Part

    def approx(name, a, b, tol=1e-6):
        result(abs(a - b) <= tol, name, "" if abs(a - b) <= tol else f"{a} != {b}")

    approx("M8 tap drill", standards.thread("M8")["tap_drill"], 6.8)
    approx("M10x1 tap drill", standards.thread("M10x1")["tap_drill"], 9.0)
    approx("M8-6H tap drill", standards.thread("M8-6H")["tap_drill"], 6.8)
    approx("M6 clearance", standards.clearance_hole("M6")["diameter"], 6.6)
    approx("M8 fine clearance", standards.clearance_hole("M8", "fine")["diameter"], 8.4)
    approx("M6 counterbore", standards.counterbore("M6")["counterbore_diameter"], 11.0)
    result(standards.keyway(35)["b"] == 10 and standards.keyway(38)["b"] == 10 and standards.keyway(38.5)["b"] == 12, "keyway widths")
    approx("groove shaft 20", standards.retaining_groove(20, "shaft")["d2_groove"], 19.0)
    approx("groove bore 30", standards.retaining_groove(30, "bore")["d2_groove"], 31.4)
    approx("20 H7 upper", standards.fit(20, "H7")["upper_deviation"], 0.021)
    approx("20 H7 lower", standards.fit(20, "H7")["lower_deviation"], 0.0)
    approx("20 g6 upper", standards.fit(20, "g6")["upper_deviation"], -0.007)
    approx("20 g6 lower", standards.fit(20, "g6")["lower_deviation"], -0.020)
    approx("30 k6", standards.fit(30, "k6")["lower_deviation"], 0.002)
    approx("30.5 k6", standards.fit(30.5, "k6")["lower_deviation"], 0.002)
    approx("10 js7", standards.fit(10, "js7")["upper_deviation"], 0.0075)
    result(standards.fit_pair(20, "H7/p6")["fit_type"] == "interference", "H7/p6 is interference")
    result(standards.fit_pair(25, "H7/k6")["fit_type"] == "transition", "H7/k6 is transition")
    approx("ISO 2768 m 45", standards.general_tolerance(45, "m")["plus_minus"], 0.3)
    approx("ISO 2768 m 30", standards.general_tolerance(30, "m")["plus_minus"], 0.2)
    approx("+0,1/-0,05", standards.tolerance(25, "+0,1/-0,05")["min"], 24.95)
    result("ISO 273 medium clearance hole for M6" in standards.identify(6.6), "identify 6.6")
    for name, bad in (("M7", lambda: standards.thread("M7")), ("fit P7", lambda: standards.fit(20, "P7")),
                      ("keyway 200", lambda: standards.keyway(200))):
        try:
            bad()
            result(False, f"{name} rejected")
        except standards.StandardError:
            result(True, f"{name} rejected")

    plate = {
        "schema_version": "0.1", "part_id": "plate", "drawing_file": "test.pdf", "units": "mm",
        "general_tolerance": "ISO 2768-mK",
        "coordinate_system": {"origin": "corner"},
        "base_body": {"type": "extruded_profile", "profile": {"rectangle": {"center": [40, 20], "size": [80, 40]}}, "extrude_height": 15},
        "dimensions": [{"id": "width", "value": 80, "status": "read", "tolerance": "±0.1"},
                       {"id": "bore", "value": 20, "status": "read", "tolerance": "H7"}],
        "features": [
            {"id": "cb", "type": "counterbore", "screw": r("M6"), "position": r([15, 20])},
            {"id": "cs", "type": "countersink", "screw": r("M6"), "position": r([40, 20])},
            {"id": "th", "type": "threaded_hole", "thread": r("M8"), "position": r([65, 20]), "through": True},
            {"id": "side", "type": "counterbore", "screw": r("M4"), "position": r([5, 7.5]), "axis": "x", "from_side": "min",
             "through": False, "depth": r(20)},
        ],
    }
    log = standards.fill(plate)
    result(any("cb.counterbore_diameter = 11" in x for x in log), "fill counterbore from table")
    result(plate["features"][2]["modeled_diameter"]["value"] == 6.8, "fill tap drill from table")
    part = Part.model_validate(plate)
    solid = build(part)
    result(validate(part, solid)["passed"], "plate with screw holes validates")
    cb = math.pi * 3.3 ** 2 * 15 + math.pi * (5.5 ** 2 - 3.3 ** 2) * 6.4
    h = (13.74 - 6.6) / 2
    cs = math.pi * 3.3 ** 2 * 15 + math.pi * h / 3 * (6.87 ** 2 + 6.87 * 3.3 + 3.3 ** 2) - math.pi * 3.3 ** 2 * h
    th = math.pi * 3.4 ** 2 * 15
    side = math.pi * 2.25 ** 2 * 20 + math.pi * (4 ** 2 - 2.25 ** 2) * 4.4
    approx("plate volume", solid.volume, 80 * 40 * 15 - cb - cs - th - side, 0.05)
    checked = standards.check(plate)
    fields = {t["field"]: t for t in checked["tolerances"]}
    result(abs(fields["bore"]["max"] - 20.021) < 1e-9 and abs(fields["width"]["min"] - 79.9) < 1e-9, "tolerances resolved")
    result(checked["general_tolerance"]["class"] == "m", "general tolerance class")

    shaft = {
        "schema_version": "0.1", "part_id": "shaft", "drawing_file": "test.pdf", "units": "mm",
        "coordinate_system": {"origin": "axis"},
        "base_body": {"type": "revolved_profile", "profile": {"segments": [
            line([0, 0], [20, 0]), line([20, 0], [20, 100]), line([20, 100], [0, 100]), line([0, 100], [0, 0])]}},
        "features": [
            {"id": "kw", "type": "keyway", "shaft_diameter": r(40), "length": r(50), "z_start": r(20), "form": "A"},
            {"id": "kwb", "type": "keyway", "shaft_diameter": r(40), "length": r(20), "z_start": r(75), "form": "B", "angle_deg": 180},
            {"id": "ring", "type": "retaining_groove", "nominal_diameter": r(40), "z_start": r(5)},
        ],
    }
    standards.fill(shaft)
    result(shaft["features"][0]["width"]["value"] == 12.0 and shaft["features"][0]["depth"]["value"] == 5.0, "fill keyway from table")
    part = Part.model_validate(shaft)
    solid = build(part)
    result(validate(part, solid)["passed"], "shaft with keyways and groove validates")

    def area(w, t1):
        half = w / 2
        return 2 * (half * math.sqrt(400 - half ** 2) / 2 + 200 * math.asin(half / 20)) - w * (20 - t1)

    def keyway_volume(length, b, t1, rounded):
        if not rounded:
            return area(b, t1) * length
        n, ends = 400, 0.0
        for i in range(n):
            x = (i + 0.5) / n * (b / 2)
            ends += area(2 * math.sqrt((b / 2) ** 2 - x ** 2), t1) * (b / 2) / n
        return area(b, t1) * (length - b) + 2 * ends

    expected = (math.pi * 20 ** 2 * 100 - math.pi * (20 ** 2 - 18.75 ** 2) * 1.85
                - keyway_volume(50, 12, 5, True) - keyway_volume(20, 12, 5, False))
    approx("shaft volume", solid.volume, expected, 0.5)
    shaft["features"][0]["width"] = r(10)
    notes = standards.check(shaft)["notes"]
    result(any(n["field"] == "kw.width" and n["severity"] == "warning" for n in notes), "wrong keyway width warned")
    plate["features"].append({"id": "odd", "type": "hole", "diameter": r(6.5), "position": r([5, 5])})
    notes = standards.check(plate)["notes"]
    result(any(n["field"] == "odd.diameter" for n in notes), "non-standard hole noted")


# ---------------------------------------------------------------- hand-written parts against reference STEP files

def test_shapes():
    from cad import build, compare, validate
    from spec import Part

    def part(pid, base, features):
        return Part.model_validate({"schema_version": "0.1", "part_id": pid, "drawing_file": "", "units": "mm",
                                    "coordinate_system": {"origin": "test"}, "base_body": base, "features": features})

    fuss = part("Fuss",
        {"type": "extruded_profile", "extrude_height": 19,
         "profile": {"rectangle": {"center": [0, 0], "size": [115, 115], "corner_radius": 22}}},
        [{"id": "boss", "type": "revolution", "mode": "add", "axis_position": [0, 0], "profile": {"segments": [
            line([0, 19], [38, 19]),
            {"type": "arc", "center": [38, 28.5], "radius": 9.5, "from": [38, 19], "to": [28.5, 28.5], "clockwise": True},
            line([28.5, 28.5], [28.5, 76.2]), line([28.5, 76.2], [0, 76.2]), line([0, 76.2], [0, 19])]}},
         {"id": "bore_top", "type": "hole", "through": False, "diameter": v(33), "position": v([0, 0]), "depth": v(57.2)},
         {"id": "bore", "type": "hole", "diameter": v(19), "position": v([0, 0])}]
        + [{"id": f"h{i}", "type": "hole", "diameter": v(19), "position": v([sx * 35.5, sy * 35.5])}
           for i, (sx, sy) in enumerate([(1, 1), (-1, 1), (-1, -1), (1, -1)])])
    welle = part("Welle",
        {"type": "revolved_profile", "profile": {"segments": [
            line([0, 0], [16.5, 0]), line([16.5, 0], [17.5, 1]), line([17.5, 1], [17.5, 35]), line([17.5, 35], [22.5, 35]),
            line([22.5, 35], [22.5, 100]), line([22.5, 100], [20, 100]), line([20, 100], [20, 250]), line([20, 250], [15, 250]),
            line([15, 250], [15, 277.5]), line([15, 277.5], [14, 277.5]), line([14, 277.5], [14, 280]), line([14, 280], [15, 280]),
            line([15, 280], [15, 300]), line([15, 300], [0, 300]), line([0, 300], [0, 0])]}},
        [{"id": "keyway", "type": "extrusion", "mode": "cut", "plane": "YZ", "offset": 20, "distance": -6,
          "profile": {"slot": {"center_1": [0, 135], "center_2": [0, 215], "width": 12}}}])
    for p, ref in ((fuss, "Fuss/Fuss.stp"), (welle, "Welle/Welle.stp")):
        solid = build(p)
        rep = validate(p, solid)
        sc = compare(solid, DATA / ref)
        result(rep["passed"] and sc["match"], f"{p.part_id} matches its reference",
               f"validation {rep['total'] - rep['failed']}/{rep['total']}, differing volume {sc['differing_volume_pct']} %, "
               f"holes {sc['holes_found']}/{sc['holes_expected']}")


# ---------------------------------------------------------------- run_script sandbox

def test_sandbox():
    import mcp_server
    sensor = key("Sensoraufnahme")

    def attempt(name, code, expect):
        mcp_server.new_part("sandbox_test")
        mcp_server.state["part"]["base_body"] = sensor["base_body"]
        res = mcp_server.run_script(id="s1", description=name, code=code)
        error = (res.get("error") or {}).get("code")
        got = "built" if res.get("ok") and not error else error
        ok = got == expect if isinstance(expect, str) else got in expect
        result(ok, f"sandbox {name}", got)

    attempt("legit: cut a square pocket", "model = model - Pos(25, 0, 0) * Box(4, 4, 20)", "built")
    attempt("legit: import math and build123d", "import math\nfrom build123d import Pos, Cylinder\nmodel = model - Pos(25, 0, 0) * Cylinder(math.sqrt(4), 20)", "built")
    attempt("blocked: import os", "import os\nos.remove('x')", "script_rejected")
    attempt("blocked: subprocess via from-import", "from subprocess import run\nrun(['ls'])", "script_rejected")
    attempt("blocked: open a file", "open('C:/secret.txt').read()", "script_rejected")
    attempt("blocked: dunder escape", "x = ().__class__.__bases__[0].__subclasses__()", "script_rejected")
    attempt("blocked: eval", "eval('1+1')", "script_rejected")
    attempt("blocked: getattr trick", "getattr(model, 'wrapped')", "script_rejected")
    attempt("error: undefined name", "model = model + does_not_exist", "script_failed")
    attempt("error: script removes the solid", "model = None", "script_rejected")
    attempt("stopped: endless loop", "while True:\n    pass", "timeout")
    attempt("stopped: memory bomb", "x = [0] * (10 ** 10)", ("script_failed", "worker_crashed"))


# ---------------------------------------------------------------- MCP server over stdio, as a client sees it

def test_server(docker: bool = False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp_server import launch_command

    async def call(session, tool, **args):
        res = await session.call_tool(tool, args)
        if res.isError:
            return {"rejected": res.content[0].text}
        return json.loads(res.content[0].text)

    def passed(data):
        return bool(data.get("validation", {}).get("passed"))

    async def run():
        command, cmd_args = launch_command(docker)
        async with stdio_client(StdioServerParameters(command=command, args=cmd_args, cwd=str(HERE))) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                result(len(names) == 20 and "build_from_spec" in names, "server lists 20 tools", f"{len(names)} tools")

                sensor = key("Sensoraufnahme")
                await call(session, "new_part", part_id="Sensoraufnahme", origin_description="left end face")
                spec = await call(session, "load_drawing_spec", spec_json=json.dumps(sensor))
                result([f["field"] for f in spec["review_flags"]] == ["width", "hole_1.position"], "spec review flags returned")
                await call(session, "create_base_body", segments=sensor["base_body"]["profile"]["segments"], thickness=5)
                await call(session, "add_slot", id="slot_1", width=5.5, x1=5, y1=0, x2=15, y2=0)
                wrong = await call(session, "add_threaded_hole", id="hole_1", thread="M8", x=44, y=0)
                hint = " ".join(c.get("hint", "") for c in wrong["validation"]["failed_checks"])
                result("dx=5.000" in hint, "misplaced hole reported with a hint", hint)
                fixed = await call(session, "add_threaded_hole", id="hole_1", thread="M8", x=39, y=0)
                result(passed(fixed), "corrected hole passes")
                exported = await call(session, "export_step")
                result(passed(exported) and abs(exported["volume"] - 4110.023) < 0.01, "Sensoraufnahme exported", str(exported.get("volume")))

                hebel = key("3-Loch-Hebel")
                await call(session, "new_part", part_id="3-Loch-Hebel", origin_description="center of the 12 mm hole")
                await call(session, "load_drawing_spec", spec_json=json.dumps(hebel))
                await call(session, "create_base_body", segments=hebel["base_body"]["profile"]["segments"], thickness=8)
                for f in hebel["features"]:
                    x, y = f["position"]["value"]
                    await call(session, "add_hole", id=f["id"], diameter=f["diameter"]["value"], x=x, y=y, chamfer=1)
                report = await call(session, "validate")
                result(passed(report), "3-Loch-Hebel built from tool calls validates")
                exported = await call(session, "export_step")
                result(abs(exported["volume"] - 19094.224) < 0.01, "3-Loch-Hebel exported", str(exported.get("volume")))

                bad = await call(session, "add_threaded_hole", id="h9", thread="M7", x=0, y=0)
                result(bad.get("error", {}).get("code") == "unknown_thread", "unknown thread rejected")
                bad = await call(session, "create_base_body", segments=[{"type": "arc", "center": [0, 0], "radius": -3}], thickness=5)
                result("rejected" in bad, "negative radius rejected by the parameter check")
                bad = await call(session, "remove_feature", id="does_not_exist")
                result(bad.get("error", {}).get("code") == "unknown_feature", "unknown feature id rejected")
                bad = await call(session, "run_script", id="s1", description="reads a file", code="print(open('/etc/passwd').read())")
                result(bad.get("error", {}).get("code") == "script_rejected", "file access in run_script rejected")

    asyncio.run(run())


# ---------------------------------------------------------------- agent loop with a fake model

def test_agent():
    from reader import run_agent
    spec_path = KEYS / "Sensoraufnahme.json"
    spec = key("Sensoraufnahme")
    script = [
        ("create_base_body", {"segments": spec["base_body"]["profile"]["segments"], "thickness": 5}),
        ("add_slot", {"id": "slot_1", "width": 5.5, "x1": 5, "y1": 0, "x2": 15, "y2": 0}),
        ("add_threaded_hole", {"id": "hole_1", "thread": "M8", "x": 44, "y": 0}),
        ("add_threaded_hole", {"id": "hole_1", "thread": "M8", "x": 39, "y": 0}),
        ("export_step", {}),
    ]
    seen_hint = []

    class Model(FakeModel):
        models = ["fake/model"]

        def answer(self, req):
            n = sum(1 for m in req["messages"] if m["role"] == "assistant")
            tools = {t["function"]["name"] for t in req["tools"]}
            assert "new_part" not in tools and "load_drawing_spec" not in tools
            last_tool = next((m["content"] for m in reversed(req["messages"]) if m["role"] == "tool"), "")
            if "deviates by dx=5.000" in last_tool:
                seen_hint.append(True)
            usage = {"prompt_tokens": 1000, "completion_tokens": 100}
            if n < len(script):
                name, args = script[n]
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, usage
            return {"role": "assistant", "content": "Built Sensoraufnahme. Assumed: width 20 (review flag)."}, usage

    server, base = start(Model)
    try:
        rec = asyncio.run(run_agent(spec_path, "fake/model", base, "test-key", False, max_cost=1.0, max_turns=20))
        result(rec["passed"] and rec["outcome"] == "finished" and bool(seen_hint) and rec["budget"]["model_calls"] == 6,
               "agent full loop", f"passed={rec['passed']}, hint reached the model={bool(seen_hint)}")
        rec = asyncio.run(run_agent(spec_path, "fake/model", base, "test-key", False, max_cost=1.0, max_turns=2))
        result(rec["outcome"].startswith("stopped: turn limit") and not rec["passed"], "agent turn limit", rec["outcome"])
        rec = asyncio.run(run_agent(spec_path, "fake/model", base, "test-key", False, max_cost=0.0005, max_turns=20))
        result(rec["outcome"].startswith("stopped: cost limit") and not rec["passed"], "agent cost limit", rec["outcome"])
    finally:
        server.shutdown()


# ---------------------------------------------------------------- reading a drawing with a fake vision model

def test_read():
    def load(name):
        d = key(name)
        d.pop("ground_truth_check", None)
        d.pop("reference_step", None)
        return d

    def transform(d, fn):
        d = copy.deepcopy(d)
        for s in d["base_body"]["profile"]["segments"]:
            for k in ("from", "to", "center"):
                if k in s:
                    s[k] = fn(s[k])
        for f in d["features"]:
            for k in ("position", "arc_center_1", "arc_center_2"):
                if k in f:
                    f[k]["value"] = fn(f[k]["value"])
        return d

    sensor, hebel = load("Sensoraufnahme"), load("3-Loch-Hebel")
    broken = copy.deepcopy(sensor)
    broken["base_body"]["profile"]["segments"][0]["to"] = [38, -10]
    del broken["features"][0]["width"]
    wrong = copy.deepcopy(sensor)
    wrong["features"][1]["position"]["value"] = [35, 0]
    outside = copy.deepcopy(sensor)
    outside["features"][1]["position"]["value"] = [300, 300]
    simplified = copy.deepcopy(sensor)
    simplified["review_flags"].append({"field": "outline", "severity": "warning", "reason": "round end simplified to a rectangle"})
    answers = {
        "fake/shifted": [transform(sensor, lambda p: [round(p[0] + 10, 3), round(p[1] + 5, 3)])],
        "fake/repair": [broken, sensor],
        "fake/rotated": [transform(hebel, lambda p: [round(-p[1], 3), round(p[0], 3)])],
        "fake/wrong": [wrong],
        "fake/buildfail": [outside, sensor],
        "fake/simplified": [simplified, sensor],
    }

    class Model(FakeModel):
        models = list(answers)

        def answer(self, req):
            images = sum(1 for p in req["messages"][1]["content"] if p.get("type") == "image_url")
            assert images == 5, images
            assert req.get("reasoning") == {"enabled": False}, req.get("reasoning")
            n = sum(1 for m in req["messages"] if m["role"] == "assistant")
            choices = answers[req["model"]]
            text = ("Calculations:\n- tangent point from R9 and the 18 mm arm\n```json\n"
                    + json.dumps(choices[min(n, len(choices) - 1)]) + "\n```")
            return {"role": "assistant", "content": text}, {"prompt_tokens": 9000, "completion_tokens": 3000}

    server, base = start(Model)
    env = {**os.environ, "CAD_API_BASE": base, "CAD_API_KEY": "test"}
    env.pop("OPENROUTER_API_KEY", None)
    sensor_pdf = DATA / "Sensoraufnahme (optional PMI)" / "Sensoraufnahme.pdf"
    hebel_pdf = DATA / "3-Loch-Hebel" / "3-Loch-Hebel.pdf"
    cases = [
        ("fake/shifted", sensor_pdf, ["attempt 1: 0 problem", "MATCH"]),
        ("fake/repair", sensor_pdf, ["attempt 1: 1 problem", "attempt 2: 0 problem", "MATCH"]),
        ("fake/rotated", hebel_pdf, ["attempt 1: 0 problem", "MATCH", "'rotation_deg': 270"]),
        ("fake/wrong", sensor_pdf, ["attempt 1: 0 problem", "NO MATCH"]),
        ("fake/buildfail", sensor_pdf, ["could not be built (hole_misses_part)", "attempt 2: 0 problem", "MATCH"]),
        ("fake/simplified", sensor_pdf, ["1 simplification(s) flagged", "attempt 2: 0 problem", "MATCH"]),
    ]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for model, pdf, expected in cases:
                proc = subprocess.run([sys.executable, str(HERE / "main.py"), "read", str(pdf), "--model", model, "--out", tmp],
                                      capture_output=True, text=True, env=env, cwd=HERE, encoding="utf-8", errors="replace")
                text = proc.stdout + proc.stderr
                ok = all(e in text for e in expected) and ("NO MATCH" in text) == ("NO MATCH" in expected)
                result(ok, f"read drawing with {model}", "" if ok else text[-1500:])
    finally:
        server.shutdown()


TESTS = {"schema": test_schema, "validate": test_validate, "standards": test_standards, "shapes": test_shapes,
         "sandbox": test_sandbox, "server": test_server, "agent": test_agent, "read": test_read}


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    docker = "--docker" in sys.argv
    chosen = [a for a in sys.argv[1:] if not a.startswith("--")] or list(TESTS)
    for name in chosen:
        print(f"\n=== {name}", flush=True)
        try:
            TESTS[name](docker) if name == "server" else TESTS[name]()
        except Exception:
            result(False, f"{name} crashed", traceback.format_exc(limit=3))
    print(f"\n{'all tests passed' if not failures else f'{len(failures)} failed: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
