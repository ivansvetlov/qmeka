#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
session_pool.py — Session Pool + browser introspection (План §7, UI_AND_SESSIONS_PLAN.md).

Назначение:
  - Интроспекция: найти установленные браузеры (Chrome/Edge/Firefox) и их профили.
  - Детект «провайдер залогинен?» по cookie-БД профиля (наличие записи домена).
  - «Открыть вход»: спавн Chrome с изолированным профилем оркестратора + login_url
    (пользователь входит вручную; сессия остаётся в профиле оркестратора).
  - Статус провайдеров для дашборда (/api/session_pool).

Принцип (§7.1): ничего не хардкодим под конкретного юзера. Конфиг-дефолты +
интроспекция. session_source=null => авто-поиск.

Cookies: Chromium (Chrome/Edge) — sqlite 'cookies', колонка 'host_key';
Firefox — cookies.sqlite, 'moz_cookies', колонка 'host'. Значения зашифрованы,
но наличие записи домена достаточно как сигнал «вероятно вошёл».
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"

# ── Интроспекция браузеров (стандартные пути Windows) ────────────────────

CHROMIUM_BROWSERS = [
    {
        "id": "chrome",
        "name": "Google Chrome",
        "user_data": os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
        "exe_candidates": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
    },
    {
        "id": "edge",
        "name": "Microsoft Edge",
        "user_data": os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
        "exe_candidates": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
    },
]

FIREFOX = {
    "id": "firefox",
    "name": "Mozilla Firefox",
    "profiles_root": os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"),
    "exe_candidates": [
        r"C:\Program Files\Mozilla Firefox\firefox.exe",
        r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    ],
}


def _list_dir_dirs(root: str | Path) -> list[str]:
    try:
        p = Path(root)
        if not p.exists():
            return []
        return sorted(d.name for d in p.iterdir() if d.is_dir())
    except OSError:
        return []


def probe_browsers() -> list[dict]:
    """Найти установленные браузеры и их профили (без оценки логина)."""
    out = []
    for b in CHROMIUM_BROWSERS:
        profiles = [
            n for n in _list_dir_dirs(b["user_data"])
            if n == "Default" or n.startswith("Profile ")
        ]
        if profiles:
            exe = next((e for e in b["exe_candidates"] if Path(e).exists()), None)
            out.append({
                "id": b["id"], "name": b["name"], "exe": exe,
                "user_data": b["user_data"],
                "profiles": [
                    {"name": n, "path": str(Path(b["user_data"]) / n)}
                    for n in profiles
                ],
            })
    ff_dirs = _list_dir_dirs(FIREFOX["profiles_root"])
    ff_profiles = [d for d in ff_dirs if ".default" in d.lower() or d.endswith("-release")]
    if ff_profiles:
        exe = next((e for e in FIREFOX["exe_candidates"] if Path(e).exists()), None)
        out.append({
            "id": FIREFOX["id"], "name": FIREFOX["name"], "exe": exe,
            "user_data": FIREFOX["profiles_root"],
            "profiles": [
                {"name": d, "path": str(Path(FIREFOX["profiles_root"]) / d)}
                for d in ff_profiles
            ],
        })
    return out


# ── Cookie-детект ────────────────────────────────────────────────────────

def _cookie_db_copy(db_path: Path) -> Path | None:
    """Cookie-БД обычно заблокирована работающим браузером — читаем копию.

    Возвращает None, если файла нет ИЛИ он заблокирован (PermissionError ⊂ OSError)
    — вызывающий код трактует это как «не проверено».
    """
    try:
        if not db_path.exists():
            return None
        tmp = Path(tempfile.gettempdir()) / f"orch_cookie_probe_{os.getpid()}_{db_path.name}"
        shutil.copy2(db_path, tmp)
        for ext in ("-wal", "-shm"):
            src = Path(str(db_path) + ext)
            if src.exists():
                shutil.copy2(src, Path(str(tmp) + ext))
        return tmp
    except OSError:
        return None


def has_domain_cookie(db_path: Path, domain: str, browser_id: str) -> bool:
    """True, если в cookie-БД есть запись для домена (сигнал «вероятно вошёл»).

    ВАЖНО: это слабый сигнал. Некоторые провайдеры хранят сессию в localStorage,
    а не в куках (DeepSeek — см. ds_token_refresh.py). Для них используйте
    has_localstorage_hint().
    """
    tmp = _cookie_db_copy(db_path)
    if not tmp:
        return False
    try:
        con = sqlite3.connect(str(tmp))
        try:
            cur = con.cursor()
            if browser_id == "firefox":
                cur.execute(
                    "SELECT 1 FROM moz_cookies WHERE host LIKE ? LIMIT 1",
                    (f"%{domain}%",))
            else:
                cur.execute(
                    "SELECT 1 FROM cookies WHERE host_key LIKE ? LIMIT 1",
                    (f"%{domain}%",))
            return cur.fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
            for ext in ("-wal", "-shm"):
                Path(str(tmp) + ext).unlink(missing_ok=True)
        except OSError:
            pass


def has_localstorage_hint(profile_path: Path, needle: str) -> bool:
    """True, если в Local Storage leveldb профиля встречается подстрока-хинт.

    Работает как ds_token_refresh.py: сканирует бинарные leveldb-файлы
    (…/Local Storage/leveldb/*) на предмет байтового вхождения (например
    'Token' для DeepSeek). Читаемо даже при работающем браузере; при
    блокировке отдельных файлов — просто пропускает их.
    """
    ls_dir = profile_path / "Local Storage" / "leveldb"
    if not ls_dir.exists():
        return False
    nb = needle.encode("utf-8")
    try:
        for f in ls_dir.iterdir():
            if not f.is_file():
                continue
            try:
                data = f.read_bytes()
            except OSError:
                continue  # файл заблокирован — пропустить
            if nb in data:
                return True
    except OSError:
        return False
    return False


# ── Конфиг ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=4), encoding="utf-8")


def get_providers(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config()
    provs = cfg.get("providers")
    if isinstance(provs, list) and provs:
        return provs
    # дефолт (§5 плана): GLM сейчас, DeepSeek/Qwen — потенциально
    return [
        {"id": "glm", "name": "GLM / z.ai", "url": "https://chat.z.ai",
         "login_url": "https://chat.z.ai/", "domains": ["chat.z.ai"],
         "detector": "cookie"},
        {"id": "deepseek", "name": "DeepSeek", "url": "https://chat.deepseek.com",
         "login_url": "https://chat.deepseek.com/",
         "domains": ["chat.deepseek.com"],
         "detector": "localstorage", "ls_hint": "Token"},
        {"id": "qwen", "name": "Qwen", "url": "https://chat.qwen.ai",
         "login_url": "https://chat.qwen.ai/", "domains": ["chat.qwen.ai"],
         "detector": "cookie"},
    ]


# ── Статус провайдеров (главный вход для дашборда) ───────────────────────

def _cookie_db_for(profile_path: Path, browser_id: str) -> Path:
    """Путь к cookie-БД профиля (Chromium: Network/Cookies; Firefox: cookies.sqlite)."""
    if browser_id == "firefox":
        return profile_path / "cookies.sqlite"
    return profile_path / "Network" / "Cookies"


def probe_providers(cfg: dict | None = None) -> dict:
    """Для каждого провайдера: найти профиль-кандидат и «вероятно вошёл».

    Provider поля детекции:
      detector: "cookie" (по умолчанию) | "localstorage"
      domains:  для cookie-детекта
      ls_hint:  подстрока в Local Storage (для detector=localstorage)
    checked=True — детект реально выполнялся (БД/файлы читались), даже если логина нет.
    """
    cfg = cfg or load_config()
    browsers = probe_browsers()
    # Изолированный профиль оркестратора — первым: там live-сессии (seed/open_login)
    orch = _orchestrator_as_browser(cfg)
    if orch:
        browsers = [orch] + browsers
    result = []
    for p in get_providers(cfg):
        entry = {
            "id": p["id"], "name": p.get("name", p["id"]),
            "url": p.get("url"), "login_url": p.get("login_url", p.get("url")),
            "detector": p.get("detector", "cookie"),
            "logged_in": False, "source": None, "checked": False,
        }
        domains = [d for d in (p.get("domains") or [p.get("url", "")]) if d]
        ls_hint = p.get("ls_hint")
        # приоритет: явный session_source → иначе интроспекция (§7.2)
        src = cfg.get("session_source") or {}
        forced = src.get("browser")
        ordered = ([b for b in browsers if b["id"] == forced] if forced else browsers)
        for b in ordered:
            for prof in b["profiles"]:
                ppath = Path(prof["path"])
                # localstorage-детект (Chromium-профили)
                if entry["detector"] == "localstorage":
                    if b["id"] == "firefox" or not ls_hint:
                        continue
                    entry["checked"] = True
                    if has_localstorage_hint(ppath, ls_hint):
                        entry.update(logged_in=True,
                                     source={"browser": b["id"], "profile": prof["name"]})
                        break
                    continue
                # cookie-детект
                db = _cookie_db_for(ppath, b["id"])
                if _cookie_db_copy(db) is None:
                    continue  # БД нет/заблокирована — профиль не проверен
                entry["checked"] = True
                if any(has_domain_cookie(db, d, b["id"]) for d in domains):
                    entry.update(logged_in=True,
                                 source={"browser": b["id"], "profile": prof["name"]})
                    break
            if entry["logged_in"]:
                break
        result.append(entry)
    return {
        "ts": datetime.now().isoformat(),
        "session_source": cfg.get("session_source"),
        "providers": result,
        "browsers_seen": [b["id"] for b in browsers],
    }


# ── «Открыть вход» ───────────────────────────────────────────────────────

def launch_cdp_chrome(cfg: dict | None = None) -> dict:
    """Start the orchestrator Chrome on CDP. Restore pinned tabs if the profile
    already has a session — do not append a URL (that duplicates tabs)."""
    cfg = cfg or load_config()
    chrome = next((b for b in probe_browsers() if b["id"] == "chrome" and b["exe"]), None)
    exe = chrome["exe"] if chrome else r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not Path(exe).exists():
        return {"ok": False, "error": "chrome.exe not found"}
    profile = cfg.get("chrome_profile_path") or str(HERE / "chrome-profile")
    port = int(cfg.get("cdp_port", 9222))
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return {"ok": True, "already": True, "cdp_port": port, "profile": profile}
    except OSError:
        pass
    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check",
           "--disable-blink-features=AutomationControlled"]
    restoring = profile_has_saved_session(profile)
    if not restoring:
        cmd.append("https://chat.z.ai/")
    proc = subprocess.Popen(cmd)
    return {"ok": True, "already": False, "pid": proc.pid, "cdp_port": port,
            "profile": profile, "restored_session": restoring}


def profile_has_saved_session(profile: str | Path) -> bool:
    """True if the orchestrator Chrome profile already has cookies (prior login)."""
    p = Path(profile)
    return (p / "Default" / "Network" / "Cookies").exists() or (
        p / "Default" / "Cookies"
    ).exists()


def open_login(provider_id: str, cfg: dict | None = None) -> dict:
    """Спавн Chrome с изолированным профилем оркестратора + login_url.
    Пользователь входит вручную; сессия остаётся в профиле оркестратора.
    If the profile already has a session, do NOT pass a URL — Chrome restores
    pinned tabs; a startup URL would open a duplicate."""
    cfg = cfg or load_config()
    prov = next((p for p in get_providers(cfg) if p["id"] == provider_id), None)
    if not prov:
        return {"ok": False, "error": f"unknown provider: {provider_id}"}
    chrome = next((b for b in probe_browsers() if b["id"] == "chrome" and b["exe"]), None)
    exe = chrome["exe"] if chrome else r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if not Path(exe).exists():
        return {"ok": False, "error": "chrome.exe not found"}
    profile = cfg.get("chrome_profile_path") or str(HERE / "chrome-profile")
    port = cfg.get("cdp_port", 9222)
    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
           "--no-first-run", "--no-default-browser-check",
           "--disable-blink-features=AutomationControlled"]
    login_url = prov.get("login_url") or prov.get("url", "")
    restoring = profile_has_saved_session(profile)
    if not restoring and login_url:
        cmd.append(login_url)
    proc = subprocess.Popen(cmd)
    return {"ok": True, "pid": proc.pid, "profile": profile, "cdp_port": port,
            "login_url": login_url, "restored_session": restoring}


def set_session_source(browser: str | None, profile: str | None) -> dict:
    """Зафиксировать источник сессий в конфиге (None,None → сброс в авто)."""
    cfg = load_config()
    cfg["session_source"] = (
        {"browser": browser, "profile": profile}
        if browser and profile else None)
    save_config(cfg)
    return {"ok": True, "session_source": cfg["session_source"]}


# ── Seed: перенос сессий из основного профиля в изолированный ────────────
# ПОЧЕМУ (диагностика 2026-09-02): Chrome 136+ запрещает CDP на стандартном
# user-data-dir, а занятый профиль молча игнорирует флаг. CDP возможен ТОЛЬКО
# на изолированном профиле оркестратора. Чтобы не перелогиниваться — seed.

def _readable_exclusive(path: Path) -> bool:
    """True, если файл можно открыть на чтение (не заблокирован браузером)."""
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def _copy_tree(src: Path, dst: Path) -> int:
    """Копировать каталог рекурсивно, пропуская заблокированные файлы."""
    n = 0
    for f in src.rglob("*"):
        if f.is_file():
            target = dst / f.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(f, target)
                n += 1
            except OSError:
                continue
    return n


def seed_orchestrator_profile(
    source_browser_id: str = "chrome",
    source_profile: str = "Default",
    include_cookies: bool = True,
    include_localstorage: bool = True,
    include_local_state: bool = True,
    cfg: dict | None = None,
) -> dict:
    """Перенести сессии из основного профиля в профиль оркестратора.

    Исходник только ЧИТАЕТСЯ (основной профиль не изменяется):
      Cookies (+wal/shm) → <orch>/Default/Network/Cookies
      Local State        → <orch>/Local State (ключи os_crypt)
      Local Storage      → <orch>/Default/Local Storage (leveldb-токены)

    Cookies заблокированы при работающем Chrome-источнике → тогда
    cookies_copied=False + cookies_requires_close=True («закрой Chrome»).
    Целевой Chrome (профиль оркестратора) тоже должен быть закрыт.
    """
    cfg = cfg or load_config()
    browsers = {b["id"]: b for b in probe_browsers()}
    src = browsers.get(source_browser_id)
    if not src:
        return {"ok": False, "error": f"browser not found: {source_browser_id}"}
    src_prof = next((p for p in src["profiles"] if p["name"] == source_profile), None)
    if not src_prof:
        return {"ok": False, "error": f"profile not found: {source_profile}"}
    src_path = Path(src_prof["path"])

    orch_udd = Path(cfg.get("chrome_profile_path") or (HERE / "chrome-profile"))
    orch_profile_dir = orch_udd / "Default"
    orch_profile_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "ok": True,
        "source": {"browser": source_browser_id, "profile": source_profile,
                   "path": str(src_path)},
        "target": {"udd": str(orch_udd), "profile": "Default"},
        "cookies_copied": False, "cookies_requires_close": False,
        "localstate_copied": False, "localstorage_files": 0,
        "warnings": [],
    }

    # 1) Cookies (эксклюзивно заблокированы работающим Chrome-источником)
    if include_cookies:
        cookie_db = src_path / "Network" / "Cookies"
        if not cookie_db.exists():
            cookie_db = src_path / "Cookies"
        if cookie_db.exists():
            if _readable_exclusive(cookie_db):
                dst_net = orch_profile_dir / "Network"
                dst_net.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(cookie_db, dst_net / "Cookies")
                    for ext in ("-wal", "-shm"):
                        s = Path(str(cookie_db) + ext)
                        if s.exists():
                            shutil.copy2(s, Path(str(dst_net / "Cookies")) + ext)
                    report["cookies_copied"] = True
                except OSError as e:
                    report["warnings"].append(f"cookies copy failed: {e}")
            else:
                report["cookies_requires_close"] = True
                report["warnings"].append(
                    "Cookies заблокированы — закрой основной Chrome и повтори seed")
        else:
            report["warnings"].append("cookie-БД не найдена в исходном профиле")

    # 2) Local State (ключи os_crypt — нужны для расшифровки скопированных куки)
    if include_local_state:
        ls_src = Path(src["user_data"]) / "Local State"
        if ls_src.exists():
            if _readable_exclusive(ls_src):
                try:
                    shutil.copy2(ls_src, orch_udd / "Local State")
                    report["localstate_copied"] = True
                except OSError as e:
                    report["warnings"].append(f"Local State copy failed: {e}")
            else:
                report["warnings"].append(
                    "Local State заблокирован (Chrome запущен) — скопированные "
                    "куки могут не расшифроваться без него")

    # 3) Local Storage (leveldb-токены; читаются даже при работающем Chrome)
    if include_localstorage:
        ls_src = src_path / "Local Storage" / "leveldb"
        if ls_src.exists():
            report["localstorage_files"] = _copy_tree(
                ls_src, orch_profile_dir / "Local Storage" / "leveldb")

    report["ok"] = bool(report["cookies_copied"] or report["localstorage_files"] > 0)
    return report


# ── Изолированный профиль как источник в probe ───────────────────────────

def _orchestrator_as_browser(cfg: dict) -> dict | None:
    """Профиль оркестратора как «браузер» для probe_providers: после seed/
    open_login дашборд видит сессии, живущие в изолированном профиле."""
    udd = Path(cfg.get("chrome_profile_path") or (HERE / "chrome-profile"))
    if not udd.exists():
        return None
    prof_dir = udd / "Default"
    if not prof_dir.exists():
        prof_dir = udd
    return {
        "id": "orchestrator",
        "name": "Orchestrator profile (isolated)",
        "exe": None,
        "user_data": str(udd),
        "profiles": [{"name": prof_dir.name, "path": str(prof_dir)}],
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true", help="статус провайдеров")
    ap.add_argument("--browsers", action="store_true", help="список браузеров")
    ap.add_argument("--open-login", help="открыть вход для провайдера (id)")
    ap.add_argument("--seed", action="store_true",
                    help="перенести сессии из основного профиля в профиль оркестратора")
    ap.add_argument("--seed-profile", default="Default",
                    help="исходный профиль для --seed (Default/Profile 1/…)")
    a = ap.parse_args()
    if a.browsers:
        print(json.dumps(probe_browsers(), ensure_ascii=False, indent=2))
    elif a.open_login:
        print(json.dumps(open_login(a.open_login), ensure_ascii=False, indent=2))
    elif a.seed:
        print(json.dumps(seed_orchestrator_profile(
            source_profile=a.seed_profile), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(probe_providers(), ensure_ascii=False, indent=2))