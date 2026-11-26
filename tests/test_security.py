"""Keys and passwords never reach the browser or the logs; requests from other sites can't log QSOs."""
import logging

import requests

import app as hamapp


def test_safe_err_removes_query_strings():
    e = requests.ConnectionError("HTTPSConnectionPool(host='www.hamqth.com', port=443): Max retries "
                                 "exceeded with url: /xml.php?u=N0CALL&p=hunter2 (Caused by ...)")
    msg = hamapp._safe_err(e)
    assert "hunter2" not in msg and "N0CALL" not in msg and "/xml.php?[redacted]" in msg
    assert hamapp._safe_err(ValueError()) == "ValueError"


def test_clublog_lookup_error_does_not_show_the_api_key(monkeypatch):
    monkeypatch.setenv("CLUBLOG_API_KEY", "0123456789abcdef0123456789abcdef01234567")

    def boom(url, params, timeout):
        resp = requests.Response()
        resp.status_code = 403
        resp.url = f"{url}?api={params['api']}&call={params['call']}&full=1"
        resp.raise_for_status()

    monkeypatch.setattr(hamapp.requests, "get", boom)
    out = hamapp.fetch_clublog("W1AW")
    assert out["status"] == "error" and "403" in out["error"]
    assert "0123456789abcdef" not in out["error"]


class _Fcc:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.url = "https://www.fcc.gov/api/license-view/basicSearch/getLicenses?searchValue=W1AW"

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_fcc_refusal_is_unavailable_and_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(hamapp, "_fcc_unavailable_logged", False)
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(headers=headers)
        return _Fcc(403, ValueError("not json"))

    monkeypatch.setattr(hamapp.requests, "get", fake_get)
    with caplog.at_level(logging.INFO):
        first = hamapp.fetch_fcc_history("W1AW")
        hamapp.fetch_fcc_history("K1ABC")
    assert first["status"] == "unavailable" and first["licenses"] == []
    assert seen["headers"]["User-Agent"].startswith("HamLookup/")
    msgs = [r.getMessage() for r in caplog.records if "FCC" in r.getMessage()]
    assert len(msgs) == 1 and "403" in msgs[0] and "searchValue" not in msgs[0]


def test_fcc_still_works_when_it_answers(monkeypatch):
    body = {"Licenses": {"License": {"licenseeName": " ARRL HQ ", "statusDesc": "Active",
                                     "grantDate": "01/01/2020", "expiredDate": "01/01/2030"}}}
    monkeypatch.setattr(hamapp.requests, "get", lambda url, params, headers, timeout: _Fcc(200, body))
    out = hamapp.fetch_fcc_history("W1AW")
    assert out["status"] == "ok" and out["licenses"][0]["name"] == "ARRL HQ"


def test_log_qso_needs_a_json_request(monkeypatch):
    # A form on another website can post to this server from the operator's browser, but it can
    # only send form or text bodies, not JSON: such a request must not log anything.
    monkeypatch.setenv("WAVELOG_URL", "https://wl.example")
    monkeypatch.setenv("WAVELOG_KEY", "k")
    monkeypatch.setattr(hamapp.logsources, "write", lambda *a: (_ for _ in ()).throw(AssertionError("wrote")))
    client = hamapp.app.test_client()
    body = '{"callsign":"W1AW","band":"20m","mode":"SSB","datetime_on":"20261126 130500"}'
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
        assert client.post("/log_qso", data=body, content_type=ctype).status_code == 400


def test_top_contacts_skip_malformed_callsigns():
    recs = [{"call": "W1AW"}, {"call": "X');alert(1);//"}, {"call": "<img src=x>"}]
    assert [c["callsign"] for c in hamapp.log_top_contacts(recs)] == ["W1AW"]


def test_debugger_is_off_unless_asked():
    src = open(hamapp.__file__, encoding="utf-8").read()
    assert "debug=True" not in src


def _basic(user, pw):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_no_password_by_default():
    assert hamapp.app.test_client().get("/").status_code == 200


def test_password_protects_every_route(monkeypatch):
    monkeypatch.setenv("HAMLOOKUP_PASSWORD", "s3cret pässword")
    monkeypatch.setattr(hamapp.time, "sleep", lambda s: None)
    client = hamapp.app.test_client()
    for method, path in [("get", "/"), ("post", "/lookup"), ("post", "/log_qso"), ("get", "/static/icon.svg")]:
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, path
        assert resp.headers["WWW-Authenticate"].startswith('Basic realm="Ham Lookup"')
    assert client.get("/", headers=_basic("anyone", "wrong")).status_code == 401
    assert client.get("/", headers=_basic("anyone", "s3cret pässword")).status_code == 200
    assert client.get("/static/icon.svg", headers=_basic("", "s3cret pässword")).status_code == 200


def test_wrong_password_is_slowed_down_and_logged(monkeypatch, caplog):
    monkeypatch.setenv("HAMLOOKUP_PASSWORD", "right")
    slept = []
    monkeypatch.setattr(hamapp.time, "sleep", slept.append)
    with caplog.at_level(logging.WARNING):
        hamapp.app.test_client().get("/", headers=_basic("x", "guess-123"))
    assert slept == [1] and any("Wrong Ham Lookup password" in r.getMessage() for r in caplog.records)
    assert not any("guess-123" in r.getMessage() for r in caplog.records)   # the guess is never logged
