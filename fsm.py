"""
fsm.py — оркестратор цикла z.ai <-> opencode (Фаза 6).
Контракт (по prompts.json):
  - opencode пишет план в {{SESSION}}/0<N>/plan.md и маркер done.json
  - z.ai ревью сохраняется в {{SESSION}}/0<N>/reply.md
  - opencode_cont читает reply.md предыдущей итерации и дорабатывает план

Предусловия (оркестратор не запускает сами агенты):
  - opencode уже открыт (окно на экране; opencode_input_xy откалибровано)
  - z.ai открыт в изолированном Chrome на CDP-порту (cdp_port)

Сквозной прогон:  python fsm.py            (реальный)
                 python fsm.py --dry-run   (только логика, без инъекций/отправки)
"""
import json
import os
import sys
import time
import shutil
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path

import opencode_driver as oc
import zai_driver as zai
from cdp_ws import browser_ws_url, find_target_ws
from _metrics import M
from _log import log as _raw_log, set_session as _set_sid, set_file as _set_logfile
from md2docx import ensure_docx

# session manager for browser login persistence
try:
    from session_manager import SessionManager
    _sm = SessionManager(port=9222)
except ImportError:
    _sm = None

# login gate for automatic auth flow
try:
    from login_gate import LoginGate
    _login_gate = LoginGate(port=9222, timeout=300)
except ImportError:
    _login_gate = None

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
PROMPTS = json.loads((HERE / "prompts.json").read_text(encoding="utf-8"))

RUN_LOG = HERE / "run.log"
JOURNAL = HERE / "journal.jsonl"
_raw_wd = CONFIG.get("workdir")
WORKDIR = Path(_raw_wd) if _raw_wd else (HERE / "sessions")
if not WORKDIR.is_absolute():
    WORKDIR = (HERE / WORKDIR).resolve()
CDP_PORT = CONFIG.get("cdp_port", 9222)
N_DEFAULT = CONFIG.get("n_default")
OPENCODE_MODEL = CONFIG.get("opencode_model")
OPENCODE_TRANSPORT = CONFIG.get("opencode_transport", "run")
OPENCODE_SESSION_PER_RUN = CONFIG.get("opencode_session_per_run", True)
# только терминальный режим: cli (subprocess opencode run)
CONTROL = CONFIG.get("opencode_control", "cli")


def _slugify_task(task, max_len=30):
    """Generate human-readable slug from task name.
    Examples:
        'Спроектировать модуль аутентификации' -> 'auth-module'
        'Design a minimal Python HTTP server' -> 'minimal-http-server'
        'greeting module z.ai opencode review' -> 'greeting-review'
    """
    import re
    ru_map = {
        'спроектировать': 'design', 'модуль': 'module', 'аутентификации': 'auth',
        'аутентификация': 'auth', 'авторизации': 'auth', 'авторизация': 'auth',
        'база': 'db', 'данных': 'data', 'пользователей': 'users',
        'пользователь': 'user', 'сервер': 'server', 'сервис': 'service',
        'клиент': 'client', 'интерфейс': 'interface', 'приложение': 'app',
        'система': 'system', 'метод': 'method', 'класс': 'class',
        'функция': 'function', 'тест': 'test', 'тесты': 'tests',
        'рефакторинг': 'refactor', 'минимальный': 'minimal', 'простой': 'simple',
        'страница': 'page', 'страницы': 'pages',
        'управление': 'manage', 'настройка': 'config', 'конфигурация': 'config',
        'логирование': 'logging', 'мониторинг': 'monitoring', 'безопасности': 'security',
    }
    words = task.lower().split()
    slug_words = []
    for w in words:
        clean = re.sub(r'[^a-zа-я0-9]', '', w)
        if clean in ru_map:
            slug_words.append(ru_map[clean])
        elif clean and len(clean) > 2:
            if all(c in 'abcdefghijklmnopqrstuvwxyz' for c in clean):
                slug_words.append(clean)
        if len(slug_words) >= 3:
            break
    if not slug_words:
        slug_words = ['task']
    slug = '-'.join(slug_words)[:max_len].rstrip('-')
    return slug


def _unique_slug(base_slug, workdir):
    """Ensure slug is unique by appending -2, -3, etc. if needed."""
    candidate = base_slug
    n = 2
    while (workdir / candidate).exists():
        candidate = f"{base_slug}-{n}"
        n += 1
    return candidate


# init unified logger
from _log import log as _raw_log, set_session as _set_sid, set_file as _set_logfile
_set_logfile(RUN_LOG)


def log(msg, level="INFO"):
    """Unified logger with correlation-id."""
    _raw_log(msg, level=level)


def journal(event, data=None):
    """Write a structured event to journal.jsonl (unified schema)."""
    rec = {
        "ts": datetime.now().isoformat(),
        "event": event,
        "data": data or {},
    }
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Session status tracking ────────────────────────────────────────────
VALID_STATUSES = ("created", "planning", "plan_ready", "reviewing",
                  "review_ready", "done", "failed", "abandoned")

def write_status(session, status, detail=None):
    """Write status.json into session root. Called at every phase transition."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    rec = {
        "status": status,
        "updated": datetime.now().isoformat(),
    }
    if detail:
        rec["detail"] = detail
    path = session / "status.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[status] {session.name} -> {status}")
    # also update central registry
    _registry_update(session.name, status, detail)


# ── Central session registry ───────────────────────────────────────────
REGISTRY = WORKDIR / "registry.json"

def _registry_load():
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"sessions": {}}

def _registry_save(data):
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def _registry_update(name, status, detail=None):
    data = _registry_load()
    entry = data["sessions"].get(name, {})
    entry["status"] = status
    entry["updated"] = datetime.now().isoformat()
    if detail:
        entry["detail"] = detail
    data["sessions"][name] = entry
    _registry_save(data)

def registry_add(name, task, N, control):
    """Register a new session in the central registry."""
    data = _registry_load()
    data["sessions"][name] = {
        "task": task,
        "N": N,
        "control": control,
        "status": "created",
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
    }
    _registry_save(data)

def registry_list(status_filter=None):
    """Return list of (name, entry) pairs, optionally filtered by status."""
    data = _registry_load()
    items = sorted(data["sessions"].items(), key=lambda x: x[1].get("updated", ""), reverse=True)
    if status_filter:
        items = [(n, e) for n, e in items if e.get("status") == status_filter]
    return items

def registry_cleanup(dry=False):
    """Remove sessions from registry that no longer exist on disk."""
    data = _registry_load()
    removed = []
    for name in list(data["sessions"].keys()):
        session_dir = WORKDIR / name
        if not session_dir.exists():
            removed.append(name)
            if not dry:
                del data["sessions"][name]
    if removed:
        _registry_save(data)
    return removed


def read_status(session):
    """Read status from session, or None if missing."""
    path = session / "status.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def cleanup_empty_sessions(dry=False):
    """Remove sessions that are empty, have no plan, or are stuck in 'created'."""
    removed = []
    if not WORKDIR.exists():
        return removed
    for d in sorted(WORKDIR.iterdir()):
        if not d.is_dir():
            continue
        st = read_status(d)
        # plan.md can be in root or in 01/ subdirectory
        has_plan = (
            ((d / "plan.md").exists() and (d / "plan.md").stat().st_size > 0)
            or any(
                (sub / "plan.md").exists() and (sub / "plan.md").stat().st_size > 0
                for sub in d.iterdir() if sub.is_dir()
            )
        )
        has_done = (d / "done.json").exists() or any(
            (sub / "done.json").exists() for sub in d.iterdir() if sub.is_dir()
        )
        status = st["status"] if st else None

        should_remove = False
        reason = ""

        if status == "failed":
            should_remove = True
            reason = "status=failed"
        elif status == "abandoned":
            should_remove = True
            reason = "status=abandoned"
        elif status == "created" and not has_plan:
            should_remove = True
            reason = "stuck at created, no plan"
        elif not has_plan and not has_done:
            # no status file and no artifacts — test run
            # check recursively for any file > 100 bytes
            has_any_content = False
            for f in d.rglob("*"):
                if f.is_file() and f.stat().st_size > 100:
                    has_any_content = True
                    break
            if not has_any_content:
                should_remove = True
                reason = "empty (no plan, no done, no content)"

        if should_remove:
            removed.append((d.name, reason))
            if not dry:
                import shutil
                shutil.rmtree(d)
                log(f"[cleanup] removed {d.name}: {reason}")
            else:
                log(f"[cleanup] would remove {d.name}: {reason}")
    return removed


STUCK_STATUSES = ("planning", "plan_ready", "reviewing", "review_ready")
STUCK_TIMEOUT_MIN = 30  # minutes before a stuck session becomes failed


def reclaim_stuck_sessions():
    """Mark sessions stuck in intermediate states as abandoned or failed.

    Called at startup / before each new run. Checks status.json timestamps:
    - if updated > STUCK_TIMEOUT_MIN ago → failed (stale, likely dead)
    - otherwise → abandoned (soft interrupt from previous run)
    """
    if not WORKDIR.exists():
        return []
    reclaimed = []
    now = time.time()
    for d in sorted(WORKDIR.iterdir()):
        if not d.is_dir():
            continue
        st = read_status(d)
        if not st:
            continue
        status = st.get("status")
        if status not in STUCK_STATUSES:
            continue
        updated = st.get("updated", "")
        stale = False
        if updated:
            try:
                ts = datetime.fromisoformat(updated)
                age_min = (now - ts.timestamp()) / 60
                stale = age_min > STUCK_TIMEOUT_MIN
            except Exception:
                stale = True  # unparseable → treat as stale
        if stale:
            new_status = "failed"
            detail = {"reason": "stale", "stuck_in": status,
                      "detail": "phase exceeded timeout, process likely dead"}
        else:
            new_status = "abandoned"
            detail = {"reason": "previous_run_interrupt", "stuck_in": status}
        write_status(d, new_status, detail)
        journal("session_reclaimed", {"session": d.name, "from": status,
                                       "to": new_status})
        reclaimed.append((d.name, status, new_status))
        log(f"[reclaim] {d.name}: {status} → {new_status}")
    return reclaimed


def watchdog_check(session, max_minutes=None):
    """Fail session if current phase has exceeded time limit.

    Call periodically during long-running phases (opencode, zai).
    Returns True if session was killed.
    """
    if max_minutes is None:
        max_minutes = STUCK_TIMEOUT_MIN
    st = read_status(session)
    if not st:
        return False
    status = st.get("status")
    if status not in STUCK_STATUSES:
        return False
    updated = st.get("updated", "")
    if not updated:
        return False
    try:
        ts = datetime.fromisoformat(updated)
        age_min = (time.time() - ts.timestamp()) / 60
    except Exception:
        return False
    if age_min > max_minutes:
        write_status(session, "failed", {
            "reason": "watchdog_timeout",
            "stuck_in": status,
            "elapsed_min": round(age_min, 1),
        })
        journal("watchdog_kill", {"session": session.name, "stuck_in": status,
                                   "elapsed_min": round(age_min, 1)})
        log(f"[watchdog] {session.name}: killed after {age_min:.0f}min in {status}")
        return True
    return False


def render(tmpl, **kw):
    out = tmpl
    for k, v in kw.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def ask_user_task():
    """Launch-оверлей: USER_TASK (многострочный) + N + выбор канала управления
    opencode (cli / serve) и опция монтирования web-вкладки."""
    state = {}

    def start():
        task = txt.get("1.0", "end").strip()
        nraw = ent_n.get().strip()
        if not task:
            lbl_err.config(text="USER_TASK обязателен")
            return
        try:
            n = int(nraw)
        except ValueError:
            lbl_err.config(text="N должно быть целым числом")
            return
        if n < 1:
            lbl_err.config(text="N >= 1")
            return
        state["task"] = task
        state["n"] = n
        state["control"] = ctl_var.get()
        root.destroy()

    root = tk.Tk()
    root.title("Orchestrator launch")
    root.geometry("620x400")
    tk.Label(root, text="USER_TASK (что спроектировать):").pack(anchor="w", padx=10, pady=(10, 0))
    txt = tk.Text(root, height=12, wrap="word")
    txt.pack(fill="both", padx=10, pady=5)
    tk.Label(root, text="N — сколько итераций ревью (без дефолта):").pack(anchor="w", padx=10)
    ent_n = tk.Entry(root)
    if N_DEFAULT:
        ent_n.insert(0, str(N_DEFAULT))
    ent_n.pack(fill="x", padx=10, pady=5)

    # выбор канала управления opencode
    ctl_var = tk.StringVar(value=CONTROL)
    frm = tk.Frame(root)
    frm.pack(anchor="w", padx=10, pady=(5, 0))
    tk.Label(frm, text="Канал opencode:").pack(side="left")
    tk.Radiobutton(frm, text="CLI (terminal)", variable=ctl_var,
                   value="cli").pack(side="left", padx=4)
    lbl_err = tk.Label(root, text="", fg="red")
    lbl_err.pack(anchor="w", padx=10)
    tk.Button(root, text="Start", command=start).pack(pady=10)
    root.mainloop()
    return state.get("task"), state.get("n"), state.get("control"), None


def iter_paths(session, i):
    d = session / f"0{i}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(dry_run=False, task=None, N=None, control=None, web_mount=None):
    # reclaim sessions stuck from previous interrupted runs
    reclaimed = reclaim_stuck_sessions()
    if reclaimed:
        log(f"[startup] reclaimed {len(reclaimed)} stuck session(s)")

    if not task or not N:
        task, N, control, web_mount = ask_user_task()
    if not task:
        log("Отмена: USER_TASK пуст")
        return
    # только терминальный режим
    ctrl = control or CONTROL
    base_slug = _slugify_task(task)
    slug = _unique_slug(base_slug, WORKDIR)
    session = WORKDIR / slug
    session.mkdir(parents=True, exist_ok=True)
    write_status(session, "created", {"task": task, "N": N, "control": ctrl})
    registry_add(session.name, task, N, ctrl)
    _set_sid(session.name)  # correlation-id для логов
    log(f"Сессия: {session} | N={N} | dry_run={dry_run} | control={ctrl}")
    journal("session_start", {"session": str(session), "N": N, "task": task, "control": ctrl})
    M.inc("sessions_total")
    M.gauge("current_session", session.name)

    # терминальный режим: opencode run --auto --format json
    try:
        prev_plan = ""
        # свежий чат z.ai на каждый прогон (снимает контаминацию историей)
        if not dry_run:
            try:
                cz = zai.connect(CDP_PORT)
                if zai.is_logged_out(cz):
                    log("[zai] logged out — starting login gate...")
                    cz.close()
                    # Use login gate for automatic login flow
                    if _login_gate:
                        logged_in = _login_gate.ensure_logged_in("zai", log_fn=log)
                        if not logged_in:
                            log("!! z.ai login failed/timeout")
                            write_status(session, "failed", {"reason": "zai_logout"})
                            journal("session_failed", {"session": session.name,
                                                        "reason": "zai_logout"})
                            return
                        log("[zai] login gate passed")
                    else:
                        log("!! login_gate not available")
                        write_status(session, "failed", {"reason": "zai_logout"})
                        journal("session_failed", {"session": session.name,
                                                    "reason": "zai_logout"})
                        return
                    # Reconnect after login
                    cz = zai.connect(CDP_PORT)
                zai.new_chat(cz)
                cz.close()
            except Exception as e:
                log(f"[warn] new_chat failed: {e}")
        run_start = time.time()
        for i in range(1, N + 1):
            d = iter_paths(session, i)
            log(f"--- Итерация {i}/{N} ---")
            journal("iter_start", {"i": i})
            # watchdog: check if previous phase exceeded timeout
            if watchdog_check(session):
                return

            # 1) opencode: спроектировать/доработать план
            write_status(session, "planning", {"iter": i})
            if i == 1:
                prompt = render(PROMPTS["PROMPT_OPENCODE_START"],
                                USER_TASK=task, N=N, SESSION=str(session))
            else:
                prompt = render(PROMPTS["PROMPT_OPENCODE_CONT"],
                                NEXT_N=i, N=i - 1, SESSION=str(session),
                                PREV_PLAN=prev_plan)
            if dry_run:
                log(f"[dry] opencode-промпт ({len(prompt)} байт) -> headless run, пишем plan.md в {d}")
                oc_start = time.time()
                plan = ""
                oc_dur = 0
            else:
                oc_start = time.time()
                raw = oc.opencode_run(prompt, str(d),
                                      timeout=CONFIG.get("timeouts", {}).get("T_open", 900),
                                      model=OPENCODE_MODEL,
                                      continue_session=(i > 1))
                # предпочитаем файл, который сам opencode записал; при serve
                # plan.md может лежать в корне сессии (--dir serve), копируем в d
                plan_path = d / "plan.md"
                if not (plan_path.exists() and plan_path.stat().st_size > 0):
                    alt = session / "plan.md"
                    if alt.exists() and alt.stat().st_size > 0:
                        shutil.copyfile(alt, plan_path)
                if plan_path.exists() and plan_path.stat().st_size > 0:
                    plan = plan_path.read_text(encoding="utf-8")
                else:
                    plan = raw
                    plan_path.write_text(plan, encoding="utf-8")
                (d / "done.json").write_text(
                    json.dumps({"role": "opencode", "iter": i}, ensure_ascii=False),
                    encoding="utf-8")
            plan = (d / "plan.md").read_text(encoding="utf-8") if (d / "plan.md").exists() else ""
            oc_dur = time.time() - oc_start
            log(f"[opencode] plan.md: {len(plan)} байт ({oc_dur:.1f}s)")
            M.inc("opencode_runs")
            M.hist("opencode_latency_ms", oc_dur * 1000)
            M.hist("opencode_plan_bytes", len(plan))
            write_status(session, "plan_ready", {"iter": i, "plan_bytes": len(plan)})
            # watchdog: check if z.ai or other phases exceeded timeout
            if watchdog_check(session):
                return
            # 2) z.ai: ревью плана
            write_status(session, "reviewing", {"iter": i})
            zprompt = render(PROMPTS["PROMPT_ZAI"],
                             NEXT_N=i, N=i, SESSION=str(session), PREV_PLAN=plan)
            if dry_run:
                log(f"[dry] z.ai-промпт ({len(zprompt)} байт) + attach {d / 'plan.docx'} -> отправить, ждать ответ")
                reply = "(dry-run reply)"
                zai_start = time.time()
                zai_dur = 0
            else:
                zai_start = time.time()
                c = zai.connect(CDP_PORT)
                if zai.is_logged_out(c):
                    log("[zai] logged out during iteration — attempting restore...")
                    c.close()
                    restored = False
                    if _sm:
                        try:
                            restored = _sm.restore("zai", verbose=False)
                        except Exception:
                            pass
                    if not restored:
                        log("!! z.ai — manual login required")
                        write_status(session, "failed", {"reason": "zai_logout", "iter": i})
                        journal("session_failed", {"session": session.name,
                                                    "reason": "zai_logout", "iter": i})
                        return
                    c = zai.connect(CDP_PORT)
                    if zai.is_logged_out(c):
                        log("!! z.ai — still logged out after restore")
                        write_status(session, "failed", {"reason": "zai_logout", "iter": i})
                        c.close()
                        return
                before = zai.inner_text(c)
                nid = zai.locate_input(c)
                x, y = zai.input_center(c, nid)
                zai.move_bezier(c, x, y)
                zai.click(c, x, y)
                zai.type_text(c, zprompt)
                try:
                    # Convert plan.md to docx for z.ai (z.ai only reads PDF/DOCX)
                    attach_path = ensure_docx(plan_path)
                    zai.attach_files(c, [str(attach_path)], do_set=True)
                except Exception as e:
                    log(f"[warn] attach не удался: {e} (продолжаем без вложения)")
                # Re-click input field to ensure focus before submit
                nid = zai.locate_input(c)
                x, y = zai.input_center(c, nid)
                zai.click(c, x, y)
                time.sleep(0.1)
                zai.submit(c)
                reply = zai.read_reply(c, before, zprompt,
                                      timeout=CONFIG.get("timeouts", {}).get("T_zai", 300))
                c.close()
                zai_dur = time.time() - zai_start
                if not reply:
                    log("!! z.ai не вернул ответ (эхо/пусто) — останов прогона.")
                    write_status(session, "failed", {"reason": "zai_empty_reply", "iter": i})
                    journal("session_failed", {"session": session.name,
                                                "reason": "zai_empty_reply", "iter": i})
                    return
            reply_path = d / "reply.md"
            reply_path.write_text(reply, encoding="utf-8")
            log(f"[z.ai] ревью: {len(reply)} байт ({zai_dur:.1f}s) -> {reply_path}")
            log(f"[metrics] iter={i} opencode={oc_dur:.1f}s zai={zai_dur:.1f}s")
            M.inc("zai_reviews")
            M.hist("zai_latency_ms", zai_dur * 1000)
            M.hist("zai_reply_bytes", len(reply))
            write_status(session, "review_ready", {"iter": i, "reply_bytes": len(reply)})

            prev_plan = plan  # для следующей итерации opencode_cont (PREV_PLAN)
            journal("iter_done", {"i": i, "plan_bytes": len(plan),
                                  "reply_bytes": len(reply),
                                  "opencode_s": oc_dur, "zai_s": zai_dur})

    except KeyboardInterrupt:
        log("[interrupt] Прервано пользователем (Ctrl+C)")
        M.inc("sessions_interrupted")
        write_status(session, "abandoned", {"reason": "interrupt"})
        journal("session_failed", {"session": session.name, "reason": "interrupt"})
        raise
    except Exception as e:
        # format_exc() внутри except ловит контекст; sys.exc_info() — запасной
        import sys
        _, exc_val, exc_tb = sys.exc_info()
        tb_lines = traceback.format_exception(type(e), e, exc_tb)
        tb_str = "".join(tb_lines)
        log(f"[CRITICAL] Необработанное исключение: {type(e).__name__}: {e}")
        for line in tb_lines:
            log(f"  {line.rstrip()}")
        M.inc("sessions_failed")
        M.inc(f"errors_{type(e).__name__}")
        write_status(session, "failed", {
            "reason": "exception",
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb_str,
        })
        journal("session_failed", {
            "session": session.name,
            "reason": "exception",
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb_str,
        })
        raise
    finally:
        pass

    total = time.time() - run_start
    log(f"[metrics] ИТОГО прогон: {total:.1f}s ({N} итераций), control={ctrl}")
    log("Цикл завершён.")
    write_status(session, "done", {"total_s": total, "iterations": N})
    journal("session_done", {"session": str(session), "N": N, "control": ctrl,
                             "total_s": total})
    # export metrics to session dir
    metrics_path = session / "metrics.json"
    M.export(str(metrics_path))
    log(f"[metrics] экспортирован в {metrics_path}")


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--dry-run", action="store_true")
    _ap.add_argument("--task", help="USER_TASK (неинтерактивный режим)")
    _ap.add_argument("--n", type=int, help="число итераций (неинтерактивный режим)")
    _ap.add_argument("--cleanup", action="store_true",
                     help="удалить пустые/битые сессии и выйти")
    _ap.add_argument("--cleanup-dry", action="store_true",
                     help="показать что будет удалено (без удаления)")
    _ap.add_argument("--list", action="store_true",
                     help="показать все сессии из реестра")
    _ap.add_argument("--list-status", help="фильтр по статусу (done, failed, ...)")
    _ap.add_argument("--reclaim", action="store_true",
                     help="пометить застрявшие сессии как abandoned/failed и выйти")
    _a = _ap.parse_args()
    if _a.reclaim:
        reclaimed = reclaim_stuck_sessions()
        if reclaimed:
            print(f"Reclaimed {len(reclaimed)} stuck session(s):")
            for name, old, new in reclaimed:
                print(f"  {name}: {old} -> {new}")
        else:
            print("Net zastryavshih sessiy")
    elif _a.list:
        items = registry_list(status_filter=_a.list_status)
        if not items:
            print("Реестр пуст")
        else:
            print(f"{'Сессия':<50} {'Статус':<15} {'Обновлён'}")
            print("-" * 90)
            for name, e in items:
                print(f"{name:<50} {e.get('status','?'):<15} {e.get('updated','?')[:19]}")
    elif _a.cleanup or _a.cleanup_dry:
        removed = cleanup_empty_sessions(dry=_a.cleanup_dry)
        orphaned = registry_cleanup(dry=_a.cleanup_dry)
        total = len(removed) + len(orphaned)
        if removed:
            print(f"Udaleno/budet udaleno: {len(removed)} sessiy (fayly):")
            for name, reason in removed:
                print(f"  {name}: {reason}")
        if orphaned:
            print(f"Udaleno iz registra: {len(orphaned)} (papki ne naydeny):")
            for name in orphaned:
                print(f"  {name}")
        if not total:
            print("Chisto: pustyh/bitih sessiy net")
    else:
        run(dry_run=_a.dry_run, task=_a.task, N=_a.n)
