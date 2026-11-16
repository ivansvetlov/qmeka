"""
debug_bootstrap.py — каркас Фазы 1 (Setup/Рекон) оркестратора z.ai<->opencode.
Запуск:  python debug_bootstrap.py --debug
Делает:
  1) запускает изолированный профиль Chrome с remote-debugging (порт 9222);
  2) проверяет CDP endpoint (/json/version);
  3) утилита координат: tkinter-окно с live x,y, клик -> захват точки ввода opencode;
  4) тест персистентности логина z.ai (закрыть/переоткрыть, спросить у юзера);
  5) генерирует config.json + пишет run.log / journal.jsonl.
Это ЗАГОТОВКА: детекция порога вставки opencode и бюджета z.ai (§19) добавляются
по ходу. Имплементация драйверов — после Фазы 1.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import shutil
import datetime

try:
    import requests
except ImportError:
    requests = None

try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    tk = None

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, "sessions")
PROFILE = r"C:\Orchestrator\ChromeProfile"
CDP_PORT = 9222
ZAI_URL = "https://chat.z.ai/"
RUN_LOG = os.path.join(HERE, "run.log")
JOURNAL = os.path.join(HERE, "journal.jsonl")
CONFIG_PATH = os.path.join(HERE, "config.json")


def log(msg, level="INFO"):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(RUN_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"[{level}] {line}")
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": datetime.datetime.now().isoformat(),
                            "event": level, "details": msg}, ensure_ascii=False) + "\n")


def find_chrome():
    cand = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            shutil.which("chrome"), shutil.which("chrome.exe")]
    for c in cand:
        if c and os.path.exists(c):
            return c
    return None


def profile_has_saved_session(profile):
    cookies = os.path.join(profile, "Default", "Network", "Cookies")
    cookies_old = os.path.join(profile, "Default", "Cookies")
    return os.path.exists(cookies) or os.path.exists(cookies_old)


def launch_chrome(port, profile, debug=False, url=None):
    chrome = find_chrome()
    if not chrome:
        log("Chrome не найден в PATH/стандартных путях", "CRITICAL")
        sys.exit(1)
    os.makedirs(profile, exist_ok=True)
    cmd = [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
           "--disable-blink-features=AutomationControlled", "--no-first-run",
           "--no-default-browser-check"]
    if url:
        cmd.append(url)
    log(f"Запуск Chrome: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    return proc


def wait_cdp(port, timeout=10):
    if requests is None:
        log("requests не установлен — CDP-проверка пропущена", "WARN")
        return True
    for _ in range(timeout):
        try:
            r = requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
            data = r.json()
            log(f"CDP OK: {data.get('Browser')} | {data.get('webSocketDebuggerUrl')}")
            return True
        except Exception as e:
            time.sleep(1)
    log(f"CDP недоступен на порту {port}", "CRITICAL")
    return False


def capture_coords():
    """Утилита координат: ГЛОБАЛЬНЫЙ перехват левых кликов по ВСЕМУ экрану
    (в т.ч. вне окна — в поле opencode), берём САМУЮ НИЖНЮЮ точку (макс y).
    Клики по самому окну калибровки игнорируются. Кнопка «Готово»."""
    if tk is None or pyautogui is None:
        log("tkinter/pyautogui не установлены — захват координат пропущен", "WARN")
        return None
    import ctypes, threading
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    WM_LBUTTONDOWN = 0x0201
    WM_QUIT = 0x0012

    # Корректные прототипы Win32 (без них GetMessageW/SetWindowsHookEx ведут себя непредсказуемо)
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

    HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)
    clicks = []
    chosen = {"pos": None}
    root_ref = {"w": None}
    stop_ev = threading.Event()
    state = {"hook": None}

    def proc(nCode, wParam, lParam):
        if nCode >= 0 and wParam == WM_LBUTTONDOWN:
            hs = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            x, y = hs.pt.x, hs.pt.y
            r = root_ref["w"]
            if r is not None:  # игнорировать клики по нашему окну
                rx, ry = r.winfo_rootx(), r.winfo_rooty()
                rw, rh = r.winfo_width(), r.winfo_height()
                if rx <= x <= rx + rw and ry <= y <= ry + rh:
                    return user32.CallNextHookEx(state["hook"], nCode, wParam, lParam)
            clicks.append((x, y))
            chosen["pos"] = max(clicks, key=lambda p: p[1])  # нижняя = макс y
            log(f"global клик {len(clicks)}: {(x, y)} -> нижняя {chosen['pos']}")
        return user32.CallNextHookEx(state["hook"], nCode, wParam, lParam)

    hookproc = HOOKPROC(proc)

    def pump():
        # LL-hook ставим в потоке, у которого есть свой message-loop
        state["hook"] = user32.SetWindowsHookExW(14, hookproc, None, 0)
        if not state["hook"]:
            log(f"Global hook не установлен (err {ctypes.GetLastError()})", "WARN")
        msg = wintypes.MSG()
        while not stop_ev.is_set():
            rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if rc <= 0 or msg.message == WM_QUIT:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if state["hook"]:
            user32.UnhookWindowsHookEx(state["hook"])
            state["hook"] = None

    threading.Thread(target=pump, daemon=True).start()

    def upd():
        try:
            cx, cy = pyautogui.position()
            txt = (f"курсор x={cx} y={cy} | кликов: {len(clicks)} | "
                   f"нижняя: {chosen['pos']}\n"
                   f"(кликайте ВНЕ окна, в поле opencode, потом «Готово»)")
            lbl.config(text=txt)
            root.after(50, upd)
        except Exception:
            root.after(200, upd)

    def finish():
        stop_ev.set()
        user32.PostQuitMessage(0)
        if not chosen["pos"]:
            messagebox.showwarning("Нет точки",
                "Кликните в поле ввода opencode ВНЕ этого окна, затем «Готово».")
            return
        log(f"Итоговая точка ввода opencode (нижняя): {chosen['pos']}")
        root.destroy()

    root = tk.Tk()
    root_ref["w"] = root
    root.title("Калибровка opencode (глобальный захват кликов)")
    root.attributes("-topmost", True)
    lbl = tk.Label(root, text="...", font=("Consolas", 12), padx=20, pady=10)
    lbl.pack()
    btn = tk.Button(root, text="Готово (захватить нижнюю)", command=finish,
                    font=("Consolas", 12))
    btn.pack(pady=8)
    upd()
    root.mainloop()
    return chosen["pos"]


def persistence_test_prompt():
    if tk is None:
        ans = input("После перезапуска Chrome z.ai остался залогинен? (y/n): ")
        return ans.strip().lower() == "y"
    return messagebox.askyesno("Тест персистентности",
                               "Chrome перезапущен с тем же профилем.\nz.ai остался залогинен?")


def load_config():
    p = CONFIG_PATH
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def clone_cookies_from(src, dst_profile):
    """Перенос куки (авторизация) из основного профиля Chrome в оркестраторский.
    ВНИМАНИЕ: основной Chrome должен быть ЗАКРЫТ (иначе Cookies заблокирован)."""
    if not os.path.isdir(src):
        log(f"Исходный профиль не найден: {src}", "CRITICAL")
        return False
    copied = False
    for rel in ["Cookies", os.path.join("Network", "Cookies")]:
        s = os.path.join(src, rel)
        d = os.path.join(dst_profile, rel)
        if os.path.exists(s):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            try:
                shutil.copy2(s, d)
                copied = True
                log(f"Скопирован {s} -> {d}")
            except OSError as e:
                log(f"Не удалось скопировать {s}: {e} (файл заблокирован? "
                    f"закройте основной Chrome)", "WARN")
        else:
            log(f"Нет файла куки: {s}", "WARN")
    if not copied:
        log("Cookies не найдены в исходном профиле", "WARN")
    return copied


def build_config(port, coords, thresholds):
    cfg = {
        "workdir": "sessions",
        "cdp_port": port,
        "chrome_profile_path": PROFILE,
        "injection_mode": "paste",
        "zai_upload_method": "cdp",
        "abs_paths_ok": True,
        "debug": True,
        "opencode_input_xy": list(coords) if coords else None,
        "zai_url": args.zai_url,
        "timeouts": {"T_open": 900, "T_zai": 300, "T_stable_max": 120},
        "thresholds": thresholds,
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)
    log("config.json сгенерирован")
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="режим debug (§19)")
    ap.add_argument("--port", type=int, default=CDP_PORT)
    ap.add_argument("--zai-url", default=ZAI_URL, help="URL чата z.ai для логина/проверки")
    ap.add_argument("--recalibrate", action="store_true",
                    help="только перекалибровать координаты opencode и выйти")
    ap.add_argument("--clone-from", default=None,
                    help="перенести куки авторизации из основного профиля Chrome (путь)")
    args = ap.parse_args()
    os.makedirs(WORKDIR, exist_ok=True)
    log(f"=== debug_bootstrap запуск (debug={args.debug}, zai_url={args.zai_url}) ===")

    # Режим: только перекалибровка координат opencode
    if args.recalibrate:
        cfg = load_config()
        log("Только перекалибровка координат opencode.")
        coords = capture_coords()
        if coords:
            cfg["opencode_input_xy"] = list(coords)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            log(f"config.json обновлён (координаты opencode: {coords}).")
        else:
            log("Координаты не захвачены — config не изменён", "WARN")
        sys.exit(0)

    # Режим: перенос куки из основного Chrome (закройте основной Chrome перед запуском)
    if args.clone_from:
        log(f"Клонирование куки из {args.clone_from} -> {PROFILE} ...")
        clone_cookies_from(args.clone_from, PROFILE)
        start_url = None if profile_has_saved_session(PROFILE) else args.zai_url
        proc = launch_chrome(args.port, PROFILE, debug=args.debug, url=start_url)
        if not wait_cdp(args.port):
            sys.exit(1)
        time.sleep(5)
        ok = persistence_test_prompt()
        log(f"Логин z.ai после клонирования: {'OK' if ok else 'НЕТ — закройте основной Chrome и повторите'}",
            "OK" if ok else "WARN")
        coords = load_config().get("opencode_input_xy")  # без повторной калибровки
        cfg = load_config()
        cfg.update({
            "workdir": "sessions", "cdp_port": args.port, "chrome_profile_path": PROFILE,
            "injection_mode": "paste", "zai_upload_method": "cdp", "abs_paths_ok": True,
            "debug": args.debug, "opencode_input_xy": coords,
            "zai_url": args.zai_url,
            "timeouts": {"T_open": 900, "T_zai": 300, "T_stable_max": 120},
            "thresholds": {"paste_dialog_kb": None, "zai_ctx_kb": 100},
        })
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        log("config.json сгенерирован (clone-режим).")
        sys.exit(0)

    start_url = None if profile_has_saved_session(PROFILE) else args.zai_url
    proc = launch_chrome(args.port, PROFILE, debug=args.debug, url=start_url)
    if not wait_cdp(args.port):
        sys.exit(1)

    # HUD-подсказка: куда логиниться (URL чата z.ai — предоставлен пользователем ранее)
    log("В открывшемся Chrome уже открыт z.ai — залогиньтесь вручную.")
    if tk is not None:
        messagebox.showinfo("Логин z.ai",
            "В открывшемся Chrome уже открыт чат z.ai. Залогиньтесь вручную, затем нажмите OK.")
    else:
        input(">>> после логина нажмите Enter: ")

    # Тест персистентности: закрыть и переоткрыть Chrome (с открытым z.ai для проверки)
    log("Тест персистентности: закрываем Chrome...")
    proc.terminate()
    time.sleep(3)
    proc = launch_chrome(args.port, PROFILE, debug=args.debug, url=None)
    wait_cdp(args.port)
    time.sleep(5)  # дать странице z.ai прогрузиться для проверки входа
    ok = persistence_test_prompt()
    log(f"Персиcтентность логина z.ai: {'OK' if ok else 'НЕ СОХРАНИЛСЯ — повторите логин'}",
        "OK" if ok else "WARN")

    # Калибровка opencode (утилита координат)
    log("Запуск утилиты координат — кликните в поле ввода opencode.")
    coords = capture_coords()

    # Пороги (§19): пока консервативные; уточняются пробными вставками позже
    thresholds = {"paste_dialog_kb": None, "zai_ctx_kb": 100}

    build_config(args.port, coords, thresholds)
    log("Фаза 1 (каркас) завершена. Дальше — имплементация драйверов.")


if __name__ == "__main__":
    main()
