"""The log readers (ported from wavelog-flagmap) and how the cache drives them. All offline."""
import json
from urllib.parse import quote

import pytest

from logbooks import clublog, lotw, net, qrz, sources, wrl
from logbooks.adif import parse_adif
from logbooks.history import History


def history(name, tmp_path):
    return sources.make_history(name, tmp_path)


# ── QRZ Logbook ─────────────────────────────────────────────────────────────

QRZ_RECS = [f"<call:5>DL1A{i}<band:3>20m<qso_date:8>2026010{i + 1}<app_qrzlog_logid:3>{100 + i}"
            f"<app_qrzlog_status:1>{'C' if i == 0 else 'N'}<eor>" for i in range(5)]


def fake_qrz(monkeypatch, http, recs, calls):
    def fake_post(url, data, headers, timeout):
        calls.append(data["OPTION"])
        opts = dict(o.split(":") for o in data["OPTION"].split(","))
        after, size = int(opts["AFTERLOGID"]), int(opts["MAX"])
        page = [r for r in recs if int(r.split("<app_qrzlog_logid:3>")[1][:3]) >= after][:size]
        if not page:
            return http("COUNT=0&RESULT=FAIL")  # what QRZ really sends past the end
        return http(f"RESULT=OK&COUNT={len(page)}&ADIF={quote(''.join(page))}")
    monkeypatch.setattr(qrz.requests, "post", fake_post)


def test_qrz_pages_by_logid_and_normalizes(monkeypatch, http):
    calls = []
    fake_qrz(monkeypatch, http, QRZ_RECS, calls)
    out = [qrz.normalize(r) for chunk in qrz.fetch("KEY", page=2) for r in parse_adif(chunk)]
    assert calls == ["MAX:2,AFTERLOGID:0", "MAX:2,AFTERLOGID:102", "MAX:2,AFTERLOGID:104"]
    assert len(out) == 5 and out[0]["qrzcom_qso_download_status"] == "Y"


def test_qrz_cache_fetches_only_new_qsos_between_full_refreshes(monkeypatch, http, tmp_path):
    monkeypatch.setenv("QRZ_LOGBOOK_KEY", "KEY")
    calls = []
    recs = list(QRZ_RECS[:3])
    fake_qrz(monkeypatch, http, recs, calls)
    h = history("qrz", tmp_path)
    assert h.incremental and h.refresh_s == 300 and h.full_refresh_s == 6 * 3600
    assert len(h.get(wait=5).records) == 3
    recs.append(QRZ_RECS[3])
    h._fetched_at -= 301
    h.refresh_now()
    assert calls == ["MAX:250,AFTERLOGID:0", "MAX:250,AFTERLOGID:103"]   # then only new log ids
    assert [r["call"] for r in h.get().records] == ["DL1A0", "DL1A1", "DL1A2", "DL1A3"]


def test_qrz_errors(monkeypatch, http):
    raw = qrz._parse("RESULT=OK&COUNT=1&ADIF=&lt;call:4&gt;K1AB&lt;eor&gt;")   # HTML-escaped
    assert raw["ADIF"] == "<call:4>K1AB<eor>"
    monkeypatch.setattr(qrz.requests, "post", lambda *a, **k: http("RESULT=AUTH&REASON=invalid api key"))
    with pytest.raises(qrz.QrzError, match="subscription"):
        list(qrz.fetch("KEY"))


# ── Club Log ────────────────────────────────────────────────────────────────

def test_clublog_export_and_errors(monkeypatch, http, tmp_path):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, headers=headers)
        return http("<eoh> <CALL:5>DL1AA<QSL_RCVD:1>Y<eor>")

    monkeypatch.setattr(clublog.requests, "post", fake_post)
    monkeypatch.setenv("CLUBLOG_TOKEN", "cl_tok")
    monkeypatch.setenv("CLUBLOG_CALLSIGN", "n0call")
    assert history("clublog", tmp_path).get(wait=5).records == [{"call": "DL1AA", "qsl_rcvd": "Y"}]
    assert seen["url"] == "https://clublog.org/api/v1/logs/N0CALL/adif-export"
    assert seen["headers"]["Authorization"] == "Bearer cl_tok"
    assert seen["headers"]["User-Agent"].startswith("HamLookup")
    err = {"error": {"code": "unauthorized", "message": "Send Authorization: Bearer <token>."}}
    monkeypatch.setattr(clublog.requests, "post", lambda *a, **k: http(err, 401))
    with pytest.raises(clublog.ClublogError, match="401.*CLUBLOG_TOKEN"):
        clublog.fetch("N0CALL", "bad")


# ── LoTW ────────────────────────────────────────────────────────────────────

LOTW_REPORT = ("ARRL Logbook of the World Status Report\n<PROGRAMID:4>LoTW\n<eoh>\n"
               "<CALL:5>DL1AA<BAND:3>20M<QSL_RCVD:1>Y<QSLRDATE:10>2026-01-02<eor>\n"
               "<CALL:4>JA1A<BAND:3>20M<QSL_RCVD:1>N<eor>\n")


def test_lotw_report_is_normalized_and_read_rarely(monkeypatch, http, tmp_path):
    seen = {}

    def fake_get(url, params, headers, timeout):
        seen.update(params=params)
        return http(LOTW_REPORT)

    monkeypatch.setattr(lotw.requests, "get", fake_get)
    monkeypatch.setenv("LOTW_USERNAME", "n0call")
    monkeypatch.setenv("LOTW_PASSWORD", "pw")
    h = history("lotw", tmp_path)
    assert h.refresh_s == 6 * 3600
    recs = h.get(wait=5).records
    assert seen["params"]["qso_qsl"] == "no" and "qso_owncall" not in seen["params"]
    assert recs[0] == {"call": "DL1AA", "band": "20m", "lotw_qsl_rcvd": "Y"}    # LoTW, not a card
    assert recs[1]["lotw_qsl_rcvd"] == "N"


def test_lotw_errors_never_show_the_password(monkeypatch, http):
    monkeypatch.setattr(lotw.requests, "get", lambda *a, **k: http("<html>Username/password incorrect</html>"))
    with pytest.raises(lotw.LotwError) as e:
        lotw.fetch("n0call", "hunter2")
    assert "hunter2" not in str(e.value)

    def boom(*a, **k):
        raise lotw.requests.ConnectionError("https://lotw.arrl.org/...?password=hunter2")
    monkeypatch.setattr(lotw.requests, "get", boom)
    with pytest.raises(lotw.LotwError) as e:
        lotw.fetch("n0call", "hunter2")
    assert "hunter2" not in str(e.value)


# ── World Radio League ──────────────────────────────────────────────────────

def test_wrl_pages_by_cursor_and_maps_contacts(monkeypatch, http, tmp_path):
    pages = {
        None: {"data": [{"call": "DL1AA", "timestamp": "2026-11-20T14:05:00Z", "band": 20, "mode": "FT8",
                         "dxcc": 230, "isDuplicate": False},
                        {"call": "DL1AA", "timestamp": "2026-11-20T14:05:00Z", "band": 20, "isDuplicate": True}],
               "meta": {"nextCursor": "abc"}, "error": None},
        "abc": {"data": [{"call": "JA1XYZ", "timestamp": "2026-11-19T01:00:00Z", "band": 0.7}],
                "meta": {"nextCursor": None}, "error": None},
    }
    monkeypatch.setattr(wrl.requests, "get",
                        lambda url, params, headers, timeout: http(pages[params.get("cursor")]))
    monkeypatch.setenv("WRL_API_KEY", "wrl_live_x")
    recs = history("wrl", tmp_path).get(wait=5).records
    assert recs[0] == {"call": "DL1AA", "qso_date": "20261120", "time_on": "1405", "band": "20m",
                       "mode": "FT8", "dxcc": "230"}
    assert len(recs) == 2 and recs[1]["band"] == "70cm"        # duplicate skipped
    assert not sources.has_confirmations("wrl")


def test_wrl_error(monkeypatch, http):
    body = {"data": None, "meta": None, "error": {"code": "MEMBERSHIP_REQUIRED", "message": "Upgrade"}}
    monkeypatch.setattr(wrl.requests, "get", lambda *a, **k: http(json.dumps(body), 403))
    with pytest.raises(wrl.WrlError, match="MEMBERSHIP_REQUIRED"):
        list(wrl.fetch("k"))


# ── ADIF file or URL ────────────────────────────────────────────────────────

def test_adif_file_relative_to_the_app_folder(monkeypatch, tmp_path):
    log = tmp_path / "cloudlog.adi"
    log.write_text("exported by Cloudlog <eoh>\n<CALL:4>W1AW<BAND:3>40M<eor>\n", encoding="utf-8")
    monkeypatch.setenv("ADIF_PATH", str(log))
    assert history("adif", tmp_path).get(wait=5).records == [{"call": "W1AW", "band": "40m"}]
    assert sources.adif_location("data/log.adi") == str(sources.APP_DIR / "data" / "log.adi")


def test_adif_missing_file_is_an_error_not_a_crash(monkeypatch, tmp_path):
    monkeypatch.setenv("ADIF_PATH", str(tmp_path / "nope.adi"))
    snap = history("adif", tmp_path).get(wait=5)
    assert snap.records == [] and "cannot read ADIF_PATH" in snap.error


def test_adif_url(monkeypatch, http, tmp_path):
    seen = []

    def fake_get(url, headers, timeout):
        seen.append(url)
        return http("<CALL:4>W1AW<eor>")

    monkeypatch.setattr(net.requests, "get", fake_get)
    monkeypatch.setenv("ADIF_PATH", "https://example.org/log.adi")
    assert history("adif", tmp_path).get(wait=5).records == [{"call": "W1AW"}]
    assert seen == ["https://example.org/log.adi"]


def test_refresh_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_REFRESH_MINUTES", "1")
    h = history("lotw", tmp_path)
    assert isinstance(h, History) and h.refresh_s == 60
