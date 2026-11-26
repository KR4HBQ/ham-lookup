import pytest

from logbooks import config, wavelog
from logbooks.adif import parse_adif, to_adif
from logbooks.base import LogbookError


def test_to_adif_round_trip_and_skips_empty_fields():
    s = to_adif({"call": "W1AW", "name": "Hiram Percy", "comment": "", "freq": "14.225"})
    assert s == "<CALL:4>W1AW<NAME:11>Hiram Percy<FREQ:6>14.225<eor>"
    assert list(parse_adif(s)) == [{"call": "W1AW", "name": "Hiram Percy", "freq": "14.225"}]


# ── settings ────────────────────────────────────────────────────────────────

def test_existing_wavelog_env_keeps_working_without_new_settings(monkeypatch):
    monkeypatch.setenv("WAVELOG_URL", "https://wl.example")
    monkeypatch.setenv("WAVELOG_KEY", "k")
    assert config.source() == "wavelog"
    assert config.writers() == ["wavelog"]
    assert config.wavelog_station_id() == "1"


def test_nothing_configured_means_no_log_check_and_no_log_button():
    assert config.source() is None and config.writers() == []


def test_log_write_list_order_dedupe_and_read_only_names(monkeypatch):
    monkeypatch.setenv("LOG_WRITE", " Wavelog, qrz ,lotw, adif, bogus, qrz")
    assert config.writers() == ["wavelog", "qrz"]
    monkeypatch.setenv("LOG_WRITE", "")
    assert config.writers() == []                      # explicitly empty: button hidden


def test_log_source_choices(monkeypatch):
    monkeypatch.setenv("WAVELOG_URL", "https://wl.example")
    monkeypatch.setenv("WAVELOG_KEY", "k")
    monkeypatch.setenv("LOG_SOURCE", "LoTW")
    assert config.source() == "lotw"
    assert config.read_missing("lotw") == ["LOTW_USERNAME", "LOTW_PASSWORD"]
    monkeypatch.setenv("LOG_SOURCE", "none")
    assert config.source() is None
    monkeypatch.setenv("LOG_SOURCE", "cloudlog")       # no export API
    assert config.source() is None


# ── Wavelog client ──────────────────────────────────────────────────────────

def test_fetch_pages_with_fetchfromid_and_sends_the_station(monkeypatch, http):
    pages = [{"status": "successful", "lastfetchedid": "10", "adif": "<call:4>W1AW<eor>"},
             {"status": "successful", "lastfetchedid": 20, "adif": "<call:5>K1ABC<eor>"},
             {"status": "successful", "lastfetchedid": 20, "adif": None}]
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json))
        return http(pages[len(calls) - 1])

    monkeypatch.setattr(wavelog.requests, "post", fake_post)
    chunks = list(wavelog.fetch("https://wl.example/index.php/", "k", "3"))
    assert len(chunks) == 2
    assert calls[0][0] == "https://wl.example/index.php/api/get_contacts_adif"
    assert [c[1]["fetchfromid"] for c in calls] == [0, 10, 20]
    assert calls[0][1]["station_id"] == "3" and calls[0][1]["key"] == "k"


def test_fetch_error_reason_and_no_key_in_messages(monkeypatch, http):
    monkeypatch.setattr(wavelog.requests, "post",
                        lambda *a, **k: http({"status": "failed", "reason": "missing api key"}, 401))
    with pytest.raises(LogbookError, match="401.*missing api key"):
        list(wavelog.fetch("https://wl.example", "sekret", 1))

    def boom(*a, **k):
        raise wavelog.requests.ConnectionError("https://wl.example key=sekret")
    monkeypatch.setattr(wavelog.requests, "post", boom)
    with pytest.raises(LogbookError) as e:
        list(wavelog.fetch("https://wl.example", "sekret", 1))
    assert "sekret" not in str(e.value)


def test_insert_sends_adif_to_api_qso(monkeypatch, http):
    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json)
        return http({"status": "created", "adif_count": 1, "adif_errors": 0, "messages": []}, 201)

    monkeypatch.setattr(wavelog.requests, "post", fake_post)
    res = wavelog.insert("https://wl.example/", "k", "2", {"call": "W1AW", "band": "20m"})
    assert res.ok and not res.duplicate
    assert seen["url"] == "https://wl.example/index.php/api/qso"
    assert seen["json"] == {"key": "k", "station_profile_id": "2", "type": "adif",
                            "string": "<CALL:4>W1AW<BAND:3>20m<eor>"}


@pytest.mark.parametrize("body,status,ok,dup,msg", [
    ({"status": "created", "adif_errors": 1, "messages": ["Duplicate for W1AW"]}, 201, True, True, "already"),
    ({"status": "abort", "adif_errors": 1, "messages": ["Wrong station callsign"]}, 400, False, False, "Wrong station"),
    ({"status": "failed", "reason": "missing api key"}, 401, False, False, "401 (missing api key)"),
    ("<html>oops</html>", 500, False, False, "HTTP 500"),
])
def test_insert_failures_and_duplicates(monkeypatch, http, body, status, ok, dup, msg):
    monkeypatch.setattr(wavelog.requests, "post", lambda *a, **k: http(body, status))
    res = wavelog.insert("https://wl.example", "k", 1, {"call": "W1AW"})
    assert (res.ok, res.duplicate) == (ok, dup) and msg in res.message
