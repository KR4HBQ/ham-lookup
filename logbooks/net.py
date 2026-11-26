"""Shared HTTP settings for the logbook clients.

Every request identifies Ham Lookup (some services rate-limit generic agents). app.py sets the
final User-Agent (version plus the optional CONTACT_INFO) with ``set_user_agent``.
"""
from __future__ import annotations

import requests

PROJECT_URL = "https://github.com/KR4HBQ/ham-lookup"
HEADERS = {"User-Agent": f"HamLookup (+{PROJECT_URL})"}
TIMEOUT = 60


class FetchError(RuntimeError):
    pass


def set_user_agent(user_agent: str) -> None:
    HEADERS["User-Agent"] = user_agent


def fetch_text(url: str) -> str:
    """GET a text document (an ADIF file published at a URL)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FetchError(f"could not download {url}: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code} from {url}")
    return resp.content.decode("utf-8", errors="replace")
