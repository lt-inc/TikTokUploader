from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

GATE = threading.Lock()
log = logging.getLogger("tiktok.browser")


@contextmanager
def browser_permit(timeout: float = 10, *, guard=None, on_wait=None, label: str = "", poll: float = 0.5):
    """Serialize browsers. Ordinary callers fail after ``timeout`` seconds.

    A publishing caller passes ``guard`` and waits cooperatively instead: the
    guard (lease, session version, operation deadline, shutdown) runs between
    short polls and may raise to abandon the wait, so no fence is loosened and
    the prepared media is kept. ``on_wait`` runs once on first contention.
    """
    if guard is None:
        if not GATE.acquire(timeout=timeout):
            raise TimeoutError("Browser is in use by operator verification; retry later")
    else:
        guard()
        if not GATE.acquire(blocking=False):
            started = time.monotonic()
            log.info("browser busy; waiting %s", label)
            if on_wait is not None:
                on_wait()
            while not GATE.acquire(timeout=poll):
                guard()
            log.info("browser acquired after %.1fs %s", time.monotonic() - started, label)
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
