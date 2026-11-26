"""Glue between the settings, the per-logger clients and the history cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, wavelog
from .adif import parse_adif
from .base import WriteResult
from .history import FetchResult, History

# minutes between refreshes, and between full refreshes for sources that fetch only new QSOs
REFRESH_MIN = {"wavelog": 5, "adif": 5}
FULL_REFRESH_MIN = {}
INCREMENTAL = set()


def _adif_records(chunks) -> list[dict]:
    return [r for chunk in chunks for r in parse_adif(chunk)]


def _fetch_wavelog(cursor) -> FetchResult:
    chunks = wavelog.fetch(config.env("WAVELOG_URL"), config.env("WAVELOG_KEY"), config.wavelog_station_id())
    return FetchResult(_adif_records(chunks))


FETCHERS = {
    "wavelog": _fetch_wavelog,
}


def has_confirmations(name: str | None) -> bool:
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
