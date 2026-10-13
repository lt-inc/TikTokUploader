from __future__ import annotations

import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

from autotok.browsers import sync_playwright

from .browser import browser_permit, open_context


def response_post_ids(response: dict) -> set[str]:
    result: set[str] = set()
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("aweme_id", "item_id", "post_id", "awemeId", "itemId", "postId") and isinstance(child, (int, str)) and str(child).isdigit():
                    result.add(str(child))
                elif isinstance(child, (list, dict)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(response)
    return result


def matching_post(items: list[dict], row: dict, journal: dict) -> dict | None:
    """No match is an unknown outcome, never proof that retrying is safe."""
    matches = {}
    known = set(journal.get("post_ids", []))
    started = float(journal.get("submitted_at", 0))
    for item in items:
        pid = str(item.get("id", item.get("aweme_id", item.get("item_id", ""))))
        caption = item.get("desc", item.get("description", item.get("caption", "")))
        created = item.get("createTime", item.get("create_time", 0))
        try:
            created = float(created)
        except (TypeError, ValueError):
            continue
        if not pid.isdigit() or not caption or caption != journal.get("caption"):
            continue
        if not started - 120 <= created <= started + 900:
            continue
        if known and pid not in known:
            continue
        creation = item.get("creation_id", item.get("creationId"))
        project = item.get("project", {})
        if creation is None and isinstance(project, dict):
            creation = project.get("creation_id", project.get("creationId"))
        # Caption/time is merely a candidate search. A platform-returned post
        # ID or an exact creation-id link is required for automated confirmation.
        if not known and (not creation or str(creation) != str(journal.get("creation_id", ""))):
            continue
        author = item.get("author", {})
        owner = author.get("id", author.get("uid", author.get("user_id"))) if isinstance(author, dict) else None
        if owner is not None and str(owner) != row["remote_account_id"]:
            continue
        if item.get("is_draft") or item.get("draft") or item.get("is_scheduled"):
            continue
        matches[pid] = {"remote_id": pid, "remote_url": f"https://www.tiktok.com/@{quote(row['username'], safe='')}/video/{pid}",
                        "verified_at": datetime.now(timezone.utc).isoformat(), "proof": "authenticated_studio_item"}
    return next(iter(matches.values())) if len(matches) == 1 else None


def inspect_posts(row: dict, journal: dict, proxy: str | None, guard=lambda: None) -> dict | None:
    items: list[dict] = []
    with browser_permit(), sync_playwright() as pw:
        browser, context = open_context(pw, row, proxy)
        try:
            page = context.new_page()
            def response_received(response):
                # Restrict proofs to TikTok's authenticated creator-management
                # responses; public recommendations are not account evidence.
                url = response.url
                hostname = (urlparse(url).hostname or "").lower()
                if (hostname != "tiktok.com" and not hostname.endswith(".tiktok.com")) or not any(x in urlparse(url).path for x in ("creator", "manage", "content", "web/project")):
                    return
                try:
                    data = response.json()
                    def gather(node):
                        if isinstance(node, dict):
                            for key, value in node.items():
                                if key in ("item_list", "itemList", "aweme_list", "items", "posts") and isinstance(value, list):
                                    items.extend(x for x in value if isinstance(x, dict))
                                elif isinstance(value, (dict, list)):
                                    gather(value)
                        elif isinstance(node, list):
                            for x in node:
                                gather(x)
                    gather(data)
                except Exception:
                    pass
            page.on("response", response_received)
            guard()
            page.goto("https://www.tiktok.com/tiktokstudio/content", wait_until="domcontentloaded", timeout=30000)
            until = time.monotonic() + 15
            while time.monotonic() < until:
                guard()
                result = matching_post(items, row, journal)
                if result:
                    return result
                if "/login" in page.url:
                    return None
                page.wait_for_timeout(1000)
            return None
        finally:
            context.close()
            browser.close()
