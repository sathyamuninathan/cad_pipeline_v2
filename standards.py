"""Lookup of standard values (ISO / DIN) from the tables in standards/*.json.

    py main.py lookup M8                  thread data, tap drill, clearance holes, counterbore
    py main.py lookup M10x1               fine thread
    py main.py lookup identify 6.6        which standard size has this diameter
    py main.py lookup clearance M8 fine   ISO 273 clearance hole (fine / medium / coarse)
    py main.py lookup counterbore M6      DIN 974-1 counterbore for ISO 4762
    py main.py lookup countersink M6      90 degree countersink for ISO 10642
    py main.py lookup keyway 35           DIN 6885-1 keyway for a 35 mm shaft
    py main.py lookup groove shaft 20     DIN 471 retaining ring groove (shaft | bore)
    py main.py lookup undercut 1x0.4      DIN 509 undercut
    py main.py lookup splitpin 5          ISO 1234 split pin
    py main.py lookup fit 20 H7/g6        ISO 286 limits (also 'fit 20 H7' or 'fit 20 g6')
    py main.py lookup tol 45 m            ISO 2768-1 general tolerance (class f/m/c/v)
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).parent.resolve()
TABLES = HERE / "standards"
EPS = 1e-6


class StandardError(ValueError):
    pass


@lru_cache(maxsize=None)
def table(name: str) -> dict:
    return json.loads((TABLES / f"{name}.json").read_text(encoding="utf-8"))


def num(text) -> float:
    return float(str(text).strip().replace(",", "."))


def fmt(x: float) -> str:
    return f"{x:g}"


def size_key(d: float) -> str:
    return fmt(d)


def in_range(d: float, lo, hi) -> bool:
    return (lo is None or d > lo + EPS or (lo == 0 and d > 0)) and (hi is None or d <= hi + EPS)


THREAD_RE = re.compile(r"\s*M\s*(\d+(?:[.,]\d+)?)\s*(?:[xX×]\s*(\d+(?:[.,]\d+)?))?\s*(?:-\s*[0-9][A-Za-z]+)?\s*(?:-?\s*LH)?\s*")


def parse_thread(text: str) -> tuple[float, float | None]:
    m = THREAD_RE.fullmatch(str(text))
    if not m:
        raise StandardError(f"unrecognised thread '{text}', expected e.g. 'M8', 'M10x1' or 'M8-6H'")
    return num(m.group(1)), (num(m.group(2)) if m.group(2) else None)


def screw_size(text) -> float:
    if isinstance(text, (int, float)):
        return float(text)
    return parse_thread(text)[0]


def coarse_row(d: float) -> dict | None:
    return next((r for r in table("metric_threads")["coarse"] if abs(r["d"] - d) < EPS), None)


def thread(text: str) -> dict:
    d, pitch = parse_thread(text)
    row = coarse_row(d)
    if pitch is None:
        if row is None:
            raise StandardError(f"no ISO coarse thread M{fmt(d)} in the table")
        pitch, drill, kind = row["pitch"], row["tap_drill"], "coarse"
        evidence = f"ISO 261/2306 coarse thread M{fmt(d)}: pitch {fmt(pitch)}, tap drill {fmt(drill)} mm"
    else:
        drill, kind = round(d - pitch, 3), "fine"
        evidence = f"ISO 261 fine thread M{fmt(d)}x{fmt(pitch)}: tap drill = d - P = {fmt(drill)} mm"
    preferred = table("metric_threads")["fine_pitches"].get(size_key(d), [])
    return {
        "designation": f"M{fmt(d)}" + ("" if kind == "coarse" else f"x{fmt(pitch)}"),
        "kind": kind,
        "nominal_d": d,
        "pitch": pitch,
        "tap_drill": drill,
        "minor_internal_D1": round(d - 1.082532 * pitch, 3),
        "minor_external_d3": round(d - 1.226869 * pitch, 3),
        "pitch_diameter_d2": round(d - 0.649519 * pitch, 3),
        "preferred_fine_pitch": kind == "coarse" or any(abs(p - pitch) < EPS for p in preferred),
        "standard": "ISO 261 / ISO 724 / ISO 2306",
        "evidence": evidence,
    }


def clearance_hole(screw, series: str = "medium") -> dict:
    d = screw_size(screw)
    t = table("clearance_holes")
    if series not in t["series"]:
        raise StandardError(f"series must be one of {t['series']}")
    sizes = t["sizes"].get(size_key(d))
    if not sizes:
        raise StandardError(f"no ISO 273 clearance hole for M{fmt(d)}")
    dh = sizes[t["series"].index(series)]
    return {"screw": f"M{fmt(d)}", "series": series, "diameter": dh, "all_series": dict(zip(t["series"], sizes)),
            "standard": "ISO 273", "evidence": f"ISO 273 {series} clearance hole for M{fmt(d)}: {fmt(dh)} mm"}


def counterbore(screw, series: str = "medium") -> dict:
    d = screw_size(screw)
    t = table("counterbores")["counterbore_iso4762"]
    row = t.get(size_key(d))
    if not row:
        raise StandardError(f"no DIN 974-1 counterbore for M{fmt(d)} ISO 4762 in the table")
    values = dict(zip(t["columns"], row))
    hole = clearance_hole(d, series)
    return {"screw": f"M{fmt(d)}", "screw_standard": "ISO 4762", **values, "hole_diameter": hole["diameter"],
            "standard": "DIN 974-1 / ISO 273",
            "evidence": f"DIN 974-1 counterbore for M{fmt(d)} ISO 4762: Ø{fmt(values['counterbore_diameter'])} x "
                        f"{fmt(values['counterbore_depth'])} deep; hole {hole['evidence']}"}


def countersink(screw, series: str = "medium") -> dict:
    d = screw_size(screw)
    t = table("counterbores")
    row = t["countersunk_head_iso10642"].get(size_key(d))
    if not row:
        raise StandardError(f"no ISO 10642 countersunk head for M{fmt(d)} in the table")
    dk, angle = row
    diameter = round(dk + t["countersink_allowance"], 2)
    hole = clearance_hole(d, series)
    return {"screw": f"M{fmt(d)}", "screw_standard": "ISO 10642", "head_diameter_dk_max": dk, "angle_deg": angle,
            "countersink_diameter": diameter, "hole_diameter": hole["diameter"], "standard": "ISO 10642 / ISO 15065 / ISO 273",
            "evidence": f"ISO 10642 M{fmt(d)} head dk {fmt(dk)} + {fmt(t['countersink_allowance'])} allowance = "
                        f"Ø{fmt(diameter)} x {angle} deg; hole {hole['evidence']}",
            "assumption": "countersink diameter = head diameter + allowance; check the drawing or the Tabellenbuch"}


def keyway(shaft_diameter: float) -> dict:
    t = table("keyways_din6885")
    for row in t["rows"]:
        r = dict(zip(t["columns"], row))
        if in_range(shaft_diameter, r["d_over"], r["d_upto"]):
            return {**r, "standard": "DIN 6885-1",
                    "evidence": f"DIN 6885-1 for shaft Ø{fmt(shaft_diameter)} (over {fmt(r['d_over'])} up to "
                                f"{fmt(r['d_upto'])}): key {fmt(r['b'])} x {fmt(r['h'])}, shaft depth t1 {fmt(r['t1_shaft'])}, "
                                f"hub depth t2 {fmt(r['t2_hub'])}"}
    raise StandardError(f"shaft diameter {fmt(shaft_diameter)} is outside the DIN 6885-1 table (6 to 130 mm)")


def retaining_groove(diameter: float, location: str = "shaft") -> dict:
    t = table("retaining_rings")["shaft_din471" if location == "shaft" else "bore_din472"]
    std = "DIN 471" if location == "shaft" else "DIN 472"
    for row in t["rows"]:
        r = dict(zip(t["columns"], row))
        if abs(r["d1"] - diameter) < EPS:
            return {**r, "location": location, "standard": std,
                    "evidence": f"{std} for {location} Ø{fmt(diameter)}: groove Ø{fmt(r['d2_groove'])}, width {fmt(r['m_width'])}"}
    sizes = [r[0] for r in t["rows"]]
    near = min(sizes, key=lambda s: abs(s - diameter))
    raise StandardError(f"no {std} groove for Ø{fmt(diameter)} in the table (nearest: {fmt(near)})")


def undercut(designation: str) -> dict:
    t = table("undercuts_din509")
    key = str(designation).strip().upper().lstrip("EF").strip().replace(",", ".").replace("X", "x").replace(" ", "")
    for row in t["rows"]:
        r = dict(zip(t["columns"], row))
        if r["designation"] == key:
            return {**r, "standard": "DIN 509", "approximate": True,
                    "evidence": f"DIN 509 undercut {key}: r {fmt(r['radius_r'])}, depth {fmt(r['depth_t1'])}, width about {fmt(r['width_f_approx'])}"}
    raise StandardError(f"no DIN 509 undercut '{designation}'; known: {', '.join(r[0] for r in t['rows'])}")


def split_pin(nominal: float) -> dict:
    t = table("split_pins_iso1234")
    for row in t["rows"]:
        r = dict(zip(t["columns"], row))
        if abs(r["nominal"] - nominal) < EPS:
            return {**r, "standard": "ISO 1234", "standard_lengths": t["standard_lengths"],
                    "evidence": f"ISO 1234 split pin {fmt(nominal)}: wire d max {fmt(r['d_max'])}, eye c max {fmt(r['c_max_eye'])}"}
    raise StandardError(f"no ISO 1234 split pin of nominal size {fmt(nominal)}")


def size_index(d: float) -> int:
    for i, (lo, hi) in enumerate(table("fits_iso286")["size_ranges"]):
        if in_range(d, lo, hi):
            return i
    raise StandardError(f"size {fmt(d)} mm is outside the ISO 286 table (up to 500 mm)")


FIT_RE = re.compile(r"\s*([A-Za-z]{1,2})\s*(\d{1,2})\s*")


def fit(nominal: float, code: str) -> dict:
    m = FIT_RE.fullmatch(code)
    if not m:
        raise StandardError(f"unrecognised fit '{code}', expected e.g. 'H7' or 'g6'")
    letter, grade = m.group(1), int(m.group(2))
    t = table("fits_iso286")
    it_row = t["it_grades"].get(f"IT{grade}")
    if not it_row:
        raise StandardError(f"IT{grade} is not in the table (IT5 to IT14)")
    i = size_index(nominal)
    it = it_row[i]
    is_hole = letter[0].isupper()
    low = letter.lower()
    if low == "js":
        upper, lower = it / 2, -it / 2
    elif not is_hole and low in t["shaft_upper_deviation_es"]:
        upper = t["shaft_upper_deviation_es"][low][i]
        lower = upper - it
    elif not is_hole and low in t["shaft_lower_deviation_ei"]:
        lower = t["shaft_lower_deviation_ei"][low][i]
        if low == "k" and not 4 <= grade <= 7:
            lower = 0
        upper = lower + it
    elif is_hole and letter.upper() in t["hole_letters_supported"] and low in t["shaft_upper_deviation_es"]:
        lower = -t["shaft_upper_deviation_es"][low][i]
        upper = lower + it
    else:
        raise StandardError(f"fit letter '{letter}' is not supported by the table (holes: {', '.join(t['hole_letters_supported'])}; "
                            f"shafts: e f g h js k m n p)")
    lo, hi = t["size_ranges"][i]
    return {"nominal": nominal, "code": f"{letter}{grade}", "kind": "hole" if is_hole else "shaft",
            "upper_deviation": round(upper / 1000, 4), "lower_deviation": round(lower / 1000, 4),
            "max": round(nominal + upper / 1000, 4), "min": round(nominal + lower / 1000, 4), "it_um": it,
            "standard": "ISO 286", "evidence": f"ISO 286 Ø{fmt(nominal)} {letter}{grade} (range over {fmt(lo)} up to {fmt(hi)}): "
                                                f"{upper:+g} / {lower:+g} um"}


def fit_pair(nominal: float, code: str) -> dict:
    hole_code, shaft_code = [c.strip() for c in code.split("/")]
    hole, shaft = fit(nominal, hole_code), fit(nominal, shaft_code)
    max_clear = round(hole["max"] - shaft["min"], 4)
    min_clear = round(hole["min"] - shaft["max"], 4)
    kind = "clearance" if min_clear >= 0 else "interference" if max_clear <= 0 else "transition"
    note = table("fits_iso286")["common_fits"].get(f"{hole_code}/{shaft_code}")
    return {"hole": hole, "shaft": shaft, "max_clearance": max_clear, "min_clearance": min_clear, "fit_type": kind,
            "description": note, "standard": "ISO 286"}


def general_tolerance(nominal: float, cls: str = "m", kind: str = "linear") -> dict:
    t = table("general_tolerances_iso2768")
    cls = cls.lower()
    if cls not in t["classes"]:
        raise StandardError(f"ISO 2768-1 class must be one of {t['classes']}")
    section = {"linear": "linear", "radius": "radius_chamfer", "angle": "angular_deg"}[kind]
    for row in t[section]["rows"]:
        r = dict(zip(t[section]["columns"], row))
        if in_range(nominal, r["over"], r["upto"]):
            tol = r[cls]
            if tol is None:
                raise StandardError(f"ISO 2768-{cls} defines no {kind} tolerance for {fmt(nominal)}")
            unit = "deg" if kind == "angle" else "mm"
            return {"nominal": nominal, "class": cls, "kind": kind, "plus_minus": tol, "unit": unit, "standard": "ISO 2768-1",
                    "evidence": f"ISO 2768-{cls} {kind} {fmt(nominal)}: ±{fmt(tol)} {unit}"}
    raise StandardError(f"{fmt(nominal)} is outside the ISO 2768-1 {kind} table")


GENERAL_RE = re.compile(r"\s*(?:DIN\s*)?(?:ISO\s*)?2768\s*-?\s*([fmcv])\s*([HKL])?\s*", re.IGNORECASE)


def parse_general_tolerance(text: str) -> tuple[str, str | None]:
    m = GENERAL_RE.fullmatch(text or "")
    if not m:
        raise StandardError(f"unrecognised general tolerance '{text}', expected e.g. 'ISO 2768-mK'")
    return m.group(1).lower(), (m.group(2).upper() if m.group(2) else None)


PLUSMINUS_RE = re.compile(r"\s*(?:±|\+/-|\+-)\s*(\d+(?:[.,]\d+)?)\s*")
DEVIATION_RE = re.compile(r"\s*([+-]?\d+(?:[.,]\d+)?)\s*/\s*([+-]?\d+(?:[.,]\d+)?)\s*")


def tolerance(nominal: float, spec: str) -> dict:
    spec = str(spec).strip()
    m = PLUSMINUS_RE.fullmatch(spec)
    if m:
        t = num(m.group(1))
        return {"nominal": nominal, "tolerance": spec, "upper_deviation": t, "lower_deviation": -t,
                "max": round(nominal + t, 4), "min": round(nominal - t, 4)}
    m = DEVIATION_RE.fullmatch(spec)
    if m:
        a, b = num(m.group(1)), num(m.group(2))
        upper, lower = max(a, b), min(a, b)
        return {"nominal": nominal, "tolerance": spec, "upper_deviation": upper, "lower_deviation": lower,
                "max": round(nominal + upper, 4), "min": round(nominal + lower, 4)}
    if "/" in spec:
        pair = fit_pair(nominal, spec)
        return {"nominal": nominal, "tolerance": spec, "fit": pair}
    f = fit(nominal, spec)
    return {"nominal": nominal, "tolerance": spec, "upper_deviation": f["upper_deviation"], "lower_deviation": f["lower_deviation"],
            "max": f["max"], "min": f["min"], "standard": "ISO 286"}


def identify(diameter: float, tol: float = 0.01) -> list[str]:
    hits = []
    t = table("clearance_holes")
    for size, values in t["sizes"].items():
        for series, v in zip(t["series"], values):
            if abs(v - diameter) <= tol:
                hits.append(f"ISO 273 {series} clearance hole for M{size}")
    for r in table("metric_threads")["coarse"]:
        if abs(r["tap_drill"] - diameter) <= tol:
            hits.append(f"tap drill for M{fmt(r['d'])} (ISO 2306)")
        if abs(r["d"] - diameter) <= tol:
            hits.append(f"nominal diameter of M{fmt(r['d'])} (external thread or thread root)")
    cb = table("counterbores")["counterbore_iso4762"]
    for size, row in cb.items():
        if size != "columns" and abs(row[2] - diameter) <= tol:
            hits.append(f"DIN 974-1 counterbore diameter for M{size} ISO 4762")
    for row in table("split_pins_iso1234")["rows"]:
        if abs(row[0] - diameter) <= tol:
            hits.append(f"hole for an ISO 1234 split pin {fmt(row[0])}")
    for loc, key in (("shaft", "shaft_din471"), ("bore", "bore_din472")):
        for row in table("retaining_rings")[key]["rows"]:
            if abs(row[1] - diameter) <= tol:
                hits.append(f"{'DIN 471' if loc == 'shaft' else 'DIN 472'} groove diameter for {loc} Ø{fmt(row[0])}")
    return hits


def hole_matches(diameter: float, tol: float = 0.01) -> list[str]:
    t = table("clearance_holes")
    order = ["medium", "fine", "coarse"]
    hits = []
    for series in order:
        i = t["series"].index(series)
        hits += [f"ISO 273 {series} clearance hole for M{size}" for size, values in t["sizes"].items() if abs(values[i] - diameter) <= tol]
    hits += [f"tap drill for M{fmt(r['d'])}" for r in table("metric_threads")["coarse"] if abs(r["tap_drill"] - diameter) <= tol]
    return hits


def nearest_standard_hole(diameter: float) -> tuple[float, str] | None:
    candidates = []
    t = table("clearance_holes")
    for size, values in t["sizes"].items():
        candidates += [(v, f"ISO 273 {s} clearance hole for M{size}") for s, v in zip(t["series"], values)]
    candidates += [(r["tap_drill"], f"tap drill for M{fmt(r['d'])}") for r in table("metric_threads")["coarse"]]
    return min(candidates, key=lambda c: abs(c[0] - diameter)) if candidates else None


def val(v):
    return v.get("value") if isinstance(v, dict) else v


def derived(value, evidence: str, source: str) -> dict:
    return {"value": value, "status": "derived", "source": source, "confidence": 0.9, "evidence": evidence}


def fill(data: dict) -> list[str]:
    """Fill values that the drawing names only by standard (thread, screw size, shaft diameter) into the JSON.
    Values already present are never overwritten. Returns a log of what was filled."""
    log = []

    def put(f, key, value, evidence, table_name):
        if val(f.get(key)) is None:
            f[key] = derived(value, evidence, f"standards/{table_name}.json")
            log.append(f"{f.get('id')}.{key} = {fmt(value) if isinstance(value, float) else value} ({evidence})")

    for f in data.get("features", []):
        t = f.get("type")
        try:
            if t == "threaded_hole" and val(f.get("thread")):
                info = thread(str(val(f["thread"])))
                put(f, "modeled_diameter", info["tap_drill"], info["evidence"], "metric_threads")
            elif t == "counterbore" and val(f.get("screw")):
                info = counterbore(val(f["screw"]), f.get("series", "medium"))
                put(f, "hole_diameter", info["hole_diameter"], info["evidence"], "counterbores")
                put(f, "counterbore_diameter", info["counterbore_diameter"], info["evidence"], "counterbores")
                put(f, "counterbore_depth", info["counterbore_depth"], info["evidence"], "counterbores")
            elif t == "countersink" and val(f.get("screw")):
                info = countersink(val(f["screw"]), f.get("series", "medium"))
                put(f, "hole_diameter", info["hole_diameter"], info["evidence"], "counterbores")
                put(f, "countersink_diameter", info["countersink_diameter"], info["evidence"], "counterbores")
            elif t == "keyway" and val(f.get("shaft_diameter")) is not None:
                info = keyway(float(val(f["shaft_diameter"])))
                put(f, "width", float(info["b"]), info["evidence"], "keyways_din6885")
                depth = info["t1_shaft"] if f.get("location", "shaft") == "shaft" else info["t2_hub"]
                put(f, "depth", float(depth), info["evidence"], "keyways_din6885")
            elif t == "retaining_groove" and val(f.get("nominal_diameter")) is not None:
                info = retaining_groove(float(val(f["nominal_diameter"])), f.get("location", "shaft"))
                put(f, "groove_diameter", info["d2_groove"], info["evidence"], "retaining_rings")
                put(f, "width", info["m_width"], info["evidence"], "retaining_rings")
        except StandardError as e:
            log.append(f"{f.get('id')}: not filled ({e})")
    return log


def note(severity: str, field: str, message: str) -> dict:
    return {"severity": severity, "field": field, "message": message}


def check(data: dict) -> dict:
    """Plausibility checks against the standards, plus resolved tolerances. Never raises."""
    notes, tolerances = [], []
    gt = data.get("general_tolerance")
    general = None
    if gt:
        try:
            general = parse_general_tolerance(gt)
        except StandardError as e:
            notes.append(note("warning", "general_tolerance", str(e)))

    for f in data.get("features", []):
        fid, t = f.get("id"), f.get("type")
        try:
            if t == "threaded_hole":
                info = thread(str(val(f.get("thread"))))
                md = val(f.get("modeled_diameter"))
                if md is not None and abs(float(md) - info["tap_drill"]) > 0.05:
                    notes.append(note("warning", f"{fid}.modeled_diameter",
                                      f"{fmt(float(md))} differs from the tap drill {fmt(info['tap_drill'])} for {info['designation']}"))
                if not info["preferred_fine_pitch"]:
                    notes.append(note("info", f"{fid}.thread", f"{info['designation']} is not a preferred ISO 262 fine pitch; check the reading"))
            elif t == "hole":
                d = float(val(f.get("diameter")))
                hits = hole_matches(d)
                if hits:
                    notes.append(note("info", f"{fid}.diameter", f"Ø{fmt(d)} matches {hits[0]}"))
                else:
                    near = nearest_standard_hole(d)
                    if near and 0.01 < abs(near[0] - d) <= 0.25 and d < 60:
                        notes.append(note("info", f"{fid}.diameter",
                                          f"Ø{fmt(d)} is close to Ø{fmt(near[0])} ({near[1]}); check the reading"))
            elif t in ("counterbore", "countersink") and val(f.get("screw")):
                ref = counterbore(val(f["screw"])) if t == "counterbore" else countersink(val(f["screw"]))
                keys = ["hole_diameter", "counterbore_diameter", "counterbore_depth"] if t == "counterbore" else ["hole_diameter", "countersink_diameter"]
                for k in keys:
                    v = val(f.get(k))
                    if v is not None and abs(float(v) - ref[k]) > 0.05 and (f.get(k) or {}).get("status") != "derived":
                        notes.append(note("info", f"{fid}.{k}", f"drawing value {fmt(float(v))} differs from the table value "
                                                                 f"{fmt(ref[k])} for {ref['screw']}; the drawing value is used"))
            elif t == "keyway":
                ref = keyway(float(val(f.get("shaft_diameter"))))
                w, depth, length = val(f.get("width")), val(f.get("depth")), val(f.get("length"))
                expected_depth = ref["t1_shaft"] if f.get("location", "shaft") == "shaft" else ref["t2_hub"]
                if w is not None and abs(float(w) - ref["b"]) > 0.01:
                    notes.append(note("warning", f"{fid}.width", f"width {fmt(float(w))} does not match DIN 6885-1 b = {fmt(ref['b'])} "
                                                                  f"for Ø{fmt(float(val(f['shaft_diameter'])))}; check the reading"))
                if depth is not None and abs(float(depth) - expected_depth) > 0.05:
                    notes.append(note("warning", f"{fid}.depth", f"depth {fmt(float(depth))} does not match DIN 6885-1 "
                                                                  f"{'t1' if f.get('location', 'shaft') == 'shaft' else 't2'} = {fmt(expected_depth)}"))
                if length is not None and float(length) not in table("keyways_din6885")["standard_lengths"]:
                    notes.append(note("info", f"{fid}.length", f"length {fmt(float(length))} is not a standard key length"))
            elif t == "retaining_groove":
                ref = retaining_groove(float(val(f.get("nominal_diameter"))), f.get("location", "shaft"))
                for k, rk in (("groove_diameter", "d2_groove"), ("width", "m_width")):
                    v = val(f.get(k))
                    if v is not None and abs(float(v) - ref[rk]) > 0.05:
                        notes.append(note("warning", f"{fid}.{k}", f"{fmt(float(v))} does not match {ref['standard']} {rk} = {fmt(ref[rk])}"))
        except (StandardError, TypeError, ValueError) as e:
            notes.append(note("warning", fid or "feature", str(e)))

    def walk(prefix, obj):
        if isinstance(obj, dict):
            if "tolerance" in obj and obj.get("tolerance"):
                v = obj.get("value")
                nominal = v if isinstance(v, (int, float)) else None
                if nominal is None:
                    notes.append(note("warning", prefix, "tolerance given on a value that is not a single number"))
                    return
                try:
                    tolerances.append({"field": prefix, **tolerance(float(nominal), obj["tolerance"])})
                except StandardError as e:
                    notes.append(note("warning", prefix, str(e)))
                return
            for k, v in obj.items():
                walk(f"{prefix}.{k}" if prefix else k, v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                key = v.get("id") if isinstance(v, dict) and v.get("id") else str(i)
                walk(f"{prefix}.{key}" if prefix and not (isinstance(v, dict) and v.get("id")) else key, v)

    walk("", {"dimensions": data.get("dimensions", []), "features": data.get("features", [])})
    result = {"notes": notes, "tolerances": tolerances}
    if general:
        result["general_tolerance"] = {"text": gt, "class": general[0], "geometric_class": general[1], "standard": "ISO 2768"}
    return result


def lookup(args: list[str]) -> dict | list:
    if not args:
        raise StandardError(__doc__)
    head, rest = args[0].lower(), args[1:]
    if head.startswith("m") and THREAD_RE.fullmatch(args[0]):
        info = thread(args[0])
        d = info["nominal_d"]
        extra = {}
        for name, fn in (("clearance_hole", clearance_hole), ("counterbore", counterbore), ("countersink", countersink)):
            try:
                extra[name] = fn(d)
            except StandardError:
                pass
        return {"thread": info, **extra}
    if head == "identify":
        d = num(rest[0])
        hits = identify(d)
        near = nearest_standard_hole(d)
        return {"diameter": d, "matches": hits or ["no standard size matches exactly"],
                "nearest_hole_standard": {"diameter": near[0], "is": near[1]} if near else None}
    if head == "clearance":
        return clearance_hole(rest[0], rest[1] if len(rest) > 1 else "medium")
    if head == "counterbore":
        return counterbore(rest[0])
    if head == "countersink":
        return countersink(rest[0])
    if head in ("keyway", "key"):
        return keyway(num(rest[0]))
    if head in ("groove", "circlip"):
        location = rest[0].lower() if rest[0].lower() in ("shaft", "bore") else "shaft"
        return retaining_groove(num(rest[-1]), location)
    if head == "undercut":
        return undercut(rest[0])
    if head in ("splitpin", "split_pin", "splint"):
        return split_pin(num(rest[0]))
    if head == "fit":
        nominal, code = num(rest[0]), "".join(rest[1:])
        return fit_pair(nominal, code) if "/" in code else fit(nominal, code)
    if head in ("tol", "iso2768"):
        nominal, cls = num(rest[0]), (rest[1] if len(rest) > 1 else "m")
        kind = rest[2] if len(rest) > 2 else "linear"
        return general_tolerance(nominal, cls, kind)
    raise StandardError(f"unknown lookup '{args[0]}'.\n{__doc__}")


def tap_drill(designation: str) -> tuple[float, str]:
    try:
        info = thread(designation)
    except StandardError as e:
        raise ValueError(str(e)) from None
    return info["tap_drill"], info["evidence"]
