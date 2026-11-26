"""Download contacts from World Radio League (WRL) via its REST API.

``GET https://api.worldradioleague.com/v1/contacts`` with ``Authorization: Bearer wrl_live_...``
(WRL > Integrations > Developer API). JSON, newest first, up to 100 per page; pass
``meta.nextCursor`` back as ``cursor`` until it is null. Limits: 120 reads/minute, 20,000/day.

WRL contacts carry no QSL / LoTW confirmation status, so this source gives worked entities only.
``dxcc`` is filled in by WRL shortly after a contact is created; until then the entity comes from
the callsign.

Writing is ``POST /v1/contacts`` with one JSON contact. Required: ``programId``, ``call``,
``timestamp``, ``freq`` (MHz), ``band`` and ``mode``; unknown fields are refused. An already logged
QSO answers ``409 DUPLICATE_CONTACT``, which counts as logged. 60 writes per minute.
"""
from __future__ import annotations

import re
import time
from typing import Iterator

import requests

from .base import LogbookError, WriteResult
from .net import HEADERS

ENDPOINT = "https://api.worldradioleague.com/v1/contacts"
TIMEOUT = 60
PAGE = 100
WRITE_TIMEOUT = 15
PROGRAM_ID = "HamLookup"
_GRID = re.compile(r"^[A-Ra-r]{2}(?:[0-9]{2}(?:[A-Xa-x]{2}(?:[0-9]{2})?)?)?$")

HAS_CONFIRMATIONS = False


class WrlError(LogbookError):
    pass


def _band(metres) -> str:
    if metres in (None, ""):
        return ""
    m = float(metres)
    if m >= 1:
        return f"{m:g}m"
    return f"{m * 100:g}cm"


def to_record(c: dict) -> dict[str, str]:
    """A WRL contact as an ADIF-style record (lower-case field names, string values)."""
    ts = c.get("timestamp") or ""
    rec = {
        "call": c.get("call") or "",
        "station_callsign": c.get("stationCallsign") or "",
        "qso_date": ts[:10].replace("-", ""),
        "time_on": ts[11:16].replace(":", ""),
        "band": _band(c.get("band")),
        "mode": c.get("mode") or "",
        "gridsquare": c.get("gridsquare") or "",
    }
    if c.get("dxcc") is not None:
        rec["dxcc"] = str(c["dxcc"])
    return {k: v for k, v in rec.items() if v}


def fetch(key: str, *, page: int = PAGE, max_pages: int = 2000) -> Iterator[dict[str, str]]:
    """Yield every contact (duplicates WRL flagged are skipped) as an ADIF-style record."""
    headers = {**HEADERS, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    cursor = None
    for _ in range(max_pages):
        params = {"limit": page}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(3):
            try:
                resp = requests.get(ENDPOINT, params=params, headers=headers, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise WrlError(f"could not reach World Radio League: {type(exc).__name__}") from None
            if resp.status_code != 429 or attempt == 2:
                break
            wait = int(resp.headers.get("Retry-After") or 60)
            if wait > 300:  # the daily budget is spent; don't sit in a background refresh for hours
                break
            time.sleep(wait)
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200 or not isinstance(body, dict):
            err = (body or {}).get("error") if isinstance(body, dict) else None
            reason = f"{err.get('code')}: {err.get('message')}" if isinstance(err, dict) else ""
            raise WrlError(f"World Radio League answered HTTP {resp.status_code}"
                           + (f" ({reason})" if reason else ""))
        for c in body.get("data") or []:
            if not c.get("isDuplicate"):
                yield to_record(c)
        cursor = (body.get("meta") or {}).get("nextCursor")
        if not cursor:
            return


def to_contact(record: dict[str, str], *, my_grid: str = "", station_callsign: str = "",
               logbook_id: str = "") -> dict:
    """An ADIF-style record as a WRL ``ContactCreate`` (only the fields WRL accepts)."""
    freq = record.get("freq", "")
    try:
        freq_mhz = float(freq)
    except ValueError:
        raise WrlError("World Radio League needs a frequency - fill in Freq (MHz)") from None
    c = {
        "programId": PROGRAM_ID,
        "call": record.get("call", ""),
        "timestamp": {"qsoDate": record.get("qso_date", ""), "timeOn": record.get("time_on", "")},
        "freq": freq_mhz,
        "band": record.get("band", ""),
        "mode": record.get("submode") or record.get("mode", ""),
        "rstSent": record.get("rst_sent", ""),
        "rstRcvd": record.get("rst_rcvd", ""),
        "txPwr": record.get("tx_pwr", ""),
        "notes": " / ".join(v for v in (record.get("comment", ""), record.get("notes", "")) if v)[:2000],
        "name": record.get("name", "")[:255],
        "qth": record.get("qth", "")[:100],
        "state": record.get("state", "")[:50],
        "stationCallsign": station_callsign,
        "logbookId": logbook_id,
    }
    for key, grid in (("gridsquare", record.get("gridsquare", "")), ("myGridsquare", my_grid)):
        if _GRID.match(grid):
            c[key] = grid
    return {k: v for k, v in c.items() if v not in ("", None)}


def insert(key: str, record: dict[str, str], **contact_opts) -> WriteResult:
    """Log one contact."""
    try:
        contact = to_contact(record, **contact_opts)
    except WrlError as exc:
        return WriteResult(False, str(exc))
    headers = {**HEADERS, "Authorization": f"Bearer {key}", "Accept": "application/json"}
    try:
        resp = requests.post(ENDPOINT, json=contact, headers=headers, timeout=WRITE_TIMEOUT)
    except requests.RequestException as exc:
        return WriteResult(False, f"could not reach World Radio League: {type(exc).__name__}")
    if resp.status_code in (200, 201):
        return WriteResult(True)
    try:
        err = (resp.json() or {}).get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    code = err.get("code", "") if isinstance(err, dict) else ""
    if code == "DUPLICATE_CONTACT":
        return WriteResult(True, "already in the log", duplicate=True)
    msg = f"{code}: {err.get('message', '')}".strip(": ") if code else f"HTTP {resp.status_code}"
    if isinstance(err, dict) and err.get("hint"):
        msg += f" ({err['hint']})"
    return WriteResult(False, msg)
