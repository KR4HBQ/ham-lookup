"""Worked-before history: the source log, cached in memory and on disk.

* Lookups never wait for a slow fetch (a LoTW report takes minutes). A stale cache is refreshed in
  a background thread; only the very first load is waited for, briefly.
* The cache is a JSON file under DATA_DIR, so it survives restarts and is shared by gunicorn's
  workers. A lock file makes sure only one worker fetches at a time; the others reload the file.
* Sources that can fetch only new QSOs (QRZ) do that between full refreshes. A full refresh picks up
  edits and new confirmations.
* A QSO logged from Ham Lookup is added at once (``add``), so the card updates before the source has
  it. It stays until the source returns it, or for LOCAL_KEEP_DAYS.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    import fcntl  # not on Windows; there the lock is per process only
except ImportError:  # pragma: no cover
    fcntl = None

logger = logging.getLogger(__name__)

LOCAL_KEEP_DAYS = 30
RETRY_AFTER_ERROR = 60  # seconds


def qso_key(rec: dict) -> tuple:
    """Same QSO across loggers: call, date, HHMM and band (mode names differ between loggers)."""
    return (rec.get("call", "").upper(), rec.get("qso_date", ""),
            rec.get("time_on", "")[:4], rec.get("band", "").lower())


def clean(rec: dict) -> dict:
    out = {k.lower(): str(v).strip() for k, v in rec.items() if v is not None and str(v).strip()}
    if "call" in out:
        out["call"] = out["call"].upper()
    if "band" in out:
        out["band"] = out["band"].lower()
    return out


@dataclass
class FetchResult:
    records: list[dict]
    cursor: object = None       # passed back to the fetcher for an incremental fetch
    incremental: bool = False   # records are only the new ones


# fetcher(cursor) -> FetchResult. cursor is None for a full fetch.
Fetcher = Callable[[object], FetchResult]


@dataclass
class Snapshot:
    records: list[dict] = field(default_factory=list)
    updated: float = 0.0        # when the source was last read successfully
    error: str = ""
    loading: bool = False       # the first load has not finished yet


class History:
    def __init__(self, name: str, fetcher: Fetcher, data_dir: str | Path, *,
                 refresh_s: float, full_refresh_s: float | None = None, incremental: bool = False):
        self.name = name
        self.fetcher = fetcher
        self.path = Path(data_dir) / f"history-{name}.json"
        self.refresh_s = refresh_s
        self.full_refresh_s = full_refresh_s or refresh_s
        self.incremental = incremental
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._records: list[dict] = []
        self._local: list[dict] = []
        self._fetched_at = 0.0
        self._full_at = 0.0
        self._cursor = None
        self._error = ""
        self._next_try = 0.0
        self._mtime = 0.0
        self._loaded = False
        self._load_disk()

    # ── disk ────────────────────────────────────────────────────────────────
    def _load_disk(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime <= self._mtime:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("%s cache unreadable (%s), refetching", self.name, exc)
            return
        self._records = d.get("records") or []
        self._local = d.get("local") or []
        self._fetched_at = float(d.get("fetched_at") or 0)
        self._full_at = float(d.get("full_at") or 0)
        self._cursor = d.get("cursor")
        self._error = d.get("error") or ""
        self._mtime = mtime
        self._loaded = self._loaded or bool(self._fetched_at)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({
            "source": self.name, "fetched_at": self._fetched_at, "full_at": self._full_at,
            "cursor": self._cursor, "error": self._error,
            "records": self._records, "local": self._local,
        }), encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ── reading ─────────────────────────────────────────────────────────────
    def _stale(self, now: float) -> bool:
        return now - self._fetched_at >= self.refresh_s and now >= self._next_try

    def get(self, wait: float = 8.0) -> Snapshot:
        """The current history. Starts a background refresh when it is stale."""
        with self._lock:
            self._load_disk()
            if self._stale(time.time()):
                self._start_refresh()
            first = not self._loaded and self._thread is not None
        if first and wait > 0:
            self._done.wait(wait)
        with self._lock:
            return Snapshot(self._merged(), self._fetched_at, self._error,
                            loading=not self._loaded and not self._error)

    def _merged(self) -> list[dict]:
        if not self._local:
            return list(self._records)
        seen = {qso_key(r) for r in self._records}
        return self._records + [r for r in self._local if qso_key(r) not in seen]

    def add(self, record: dict) -> None:
        """A QSO Ham Lookup just logged."""
        rec = clean(record)
        rec["_added"] = str(int(time.time()))
        with self._lock:
            self._load_disk()
            self._local.append(rec)
            try:
                self._save()
            except OSError as exc:
                logger.warning("%s cache not saved: %s", self.name, exc)

    # ── refreshing ──────────────────────────────────────────────────────────
    def _start_refresh(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._done.clear()
        self._thread = threading.Thread(target=self._refresh, name=f"history-{self.name}", daemon=True)
        self._thread.start()

    def refresh_now(self) -> None:
        """Refresh in this thread (tests, and a first load from the command line)."""
        self._refresh()

    def _refresh(self) -> None:
        lock_file = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if fcntl is not None:
                lock_file = open(self.path.with_name(self.path.name + ".lock"), "w")
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:  # another worker is fetching; it writes the file we then reload
                    lock_file.close()
                    lock_file = None
                    return
            with self._lock:
                self._load_disk()  # another worker may have just refreshed
                now = time.time()
                if not self._stale(now):
                    return
                incremental = (self.incremental and self._cursor is not None
                               and now - self._full_at < self.full_refresh_s)
                cursor = self._cursor if incremental else None
            started = time.time()
            try:
                result = self.fetcher(cursor)
            except Exception as exc:  # noqa: BLE001 - any failure keeps the old cache
                msg = str(exc) or type(exc).__name__
                logger.error("%s fetch failed: %s", self.name, msg)
                with self._lock:
                    self._error = msg
                    self._next_try = time.time() + min(RETRY_AFTER_ERROR, self.refresh_s)
                return
            records = [clean(r) for r in result.records]
            with self._lock:
                if result.incremental and incremental:
                    seen = {qso_key(r) for r in self._records}
                    records = self._records + [r for r in records if qso_key(r) not in seen]
                else:
                    self._full_at = started
                self._records = records
                self._cursor = result.cursor
                self._fetched_at = started
                self._error = ""
                self._next_try = 0.0
                self._loaded = True
                seen = {qso_key(r) for r in records}
                cutoff = time.time() - LOCAL_KEEP_DAYS * 86400
                self._local = [r for r in self._local
                               if qso_key(r) not in seen and float(r.get("_added") or 0) > cutoff]
                try:
                    self._save()
                except OSError as exc:
                    logger.warning("%s cache not saved: %s", self.name, exc)
            logger.info("%s history: %d QSOs (%s, %.1fs)", self.name, len(records),
                        "incremental" if result.incremental and incremental else "full",
                        time.time() - started)
        finally:
            if lock_file is not None:
                lock_file.close()
            self._done.set()
