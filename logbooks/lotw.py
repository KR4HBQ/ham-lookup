"""Download your QSOs from ARRL Logbook of The World (LoTW).

``GET https://lotw.arrl.org/lotwuser/lotwreport.adi`` with ``login``, ``password`` and
``qso_query=1``. ``qso_qsl=no`` returns every QSO you uploaded (not just confirmed ones);
``qso_qsldetail=yes`` adds DXCC / COUNTRY to confirmed records. Unconfirmed records carry no
DXCC field, so their entity comes from the callsign. LoTW answers an HTML page instead of
ADIF when the login fails.

LoTW only accepts the password as a URL parameter, so the request URL is never shown in
errors or logs.
"""
from __future__ import annotations

import requests

from .base import LogbookError
from .net import HEADERS

ENDPOINT = "https://lotw.arrl.org/lotwuser/lotwreport.adi"
TIMEOUT = 900  # LoTW builds the whole report before answering: ~3 min for 5k QSOs


class LotwError(LogbookError):
    pass


def fetch(username: str, password: str, *, owncall: str | None = None) -> str:
    """Return the ADIF report of all uploaded QSOs (confirmed ones marked QSL_RCVD=Y)."""
    params = {"login": username, "password": password, "qso_query": "1", "qso_qsl": "no",
              "qso_qsorxsince": "1900-01-01", "qso_qsldetail": "yes"}
    if owncall:
        params["qso_owncall"] = owncall
    try:
        resp = requests.get(ENDPOINT, params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as exc:  # its message would contain the URL, password included
        raise LotwError(f"could not reach LoTW: {type(exc).__name__}") from None
    if resp.status_code != 200:
        raise LotwError(f"LoTW answered HTTP {resp.status_code}")
    text = resp.content.decode("utf-8", errors="replace")
    if "<eoh>" not in text.lower():
        raise LotwError("LoTW did not return ADIF - check the username and password")
    return text


def normalize(rec: dict[str, str]) -> dict[str, str]:
    """LoTW reports its own confirmation as QSL_RCVD; store it where confirmed_by 'lotw' looks."""
    rec = dict(rec)
    rec["lotw_qsl_rcvd"] = rec.pop("qsl_rcvd", "")
    rec.pop("qslrdate", None)
    return rec
