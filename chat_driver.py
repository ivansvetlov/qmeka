"""
Unified browser driver for z.ai / DeepSeek / Qwen / Google AI via Playwright.
Uses Playwright-managed Chromium with storage state per service.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

CONFIG_PATH = Path(__file__).parent / "config.json"
STORAGE_DIR = Path(__file__).parent / "storage"
CDP_PORT = 9222

# Overlay script — injects a badge into the page
_OVERLAY_JS = """
(function(name, color, details){
  if (document.getElementById('qmeka_badge')) return;
  var b = document.createElement('div');
  b.id = 'qmeka_badge';
  b.innerHTML = '<strong>⚡ Qmeka</strong> ' + name +
    (details ? '<br><span style="font-size:10px;opacity:.8">' + details + '</span>' : '');
  b.style.cssText = 'position:fixed;top:8px;left:8px;z-index:2147483647;' +
    'background:' + color + ';color:#fff;padding:6px 12px;border-radius:8px;' +
    'font:12px/1.4 sans-serif;box-shadow:0 2px 12px rgba(0,0,0,.5);pointer-events:none;' +
    'max-width:280px;word-break:break-word;';
  document.body.appendChild(b);
})("%s", "%s", "%s");
"""

# Lock overlay — blocks ALL input on the page
_LOCK_JS = """
(function(){
  if (document.getElementById('qmeka_lock')) return;
  var overlay = document.createElement('div');
  overlay.id = 'qmeka_lock';
  overlay.innerHTML = '<div style="text-align:center;color:#fff;font-family:sans-serif;">' +
    '<div style="font-size:24px;margin-bottom:8px;">🔒</div>' +
    '<div style="font-size:13px;font-weight:600;">Pipeline running</div>' +
    '<div style="font-size:10px;opacity:.7;margin-top:4px;">Input locked by Qmeka</div></div>';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483646;' +
    'background:rgba(0,0,0,0.3);backdrop-filter:blur(2px);' +
    'display:flex;align-items:center;justify-content:center;' +
    'pointer-events:auto;cursor:not-allowed;';
  document.body.appendChild(overlay);
  // Block keyboard
  document.addEventListener('keydown', function(e) {
    if (document.getElementById('qmeka_lock')) {
      e.stopPropagation();
      e.preventDefault();
    }
  }, true);
})();
"""

_UNLOCK_JS = """
(function(){
  var el = document.getElementById('qmeka_lock');
  if (el) el.remove();
})();
"""

# Service overlay configs: (name, color, details, extra_js_after_nav)
_SERVICE_OVERLAYS = {
    "zai": {
        "name": "z.ai",
        "color": "#6d28d9",
        "details": "GLM 5.3 Flash · reviewer",
    },
    "deepseek": {
        "name": "DeepSeek",
        "color": "#1a56db",
        "details": "DeepThink R1 · planner/judge",
        "extra_js": """
          (function(){
            var btn = document.querySelector('[class*="deepthink"], [data-mode="deepthink"]');
            if (btn) btn.click();
          })();
        """,
    },
    "qwen": {
        "name": "Qwen",
        "color": "#059669",
        "details": "Qwen-Max · planner",
        "extra_js": """
          (function(){
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
              if (btns[i].textContent.includes('Search') || btns[i].textContent.includes('Форс')) {
                btns[i].click(); break;
              }
            }
          })();
        """,
    },
    "google": {
        "name": "Google AI",
        "color": "#dc2626",
        "details": "Gemini · research",
        "extra_js": """
          (function(){
            var btn = document.querySelector('/html/body/div[2]/div[6]/form/div[1]/div/div[1]/div[2]/div[3]/button[1]');
            if (btn) btn.click();
          })();
        """,
    },
}


_JUNK_URL_PREFIXES = (
    "chrome://",
    "chrome-extension://",
    "devtools://",
    "about:",
    "edge://",
)


def is_work_url(url: str) -> bool:
    """Real http(s) tab — not extension popups, NTP, or Chrome UI."""
    u = (url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    return not u.startswith(_JUNK_URL_PREFIXES)


def url_matches_domains(url: str, domains: list) -> bool:
    if not url or not domains:
        return False
    return any(d and d in url for d in domains)


def rank_existing_tab(url: str) -> tuple:
    """Higher is better. Prefer already-open chats over landing / stray search."""
    u = url or ""
    chat = 1 if any(x in u for x in ("/c/", "/a/chat", "/chat/", "udm=50")) else 0
    return (chat, len(u))


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class ChatDriver:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.config = load_config()
        # Support both "services" and "providers" keys in config
        services_list = self.config.get("services", []) or self.config.get("providers", [])
        self.services = {}
        for s in services_list:
            # Map provider IDs to service names
            name = s.get("name", s.get("id", ""))
            sid = s.get("id", "")
            if sid == "glm":
                name = "zai"
            elif sid:
                name = sid
            self.services[name] = s
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, Page] = {}
        self._cdp_connected = False
        STORAGE_DIR.mkdir(exist_ok=True)

    async def start(self):
        self._playwright = await async_playwright().start()
        # Connect to existing Chrome via CDP (port 9222) instead of launching own browser
        cdp_url = f"http://127.0.0.1:{CDP_PORT}"
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
            self._cdp_connected = True
            print(f"[chat_driver] Connected to Chrome via CDP at {cdp_url}")
            # Inject overlays into ALL existing tabs
            await self._inject_existing_tabs()
        except Exception as e:
            print(f"[chat_driver] CDP connect failed ({e}), launching own Chromium")
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._cdp_connected = False

    async def stop(self):
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser and not self._cdp_connected:
            # Own Playwright Chromium — we spawned it, we close it.
            # CDP Chrome is the user's profile window: leave it running.
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._pages.clear()

    async def _is_popup(self, page: Page) -> bool:
        """Extension popups, z.ai promo windows, tiny extra Chrome windows."""
        try:
            opener = page.opener
            if callable(opener):
                opener = await opener()
            if opener is not None:
                return True
        except Exception:
            pass
        try:
            dims = await page.evaluate(
                "() => ({w: window.outerWidth || 0, h: window.outerHeight || 0})"
            )
            if dims and (dims.get("w", 0) < 520 or dims.get("h", 0) < 420):
                return True
        except Exception:
            pass
        return False

    async def _find_existing_tab(self, service_name: str) -> Optional[Page]:
        """Reuse an already-open (often pinned) tab. Never create one here."""
        if not (self._cdp_connected and self._browser and self._browser.contexts):
            return None
        svc = self.services.get(service_name, {})
        domains = svc.get("domains", [])
        scored = []
        for ctx in self._browser.contexts:
            for page in ctx.pages:
                if page.is_closed():
                    continue
                url = page.url or ""
                if not is_work_url(url) or not url_matches_domains(url, domains):
                    continue
                if await self._is_popup(page):
                    print(f"[chat_driver] skip popup for {service_name}: {url[:70]}")
                    continue
                scored.append((rank_existing_tab(url), page))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    async def get_page(self, service_name: str) -> Page:
        # Return cached page if still open
        if service_name in self._pages:
            page = self._pages[service_name]
            if not page.is_closed():
                return page

        existing = await self._find_existing_tab(service_name)
        if existing:
            self._pages[service_name] = existing
            print(f"[chat_driver] Reusing tab: {service_name} ({existing.url[:70]})")
            return existing

        # CDP: no matching tab — only now create one
        if self._cdp_connected and self._browser.contexts:
            ctx = self._browser.contexts[0]
            print(f"[chat_driver] No existing tab for {service_name}, creating one")
            page = await ctx.new_page()
            self._pages[service_name] = page
            return page

        # Launched browser: create new context with storage state
        svc = self.services.get(service_name)
        if not svc:
            raise ValueError(f"Unknown service: {service_name}")
        storage_path = STORAGE_DIR / f"{service_name}_state.json"
        ctx_args = {}
        if storage_path.exists():
            ctx_args["storage_state"] = str(storage_path)
        ctx = await self._browser.new_context(**ctx_args)
        page = await ctx.new_page()
        self._contexts[service_name] = ctx
        self._pages[service_name] = page
        return page

    async def save_state(self, service_name: str):
        ctx = self._contexts.get(service_name)
        if ctx:
            storage_path = STORAGE_DIR / f"{service_name}_state.json"
            await ctx.storage_state(path=str(storage_path))

    async def navigate(self, service_name: str, url: str = None):
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        target = url or svc.get("url", "")
        await page.goto(target, wait_until="domcontentloaded", timeout=60000)
        # Skip networkidle for CDP-connected Chrome (ws connections keep it open)
        if not self._cdp_connected:
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
        await asyncio.sleep(2)  # Brief wait for JS to settle
        # Inject overlay badge
        await self._inject_overlay(page, service_name)
        return page

    async def _inject_overlay(self, page: Page, service_name: str):
        """Inject Qmeka badge overlay into the page."""
        cfg = _SERVICE_OVERLAYS.get(service_name)
        if not cfg:
            return
        js = _OVERLAY_JS % (cfg["name"], cfg["color"], cfg["details"])
        try:
            await page.evaluate(js)
        except Exception:
            pass
        # Run extra JS if defined (e.g. switch to DeepThink mode)
        extra = cfg.get("extra_js")
        if extra:
            try:
                await page.evaluate(extra)
            except Exception:
                pass

    async def lock_page(self, service_name: str):
        """Lock a page — block all user input during pipeline execution."""
        page = await self.get_page(service_name)
        try:
            await page.evaluate(_LOCK_JS)
            print(f"[chat_driver] Locked: {service_name}")
        except Exception:
            pass

    async def unlock_page(self, service_name: str):
        """Unlock a page — restore user input."""
        page = await self.get_page(service_name)
        try:
            await page.evaluate(_UNLOCK_JS)
            print(f"[chat_driver] Unlocked: {service_name}")
        except Exception:
            pass

    async def lock_all(self):
        """Lock all pinned pages."""
        for name in self._pages:
            await self.lock_page(name)

    async def unlock_all(self):
        """Unlock all pinned pages."""
        for name in self._pages:
            await self.unlock_page(name)

    def _service_for_url(self, url: str) -> Optional[str]:
        if not is_work_url(url):
            return None
        for name, svc in self.services.items():
            if url_matches_domains(url, svc.get("domains", [])):
                return name
        return None

    async def _inject_existing_tabs(self):
        """Badges on real service tabs only. Skip extension popups and promo windows."""
        if not self._cdp_connected or not self._browser.contexts:
            return
        injected = 0
        seen = 0
        for ctx in self._browser.contexts:
            for page in ctx.pages:
                seen += 1
                try:
                    url = page.url or ""
                    service = self._service_for_url(url)
                    if not service:
                        continue
                    if await self._is_popup(page):
                        print(f"[chat_driver] skip overlay on popup: {url[:70]}")
                        continue
                    await self._inject_overlay(page, service)
                    injected += 1
                    print(f"[chat_driver] Injected overlay on: {url[:60]}")
                except Exception:
                    pass
        print(f"[chat_driver] Injected overlays on {injected}/{seen} tabs")

    async def pre_open_tabs(self, service_names: list[str]):
        """Reuse pinned/existing tabs. Create a tab only if that service has none."""
        for name in service_names:
            if name in self._pages and not self._pages[name].is_closed():
                print(f"[chat_driver] Tab already pinned: {name}")
                continue
            svc = self.services.get(name)
            if not svc:
                print(f"[chat_driver] Unknown service: {name}, skipping")
                continue
            existing = await self._find_existing_tab(name)
            if existing:
                self._pages[name] = existing
                await self._inject_overlay(existing, name)
                print(f"[chat_driver] Reusing tab: {name} ({existing.url[:60]})")
                continue
            page = await self.get_page(name)
            await self.navigate(name)
            print(f"[chat_driver] Opened tab: {name} -> {page.url[:60]}")

    async def new_chat(self, service_name: str):
        """Start a new chat session on a service (click 'New Chat' button)."""
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        selector = svc.get("new_chat_selector")
        if not selector:
            print(f"[chat_driver] No new_chat_selector for {service_name}")
            return
        try:
            btn = await page.query_selector(selector)
            if btn:
                await btn.click()
                await asyncio.sleep(1)
                print(f"[chat_driver] New chat started on {service_name}")
            else:
                print(f"[chat_driver] New Chat button not found for {service_name}")
        except Exception as e:
            print(f"[chat_driver] new_chat error on {service_name}: {e}")

    async def check_login(self, service_name: str) -> bool:
        """Check if a service is logged in, based on its login_check indicator."""
        svc = self.services.get(service_name, {})
        check = svc.get("login_check", {})
        method = check.get("method", "")
        name = check.get("name", "")
        if not method or not name:
            return True  # No check configured — assume available

        # Find matching tab
        page = None
        if service_name in self._pages and not self._pages[service_name].is_closed():
            page = self._pages[service_name]
        elif self._browser and self._browser.contexts:
            ctx = self._browser.contexts[0]
            domains = svc.get("domains", [])
            for p in ctx.pages:
                for d in domains:
                    if d in p.url:
                        page = p
                        break
                if page:
                    break
        if not page:
            return False

        try:
            if method == "cookie":
                cookies = await self._browser.contexts[0].cookies(page.url)
                for c in cookies:
                    if c["name"] == name and c.get("value"):
                        return True
                return False
            elif method == "localstorage":
                val = await page.evaluate(
                    "k => localStorage.getItem(k)", name
                )
                return bool(val and val not in ("null", "{}", '{"value":null}'))
        except Exception:
            return False
        return False

    async def readiness(self) -> dict:
        """Full readiness check: CDP + login state for every service."""
        result = {
            "cdp": self._cdp_connected,
            "cdp_port": CDP_PORT,
            "services": {},
        }
        for name, svc in self.services.items():
            logged_in = await self.check_login(name)
            result["services"][name] = {
                "name": svc.get("name", name),
                "logged_in": logged_in,
                "url": svc.get("url", ""),
                "login_url": svc.get("login_url", ""),
            }
        return result

    async def login_interactive(self, service_name: str):
        """Open login page, wait for user to log in manually, then save state."""
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        await page.goto(svc.get("url", ""), wait_until="domcontentloaded")
        print(f"Please log in to {service_name} in the browser window.")
        print("Press Enter here when done...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await self.save_state(service_name)
        print(f"Login state saved for {service_name}")

    async def type_prompt(self, service_name: str, text: str):
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        selector = svc.get("input_selector", "textarea")
        await page.wait_for_selector(selector, timeout=15000)
        el = page.locator(selector).first
        await el.click()
        await el.fill(text)

    async def attach_docx(self, service_name: str, file_path: str):
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        attach_selector = svc.get("attach_selector", 'input[type="file"]')
        file_input = page.locator(attach_selector).first
        await file_input.set_input_files(file_path)

    async def submit(self, service_name: str):
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        send_selector = svc.get("send_selector", "#send-message-button")
        try:
            btn = page.locator(send_selector).first
            await btn.wait_for(state="visible", timeout=5000)
            await btn.click()
        except Exception:
            await page.keyboard.press("Enter")

    async def wait_for_response(self, service_name: str, timeout: int = 300):
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        response_selector = svc.get("response_selector", ".message-content")
        start = time.time()
        last_text = ""
        stable_count = 0
        while time.time() - start < timeout:
            await asyncio.sleep(3)
            try:
                msgs = page.locator(response_selector).all()
                if msgs:
                    current_text = msgs[-1].inner_text()
                    if current_text and current_text == last_text:
                        stable_count += 1
                        if stable_count >= 3:
                            return current_text
                    else:
                        stable_count = 0
                        last_text = current_text
            except Exception:
                pass
        return last_text or "[timeout]"

    async def read_last_response(self, service_name: str) -> str:
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        response_selector = svc.get("response_selector", ".message-content")
        try:
            msgs = page.locator(response_selector).all()
            if msgs:
                return msgs[-1].inner_text()
        except Exception:
            pass
        return ""

    async def run_prompt(
        self,
        service_name: str,
        prompt: str,
        docx_path: str = None,
        timeout: int = 300,
    ) -> str:
        page = await self.get_page(service_name)
        svc = self.services.get(service_name, {})
        url = svc.get("url")
        if page.url != url and url not in page.url:
            await self.navigate(service_name)
        await self.type_prompt(service_name, prompt)
        if docx_path:
            await self.attach_docx(service_name, docx_path)
        await asyncio.sleep(1)
        await self.submit(service_name)
        response = await self.wait_for_response(service_name, timeout)
        return response
