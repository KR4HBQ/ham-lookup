"""Wavelog (and Cloudlog) API client.

Read (Wavelog only, from wavelog-flagmap): ``POST /index.php/api/get_contacts_adif`` with JSON
``{key, station_id, fetchfromid}``; the response is ``{status, lastfetchedid, exported_qsos, adif}``
where ``adif`` is null when nothing new is available. Wavelog returns at most 20000 QSOs per call,
so we page with ``fetchfromid``. Cloudlog has no such export call.

Write (Wavelog and Cloudlog; Wavelog is a Cloudlog fork and kept the call): ``POST
/index.php/api/qso`` with JSON ``{key, station_profile_id, type: "adif", string}``. The answer is
``{status: "created", adif_count, adif_errors, messages}``; a rejected record (a duplicate, for
example) shows up as ``adif_errors`` > 0 with the reason in ``messages``.
"""
from __future__ import annotations

from typing import Iterator

import requests

from .adif import to_adif
from .base import LogbookError, WriteResult
from .net import HEADERS

TIMEOUT = 60
WRITE_TIMEOUT = 15


def _base(url: str) -> str:
    url = url.rstrip("/")
    return url[: -len("/index.php")] if url.endswith("/index.php") else url


def _body(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return None


def _reason(body) -> str:
    if not isinstance(body, dict):
        return ""
    msgs = body.get("messages")
    if isinstance(msgs, list) and msgs:
        return "; ".join(str(m) for m in msgs)
    return str(body.get("reason") or body.get("message") or "")


def fetch(url: str, key: str, station_id: str | int, *, max_pages: int = 1000,
          label: str = "Wavelog") -> Iterator[str]:
    """Yield ADIF text chunks of the station location's whole log."""
    endpoint = f"{_base(url)}/index.php/api/get_contacts_adif"
    last_id = 0
    for _ in range(max_pages):
        try:
            resp = requests.post(endpoint, json={"key": key, "station_id": str(station_id),
                                                 "fetchfromid": last_id},
                                 headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise LogbookError(f"could not reach {label}: {type(exc).__name__}") from None
        body = _body(resp)
        if resp.status_code != 200 or not isinstance(body, dict):
            if resp.status_code == 200:
                raise LogbookError(f"{label} did not return JSON - check WAVELOG_URL")
            reason = _reason(body)
            raise LogbookError(f"{label} answered HTTP {resp.status_code}" + (f" ({reason})" if reason else ""))
        if body.get("status") not in (None, "successful"):
            raise LogbookError(f"{label} reported failure: {_reason(body) or body.get('status')}")
        adif = body.get("adif") or ""
        new_last = int(body.get("lastfetchedid") or last_id)
        if adif.strip():
            yield adif
        if new_last <= last_id or not adif.strip():
            return
        last_id = new_last


def insert(url: str, key: str, station_id: str | int, record: dict[str, str], *,
           label: str = "Wavelog") -> WriteResult:
    """Log one QSO through ``/api/qso``."""
    endpoint = f"{_base(url)}/index.php/api/qso"
    payload = {"key": key, "station_profile_id": str(station_id), "type": "adif",
               "string": to_adif(record)}
    try:
        resp = requests.post(endpoint, json=payload, headers={**HEADERS, "Accept": "application/json"},
                             timeout=WRITE_TIMEOUT)
    except requests.RequestException as exc:
        return WriteResult(False, f"could not reach {label}: {type(exc).__name__}")
    body = _body(resp)
    reason = _reason(body)
    errors = 0
    if isinstance(body, dict):
        try:
            errors = int(body.get("adif_errors") or 0)
        except (TypeError, ValueError):
            errors = 0
    failed = (resp.status_code >= 300 or errors > 0
              or (isinstance(body, dict) and body.get("status") in ("failed", "abort")))
    if not failed:
        return WriteResult(True)
    if "duplicate" in reason.lower():
        return WriteResult(True, "already in the log", duplicate=True)
    if resp.status_code >= 300:
        return WriteResult(False, f"HTTP {resp.status_code}" + (f" ({reason})" if reason else ""))
    return WriteResult(False, reason or "the QSO was rejected")
