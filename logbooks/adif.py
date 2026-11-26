"""Minimal, dependency-free ADIF (.adi) reader and writer (reader from wavelog-flagmap).

``parse_adif`` yields one lower-cased field dict per record. It handles <FIELD:LEN[:TYPE]>value,
<EOH>, <EOR>, and arbitrary whitespace/newlines. Field lengths are in characters.
``to_adif`` turns such a dict back into one ADIF record.
"""
from __future__ import annotations

import re
from typing import Iterator

_TAG = re.compile(r"<([A-Za-z0-9_]+)(?::(\d+))?(?::[A-Za-z]+)?>")


def parse_adif(text: str) -> Iterator[dict[str, str]]:
    """Yield one dict per QSO record (header is skipped)."""
    pos = 0
    record: dict[str, str] = {}
    in_header = not text.lstrip().startswith("<") or "<eoh>" in text[:4096].lower()
    while True:
        m = _TAG.search(text, pos)
        if not m:
            break
        name = m.group(1).lower()
        pos = m.end()
        if name == "eoh":
            in_header = False
            record = {}
            continue
        if name == "eor":
            if record and not in_header:
                yield record
            record = {}
            continue
        length = int(m.group(2)) if m.group(2) else 0
        value = text[pos:pos + length]
        pos += length
        if not in_header:
            record[name] = value.strip()
    if record and not in_header:  # file without trailing <eor>
        yield record


def to_adif(record: dict[str, str]) -> str:
    """One ADIF record (upper-case field names, empty fields left out), ending in <eor>."""
    out = []
    for name, value in record.items():
        v = str(value).strip() if value is not None else ""
        if v:
            out.append(f"<{name.upper()}:{len(v)}>{v}")
    return "".join(out) + "<eor>"
