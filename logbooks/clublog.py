"""Download a log from Club Log (REST API).

``POST https://clublog.org/api/v1/logs/{call}/adif-export`` with ``Authorization: Bearer <token>``
and a JSON body (``{}`` = the whole log) returns the log as ADIF. Each user makes their own token
in Club Log under Settings > API; it acts as that account and only sees its own callsigns.
Limits: 500 general requests per day per account (one nightly export is fine).
"""
from __future__ import annotations

import requests

from .base import LogbookError
from .net import HEADERS

BASE = "https://clublog.org/api/v1/logs"
TIMEOUT = 600


class ClublogError(LogbookError):
    pass


def fetch(callsign: str, token: str) -> str:
    """Return the whole Club Log log of ``callsign`` as ADIF text."""
    url = f"{BASE}/{requests.utils.quote(callsign.strip().upper(), safe='/')}/adif-export"
    headers = {**HEADERS, "Authorization": f"Bearer {token}", "Accept": "*/*"}
    try:
        resp = requests.post(url, json={}, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ClublogError(f"could not reach Club Log: {type(exc).__name__}") from None
    if resp.status_code != 200:
        reason = ""
        try:
            err = resp.json().get("error") or {}
            reason = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
        except (ValueError, AttributeError):
            pass
        hint = {401: " - check CLUBLOG_TOKEN (a REST token from Club Log > Settings > API)",
                403: f" - {callsign} is not a log on the token's account",
                429: " - rate limited, try again later"}.get(resp.status_code, "")
        raise ClublogError(f"Club Log answered HTTP {resp.status_code}"
                           + (f" ({reason})" if reason else "") + hint)
    return resp.content.decode("utf-8", errors="replace")
