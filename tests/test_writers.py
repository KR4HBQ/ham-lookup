"""The QSO writers. All offline: requests is replaced by fakes."""
import pytest

from logbooks import clublog, qrz, sources, wavelog, wrl
from logbooks.adif import parse_adif

REC = {"call": "DL1AA", "band": "20m", "mode": "SSB", "qso_date": "20261126", "time_on": "130500",
       "time_off": "130500", "rst_sent": "59", "rst_rcvd": "57", "freq": "14.225", "name": "Hans",
       "gridsquare": "JO62QM", "comment": "nice signal", "notes": "met at Ham Radio", "country": "Germany"}


@pytest.fixture(autouse=True)
def reset_clublog():
    clublog._login_failed = ""
    yield
    clublog._login_failed = ""


# ── Cloudlog (same call as Wavelog) ─────────────────────────────────────────

def test_cloudlog_uses_its_own_url_key_and_station(monkeypatch, http):
    monkeypatch.setenv("CLOUDLOG_URL", "https://cloudlog.example")
    monkeypatch.setenv("CLOUDLOG_KEY", "cl-key")
    monkeypatch.setenv("CLOUDLOG_STATION_ID", "4")
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return http({"status": "created", "adif_count": 1, "adif_errors": 0}, 201)

    monkeypatch.setattr(wavelog.requests, "post", fake_post)
    [res] = sources.write(["cloudlog"], REC)
    assert res["ok"] and res["label"] == "Cloudlog"
    assert seen["url"] == "https://cloudlog.example/index.php/api/qso"
    assert seen["json"]["key"] == "cl-key" and seen["json"]["station_profile_id"] == "4"
    assert list(parse_adif(seen["json"]["string"])) == [REC]


def test_cloudlog_not_configured_names_the_settings():
    [res] = sources.write(["cloudlog"], REC)
    assert not res["ok"] and "CLOUDLOG_URL, CLOUDLOG_KEY, CLOUDLOG_STATION_ID" in res["message"]


# ── QRZ Logbook ─────────────────────────────────────────────────────────────

def test_qrz_insert(monkeypatch, http):
    seen = {}

    def fake_post(url, data, headers, timeout):
        seen.update(url=url, data=data, headers=headers)
        return http("RESULT=OK&LOGID=123456&COUNT=1")

    monkeypatch.setattr(qrz.requests, "post", fake_post)
    res = qrz.insert("KEY", REC)
    assert res.ok and not res.duplicate
    assert seen["url"] == "https://logbook.qrz.com/api"
    assert seen["data"]["ACTION"] == "INSERT" and seen["data"]["KEY"] == "KEY"
    assert list(parse_adif(seen["data"]["ADIF"])) == [REC]
    assert seen["headers"]["User-Agent"].startswith("HamLookup")


@pytest.mark.parametrize("answer,ok,dup,msg", [
    ("RESULT=FAIL&REASON=Unable to add QSO to database: duplicate&EXTENDED=", True, True, "already"),
    ("RESULT=FAIL&REASON=wrong station_callsign for this logbook", False, False, "station_callsign"),
    ("RESULT=AUTH&REASON=invalid api key", False, False, "QRZ_LOGBOOK_KEY"),
])
def test_qrz_insert_answers(monkeypatch, http, answer, ok, dup, msg):
    monkeypatch.setattr(qrz.requests, "post", lambda *a, **k: http(answer))
    res = qrz.insert("KEY", REC)
    assert (res.ok, res.duplicate) == (ok, dup) and msg in res.message


# ── Club Log ────────────────────────────────────────────────────────────────

def test_clublog_realtime_upload(monkeypatch, http):
    seen = {}

    def fake_post(url, data, headers, timeout):
        seen.update(url=url, data=data)
        return http("OK")

    monkeypatch.setattr(clublog.requests, "post", fake_post)
    for name, value in [("CLUBLOG_EMAIL", "me@example.org"), ("CLUBLOG_APP_PASSWORD", "app-pw"),
                        ("CLUBLOG_CALLSIGN", "n0call"), ("CLUBLOG_API_KEY", "a" * 40)]:
        monkeypatch.setenv(name, value)
    [res] = sources.write(["clublog"], REC)
    assert res["ok"] and res["label"] == "Club Log"
    assert seen["url"] == "https://clublog.org/realtime.php"
    assert seen["data"]["callsign"] == "N0CALL" and seen["data"]["password"] == "app-pw"
    assert seen["data"]["api"] == "a" * 40 and seen["data"]["email"] == "me@example.org"
    assert list(parse_adif(seen["data"]["adif"])) == [REC]


def test_clublog_duplicate_is_logged(monkeypatch, http):
    monkeypatch.setattr(clublog.requests, "post", lambda *a, **k: http("Dupe"))
    res = clublog.insert("e", "p", "N0CALL", "k", REC)
    assert res.ok and res.duplicate


def test_clublog_stops_after_a_login_failure(monkeypatch, http):
    calls = []
    monkeypatch.setattr(clublog.requests, "post", lambda *a, **k: calls.append(1) or http("Login rejected", 403))
    first = clublog.insert("e", "wrong", "N0CALL", "k", REC)
    second = clublog.insert("e", "wrong", "N0CALL", "k", REC)
    assert not first.ok and "Application" in first.message and "wrong" not in first.message
    assert not second.ok and "not retried" in second.message
    assert len(calls) == 1                               # Club Log firewalls repeated failed logins


def test_clublog_rejected_record(monkeypatch, http):
    monkeypatch.setattr(clublog.requests, "post", lambda *a, **k: http("Rejected: invalid band", 400))
    res = clublog.insert("e", "p", "N0CALL", "k", REC)
    assert not res.ok and res.message == "HTTP 400 (Rejected: invalid band)"
    assert not clublog._login_failed


# ── World Radio League ──────────────────────────────────────────────────────

def test_wrl_contact_has_only_fields_wrl_accepts():
    c = wrl.to_contact(REC, my_grid="EM72", station_callsign="N0CALL")
    assert c == {"programId": "HamLookup", "call": "DL1AA",
                 "timestamp": {"qsoDate": "20261126", "timeOn": "130500"}, "freq": 14.225,
                 "band": "20m", "mode": "SSB", "rstSent": "59", "rstRcvd": "57",
                 "notes": "nice signal / met at Ham Radio", "name": "Hans", "stationCallsign": "N0CALL",
                 "gridsquare": "JO62QM", "myGridsquare": "EM72"}
    assert wrl.to_contact({**REC, "mode": "MFSK", "submode": "FT4", "gridsquare": "bogus"})["mode"] == "FT4"
    assert "gridsquare" not in wrl.to_contact({**REC, "gridsquare": "bogus"})


def test_wrl_insert(monkeypatch, http):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return http({"data": {"id": "x", "enrichment": "pending"}, "meta": {}, "error": None}, 201)

    monkeypatch.setattr(wrl.requests, "post", fake_post)
    monkeypatch.setenv("WRL_API_KEY", "wrl_live_x")
    monkeypatch.setenv("MY_GRID", "AA00aa")                  # the .env.example placeholder is not sent
    [res] = sources.write(["wrl"], REC)
    assert res["ok"]
    assert seen["url"] == "https://api.worldradioleague.com/v1/contacts"
    assert seen["headers"]["Authorization"] == "Bearer wrl_live_x"
    assert "myGridsquare" not in seen["json"]


def test_wrl_needs_a_frequency(monkeypatch):
    monkeypatch.setattr(wrl.requests, "post", lambda *a, **k: pytest.fail("must not be sent"))
    res = wrl.insert("k", {**REC, "freq": ""})
    assert not res.ok and "frequency" in res.message


@pytest.mark.parametrize("status,error,ok,dup,msg", [
    (409, {"code": "DUPLICATE_CONTACT", "message": "This QSO is already in the operator's log."}, True, True, "already"),
    (422, {"code": "LOGBOOK_REQUIRED", "message": "several logbooks", "hint": "Send `logbookId`"}, False, False,
     "LOGBOOK_REQUIRED: several logbooks (Send `logbookId`)"),
    (401, {"code": "INVALID_KEY", "message": "The API key is not valid."}, False, False, "INVALID_KEY"),
])
def test_wrl_insert_errors(monkeypatch, http, status, error, ok, dup, msg):
    monkeypatch.setattr(wrl.requests, "post",
                        lambda *a, **k: http({"data": None, "meta": None, "error": error}, status))
    res = wrl.insert("k", REC)
    assert (res.ok, res.duplicate) == (ok, dup) and msg in res.message


def test_every_writer_in_the_settings_has_an_implementation():
    from logbooks import config
    assert set(config.WRITERS) == set(sources.WRITERS)
    assert set(config.READERS) == set(sources.FETCHERS)
