"""Drawing2CAD: technical drawing (PDF) -> part JSON -> 3D model (STEP), with validation and review flags.

    py main.py run <drawing.pdf> [--docker]       whole chain: read with OpenRouter, build through MCP, report
    py main.py run --spec extracted/X.json        whole chain from a saved part JSON (no API calls)
    py main.py web                                local web page (upload a drawing, see the 3D result)

  Helpers for Claude Code (free):
    py main.py render <drawing.pdf>               page + 4 zoomed tiles as PNG in out/drawings/<name>/
    py main.py crop <drawing.pdf> x0 y0 x1 y1     zoom into a region (fractions of the page, from the top left)
    py main.py check extracted/X.json             clean up, fill standard sizes, check schema + rules + test build
    py main.py lookup <query>                     ISO/DIN values, e.g. 'lookup M8', 'lookup help'

  Scoring and batches:
    py main.py evaluate                           build every extracted/*.json and compare with the reference STEP (free)
    py main.py compare <built.step> <ref.stp>     compare two STEP files, ignoring origin and orientation
    py main.py reread [parts...]                  read drawings again with Claude Code, then score them
    py main.py batch [parts...]                   read many drawings with OpenRouter and score them (costs API credit)
    py main.py read <drawing.pdf>                 only the OpenRouter reading step (costs API credit)
    py main.py agent extracted/X.json             let an OpenRouter model build the part through the MCP tools

Everything generated goes to out/: STEP files and reports, logs/, drawings/, evaluated/, web/.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

from reader import AGENT_MODEL, DEFAULT_BASE_URL, READ_MODEL

HERE = Path(__file__).parent.resolve()
OUT = HERE / "out"
LOGS = OUT / "logs"
EXTRACTED = HERE / "extracted"
DATA_ROOT = HERE.parent / "Drawing2CAD" / "Drawing2CAD"
ANSWER_KEYS = HERE.parent / "answer_keys"


# ---------------------------------------------------------------- run: drawing -> JSON -> MCP build -> report

def step(msg: str):
    print(f"\n== {msg}", flush=True)


def saved_reading(pdf: Path) -> Path | None:
    for p in sorted(EXTRACTED.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            if Path(str(json.loads(p.read_text(encoding="utf-8")).get("drawing_file", "")).replace("\\", "/")).name == pdf.name:
                return p
        except (ValueError, OSError):
            pass
    return None


def reading_for(pdf: Path, args) -> Path:
    saved = saved_reading(pdf)
    if (args.reuse or args.offline) and saved:
        print(f"reusing {saved.relative_to(HERE)} (no API call)")
        return saved
    if args.offline:
        sys.exit(f"no saved reading of {pdf.name} in extracted/; run without --offline to read it with the AI")
    from reader import read_drawing
    args.pdf = pdf
    run = read_drawing(args)
    if not run["json"]:
        sys.exit("extraction failed")
    return Path(run["json"])


async def mcp_build(spec: dict, use_docker: bool) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp_server import launch_command
    command, cmd_args = launch_command(use_docker)
    params = StdioServerParameters(command=command, args=cmd_args, cwd=str(HERE))
    calls = []

    async def call(session, name, arguments):
        result = json.loads((await session.call_tool(name, arguments)).content[0].text)
        calls.append({"tool": name, "result": result})
        ok = result.get("ok", not result.get("error"))
        print(f"  {name:18s} {'ok' if ok else 'FAILED'}" + (f"  {result['error']['code']}: {result['error']['message'][:100]}"
                                                         if result.get("error") else ""))
        return result

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await call(session, "new_part", {"part_id": spec["part_id"],
                                             "origin_description": spec.get("coordinate_system", {}).get("origin", "")})
            await call(session, "load_drawing_spec", {"spec_json": json.dumps(spec)})
            built = await call(session, "build_from_spec", {})
            if not built.get("error"):
                await call(session, "export_step", {"filename": f"{spec['part_id']}.step"})
    return calls


def cmd_run(args):
    import standards
    from spec import clean
    if not args.drawing and not args.spec:
        sys.exit("give a drawing PDF or --spec")

    step("1/4 drawing -> JSON spec")
    spec_path = args.spec or reading_for(args.drawing, args)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for k in ("ground_truth_check", "reference_step"):
        spec.pop(k, None)
    for line in clean(spec):
        print(f"  from standards: {line}")
    standards_result = standards.check(spec)
    flags = spec.get("review_flags", [])
    print(f"spec {spec_path.name}: {len(spec.get('features', []))} features, {len(flags)} review flags")

    step(f"2/4 MCP build ({'Docker sandbox' if args.docker else 'local server'})")
    calls = asyncio.run(mcp_build(spec, args.docker))
    exported = next((c["result"] for c in calls if c["tool"] == "export_step"), None)
    build_error = next((c["result"]["error"] for c in calls if c["result"].get("error")), None)

    step("3/4 validation")
    validation = (exported or {}).get("validation")
    if validation:
        print(f"  {validation['checks_run'] - len(validation['failed_checks'])}/{validation['checks_run']} checks passed")
        for c in validation["failed_checks"]:
            print(f"  FAILED {c['check']} {c.get('target') or ''}: expected {c.get('expected')}, measured {c.get('measured')}"
                  + (f" - {c['hint']}" if c.get("hint") else ""))
    else:
        print(f"  not built: {build_error}")

    step("4/4 report")
    blocking = [f for f in flags if f.get("severity") == "blocking_until_confirmed"]
    status = ("not_built" if not exported else
              "needs_review" if blocking else
              "valid" if validation and validation["passed"] else "built_with_failed_checks")
    step_file = str(OUT / Path(exported["step_file"]).name) if exported else None
    report = {
        "part_id": spec["part_id"],
        "drawing": spec.get("drawing_file"),
        "drawing_path": str(args.drawing.resolve()) if args.drawing else None,
        "spec": str(spec_path),
        "server": "docker" if args.docker else "local",
        "created": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "step_file": step_file,
        "volume": (exported or {}).get("volume"),
        "bbox_size": (exported or {}).get("bbox_size"),
        "validation": validation,
        "build_error": build_error,
        "review_flags": flags,
        "general_tolerance": standards_result.get("general_tolerance"),
        "tolerances": standards_result["tolerances"],
        "standards_notes": standards_result["notes"],
        "tool_calls": calls,
    }
    out = OUT / f"{spec['part_id']}.report.json"
    OUT.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  status: {status}")
    if step_file:
        print(f"  STEP:   {step_file}")
    print(f"  report: {out}")
    for f in flags:
        print(f"  [{f.get('severity')}] {f.get('field')}: {f.get('reason')}"[:160])
    for n in standards_result["notes"]:
        print(f"  standards [{n['severity']}] {n['field']}: {n['message']}"[:160])
    sys.exit(0 if status in ("valid", "needs_review") else 1)


# ---------------------------------------------------------------- helpers for Claude Code

def cmd_render(args):
    from reader import render
    render(args.pdf)


def cmd_crop(args):
    from reader import crop
    crop(args.pdf, args.box)


def cmd_check(args):
    import standards
    from cad import check_spec
    from spec import clean, simplifications
    data = json.loads(args.json.read_text(encoding="utf-8"))
    filled = clean(data)
    args.json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for line in filled:
        print("  from standards:", line)
    issues, solid = check_spec(data)
    if issues:
        print(f"{len(issues)} problem(s):")
        for i in issues:
            print("  -", i)
        sys.exit(1)
    bb = solid.bounding_box()
    print(f"OK: builds as one solid, volume {solid.volume:.2f} mm3, size {bb.size.X:.2f} x {bb.size.Y:.2f} x {bb.size.Z:.2f} mm")
    for s in simplifications(data):
        print("  simplified:", s)
    for f in data.get("review_flags", []):
        print(f"  flag [{f.get('severity')}] {f.get('field')}: {f.get('reason')}")
    result = standards.check(data)
    if result.get("general_tolerance"):
        g = result["general_tolerance"]
        print(f"  general tolerance: {g['text']} (ISO 2768-1 class {g['class']}, part 2 class {g['geometric_class'] or '-'})")
    for n in result["notes"]:
        print(f"  standards [{n['severity']}] {n['field']}: {n['message']}")
    for t in result["tolerances"]:
        if "fit" in t:
            fp = t["fit"]
            print(f"  tolerance {t['field']}: {t['nominal']:g} {t['tolerance']} -> {fp['fit_type']} fit, "
                  f"clearance {fp['min_clearance']:+g} .. {fp['max_clearance']:+g} mm")
        else:
            print(f"  tolerance {t['field']}: {t['nominal']:g} {t['tolerance']} -> {t['min']:g} .. {t['max']:g} mm")


def cmd_lookup(args):
    import standards
    if not args.query or args.query[0] in ("help", "-h", "--help"):
        sys.exit(standards.__doc__)
    try:
        print(json.dumps(standards.lookup(args.query), indent=2, ensure_ascii=False))
    except standards.StandardError as e:
        sys.exit(f"lookup failed: {e}")


# ---------------------------------------------------------------- evaluate: score saved readings against references

def find_reference(data_root: Path, drawing_file: str) -> Path | None:
    for pdf in data_root.rglob(Path(drawing_file.replace("\\", "/")).name):
        steps = [p for p in pdf.parent.glob("*.stp") if "PMI" not in p.name.upper()]
        if steps:
            return steps[0]
    return None


def evaluate(path: Path, data_root: Path, out_dir: Path) -> dict:
    from build123d import export_step
    from cad import BuildError, build, compare, validate
    from spec import Part, clean
    data = json.loads(path.read_text(encoding="utf-8"))
    clean(data)
    row = {"part": data.get("part_id", path.stem), "drawing": data.get("drawing_file", ""),
           "base_body": (data.get("base_body") or {}).get("type"), "features": len(data.get("features", [])),
           "flags": [{"severity": f.get("severity"), "field": f.get("field"), "reason": f.get("reason")}
                     for f in data.get("review_flags", [])]}
    try:
        part = Part.model_validate(data)
    except Exception as e:
        row.update(stage="schema", error=str(e).splitlines()[0])
        return row
    issues = part.consistency_issues()
    if issues:
        row.update(stage="consistency", error="; ".join(issues[:3]))
        return row
    try:
        solid = build(part)
    except BuildError as e:
        row.update(stage="build", error=f"{e.code} at {e.target}: {e.message}")
        return row
    except Exception as e:
        row.update(stage="build", error=f"{type(e).__name__}: {e}")
        return row
    out_dir.mkdir(parents=True, exist_ok=True)
    step_path = out_dir / f"{row['part']}.step"
    export_step(solid, str(step_path))
    v = validate(part, solid)
    bb = solid.bounding_box()
    row.update(stage="built", step_file=str(step_path), volume=round(solid.volume, 2),
               size=[round(bb.size.X, 2), round(bb.size.Y, 2), round(bb.size.Z, 2)],
               validation={"passed": v["passed"], "total": v["total"], "failed": v["failed"]})
    ref = find_reference(data_root, row["drawing"]) if row["drawing"] else None
    if ref:
        try:
            s = compare(solid, ref)
        except Exception as e:
            row["score_error"] = f"{type(e).__name__}: {e}"
            return row
        row["score"] = {k: s[k] for k in ("match", "differing_volume_pct", "holes_found", "holes_expected", "holes_ok",
                                          "size_deviation", "best_pose", "timed_out")}
    return row


def verdict(row: dict) -> str:
    if row["stage"] != "built":
        return "not built"
    s = row.get("score")
    if not s:
        return "score error" if row.get("score_error") else "built"
    if s["match"]:
        return "MATCH"
    return "close" if s["differing_volume_pct"] <= 2 else "off"


def cmd_evaluate(args):
    rows = []
    for path in sorted(args.folder.glob("*.json")):
        print(f"evaluating {path.name} ...", flush=True)
        t0 = datetime.now()
        row = evaluate(path, args.data_root, OUT / "evaluated")
        row["verdict"] = verdict(row)
        row["seconds"] = round((datetime.now() - t0).total_seconds(), 1)
        print(f"    {row['verdict']} ({row['seconds']} s)", flush=True)
        rows.append(row)
    print(f"\n{'part':18s} {'type':17s} {'result':10s} {'differs %':>10s} {'holes':>7s} {'checks':>7s} {'flags':>6s}")
    for r in rows:
        s = r.get("score") or {}
        pct = f"{s['differing_volume_pct']:.3f}" if s else "-"
        holes = f"{s['holes_found']}/{s['holes_expected']}" if s else "-"
        checks = f"{r['validation']['total'] - r['validation']['failed']}/{r['validation']['total']}" if "validation" in r else "-"
        print(f"{r['part'][:18]:18s} {str(r['base_body'])[:17]:17s} {r['verdict']:10s} {pct:>10s} {holes:>7s} {checks:>7s} {len(r['flags']):>6d}")
        if r["stage"] != "built":
            print(f"{'':18s} -> {r['stage']}: {r['error'][:110]}")
        elif (r.get("score") or {}).get("timed_out"):
            print(f"{'':18s} -> score: time limit hit, best pose found so far")
        elif r.get("score_error"):
            print(f"{'':18s} -> score: {r['score_error'][:110]}")
    counts = {k: sum(1 for r in rows if r["verdict"] == k) for k in ("MATCH", "close", "off", "score error", "not built")}
    print(f"\n{counts['MATCH']} match, {counts['close']} close (within 2 %), {counts['off']} off, "
          f"{counts['score error']} score error, {counts['not built']} not built - out of {len(rows)} drawings")
    LOGS.mkdir(parents=True, exist_ok=True)
    result = LOGS / f"evaluation__{datetime.now():%Y%m%d_%H%M%S}.json"
    result.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {result.relative_to(HERE)}")


def cmd_compare(args):
    from build123d import import_step
    from cad import compare
    print(json.dumps(compare(import_step(str(args.built)), args.reference), indent=2))


# ---------------------------------------------------------------- reread: Claude Code over several drawings

REREAD_PARTS = {"Trägerplatte V2": "Traegerplatte", "Maschinenteil": "Maschinenteil", "Haken": "Haken",
                "Flansch": "Flansch", "Fuss": "Fuss"}


def docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def last_scores() -> dict:
    runs = sorted(LOGS.glob("evaluation__*.json"))
    return {r["part"]: r for r in json.loads(runs[-1].read_text(encoding="utf-8"))} if runs else {}


def describe(row: dict | None) -> str:
    if not row:
        return "-"
    s = row.get("score") or {}
    if s:
        return f"{row.get('verdict', verdict(row))} {s['differing_volume_pct']:.3f} % holes {s['holes_found']}/{s['holes_expected']}"
    if row.get("error"):
        return f"not built ({row['stage']}: {row['error'][:90]})"
    if row.get("score_error"):
        return f"score error ({row['score_error'][:90]})"
    return row.get("verdict") or row.get("stage", "-")


def reread_one(folder: str, part_id: str, model: str, docker: bool) -> tuple[str, float]:
    import webapp
    pdfs = sorted((DATA_ROOT / folder).glob("*.pdf"))
    if not pdfs:
        return "no PDF found", 0.0
    job_id = uuid.uuid4().hex[:10]
    target = webapp.RUNS / job_id
    target.mkdir(parents=True)
    pdf = target / webapp.safe_pdf_name(pdfs[0].name)
    shutil.copy2(pdfs[0], pdf)
    old = EXTRACTED / f"{part_id}.json"
    if old.exists():
        (EXTRACTED / "backup").mkdir(exist_ok=True)
        shutil.copy2(old, EXTRACTED / "backup" / f"{part_id}__{datetime.now():%Y%m%d_%H%M%S}.json")
    job = {"id": job_id, "state": "starting", "pdf": pdf, "file": pdf.name, "mode": "claude", "max_cost": 0.0,
           "model": model, "docker": docker, "log": [], "part_id": None, "summary": "",
           "hint": f"Use part_id `{part_id}` so the file is extracted/{part_id}.json (replace the existing file)."}
    started = time.time()
    worker = threading.Thread(target=webapp.run_job, args=(job,), daemon=True)
    worker.start()
    shown = 0
    while worker.is_alive() or shown < len(job["log"]):
        while shown < len(job["log"]):
            print("   " + job["log"][shown], flush=True)
            shown += 1
        time.sleep(1)
    return job["state"], time.time() - started


def cmd_reread(args):
    folders = {f: REREAD_PARTS.get(f, Path(f).name.replace("ä", "ae").replace(" V2", "")) for f in args.parts} or REREAD_PARTS
    docker = not args.local and docker_ready()
    if not args.local and not docker:
        print("Docker is not running, so the MCP server runs locally (start Docker Desktop to use the sandbox).")
    before = last_scores()
    results = []
    for i, (folder, part_id) in enumerate(folders.items(), 1):
        print(f"\n[{i}/{len(folders)}] {folder} -> extracted/{part_id}.json  ({datetime.now():%H:%M})", flush=True)
        state, secs = reread_one(folder, part_id, args.model, docker)
        path = EXTRACTED / f"{part_id}.json"
        row = None
        if path.exists() and path.stat().st_mtime >= time.time() - secs - 5:
            row = evaluate(path, DATA_ROOT, OUT / "evaluated")
            row["verdict"] = verdict(row)
        else:
            state = f"{state}, no new reading"
        results.append((part_id, state, secs, before.get(part_id), row))
        print(f"   => {state} in {secs / 60:.1f} min · before: {describe(before.get(part_id))} · now: {describe(row)}", flush=True)
    print(f"\n{'part':16s} {'run':22s} {'min':>5s}  {'before':34s} now")
    for part_id, state, secs, old, new in results:
        print(f"{part_id[:16]:16s} {state[:22]:22s} {secs / 60:5.1f}  {describe(old)[:34]:34s} {describe(new)}")
    print("\nOld readings are in extracted/backup/. Run `py main.py evaluate` for the full table of all parts.")


# ---------------------------------------------------------------- batch: OpenRouter over several drawings

BATCH_PARTS = ["Sensoraufnahme (optional PMI)", "3-Loch-Hebel", "Fuss", "Welle", "Achse", "Haken", "Rotation",
               "Getriebedeckel (optional PMI)", "Flansch", "Trägerplatte V2", "Maschinenteil", "Lagerschaft"]


def cmd_batch(args):
    from reader import read_drawing
    spent, rows = 0.0, []
    for folder in args.parts or BATCH_PARTS:
        pdfs = sorted((args.data_root / folder).glob("*.pdf"))
        if not pdfs:
            print(f"skip {folder}: no PDF found")
            continue
        if spent + args.max_cost > args.budget:
            print(f"stop: the next drawing could exceed the batch budget ({spent:.3f} of {args.budget} USD spent)")
            break
        print(f"\n=== {folder} ===", flush=True)
        args.pdf = pdfs[0]
        try:
            log = read_drawing(args)
        except Exception as e:
            print(f"failed: {e}")
            log = {}
        cost = log.get("budget", {}).get("cost_usd", 0.0)
        spent += cost
        score, build = log.get("score") or {}, log.get("build") or {}
        rows.append({
            "part": folder, "model": log.get("model"), "cost_usd": cost,
            "json_ok": bool(log) and not log.get("remaining_problems"),
            "built": build.get("ok", False),
            "match": score.get("match"),
            "differing_volume_pct": score.get("differing_volume_pct"),
            "holes": f"{score.get('holes_found', '-')}/{score.get('holes_expected', '-')}" if score else "-",
            "problems": log.get("remaining_problems", []),
            "build_error": build.get("error"),
        })
    print("\n" + "=" * 96)
    print(f"{'part':32s} {'JSON':5s} {'built':6s} {'result':9s} {'differs %':>10s} {'holes':>7s} {'cost USD':>9s}")
    for r in rows:
        result = "MATCH" if r["match"] else ("NO MATCH" if r["match"] is False else "-")
        pct = "-" if r["differing_volume_pct"] is None else f"{r['differing_volume_pct']:.3f}"
        print(f"{r['part'][:32]:32s} {'ok' if r['json_ok'] else 'no':5s} {'yes' if r['built'] else 'no':6s} {result:9s} "
              f"{pct:>10s} {r['holes']:>7s} {r['cost_usd']:>9.4f}")
    print(f"\n{sum(1 for r in rows if r['match'])} of {len(rows)} drawings match their reference STEP | total cost {spent:.3f} USD")
    LOGS.mkdir(parents=True, exist_ok=True)
    out = LOGS / f"batch__{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"rows": rows, "total_cost_usd": round(spent, 4)}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out.relative_to(HERE)}")


# ---------------------------------------------------------------- read / agent / web

def cmd_read(args):
    from reader import read_drawing
    sys.exit(0 if read_drawing(args)["json"] else 1)


def cmd_agent(args):
    from reader import api_key, run_agent, safe, save_log
    key = api_key(args.base_url)
    print(f"part {args.spec.name} | model {args.model} | server {'docker' if args.docker else 'local'} | "
          f"limits {args.max_cost} USD, {args.max_turns} model calls", flush=True)
    try:
        record = asyncio.run(run_agent(args.spec, args.model, args.base_url, key, args.docker, args.max_cost, args.max_turns))
    except BaseException as e:
        errors = [e]
        while errors and isinstance(errors[0], BaseExceptionGroup):
            errors = list(errors[0].exceptions)
        cause = errors[0] if errors else e
        if isinstance(cause, (KeyboardInterrupt, SystemExit)):
            raise
        sys.exit(f"\nstopped: {cause}")
    log = save_log(f"agent__{record['part_id']}__{safe(args.model)}__{datetime.now():%Y%m%d_%H%M%S}.json", record)
    b = record["budget"]
    print(f"\nresult: {'PASSED' if record['passed'] else 'NOT PASSED'} | {record['outcome']} | {len(record['tool_calls'])} tool calls | "
          f"{b['model_calls']} model calls | {b['prompt_tokens'] + b['completion_tokens']} tokens | {b['cost_usd']} USD")
    if record.get("final_reply"):
        print("model summary: " + record["final_reply"].strip())
    print(f"run log: {log}")
    sys.exit(0 if record["passed"] else 1)


def cmd_web(args):
    import webapp
    webapp.serve(args.port, not args.no_browser)


# ---------------------------------------------------------------- command line

def add_read_options(p, max_cost: float):
    p.add_argument("--model", default=os.environ.get("CAD_EXTRACT_MODEL", READ_MODEL), help="vision model on OpenRouter")
    p.add_argument("--base-url", default=os.environ.get("CAD_API_BASE", DEFAULT_BASE_URL))
    p.add_argument("--max-cost", type=float, default=max_cost, help="USD limit per drawing")
    p.add_argument("--out", type=Path, default=EXTRACTED, help="folder for the part JSON")
    p.add_argument("--keys", type=Path, default=ANSWER_KEYS, help="folder with example part JSONs")
    p.add_argument("--repairs", type=int, default=3)
    p.add_argument("--max-tokens", type=int, default=24000, help="limit for one answer, reasoning included")
    p.add_argument("--reasoning-tokens", type=int, default=0, help="thinking budget; 0 switches reasoning off")
    p.add_argument("--effort", default="low", help="reasoning effort when the model requires reasoning")
    p.add_argument("--no-improve", action="store_true", help="skip the extra round for simplified geometry")


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser("run", help="drawing PDF -> 3D STEP model, with validation and review flags")
    p.add_argument("drawing", type=Path, nargs="?", help="drawing PDF")
    p.add_argument("--spec", type=Path, help="start from an existing part JSON instead of reading (no API calls)")
    p.add_argument("--reuse", action="store_true", help="reuse extracted/<part>.json if it exists")
    p.add_argument("--offline", action="store_true", help="only use a saved reading; never call the AI")
    p.add_argument("--docker", action="store_true", help="run the MCP server in the sandbox container")
    add_read_options(p, 0.40)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("web", help="local web page")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("render", help="drawing as PNG images for reading")
    p.add_argument("pdf", type=Path)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("crop", help="zoom into part of a drawing")
    p.add_argument("pdf", type=Path)
    p.add_argument("box", nargs=4, metavar="x0 y0 x1 y1")
    p.set_defaults(func=cmd_crop)

    p = sub.add_parser("check", help="check a part JSON (schema, rules, test build)")
    p.add_argument("json", type=Path)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("lookup", help="ISO/DIN standard values")
    p.add_argument("query", nargs="*")
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("evaluate", help="score every saved reading against its reference STEP")
    p.add_argument("folder", type=Path, nargs="?", default=EXTRACTED)
    p.add_argument("--data-root", type=Path, default=DATA_ROOT)
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("compare", help="compare a STEP file with a reference STEP")
    p.add_argument("built", type=Path)
    p.add_argument("reference", type=Path)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("reread", help="read drawings again with Claude Code and score them")
    p.add_argument("parts", nargs="*", help="part folder names in Drawing2CAD (default: the five weakest)")
    p.add_argument("--model", choices=["opus", "sonnet"], default="opus")
    p.add_argument("--local", action="store_true", help="run the MCP server without Docker")
    p.set_defaults(func=cmd_reread)

    p = sub.add_parser("batch", help="read many drawings with OpenRouter and score them")
    p.add_argument("parts", nargs="*", help="part folder names in the data root")
    p.add_argument("--data-root", type=Path, default=DATA_ROOT)
    p.add_argument("--budget", type=float, default=2.0, help="USD limit for the whole batch")
    add_read_options(p, 0.30)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("read", help="only read a drawing into a part JSON with OpenRouter")
    p.add_argument("pdf", type=Path)
    add_read_options(p, 0.40)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("agent", help="let an OpenRouter model build a part through the MCP tools")
    p.add_argument("spec", type=Path, help="part JSON")
    p.add_argument("--model", default=os.environ.get("CAD_MODEL", AGENT_MODEL))
    p.add_argument("--base-url", default=os.environ.get("CAD_API_BASE", DEFAULT_BASE_URL))
    p.add_argument("--docker", action="store_true", help="run the MCP server in the sandbox container")
    p.add_argument("--max-cost", type=float, default=0.10, help="stop when this run has cost this many USD")
    p.add_argument("--max-turns", type=int, default=25, help="stop after this many model calls")
    p.set_defaults(func=cmd_agent)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
