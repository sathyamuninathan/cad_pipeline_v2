"""Reading drawings: page images for Claude Code, and the OpenRouter route (vision model -> part JSON, agent).

Only read_drawing() and run_agent() call a paid API; render() and crop() are free.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from spec import clean, simplifications

HERE = Path(__file__).parent.resolve()
DRAWINGS = HERE / "out" / "drawings"
LOGS = HERE / "out" / "logs"
MAX_SIDE = 1568
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
READ_MODEL = "anthropic/claude-sonnet-5.5"
AGENT_MODEL = "anthropic/claude-haiku-5.5"


# ---------------------------------------------------------------- page images

def page_tiles(pdf: Path):
    """The whole page plus four overlapping zoomed quarters, as (name, pymupdf page, clip rectangle or None)."""
    import pymupdf
    page = pymupdf.open(pdf)[0]
    w, h = page.rect.width, page.rect.height
    tiles = [("page", page, None)]
    overlap, names = 0.08, ["tile_top_left", "tile_top_right", "tile_bottom_left", "tile_bottom_right"]
    for i, (row, col) in enumerate([(0, 0), (0, 1), (1, 0), (1, 1)]):
        x0, y0 = max(0, col * w / 2 - overlap * w / 2), max(0, row * h / 2 - overlap * h / 2)
        clip = pymupdf.Rect(x0, y0, min(w, x0 + w / 2 * (1 + 2 * overlap)), min(h, y0 + h / 2 * (1 + 2 * overlap)))
        tiles.append((names[i], page, clip))
    return tiles


def pixmap(page, clip=None):
    import pymupdf
    rect = clip or page.rect
    z = MAX_SIDE / max(rect.width, rect.height)
    return page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip)


def render(pdf: Path):
    out = DRAWINGS / pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    tiles = page_tiles(pdf)
    for name, page, clip in tiles:
        pixmap(page, clip).save(out / f"{name}.png")
    text = tiles[0][1].get_text().strip()
    print(f"wrote {out.relative_to(HERE)}: " + ", ".join(f"{n}.png" for n, _, _ in tiles))
    print("text layer: " + (text[:400].replace("\n", " | ") if text else "none (read the images)"))


def crop(pdf: Path, box: list[str]):
    import pymupdf
    try:
        x0, y0, x1, y1 = (min(max(float(v), 0.0), 1.0) for v in box)
    except ValueError:
        sys.exit("crop needs four numbers between 0 and 1: x0 y0 x1 y1")
    if x1 - x0 < 0.02 or y1 - y0 < 0.02:
        sys.exit("crop region is too small (x1 > x0 and y1 > y0, at least 0.02 each)")
    out = DRAWINGS / pdf.stem
    out.mkdir(parents=True, exist_ok=True)
    page = pymupdf.open(pdf)[0]
    w, h = page.rect.width, page.rect.height
    clip = pymupdf.Rect(x0 * w, y0 * h, x1 * w, y1 * h)
    name = f"crop_{x0:.2f}_{y0:.2f}_{x1:.2f}_{y1:.2f}.png"
    pixmap(page, clip).save(out / name)
    zoom = max(w, h) / max(clip.width, clip.height)
    print(f"wrote {(out / name).relative_to(HERE)} ({zoom:.1f}x the page image)")


def page_images(pdf: Path) -> list[str]:
    return ["data:image/png;base64," + base64.b64encode(pixmap(page, clip).tobytes("png")).decode()
            for _, page, clip in page_tiles(pdf)]


# ---------------------------------------------------------------- OpenRouter (any OpenAI-compatible API)

def api_key(base_url: str) -> str:
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("CAD_API_KEY", "")
    if not key and "openrouter.ai" in base_url:
        sys.exit("OPENROUTER_API_KEY is not set. Run: setx OPENROUTER_API_KEY \"sk-or-...\" and open a new PowerShell.")
    return key


def model_prices(base_url: str, model: str) -> tuple[float, float]:
    try:
        r = httpx.get(f"{base_url}/models", timeout=20)
        for m in r.json().get("data", []):
            if m.get("id") == model:
                p = m.get("pricing", {})
                return float(p.get("prompt", 0)), float(p.get("completion", 0))
    except Exception:
        pass
    return 0.0, 0.0


class Budget:
    def __init__(self, max_cost: float, max_turns: int, prices: tuple[float, float]):
        self.max_cost, self.max_turns, self.prices = max_cost, max_turns, prices
        self.cost = self.prompt_tokens = self.completion_tokens = self.turns = 0

    def add(self, usage: dict):
        self.turns += 1
        p, c = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        self.prompt_tokens += p
        self.completion_tokens += c
        reported = usage.get("cost")
        self.cost += float(reported) if reported is not None else p * self.prices[0] + c * self.prices[1]

    def exceeded(self) -> str | None:
        if self.cost >= self.max_cost:
            return f"cost limit reached ({self.cost:.4f} of {self.max_cost} USD)"
        if self.turns >= self.max_turns:
            return f"turn limit reached ({self.turns} model calls)"
        return None

    def summary(self) -> dict:
        return {"model_calls": self.turns, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens, "cost_usd": round(self.cost, 5)}


def chat(client: httpx.Client, base_url: str, key: str, payload: dict) -> dict:
    for attempt in range(4):
        r = client.post(f"{base_url}/chat/completions", json=payload,
                        headers={"Authorization": f"Bearer {key}", "X-Title": "drawing2cad hackathon"})
        if r.status_code == 401:
            raise RuntimeError("the API key was not accepted (401). Check it with: Invoke-RestMethod https://openrouter.ai/api/v1/key "
                               "-Headers @{Authorization=\"Bearer $env:OPENROUTER_API_KEY\"}")
        if r.status_code in (402, 403):
            raise RuntimeError(f"budget or permission error from the API ({r.status_code}): {r.text[:300]}")
        if r.status_code in (429, 500, 502, 503) and attempt < 3:
            time.sleep(2 ** attempt * 2)
            continue
        if r.status_code >= 400:
            try:
                detail = r.json().get("error", {}).get("message", r.text)
            except ValueError:
                detail = r.text
            raise RuntimeError(f"API error {r.status_code} for model '{payload['model']}': {str(detail)[:400]}")
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"API error: {data['error']}")
        return data
    raise RuntimeError("API kept failing after retries")


def save_log(name: str, record: dict) -> str:
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / name).write_text(json.dumps(record, indent=2), encoding="utf-8")
    return f"out/logs/{name}"


def safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "_", text)


# ---------------------------------------------------------------- drawing -> part JSON with a vision model

def claude_md_section(title: str) -> str:
    text = (HERE / "CLAUDE.md").read_text(encoding="utf-8")
    start = text.index(f"## {title}")
    end = text.find("\n## ", start + 1)
    return text[start:end if end > 0 else None].split("\n", 1)[1].strip()


def read_prompt() -> str:
    return ("You read 2D technical drawings (ISO/DIN conventions, dimensions in mm) and write a structured part description "
            "as JSON.\nThe JSON is used to build a 3D model, so geometry must be exact and complete.\n\n"
            "Supported geometry (nothing else):\n" + claude_md_section("Supported geometry (nothing else)")
            + "\n\nRules:\n" + claude_md_section("Rules for reading the drawing")
            + "\n\nAnswer format: first a short section starting with \"Calculations:\" where you compute tangent points and "
              "derived values\n(keep it brief, at most about 40 lines), then the complete JSON in one ```json code block. "
              "Nothing after the code block.")


def example_json(exclude: str, keys_dir: Path) -> str:
    for name in ("Sensoraufnahme", "3-Loch-Hebel"):
        path = keys_dir / f"{name}.json"
        if name != exclude and path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for k in ("ground_truth_check", "reference_step"):
                data.pop(k, None)
            return json.dumps(data, indent=1, ensure_ascii=False)
    return ""


def parse_json(text: str) -> dict:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced[-1])
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found in the answer")
    return json.loads(text[start:end + 1])


def find_reference(pdf: Path) -> Path | None:
    steps = [p for p in pdf.parent.glob("*.stp") if "PMI" not in p.name.upper()]
    return steps[0] if steps else None


def read_drawing(args) -> dict:
    """Read args.pdf with args.model, repair up to args.repairs times, write <args.out>/<part_id>.json.
    Returns the run record; record["json"] is the written file or None."""
    from cad import check_spec, compare
    key = api_key(args.base_url)
    part_id = re.sub(r"_dwg$", "", args.pdf.stem)
    print(f"drawing {args.pdf.name} | model {args.model} | limit {args.max_cost} USD", flush=True)

    example = example_json(part_id, args.keys)
    user = [{"type": "text", "text":
             f"Drawing of part '{part_id}'. Image 1 is the whole sheet, images 2 to 5 are zoomed quarters "
             f"(top-left, top-right, bottom-left, bottom-right) for reading small text.\n\n"
             f"Write the part description JSON with part_id \"{part_id}\", drawing_file \"{args.pdf.name}\", "
             f"schema_version \"0.1\", units \"mm\".\n\n"
             + (f"Example of the expected format, for a different part:\n{example}" if example else "")}]
    user += [{"type": "image_url", "image_url": {"url": url}} for url in page_images(args.pdf)]
    messages = [{"role": "system", "content": read_prompt()}, {"role": "user", "content": user}]

    budget = Budget(args.max_cost, 10, model_prices(args.base_url, args.model))
    data, issues, solid = None, ["no answer yet"], None
    reasoning_off = [True]

    def ask(client):
        payload = {"model": args.model, "messages": messages, "max_tokens": args.max_tokens, "usage": {"include": True}}
        if args.reasoning_tokens > 0:
            payload["reasoning"] = {"max_tokens": args.reasoning_tokens}
        elif reasoning_off[0]:
            payload["reasoning"] = {"enabled": False}
            payload["temperature"] = 0
        else:
            payload["reasoning"] = {"effort": args.effort}
        try:
            return chat(client, args.base_url, key, payload)
        except RuntimeError as e:
            if reasoning_off[0] and args.reasoning_tokens <= 0 and "reasoning" in str(e).lower():
                print(f"  model requires reasoning; using reasoning effort '{args.effort}'", flush=True)
                reasoning_off[0] = False
                payload["reasoning"] = {"effort": args.effort}
                payload.pop("temperature", None)
                return chat(client, args.base_url, key, payload)
            raise

    with httpx.Client(timeout=300) as client:
        improve_done, attempt = args.no_improve, 0
        while attempt <= args.repairs:
            if budget.exceeded():
                print("stopped:", budget.exceeded())
                break
            reply = ask(client)
            usage = reply.get("usage", {})
            budget.add(usage)
            choice = reply["choices"][0]
            text = choice["message"].get("content") or ""
            reasoning = choice["message"].get("reasoning") or ""
            details = usage.get("completion_tokens_details") or {}
            print(f"  answer: {len(text)} characters, finish reason '{choice.get('finish_reason')}', "
                  f"{usage.get('completion_tokens', '?')} output tokens"
                  + (f" ({details.get('reasoning_tokens')} for reasoning)" if details.get("reasoning_tokens") else "")
                  + (f", {len(reasoning)} characters of reasoning" if reasoning else ""), flush=True)
            if not text.strip() and "{" in reasoning:
                text = reasoning[reasoning.find("{"):]
            complete = re.search(r"```(?:json)?\s*\{.*?\}\s*```", text, re.S) or text.rstrip().endswith("}")
            if choice.get("finish_reason") == "length" and not complete:
                text = ""
                print("     answer was cut off at the token limit; raise --max-tokens or lower --reasoning-tokens", flush=True)
            messages.append({"role": "assistant", "content": text})
            try:
                candidate = parse_json(text)
                candidate.update(part_id=part_id, drawing_file=args.pdf.name, schema_version="0.1", units="mm")
                clean(candidate)
                cand_issues, cand_solid = check_spec(candidate)
            except (ValueError, json.JSONDecodeError) as e:
                candidate, cand_issues, cand_solid = None, [f"answer was not valid JSON: {e}"], None
            attempt += 1
            print(f"  attempt {attempt}: {len(cand_issues)} problem(s), cost so far {budget.cost:.4f} USD", flush=True)
            for i in cand_issues[:8]:
                print("     -", i)
            if not cand_issues:
                data, issues, solid = candidate, [], cand_solid
                simplified = simplifications(data)
                if improve_done or not simplified or budget.cost * 2 > args.max_cost:
                    break
                improve_done = True
                print(f"  {len(simplified)} simplification(s) flagged; asking once for the exact geometry", flush=True)
                messages.append({"role": "user", "content":
                    "You flagged these simplifications:\n- " + "\n- ".join(simplified) + "\nModel them exactly with the "
                    "supported types (segments with tangent arcs, extrusions with offsets on both faces, revolutions, "
                    "stepped holes). Keep a flag only for what the supported types truly cannot express (draft angles, "
                    "fillets on 3D edges). Answer with the complete JSON in the same format."})
                continue
            if data is None:
                data, issues = candidate, cand_issues
            messages.append({"role": "user", "content": "The JSON has these problems. Fix them and answer with the complete "
                                                        "corrected JSON:\n- " + "\n- ".join(cand_issues)})

    run = {"part_id": part_id, "drawing": str(args.pdf), "model": args.model, "budget": budget.summary(),
           "started": datetime.now().isoformat(timespec="seconds"), "remaining_problems": issues, "json": None}
    if data is None:
        print("\nresult: no usable JSON")
        return run
    args.out.mkdir(parents=True, exist_ok=True)
    out_json = args.out / f"{part_id}.json"
    out_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    run["json"] = str(out_json)

    flags = data.get("review_flags", [])
    print(f"\nwrote {out_json.relative_to(HERE) if out_json.is_relative_to(HERE) else out_json}")
    print(f"  {len(data.get('dimensions', []))} dimensions, {len(data.get('features', []))} features, {len(flags)} review flags")
    for f in flags:
        print(f"     flag [{f.get('severity')}] {f.get('field')}: {f.get('reason')}")

    run["simplifications"] = simplifications(data)
    if solid is not None:
        run["build"] = {"ok": True, "volume": round(solid.volume, 2)}
        ref = find_reference(args.pdf)
        if ref:
            score = compare(solid, ref)
            run["score"] = score
            print(f"\nscore against {ref.name}: {'MATCH' if score['match'] else 'NO MATCH'} | differing volume "
                  f"{score['differing_volume_pct']} % | holes {score['holes_found']}/{score['holes_expected']} "
                  f"{'ok' if score['holes_ok'] else 'wrong'} | size deviation {score['size_deviation']} | pose {score['best_pose']}")
    else:
        run["build"] = {"ok": False, "error": "; ".join(issues)}
        print("\nno buildable model: " + "; ".join(issues)[:300])
    b = run["budget"]
    print(f"cost {b['cost_usd']} USD | {b['prompt_tokens'] + b['completion_tokens']} tokens | {b['model_calls']} model calls")
    print("run log: " + save_log(f"extract__{part_id}__{safe(args.model)}__{datetime.now():%Y%m%d_%H%M%S}.json", run))
    return run


# ---------------------------------------------------------------- agent: a model drives the MCP tools itself

AGENT_PROMPT = """You build 3D CAD parts by calling tools on a CAD server. Units are millimetres.

The part description (spec) extracted from the technical drawing is given below. It is already loaded on the server,
and the server checks your model against it after every tool call.

How to work:
1. Create the base body from the spec: "extruded_profile" -> create_base_body with thickness=extrude_height and either
   segments (if the spec profile has segments) or profile (if it is a circle or rectangle), "revolved_profile" ->
   create_turned_body with its segments.
2. Add every feature in the order of the spec, with the same ids: "extrusion" -> add_extrusion, "revolution" ->
   add_revolution, "hole" -> add_hole, "threaded_hole" -> add_threaded_hole, "slot" -> add_slot.
   Pass axis and from_side for holes when the spec has them.
3. Read the validation result returned by each call. failed_checks lists what is still missing or wrong;
   each check has a hint. Fix problems by calling the tool again with the same id.
4. When validation passes, call export_step once, then stop and reply with a short summary.

Rules:
- Use only values from the spec. Never invent dimensions.
- Values marked "assumed" or with a review flag must still be built as given, and you must list them in your final summary.
- run_script is a last resort for geometry no other tool can make. Do not use it for holes, slots or the base body.
- Keep replies short. Do not explain your plan before calling tools."""
PRELOADED_TOOLS = {"new_part", "load_drawing_spec"}


def short_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        parts.append(f"segments={len(v)}" if k == "segments" else "code=..." if k == "code" else f"{k}={v}")
    return ", ".join(parts)[:120]


async def run_agent(spec_path: Path, model: str, base_url: str, key: str, use_docker: bool, max_cost: float,
                    max_turns: int) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp_server import launch_command
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec.pop("ground_truth_check", None)
    spec.pop("reference_step", None)
    budget = Budget(max_cost, max_turns, model_prices(base_url, model))
    command, cmd_args = launch_command(use_docker)
    params = StdioServerParameters(command=command, args=cmd_args, cwd=str(HERE))
    record = {"part_id": spec["part_id"], "model": model, "server": "docker" if use_docker else "local",
              "started": datetime.now().isoformat(timespec="seconds"), "tool_calls": [], "outcome": None}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                       "parameters": t.inputSchema}}
                     for t in (await session.list_tools()).tools if t.name not in PRELOADED_TOOLS]
            origin = spec.get("coordinate_system", {}).get("origin", "")
            await session.call_tool("new_part", {"part_id": spec["part_id"], "origin_description": origin})
            loaded = json.loads((await session.call_tool("load_drawing_spec", {"spec_json": json.dumps(spec)})).content[0].text)
            if not loaded.get("ok"):
                raise RuntimeError(f"spec rejected by server: {loaded}")

            messages = [{"role": "system", "content": AGENT_PROMPT},
                        {"role": "user", "content": "Build this part.\n\nSpec:\n" + json.dumps(spec, indent=1)}]
            last_validation, exported = None, False
            with httpx.Client(timeout=180) as client:
                while True:
                    stop = budget.exceeded()
                    if stop:
                        record["outcome"] = f"stopped: {stop}"
                        break
                    data = chat(client, base_url, key, {"model": model, "messages": messages, "tools": tools,
                                                        "temperature": 0, "usage": {"include": True}})
                    budget.add(data.get("usage", {}))
                    message = data["choices"][0]["message"]
                    messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
                    calls = message.get("tool_calls") or []
                    if not calls:
                        record["final_reply"] = message.get("content", "")
                        record["outcome"] = "finished"
                        break
                    for call in calls:
                        name = call["function"]["name"]
                        try:
                            args = json.loads(call["function"]["arguments"] or "{}")
                        except json.JSONDecodeError:
                            args, text = {}, json.dumps({"ok": False, "error": {"code": "bad_json_arguments"}})
                        else:
                            result = await session.call_tool(name, args)
                            text = result.content[0].text if result.content else "{}"
                        try:
                            parsed = json.loads(text)
                        except json.JSONDecodeError:
                            parsed = {"ok": False, "error": {"code": "tool_rejected", "message": text[:300]}}
                        validation = parsed.get("validation", {})
                        if validation:
                            last_validation = validation
                        if name == "export_step" and validation.get("passed"):
                            exported = True
                        status = ("error " + str(parsed["error"].get("code"))) if parsed.get("error") else \
                            (("PASSED" if validation.get("passed") else f"FAILED ({len(validation.get('failed_checks', []))} open)") if validation else "ok")
                        print(f"  [{budget.turns:2d}] {name}({short_args(args)}) -> {status}   cost so far {budget.cost:.4f} USD", flush=True)
                        record["tool_calls"].append({"tool": name, "args": args, "status": status})
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})

    record.update(budget=budget.summary(), exported=exported, passed=bool(last_validation and last_validation.get("passed")) and exported)
    return record
