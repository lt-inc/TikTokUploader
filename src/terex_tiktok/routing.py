"""One account route for identity, browser, upload and post reconciliation."""
from __future__ import annotations

from autotok.proxy import parse_proxy

from .config import Config


def normalized_proxy(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("A valid proxy address is required")
    try:
        proxy = parse_proxy(value)
        if proxy is None:
            raise ValueError
        # Both upload transports and browser verification must support the route.
        proxy.for_playwright()
        return proxy.url
    except ValueError:
        raise ValueError("Invalid proxy address or unsupported browser proxy authentication") from None


def proxy_for(row: dict, config: Config) -> str | None:
    mode = row.get("proxy_mode", "default")
    if mode == "direct":
        return None
    if mode == "custom":
        return normalized_proxy(row.get("proxy_url"))
    if mode == "default":
        return config.proxy_url
    raise ValueError("Invalid saved proxy mode")


def proxy_public(row: dict) -> dict:
    saved = row.get("proxy_url")
    display = ""
    if saved:
        proxy = parse_proxy(saved)
        host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
        display = f"{proxy.scheme}://{host}:{proxy.port}"
    return {"proxy_mode": row.get("proxy_mode", "default"),
            "proxy_display": display, "custom_proxy_configured": bool(saved)}
