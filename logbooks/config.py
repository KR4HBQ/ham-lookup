"""Which logger is read (LOG_SOURCE) and which are written (LOG_WRITE), from environment variables.

Backward compatible: with neither setting, a configured Wavelog (WAVELOG_URL + WAVELOG_KEY) is
both the source and the only writer, as before.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

LABELS = {
    "wavelog": "Wavelog",
    "cloudlog": "Cloudlog",
    "qrz": "QRZ Logbook",
    "clublog": "Club Log",
    "lotw": "LoTW",
    "wrl": "World Radio League",
    "adif": "ADIF file",
}
READERS = ("wavelog", "qrz", "clublog", "lotw", "wrl", "adif")
WRITERS = ("wavelog", "cloudlog", "qrz", "clublog", "wrl")

# settings each logger needs, for reading and for writing
_READ_NEEDS = {
    "wavelog": ("WAVELOG_URL", "WAVELOG_KEY"),
    "qrz": ("QRZ_LOGBOOK_KEY",),
    "clublog": ("CLUBLOG_TOKEN", "CLUBLOG_CALLSIGN"),
    "lotw": ("LOTW_USERNAME", "LOTW_PASSWORD"),
    "wrl": ("WRL_API_KEY",),
    "adif": ("ADIF_PATH",),
}
_WRITE_NEEDS = {
    "wavelog": ("WAVELOG_URL", "WAVELOG_KEY"),
    "cloudlog": ("CLOUDLOG_URL", "CLOUDLOG_KEY", "CLOUDLOG_STATION_ID"),
    "qrz": ("QRZ_LOGBOOK_KEY",),
    "clublog": ("CLUBLOG_EMAIL", "CLUBLOG_APP_PASSWORD", "CLUBLOG_CALLSIGN", "CLUBLOG_API_KEY"),
    "wrl": ("WRL_API_KEY",),
}


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def wavelog_station_id() -> str:
    return env("WAVELOG_STATION_ID", "1")


def _missing(needs: tuple[str, ...]) -> list[str]:
    return [n for n in needs if not env(n)]


def read_missing(name: str) -> list[str]:
    return _missing(_READ_NEEDS.get(name, ()))


def write_missing(name: str) -> list[str]:
    return _missing(_WRITE_NEEDS.get(name, ()))


def _names(raw: str) -> list[str]:
    return [n.strip().lower() for n in raw.split(",") if n.strip()]


def source() -> str | None:
    """The logger that worked-before history comes from, or None (log check off)."""
    raw = os.environ.get("LOG_SOURCE")
    if raw is None or not raw.strip():
        return "wavelog" if not read_missing("wavelog") else None
    name = raw.strip().lower()
    if name in ("none", "off"):
        return None
    if name not in READERS:
        hint = " (Cloudlog has no export API: export ADIF and use LOG_SOURCE=adif)" if name == "cloudlog" else ""
        logger.warning("LOG_SOURCE=%s is not one of %s%s", name, ", ".join(READERS), hint)
        return None
    return name


def writers() -> list[str]:
    """The loggers new QSOs are sent to, in LOG_WRITE order."""
    raw = os.environ.get("LOG_WRITE")
    if raw is None:
        return ["wavelog"] if not write_missing("wavelog") else []
    out = []
    for name in _names(raw):
        if name in ("none", "off"):
            continue
        if name not in WRITERS:
            why = "is read-only" if name in READERS else "is not a known logger"
            logger.warning("LOG_WRITE: %s %s, ignored", name, why)
            continue
        if name not in out:
            out.append(name)
    return out


def label(name: str | None) -> str:
    return LABELS.get(name or "", name or "")
