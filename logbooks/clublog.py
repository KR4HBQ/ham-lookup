"""Download a log from Club Log (REST API).

``POST https://clublog.org/api/v1/logs/{call}/adif-export`` with ``Authorization: Bearer <token>``
and a JSON body (``{}`` = the whole log) returns the log as ADIF. Each user makes their own token
in Club Log under Settings > API; it acts as that account and only sees its own callsigns.
Limits: 500 general requests per day per account (one nightly export is fine).

Writing (one QSO as it is logged) is ``POST https://clublog.org/realtime.php`` with ``email``,
``password`` (an Application Password from Club Log > Settings, not the login password),
``callsign``, ``adif`` (one record) and ``api``: an application API key that Club Log issues per
application. Club Log revokes any API key it finds published, so each self-hoster requests their
own and keeps it in .env (the same CLUBLOG_API_KEY the DXCC lookup uses). Club Log firewalls clients
that keep sending failed logins, so after a login failure this module stops writing until restart.
"""
from __future__ import annotations

import requests

from .adif import to_adif
from .base import LogbookError, WriteResult
from .net import HEADERS

BASE = "https://clublog.org/api/v1/logs"
REALTIME = "https://clublog.org/realtime.php"
TIMEOUT = 600
WRITE_TIMEOUT = 15

_login_failed = ""  # set after a 403 from realtime.php: don't retry until the app restarts


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


def insert(email: str, password: str, callsign: str, api_key: str, record: dict[str, str]) -> WriteResult:
    """Upload one QSO in real time."""
    global _login_failed
    if _login_failed:
        return WriteResult(False, _login_failed + " (not retried until Ham Lookup restarts)")
    data = {"email": email, "password": password, "callsign": callsign.strip().upper(),
            "adif": to_adif(record), "api": api_key}
    try:
        resp = requests.post(REALTIME, data=data, headers=HEADERS, timeout=WRITE_TIMEOUT)
    except requests.RequestException as exc:
        return WriteResult(False, f"could not reach Club Log: {type(exc).__name__}")
    text = resp.content.decode("utf-8", errors="replace").strip()
    if resp.status_code == 200:
        if "dupe" in text.lower() or "duplicate" in text.lower():
            return WriteResult(True, "already in the log", duplicate=True)
        return WriteResult(True, "" if text.upper().startswith("OK") else text[:200])
    if resp.status_code == 403:
        _login_failed = ("login refused - check CLUBLOG_EMAIL, CLUBLOG_APP_PASSWORD (an Application "
                         "Password), CLUBLOG_CALLSIGN and CLUBLOG_API_KEY")
        return WriteResult(False, _login_failed)
    return WriteResult(False, f"HTTP {resp.status_code}" + (f" ({text[:200]})" if text else ""))
