"""
opencode_driver.py — OS-драйвер opencode (Фаза 4–5, микрошаг: фокус/клик + focus-guard).
Зависимости: ctypes (встроен), pyautogui (уже установлен). Без сети.

Назначение:
- найти окно opencode по калиброванной точке ввода (config.json: opencode_input_xy);
- надёжно поднять его в фокус (SetForegroundWindow + AttachThreadInput — обход
  ограничения Windows на подъём чужого окна);
- кликнуть в поле;
- focus-guard: проверять фокус до/во время ввода и перефокусировать при краже.

Режимы (безопасные для проверки):
  --check    : только сообщить, какое окно находится в точке координат (без клика)
  --click    : поднять фокус и кликнуть в поле (без ввода текста)
"""
import json
import os
import sys
import time
import subprocess
import ctypes
import ctypes.wintypes
import urllib.request
import urllib.error
from datetime import datetime
from ctypes import wintypes
from pathlib import Path

from _log import log as _raw_log, set_file as _set_logfile

RUN_LOG = Path(__file__).parent / "run.log"
_set_logfile(RUN_LOG)

try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    import tkinter as tk
except ImportError:
    tk = None

CONFIG = str(Path(__file__).resolve().parent / "config.json")
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

GA_ROOT = 2
WM_NULL = 0


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def top_level_at(x, y):
    """Верхнеуровневое окно, содержащее точку (x,y)."""
    pt = wintypes.POINT(x, y)
    child = user32.WindowFromPoint(pt)
    if not child:
        return None
    root = user32.GetAncestor(child, GA_ROOT)
    return root or child


def window_title(hwnd):
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_class(hwnd):
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def bring_to_front(hwnd):
    """Поднять окно в фокус. Возвращает True, если окно стало foreground."""
    if not hwnd:
        return False
    cur = user32.GetForegroundWindow()
    if hwnd == cur:
        return True
    my_thread = kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(cur, None)
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    if fg_thread and fg_thread != my_thread:
        user32.AttachThreadInput(my_thread, fg_thread, True)
        attached.append(fg_thread)
    if target_thread and target_thread != my_thread:
        user32.AttachThreadInput(my_thread, target_thread, True)
        attached.append(target_thread)
    try:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.05)
        user32.SetFocus(hwnd)
    finally:
        for t in attached:
            user32.AttachThreadInput(my_thread, t, False)
    return user32.GetForegroundWindow() == hwnd


def focus_guard(x, y, retries=3, settle=0.15):
    """Убедиться, что окно в точке (x,y) в фокусе; если нет — поднять.
    Возвращает hwnd цели или None после исчерпания попыток."""
    target = top_level_at(x, y)
    if not target:
        return None
    for i in range(retries):
        if user32.GetForegroundWindow() == target:
            return target
        bring_to_front(target)
        time.sleep(settle)
        if user32.GetForegroundWindow() == target:
            return target
    return None  # не удалось — вызывающий должен поднять HUD-алерт


def click_input(x, y, retries=3):
    """Поднять фокус и кликнуть в поле opencode (без ввода текста)."""
    if pyautogui is None:
        print("pyautogui не установлен")
        return False
    hwnd = focus_guard(x, y, retries)
    if not hwnd:
        print(f"[FAIL] не удалось удержать фокус окна в точке ({x},{y})")
        return False
    pyautogui.click(x, y)
    print(f"[OK] фокус+клик в ({x},{y}); окно: '{window_title(hwnd)}' [{window_class(hwnd)}]")
    return True


def log(msg, level="INFO"):
    """Unified logger with correlation-id."""
    _raw_log(msg, level=level)


def save_clipboard():
    """Сохранить текущий буфер обмена (tkinter, без зависимостей)."""
    if tk is None:
        return None
    try:
        r = tk.Tk()
        r.withdraw()
        data = r.clipboard_get()
        r.destroy()
        return data
    except Exception:
        return None


def set_clipboard(text):
    if tk is None:
        return False
    r = tk.Tk()
    r.withdraw()
    r.clipboard_clear()
    r.clipboard_append(text)
    r.update()  # принудительно протолкнуть в системный буфер
    r.destroy()
    return True


def restore_clipboard(data):
    if data is None or tk is None:
        return
    r = tk.Tk()
    r.withdraw()
    r.clipboard_clear()
    r.clipboard_append(data)
    r.update()
    r.destroy()


def paste_text(text, x, y, retries=3):
    """Вставить text в поле opencode: save/restore буфера + Ctrl+V.
    Возвращает True/False."""
    if pyautogui is None:
        print("pyautogui не установлен")
        return False
    saved = save_clipboard()
    hwnd = focus_guard(x, y, retries)
    if not hwnd:
        print(f"[FAIL] не удалось удержать фокус окна в точке ({x},{y})")
        if saved is not None:
            restore_clipboard(saved)
        return False
    if not set_clipboard(text):
        print("[FAIL] не удалось записать буфер")
        return False
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    restore_clipboard(saved)  # race-condition: вернуть буфер пользователя
    print(f"[OK] вставка ({len(text)} симв.) в '{window_title(hwnd)}'")
    return True


def send_enter_seq(x, y, dialog_pause=0.3, retries=3):
    """ENTER_SEQ (единый, 2 Enter): фокус -> Enter (диалог) -> пауза -> Enter.
    При краже фокуса ВО ВРЕМЯ паузы — повторить ВСЮ серию (§21), не докидывать."""
    if pyautogui is None:
        print("pyautogui не установлен")
        return False
    for _ in range(retries):
        hwnd = focus_guard(x, y, retries)
        if not hwnd:
            print("[FAIL] не удалось удержать фокус перед Enter")
            return False
        pyautogui.press("enter")
        time.sleep(dialog_pause)
        if user32.GetForegroundWindow() != hwnd:
            # фокус украден — перефокусируемся и повторяем оба Enter целиком
            continue
        pyautogui.press("enter")
        return True
    print("[FAIL] ENTER_SEQ не прошёл после повторов (фокус крадётся)")
    return False


def inject_prompt(text, x, y):
    """Полный впрыск: вставка промпта + ENTER_SEQ."""
    if not paste_text(text, x, y):
        return False
    return send_enter_seq(x, y)


def _build_run_cmd(prompt_file, session_dir, *, model=None,
                    continue_session=False, attach_url=None, session_id=None):
    """Собрать команду `opencode run` (direct, через serve или serve-сессию)."""
    msg = "Execute the task specified in the attached file prompt.md. Follow it exactly."
    if attach_url:
        # транспорт serve: сервер уже держит сессию/провайдера/MCP
        cmd = (f'opencode run "{msg}" --auto --format json '
               f'--attach "{attach_url}" --file "{prompt_file}"')
    elif session_id:
        # drive конкретной serve-сессии: -s <id> продолжает именно её
        # (контекст между итерациями оркестратора; без -c/-m).
        cmd = (f'opencode run "{msg}" --auto --format json '
               f'--dir "{session_dir}" --file "{prompt_file}" -s "{session_id}"')
    else:
        cmd = (f'opencode run "{msg}" --auto --format json '
               f'--dir "{session_dir}" --file "{prompt_file}"')
    if model and not session_id:
        cmd += f' -m {model}'
    if continue_session and not session_id:
        # -c (continue) продолжает ПОСЛЕДНЮЮ сессию opencode (контекст
        # переносится между итерациями оркестратора). Именованные сессии
        # opencode не создаёт сам — поэтому продолжаем последнюю.
        cmd += ' -c'
    return cmd


def opencode_run(prompt, session_dir, timeout=900, model=None,
                 continue_session=False, attach_url=None, session_id=None):
    """Детерминированный headless-режим: `opencode run --auto --format json`
    пишет ответ в stdout (JSON-события), не требует TUI/фокуса/выбора сессии.
    Промпт кладём в файл и аттачим через --file (чтобы не квотировать
    многострочный текст в shell). План пишем сами.

    - model:            фиксирует модель (напр. hy3-free) из config.
    - continue_session: True на i>1 — продолжить последнюю сессию opencode
                        (общий контекст между шагами оркестратора).
    - attach_url:       если задан — подключаемся к `opencode serve` (без
                        холодного старта провайдера/MCP на каждую итерацию).
    - session_id:       если задан — продолжаем КОНКРЕТНУЮ serve-сессию
                        через `opencode run -s <id>` (структурированный JSON,
                        общий контекст; то же, что показывает web-вкладка).
    Возвращает текст ответа агента."""
    import json as _json
    import subprocess
    import time as _time
    from _metrics import M
    # промпт -> файл (без shell-квотинга новых строк)
    prompt_file = os.path.join(session_dir, "prompt.md")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = _build_run_cmd(prompt_file, session_dir, model=model,
                         continue_session=continue_session, attach_url=attach_url,
                         session_id=session_id)
    t0 = _time.time()
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        M.inc("opencode_timeouts")
        log(f"[opencode] TIMEOUT after {timeout}s", level="ERROR")
        return ""
    except Exception as e:
        M.inc("opencode_errors")
        log(f"[opencode] ERROR: {e}", level="ERROR")
        return ""
    dur = _time.time() - t0
    M.hist("opencode_run_ms", dur * 1000)
    if not proc.stdout.strip():
        log(f"[opencode] stdout пуст; stderr: {proc.stderr.strip()[:500]}", level="WARN")
    texts = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = _json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "text":
            t = ev.get("part", {}).get("text") or ev.get("text")
            if t:
                texts.append(t)
    return "".join(texts)


def wait_for_done(session_dir, timeout=900, stable_max=120, stable_window=10, poll=2):
    """Watchdog: ждать done.json (авторитетный маркер), затем стабилизацию
    размера plan.md. Возвращает путь к done.json или None по таймауту."""
    from _metrics import M
    import time as _time
    done = os.path.join(session_dir, "done.json")
    plan = os.path.join(session_dir, "plan.md")
    elapsed = 0
    while elapsed < timeout:
        if os.path.exists(done):
            break
        time.sleep(poll)
        elapsed += poll
    else:
        M.inc("opencode_wait_done_timeouts")
        log(f"[opencode] TIMEOUT waiting done.json after {timeout}s: {session_dir}", level="ERROR")
        return None
    # стабилизация plan.md (done.json уже есть)
    if not os.path.exists(plan):
        return done
    stable_for = 0
    last = None
    e2 = 0
    while e2 < stable_max:
        if os.path.exists(plan):
            sz = os.path.getsize(plan)
            if sz == last:
                stable_for += poll
            else:
                stable_for = 0
                last = sz
            if stable_for >= stable_window:
                break
        time.sleep(poll)
        e2 += poll
    M.hist("opencode_wait_done_ms", (elapsed + e2) * 1000)
    log(f"[opencode] done.json + plan.md стабилен: {session_dir}", level="DEBUG")
    return done


def start_serve(port=4096, session_id=None, session_dir=None, timeout=15):
    """Запустить `opencode serve` в фоне (без холодного старта провайдера/MCP
    на каждую итерацию). Возвращает (proc, url) или (None, None) при неудаче."""
    import urllib.request
    cmd = f'opencode serve --port {port}'
    if session_id:
        cmd += f' -s "{session_id}"'
    if session_dir:
        cmd += f' --dir "{session_dir}"'
    try:
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=0x00000008)  # DETACHED_PROCESS
    except Exception as e:
        log(f"[serve] не удалось стартовать: {e}")
        return None, None
    url = f"http://localhost:{port}"
    # ждать, пока сервер поднимет порт
    for _ in range(timeout):
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status < 500:
                    log(f"[serve] поднят: {url} (pid={proc.pid})")
                    return proc, url
        except Exception:
            time.sleep(1)
    log("[serve] таймаут ожидания порта — фоллбэк на direct run")
    try:
        proc.kill()
    except Exception:
        pass
    return None, None


def stop_serve(proc):
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log("[serve] остановлен")


def _serve_req(method, port, path, body=None, timeout=120):
    """HTTP-запрос к REST API opencode serve (база /api)."""
    url = f"http://localhost:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def create_session(port, directory, agent="build", model=None, timeout=15):
    """Создать serve-сессию opencode (HTTP). Возвращает session id или None.
    Сессия фиксирует directory/agent/model — дальше управляем через
    `opencode run -s <id>` (структурированный JSON, тот же, что в web-UI).
    model передаётся как Model.Ref ({id, providerID}); если None — берётся
    дефолт сервера (hy3-free)."""
    body = {"directory": directory, "agent": agent}
    if model:
        if isinstance(model, dict):
            body["model"] = model
        else:
            # "opencode/hy3-free" -> {"id":"hy3-free","providerID":"opencode"}
            prov, _, mid = model.partition("/")
            body["model"] = {"id": mid or model, "providerID": prov or "opencode"}
    s, b = _serve_req("POST", port, "/api/session", body, timeout=timeout)
    if s != 200:
        log(f"[serve] create session failed: {s} {b[:200]}")
        return None
    try:
        return json.loads(b)["data"]["id"]
    except Exception:
        return None


def mount_opencode_web(browser_port, serve_port, close_on_exit=True):
    """Открыть opencode web-вкладку в ТОМ ЖЕ изолированном Chrome (browser CDP),
    где живёт z.ai. Возвращает targetId или None. control через эту вкладку —
    HTTP поверх того же структурированного бэкенда serve (без потери JSON)."""
    try:
        from cdp_ws import browser_ws_url, CDP
        b = CDP(browser_ws_url(browser_port))
        r = b.send("Target.createTarget",
                   {"url": f"http://127.0.0.1:{serve_port}"})
        tid = r.get("targetId")
        b.close()
        log(f"[web] opencode web tab mounted: targetId={tid} "
            f"url=http://127.0.0.1:{serve_port}")
        return tid
    except Exception as e:
        log(f"[web] mount failed: {e}")
        return None


def opencode_http_send(prompt, session_id, port, timeout=900, poll=2):
    """HTTP-канал управления opencode (вместо зависающего `opencode run -s`):
    POST /api/session/:id/prompt {"prompt":{"text":...}} -> ждём ответ
    ассистента через GET /api/session/:id/message. Возвращает текст ответа.
    (plan.md агент пишет сам в directory сессии.)"""
    s, b = _serve_req("POST", port, f"/api/session/{session_id}/prompt",
                      {"prompt": {"text": prompt}}, timeout=30)
    if s != 200:
        log(f"[serve] prompt send failed: {s} {b[:200]}")
        return ""
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        time.sleep(poll)
        st, mb = _serve_req("GET", port, f"/api/session/{session_id}/message", timeout=30)
        if st != 200:
            continue
        try:
            msgs = json.loads(mb).get("data", [])
        except Exception:
            continue
        cur = _collect_assistant_text(msgs)
        if cur:
            last_text = cur
            # генерация завершена, если ответ не растёт (стабилизация)
            time.sleep(poll)
            st2, mb2 = _serve_req("GET", port, f"/api/session/{session_id}/message", timeout=30)
            if st2 == 200:
                cur2 = _collect_assistant_text(json.loads(mb2).get("data", []))
                if cur2 == last_text:
                    return last_text
                last_text = cur2
    log("[serve] prompt ответ не получен за таймаут")
    return last_text


def _collect_assistant_text(msgs):
    """Собрать текст ответа ассистента. opencode кладёт ответ в m['content']
    (строка или объект); наше сообщение имеет m['prompt']/m['text']."""
    out = []
    for m in msgs:
        if "prompt" in m or m.get("type") == "user":
            continue
        c = m.get("content")
        if c is None:
            c = "".join(p.get("text", "") for p in m.get("parts", [])
                       if p.get("type") == "text")
        if isinstance(c, dict):
            c = c.get("text") or c.get("content") or ""
        if c:
            out.append(str(c))
    return "".join(out)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="только показать окно в точке координат")
    ap.add_argument("--click", action="store_true", help="поднять фокус и кликнуть")
    ap.add_argument("--paste-test", action="store_true",
                    help="безопасно вставить сентинел ORCHESTRATOR_PASTE_OK (проверка буфера+Ctrl+V)")
    ap.add_argument("--enter-test", action="store_true",
                    help="ОПАСНО без opencode: шлёт 2×Enter в окно в точке (только при живом opencode)")
    ap.add_argument("--watch-self-test", action="store_true",
                    help="безопасный тест watchdog: создаёт временную папку с done.json+plan.md")
    args = ap.parse_args()
    cfg = load_config()
    xy = cfg.get("opencode_input_xy")
    if not xy or len(xy) != 2:
        print("opencode_input_xy нет в config.json — сначала калибровка")
        sys.exit(1)
    x, y = xy
    hwnd = top_level_at(x, y)
    print(f"Точка ({x},{y}) -> окно: '{window_title(hwnd)}' [{window_class(hwnd)}] hwnd={hwnd}")
    if args.check:
        return
    if args.click:
        click_input(x, y)
    if args.paste_test:
        paste_text("ORCHESTRATOR_PASTE_OK", x, y)
    if args.enter_test:
        print("[WARN] --enter-test шлёт Enter в окно в точке; запускай только при живом opencode!")
        send_enter_seq(x, y)
    if args.watch_self_test:
        import tempfile, shutil
        d = tempfile.mkdtemp(prefix="orch_watch_")
        with open(os.path.join(d, "done.json"), "w", encoding="utf-8") as f:
            f.write('{"role":"opencode","iter":1}')
        with open(os.path.join(d, "plan.md"), "w", encoding="utf-8") as f:
            f.write("x" * 100)
        time.sleep(1)
        with open(os.path.join(d, "plan.md"), "a", encoding="utf-8") as f:
            f.write("y" * 50)  # имитация дописывания
        time.sleep(1)
        res = wait_for_done(d, timeout=10, stable_max=10, stable_window=2, poll=1)
        shutil.rmtree(d, ignore_errors=True)
        print(f"[SELFTEST] watchdog вернул: {res}")


if __name__ == "__main__":
    main()
