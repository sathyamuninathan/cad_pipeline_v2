# Drawing conventions for reading German / ISO technical drawings

Short reference for reading the drawings in this dataset. It describes the standards; it does not reproduce them.
Use `py drawing_tools.py lookup <query>` for numeric values (threads, holes, keyways, grooves, fits, tolerances).

## 1. Views and projection (ISO 5456-2, ISO 128-30)

- European drawings use **first-angle projection (Projektionsmethode 1)**. The symbol in the title block is a truncated
  cone: in method 1 the circles are to the **right** of the cone outline.
  - The view seen from the **left** is placed on the **right** of the front view.
  - The view seen from **above** (top view, Draufsicht) is placed **below** the front view.
- Third-angle projection (method 3, USA) has the circles to the left of the cone and the views swapped. If the symbol shows
  method 3, swap the placement rules above; if no symbol is shown, assume method 1 and say so in an info flag.
- Getting the method wrong mirrors the part. Cross-check with a feature that is visible in two views (a hole, a step).
- Section views: `A-A` marks the cutting plane in another view, arrows show the viewing direction. Hatching (thin 45 degree
  lines) marks cut material. Ribs, shafts, pins, screws and keys cut lengthwise are drawn **without** hatching.
- Detail views: a circled area with a letter (`X`) and a scale such as `X (5:1)`. Dimensions in a detail are true size;
  only the drawing is enlarged.
- Half section (Halbschnitt): one half of a symmetric part drawn in section, the other half as a view, separated by a centre line.

## 2. Line types (ISO 128-20 / ISO 128-24)

| Line | Meaning |
|---|---|
| Thick continuous | Visible edges and outlines |
| Thin continuous | Dimension and extension lines, hatching, thread roots, fillet transitions (imaginary intersections) |
| Thin dashed | Hidden edges (Verdeckte Kanten) |
| Thin dash-dot | Centre lines, axes, pitch circles, symmetry lines |
| Thick dash-dot | Surfaces with special requirements (e.g. hardened zone) |
| Thin dash-double-dot | Adjacent parts, extreme positions, original contour before machining |
| Thin freehand / zigzag | Break lines (part shown shortened: the real length is the dimension, not the drawn length) |

## 3. Dimension symbols (ISO 129-1, ISO 3040)

| Symbol | Meaning | Note for the JSON |
|---|---|---|
| `Ø20` | diameter | radius 10 in revolved profiles |
| `R5` | radius | arc radius |
| `SØ` / `SR` | sphere diameter / radius | revolved arc |
| `□20` | square 20 x 20 | rectangle |
| `t=5` / `t 5` | thickness of a flat part | extrude_height |
| `4x Ø6,6` | four identical features | one feature per instance |
| `2x45°` or `2x45°` on an edge | chamfer 2 mm at 45 degrees | short line in the profile |
| `(25)` | auxiliary / reference dimension, not toleranced | still valid geometry |
| `25` underlined or framed `[25]` | not to scale / theoretically exact (in GD&T) | trust the number, not the drawing |
| `⌴` | counterbore | `counterbore` feature |
| `⌵` | countersink | `countersink` feature |
| `↧` | depth | depth value |
| `⌒25` | arc length | |
| `1:5` / `◁` | taper / slope | flag as simplified if not modellable |
| `TK Ø80` / `LK Ø80` | pitch circle (Teilkreis / Lochkreis) | hole positions = r * (cos, sin) |
| `gleichm. verteilt` / `EQS` | equally spaced | derive the angles |

- Decimal commas: `51,8` means 51.8.
- Chain dimensions should add up to the overall length; if they don't, a value was misread.
- A dimension on a hidden line or in a section always refers to the real feature, not the drawn line.

## 4. Threads (ISO 6410, ISO 261)

- External thread: crest (outer diameter) thick, root thin. Internal thread in section: crest (minor diameter, the tap
  drill bore) thick, root (nominal diameter) thin. In an end view the root is a thin **3/4 circle**.
- Callouts: `M8` coarse, `M8x1` fine, `M8-6H` with tolerance class (6H internal, 6g external), `M8 ↧12` thread depth 12.
  Left-hand: `M8-LH`.
- A blind threaded hole usually shows two depths: thread depth and drill depth (with a 118 degree cone tip, not modelled).
- Model internal threads as the tap-drill hole (`threaded_hole`), external threads at the nominal diameter.

## 5. Edges and surfaces

- **ISO 13715 edge symbol**: a corner symbol with `+0,3` (burr / material allowed) or `-0,3` (break edge, remove up to
  0.3 mm). These are not geometry to model; record them as info.
- Undimensioned small chamfers on turned parts are usually 0.5x45 degrees or given in a general note (`Kanten gebrochen 0,5x45°`).
- **Surface texture (ISO 1302 / ISO 21920)**: the check-mark symbol with `Ra 1,6` / `Rz 6,3`. A symbol next to the title
  block followed by symbols in brackets is the general surface for the whole part. Record, do not model.
- Fillets drawn with a thin line where two faces meet (`Lichtkante`) mean a rounded transition with no visible edge.

## 6. Tolerances

- **General tolerances (ISO 2768)**: title block text `ISO 2768-mK` (or older `DIN ISO 2768-m`). Applies to every dimension
  without its own tolerance. Record it in the part field `general_tolerance`.
- **Fits (ISO 286)**: capital letter = hole (`Ø20 H7`), small letter = shaft (`Ø20 g6`). The model is built at the nominal
  size; write the code into the value's `tolerance` field so the report lists the limits.
- **Individual deviations**: `25 ±0,1`, `25 +0,1/-0,05`, `25 +0,2/0`. Write them into `tolerance` as `"±0.1"`, `"+0.1/-0.05"`.
- **Geometric tolerances (ISO 1101)**: framed boxes such as `⏥ 0,05 A` with datum letters `A` in triangles. Record them as
  info review flags (the extension goal is to carry them into STEP AP242 PMI).

## 7. Standard parts and features: what the callout means

| Callout on the drawing | Meaning | Feature / lookup |
|---|---|---|
| `Ø6,6` with `4x` around a bolt circle | ISO 273 medium clearance hole for M6 | `hole`; `lookup identify 6.6` |
| `DIN 974 - 11x6,4` | counterbore Ø11 x 6.4 deep | `counterbore` (from the numbers) |
| `Senkung DIN 974 - M6 ISO 4762` | counterbore for an M6 socket head screw | `counterbore` with screw "M6" |
| `ISO 15065 - M6` / `DIN 74 - A M6` | 90 degree countersink for an M6 flat head screw | `countersink` with screw "M6" |
| `Passfedernut DIN 6885 - A 10x8x40` | shaft keyway, form A, b 10, l 40, t1 from table | `keyway` |
| `Sicherungsring DIN 471 - 20x1,2` | groove on a 20 shaft, d2 and m from table | `retaining_groove` location shaft |
| `Sicherungsring DIN 472 - 30x1,2` | groove in a 30 bore | `retaining_groove` location bore |
| `Freistich DIN 509 - E1x0,4` | undercut at a ground shoulder | revolved profile detail or info flag |
| `Zentrierbohrung DIN 332 - A3,15x6,7` | centre hole on the shaft end face | info flag, usually not modelled |
| `Splint ISO 1234 - 5x40` | split pin for a 5 mm hole, length 40 | `lookup splitpin 5` |

## 8. Title block (Schriftfeld, ISO 7200)

Read it every time: part name (Benennung), drawing number, **scale (Massstab)**, material (Werkstoff, e.g. `S235JR`,
`C45`, `EN AW-6082`), general tolerance, surface note, projection symbol, sheet size. A scale such as `2:1` only changes the
drawing size; the dimension values are always true size.

## 9. German terms

| German | English |
|---|---|
| Bohrung / Durchgangsbohrung / Sackloch | hole / through hole / blind hole |
| Gewinde / Gewindetiefe / Kernloch | thread / thread depth / tap-drill hole |
| Senkung / Flachsenkung / Kegelsenkung | countersink or counterbore / counterbore / countersink |
| Fase / Rundung / Radius | chamfer / fillet / radius |
| Nut / Passfedernut / Einstich / Freistich | groove or slot / keyway / groove / undercut |
| Absatz / Bund / Schulter | step / collar / shoulder |
| Langloch | slot |
| Teilkreis / Lochkreis | pitch circle / bolt circle |
| gleichmaessig verteilt | equally spaced |
| Schnitt / Ansicht / Einzelheit | section / view / detail |
| Werkstueckkanten / Kanten gebrochen | workpiece edges / edges broken |
| Ausformschraege / Entformungsschraege | draft angle |
| Allgemeintoleranz | general tolerance |
| Rautiefe / Oberflaeche | roughness / surface |
| Werkstoff / Rohteil | material / blank |
