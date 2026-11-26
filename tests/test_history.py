import time

from logbooks.history import FetchResult, History, qso_key


def rec(call, date="20261101", t="1200", band="20m", **kw):
    return {"call": call, "qso_date": date, "time_on": t, "band": band, "mode": "SSB", **kw}


class Source:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, cursor):
        self.calls.append(cursor)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def test_first_get_waits_for_the_load_and_cleans_records(tmp_path):
    src = Source(FetchResult([{"CALL": "w1aw", "BAND": "20M", "QSO_DATE": "20261101", "EMPTY": ""}]))
    h = History("t", src, tmp_path, refresh_s=300)
    snap = h.get(wait=5)
    assert snap.records == [{"call": "W1AW", "band": "20m", "qso_date": "20261101"}]
    assert snap.updated and not snap.error and not snap.loading
    assert h.get().records == snap.records and len(src.calls) == 1   # fresh: no second fetch


def test_cache_survives_a_restart_and_is_shared_through_the_file(tmp_path):
    History("t", Source(FetchResult([rec("W1AW")])), tmp_path, refresh_s=300).get(wait=5)
    src = Source()
    other = History("t", src, tmp_path, refresh_s=300)  # a second worker, or after a restart
    assert [r["call"] for r in other.get(wait=0).records] == ["W1AW"] and src.calls == []


def test_stale_cache_is_returned_at_once_and_refreshed_in_the_background(tmp_path):
    h = History("t", Source(FetchResult([rec("W1AW")]), FetchResult([rec("W1AW"), rec("K1ABC")])),
                tmp_path, refresh_s=300)
    h.get(wait=5)
    h._fetched_at -= 301
    assert len(h.get(wait=0).records) == 1          # old data, no waiting
    h._thread.join(5)
    assert len(h.get(wait=0).records) == 2


def test_a_failed_fetch_keeps_the_old_records_and_backs_off(tmp_path):
    src = Source(FetchResult([rec("W1AW")]), RuntimeError("Wavelog answered HTTP 500"))
    h = History("t", src, tmp_path, refresh_s=300)
    h.get(wait=5)
    h._fetched_at -= 301
    h.refresh_now()
    snap = h.get(wait=0)
    assert [r["call"] for r in snap.records] == ["W1AW"] and "HTTP 500" in snap.error
    assert len(src.calls) == 2                       # no immediate retry


def test_first_load_failure_reports_error_not_loading(tmp_path):
    h = History("t", Source(RuntimeError("bad key")), tmp_path, refresh_s=300)
    snap = h.get(wait=5)
    assert snap.records == [] and snap.error == "bad key" and not snap.loading


def test_logged_qso_shows_at_once_and_is_dropped_when_the_source_has_it(tmp_path):
    new = rec("DL1AA", t="130500")
    h = History("t", Source(FetchResult([rec("W1AW")]), FetchResult([rec("W1AW"), rec("DL1AA", t="1305")])),
                tmp_path, refresh_s=300)
    h.get(wait=5)
    h.add(new)
    assert [r["call"] for r in h.get().records] == ["W1AW", "DL1AA"]
    h._fetched_at -= 301
    h.refresh_now()
    assert [r["call"] for r in h.get().records] == ["W1AW", "DL1AA"]   # once, from the source
    assert h._local == []


def test_logged_qso_persists_until_the_source_catches_up(tmp_path):
    h = History("t", Source(FetchResult([]), FetchResult([])), tmp_path, refresh_s=300)
    h.get(wait=5)
    h.add(rec("DL1AA"))
    h._fetched_at -= 301
    h.refresh_now()
    assert [r["call"] for r in History("t", Source(), tmp_path, refresh_s=300).get(wait=0).records] == ["DL1AA"]


def test_incremental_fetch_between_full_refreshes(tmp_path):
    src = Source(FetchResult([rec("W1AW")], cursor=10),
                 FetchResult([rec("K1ABC"), rec("W1AW")], cursor=12, incremental=True),
                 FetchResult([rec("N0CALL")], cursor=13))
    h = History("t", src, tmp_path, refresh_s=60, full_refresh_s=3600, incremental=True)
    h.get(wait=5)
    h._fetched_at -= 61
    h.refresh_now()
    assert [r["call"] for r in h.get().records] == ["W1AW", "K1ABC"]   # merged, no duplicate
    h._fetched_at -= 61
    h._full_at -= 3601
    h.refresh_now()
    assert src.calls == [None, 10, None]                                # full refresh again
    assert [r["call"] for r in h.get().records] == ["N0CALL"]


def test_qso_key_ignores_seconds_and_case():
    assert qso_key(rec("w1aw", t="120059", band="20M")) == qso_key(rec("W1AW", t="1200"))


def test_disk_file_is_written_atomically(tmp_path):
    History("t", Source(FetchResult([rec("W1AW")])), tmp_path, refresh_s=300).get(wait=5)
    assert (tmp_path / "history-t.json").exists()
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    time.sleep(0)
