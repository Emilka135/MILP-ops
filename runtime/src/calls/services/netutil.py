"""Small helpers shared by the HTTP clients: scheme-checked ``urlopen`` (bandit B310) and JSON POST."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

ALLOWED_SCHEMES = ("http://", "https://")


def open_http(req: urllib.request.Request | str, timeout: float = 10.0) -> Any:
    """``urllib.request.urlopen`` restricted to http(s): module URLs come from the environment,
    so ``file:`` / custom schemes must never be reachable through them."""
    url = req if isinstance(req, str) else req.full_url
    if not url.startswith(ALLOWED_SCHEMES):
        raise ValueError(f"refusing non-http URL {url!r}")
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310  # nosec B310  # scheme validated above


def post_json(url: str, payload: dict, timeout: float = 10.0, headers: dict[str, str] | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    with open_http(req, timeout=timeout) as resp:
        return json.loads(resp.read())
