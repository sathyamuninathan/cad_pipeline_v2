"""Local web page for the pipeline: upload a drawing PDF, watch the run, view the 3D result.

    py main.py web          then open http://127.0.0.1:8000

Runs only on this computer (bound to 127.0.0.1). The API key stays in the environment and never reaches the page.
Drawings can be read by Claude Code in the background (your Claude subscription), by OpenRouter, or from a saved reading.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HERE = Path(__file__).parent.resolve()
WEB = HERE / "web"
OUT = HERE / "out"
RUNS = OUT / "web"
CACHE = RUNS / "cache"
DATA_ROOT = HERE.parent / "Drawing2CAD"
MAX_UPLOAD = 30 * 1024 * 1024
NOISE = re.compile(r"warning|return self|warnings\.warn", re.I)
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".png": "image/png", ".stl": "model/stl",
         ".step": "application/step", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8"}

CLAUDE_TIMEOUT_S = 25 * 60
CLAUDE_MODELS = {"opus": "opus", "sonnet": "sonnet"}
CLAUDE_ALLOWED = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "PowerShell(py main.py render *)", "Bash(py main.py render *)",
    "PowerShell(py main.py crop *)", "Bash(py main.py crop *)",
    "PowerShell(py main.py check *)", "Bash(py main.py check *)",
    "PowerShell(py main.py lookup *)", "Bash(py main.py lookup *)",
    "PowerShell(py main.py run --spec *)", "Bash(py main.py run --spec *)",
    "mcp__cad-builder",
]
CLAUDE_DENIED = [
    "PowerShell(py main.py run *.pdf*)", "Bash(py main.py run *.pdf*)",
    "PowerShell(py main.py read *)", "Bash(py main.py read *)", "PowerShell(py main.py batch *)", "Bash(py main.py batch *)",
    "PowerShell(py main.py agent *)", "Bash(py main.py agent *)", "Read(**/*.stp)", "Read(**/*.STP)", "Read(**/*.prt)",
    "WebFetch", "WebSearch",
]

jobs: dict[str, dict] = {}
run_lock = threading.Lock()


def safe_pdf_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ()]", "_", Path(name).name).strip() or "drawing.pdf"
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def report_path(part: str) -> Path | None:
    path = (OUT / f"{part}.report.json").resolve()
    return path if path.parent == OUT.resolve() and path.exists() else None


def find_drawing(report: dict) -> Path | None:
    given = report.get("drawing_path")
    if given and Path(given).exists():
        return Path(given)
    name = report.get("drawing")
    if not name:
        return None
    for root in (RUNS, DATA_ROOT):
        hits = sorted(root.rglob(Path(name.replace("\\", "/")).name), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
        if hits:
            return hits[0]
    return None


def fresh(target: Path, source: Path) -> bool:
    return target.exists() and target.stat().st_mtime >= source.stat().st_mtime


def drawing_png(part: str) -> Path | None:
    rp = report_path(part)
    pdf = find_drawing(json.loads(rp.read_text(encoding="utf-8"))) if rp else None
    if not pdf:
        return None
    png = CACHE / f"{part}.png"
    if not fresh(png, pdf):
        import pymupdf
        CACHE.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf) as doc:
            page = doc[0]
            zoom = 2000 / max(page.rect.width, page.rect.height)
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(png)
    return png


def model_stl(part: str) -> Path | None:
    step = (OUT / f"{part}.step").resolve()
    if step.parent != OUT.resolve() or not step.exists():
        return None
    stl = CACHE / f"{part}.stl"
    if not fresh(stl, step):
        CACHE.mkdir(parents=True, exist_ok=True)
        code = ("import sys, warnings; warnings.filterwarnings('ignore');"
                "from build123d import import_step, export_stl;"
                "export_stl(import_step(sys.argv[1]), sys.argv[2], tolerance=0.01, angular_tolerance=0.1)")
        subprocess.run([sys.executable, "-c", code, str(step), str(stl)], capture_output=True, timeout=180)
    return stl if stl.exists() else None


def results() -> list[dict]:
    rows = []
    for rp in sorted(OUT.glob("*.report.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        rows.append({"part_id": r.get("part_id"), "status": r.get("status"), "created": r.get("created"),
                     "drawing": r.get("drawing"), "flags": len(r.get("review_flags", []))})
    return rows


def claude_exe() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home() / ".local" / "bin"
    for name in ("claude.exe", "claude"):
        if (home / name).exists():
            return str(home / name)
    return None


def mcp_config(docker: bool) -> Path:
    from mcp_server import launch_command
    command, args = launch_command(docker)
    server = {"type": "stdio", "command": command, "args": args}
    RUNS.mkdir(parents=True, exist_ok=True)
    path = RUNS / f"mcp_{'docker' if docker else 'local'}.json"
    path.write_text(json.dumps({"mcpServers": {"cad-builder": server}}, indent=2), encoding="utf-8")
    return path


def short(value, n: int = 140) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def describe_tool(name: str, args: dict) -> str:
    if name in ("Bash", "PowerShell"):
        return f"$ {short(args.get('command', ''), 160)}"
    if name in ("Read", "Write", "Edit"):
        path = str(args.get("file_path", ""))
        try:
            path = str(Path(path).resolve().relative_to(HERE))
        except (ValueError, OSError):
            pass
        return f"{name.lower()} {path}"
    if name.startswith("mcp__cad-builder__"):
        tool = name.split("__", 2)[2]
        brief = {k: v for k, v in args.items() if k != "spec_json"}
        return f"MCP {tool}" + (f" {short(json.dumps(brief), 100)}" if brief else "")
    return f"{name} {short(json.dumps(args), 100)}"


def run_claude(job: dict):
    exe = claude_exe()
    if not exe:
        job["log"].append("Claude Code was not found. Install it and log in once by running `claude` in PowerShell.")
        return
    pdf_rel = job["pdf"].relative_to(HERE).as_posix()
    run_cmd = "py main.py run --spec extracted/<part_id>.json" + (" --docker" if job["docker"] else "")
    prompt = (f"Build a 3D model from {pdf_rel} following CLAUDE.md exactly. You run unattended from the web page: "
              f"nobody can answer questions, so decide yourself and record every uncertainty as a review flag. "
              f"Run each command on its own, not chained in one line. In step 5 run: {run_cmd}. "
              f"Finish with a short summary for the user (status, size, review flags) and a last line `PART_ID: <part_id>`." + (" " + job["hint"] if job.get("hint") else ""))
    cmd = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", CLAUDE_MODELS.get(job["model"], "opus"),
           "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--strict-mcp-config", "--mcp-config", str(mcp_config(job["docker"])),
           "--add-dir", str(HERE.parent / "answer_keys"),
           "--allowedTools", *CLAUDE_ALLOWED, "--disallowedTools", *CLAUDE_DENIED]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY")}
    env["PYTHONIOENCODING"] = "utf-8"
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        env.setdefault(var, "1")
    job["log"].append(f"Claude Code ({job['model']}) is reading {job['file']} with your Claude account …")
    proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, text=True, encoding="utf-8", errors="replace")
    timer = threading.Timer(CLAUDE_TIMEOUT_S, proc.kill)
    timer.start()
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                if not NOISE.search(line):
                    job["log"].append(short(line, 300))
                continue
            kind, sub = ev.get("type"), ev.get("subtype")
            if kind == "system" and sub == "init":
                servers = ", ".join(f"{m.get('name')} {m.get('status')}" for m in ev.get("mcp_servers", []))
                job["log"].append(f"session started · model {ev.get('model')} · MCP: {servers or 'none'}")
            elif kind == "system" and sub == "api_retry":
                job["log"].append(f"  retrying API call ({ev.get('error')}, attempt {ev.get('attempt')})")
            elif kind == "system" and sub == "permission_denied":
                job["log"].append(f"  denied: {short(ev, 200)}")
            elif kind == "assistant" and not ev.get("parent_tool_use_id"):
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text", "").strip():
                        job["log"].append("Claude: " + short(block["text"], 400))
                    elif block.get("type") == "tool_use":
                        job["log"].append("  → " + describe_tool(block.get("name", ""), block.get("input") or {}))
            elif kind == "user":
                for block in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
                    if block.get("type") == "tool_result" and block.get("is_error"):
                        content = block.get("content")
                        text = content if isinstance(content, str) else " ".join(c.get("text", "") for c in content or [] if isinstance(c, dict))
                        job["log"].append("    ! " + short(text, 200))
            elif kind == "result":
                job["summary"] = ev.get("result") or ""
                secs = (ev.get("duration_ms") or 0) / 1000
                job["log"].append(f"Claude Code finished: {'error' if ev.get('is_error') else 'ok'} · {ev.get('num_turns')} turns · {secs:.0f} s")
                for d in ev.get("permission_denials") or []:
                    job["log"].append(f"  blocked by the web page's rules: {short(d.get('tool_name'))} {short(d.get('tool_input'), 120)}")
        proc.wait()
    finally:
        timer.cancel()
    if proc.returncode not in (0, None):
        job["log"].append(f"Claude Code exited with code {proc.returncode}")


def newest_report(since: float) -> Path | None:
    reports = [p for p in OUT.glob("*.report.json") if p.stat().st_mtime >= since]
    return max(reports, key=lambda p: p.stat().st_mtime) if reports else None


def run_job(job: dict):
    if job["mode"] == "claude":
        with run_lock:
            job["state"] = "running"
            started = time.time()
            try:
                run_claude(job)
            except Exception as e:
                job["log"].append(f"could not run Claude Code: {e}")
            m = re.search(r"PART_ID:\s*`?([\w.\-]+)`?", job.get("summary", ""))
            report = report_path(m.group(1)) if m else None
            if report is None or report.stat().st_mtime < started:
                report = newest_report(started)
            if report:
                job["part_id"] = report.name[: -len(".report.json")]
                summary = re.sub(r"\n?PART_ID:.*$", "", job.get("summary", "")).strip()
                if summary:
                    (OUT / f"{job['part_id']}.claude.md").write_text(summary, encoding="utf-8")
                job["state"] = "done"
            else:
                job["state"] = "failed"
            job["finished"] = datetime.now().isoformat(timespec="seconds")
        return
    cmd = [sys.executable, "-u", str(HERE / "main.py"), "run", str(job["pdf"]), "--max-cost", str(job["max_cost"])]
    if job["mode"] == "saved":
        cmd.append("--offline")
    if job["docker"]:
        cmd.append("--docker")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with run_lock:
        job["state"] = "running"
        job["log"].append("$ py main.py run " + " ".join([f'"{job["file"]}"'] + cmd[5:]))
        try:
            proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                    text=True, encoding="utf-8", errors="replace")
            report = None
            for line in proc.stdout:
                line = line.rstrip()
                if not line or NOISE.search(line):
                    continue
                job["log"].append(line)
                m = re.match(r"\s*report:\s+(.+\.report\.json)\s*$", line)
                if m:
                    report = Path(m.group(1))
            proc.wait()
        except Exception as e:
            job["log"].append(f"could not start the pipeline: {e}")
            report = None
        if report and report.exists():
            job["part_id"] = report.name[: -len(".report.json")]
            job["state"] = "done"
        else:
            job["state"] = "failed"
        job["finished"] = datetime.now().isoformat(timespec="seconds")


class Handler(BaseHTTPRequestHandler):
    server_version = "Drawing2CAD"

    def log_message(self, *args):
        pass

    def send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status: int = 200):
        self.send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), TYPES[".json"])

    def file(self, path: Path | None, download: str | None = None):
        if not path or not path.exists():
            return self.json({"error": "not found"}, 404)
        extra = {"Content-Disposition": f'attachment; filename="{download}"'} if download else None
        self.send(200, path.read_bytes(), TYPES.get(path.suffix.lower(), "application/octet-stream"), extra)

    def do_GET(self):
        url = urlparse(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        if not parts or parts == ["index.html"]:
            return self.file(WEB / "index.html")
        if parts[0] == "vendor":
            path = (WEB / Path(*parts)).resolve()
            return self.file(path if path.is_relative_to(WEB.resolve()) else None)
        if parts[:2] == ["api", "results"]:
            return self.json(results())
        if parts[:2] == ["api", "status"]:
            return self.json({"claude_code": claude_exe() is not None,
                              "openrouter_key": bool(os.environ.get("OPENROUTER_API_KEY"))})
        if parts[:2] == ["api", "job"] and len(parts) == 3:
            job = jobs.get(parts[2])
            if not job:
                return self.json({"error": "unknown job"}, 404)
            start = int(parse_qs(url.query).get("from", ["0"])[0])
            return self.json({k: job[k] for k in ("id", "state", "part_id", "file", "mode")} | {"log": job["log"][start:]})
        if parts[:2] == ["api", "result"] and len(parts) >= 3:
            part = parts[2]
            rp = report_path(part)
            if not rp:
                return self.json({"error": "no result for this part"}, 404)
            what = parts[3] if len(parts) > 3 else ""
            if what == "":
                report = json.loads(rp.read_text(encoding="utf-8"))
                report.pop("tool_calls", None)
                report["has_model"] = (OUT / f"{part}.step").exists() and report.get("step_file") is not None
                report["has_drawing"] = find_drawing(report) is not None
                note = OUT / f"{part}.claude.md"
                if note.exists() and note.stat().st_mtime >= rp.stat().st_mtime - 600:
                    report["claude_summary"] = note.read_text(encoding="utf-8")
                return self.json(report)
            if what == "drawing.png":
                return self.file(drawing_png(part))
            if what == "model.stl":
                return self.file(model_stl(part))
            if what == "model.step":
                return self.file(OUT / f"{part}.step", f"{part}.step")
            if what == "report.json":
                return self.file(rp, f"{part}.report.json")
            if what == "spec.json":
                spec = Path(json.loads(rp.read_text(encoding="utf-8")).get("spec", ""))
                ok = spec.exists() and spec.resolve().is_relative_to(HERE)
                return self.file(spec if ok else None, f"{part}.json")
        self.json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/run":
            return self.json({"error": "not found"}, 404)
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            return self.json({"error": "forbidden"}, 403)
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_UPLOAD:
            return self.json({"error": f"upload a PDF up to {MAX_UPLOAD // 2**20} MB"}, 400)
        body = self.rfile.read(size)
        if not body.startswith(b"%PDF"):
            return self.json({"error": "this is not a PDF file"}, 400)
        q = parse_qs(url.query)
        try:
            max_cost = min(max(float(q.get("max_cost", ["0.30"])[0]), 0.01), 2.0)
        except ValueError:
            max_cost = 0.30
        job_id = uuid.uuid4().hex[:10]
        folder = RUNS / job_id
        folder.mkdir(parents=True)
        pdf = folder / safe_pdf_name(unquote(q.get("name", ["drawing.pdf"])[0]))
        pdf.write_bytes(body)
        mode = q.get("mode", ["claude"])[0]
        job = {"id": job_id, "state": "queued" if run_lock.locked() else "starting", "pdf": pdf, "file": pdf.name,
               "mode": mode if mode in ("claude", "ai", "saved") else "claude", "max_cost": max_cost,
               "model": q.get("model", ["opus"])[0], "docker": q.get("docker", ["1"])[0] == "1",
               "log": [], "part_id": None, "summary": ""}
        jobs[job_id] = job
        threading.Thread(target=run_job, args=(job,), daemon=True).start()
        self.json({"id": job_id})


def serve(port: int = 8000, open_browser: bool = True):
    RUNS.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"Drawing2CAD page: {url}   (Ctrl+C to stop)")
    print("Claude Code: " + ("found" if claude_exe() else "not found (install it to read drawings with your Claude account)"))
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("note: OPENROUTER_API_KEY is not set, so the OpenRouter option will not work")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
