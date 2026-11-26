"""Glue between the settings, the per-logger clients and the history cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import clublog, config, lotw, net, qrz, wavelog, wrl
from .adif import parse_adif
from .base import WriteResult
from .history import FetchResult, History

# minutes between refreshes, and between full refreshes for sources that fetch only new QSOs
# QRZ fetches only new QSOs every 5 minutes and the whole log every 6 hours (new confirmations).
# LoTW builds the whole report for every request (minutes for a big log), so it is read rarely.
REFRESH_MIN = {"wavelog": 5, "adif": 5, "qrz": 5, "clublog": 60, "wrl": 60, "lotw": 360}
FULL_REFRESH_MIN = {"qrz": 360}
INCREMENTAL = {"qrz"}
APP_DIR = Path(__file__).resolve().parents[1]


def _adif_records(chunks) -> list[dict]:
    return [r for chunk in chunks for r in parse_adif(chunk)]


def _fetch_wavelog(cursor) -> FetchResult:
    chunks = wavelog.fetch(config.env("WAVELOG_URL"), config.env("WAVELOG_KEY"), config.wavelog_station_id())
    return FetchResult(_adif_records(chunks))


def _fetch_qrz(cursor) -> FetchResult:
    after = int(cursor or 0)
    recs = [qrz.normalize(r) for r in _adif_records(qrz.fetch(config.env("QRZ_LOGBOOK_KEY"), after=after))]
    ids = [int(r["app_qrzlog_logid"]) for r in recs if r.get("app_qrzlog_logid", "").isdigit()]
    return FetchResult(recs, cursor=max(ids + [after - 1]) + 1, incremental=after > 0)


def _fetch_clublog(cursor) -> FetchResult:
    text = clublog.fetch(config.env("CLUBLOG_CALLSIGN"), config.env("CLUBLOG_TOKEN"))
    return FetchResult(list(parse_adif(text)))


def _fetch_lotw(cursor) -> FetchResult:
    text = lotw.fetch(config.env("LOTW_USERNAME"), config.env("LOTW_PASSWORD"),
                      owncall=config.env("LOTW_OWNCALL") or None)
    return FetchResult([lotw.normalize(r) for r in parse_adif(text)])


def _fetch_wrl(cursor) -> FetchResult:
    return FetchResult(list(wrl.fetch(config.env("WRL_API_KEY"))))


def adif_location(raw: str) -> str:
    """ADIF_PATH: an http(s) URL, or a file path (relative paths are relative to the app folder)."""
    if raw.lower().startswith(("http://", "https://")):
        return raw
    p = Path(raw).expanduser()
    return str(p if p.is_absolute() else APP_DIR / p)


def _fetch_adif(cursor) -> FetchResult:
    where = adif_location(config.env("ADIF_PATH"))
    if where.lower().startswith(("http://", "https://")):
        text = net.fetch_text(where)
    else:
        try:
            text = Path(where).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise net.FetchError(f"cannot read ADIF_PATH {where}: {exc.strerror or exc}") from None
    return FetchResult(list(parse_adif(text)))


FETCHERS = {
    "wavelog": _fetch_wavelog,
    "qrz": _fetch_qrz,
    "clublog": _fetch_clublog,
    "lotw": _fetch_lotw,
    "wrl": _fetch_wrl,
    "adif": _fetch_adif,
}


def has_confirmations(name: str | None) -> bool:
    """WRL contacts carry no QSL status, so its log check shows no confirmations."""
    return name in ("wavelog", "qrz", "clublog", "lotw", "adif")


def make_history(name: str, data_dir: str | Path | None = None) -> History:
    data_dir = data_dir or config.env("DATA_DIR") or Path(__file__).resolve().parents[1] / "data"
    minutes = float(config.env("LOG_REFRESH_MINUTES") or REFRESH_MIN.get(name, 60))
    full = float(config.env("LOG_FULL_REFRESH_MINUTES") or FULL_REFRESH_MIN.get(name, minutes))
    return History(name, FETCHERS[name], data_dir, refresh_s=minutes * 60,
                   full_refresh_s=max(full, minutes) * 60, incremental=name in INCREMENTAL)


# ── writing ─────────────────────────────────────────────────────────────────

def _write_wavelog(record: dict) -> WriteResult:
    return wavelog.insert(config.env("WAVELOG_URL"), config.env("WAVELOG_KEY"),
                          config.wavelog_station_id(), record)


WRITERS = {
    "wavelog": _write_wavelog,
}


def write(names: list[str], record: dict) -> list[dict]:
    """Send one QSO to every logger in ``names`` (in parallel). One result per logger, in order."""
    def one(name: str) -> dict:
        out = {"logger": name, "label": config.label(name), "ok": False, "duplicate": False, "message": ""}
        missing = config.write_missing(name)
        if missing:
            out["message"] = "not configured (set " + ", ".join(missing) + ")"
        elif name not in WRITERS:
            out["message"] = "cannot log QSOs"
        else:
            try:
                res = WRITERS[name](dict(record))
            except Exception as exc:  # noqa: BLE001 - one failing logger must not stop the others
                res = WriteResult(False, str(exc) or type(exc).__name__)
            out.update(ok=res.ok, duplicate=res.duplicate, message=res.message)
        return out

    if not names:
        return []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return list(pool.map(one, names))
