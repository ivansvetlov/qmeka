"""Tab reuse: do not spawn duplicates; ignore extension/promo windows."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from chat_driver import is_work_url, rank_existing_tab, url_matches_domains
from session_pool import profile_has_saved_session


def test_junk_urls_are_not_work_tabs():
    assert is_work_url("chrome-extension://abc/popup.html") is False
    assert is_work_url("chrome://omnibox-popup.top-chrome/") is False
    assert is_work_url("about:blank") is False
    assert is_work_url("devtools://devtools/bundled/inspector.html") is False
    assert is_work_url("") is False


def test_real_service_urls_are_work_tabs():
    assert is_work_url("https://chat.z.ai/") is True
    assert is_work_url("https://chat.deepseek.com/a/chat/s/abc") is True
    assert is_work_url("https://chat.qwen.ai/") is True


def test_domain_match():
    assert url_matches_domains("https://chat.z.ai/c/xyz", ["chat.z.ai"]) is True
    assert url_matches_domains("https://chat.qwen.ai/", ["chat.z.ai"]) is False


def test_rank_prefers_existing_chat_over_landing():
    landing = rank_existing_tab("https://chat.z.ai/")
    chat = rank_existing_tab("https://chat.z.ai/c/uuid-here")
    assert chat > landing
    ds = rank_existing_tab("https://chat.deepseek.com/a/chat/s/1316")
    assert ds > rank_existing_tab("https://chat.deepseek.com/")


def test_profile_with_cookies_counts_as_saved(tmp_path):
    assert profile_has_saved_session(tmp_path) is False
    cookies = tmp_path / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_bytes(b"x")
    assert profile_has_saved_session(tmp_path) is True
