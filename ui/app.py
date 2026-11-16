"""
Orchestrator Command Center — FastAPI + SSE dashboard.
Читает данные из sessions/, journal.jsonl, run.log. SSE для live-стриминга.
"""
import asyncio
import html as html_lib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import session_pool  # Session Pool: интроспекция браузеров + детект логинов

app = FastAPI(title="Qmeka — thinking workspace")

BASE = Path(__file__).parent.parent  # repo root
SESSIONS = BASE / "sessions"
REGISTRY = SESSIONS / "registry.json"
JOURNAL = BASE / "journal.jsonl"
RUN_LOG = BASE / "run.log"
METRICS_FILE = BASE / "metrics.json"
PROJECTS_FILE = BASE / "qmeka-projects.json"
PROJECTS_DIR = BASE / "qmeka-projects"
USER_PRESETS_FILE = BASE / "user_presets.json"
PIPELINE_FILE = BASE / "pipeline.json"
CONFIG_FILE = BASE / "config.json"

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


# ── Helpers ──────────────────────────────────────────────────────────

def _read_json(path):
    """Read JSON file safely."""
    try:
        p = Path(path)
        if p.exists() and p.stat().st_size > 0:
            return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return {}


def _read_jsonl(path, limit=50):
    """Read last N lines from JSONL file."""
    lines = []
    try:
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            lines.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            return lines[-limit:]
    except Exception:
        pass
    return []


def _tail_log(path, lines=30):
    """Read last N lines from log file."""
    try:
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                all_lines = f.readlines()
            return [l.rstrip() for l in all_lines[-lines:]]
    except Exception:
        pass
    return []


def _fmt_ts(ts):
    """Format epoch/ISO timestamp to HH:MM:SS."""
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        except Exception:
            return str(ts)
    s = str(ts)
    return s.split(" ")[-1] if s else ""


def _list_roles():
    """List available role names from roles/ directory."""
    roles_dir = BASE / "roles"
    if not roles_dir.exists():
        return []
    return sorted(f.stem for f in roles_dir.glob("*.md"))


def _write_json(path, data, indent=2):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")


def _slug_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:32]
    return (slug or "project") + "-" + uuid.uuid4().hex[:6]


def _projects_default():
    now = datetime.now().isoformat()
    cfg = _read_json(CONFIG_FILE) or {}
    mode = cfg.get("target_mode") or "scratch"
    path = cfg.get("target_project")
    pid = "scratch"
    name = "Scratch"
    if mode == "project" and path:
        pid = _slug_id(Path(str(path)).name)
        name = Path(str(path)).name
    return {
        "active_id": pid,
        "projects": [{
            "id": pid,
            "name": name,
            "mode": mode if mode in ("scratch", "project") else "scratch",
            "path": path,
            "created": now,
            "updated": now,
        }],
    }


def _projects_load():
    data = _read_json(PROJECTS_FILE)
    if not data or not data.get("projects"):
        data = _projects_default()
        _write_json(PROJECTS_FILE, data)
    return data


def _projects_save(data):
    _write_json(PROJECTS_FILE, data)
    return data


def _project_by_id(store, pid):
    for p in store.get("projects") or []:
        if p.get("id") == pid:
            return p
    return None


def _projects_active(store=None):
    store = store if store is not None else _projects_load()
    p = _project_by_id(store, store.get("active_id"))
    if p:
        return p
    if store.get("projects"):
        return store["projects"][0]
    return None


def _project_pipeline_path(pid):
    return PROJECTS_DIR / pid / "pipeline.json"


def _sync_active_project(proj):
    """Mirror the Qmeka desk into config.json + pipeline.json for fsm_v2."""
    cfg = _read_json(CONFIG_FILE) or {}
    cfg["target_mode"] = proj.get("mode") or "scratch"
    cfg["target_project"] = proj.get("path") if proj.get("mode") == "project" else None
    cfg["active_project_id"] = proj.get("id")
    _write_json(CONFIG_FILE, cfg, indent=4)
    ppath = _project_pipeline_path(proj["id"])
    if ppath.exists():
        PIPELINE_FILE.write_bytes(ppath.read_bytes())
    elif PIPELINE_FILE.exists():
        ppath.parent.mkdir(parents=True, exist_ok=True)
        ppath.write_bytes(PIPELINE_FILE.read_bytes())
    return cfg


def _upsert_project_from_target(mode, path):
    """First-run / attach-folder also becomes a Qmeka desk."""
    store = _projects_load()
    now = datetime.now().isoformat()
    if mode == "project" and path:
        existing = next((p for p in store["projects"] if p.get("path") == path), None)
        if existing:
            existing["mode"] = "project"
            existing["updated"] = now
            if not existing.get("name"):
                existing["name"] = Path(str(path)).name
            store["active_id"] = existing["id"]
            proj = existing
        else:
            name = Path(str(path)).name
            proj = {
                "id": _slug_id(name),
                "name": name,
                "mode": "project",
                "path": path,
                "created": now,
                "updated": now,
            }
            store["projects"].append(proj)
            store["active_id"] = proj["id"]
    else:
        scratch = next((p for p in store["projects"] if p.get("mode") == "scratch"), None)
        if not scratch:
            scratch = {
                "id": "scratch",
                "name": "Scratch",
                "mode": "scratch",
                "path": None,
                "created": now,
                "updated": now,
            }
            store["projects"].insert(0, scratch)
        store["active_id"] = scratch["id"]
        proj = scratch
    _projects_save(store)
    _sync_active_project(proj)
    return proj


def _derive_parallel_groups(pipeline: dict) -> dict:
    """Siblings that share the same parents become one parallel_group."""
    stages = pipeline.get("stages") or []
    producers = {}
    for s in stages:
        for o in s.get("outputs") or []:
            producers[o] = s.get("id")
    buckets = {}
    for s in stages:
        parents = []
        for inp in s.get("inputs") or []:
            if inp == "task":
                continue
            pid = producers.get(inp)
            if pid and pid != s.get("id"):
                parents.append(pid)
        key = tuple(sorted(set(parents)))
        buckets.setdefault(key, []).append(s)
        s.pop("parallel_group", None)
        role = f"{s.get('role') or ''} {s.get('id') or ''}".lower()
        if "judge" in role or "meta-review" in role:
            n = s.get("in_slots")
            if n is None:
                n = len([i for i in (s.get("inputs") or []) if i != "task"]) or 2
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = 2
            s["in_slots"] = max(2, min(4, n))
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        gid = "fork-start" if not key else "fork-" + "-".join(key)
        if len(gid) > 48:
            gid = gid[:48]
        for s in group:
            s["parallel_group"] = gid
    return pipeline


def _pipeline_from_builder(data: dict) -> dict:
    current = _read_json(PIPELINE_FILE) or {}
    stages = data.get("stages") or []
    services = dict(data.get("services") or current.get("services") or {})
    urls = {
        "zai": "https://z.ai",
        "deepseek": "https://chat.deepseek.com",
        "qwen": "https://chat.qwen.ai",
        "google": "https://gemini.google.com",
    }
    for s in stages:
        svc = s.get("service")
        if svc and svc not in services and svc not in ("opencode", "local", ""):
            services[svc] = {"url": urls.get(svc, "")}
    out = {
        "name": data.get("name") or current.get("name") or "pipeline",
        "description": data.get("description") or current.get("description") or "",
        "stages": stages,
        "services": services,
    }
    if data.get("loops"):
        out["loops"] = data["loops"]
    if data.get("iterations") is not None:
        out["iterations"] = data["iterations"]
    elif current.get("iterations") is not None:
        out["iterations"] = current["iterations"]
    return _derive_parallel_groups(out)


def _save_pipeline_for_active(data: dict) -> dict:
    data = _pipeline_from_builder(data) if "stages" in data else _derive_parallel_groups(data)
    _write_json(PIPELINE_FILE, data)
    active = _projects_active()
    if active:
        _write_json(_project_pipeline_path(active["id"]), data)
        store = _projects_load()
        p = _project_by_id(store, active["id"])
        if p:
            p["updated"] = datetime.now().isoformat()
            _projects_save(store)
    return data


def _load_active_pipeline() -> dict:
    active = _projects_active()
    if active:
        ppath = _project_pipeline_path(active["id"])
        if ppath.exists():
            return _read_json(ppath) or {}
    return _read_json(PIPELINE_FILE) or {}


def _user_presets_load():
    data = _read_json(USER_PRESETS_FILE)
    return data if isinstance(data, list) else []


def _user_presets_save(items):
    _write_json(USER_PRESETS_FILE, items)
    return items


def _all_presets():
    builtin = []
    for p in _default_presets():
        item = dict(p)
        item["origin"] = "builtin"
        builtin.append(item)
    user = []
    for p in _user_presets_load():
        item = dict(p)
        item["origin"] = "user"
        user.append(item)
    return builtin, user


def _find_preset(preset_id: str):
    builtin, user = _all_presets()
    for p in user + builtin:
        if p.get("id") == preset_id:
            return p
    return None


def _get_current_session():
    """Most recent run that belongs to the active Qmeka project."""
    reg = _read_json(REGISTRY)
    sessions = reg.get("sessions", {})
    if not sessions:
        return None, {}
    active = _projects_active()
    pid = (active or {}).get("id")
    if pid:
        owned = {}
        for name, info in sessions.items():
            spid = info.get("project_id") or "scratch"
            if spid == pid:
                owned[name] = info
        sessions = owned
        if not sessions:
            return None, {}
    latest_name = max(sessions, key=lambda k: sessions[k].get("updated", ""))
    return latest_name, sessions[latest_name]


def _get_session_status(session_name):
    """Read status.json from a session directory."""
    sdir = SESSIONS / session_name
    return _read_json(sdir / "status.json")


def _get_session_metrics(session_name):
    """Read metrics.json from a session directory."""
    sdir = SESSIONS / session_name
    return _read_json(sdir / "metrics.json")


def _get_artifact(session_name, iter_num, filename):
    """Read an artifact file from a session iteration."""
    sdir = SESSIONS / session_name / f"{iter_num:02d}"
    fpath = sdir / filename
    if fpath.exists():
        return fpath.read_text(encoding="utf-8")
    return None


def _format_duration(seconds):
    """Format seconds as HH:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Routes ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard (Screen B)."""
    session_name, session_info = _get_current_session()
    status = _get_session_status(session_name) if session_name else {}
    metrics = _get_session_metrics(session_name) if session_name else {}

    # Journal events
    journal = _read_jsonl(JOURNAL, limit=30)

    # Latest task (for "auto" pipeline config)
    current_task = ""
    for ev in reversed(journal):
        if ev.get("event") == "pipeline_start" and ev.get("task"):
            current_task = ev["task"]
            break

    # Log tail
    log_lines = _tail_log(RUN_LOG, lines=40)

    # Parse log lines into structured data
    parsed_logs = []
    for line in log_lines:
        parsed = _parse_log_line(line)
        if parsed:
            parsed_logs.append(parsed)

    # Build timeline from journal
    timeline = _build_timeline(journal, status)

    # Get last plan/reply
    current_iter = status.get("detail", {}).get("iter", 1) if status else 1
    last_plan = _get_artifact(session_name, current_iter, "plan.md") if session_name else None
    last_reply = _get_artifact(session_name, current_iter, "reply.md") if session_name else None

    # Plan/reply sizes
    plan_size = _file_size(session_name, current_iter, "plan.md")
    reply_size = _file_size(session_name, current_iter, "reply.md")

    # Verdict from reply
    verdict = _extract_verdict(last_reply) if last_reply else "pending"

    # Metrics
    counters = metrics.get("counters", {})
    histograms = metrics.get("histograms", {})

    # Session uptime
    uptime = status.get("detail", {}).get("total_s", 0) if status else 0

    ctx = {
        "request": request,
        "session_name": session_name or "Нет активной сессии",
        "status": status.get("status", "unknown"),
        "iter": current_iter,
        "current_iter": current_iter,
        "total_iters": status.get("detail", {}).get("N", 0) if status else 0,
        "timeline": timeline,
        "parsed_logs": parsed_logs,
        "last_plan": last_plan,
        "last_reply": last_reply,
        "plan_size": plan_size,
        "reply_size": reply_size,
        "verdict": verdict,
        "counters": counters,
        "histograms": histograms,
        "uptime": _format_duration(uptime),
        "status_detail": status.get("detail", {}),
        "roles": _list_roles(),
        "current_task": current_task,
        "pipeline_name": (_load_active_pipeline().get("name") or "Default"),
        "target_hint": _desk_hint(),
    }
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@app.get("/api/status")
async def api_status():
    """JSON status endpoint for HTMX polling."""
    session_name, session_info = _get_current_session()
    status = _get_session_status(session_name) if session_name else {}
    metrics = _get_session_metrics(session_name) if session_name else {}
    return JSONResponse({
        "session": session_name,
        "status": status.get("status", "unknown"),
        "detail": status.get("detail", {}),
        "counters": metrics.get("counters", {}),
        "histograms": metrics.get("histograms", {}),
    })


@app.get("/api/journal", response_class=HTMLResponse)
async def api_journal(request: Request, limit: int = 20):
    """HTML journal endpoint for HTMX polling."""
    journal = _read_jsonl(JOURNAL, limit=limit)

    # Build timeline HTML
    html = '<div class="timeline-track"><div class="timeline-line"></div>'
    events_seen = set()
    for ev in journal:
        event = ev.get("event", "")
        if event in ("session_start", "iter_start", "iter_done", "session_done", "session_failed"):
            if event not in events_seen:
                label = _timeline_label(event, ev)
                state = "done" if event != "iter_start" else "active"
                html += f'<div class="timeline-node"><div class="timeline-dot {state}"></div><div class="timeline-label {state}">{label}</div></div>\n'
                events_seen.add(event)
    html += '</div>'

    return HTMLResponse(content=html)


@app.get("/api/journal/json")
async def api_journal_json(limit: int = 50):
    """JSON journal endpoint for client-side refresh."""
    events = _read_jsonl(JOURNAL, limit=limit)
    for ev in events:
        if "timestamp" in ev:
            ev["ts_fmt"] = _fmt_ts(ev.get("timestamp"))
    return JSONResponse(events)


@app.get("/api/log", response_class=HTMLResponse)
async def api_log(request: Request, lines: int = 30):
    """HTML log endpoint for HTMX polling."""
    log_lines = _tail_log(RUN_LOG, lines=lines)
    parsed = [_parse_log_line(l) for l in log_lines if _parse_log_line(l)]

    html = ""
    for entry in parsed:
        ts = entry.get("ts", "").split(" ")[-1] if entry.get("ts") else ""
        level = entry.get("level", "INFO")
        session = entry.get("session", "")
        msg = entry.get("msg", "")

        sid_part = f'<span class="log-sid">[{session}]</span> ' if session else ""
        html += f'<div class="log-line"><span class="log-ts">{ts}</span><span class="log-level {level}">{level}</span>{sid_part}<span class="log-msg">{msg}</span></div>\n'

    return HTMLResponse(content=html)


@app.get("/api/metrics")
async def api_metrics():
    """JSON metrics endpoint."""
    session_name, _ = _get_current_session()
    metrics = _get_session_metrics(session_name) if session_name else {}
    return JSONResponse(metrics)


@app.get("/api/registry")
async def api_registry():
    """JSON registry endpoint (all sessions)."""
    reg = _read_json(REGISTRY)
    return JSONResponse(reg)


@app.get("/api/artifact/{session}/{iter_num}/{filename}")
async def api_artifact(session: str, iter_num: int, filename: str):
    """Read an artifact file."""
    content = _get_artifact(session, iter_num, filename)
    if content is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"content": content, "size": len(content)})


# ── Screen routes ────────────────────────────────────────────────────

def _desk_hint():
    active = _projects_active()
    mode = (active or {}).get("mode") or "scratch"
    path = (active or {}).get("path")
    name = (active or {}).get("name") or "Scratch"
    if mode == "project" and path:
        return f"{name} — {path}"
    return f"{name} — sketches stay in this run"


@app.get("/launch")
async def launch_page():
    """Launch lives on start (/). Keep the old URL as a redirect."""
    return RedirectResponse(url="/", status_code=302)


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    """Session history for the active Qmeka project."""
    reg = _read_json(REGISTRY)
    sessions = reg.get("sessions", {})
    active = _projects_active()
    pid = (active or {}).get("id")
    if pid:
        sessions = {
            name: info for name, info in sessions.items()
            if (info.get("project_id") or "scratch") == pid
        }
    return templates.TemplateResponse(request, "history.html", {
        "sessions": sessions,
        "project_name": (active or {}).get("name") or "Scratch",
    })


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(request: Request):
    """Pipeline builder."""
    pipeline = _load_active_pipeline()
    roles = []
    roles_dir = BASE / "roles"
    if roles_dir.exists():
        roles = sorted(f.stem for f in roles_dir.glob("*.md"))
    services = list(pipeline.get("services", {}).keys())
    return templates.TemplateResponse(request, "pipeline.html", {
        "pipeline": pipeline,
        "roles": roles,
        "services": services,
    })


@app.get("/splash", response_class=HTMLResponse)
async def splash_page(request: Request):
    """Branded splash screen (preview)."""
    return templates.TemplateResponse(request, "splash.html")


@app.get("/backlog", response_class=HTMLResponse)
async def backlog_page(request: Request):
    """Backlog questions (Screen D)."""
    # Parse backlog.md from latest session
    session_name, _ = _get_current_session()
    items = []
    if session_name:
        backlog_file = SESSIONS / session_name / "backlog.md"
        if backlog_file.exists():
            text = backlog_file.read_text(encoding="utf-8")
            items = _parse_backlog(text, session_name)
    return templates.TemplateResponse(request, "backlog.html", {
        "items": items,
    })


@app.get("/debug", response_class=HTMLResponse)
async def debug_page(request: Request):
    """Debug & diagnostics (Screen E)."""
    import shutil
    import sys as _sys

    session_name, _ = _get_current_session()
    status = _get_session_status(session_name) if session_name else {}
    journal = _read_jsonl(JOURNAL, limit=30)
    for ev in journal:
        ev["ts_fmt"] = _fmt_ts(ev.get("timestamp"))

    config = _read_json(BASE / "config.json")
    cdp_port = config.get("cdp_port", 9222)

    # Real environment checks
    cdp_ok = _check_port(cdp_port)
    opencode_ok = bool(shutil.which("opencode"))

    return templates.TemplateResponse(request, "debug.html", {
        "cdp_port": cdp_port,
        "cdp_ok": cdp_ok,
        "opencode_ok": opencode_ok,
        "python_version": _sys.version.split()[0],
        "platform": _sys.platform,
        "workdir": str(BASE),
        "session_name": session_name,
        "status": status.get("status", "unknown"),
        "iter": status.get("detail", {}).get("iter", 0),
        "total_iters": status.get("detail", {}).get("N", 0),
        "journal_events": journal,
    })


# ── API: Launch ──────────────────────────────────────────────────────

@app.post("/api/launch")
async def api_launch(request: Request):
    """Start a new orchestrator session."""
    form = await request.form()
    task = form.get("task", "")
    n = int(form.get("n", 3))
    generated_pipeline = form.get("generated_pipeline", "")

    if not task.strip():
        return JSONResponse({"ok": False, "error": "Task is empty"})

    # If a generated pipeline was provided, save it first
    if generated_pipeline:
        try:
            pipeline_data = json.loads(generated_pipeline)
            _save_pipeline_for_active(pipeline_data)
        except Exception:
            pass  # fall back to existing pipeline.json

    # Launch fsm_v2.py in background
    import subprocess
    cmd = [
        r"C:\Python314\python.exe",
        str(BASE / "fsm_v2.py"),
        "--task", task,
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE),
        creationflags=0x00000008,  # DETACHED_PROCESS
    )

    return JSONResponse({"ok": True, "pid": proc.pid})


# ── API: Readiness check ────────────────────────────────────────────

@app.get("/api/readiness", response_class=HTMLResponse)
async def api_readiness(request: Request):
    """Check environment readiness for launch. Returns HTML fragments for HTMX.
    Real login check via Playwright CDP when Chrome is up."""
    items = []
    config = _read_json(BASE / "config.json")
    cdp_port = config.get("cdp_port", 9222)

    # 1. CDP port — fast check
    cdp_ok = _check_port(cdp_port)
    items.append((f"Chrome / CDP (port {cdp_port})", cdp_ok,
                  "connected" if cdp_ok else "not responding"))

    # 2. opencode — always ok (CLI tool)
    items.append(("opencode", True, "available"))

    # 3. Browser agents — real login check via Playwright
    if cdp_ok:
        try:
            import sys
            sys.path.insert(0, str(BASE))
            from chat_driver import ChatDriver
            driver = ChatDriver(headless=True)
            await driver.start()
            result = await driver.readiness()
            services = result.get("services", {})
            await driver.stop()
            for svc_name in ["zai", "deepseek", "qwen", "google"]:
                info = services.get(svc_name, {})
                if isinstance(info, dict):
                    logged_in = info.get("logged_in", False)
                    label = info.get("name", svc_name.upper())
                    login_url = info.get("login_url", "") if not logged_in else None
                    items.append((label, logged_in,
                                  "logged in" if logged_in else "NOT logged in",
                                  login_url))
                else:
                    items.append((svc_name.upper(), False, "unknown"))
        except Exception as e:
            items.append(("browser agents", False, f"check failed: {e}"))
    else:
        for svc_id in ["zai", "deepseek", "qwen", "google"]:
            items.append((svc_id.upper(), False, "needs CDP"))

    compact = str(request.query_params.get("compact", "")).lower() in ("1", "true", "yes")
    if compact:
        return HTMLResponse(content=_render_checks_compact(items))
    return HTMLResponse(content=_render_checks(items))


# ── Diagnostics bundle ───────────────────────────────────────────────

_SENSITIVE_KEYS = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "cookie", "session", "auth", "credential", "jwt", "bearer",
)


def _sanitize(obj, path=""):
    """Recursively strip sensitive values from a dict/list."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key_lower = str(k).lower()
            if any(s in key_lower for s in _SENSITIVE_KEYS):
                out[k] = "<redacted>"
            else:
                out[k] = _sanitize(v, f"{path}.{k}")
        return out
    if isinstance(obj, list):
        return [_sanitize(v, path) for v in obj]
    if isinstance(obj, str):
        # Redact JWT-looking strings (base64url.header.payload.signature)
        parts = obj.split(".")
        if len(parts) >= 3 and obj.startswith("eyJ") and len(obj) > 40:
            return "<redacted-token>"
        return obj
    return obj


@app.get("/api/diagnostics")
async def api_diagnostics():
    """Build a diagnostic bundle (zip): logs + sanitized config + system snapshot.
    Attaches as a downloadable file — no secrets included."""
    import io
    import platform
    import shutil
    import sys as _sys
    import zipfile
    from fastapi.responses import Response

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. run.log — last 500 lines
        log_lines = _tail_log(RUN_LOG, lines=500)
        zf.writestr("run.log", "\n".join(log_lines))

        # 2. journal.jsonl — last 500 lines
        journal_text = ""
        if JOURNAL.exists():
            try:
                lines = JOURNAL.read_text(encoding="utf-8").strip().split("\n")
                journal_text = "\n".join(lines[-500:])
            except Exception:
                pass
        zf.writestr("journal.jsonl", journal_text)

        # 3. sanitized config.json
        cfg = _read_json(BASE / "config.json")
        zf.writestr(
            "config.json",
            json.dumps(_sanitize(cfg), ensure_ascii=False, indent=2),
        )

        # 4. system + environment snapshot
        config_for_port = cfg.get("cdp_port", 9222)
        snapshot = {
            "platform": platform.platform(),
            "python": _sys.version,
            "python_executable": _sys.executable,
            "cwd": str(BASE),
            "cdp_port_listening": _check_port(config_for_port),
            "browsers": session_pool.probe_browsers(),
            "providers": session_pool.probe_providers(),
            "agents": {},
        }
        # CLI agents availability
        for cmd in ("opencode", "claude", "codex", "grok", "hermes", "cursor"):
            snapshot["agents"][cmd] = bool(shutil.which(cmd))
        zf.writestr(
            "snapshot.json",
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
        )

    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        content=buf.read(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=qmeka-diagnostics-{stamp}.zip"
        },
    )


@app.post("/api/control/stop")
async def api_control_stop():
    """Soft stop at checkpoint. (TODO: сигнал в fsm через journal/файл-флаг.)"""
    return JSONResponse({"ok": False, "error": "not implemented yet",
                         "message": "Мягкий стоп пока не связан с fsm — требуется флаг-файл/journal-сигнал"})


@app.post("/api/control/emergency")
async def api_control_emergency():
    """Emergency stop. (TODO: kill fsm PID из registry.)"""
    return JSONResponse({"ok": False, "error": "not implemented yet",
                         "message": "Аварийный стоп пока не связан с fsm — требуется PID-менеджмент"})


@app.post("/api/coords")
async def api_save_coords(request: Request):
    """Save input coordinates. Пишет в реальный ключ fsm: opencode_input_xy [x, y]."""
    coords = await request.json()
    config = _read_json(BASE / "config.json")
    # совместимость: принимаем {x,y} или {opencode_x, opencode_y} → массив
    x = coords.get("x") or coords.get("opencode_x")
    y = coords.get("y") or coords.get("opencode_y")
    if x is not None and y is not None:
        config["opencode_input_xy"] = [int(x), int(y)]
    else:
        config["input_coords"] = coords  # сырой вариант — на всякий случай
    (BASE / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return JSONResponse({"ok": True, "opencode_input_xy": config["opencode_input_xy"]})


@app.patch("/api/backlog/{item_id}")
async def api_backlog_update(item_id: str, request: Request):
    """Update backlog item status."""
    data = await request.json()
    return JSONResponse({"ok": True, "id": item_id, "status": data.get("status")})


# ── API: Pipeline builder ───────────────────────────────────────────

@app.get("/api/pipeline")
async def api_pipeline_get():
    """Get pipeline for the active Qmeka project."""
    return JSONResponse(_load_active_pipeline())


@app.post("/api/pipeline")
async def api_pipeline_save(request: Request):
    """Save pipeline for the active Qmeka project."""
    data = await request.json()
    saved = _save_pipeline_for_active(data)
    return JSONResponse({"ok": True, "pipeline": saved})


@app.get("/api/roles")
async def api_roles_list():
    """List available roles from roles/ directory."""
    roles_dir = BASE / "roles"
    if not roles_dir.exists():
        return JSONResponse([])
    roles = sorted(f.stem for f in roles_dir.glob("*.md"))
    return JSONResponse(roles)


@app.get("/api/role/{role_name}")
async def api_role_get(role_name: str):
    """Get role content."""
    role_path = BASE / "roles" / f"{role_name}.md"
    if not role_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    content = role_path.read_text(encoding="utf-8")
    return JSONResponse({"name": role_name, "content": content})


@app.post("/api/role/{role_name}")
async def api_role_save(role_name: str, request: Request):
    """Save role content."""
    data = await request.json()
    content = data.get("content", "")
    role_path = BASE / "roles" / f"{role_name}.md"
    role_path.write_text(content, encoding="utf-8")
    return JSONResponse({"ok": True})


@app.get("/api/services")
async def api_services_list():
    """List available services."""
    pipeline = _read_json(BASE / "pipeline.json")
    services = list(pipeline.get("services", {}).keys())
    return JSONResponse(services)


@app.get("/api/agents")
async def api_agents_scan():
    """Scan for installed CLI agents on the system."""
    import shutil
    agents = []
    # known CLI agents: name, command, type
    known = [
        {"id": "opencode", "name": "opencode", "cmd": "opencode", "type": "local", "icon": "⚙"},
        {"id": "claude", "name": "Claude Code", "cmd": "claude", "type": "local", "icon": "🤖"},
        {"id": "codex", "name": "Codex CLI", "cmd": "codex", "type": "local", "icon": "💻"},
        {"id": "grok", "name": "Grok Build", "cmd": "grok", "type": "local", "icon": "⚡"},
        {"id": "hermes", "name": "Hermes Agent", "cmd": "hermes", "type": "local", "icon": "🏛"},
        {"id": "cursor", "name": "Cursor CLI", "cmd": "cursor", "type": "local", "icon": "📝"},
    ]
    for a in known:
        path = shutil.which(a["cmd"])
        agents.append({
            **a,
            "installed": path is not None,
            "path": path or "",
        })
    # browser-based agents (always available if CDP running)
    agents.append({"id": "zai", "name": "z.ai (browser)", "cmd": "", "type": "browser", "icon": "🌐", "installed": True})
    agents.append({"id": "deepseek", "name": "DeepSeek (browser)", "cmd": "", "type": "browser", "icon": "🔍", "installed": True})
    agents.append({"id": "qwen", "name": "Qwen (browser)", "cmd": "", "type": "browser", "icon": "🧠", "installed": True})
    return JSONResponse(agents)


@app.get("/api/presets")
async def api_presets_list():
    """Built-in presets plus named user templates."""
    builtin, user = _all_presets()
    return JSONResponse({"builtin": builtin, "user": user})


@app.post("/api/presets")
async def api_presets_save(request: Request):
    """Save the current pipeline as a named user template."""
    data = await request.json()
    name = (data.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "name required"}, status_code=400)
    pipeline = data.get("pipeline") or _load_active_pipeline()
    if not isinstance(pipeline, dict):
        return JSONResponse({"ok": False, "error": "pipeline required"}, status_code=400)
    pipeline = _derive_parallel_groups(dict(pipeline))
    item = {
        "id": "user-" + uuid.uuid4().hex[:8],
        "name": name,
        "description": (data.get("description") or "").strip(),
        "icon": data.get("icon") or "◆",
        "stages": len(pipeline.get("stages") or []),
        "origin": "user",
        "pipeline": pipeline,
    }
    items = _user_presets_load()
    items.append(item)
    _user_presets_save(items)
    return JSONResponse({"ok": True, "preset": item})


@app.delete("/api/presets/{preset_id}")
async def api_presets_delete(preset_id: str):
    """Delete a user template. Built-in presets cannot be removed."""
    items = _user_presets_load()
    kept = [p for p in items if p.get("id") != preset_id]
    if len(kept) == len(items):
        return JSONResponse({"ok": False, "error": "user template not found"}, status_code=404)
    _user_presets_save(kept)
    return JSONResponse({"ok": True})


@app.post("/api/presets/apply/{preset_id}")
async def api_preset_apply(preset_id: str):
    """Apply a built-in or user preset to the active project."""
    preset = _find_preset(preset_id)
    if not preset:
        return JSONResponse({"ok": False, "error": "preset not found"}, status_code=404)
    saved = _save_pipeline_for_active(preset["pipeline"])
    return JSONResponse({"ok": True, "pipeline": saved})


@app.post("/api/generate-pipeline")
async def api_generate_pipeline(request: Request):
    """Use an AI agent to generate an optimal pipeline for a task."""
    data = await request.json()
    task = data.get("task", "").strip()
    agent = data.get("agent", "opencode")  # which agent to use for generation

    if not task:
        return JSONResponse({"ok": False, "error": "No task provided"})

    # Read the pipeline-generator system prompt
    gen_prompt_path = BASE / "roles" / "pipeline-generator.md"
    if not gen_prompt_path.exists():
        return JSONResponse({"ok": False, "error": "pipeline-generator.md not found"})

    system_prompt = gen_prompt_path.read_text(encoding="utf-8")
    full_prompt = f"{system_prompt}\n\n---\n\n## Задача пользователя\n\n{task}\n\n---\n\nСгенерируй оптимальный пайплайн для этой задачи. Верни ТОЛЬКО JSON."

    # Try to use opencode for generation
    try:
        from opencode_driver import opencode_run
        import asyncio
        gen_session_dir = str(BASE / "sessions" / "_pipeline_gen")
        os.makedirs(gen_session_dir, exist_ok=True)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: opencode_run(full_prompt, gen_session_dir)
        )
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Agent error: {str(e)}"})

    # Parse JSON from response
    import re
    json_match = re.search(r'\{[\s\S]*\}', result)
    if not json_match:
        return JSONResponse({"ok": False, "error": "Agent returned non-JSON response", "raw": result[:500]})

    try:
        pipeline = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        return JSONResponse({"ok": False, "error": f"Invalid JSON: {e}", "raw": result[:500]})

    # Validate structure
    if "stages" not in pipeline:
        return JSONResponse({"ok": False, "error": "Missing 'stages' in response", "raw": result[:500]})

    # Ensure services dict exists
    if "services" not in pipeline:
        pipeline["services"] = {}

    return JSONResponse({
        "ok": True,
        "pipeline": pipeline,
        "reasoning": pipeline.get("reasoning", ""),
    })


def _default_presets():
    return [
        {
            "id": "simple-grill",
            "name": "Simple Grill",
            "description": "opencode → z.ai grill → opencode answers → opencode analyze",
            "icon": "🔥",
            "stages": 4,
            "pipeline": {
                "name": "simple-grill",
                "description": "Simple grill: plan + z.ai review + analysis",
                "stages": [
                    {"id": "plan", "type": "opencode", "role": "plan", "prompt": "Напиши детальный план реализации задачи.", "inputs": ["task"], "outputs": ["plan.md"], "artifact_dir": "spec"},
                    {"id": "zai_grill", "type": "reviewer", "service": "zai", "role": "grill", "prompt": "Проведи полный допрос плана. Найди слабые места.", "inputs": ["plan.md"], "outputs": ["review_zai.md"], "artifact_dir": "reviews"},
                    {"id": "answer", "type": "opencode", "role": "analyze", "prompt": "Проанализируй замечания ревьюера. Прими или отклони каждое замечание.", "inputs": ["plan.md", "review_zai.md"], "outputs": ["analysis.md"], "artifact_dir": "analysis"},
                    {"id": "finalize", "type": "opencode", "role": "plan", "prompt": "На основе анализа допиши финальный план.", "inputs": ["plan.md", "analysis.md"], "outputs": ["plan_final.md"], "artifact_dir": "analysis"}
                ],
                "services": {"zai": {"url": "https://z.ai"}}
            }
        },
        {
            "id": "dual-planner",
            "name": "Dual Planner + Judge",
            "description": "plan → z.ai grill → opencode answers → DS+Qwen on the answers (not the original plan) → judge → opencode analyze",
            "icon": "⚖",
            "stages": 7,
            "pipeline": {
                "name": "dual-planner",
                "description": "Grill the plan, answer it, then two planners see only the answers — not the original plan. Judge merges. Analyze does not get the start plan.",
                "stages": [
                    {"id": "plan", "type": "opencode", "role": "plan", "prompt": "Напиши детальный план.", "inputs": ["task"], "outputs": ["plan.md"], "artifact_dir": "spec"},
                    {"id": "zai_grill", "type": "reviewer", "service": "zai", "role": "grill", "prompt": "Проведи допрос плана. Задай жёсткие вопросы, не предлагай готовый новый план.", "inputs": ["plan.md"], "outputs": ["review_zai.md"], "artifact_dir": "reviews"},
                    {"id": "answer", "type": "opencode", "role": "analyze", "prompt": "Ответь на каждый вопрос гриля. Не тащи исходный план дальше — в выходе только ответы.", "inputs": ["plan.md", "review_zai.md"], "outputs": ["answers.md"], "artifact_dir": "analysis"},
                    {"id": "ds_plan", "type": "reviewer", "service": "deepseek", "role": "plan", "prompt": "По ответам на гриль предложи план. Исходного плана у тебя нет — опирайся только на answers.md.", "inputs": ["answers.md"], "outputs": ["plan_ds.md"], "artifact_dir": "reviews", "parallel_group": "planners"},
                    {"id": "qwen_plan", "type": "reviewer", "service": "qwen", "role": "plan", "prompt": "По ответам на гриль предложи план. Исходного плана у тебя нет — опирайся только на answers.md.", "inputs": ["answers.md"], "outputs": ["plan_qwen.md"], "artifact_dir": "reviews", "parallel_group": "planners"},
                    {"id": "judge", "type": "reviewer", "service": "deepseek", "role": "meta-review", "prompt": "Судья: рекомбинируй планы DS и Qwen в один. Не подмешивай исходный план автора.", "inputs": ["plan_ds.md", "plan_qwen.md"], "outputs": ["plan_recombined.md"], "artifact_dir": "analysis", "in_slots": 2},
                    {"id": "analyze", "type": "opencode", "role": "analyze", "prompt": "Собери финальный план из решения судьи. Исходного стартового плана у тебя нет.", "inputs": ["plan_recombined.md"], "outputs": ["plan_final.md"], "artifact_dir": "analysis"}
                ],
                "services": {"zai": {"url": "https://z.ai"}, "deepseek": {"url": "https://chat.deepseek.com"}, "qwen": {"url": "https://chat.qwen.ai"}}
            }
        },
        {
            "id": "full-pipeline",
            "name": "Full Pipeline",
            "description": "spec → plan → grill → meta-review → parallel analyze → finalize",
            "icon": "🔄",
            "stages": 6,
            "pipeline": {
                "name": "full-pipeline",
                "description": "Full pipeline with spec + grill + meta-review",
                "stages": [
                    {"id": "spec", "type": "opencode", "role": "spec", "prompt": "Создай спецификацию (ТЗ) для задачи.", "inputs": ["task"], "outputs": ["spec.md"], "artifact_dir": "spec"},
                    {"id": "plan", "type": "opencode", "role": "plan", "prompt": "На основе спецификации напиши план.", "inputs": ["spec.md"], "outputs": ["plan.md"], "artifact_dir": "spec"},
                    {"id": "zai_grill", "type": "reviewer", "service": "zai", "role": "grill", "prompt": "Проведи допрос плана.", "inputs": ["plan.md"], "outputs": ["review_zai.md"], "artifact_dir": "reviews", "parallel_group": "r1"},
                    {"id": "ds_meta", "type": "reviewer", "service": "deepseek", "role": "meta-review", "prompt": "Оцени качество ревью z.ai.", "inputs": ["plan.md", "review_zai.md"], "outputs": ["review_ds.md"], "artifact_dir": "reviews", "parallel_group": "r1"},
                    {"id": "analyze", "type": "opencode", "role": "analyze", "prompt": "Проанализируй все замечания.", "inputs": ["plan.md", "review_zai.md", "review_ds.md"], "outputs": ["analysis.md"], "artifact_dir": "analysis"},
                    {"id": "finalize", "type": "opencode", "role": "plan", "prompt": "Собери финальный план с учётом анализа.", "inputs": ["plan.md", "analysis.md"], "outputs": ["plan_final.md"], "artifact_dir": "analysis"}
                ],
                "services": {"zai": {"url": "https://z.ai"}, "deepseek": {"url": "https://chat.deepseek.com"}}
            }
        },
        {
            "id": "quick-review",
            "name": "Quick Review",
            "description": "plan → z.ai review → os analyze (2 stages)",
            "icon": "⚡",
            "stages": 3,
            "pipeline": {
                "name": "quick-review",
                "description": "Quick single review cycle",
                "stages": [
                    {"id": "plan", "type": "opencode", "role": "plan", "prompt": "Напиши план реализации.", "inputs": ["task"], "outputs": ["plan.md"], "artifact_dir": "spec"},
                    {"id": "review", "type": "reviewer", "service": "zai", "role": "grill", "prompt": "Быстрый допрос плана.", "inputs": ["plan.md"], "outputs": ["review.md"], "artifact_dir": "reviews"},
                    {"id": "analyze", "type": "opencode", "role": "analyze", "prompt": "Проанализируй замечания.", "inputs": ["plan.md", "review.md"], "outputs": ["analysis.md"], "artifact_dir": "analysis"}
                ],
                "services": {"zai": {"url": "https://z.ai"}}
            }
        },
        {
            "id": "tdd-pipeline",
            "name": "TDD Pipeline",
            "description": "spec → plan → tests → implementation → review → finalize",
            "icon": "🧪",
            "stages": 6,
            "pipeline": {
                "name": "tdd-pipeline",
                "description": "TDD-focused pipeline with test-first approach",
                "stages": [
                    {"id": "spec", "type": "opencode", "role": "spec", "prompt": "Создай спецификацию с фокусом на тесты.", "inputs": ["task"], "outputs": ["spec.md"], "artifact_dir": "spec"},
                    {"id": "plan", "type": "opencode", "role": "plan", "prompt": "Напиши план: сначала тесты, потом реализация.", "inputs": ["spec.md"], "outputs": ["plan.md"], "artifact_dir": "spec"},
                    {"id": "tests", "type": "opencode", "role": "plan", "prompt": "Напиши unit-тесты по плану (TDD).", "inputs": ["plan.md"], "outputs": ["tests.md"], "artifact_dir": "spec"},
                    {"id": "review", "type": "reviewer", "service": "zai", "role": "grill", "prompt": "Проверь план и тесты на полноту.", "inputs": ["plan.md", "tests.md"], "outputs": ["review.md"], "artifact_dir": "reviews"},
                    {"id": "implement", "type": "opencode", "role": "analyze", "prompt": "Реализуй код по плану,使其通过 тесты.", "inputs": ["plan.md", "tests.md", "review.md"], "outputs": ["implementation.md"], "artifact_dir": "analysis"},
                    {"id": "finalize", "type": "opencode", "role": "plan", "prompt": "Финальный план с кодом и тестами.", "inputs": ["plan.md", "tests.md", "implementation.md"], "outputs": ["plan_final.md"], "artifact_dir": "analysis"}
                ],
                "services": {"zai": {"url": "https://z.ai"}}
            }
        },
        {
            "id": "blank",
            "name": "Blank Pipeline",
            "description": "Empty pipeline — add your own stages",
            "icon": "📄",
            "stages": 0,
            "pipeline": {
                "name": "blank",
                "description": "Custom pipeline",
                "stages": [],
                "services": {}
            }
        }
    ]


# ── API: Session Pool (§7 плана UI_AND_SESSIONS) ──────────────────────

@app.get("/api/session_pool")
async def api_session_pool():
    """Статус провайдеров: какие залогинены в основных браузерах."""
    try:
        return JSONResponse(session_pool.probe_providers())
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/session_pool/open-login/{provider_id}")
async def api_session_pool_open_login(provider_id: str):
    """«Открыть вход»: спавн Chrome (изолированный профиль) + login_url провайдера."""
    result = session_pool.open_login(provider_id)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@app.post("/api/session_pool/set-source")
async def api_session_pool_set_source(request: Request):
    """Зафиксировать/сбросить источник сессий в config (null → авто)."""
    data = await request.json()
    browser = data.get("browser")
    profile = data.get("profile")
    return JSONResponse(session_pool.set_session_source(browser, profile))


@app.get("/api/setup/status")
async def api_setup_status():
    """First-run checklist: profile, CDP, provider logins."""
    config = _read_json(BASE / "config.json")
    port = config.get("cdp_port", 9222)
    profile = config.get("chrome_profile_path") or ""
    providers = []
    try:
        probe = session_pool.probe_providers()
        for p in probe.get("providers", []):
            providers.append({
                "id": p.get("id"),
                "name": p.get("name") or p.get("id"),
                "ok": bool(p.get("logged_in")),
            })
    except Exception:
        providers = [
            {"id": "glm", "name": "z.ai", "ok": False},
            {"id": "deepseek", "name": "DeepSeek", "ok": False},
            {"id": "qwen", "name": "Qwen", "ok": False},
            {"id": "google", "name": "Google AI", "ok": False},
        ]
    active = _projects_active()
    return JSONResponse({
        "profile_exists": bool(profile and Path(profile).exists()),
        "profile_has_session": bool(profile and session_pool.profile_has_saved_session(profile)),
        "cdp": _check_port(port),
        "cdp_port": port,
        "providers": providers,
        "session_name": (_get_current_session()[0] or "") or None,
        "target_mode": (active or {}).get("mode") or config.get("target_mode") or "scratch",
        "target_project": (active or {}).get("path") or config.get("target_project"),
        "project_id": (active or {}).get("id"),
        "project_name": (active or {}).get("name") or "Scratch",
        "projects": _list_workspace_projects(),
    })


@app.post("/api/setup/ensure-chrome")
async def api_setup_ensure_chrome():
    """Open orchestrator Chrome (restore pins if the profile already has logins)."""
    try:
        result = session_pool.launch_cdp_chrome()
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/setup/target")
async def api_setup_target(request: Request):
    """scratch = Qmeka sandbox. project = agents edit this folder."""
    data = await request.json()
    mode = data.get("mode") or "scratch"
    path = data.get("path") or None
    if mode not in ("scratch", "project"):
        return JSONResponse({"ok": False, "error": "mode must be scratch or project"}, status_code=400)
    if mode == "project":
        if not path or not Path(path).is_dir():
            return JSONResponse({"ok": False, "error": "folder not found"}, status_code=400)
    else:
        path = None
    proj = _upsert_project_from_target(mode, path)
    return JSONResponse({
        "ok": True,
        "target_mode": mode,
        "target_project": path,
        "project": proj,
    })


@app.post("/api/session_pool/seed")
async def api_session_pool_seed(request: Request):
    """Перенести сессии из основного профиля в профиль оркестратора (seed).

    Тело (опционально): {"source_profile": "Default" | "Profile 1", ...}
    Cookies заблокированы, если основной Chrome запущен → в ответе
    cookies_requires_close=true (закрыть Chrome и повторить).
    """
    try:
        data = await request.json() if request.headers.get("content-length", 0) not in (None, "", "0") else {}
    except Exception:
        data = {}
    result = session_pool.seed_orchestrator_profile(
        source_profile=data.get("source_profile", "Default"))
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


# ── API: Qmeka projects (desks) ──────────────────────────────────────

@app.post("/api/browse-folder")
async def api_browse_folder():
    """Native Windows folder dialog on this machine. Returns the full path."""
    script = Path(__file__).parent / "_browse_folder.ps1"
    if not script.exists():
        return JSONResponse({"ok": False, "error": "browse script missing"}, status_code=500)

    import subprocess

    def _run():
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-STA",
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                ],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(BASE),
            )
            return (proc.stdout or "").strip()
        except Exception as e:
            return f"__err__{e}"

    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, _run)
    if path.startswith("__err__"):
        return JSONResponse({"ok": False, "error": path[7:]}, status_code=500)
    if not path:
        return JSONResponse({"ok": False, "cancelled": True})
    return JSONResponse({"ok": True, "path": path})


@app.get("/api/projects")
async def api_projects_list():
    store = _projects_load()
    return JSONResponse({
        "active_id": store.get("active_id"),
        "projects": store.get("projects") or [],
        "folders": _list_workspace_projects(),
    })


@app.post("/api/projects")
async def api_projects_create(request: Request):
    data = await request.json()
    name = (data.get("name") or "").strip()
    mode = data.get("mode") or "scratch"
    path = data.get("path") or None
    if mode not in ("scratch", "project"):
        return JSONResponse({"ok": False, "error": "mode must be scratch or project"}, status_code=400)
    if not name:
        name = Path(str(path)).name if (mode == "project" and path) else "Scratch"
    if mode == "project":
        if not path or not Path(path).is_dir():
            return JSONResponse({"ok": False, "error": "folder not found"}, status_code=400)
    else:
        path = None
    store = _projects_load()
    if mode == "project":
        existing = next((p for p in store["projects"] if p.get("path") == path), None)
        if existing:
            store["active_id"] = existing["id"]
            _projects_save(store)
            _sync_active_project(existing)
            return JSONResponse({"ok": True, "project": existing, "activated": True})
    now = datetime.now().isoformat()
    proj = {
        "id": "scratch" if mode == "scratch" and not _project_by_id(store, "scratch") else _slug_id(name),
        "name": name,
        "mode": mode,
        "path": path,
        "created": now,
        "updated": now,
    }
    store["projects"].append(proj)
    store["active_id"] = proj["id"]
    _projects_save(store)
    _sync_active_project(proj)
    return JSONResponse({"ok": True, "project": proj})


@app.post("/api/projects/{project_id}/activate")
async def api_projects_activate(project_id: str):
    store = _projects_load()
    proj = _project_by_id(store, project_id)
    if not proj:
        return JSONResponse({"ok": False, "error": "project not found"}, status_code=404)
    store["active_id"] = project_id
    proj["updated"] = datetime.now().isoformat()
    _projects_save(store)
    _sync_active_project(proj)
    return JSONResponse({"ok": True, "project": proj})


@app.patch("/api/projects/{project_id}")
async def api_projects_patch(project_id: str, request: Request):
    data = await request.json()
    store = _projects_load()
    proj = _project_by_id(store, project_id)
    if not proj:
        return JSONResponse({"ok": False, "error": "project not found"}, status_code=404)
    if "name" in data and str(data.get("name") or "").strip():
        proj["name"] = str(data["name"]).strip()
    if "mode" in data:
        mode = data.get("mode") or "scratch"
        if mode not in ("scratch", "project"):
            return JSONResponse({"ok": False, "error": "mode must be scratch or project"}, status_code=400)
        proj["mode"] = mode
        if mode == "scratch":
            proj["path"] = None
    if "path" in data and proj.get("mode") == "project":
        path = data.get("path")
        if path and not Path(path).is_dir():
            return JSONResponse({"ok": False, "error": "folder not found"}, status_code=400)
        proj["path"] = path
    proj["updated"] = datetime.now().isoformat()
    _projects_save(store)
    if store.get("active_id") == project_id:
        _sync_active_project(proj)
    return JSONResponse({"ok": True, "project": proj})


@app.delete("/api/projects/{project_id}")
async def api_projects_delete(project_id: str):
    store = _projects_load()
    projects = store.get("projects") or []
    if len(projects) <= 1:
        return JSONResponse({"ok": False, "error": "keep at least one project"}, status_code=400)
    proj = _project_by_id(store, project_id)
    if not proj:
        return JSONResponse({"ok": False, "error": "project not found"}, status_code=404)
    store["projects"] = [p for p in projects if p.get("id") != project_id]
    if store.get("active_id") == project_id:
        store["active_id"] = store["projects"][0]["id"]
        _sync_active_project(store["projects"][0])
    _projects_save(store)
    pdir = PROJECTS_DIR / project_id
    if pdir.exists():
        import shutil
        shutil.rmtree(pdir, ignore_errors=True)
    return JSONResponse({"ok": True, "active_id": store.get("active_id")})


# ── Helpers ──────────────────────────────────────────────────────────

def _list_workspace_projects():
    """Sibling repos under C:\\Workspace\\projects for the first-run picker."""
    root = Path(r"C:\Workspace\projects")
    out = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        out.append({
            "name": child.name,
            "path": str(child),
            "has_git": (child / ".git").exists(),
        })
    return out


def _check_port(port):
    """Check if a port is listening."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False


def _render_checks_compact(items):
    """Header dots: one span per check, tooltip has the full status."""
    bits = []
    for item in items:
        label, ok, detail = item[0], item[1], item[2]
        cls = "ok" if ok else "fail"
        title = html_lib.escape(f"{label}: {detail}", quote=True)
        bits.append(
            f'<span class="ready-dot ready-dot--{cls}" data-tip="{title}"></span>'
        )
    return "".join(bits)


def _render_checks(items):
    """Render readiness check items as HTML. Items: (label, ok, detail, login_url|None)."""
    html = ""
    for item in items:
        label, ok, detail = item[0], item[1], item[2]
        login_url = item[3] if len(item) > 3 else None
        cls = "ok" if ok else "fail"
        icon = "&#10003;" if ok else "&#10007;"
        action = ""
        if not ok and login_url:
            action = (
                f' <a class="check__action" href="{login_url}" target="_blank" '
                f'rel="noopener">открыть вход &#8599;</a>'
            )
        html += (
            f'<div class="check"><span class="check__dot check__dot--{cls}"></span>'
            f'<span class="check__text">{label}: {detail}</span>{action}</div>\n'
        )
    return html


def _check_pyautogui():
    """Check if pyautogui is installed."""
    try:
        import pyautogui
        return True
    except ImportError:
        return False


def _parse_backlog(text, session_name):
    """Parse backlog.md into structured items."""
    items = []
    lines = text.strip().split("\n")
    for i, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Simple parsing: - [ ] text or - [x] text
        status = "new"
        if line.startswith("- [x]") or line.startswith("- [X]"):
            status = "resolved"
            text_content = line[5:].strip()
        elif line.startswith("- [ ]"):
            status = "new"
            text_content = line[5:].strip()
        elif line.startswith("- "):
            text_content = line[2:].strip()
        else:
            text_content = line

        items.append({
            "id": f"{session_name}_{i}",
            "text": text_content,
            "status": status,
            "source": session_name,
            "iter": "?",
            "added": datetime.now().strftime("%Y-%m-%d"),
        })
    return items


# ── Parsing helpers ──────────────────────────────────────────────────

def _parse_log_line(line):
    """Parse a log line like [2026-09-02 01:02:51] [INFO] [session] message."""
    import re
    m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+\[(\w+)\]\s+(?:\[([^\]]+)\]\s+)?(.*)', line)
    if m:
        return {
            "ts": m.group(1),
            "level": m.group(2),
            "session": m.group(3) or "",
            "msg": m.group(4),
        }
    return {"ts": "", "level": "INFO", "session": "", "msg": line}


def _build_timeline(journal, status):
    """Build timeline nodes from journal events."""
    nodes = []
    events_seen = set()
    for ev in journal:
        event = ev.get("event", "")
        if event in ("session_start", "iter_start", "iter_done", "session_done", "session_failed"):
            if event not in events_seen:
                nodes.append({
                    "label": _timeline_label(event, ev),
                    "state": "done" if event != "iter_start" else "active",
                })
                events_seen.add(event)
    # Add remaining empty nodes
    current_status = status.get("status", "") if status else ""
    if current_status in ("planning", "reviewing"):
        nodes.append({"label": "...", "state": ""})
    return nodes


def _timeline_label(event, ev):
    """Human-readable timeline label."""
    labels = {
        "session_start": "Старт",
        "iter_start": f"Итер. #{ev.get('data', {}).get('i', '?')}",
        "iter_done": f"Готово #{ev.get('data', {}).get('i', '?')}",
        "session_done": "Завершено",
        "session_failed": "Ошибка",
    }
    return labels.get(event, event)


def _file_size(session_name, iter_num, filename):
    """Get file size in human-readable format."""
    if not session_name:
        return ""
    fpath = SESSIONS / session_name / f"{iter_num:02d}" / filename
    if fpath.exists():
        size = fpath.stat().st_size
        if size > 1024:
            return f"{size/1024:.1f} KB"
        return f"{size} B"
    return ""


def _extract_verdict(reply_text):
    """Extract verdict from reply.md text."""
    if not reply_text:
        return "pending"
    lower = reply_text.lower()
    if any(w in lower for w in ["одобрено", "approved", "готов", "gtm", "good to merge"]):
        return "approved"
    if any(w in lower for w in ["нужны правки", "needs changes", "revision", "исправить"]):
        return "changes"
    if any(w in lower for w in ["отклонить", "rejected", "отклонен", "reject"]):
        return "rejected"
    return "changes"  # default to "needs changes" as most common


# ── SSE: Live streaming ─────────────────────────────────────────────

_sse_clients: list[asyncio.Queue] = []


def _sse_notify(event: dict):
    """Push event to all connected SSE clients."""
    for q in list(_sse_clients):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


# ── Background: watch journal.jsonl → push iter_done to SSE ─────
_journal_watcher_started = False
_journal_pos = 0

async def _journal_watcher():
    """Poll journal.jsonl for new iter_done events and push to SSE."""
    global _journal_pos, _journal_watcher_started
    if _journal_watcher_started:
        return
    _journal_watcher_started = True
    while True:
        try:
            if JOURNAL.exists():
                size = JOURNAL.stat().st_size
                if size > _journal_pos:
                    with open(JOURNAL, "r", encoding="utf-8") as f:
                        f.seek(_journal_pos)
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                ev = json.loads(line)
                                event_type = ev.get("event", "")
                                if event_type in ("iter_done", "iter_start", "session_start", "session_done", "session_failed"):
                                    _sse_notify({
                                        "type": event_type,
                                        "i": ev.get("data", {}).get("i", 0),
                                        "iter": ev.get("data", {}).get("i", 0),
                                        "status": event_type,
                                        "ts": ev.get("ts", ""),
                                    })
                            except json.JSONDecodeError:
                                pass
                        _journal_pos = f.tell()
                else:
                    _journal_pos = size
        except Exception:
            pass
        await asyncio.sleep(2)

# Start watcher on first request
@app.on_event("startup")
async def _start_journal_watcher():
    asyncio.create_task(_journal_watcher())


@app.get("/api/stream")
async def api_stream(request: Request):
    """SSE endpoint — live log + journal events."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_clients.append(queue)

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    event_type = event.pop("type", "message")
                    data = json.dumps(event, ensure_ascii=False)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        finally:
            _sse_clients.remove(queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── API: User comments (инъекция между итерациями) ───────────────────

@app.get("/api/comments")
async def api_comments_get(session: str = ""):
    """Get user comments for a session."""
    if not session:
        session_name, _ = _get_current_session()
        session = session_name or ""
    if not session:
        return JSONResponse([])
    comments_path = SESSIONS / session / "user_comments.json"
    if comments_path.exists():
        return JSONResponse(json.loads(comments_path.read_text(encoding="utf-8")))
    return JSONResponse([])


@app.post("/api/comments")
async def api_comments_add(request: Request):
    """Add a user comment for injection into next iteration."""
    data = await request.json()
    text = data.get("text", "").strip()
    session = data.get("session", "")
    target_iter = data.get("target_iter", 0)  # 0 = next iteration

    if not text:
        return JSONResponse({"ok": False, "error": "Empty comment"})

    if not session:
        session_name, _ = _get_current_session()
        session = session_name or ""

    if not session:
        return JSONResponse({"ok": False, "error": "No active session"})

    comments_path = SESSIONS / session / "user_comments.json"
    comments = []
    if comments_path.exists():
        try:
            comments = json.loads(comments_path.read_text(encoding="utf-8"))
        except Exception:
            comments = []

    comments.append({
        "id": len(comments) + 1,
        "text": text,
        "target_iter": 0 if target_iter == "next" else int(target_iter),
        "created": datetime.now().isoformat(),
        "injected": False,
    })

    comments_path.write_text(
        json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _sse_notify({"type": "comment_added", "comment": comments[-1]})

    return JSONResponse({"ok": True, "comment": comments[-1]})


@app.delete("/api/comments/{comment_id}")
async def api_comments_delete(comment_id: int, session: str = ""):
    """Delete a user comment."""
    if not session:
        session_name, _ = _get_current_session()
        session = session_name or ""
    if not session:
        return JSONResponse({"ok": False, "error": "No active session"})

    comments_path = SESSIONS / session / "user_comments.json"
    if not comments_path.exists():
        return JSONResponse({"ok": False, "error": "No comments file"})

    comments = json.loads(comments_path.read_text(encoding="utf-8"))
    comments = [c for c in comments if c.get("id") != comment_id]
    comments_path.write_text(
        json.dumps(comments, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return JSONResponse({"ok": True})


@app.get("/api/comments/pending")
async def api_comments_pending(session: str = ""):
    """Get un-injected comments for the current session."""
    if not session:
        session_name, _ = _get_current_session()
        session = session_name or ""
    if not session:
        return JSONResponse([])
    comments_path = SESSIONS / session / "user_comments.json"
    if not comments_path.exists():
        return JSONResponse([])
    comments = json.loads(comments_path.read_text(encoding="utf-8"))
    pending = [c for c in comments if not c.get("injected")]
    return JSONResponse(pending)


# ── Helpers: SSE notify from external code ──────────────────────────

def notify_log(level: str, msg: str, session: str = ""):
    """Push a log event to SSE clients."""
    _sse_notify({
        "type": "log",
        "level": level,
        "msg": msg,
        "session": session,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


def notify_stage(stage: str, status: str, detail: str = ""):
    """Push a stage event to SSE clients."""
    _sse_notify({
        "type": "stage",
        "stage": stage,
        "status": status,
        "detail": detail,
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8080)
