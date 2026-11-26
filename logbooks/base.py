"""Types shared by the logbook clients."""
from __future__ import annotations

from dataclasses import dataclass


class LogbookError(RuntimeError):
    """A logbook could not be read or written. The message never contains keys or passwords."""


@dataclass
class WriteResult:
    ok: bool
    message: str = ""
    duplicate: bool = False  # the logger already had this QSO (counts as logged)
