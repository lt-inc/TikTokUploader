from __future__ import annotations

import threading
from contextlib import contextmanager

GATE = threading.Lock()


@contextmanager
def browser_permit(timeout: float = 10):
    if not GATE.acquire(timeout=timeout):
        raise TimeoutError("Browser is in use by operator verification; retry later")
    try:
        yield
    finally:
        GATE.release()


def open_context(pw, row: dict, proxy: str | None, headless=True):
    kwargs = {"headless": headless}
    if proxy:
        from autotok.proxy import parse_proxy
        kwargs["proxy"] = parse_proxy(proxy).for_playwright()
    browser = pw.chromium.launch(**kwargs)
    context = browser.new_context(user_agent=row.get("pending_user_agent", row.get("user_agent")), locale="en-US")
    context.add_cookies(row.get("pending_cookies", row.get("cookies", [])))
    return browser, context
