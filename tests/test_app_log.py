import pytest

import app as hamapp
from logbooks import sources
from logbooks.base import WriteResult
from logbooks.history import FetchResult

FORM = {"callsign": "dl1aa", "band": "20m", "mode": "ssb", "datetime_on": "20261126 130500",
        "freq": "14.225", "gridsquare": "jo62qm", "comment": "", "name": "Hans"}


@pytest.fixture(autouse=True)
def fresh_histories():
    hamapp._histories.clear()
    yield
    hamapp._histories.clear()


@pytest.fixture
def client():
    return hamapp.app.test_client()


@pytest.fixture
def wavelog_env(monkeypatch):
    monkeypatch.setenv("WAVELOG_URL", "https://wl.example")
    monkeypatch.setenv("WAVELOG_KEY", "k")


def fake_source(monkeypatch, name, records):
    monkeypatch.setitem(sources.FETCHERS, name, lambda cursor: FetchResult(records))


def test_build_record_defaults_and_validation():
    rec, err = hamapp.build_qso_record(FORM)
    assert not err
    assert rec == {"call": "DL1AA", "band": "20m", "mode": "SSB", "qso_date": "20261126",
                   "time_on": "130500", "time_off": "130500", "rst_sent": "59", "rst_rcvd": "59",
                   "freq": "14.225", "name": "Hans", "gridsquare": "JO62QM"}
    assert list(rec)[:8] == ["call", "band", "mode", "qso_date", "time_on", "time_off", "rst_sent", "rst_rcvd"]
    assert hamapp.build_qso_record({**FORM, "mode": "CW"})[0]["rst_sent"] == "599"
    assert "required" in hamapp.build_qso_record({**FORM, "band": ""})[1]
    assert "YYYYMMDD" in hamapp.build_qso_record({**FORM, "datetime_on": "2026-11-26 13:05"})[1]


def test_log_check_from_the_cache(monkeypatch, wavelog_env):
    fake_source(monkeypatch, "wavelog", [
        {"call": "DL1AA", "qso_date": "20251001", "time_on": "0900", "band": "40M", "mode": "CW",
         "lotw_qsl_rcvd": "Y", "name": "Hans"},
        {"call": "DL1AA", "qso_date": "20261001", "time_on": "0900", "band": "20m", "mode": "MFSK",
         "submode": "FT4"},
        {"call": "W1AW", "qso_date": "20261002", "time_on": "1000", "band": "20m", "mode": "SSB"},
    ])
    info = hamapp.log_check("dl1aa")
    assert info["status"] == "ok" and info["worked"] and info["label"] == "Wavelog"
    assert info["total_qsos"] == 3 and info["name"] == "Hans" and info["has_confirmations"]
    assert [(q["date"], q["band"], q["mode"], q["confirmed"]) for q in info["qsos"]] == [
        ("20261001", "20m", "FT4", False), ("20251001", "40m", "CW", True)]
    assert info["top_contacts"][0] == {"callsign": "DL1AA", "count": 2, "last_date": "20261001", "name": "Hans"}
    assert not hamapp.log_check("K1ABC")["worked"]


def test_log_check_not_configured_and_missing_settings(monkeypatch):
    assert hamapp.log_check("W1AW")["status"] == "not_configured"
    monkeypatch.setenv("LOG_SOURCE", "qrz")
    info = hamapp.log_check("W1AW")
    assert info["status"] == "not_configured" and "QRZ_LOGBOOK_KEY" in info["error"]


def test_log_check_error_when_the_first_load_fails(monkeypatch, wavelog_env):
    def boom(cursor):
        raise RuntimeError("Wavelog answered HTTP 401 (missing api key)")
    monkeypatch.setitem(sources.FETCHERS, "wavelog", boom)
    info = hamapp.log_check("W1AW")
    assert info["status"] == "error" and "401" in info["error"]


def test_index_shows_log_button_only_with_writers(client, monkeypatch, wavelog_env):
    page = client.get("/").get_data(as_text=True)
    assert '"label": "Wavelog"' in page and 'window.LOG_URL     = "https://wl.example"' in page
    monkeypatch.setenv("LOG_WRITE", "")
    assert "window.LOG_WRITERS = []" in client.get("/").get_data(as_text=True)


def test_log_qso_unchanged_wavelog_path(client, monkeypatch, wavelog_env, http):
    fake_source(monkeypatch, "wavelog", [])
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return http({"status": "created", "adif_count": 1, "adif_errors": 0}, 201)

    monkeypatch.setattr(sources.wavelog.requests, "post", fake_post)
    resp = client.post("/log_qso", json=FORM)
    assert resp.status_code == 200 and resp.get_json()["status"] == "ok"
    assert seen["url"] == "https://wl.example/index.php/api/qso"
    assert seen["json"]["station_profile_id"] == "1"
    assert seen["json"]["string"].startswith("<CALL:5>DL1AA<BAND:3>20m<MODE:3>SSB<QSO_DATE:8>20261126")
    assert hamapp.log_check("DL1AA")["worked"]     # in the cache at once


def test_log_qso_fans_out_and_reports_each_logger(client, monkeypatch, wavelog_env):
    monkeypatch.setenv("LOG_WRITE", "wavelog,qrz")
    monkeypatch.setenv("QRZ_LOGBOOK_KEY", "q")
    fake_source(monkeypatch, "wavelog", [])
    got = []
    monkeypatch.setitem(sources.WRITERS, "wavelog", lambda r: got.append(("wavelog", r)) or WriteResult(True))
    monkeypatch.setitem(sources.WRITERS, "qrz", lambda r: WriteResult(False, "invalid api key"))
    resp = client.post("/log_qso", json=FORM)
    body = resp.get_json()
    assert resp.status_code == 200 and body["status"] == "partial"
    assert [(r["logger"], r["ok"]) for r in body["results"]] == [("wavelog", True), ("qrz", False)]
    assert "QRZ Logbook: invalid api key" in body["error"]
    assert got[0][1]["call"] == "DL1AA"


def test_log_qso_all_failed_and_unconfigured_logger(client, monkeypatch, wavelog_env):
    monkeypatch.setenv("LOG_WRITE", "wavelog,cloudlog")
    monkeypatch.setitem(sources.WRITERS, "wavelog", lambda r: WriteResult(False, "HTTP 500"))
    resp = client.post("/log_qso", json=FORM)
    body = resp.get_json()
    assert resp.status_code == 502 and body["status"] == "error"
    assert "CLOUDLOG_URL" in body["results"][1]["message"]


def test_log_qso_without_writers(client):
    assert client.post("/log_qso", json=FORM).status_code == 503


def test_one_logger_raising_does_not_stop_the_others(monkeypatch, wavelog_env):
    monkeypatch.setenv("QRZ_LOGBOOK_KEY", "q")
    monkeypatch.setitem(sources.WRITERS, "wavelog", lambda r: (_ for _ in ()).throw(ValueError("boom")))
    monkeypatch.setitem(sources.WRITERS, "qrz", lambda r: WriteResult(True))
    out = sources.write(["wavelog", "qrz"], {"call": "W1AW"})
    assert [(r["ok"], r["message"]) for r in out] == [(False, "boom"), (True, "")]
