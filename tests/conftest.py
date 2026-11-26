import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Never let a developer's real .env leak into the tests: app.py calls load_dotenv() on import.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **k: False

LOG_ENV = [
    "LOG_SOURCE", "LOG_WRITE", "LOG_REFRESH_MINUTES", "LOG_FULL_REFRESH_MINUTES", "DATA_DIR",
    "WAVELOG_URL", "WAVELOG_KEY", "WAVELOG_STATION_ID",
    "CLOUDLOG_URL", "CLOUDLOG_KEY", "CLOUDLOG_STATION_ID",
    "QRZ_LOGBOOK_KEY", "CLUBLOG_TOKEN", "CLUBLOG_CALLSIGN", "CLUBLOG_EMAIL", "CLUBLOG_APP_PASSWORD",
    "CLUBLOG_API_KEY", "LOTW_USERNAME", "LOTW_PASSWORD", "LOTW_OWNCALL",
    "WRL_API_KEY", "WRL_LOGBOOK_ID", "WRL_STATION_CALLSIGN", "ADIF_PATH", "MY_GRID", "HAMLOOKUP_PASSWORD",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in LOG_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    yield


class Http:
    """A stand-in for requests.Response."""

    def __init__(self, body="", status_code=200, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.text = self.content.decode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return json.loads(self.content)


@pytest.fixture
def http():
    return Http
