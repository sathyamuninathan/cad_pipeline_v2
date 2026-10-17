# Drawing2CAD

Turns a 2D technical drawing (PDF) into a validated 3D STEP model. An AI reads the drawing into a JSON part
description; a local MCP server builds the model with build123d in a Docker sandbox, validates it after every build
and exports STEP. Anything the drawing does not state clearly is flagged, never guessed silently.

```
drawing.pdf
 └─ 1. read      Claude Code (CLAUDE.md) or OpenRouter (reader.py)  →  extracted/<part>.json
     └─ 2. build    MCP server (mcp_server.py) in Docker, one cad.py process per build
         └─ 3. check   validation after every build (cad.py)
             └─ 4. result  out/<part>.step + out/<part>.report.json
```

## Files

```
main.py          the one command line: run, web, render, crop, check, lookup, evaluate, ... (py main.py -h)
spec.py          the part description: data model, consistency rules, clean-up
standards.py     ISO/DIN tables (threads, clearance holes, counterbores, keyways, grooves, fits, ISO 2768)
cad.py           build123d geometry: build, validate, compare with a reference STEP, build worker
mcp_server.py    the 20 MCP tools (new_part, load_drawing_spec, build_from_spec, add_hole, ..., export_step)
reader.py        drawing images, OpenRouter reading with repair loop, OpenRouter agent
webapp.py        local web page (web/ holds the page and a bundled three.js)
tests.py         all tests, no API calls
CLAUDE.md        workflow and drawing-reading rules for Claude Code (also used as the OpenRouter prompt)
standards/       the ISO/DIN tables as JSON, plus drawing_conventions.md
extracted/       saved part descriptions, one per drawing
part.schema.json JSON schema of the part description (generated from spec.py by tests.py)
out/             everything generated (not in Git): STEP files, reports, logs/, drawings/, evaluated/, web/
```

The drawings, reference STEP files and answer keys stay outside this folder in `..\Drawing2CAD\Drawing2CAD` and
`..\answer_keys`.

## Quick start (Windows PowerShell, in this folder)

```powershell
py -m pip install -r requirements.txt
docker build -t cad-mcp-v2 .                       # again after changing a .py file

py main.py web                                     # web page on http://127.0.0.1:8000
py main.py run --spec extracted\Sensoraufnahme.json --docker      # saved reading -> STEP (free)
py main.py run ..\Drawing2CAD\Drawing2CAD\Splint\Splint.pdf --docker   # read with OpenRouter (needs OPENROUTER_API_KEY)
py main.py evaluate                                # score all saved readings against the reference STEP files (free)
py tests.py                                        # all tests
```

With Claude Code instead of an API key: run `claude` in this folder and ask
*"Build a 3D model from ..\Drawing2CAD\Drawing2CAD\Splint\Splint.pdf"*. `.mcp.json` connects it to the MCP server in
Docker, and `CLAUDE.md` tells it the steps (`main.py render`, `check`, `lookup`, then the MCP tools, then `run --spec`).

`main.py run` exits with 0 when the model is valid or valid-but-needs-review, 1 when the build or a check failed.

## How the challenge requirements are met

| Requirement | Where |
|---|---|
| Recognise contours, dimensions, holes, features | `spec.py`: outlines from lines/arcs, rectangles, circles, slots; turned parts; holes, threads, counterbores, countersinks, keyways, grooves, extrusions, revolutions |
| Flag ambiguity instead of guessing | Every value has status, source, confidence; assumed values need a review flag (`Part.consistency_issues`). A blocking flag makes the status `needs_review` |
| Local MCP server, Docker, no cloud | `mcp_server.py` in the `cad-mcp-v2` image: `--network none --read-only --memory 2g --cpus 2 --pids-limit 256 --cap-drop ALL` |
| Sandbox with time limit | Docker flags + every build in its own `cad.py` process, killed after 60 s |
| Few structured tools, free code only as fallback | 19 structured tools; `run_script` with an allow-list (build123d and math only) |
| Validation after every build, machine-readable | Every tool returns the checks; `out/<part>.report.json` |
| Closed valid solid, dimensions, STEP re-import | `cad.py` validate section |
| Independent of the AI model | MCP works with any client; OpenRouter commands take `--model`; `run --spec` needs no model |
