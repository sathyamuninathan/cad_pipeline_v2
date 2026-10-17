# Drawing2CAD: turning a 2D drawing into a 3D model

This folder is a hackathon prototype (README.md lists the files). You (Claude Code) read a technical drawing (PDF), write a structured part
description as JSON, and build the 3D model **only through the `cad-builder` MCP server** (build123d in a Docker sandbox:
no network, time limit). Ambiguous information is flagged, never guessed silently.

## Workflow for "build a 3D model from <drawing.pdf>"

1. **Render the drawing:** `py main.py render "<drawing.pdf>"`, then look at every image it lists in
   `out/drawings/<name>/` (the full page and four zoomed tiles). Most drawings have no text layer; read the images.
   For small details (section views, tiny dimensions) zoom in with `py main.py crop "<drawing.pdf>" x0 y0 x1 y1`
   (fractions of the page, 0..1 from the top left) instead of writing your own scripts.
   Read `standards/drawing_conventions.md` once per session: projection method, line types, symbols, thread
   representation, title block, German terms.
2. **Write the part description** to `extracted/<part_id>.json`. Set `drawing_file` to the PDF file name only
   (e.g. `Rotation_dwg.pdf`, no folders). Use `part.schema.json` for the exact format and
   `..\answer_keys\3-Loch-Hebel.json` or `..\answer_keys\Sensoraufnahme.json` as examples of a finished description
   (only as format examples; never copy their values into a different part). Before writing, work out tangent points and
   derived values briefly in your reply.
   For every standard callout (thread, screw, keyway, retaining ring, fit, general tolerance) look the values up with
   `py main.py lookup <query>` (e.g. `lookup M8`, `lookup keyway 40`, `lookup groove shaft 20`,
   `lookup fit 20 H7/g6`, `lookup identify 6.6`; `lookup help` lists all). Never take these numbers from memory.
3. **Check it locally:** `py main.py check extracted/<part_id>.json`. It normalises the file, then checks
   schema, consistency rules and a test build. Fix every problem it lists and run it again until it prints `OK`.
   It also fills sizes that the drawing gives only by standard (lines `from standards:`) and prints
   `standards [warning]` notes when a read value contradicts a standard (e.g. keyway width vs shaft diameter): re-read
   that dimension on the drawing before accepting it.
   If it lists simplifications, try once to model that geometry exactly.
   `open_profile` usually means the outline runs back over itself (e.g. along the axis of a turned part):
   every edge of the outline must appear exactly once.
4. **Build through the MCP server**, in this order: `new_part` → `load_drawing_spec` (the full JSON as a string) →
   `build_from_spec` → `validate`. If a check fails, use its hint: fix the JSON and load it again, or adjust single
   features with the `add_*` / `remove_feature` tools. Then `export_step`.
5. **Write the report** so it also appears on the web page: `py main.py run --spec extracted/<part_id>.json --docker`.
6. **Answer the user** with: status (valid / needs review / not built), overall size, and every review flag, blocking
   ones first, with the assumed value and what to check on the drawing. Mention standards warnings and the fits /
   general tolerance found.

## Ground rules

- Do not open the reference models (`*.stp`, `*.prt`) or the answer key of the drawing you are reading. The model must
  come from the drawing alone. Afterwards the user may run `py main.py evaluate` to score it.
- Build only through the MCP tools; `run_script` is a last resort for geometry no other tool can express, and needs a
  review flag explaining why.
- Never invent a missing dimension without flagging it (rule 3 below).
- Everything here runs locally and costs nothing except your own usage. Do not call OpenRouter (`main.py read`,
  `main.py agent`, `main.py batch`, or `main.py run` with a PDF argument).
- On Windows use `py`, not `python`.
- The two sections below are also the prompt of the OpenRouter route (`reader.py`); keep their headings.

## Supported geometry (nothing else)

- A profile is exactly one of:
  {"segments": [...]}: a closed chain of "line" {"type":"line","from":[a,b],"to":[a,b]} and "arc" {"type":"arc","center":[a,b],
    "radius":r,"from":[a,b],"to":[a,b],"clockwise":false} segments, in order, each starting where the previous one ends
    (3 decimals). Run outlines counter-clockwise: convex round ends clockwise=false, concave fillets clockwise=true.
    Compute tangent points yourself where lines meet arcs.
  {"circle": {"center":[a,b], "radius":r}}
  {"rectangle": {"center":[a,b], "size":[w,h], "corner_radius":r}}  (corner_radius 0 for sharp corners)
  {"slot": {"center_1":[a,b], "center_2":[a,b], "width":w}}
- base_body, choose one:
  {"type":"extruded_profile", "profile":..., "extrude_height":t}: flat parts. Profile = outline in the top view (XY),
    extruded from z=0 to z=t (a plain number; record its source in dimensions).
  {"type":"revolved_profile", "profile":{"segments":[...]}}: lathe parts (shafts, bushings, pulleys). The profile is the
    half-section in [radius, z] coordinates (radius >= 0), rotated 360 degrees around the Z axis. Put the part axis on Z.
    Include steps, grooves, chamfers (as short lines) and fillets (as arcs) in the profile. A central bore can be part of it
    (inner radius > 0). Example stepped shaft: [0,0]->[17.5,0]->[17.5,35]->[22.5,35]->[22.5,100]->[0,100]->[0,0].
- features, applied in the listed order (list added material before cuts and holes):
  "extrusion": {"id","type":"extrusion","mode":"add"|"cut","plane":"XY"|"XZ"|"YZ","offset":o,"distance":d,"profile":...}
    plane XY: profile in [x,y], extruded along Z from z=o by d (negative d goes down). XZ: profile in [x,z] along Y.
    YZ: profile in [y,z] along X. Use for steps, raised rings, bosses, pockets, recesses (keyways: use "keyway").
  "revolution": {"id","type":"revolution","mode":"add"|"cut","axis_position":[x,y],"profile":{"segments":[...]}}
    a turned shape around a vertical axis through (x,y), profile in [radius, z]; e.g. a round boss with a fillet at its base.
  "hole": diameter, position, through or depth, optional chamfer (45-degree size, applied to both hole edges, Z holes only),
    "axis" "z" (default) | "x" | "y", "from_side" "max" (default, starts at the highest coordinate along the axis) | "min".
    position is [x,y] for axis z, [y,z] for axis x, [x,z] for axis y. A stepped bore is two holes (the deeper, smaller one
    through or with its depth, the larger one with its depth).
  "threaded_hole": thread like "M8" (modelled as the tap-drill hole), position, through or depth, axis, from_side.
  "slot": width, arc_center_1, arc_center_2, through or depth, cut downward from the top.
  "counterbore": screw "M6" (ISO 4762 socket head), position, optional hole_diameter / counterbore_diameter /
    counterbore_depth (use the drawing values when given; missing ones come from ISO 273 + DIN 974-1), series
    "medium" (default) | "fine" | "coarse", through or depth, axis, from_side.
  "countersink": screw "M6" (ISO 10642, 90 degrees), position, optional hole_diameter / countersink_diameter, angle_deg,
    through or depth, axis, from_side. The table countersink diameter is head + 0.3 mm: flag it (info) if the drawing
    does not give the diameter.
  "keyway": shaft or hub keyway (DIN 6885-1) around the Z axis: location "shaft" | "hub", form "A" (round ends) | "B"
    (square ends), shaft_diameter (bore diameter for a hub), length (overall, incl. round ends), z_start (lower end),
    optional width and depth (t1 shaft / t2 hub, from the table when omitted), angle_deg (side: 0 = +X, 90 = +Y),
    axis_position. Use this instead of an extrusion cut for keyways.
  "retaining_groove": DIN 471 (location "shaft") / DIN 472 ("bore") groove: nominal_diameter, z_start (lower face),
    optional groove_diameter and width (from the table when omitted), axis_position.
  Hole diameter, position, depth, chamfer, thread, screw, keyway, groove and slot values use the {"value":..., "status":...} form;
  extrusion and revolution numbers are plain numbers, with their sources recorded in dimensions.

## Rules for reading the drawing

1. Choose a clear origin (a hole center or a corner), state it in coordinate_system, and keep x to the right and y up as in the top view.
2. Every value gets: value, status, source, confidence.
   status "read" = written on the drawing; "derived" = computed from other dimensions or from tangency, symmetry, concentricity;
   "assumed" = not determinable from the drawing.
   Every "derived" and every "assumed" value MUST have an "evidence" text, including hole positions computed from a
   pitch or a bolt circle (e.g. "x = 71 / 2, symmetric pattern"). Every "assumed" value MUST also have a "review_flag" text.
   Values with confidence below 0.8 need a review_flag or an entry in review_flags.
   source = the view and the dimension text, e.g. "top view, '51,8'". confidence between 0 and 1.
3. Never guess silently. If a needed value is missing or unclear, still give your best estimate (you may use the drawing scale
   from the title block and the proportions of the drawing), mark it status "assumed" with a "review_flag" text, and add an entry
   to review_flags with severity "blocking_until_confirmed" and assumed_value. Values computed from an assumed value list it in
   depends_on.
4. Note other uncertainties (implicit tangency, a callout assumed to apply to several edges) as review_flags with severity "info".
5. If the part has geometry the supported types cannot express (draft angles, angled bosses, edge fillets on 3D edges),
   approximate it with the supported types where reasonable and add a review_flag with severity "warning" describing what
   was simplified or left out.
6. Use mm. Decimal commas on the drawing ("51,8") mean 51.8.
7. Read every view together and apply standard drawing conventions:
   - In a side view or section, check on which faces a thicker region protrudes. If it sticks out on both faces, it is
     centred on the thinner part (e.g. a 30 mm boss on a 20 mm plate protrudes 5 mm on each side: offset -5, distance 30).
   - Hidden or section lines of a bore that change width along its length mean a stepped bore: model each diameter
     with its own depth. Do not make a bore through at one diameter unless every view shows it that way.
   - Extension lines that end at the arc centres of a slot or keyway mean the dimension is measured centre to centre.
   - Small details (grooves, chamfers) at a large drawing scale: take positions from the dimension chain, and check the
     chain adds up to the overall length.
8. Standards and tolerances:
   - A value written on the drawing always wins over a table value. Use the table only for sizes the drawing gives by
     designation (`M6`, `DIN 6885 - A 12x8x80`, `DIN 471 - 20x1,2`) or leaves out; such values get status "derived",
     the table as source and the lookup evidence text.
   - If a read value disagrees with the standard (a 10 mm keyway on a 40 mm shaft), re-check the drawing first; if the
     drawing really says so, keep the drawing value and add an info review flag.
   - Put fits and individual tolerances in the value's "tolerance" field ("H7", "g6", "±0.1", "+0.1/-0.05") and the title
     block general tolerance in the part's "general_tolerance" ("ISO 2768-mK"). The model is built at the nominal size; the
     report lists the limits.
   - Undercuts (DIN 509), centre holes (DIN 332), edge conditions (ISO 13715), surface texture and geometric tolerances
     are recorded as info review flags unless the drawing dimensions them as geometry.
