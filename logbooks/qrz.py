"""Download a log from the QRZ.com Logbook API (needs a QRZ Logbook subscription).

``POST https://logbook.qrz.com/api`` with ``KEY`` (the logbook's API access key, QRZ Logbook >
Settings), ``ACTION=FETCH`` and ``OPTION=MAX:n,AFTERLOGID:id``. The response is name=value pairs
(``RESULT``, ``COUNT``, ``REASON``, ``ADIF``). QRZ asks for paging and an identifying User-Agent.
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, unquote_plus

import requests

from .adif import parse_adif
from .base import LogbookError
from .net import HEADERS

ENDPOINT = "https://logbook.qrz.com/api"
TIMEOUT = 120
PAGE = 250  # QRZ's recommended page size


class QrzError(LogbookError):
    pass


def _parse(text: str) -> dict[str, str]:
    """name=value response; ADIF (if any) may hold raw '&' and '=', so it is cut off first."""
    out: dict[str, str] = {}
    m = re.search(r"(?:^|&)ADIF=", text)
    if m:
        adif = text[m.end():]
        text = text[:m.start()]
        if "&lt;" in adif.lower():  # HTML-escaped
            adif = html.unescape(adif)
        elif "<" not in adif:  # URL-encoded
            adif = unquote_plus(adif)
        out["ADIF"] = adif  # anything after the last <eor> is ignored by the ADIF parser
    out.update({k.upper(): v for k, v in parse_qsl(text, keep_blank_values=True)})
    return out


def fetch(key: str, *, after: int = 0, page: int = PAGE, max_pages: int = 1000):
    """Yield ADIF chunks of the logbook from log id ``after`` on, ``page`` QSOs at a time."""
    for _ in range(max_pages):
        data = {"KEY": key, "ACTION": "FETCH", "OPTION": f"MAX:{page},AFTERLOGID:{after}"}
        try:
            resp = requests.post(ENDPOINT, data=data, headers=HEADERS, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise QrzError(f"could not reach the QRZ Logbook: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise QrzError(f"QRZ Logbook answered HTTP {resp.status_code}")
        body = _parse(resp.content.decode("utf-8", errors="replace"))
        result = body.get("RESULT", "").upper()
        adif = body.get("ADIF", "")
        if result == "FAIL" and body.get("COUNT") == "0" and not body.get("REASON"):
            return  # past the last page: QRZ answers "COUNT=0&RESULT=FAIL" to an empty fetch
        if result != "OK":
            reason = body.get("REASON") or result or "no RESULT in the response"
            if result == "AUTH":
                reason += " (the Logbook API needs a QRZ subscription and the logbook's API key)"
            raise QrzError(f"QRZ Logbook: {reason}")
        recs = list(parse_adif(adif))
        if recs:
            yield adif
        ids = [int(r["app_qrzlog_logid"]) for r in recs if r.get("app_qrzlog_logid", "").isdigit()]
        if len(recs) < page or not ids or max(ids) + 1 <= after:
            return
        after = max(ids) + 1


def normalize(rec: dict[str, str]) -> dict[str, str]:
    """QRZ marks its own confirmations with APP_QRZLOG_STATUS=C; expose that as confirmed_by 'qrz'."""
    rec = dict(rec)
    if rec.get("app_qrzlog_status", "").upper() == "C":
        rec["qrzcom_qso_download_status"] = "Y"
    return rec
